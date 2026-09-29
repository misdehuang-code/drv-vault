"""
Google 长尾需求与痛点挖掘模块（Link Curation）。

1. 长尾需求生成器：根据急需型号白名单，用本地 Ollama 生成海外极客检索词。
2. 精准靶向检索：Archive.org / GitHub Releases 等 → 写入 sources.txt。
3. SEO 缓存：写入 data/longtail_queries.json（不写本地 vault 目录）。

用法：
  python longtail_demand.py
  python longtail_demand.py --limit 8 --max-links 20 --no-ai
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlparse

import requests

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
DEFAULT_WHITELIST = ROOT / "data" / "急需设备型号白名单.json"
DEFAULT_SOURCES = ROOT / "sources.txt"
DEFAULT_FAILED = ROOT / "failed_sources.txt"
DEFAULT_LONGTAIL_CACHE = ROOT / "data" / "longtail_queries.json"

DIRECT_EXTS = (
    ".zip",
    ".rar",
    ".7z",
    ".exe",
    ".msi",
    ".cab",
    ".iso",
    ".img",
    ".bin",
    ".gz",
    ".tar",
)

# 海外极客常见痛点修饰词（规则兜底 + AI 扩写基础）
PAIN_MODIFIERS = (
    "driver download archive",
    "legacy driver zip",
    "fix code 43",
    "fix code 10",
    "windows xp driver",
    "windows 7 legacy support",
    "official driver discontinued",
    "archived driver package",
    "old version driver download",
)

TARGET_SITES = (
    "site:github.com/releases",
    "site:sourceforge.net",
    "site:drivers.softpedia.com",
    "site:www.techpowerup.com",
    "site:station-drivers.com",
    "site:oldversion.com",
    "site:downloadcenter.intel.com",
    "site:www.nvidia.com/Download",
    "site:www.broadcom.com/support",
    "site:www.realtek.com",
    "site:www.dell.com/support",
    "site:support.hpe.com",
)

LONGTAIL_SYSTEM = """You generate Google long-tail search queries that overseas PC geeks use
to find discontinued / legacy Windows hardware drivers.

Output ONLY valid JSON:
{
  "queries": ["english query 1", "..."],
  "pain_points": ["English buyer pain 1", "..."],
  "seo_title": "under 70 chars English title with model + pain",
  "seo_description": "1-2 sentences English meta description for a driver archive product page",
  "tags": ["tag1", "tag2"]
}

Rules:
- queries: 4-8 English long-tail phrases; mix model + pain modifiers
  (driver download archive, fix code 43, legacy support zip, XP/Win7, discontinued, etc.)
- Do NOT invent fake version numbers.
- pain_points must sound like real Google search intent (what the buyer types / fears).
- Prefer searchable English; no Chinese in queries/seo fields.
"""

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": UA, "Accept": "application/json,text/html,*/*"})
    return s


def _is_direct_file(url: str) -> bool:
    path = unquote(urlparse(url).path).lower()
    return any(path.endswith(ext) for ext in DIRECT_EXTS)


def _sanitize(name: str) -> str:
    from source_queue import sanitize_name

    return sanitize_name(name)


def _load_failed(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line.split("|", 1)[0].strip())
    return out


def _load_existing_sources(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line.split("|", 1)[0].strip())
    return out


def load_whitelist_items(path: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    out: list[dict[str, Any]] = []
    for row in data.get("items") or []:
        model = str(row.get("device_model") or "").strip()
        cat = str(row.get("device_category") or "").strip()
        if not model or model in {"未知型号", "未知设备", "其他外设"}:
            continue
        if not cat or cat in {"其他", "其他外设"}:
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


def rule_longtail_bundle(row: dict[str, Any]) -> dict[str, Any]:
    """无 Ollama 时的规则兜底长尾包。"""
    model = str(row.get("device_model") or "").strip()
    cat = str(row.get("device_category") or "").strip()
    errors = [str(e) for e in (row.get("error_codes") or []) if e][:3]
    pains_zh = [str(p) for p in (row.get("pain_points") or []) if p][:2]

    queries = [
        f"{model} driver download archive",
        f"{model} legacy driver zip",
        f"{model} windows 7 driver download",
        f'"{model}" filetype:zip driver',
    ]
    if errors:
        queries.append(f"{model} fix {errors[0]} driver")
    else:
        queries.append(f"{model} fix code 43 driver")
    queries.append(f"{model} {cat} archived driver package")

    pain_en = [
        f"Looking for archived {model} driver download after official support ended",
        f"Fix Code 43 / missing driver on legacy Windows for {model}",
    ]
    if pains_zh:
        pain_en.append(f"Hard-to-find {model} package: {pains_zh[0][:80]}")

    return {
        "queries": queries[:8],
        "pain_points": pain_en[:3],
        "seo_title": f"{model} Legacy Driver Download | Fix Code 43 Archive"[:70],
        "seo_description": (
            f"Download archived {model} ({cat}) Windows drivers. "
            f"Legacy XP/Win7 packages for Code 43 and discontinued OEM support."
        ),
        "tags": [
            model.lower()[:40],
            "legacy driver",
            "code 43",
            "driver archive",
            "windows 7",
            cat.lower()[:30] if cat else "hardware",
        ],
        "ai_used": False,
        "ai_backend": "rule_longtail",
    }


def generate_longtail_with_ai(row: dict[str, Any]) -> dict[str, Any]:
    """Ollama 生成 Google 风格长尾词 + SEO 痛点。"""
    fallback = rule_longtail_bundle(row)
    try:
        from ai_cleaner import get_ollama_client

        client = get_ollama_client()
        if not client.probe():
            return fallback
        model = str(row.get("device_model") or "")
        cat = str(row.get("device_category") or "")
        user = (
            f"Hardware category: {cat}\n"
            f"Exact model: {model}\n"
            f"Known pains (may be Chinese): {row.get('pain_points') or []}\n"
            f"Error codes: {row.get('error_codes') or []}\n"
            f"Seed modifiers: {list(PAIN_MODIFIERS)}\n"
            "Generate Google long-tail queries + SEO for a driver archive storefront."
        )
        data = client.chat_json(LONGTAIL_SYSTEM, user, temperature=0.35)
        if not data:
            return fallback
        queries = [
            str(q).strip()
            for q in (data.get("queries") or [])
            if str(q).strip() and len(str(q).strip()) > 6
        ]
        pains = [
            str(p).strip()
            for p in (data.get("pain_points") or [])
            if str(p).strip()
        ]
        if not queries:
            queries = fallback["queries"]
        if not pains:
            pains = fallback["pain_points"]
        return {
            "queries": (queries + fallback["queries"])[:8],
            "pain_points": pains[:4],
            "seo_title": str(data.get("seo_title") or fallback["seo_title"])[:70],
            "seo_description": str(
                data.get("seo_description") or fallback["seo_description"]
            )[:320],
            "tags": [
                str(t).strip().lower()
                for t in (data.get("tags") or fallback["tags"])
                if str(t).strip()
            ][:12],
            "ai_used": True,
            "ai_backend": "ollama_longtail",
        }
    except Exception as exc:
        logger.info("长尾 AI 生成失败，规则兜底: %s", exc)
        return fallback


def search_archive_org(
    session: requests.Session, query: str, *, max_files: int = 6
) -> list[str]:
    """Archive.org advancedsearch → metadata → 文件直链。"""
    # 收紧到软件/文件类，避免百科条目
    q = f"({query}) AND (mediatype:software OR mediatype:texts OR collection:open_source_software)"
    api = "https://archive.org/advancedsearch.php"
    try:
        resp = session.get(
            api,
            params={
                "q": q,
                "fl[]": ["identifier", "title"],
                "rows": 5,
                "page": 1,
                "output": "json",
            },
            timeout=18,
        )
        if resp.status_code != 200:
            return []
        docs = ((resp.json().get("response") or {}).get("docs")) or []
    except Exception as exc:
        logger.debug("archive.org 搜索失败: %s", exc)
        return []

    links: list[str] = []
    for doc in docs:
        ident = str(doc.get("identifier") or "").strip()
        if not ident:
            continue
        try:
            meta = session.get(
                f"https://archive.org/metadata/{ident}", timeout=15
            )
            if meta.status_code != 200:
                continue
            files = (meta.json() or {}).get("files") or []
            for f in files:
                name = str(f.get("name") or "")
                low = name.lower()
                if not any(low.endswith(ext) for ext in DIRECT_EXTS):
                    continue
                # 跳过衍生小文件
                if low.endswith(".xml") or "/__ia_thumb" in low:
                    continue
                # 保留文件名中的路径分隔；不要 quote_plus 整段
                url = f"https://archive.org/download/{ident}/{name}"
                if url not in links:
                    links.append(url)
                if len(links) >= max_files:
                    return links
        except Exception as exc:
            logger.debug("archive.org metadata %s: %s", ident, exc)
            continue
        time.sleep(0.3)
    return links


def search_github_releases(
    session: requests.Session, model: str, *, max_files: int = 6
) -> list[str]:
    """GitHub 仓库搜索 → Releases 资产直链。"""
    q = f"{model} driver"
    try:
        resp = session.get(
            "https://api.github.com/search/repositories",
            params={"q": q, "sort": "stars", "order": "desc", "per_page": 5},
            headers={"Accept": "application/vnd.github+json"},
            timeout=18,
        )
        if resp.status_code != 200:
            return []
        repos = (resp.json() or {}).get("items") or []
    except Exception as exc:
        logger.debug("GitHub 仓库搜索失败: %s", exc)
        return []

    links: list[str] = []
    for repo in repos:
        full = repo.get("full_name")
        if not full:
            continue
        try:
            rel = session.get(
                f"https://api.github.com/repos/{full}/releases",
                params={"per_page": 3},
                headers={"Accept": "application/vnd.github+json"},
                timeout=15,
            )
            if rel.status_code != 200:
                continue
            for release in rel.json() or []:
                for asset in release.get("assets") or []:
                    url = str(asset.get("browser_download_url") or "")
                    if _is_direct_file(url) and url not in links:
                        links.append(url)
                    if len(links) >= max_files:
                        return links
        except Exception as exc:
            logger.debug("GitHub releases %s: %s", full, exc)
            continue
        time.sleep(0.4)
    return links


def search_site_targeted(
    session: requests.Session,
    longtail_query: str,
    *,
    engine: str = "bing",
    dead_engines: Optional[set[str]] = None,
) -> list[str]:
    """用长尾词 + site: 限定公开归档站，再只保留直链命中。"""
    from seeker_agent import search_web

    found: list[str] = []
    for site in TARGET_SITES:
        q = f"{longtail_query} {site}"
        try:
            hits = search_web(
                session,
                q,
                engine=engine,
                max_results=6,
                _dead=dead_engines,
            )
        except Exception:
            hits = []
        for hit in hits:
            if _is_direct_file(hit) and hit not in found:
                found.append(hit)
        time.sleep(random.uniform(0.4, 0.9))
    return found


def append_sources(
    sources_path: Path,
    entries: list[tuple[str, str, str, str, str, float]],
) -> int:
    if not entries:
        return 0
    existing = _load_existing_sources(sources_path)
    lines = [
        "",
        f"# ---- 长尾痛点挖掘 {datetime.now().strftime('%Y-%m-%d %H:%M')} ----",
    ]
    n = 0
    for url, cat, device, os_ver, pain, score in entries:
        if url in existing:
            continue
        lines.append(f"{url}|{cat}|{device}|{os_ver}|{pain[:120]}|{score}")
        existing.add(url)
        n += 1
    if n:
        sources_path.parent.mkdir(parents=True, exist_ok=True)
        with sources_path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return n


def find_device_dir(vault_root: Path, category: str, model: str) -> Path:
    """兼容旧接口：仅返回逻辑路径，不创建本地目录。"""
    return vault_root / _sanitize(category) / _sanitize(model)


def sync_pain_metadata(
    vault_root: Path,
    row: dict[str, Any],
    bundle: dict[str, Any],
    *,
    longtail_queries: list[str],
    source_hits: list[str],
    create_empty_dirs: bool = True,
) -> Optional[Path]:
    """Link Curation：不再写 vault 侧车；SEO 随 cache 落盘。"""
    del vault_root, create_empty_dirs, row, bundle, longtail_queries, source_hits
    return None


def run_longtail_mining(
    *,
    whitelist_path: Path = DEFAULT_WHITELIST,
    sources_path: Path = DEFAULT_SOURCES,
    failed_path: Path = DEFAULT_FAILED,
    vault_root: Optional[Path] = None,
    cache_path: Path = DEFAULT_LONGTAIL_CACHE,
    limit_models: int = 12,
    max_new_links: int = 30,
    use_ai: bool = True,
    engine: str = "bing",
    should_stop: Optional[Callable[[], bool]] = None,
    create_empty_dirs: bool = False,
) -> dict[str, Any]:
    """主入口：长尾检索直链 → sources + SEO 缓存（无本地下载）。"""
    del vault_root, create_empty_dirs
    stop = should_stop or (lambda: False)
    items = load_whitelist_items(whitelist_path, limit=limit_models)
    failed = _load_failed(failed_path)
    existing = _load_existing_sources(sources_path)
    session = _session()
    dead_engines: set[str] = set()

    stats: dict[str, Any] = {
        "models": len(items),
        "queries": 0,
        "archive_hits": 0,
        "github_hits": 0,
        "site_hits": 0,
        "added": 0,
        "metadata_updated": 0,
        "ai_bundles": 0,
    }
    cache: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": [],
    }
    pending: list[tuple[str, str, str, str, str, float]] = []
    zero_streak = 0

    try:
        for row in items:
            if stop() or stats["added"] + len(pending) >= max_new_links:
                break
            if zero_streak >= 3 and stats["added"] == 0 and not pending:
                print("[长尾] 连续 0 链，提前结束以免拖死验链", flush=True)
                stats["early_stop"] = "zero_streak"
                break
            model = str(row.get("device_model") or "").strip()
            cat = _sanitize(str(row.get("device_category") or "工控设备"))
            score = float(row.get("value_score") or 0)
            bundle = (
                generate_longtail_with_ai(row) if use_ai else rule_longtail_bundle(row)
            )
            if bundle.get("ai_used"):
                stats["ai_bundles"] += 1

            queries = list(bundle.get("queries") or [])
            expanded = list(queries)
            for q in queries[:2]:
                expanded.append(f"{q} site:archive.org OR site:github.com")
            stats["queries"] += len(expanded)

            found_urls: list[str] = []
            for q in queries[:3]:
                if stop():
                    break
                for url in search_archive_org(session, q, max_files=4):
                    if url in existing or url in failed:
                        continue
                    if url not in found_urls:
                        found_urls.append(url)
                        stats["archive_hits"] += 1
                try:
                    from stop_utils import StopRequested, interruptible_sleep

                    interruptible_sleep(
                        random.uniform(0.3, 0.7), stop, label="longtail"
                    )
                except StopRequested:
                    break

            if not stop():
                for url in search_github_releases(session, model, max_files=4):
                    if url in existing or url in failed:
                        continue
                    if url not in found_urls:
                        found_urls.append(url)
                        stats["github_hits"] += 1

            for q in queries[:3]:
                if stop() or len(found_urls) >= 10:
                    break
                for url in search_site_targeted(
                    session, q, engine=engine, dead_engines=dead_engines
                ):
                    if url in existing or url in failed:
                        continue
                    if url not in found_urls:
                        found_urls.append(url)
                        stats["site_hits"] += 1

            pain_label = (bundle.get("pain_points") or ["legacy driver demand"])[0]
            for url in found_urls:
                if url in existing:
                    continue
                pending.append((url, cat, _sanitize(model), "通用", pain_label, score))
                existing.add(url)

            cache["items"].append(
                {
                    "model": model,
                    "category": cat,
                    "queries": queries,
                    "pain_points": bundle.get("pain_points"),
                    "seo_title": bundle.get("seo_title"),
                    "seo_description": bundle.get("seo_description"),
                    "links_found": found_urls,
                    "ai_used": bundle.get("ai_used"),
                }
            )
            print(
                f"[长尾] {model}: queries={len(queries)} links={len(found_urls)} "
                f"seo={str(bundle.get('seo_title', ''))[:50]}",
                flush=True,
            )
            if found_urls:
                zero_streak = 0
            else:
                zero_streak += 1

            if pending and len(pending) >= 8:
                n = append_sources(sources_path, pending)
                stats["added"] += n
                pending = []
    finally:
        session.close()

    if pending:
        stats["added"] += append_sources(sources_path, pending)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"[长尾完成] models={stats['models']} added={stats['added']} "
        f"archive={stats['archive_hits']} github={stats['github_hits']} "
        f"site={stats['site_hits']} ai={stats['ai_bundles']}",
        flush=True,
    )
    print(f"[长尾缓存] {cache_path}", flush=True)
    return stats


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Google 长尾需求与痛点挖掘")
    p.add_argument("--limit", type=int, default=12)
    p.add_argument("--max-links", type=int, default=30)
    p.add_argument("--engine", choices=("ddg", "bing"), default="bing")
    p.add_argument("--no-ai", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    run_longtail_mining(
        limit_models=max(1, args.limit),
        max_new_links=max(1, args.max_links),
        use_ai=not args.no_ai,
        engine=args.engine,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
