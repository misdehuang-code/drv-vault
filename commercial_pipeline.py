"""
驱匣 · 商业级常驻流水线（唯一业务入口）

Link Curation 模式（严禁本地下载驱动/固件包）：
  1. DEMAND      需求发现：论坛/问答 → 清洗 → 需求型号白名单
  2. ACQUIRE     寻址：结构化索引抓取（sitemap/目录/Releases/IA metadata）→ sources.txt
  3. FULFILL     宽松入库：线索/兜底强制写入双写目录（不经严格 HEAD 验活）
  4. INTENT      搜索意图：Ollama 英文 SEO / 痛点文案（写 JSON，不落盘）
  5. MERCHANDIZE 商品化：commercial_catalog.json（导航索引，无本地路径）

核心产物：vault_index.json + commercial_catalog.json
字段：model / category / description / original_direct_url（+ Content-Length 元数据）
强制 local_storage=false，禁止 data/vault 包体。
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from json_io import read_json, write_json
from link_only_guard import assert_link_only_pipeline, catalog_sku_row

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
DEFAULT_WHITELIST = ROOT / "data" / "急需设备型号白名单.json"
DEFAULT_SOURCES = ROOT / "sources.txt"
DEFAULT_FAILED = ROOT / "failed_sources.txt"
DEFAULT_INDEX = ROOT / "vault_index.json"
DEFAULT_SEARCH_RULES = ROOT / "data" / "search_rules.txt"
DEFAULT_LONGTAIL_CACHE = ROOT / "data" / "longtail_queries.json"
COMMERCIAL_CATALOG = ROOT / "data" / "commercial_catalog.json"

PHASES = (
    ("DEMAND", "需求发现 · 论坛/问答 → 需求型号"),
    ("ACQUIRE", "寻址 · 抓取原始直链 URL"),
    ("FULFILL", "宽松入库 · Soft-Fallback 强制上架（不经严格验活）"),
    ("INTENT", "搜索意图 · Ollama 英文 SEO/痛点"),
    ("MERCHANDIZE", "商品化 · 纯 JSON 导航目录"),
)

WEB_CATALOG = ROOT / "web" / "data" / "commercial_catalog.json"


class PipelineStopped(Exception):
    """用户暂停。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check(stop: Optional[Callable[[], bool]], phase: str) -> None:
    if stop and stop():
        raise PipelineStopped(f"用户暂停于阶段 {phase}")


def _log(append: Optional[Callable[[str], None]], msg: str) -> None:
    print(msg, flush=True)
    if append:
        append(msg)


def purge_legacy_workspace(
    *,
    index_path: Path = DEFAULT_INDEX,
    sources_path: Path = DEFAULT_SOURCES,
    failed_path: Path = DEFAULT_FAILED,
    whitelist_path: Path = DEFAULT_WHITELIST,
    catalog_path: Path = COMMERCIAL_CATALOG,
    search_rules_path: Path = DEFAULT_SEARCH_RULES,
    longtail_cache: Path = DEFAULT_LONGTAIL_CACHE,
    legacy_vault: Path | None = None,
) -> dict[str, Any]:
    """清空遗留本地下载目录与旧索引，留给 Link Curation 从零重建。"""
    import shutil

    vault = legacy_vault or (ROOT / "data" / "vault")
    removed_dirs = 0
    removed_bytes = 0
    if vault.exists():
        for child in list(vault.rglob("*")):
            if child.is_file():
                try:
                    removed_bytes += child.stat().st_size
                except OSError:
                    pass
        for child in list(vault.iterdir()):
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
                removed_dirs += 1
            else:
                child.unlink(missing_ok=True)
        shutil.rmtree(vault, ignore_errors=True)

    for p in (
        index_path,
        failed_path,
        catalog_path,
        WEB_CATALOG,
        search_rules_path,
        longtail_cache,
    ):
        if p.exists():
            p.unlink(missing_ok=True)

    sources_path.write_text(
        "# Link Curation 直链队列（验链，不本地下载）\n"
        "# url|hardware_category|device_name|os_version|pain_points|value_score\n",
        encoding="utf-8",
    )
    write_json(
        whitelist_path,
        {
            "generated_at": None,
            "title": "急需设备型号白名单",
            "sort_by": "value_score_desc",
            "scoring_model": "commercial_v1",
            "total_models": 0,
            "items": [],
        },
    )
    return {
        "removed_vault_dirs": removed_dirs,
        "removed_bytes": removed_bytes,
        "removed_gb": round(removed_bytes / (1024**3), 3),
        "reset_at": _utc_now(),
    }


def _phase_demand(
    *,
    round_no: int,
    max_per_query: int,
    sources: str,
    clean_forum_limit: int,
    use_ai: bool,
    stop: Optional[Callable[[], bool]],
    append: Optional[Callable[[str], None]],
) -> dict[str, Any]:
    _check(stop, "DEMAND")
    import crawler_forums as cf
    from cleaner_forum import (
        build_whitelist,
        load_ai_config,
        rescore_cleaned as rescore_forum,
        run_analysis,
        write_whitelist,
    )
    from crawler_forums import collect_hits, save_hits
    from database import Database
    from utils import create_session
    from catalog_rules import normalize_category, normalize_model

    # 默认 GitHub + Reddit：持续挖需求帖；SE 无 key 易 400/429，仅显式开启
    source_list = [s.strip() for s in sources.split(",") if s.strip()] or [
        "github",
        "reddit",
    ]
    if cf._se_in_cooldown():
        source_list = [s for s in source_list if s != "stackexchange"]
        _log(append, "[DEMAND] StackExchange 冷却中，本轮跳过 SE")
    # GitHub 优先；保留 reddit 以扩大论坛覆盖
    preferred = [s for s in ("github", "reddit") if s in source_list]
    source_list = preferred + [s for s in source_list if s not in preferred]
    cf.DELAY_RANGE = (2.5, 5.5)

    def _forum_once(src_list: list[str], per_q: int) -> dict[str, int]:
        session = create_session(
            {
                "User-Agent": "DrvVaultCommercial/1.2 (demand; link-curation)",
                "Accept": "application/json",
            }
        )
        try:
            hits = collect_hits(
                session,
                sources=src_list,
                max_per_query=max(1, min(per_q, 8)),
                polite=True,
                should_stop=stop,
            )
            with Database() as db:
                return save_hits(db, hits, dry_run=False)
        finally:
            session.close()

    # 白名单已充足时少抓帖；但「型号几乎都已上架」时必须继续抓新型号
    wl_existing = _whitelist_count()
    skip_crawl = False
    fresh_uncataloged = 0
    try:
        from seeker_agent import cataloged_models, load_whitelist_targets

        fresh_uncataloged = len(
            load_whitelist_targets(
                DEFAULT_WHITELIST,
                limit=80,
                skip_models=cataloged_models(),
                prefer_new=True,
            )
        )
    except Exception:
        fresh_uncataloged = 99
    if fresh_uncataloged <= 6:
        skip_crawl = False
        _log(
            append,
            f"[DEMAND] 白名单未上架仅剩 {fresh_uncataloged}，强制抓帖扩需求",
        )
    elif wl_existing >= 40 and (round_no % 3 != 0):
        skip_crawl = True
    elif wl_existing >= 20 and round_no > 1 and (round_no % 2 == 0):
        skip_crawl = True
    if skip_crawl:
        _log(
            append,
            f"[DEMAND] 白名单已有 {wl_existing}，本轮跳过抓帖（第 {round_no} 轮节流）",
        )
        data = read_json(DEFAULT_WHITELIST, default={}) or {}
        return {
            "skipped": True,
            "reason": "whitelist_sufficient",
            "whitelist_models": len(data.get("items") or []),
            "crawl": {"saved": 0, "skipped": True},
        }

    # 白名单够用时缩短礼貌延迟，少在「跳过-已存在」上烧时间
    if wl_existing >= 20:
        cf.DELAY_RANGE = (0.8, 1.6)

    crawl_stats = _forum_once(source_list, max(3, max_per_query))
    if stop and stop():
        raise PipelineStopped("用户暂停于 DEMAND 抓取后")
    saved_n = int(crawl_stats.get("saved") or 0)
    wl_n = _whitelist_count()
    # 白名单已够且本轮无新帖：禁止「再补一轮」空刷 GitHub（全是跳过-已存在）
    if saved_n == 0 and wl_n >= 20:
        _log(append, f"[DEMAND] 无新帖且白名单已有 {wl_n}，跳过补抓，直接用现有需求")
    elif saved_n == 0:
        _log(append, "[DEMAND] 无新增帖 → GitHub 再补一轮")
        retry = _forum_once(["github"], max_per_query + 2)
        crawl_stats = {
            "first": crawl_stats,
            "retry": retry,
            "saved": int((retry or {}).get("saved") or 0),
        }
        saved_n = int(crawl_stats["saved"] or 0)

    _check(stop, "DEMAND")
    config = load_ai_config()
    with Database() as db:
        rs = rescore_forum(db)
        # 无新帖时清洗额度降到极小，避免 AI/规则空扫
        clean_lim = (
            min(20, clean_forum_limit)
            if saved_n == 0 and wl_n >= 20
            else max(20, clean_forum_limit)
        )
        clean_stats = run_analysis(
            db,
            limit=clean_lim,
            offline=not use_ai or (saved_n == 0 and wl_n >= 20),
            config=config,
            should_stop=stop,
        )
        cleaned = db.list_forum_cleaned(limit=10000)
        wl = build_whitelist(cleaned)
        write_whitelist(wl, DEFAULT_WHITELIST)
        clean_stats = dict(clean_stats)
        clean_stats["rescore"] = rs
        clean_stats["whitelist_raw"] = wl.get("total_models", 0)

    # 商品级过滤：踢掉笼统型号
    data = read_json(DEFAULT_WHITELIST, default={}) or {}
    kept: list[dict[str, Any]] = []
    dropped = 0
    for row in data.get("items") or []:
        model = normalize_model(
            str(row.get("device_model") or ""),
            context=" ".join(
                str(x)
                for x in (
                    row.get("device_category"),
                    " ".join(row.get("pain_points") or []),
                )
            ),
        )
        cat = normalize_category(
            str(row.get("device_category") or ""),
            context=str(model or "") + " " + " ".join(row.get("pain_points") or []),
        )
        if not model or not cat:
            dropped += 1
            continue
        row = dict(row)
        row["device_model"] = model
        row["device_category"] = cat
        kept.append(row)
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in sorted(kept, key=lambda r: float(r.get("value_score") or 0), reverse=True):
        key = f"{row['device_category']}|{row['device_model']}".lower()
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        row["rank"] = len(unique) + 1
        unique.append(row)
    data["items"] = unique
    data["total_models"] = len(unique)
    data["scoring_model"] = "commercial_v1"
    data["generated_at"] = _utc_now()
    data["commercial_dropped"] = dropped
    write_json(DEFAULT_WHITELIST, data)
    out = {
        "crawl": crawl_stats,
        "clean": clean_stats,
        "sku_count": len(unique),
        "dropped": dropped,
        "sources": source_list,
    }
    _log(append, f"[DEMAND] 需求型号={len(unique)} | 剔除笼统={dropped}")
    return out


def _whitelist_count() -> int:
    try:
        data = read_json(DEFAULT_WHITELIST, default={}) or {}
        return len(data.get("items") or [])
    except Exception:
        return 0


def _queue_link_count() -> int:
    """待入库 = sources 里尚未入库的线索（直链 / 落地页 / PAGE_CLUE / SEARCH_ONLY）。"""
    if not DEFAULT_SOURCES.exists():
        return 0
    try:
        from link_curator import load_index, load_processed_urls, parse_source_rows_loose
    except Exception:
        return 0
    skip: set[str] = set(load_processed_urls())
    have_models: set[str] = set()
    try:
        for it in (load_index(DEFAULT_INDEX).get("items") or []):
            if not isinstance(it, dict):
                continue
            for key in ("original_direct_url", "verified_url", "source_url"):
                u = str(it.get(key) or "").strip()
                if u:
                    skip.add(u)
            m = str(it.get("exact_model") or it.get("model") or "").strip().lower()
            if m:
                have_models.add(m)
    except Exception:
        pass
    if DEFAULT_FAILED.exists():
        for raw in DEFAULT_FAILED.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            skip.add(line.split("|", 1)[0].strip())
    n = 0
    try:
        for row in parse_source_rows_loose(DEFAULT_SOURCES, require_direct=False):
            url = str(row.get("url") or "").strip()
            mk = str(row.get("model_hint") or "").strip().lower()
            if mk and mk in have_models:
                continue
            if url and url in skip:
                continue
            n += 1
    except Exception:
        return 0
    return n


_TRANSIENT_FAIL_MARKERS = (
    "connection aborted",
    "connectionreset",
    "10054",
    "max retries exceeded",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "name resolution",
    "10060",
)


def _revive_transient_failed(
    failed_path: Path,
    *,
    max_github_timeout: int = 8,
    sources_path: Optional[Path] = None,
) -> int:
    """限量复活：只救 GitHub Releases 超时；绝不复活 archive.org / 已降权站。"""
    if not failed_path.exists():
        return 0
    lines = failed_path.read_text(encoding="utf-8-sig").splitlines()
    keep: list[str] = []
    revived_urls: list[str] = []
    gh_budget = max(0, int(max_github_timeout))
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            keep.append(raw)
            continue
        low = line.lower()
        url = line.split("|", 1)[0].strip()
        if "pattern_noise" in low or "nondriver" in low or "non_driver" in low:
            keep.append(raw)
            continue
        if "archive.org" in low or "download.ni.com" in low:
            keep.append(raw)
            continue
        try:
            from host_scoreboard import is_demoted

            if is_demoted(url, threshold=-6.0):
                keep.append(raw)
                continue
        except Exception:
            pass
        is_gh = "github.com" in low or "githubusercontent.com" in low
        is_transient = any(m in low for m in _TRANSIENT_FAIL_MARKERS)
        if is_gh and is_transient and gh_budget > 0 and "/releases/download/" in low:
            revived_urls.append(url)
            gh_budget -= 1
            continue
        keep.append(raw)
    if not revived_urls:
        return 0
    failed_path.write_text(
        "\n".join(keep) + ("\n" if keep else ""),
        encoding="utf-8",
    )
    try:
        from link_curator import DEFAULT_PROCESSED

        proc = DEFAULT_PROCESSED
        if proc.exists():
            drop = set(revived_urls)
            out_lines: list[str] = []
            for raw in proc.read_text(encoding="utf-8-sig").splitlines():
                s = raw.strip()
                if not s or s.startswith("#"):
                    out_lines.append(raw)
                    continue
                if s.split("|", 1)[0].strip() in drop:
                    continue
                out_lines.append(raw)
            proc.write_text(
                "\n".join(out_lines) + ("\n" if out_lines else ""),
                encoding="utf-8",
            )
    except Exception:
        pass
    try:
        sp = sources_path or DEFAULT_SOURCES
        existing: set[str] = set()
        if sp.exists():
            for raw in sp.read_text(encoding="utf-8-sig").splitlines():
                s = raw.strip()
                if s.startswith("http"):
                    existing.add(s.split("|", 1)[0].strip())
        add_lines = [
            f"{u}|复验|async-revive|通用|网络抖动复验|0"
            for u in revived_urls
            if u.startswith("http") and u not in existing
        ]
        if add_lines:
            with sp.open("a", encoding="utf-8") as f:
                f.write("\n".join(add_lines) + "\n")
    except Exception:
        pass
    return len(revived_urls)



def _phase_intent(
    *,
    round_no: int,
    use_ai: bool,
    stop: Optional[Callable[[], bool]],
    append: Optional[Callable[[str], None]],
    seo_limit: Optional[int] = None,
) -> dict[str, Any]:
    _check(stop, "INTENT")
    from link_curator import DEFAULT_INDEX as IDX, enrich_index_seo, load_index

    index = load_index(IDX)
    n = len(index.get("items") or [])
    if n == 0:
        _log(append, "[INTENT] 索引无已验链条目，跳过 SEO（等待 FULFILL）")
        return {"skipped": True, "reason": "empty_index"}

    # 默认按轮次；调用方可传入更小额度（本轮只验到 1 条时别重跑 14 条）
    lim = (
        int(seo_limit)
        if seo_limit is not None
        else (12 + min(round_no * 2, 20))
    )
    stats = enrich_index_seo(
        index_path=IDX,
        catalog_path=COMMERCIAL_CATALOG,
        web_catalog_path=WEB_CATALOG,
        limit=max(1, min(lim, 24)),
        use_ai=use_ai,
        should_stop=stop,
    )
    _log(
        append,
        f"[INTENT] SEO 富化={stats.get('updated')} | 索引条目={stats.get('sku_count')}",
    )
    return stats


def _phase_acquire(
    *,
    round_no: int,
    use_ai: bool,
    stop: Optional[Callable[[], bool]],
    append: Optional[Callable[[str], None]],
) -> dict[str, Any]:
    _check(stop, "ACQUIRE")
    if _whitelist_count() == 0:
        _log(append, "[ACQUIRE] 白名单为空，跳过挖链")
        return {"skipped": True, "reason": "empty_whitelist", "added": 0}

    from source_queue import (
        DEFAULT_FAILED,
        DEFAULT_SEARCH_RULES,
        DEFAULT_SOURCES,
        DEFAULT_WHITELIST,
        dedupe_sources_file,
        enqueue_scraped_for_whitelist,
        load_failed_urls,
        sync_whitelist_to_sources,
    )
    from github_miner import mine_github_for_whitelist
    from seeker_agent import seek_for_whitelist

    # 网络抖动导致的假死链：每轮 ACQUIRE 清掉可重试项
    try:
        from github_auth import reset_html_circuit

        reset_html_circuit()
    except Exception:
        pass
    revived = _revive_transient_failed(DEFAULT_FAILED)
    if revived:
        _log(append, f"[ACQUIRE] 复活网络假死链 {revived} 条，允许复验")

    failed = load_failed_urls(DEFAULT_FAILED)
    sync = sync_whitelist_to_sources(
        DEFAULT_WHITELIST,
        DEFAULT_SOURCES,
        search_rules_path=DEFAULT_SEARCH_RULES,
        min_score=0.0,
        failed_urls=failed,
        enqueue_pages=False,
    )
    backfill = enqueue_scraped_for_whitelist(
        DEFAULT_WHITELIST,
        DEFAULT_SOURCES,
        failed_urls=failed,
        limit=40,
    )
    _check(stop, "ACQUIRE")

    # 只挖未上架型号；没有新型号就别空转
    try:
        from seeker_agent import cataloged_models, load_whitelist_targets

        already = cataloged_models()
        fresh_targets = load_whitelist_targets(
            DEFAULT_WHITELIST, limit=80, skip_models=already, prefer_new=True
        )
        fresh_n = len(fresh_targets)
    except Exception:
        fresh_n = 20
        already = set()

    # 已上架型号仍继续从结构化上游扩链；盲猜矩阵已废除
    if fresh_n == 0 and round_no > 1:
        _log(
            append,
            "[ACQUIRE] 白名单型号均已上架，继续结构化索引扩链",
        )

    # ——— 0) 高密度结构化索引抓取（sitemap / 目录索引 / Releases / IA metadata）———
    # 盲猜规则矩阵已彻底停用：只收真实索引源里的直链
    from structured_index_harvest import run_structured_index_harvest
    from dead_end_blacklist import count_sunk_skipped

    harvest = run_structured_index_harvest(
        whitelist_path=DEFAULT_WHITELIST,
        sources_path=DEFAULT_SOURCES,
        failed_path=DEFAULT_FAILED,
        max_new_links=80,
        round_no=round_no,
        should_stop=stop,
        max_seconds=18.0,
    )
    struct_scraped = int(harvest.get("scraped") or 0)
    struct_added = int(harvest.get("added") or 0)
    dead_skipped = count_sunk_skipped(round_no=round_no)
    _log(
        append,
        f"[ACQUIRE] 结构化索引 scraped={struct_scraped} added={struct_added} "
        f"sources={harvest.get('sources_scanned') or 0}",
    )
    _check(stop, "ACQUIRE")

    # ——— 1) GitHub 矿工 + 2) Bing/DDG 排行榜挖链（变通喂料，不空转同一站）———
    # 轮转引擎：优先 Bing（近似 Google 排行），隔轮 DDG
    engine = "bing" if (round_no % 2) else "ddg"
    strategy = (round_no - 1) % 4
    seek_limit = min(6, max(3, (fresh_n or 2) + 2))
    dry_path = Path(__file__).resolve().parent / "data" / "acquire_dry_streak.txt"
    dry_streak = 0
    try:
        dry_streak = int(dry_path.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        dry_streak = 0

    api_blocked = False
    gh_down = False
    try:
        from github_auth import token_status

        st = token_status()
        api_blocked = not st.get("api_allowed")
        gh_down = api_blocked and (not st.get("html_allowed"))
    except Exception:
        pass

    use_seek_ai = False
    miner_budget = 35.0 if (api_blocked or dry_streak >= 3) else 55.0

    gh_kwargs = dict(
        whitelist_path=DEFAULT_WHITELIST,
        sources_path=DEFAULT_SOURCES,
        failed_path=DEFAULT_FAILED,
        limit_models=min(10, 6 + (fresh_n or 0)),
        max_new_links=24,
        should_stop=stop,
        max_seconds=miner_budget,
    )
    seek_kwargs = dict(
        whitelist_path=DEFAULT_WHITELIST,
        sources_path=DEFAULT_SOURCES,
        failed_path=DEFAULT_FAILED,
        search_rules_path=DEFAULT_SEARCH_RULES,
        limit_models=seek_limit,
        max_new_links=24,
        strategy=strategy,
        engine=engine,
        should_stop=stop,
        use_ai_queries=use_seek_ai,
    )

    from concurrent.futures import TimeoutError as FuturesTimeout
    from concurrent.futures import ThreadPoolExecutor

    # 变通策略：结构化索引 + 搜索排行榜 并行；搜索不再被「结构化有料」关掉
    _log(
        append,
        f"[ACQUIRE] 变通挖链 engine={engine} strategy={strategy} "
        f"seek_models={seek_limit}",
    )
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        fut_gh = pool.submit(mine_github_for_whitelist, **gh_kwargs)
        fut_seek = pool.submit(seek_for_whitelist, **seek_kwargs)
        try:
            seek = fut_seek.result(timeout=95.0)
        except FuturesTimeout:
            _log(append, "[ACQUIRE] 排行榜挖链超时截断")
            seek = {"added": 0, "queries": 0, "timed_out": True}
        except Exception as exc:
            seek = {"added": 0, "queries": 0, "error": str(exc)}
        try:
            gh = fut_gh.result(timeout=min(90.0, miner_budget + 25))
        except FuturesTimeout:
            _log(append, "[ACQUIRE] GitHub矿工超时截断，先进入验链")
            gh = {
                "added": 0,
                "assets_found": 0,
                "timed_out": True,
                "models": 0,
                "repos": 0,
            }
        except Exception as exc:
            gh = {"added": 0, "assets_found": 0, "error": str(exc)}
    finally:
        pool.shutdown(wait=False)

    _log(
        append,
        f"[ACQUIRE] GitHub矿工 +{gh.get('added')} assets={gh.get('assets_found')}",
    )
    _log(
        append,
        f"[ACQUIRE] 排行榜挖链 +{seek.get('added')} queries={seek.get('queries')} "
        f"hits={seek.get('search_hits')} engine={engine}",
    )

    _check(stop, "ACQUIRE")

    # 长尾 site: 高排名站：结构化+搜索仍干时补一刀
    pre_links = (
        struct_added + int(gh.get("added") or 0) + int(seek.get("added") or 0)
    )
    if pre_links >= 8:
        _log(append, "[ACQUIRE] 本轮已够料，跳过长尾 site 补刀")
        lt = {"added": 0, "skipped": True, "reason": "enough_feed"}
    elif dry_streak >= 5:
        _log(append, "[ACQUIRE] 连续空转过久，跳过长尾防烧")
        lt = {"added": 0, "skipped": True, "reason": "dry_streak"}
    else:
        from longtail_demand import run_longtail_mining

        _log(append, "[ACQUIRE] 长尾 site: 高排名站补刀（Softpedia/SF/OEM）")
        lt = run_longtail_mining(
            whitelist_path=DEFAULT_WHITELIST,
            sources_path=DEFAULT_SOURCES,
            failed_path=DEFAULT_FAILED,
            cache_path=DEFAULT_LONGTAIL_CACHE,
            limit_models=4,
            max_new_links=16,
            use_ai=False,
            engine=engine,
            should_stop=stop,
            create_empty_dirs=False,
        )

    dd = dedupe_sources_file(DEFAULT_SOURCES)
    # 宽松策略：挖不到直链时，把未上架白名单型号直接软注入 sources（兜底 URL）
    soft_seeded = 0
    try:
        from link_curator import soft_fallback_url
        from seeker_agent import append_sources, cataloged_models, load_whitelist_targets

        already = cataloged_models()
        missing = load_whitelist_targets(
            DEFAULT_WHITELIST, limit=40, skip_models=already, prefer_new=True
        )
        entries: list[tuple[str, str, str, str, str, float]] = []
        for t in missing:
            model = str(t.get("device_model") or t.get("model") or "").strip()
            cat = str(
                t.get("device_category") or t.get("category") or "工控设备"
            ).strip()
            if not model:
                continue
            url = soft_fallback_url(model, cat)
            pain = ""
            pains = t.get("pain_points") or []
            if isinstance(pains, list) and pains:
                pain = str(pains[0])[:80]
            entries.append(
                (
                    url,
                    cat,
                    model,
                    "通用",
                    pain or "soft seed",
                    float(t.get("value_score") or 45),
                )
            )
        if entries:
            soft_seeded = append_sources(DEFAULT_SOURCES, entries)
            _log(
                append,
                f"[ACQUIRE] 宽松软注入未上架型号 {soft_seeded} 条（直链挖空时兜底投喂）",
            )
    except Exception as exc:
        _log(append, f"[ACQUIRE] 软注入跳过: {exc}")

    added_total = (
        struct_added
        + int(gh.get("added") or 0)
        + int(seek.get("added") or 0)
        + int(lt.get("added") or 0)
        + int(sync.get("added_total") or 0)
        + int(backfill.get("added") or 0)
        + soft_seeded
    )
    out = {
        "sync": sync,
        "backfill": backfill,
        "structured_index": harvest,
        "pattern_discovery": {"skipped": True, "reason": "disabled_blind_guess"},
        "github_miner": gh,
        "seeker": seek,
        "longtail": lt,
        "soft_seeded": soft_seeded,
        "dedupe_removed": dd.get("removed", 0),
        "added": added_total,
        "structured_scraped": struct_scraped,
        "structured_added": struct_added,
        "pattern_generated": 0,
        "pattern_added": 0,
        "dead_skipped": dead_skipped,
        "revived_failed": revived,
        "queue_candidates": queue_now,
    }
    queue_now = _queue_link_count()
    harvest_line = (
        f"抓取结构化源：{struct_scraped} 条 | 待验活候选：{queue_now} 条 | "
        f"本轮有效入库 SKU：0"
    )
    _log(append, harvest_line)
    print(harvest_line, flush=True)
    _log(
        append,
        f"[ACQUIRE] 本轮新链≈{added_total} | 队列现有={queue_now} | "
        f"去重={dd.get('removed', 0)}",
    )
    try:
        dry_path.parent.mkdir(parents=True, exist_ok=True)
        next_dry = 0 if added_total > 0 else min(dry_streak + 1, 20)
        dry_path.write_text(str(next_dry), encoding="utf-8")
        out["dry_streak"] = next_dry
    except Exception:
        pass
    return out



def _phase_fulfill(
    *,
    use_ai: bool,
    harvest_workers: int,
    harvest_rate_mbps: float,
    stop: Optional[Callable[[], bool]],
    append: Optional[Callable[[str], None]],
) -> dict[str, Any]:
    """宽松强制入库：宁滥勿缺，不做 HEAD 拦截 / 超时丢弃。"""
    del harvest_rate_mbps, use_ai, harvest_workers
    assert_link_only_pipeline()
    _check(stop, "FULFILL")
    queued = _queue_link_count()
    if queued == 0:
        _log(append, "[FULFILL] empty queue — wait for ACQUIRE")
        return {"skipped": True, "reason": "empty_queue"}

    from link_curator import force_soft_curate

    # 宁滥勿缺：有型号 / 有线索就强行写入双写目录，不经严格验活
    fulfill_limit = min(500, max(80, queued))
    _log(
        append,
        f"[FULFILL] 宽松强制入库模式 | 待入库线索≈{queued} | 本轮额度={fulfill_limit}",
    )
    stats = force_soft_curate(
        sources_path=DEFAULT_SOURCES,
        index_path=DEFAULT_INDEX,
        catalog_path=COMMERCIAL_CATALOG,
        web_catalog_path=WEB_CATALOG,
        limit=fulfill_limit,
        should_stop=stop,
    )
    try:
        from link_curator import prune_indexed_from_sources

        pruned = prune_indexed_from_sources(
            sources_path=DEFAULT_SOURCES, index_path=DEFAULT_INDEX
        )
        if pruned:
            _log(append, f"[FULFILL] 已从待验队列移除 {pruned} 条已入库线索")
            stats["pruned_sources"] = pruned
    except Exception as exc:
        _log(append, f"[FULFILL] 清理待验队列跳过: {exc}")
    rate_line = (
        f"宽松入库成功批次：{stats.get('indexed') or stats.get('verified') or 0} | "
        f"智能兜底：{stats.get('soft_fallback') or 0} | "
        f"跳过已有：{stats.get('skipped_existing') or 0} | "
        f"目录 SKU：{stats.get('catalog_count') or '?'}"
    )
    _log(append, rate_line)
    print(rate_line, flush=True)
    _log(
        append,
        f"[FULFILL] soft_indexed={stats.get('indexed')} "
        f"fallback={stats.get('soft_fallback')} "
        f"| 待入库剩余={_queue_link_count()} "
        f"(mode=force_soft 不经严格验活)",
    )
    return stats


def _phase_merchandize(
    *,
    use_ai: bool,
    stop: Optional[Callable[[], bool]],
    append: Optional[Callable[[str], None]],
) -> dict[str, Any]:
    """Emit pure navigation JSON — original_direct_url only, never local paths."""
    assert_link_only_pipeline()
    _check(stop, "MERCHANDIZE")
    from catalog_rules import normalize_category, normalize_model
    from link_curator import load_index, suggest_price

    index = load_index(DEFAULT_INDEX)
    raw_items = list(index.get("items") or [])
    skus: list[dict[str, Any]] = []
    removed = 0

    for it in raw_items:
        url = str(
            it.get("original_direct_url")
            or it.get("verified_url")
            or it.get("source_url")
            or ""
        ).strip()
        if not url or not it.get("verified", True):
            removed += 1
            continue
        if url.startswith(("file:", "/", "\\\\")) or (len(url) > 2 and url[1:3] == ":\\"):
            removed += 1
            continue
        model = normalize_model(
            str(it.get("exact_model") or it.get("hardware_model") or it.get("model") or ""),
            context=str(it.get("pain_point") or ""),
        )
        cat = normalize_category(
            str(it.get("hardware_category") or it.get("category") or ""),
            context=str(model or ""),
        )
        if not model or not cat:
            removed += 1
            continue
        if use_ai and not it.get("seo_title"):
            try:
                from link_curator import enrich_with_ai

                seo = enrich_with_ai(
                    model=model,
                    category=cat,
                    os_version=str(it.get("os_version") or "Legacy Windows"),
                    pain_hint=str(it.get("pain_point") or ""),
                    file_name=str(it.get("file_name") or ""),
                    url=url,
                    use_ai=True,
                )
                it.update(
                    {
                        "seo_title": seo.get("seo_title"),
                        "seo_description": seo.get("seo_description"),
                        "description": seo.get("seo_description"),
                        "pain_points": seo.get("pain_points"),
                        "tags": seo.get("tags"),
                        "ai_used": seo.get("ai_used"),
                    }
                )
            except Exception:
                pass
        price = it.get("price")
        if price is None:
            price = suggest_price(
                category=cat,
                model=model,
                content_length=it.get("content_length"),
                value_score=float(it.get("value_score") or 0),
            )
        slug = it.get("slug") or _sku_slug(cat, model)
        desc = str(
            it.get("description")
            or it.get("seo_description")
            or (it.get("pain_points") or [""])[0]
            or ""
        )
        skus.append(
            catalog_sku_row(
                model=model,
                category=cat,
                description=desc,
                original_direct_url=url,
                content_length=it.get("content_length"),
                file_name=str(it.get("file_name") or ""),
                slug=str(slug),
                seo_title=str(it.get("seo_title") or f"{model} Legacy Driver Archive"[:70]),
                pain_points=list(
                    it.get("pain_points")
                    or [f"Looking for archived {model} driver after OEM discontinued support"]
                ),
                tags=list(it.get("tags") or []),
                price=price,
                value_score=it.get("value_score"),
                extra={
                    "sku": f"{cat}/{model}",
                    "hardware_model": model,
                    "pain_point": (it.get("pain_points") or [it.get("pain_point") or ""])[0],
                    "google_longtail_queries": it.get("google_longtail_queries") or [],
                    "file_count": 1,
                },
            )
        )

    best: dict[str, dict[str, Any]] = {}
    for s in skus:
        key = f"{s['hardware_category']}|{s['exact_model']}"
        prev = best.get(key)
        if not prev or float(s.get("value_score") or 0) >= float(prev.get("value_score") or 0):
            best[key] = s
    skus = sorted(best.values(), key=lambda x: float(x.get("value_score") or 0), reverse=True)

    # 可持续增长：与历史账本合并，禁止用更小集合覆盖店面
    from catalog_growth import grow_catalog

    grown = grow_catalog(skus, write_storefront=True)
    skus = list(grown.get("items") or skus)
    _log(
        append,
        f"[MERCHANDIZE] growth SKUs={grown.get('sku_count')} | ledger={grown.get('ledger_count')} "
        f"| +{grown.get('added')} | preserved={grown.get('preserved')} | "
        f"synced {WEB_CATALOG} (只增不减，垃圾链除外)",
    )
    return {
        "sku_count": int(grown.get("sku_count") or len(skus)),
        "ledger_count": int(grown.get("ledger_count") or 0),
        "added": int(grown.get("added") or 0),
        "removed": removed,
        "catalog_path": str(COMMERCIAL_CATALOG),
        "web_catalog": str(WEB_CATALOG),
        "index_path": str(DEFAULT_INDEX),
        "growth": True,
    }



def _sku_slug(category: str, model: str) -> str:
    raw = f"{category}-{model}".lower()
    raw = re.sub(r"[\\/]+", "-", raw)
    raw = re.sub(r"[^a-z0-9\u4e00-\u9fff\-]+", "-", raw)
    raw = re.sub(r"-+", "-", raw).strip("-")
    return raw[:80] or "sku"


def run_commercial_round(
    *,
    round_no: int = 1,
    max_per_query: int = 5,
    sources: str = "github",
    clean_forum_limit: int = 80,
    use_ai: bool = True,
    harvest_workers: int = 4,
    harvest_rate_mbps: float = 8.0,
    should_stop: Optional[Callable[[], bool]] = None,
    append_log: Optional[Callable[[str], None]] = None,
    on_phase: Optional[Callable[[str, str, int, int], None]] = None,
) -> dict[str, Any]:
    """跑完整一轮五阶段商业流程（Find & Verify only）。"""
    assert_link_only_pipeline()
    stop = should_stop
    append = append_log
    total = len(PHASES)
    results: dict[str, Any] = {
        "ok": False,
        "round": round_no,
        "phases": {},
        "started_at": _utc_now(),
    }

    for i, (code, title) in enumerate(PHASES, start=1):
        _check(stop, code)
        # 货架已厚时节流 DEMAND；但队列干涸 / 白名单型号几乎都已上架时必须补需求
        backlog = _queue_link_count()
        sku_n = 0
        try:
            sku_n = int(commercial_stats().get("sku_ready") or 0)
        except Exception:
            sku_n = 0
        wl_n = _whitelist_count()
        fresh_wl = 0
        try:
            from seeker_agent import cataloged_models, load_whitelist_targets

            fresh_wl = len(
                load_whitelist_targets(
                    DEFAULT_WHITELIST,
                    limit=80,
                    skip_models=cataloged_models(),
                    prefer_new=True,
                )
            )
        except Exception:
            fresh_wl = 0
        # 断粮强制 DEMAND：待入库很少 或 白名单几乎都被吃光
        force_demand = backlog <= 8 or fresh_wl <= 6
        if code == "DEMAND" and not force_demand and (
            backlog >= 15
            or (wl_n >= 40 and round_no % 8 != 1)
            or (sku_n >= 100 and round_no % 8 != 1)
        ):
            _log(
                append,
                f"[加速] 跳过 DEMAND（sku={sku_n} wl={wl_n} q={backlog} fresh={fresh_wl}）→ 优先挖链验活",
            )
            results["phases"][code] = {
                "skipped": True,
                "reason": "efficiency_skip_demand",
                "backlog": backlog,
                "sku_ready": sku_n,
                "fresh_whitelist": fresh_wl,
                "elapsed_sec": 0,
            }
            continue
        if code == "DEMAND" and force_demand:
            _log(
                append,
                f"[断粮] 强制 DEMAND 补新型号（q={backlog} fresh_wl={fresh_wl} sku={sku_n}）",
            )
        if backlog >= 40 and code == "DEMAND" and not force_demand:
            _log(append, f"[加速] 待验积压 {backlog} 条，跳过 DEMAND，优先验链成商品")
            results["phases"][code] = {
                "skipped": True,
                "reason": "backlog_fulfill_first",
                "backlog": backlog,
                "elapsed_sec": 0,
            }
            continue
        if backlog >= 120 and code == "ACQUIRE":
            _log(append, f"[加速] 待验严重积压 {backlog}，暂缓 ACQUIRE")
            results["phases"][code] = {
                "skipped": True,
                "reason": "backlog_extreme",
                "backlog": backlog,
                "elapsed_sec": 0,
            }
            continue
        if on_phase:
            on_phase(code, title, i, total)
        _log(append, f"—— 阶段 {i}/{total} {code}: {title} ——")
        t0 = time.monotonic()
        try:
            if code == "DEMAND":
                # 默认只走 GitHub，彻底避开 SE 429/400；sources 显式含 stackexchange 时才偶发尝试
                want_se = "stackexchange" in (sources or "").lower()
                rotations = [
                    "github,reddit",
                    "github,reddit",
                    "github",
                    "github,reddit,stackexchange" if want_se else "github,reddit",
                ]
                src = rotations[(round_no - 1) % 4]
                if round_no == 1:
                    src = "github,reddit"
                phase_out = _phase_demand(
                    round_no=round_no,
                    max_per_query=max_per_query,
                    sources=src,
                    clean_forum_limit=clean_forum_limit,
                    use_ai=use_ai,
                    stop=stop,
                    append=append,
                )
            elif code == "ACQUIRE":
                phase_out = _phase_acquire(
                    round_no=round_no, use_ai=use_ai, stop=stop, append=append
                )
            elif code == "FULFILL":
                phase_out = _phase_fulfill(
                    use_ai=use_ai,
                    harvest_workers=harvest_workers,
                    harvest_rate_mbps=harvest_rate_mbps,
                    stop=stop,
                    append=append,
                )
            elif code == "INTENT":
                ful = results["phases"].get("FULFILL") or {}
                verified_n = int(ful.get("verified") or 0)
                indexed_n = int(ful.get("indexed") or 0)
                new_n = max(verified_n, indexed_n)
                # 本轮 0 新验链：跳过 SEO（否则 Ollama 白烧 ~1 分钟）
                if new_n <= 0:
                    _log(append, "[INTENT] 本轮无新验链，跳过 SEO")
                    phase_out = {
                        "skipped": True,
                        "reason": "no_new_verified",
                    }
                elif backlog >= 40:
                    _log(append, f"[加速] 待验积压 {backlog}，INTENT 轻量跳过 AI")
                    phase_out = _phase_intent(
                        round_no=round_no,
                        use_ai=False,
                        stop=stop,
                        append=append,
                        seo_limit=min(4, new_n + 1),
                    )
                else:
                    # 只富化新增附近额度，禁止每轮重跑十几条
                    phase_out = _phase_intent(
                        round_no=round_no,
                        use_ai=use_ai,
                        stop=stop,
                        append=append,
                        seo_limit=min(6, max(2, new_n + 1)),
                    )
            else:
                ful = results["phases"].get("FULFILL") or {}
                new_sku = int(ful.get("verified") or 0) + int(ful.get("indexed") or 0)
                if new_sku <= 0:
                    _log(append, "[MERCHANDIZE] 无新商品，跳过重写目录")
                    phase_out = {"skipped": True, "reason": "no_new_sku"}
                else:
                    phase_out = _phase_merchandize(
                        use_ai=False if backlog >= 40 else use_ai,
                        stop=stop,
                        append=append,
                    )
        except PipelineStopped:
            raise
        except Exception as exc:
            logger.exception("阶段 %s 失败", code)
            _log(append, f"[容错] 阶段 {code} 异常，继续后续阶段: {exc}")
            phase_out = {"ok": False, "error": str(exc)}
        phase_out["elapsed_sec"] = round(time.monotonic() - t0, 1)
        results["phases"][code] = phase_out

    results["ok"] = True
    results["finished_at"] = _utc_now()
    results["message"] = f"第 {round_no} 轮商业流水线完成"
    results["sku_ready"] = commercial_stats().get("sku_ready", 0)
    results["whitelist_skus"] = _whitelist_count()
    results["queue_links"] = _queue_link_count()

    # 产量指标汇报（结构化索引模式）
    acq = results.get("phases", {}).get("ACQUIRE") or {}
    ful = results.get("phases", {}).get("FULFILL") or {}
    x = int(acq.get("structured_scraped") or acq.get("structured_added") or 0)
    y_sku = int(ful.get("indexed") or 0)
    if y_sku <= 0:
        y_sku = int(ful.get("verified") or 0)
    # 待验候选：优先 ACQUIRE 入队时快照；否则用本轮实际探针数
    y_queue = int(acq.get("queue_candidates") or 0)
    attempted = int(ful.get("verified") or 0) + int(ful.get("failed") or 0)
    if attempted > y_queue:
        y_queue = attempted
    if y_queue <= 0:
        y_queue = int(results.get("queue_links") or 0)
    yield_line = (
        f"抓取结构化源：{x} 条 | 待验活候选：{y_queue} 条 | 本轮有效入库 SKU：{y_sku}"
    )
    results["yield_report"] = {
        "structured_scraped": x,
        "queue_candidates": y_queue,
        "verified_sku": y_sku,
        "dead_skipped": int(acq.get("dead_skipped") or 0),
    }
    _log(append, yield_line)
    print(yield_line, flush=True)
    rate_line = (
        f"当前并发协程：{ful.get('concurrency') or 0} | "
        f"验活吞吐量：{ful.get('throughput') or 0} 个/秒 | "
        f"本轮有效入库：{y_sku}"
    )
    _log(append, rate_line)
    print(rate_line, flush=True)
    return results


def run_commercial_daemon(
    *,
    continuous: bool = True,
    round_sleep_sec: float = 12.0,
    max_per_query: int = 5,
    sources: str = "github",
    clean_forum_limit: int = 80,
    use_ai: bool = True,
    harvest_workers: int = 4,
    harvest_rate_mbps: float = 8.0,
    should_stop: Optional[Callable[[], bool]] = None,
    append_log: Optional[Callable[[str], None]] = None,
    on_phase: Optional[Callable[[str, str, int, int], None]] = None,
    on_round_start: Optional[Callable[[int], None]] = None,
) -> dict[str, Any]:
    """常驻：不暂停就一直跑商业轮次。"""
    rounds: list[dict[str, Any]] = []
    round_no = 0
    try:
        while True:
            round_no += 1
            if on_round_start:
                on_round_start(round_no)
            _log(append_log, f"######## 商业轮次 #{round_no} 开始 ########")
            one = run_commercial_round(
                round_no=round_no,
                max_per_query=max_per_query + min((round_no - 1) // 2, 5),
                sources=sources,
                clean_forum_limit=clean_forum_limit,
                use_ai=use_ai,
                harvest_workers=harvest_workers,
                harvest_rate_mbps=harvest_rate_mbps,
                should_stop=should_stop,
                append_log=append_log,
                on_phase=on_phase,
            )
            rounds.append(one)
            _log(append_log, f"######## 商业轮次 #{round_no} 结束 ########")
            if not continuous:
                break
            if should_stop and should_stop():
                break
            # 本轮几乎空转时拉长等待，避免 DEMAND/ACQUIRE 空转刷屏
            phases = one.get("phases") or {}
            acq = phases.get("ACQUIRE") or {}
            dem = phases.get("DEMAND") or {}
            added = int(acq.get("added") or 0)
            skipped_heavy = bool(acq.get("skipped")) or bool(dem.get("skipped"))
            crawl_saved = 0
            try:
                crawl_saved = int((dem.get("crawl") or {}).get("saved") or 0)
            except Exception:
                crawl_saved = 0
            # 空转轮少睡：GitHub 双挂时 30s 轮间等待纯浪费
            queued_now = _queue_link_count()
            if added == 0 and crawl_saved == 0 and queued_now < 5:
                sleep_sec = max(8.0, min(14.0, round_sleep_sec))
            elif skipped_heavy or added == 0:
                sleep_sec = (
                    max(8.0, round_sleep_sec * 0.7)
                    if queued_now >= 10
                    else max(10.0, round_sleep_sec)
                )
            else:
                sleep_sec = (
                    max(3.0, round_sleep_sec * 0.3)
                    if queued_now >= 15
                    else max(5.0, round_sleep_sec * 0.45)
                )
            _log(append_log, f"[常驻] {sleep_sec:.0f}s 后下一轮（可暂停）")
            end = time.time() + sleep_sec
            while time.time() < end:
                if should_stop and should_stop():
                    raise PipelineStopped("用户暂停于轮间等待")
                time.sleep(min(0.25, end - time.time()))
    except PipelineStopped as exc:
        _log(append_log, f"[暂停] {exc}")
        return {
            "ok": True,
            "paused": True,
            "rounds": round_no,
            "results": rounds,
            "message": "已按用户暂停停止",
        }

    return {
        "ok": True,
        "paused": bool(should_stop and should_stop()),
        "rounds": round_no,
        "results": rounds,
        "message": "商业流水线结束",
    }


def load_commercial_catalog(
    limit: int = 30,
    *,
    allow_cold_parse: bool = True,
) -> list[dict[str, Any]]:
    """Return catalog rows. Polling paths should set allow_cold_parse=False."""
    try:
        try:
            mtime = COMMERCIAL_CATALOG.stat().st_mtime
        except OSError:
            return []
        cached = _CATALOG_MEM.get("data")
        if _CATALOG_MEM.get("mtime") == mtime and isinstance(cached, dict):
            items = cached.get("items") or []
            return list(items[:limit] if limit and limit > 0 else items)
        if not allow_cold_parse:
            prev = _CATALOG_MEM.get("preview") or []
            return list(prev)[:limit] if limit and limit > 0 else list(prev)
        data = _cached_catalog_data()
        items = data.get("items") or []
        preview = list(items[:40])
        _CATALOG_MEM["preview"] = preview
        if limit and limit > 0:
            return list(items[:limit])
        return list(items)
    except Exception:
        return []


_CATALOG_MEM: dict[str, Any] = {"mtime": None, "data": None, "preview": None}


def _cached_catalog_data() -> dict[str, Any]:
    """Parse the 20MB+ catalog at most once per mtime change."""
    try:
        mtime = COMMERCIAL_CATALOG.stat().st_mtime
    except OSError:
        return {}
    if _CATALOG_MEM.get("mtime") == mtime and isinstance(_CATALOG_MEM.get("data"), dict):
        return _CATALOG_MEM["data"]  # type: ignore[return-value]
    data = read_json(COMMERCIAL_CATALOG, default={}) or {}
    _CATALOG_MEM["mtime"] = mtime
    _CATALOG_MEM["data"] = data
    _CATALOG_MEM["preview"] = list((data.get("items") or [])[:40])
    return data


def _sku_count_fast() -> int:
    """Read sku_count from file header without full JSON parse when possible."""
    try:
        with COMMERCIAL_CATALOG.open("rb") as fh:
            head = fh.read(2048).decode("utf-8", errors="ignore")
        m = re.search(r'"sku_count"\s*:\s*(\d+)', head)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    data = _cached_catalog_data()
    n = int(data.get("sku_count") or 0)
    if n > 0:
        return n
    return len(data.get("items") or [])


def commercial_stats() -> dict[str, Any]:
    # Keep this poll-safe: no full vault_index / sources scan on every tick.
    return {
        "sku_ready": _sku_count_fast(),
        "whitelist_skus": _whitelist_count(),
        "queue_links": _queue_link_count_cached(),
        "phases": [{"code": c, "title": t} for c, t in PHASES],
    }


_QUEUE_CACHE: dict[str, Any] = {"key": None, "n": 0, "ts": 0.0}


def _queue_link_count_cached() -> int:
    """Throttle expensive queue scans to at most once per ~30s / mtime change."""
    try:
        parts = []
        for p in (DEFAULT_SOURCES, DEFAULT_INDEX, DEFAULT_FAILED):
            try:
                parts.append(f"{p}:{p.stat().st_mtime_ns}")
            except OSError:
                parts.append(f"{p}:0")
        key = "|".join(parts)
        now = time.time()
        if (
            _QUEUE_CACHE.get("key") == key
            and now - float(_QUEUE_CACHE.get("ts") or 0) < 30
        ):
            return int(_QUEUE_CACHE.get("n") or 0)
        n = _queue_link_count()
        _QUEUE_CACHE["key"] = key
        _QUEUE_CACHE["n"] = n
        _QUEUE_CACHE["ts"] = now
        return n
    except Exception:
        return 0
