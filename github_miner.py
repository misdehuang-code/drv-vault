"""
从白名单帖子线索反挖 GitHub Releases 真包直链。

论坛/Issue 本身不能下，但帖子里的仓库往往有 releases/assets。
这是目前命中率最高的获链路径之一。
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
DEFAULT_WHITELIST = ROOT / "data" / "急需设备型号白名单.json"
DEFAULT_SOURCES = ROOT / "sources.txt"
DEFAULT_FAILED = ROOT / "failed_sources.txt"

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
    ".inf",
    ".sys",
)

_REPO_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:/|$)",
    re.I,
)


def _session() -> requests.Session:
    from github_auth import get_github_token

    s = requests.Session()
    s.trust_env = False
    headers = {
        "User-Agent": "DrvVaultMiner/1.2 (+local commercial pipeline)",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = get_github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    s.headers.update(headers)
    return s


def _is_direct(url: str) -> bool:
    path = urlparse(url).path.lower()
    low = (url or "").lower()
    if any(
        k in low
        for k in (
            "armbian",
            "amlogic",
            "rootfs",
            "tensorrt_sample",
            "sample_data",
            "linux-x64",
            "linux-aarch64",
            "-linux-",
            ".tar.gz",
        )
    ):
        return False
    if path.endswith((".gz", ".tar", ".tgz", ".deb", ".rpm", ".sig", ".asc")):
        return False
    if "/releases/download/" in low:
        return True
    return any(path.endswith(ext) for ext in DIRECT_EXTS)


def _load_lines(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out: set[str] = set()
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line.split("|", 1)[0].strip())
    return out


def extract_repos_from_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    repos: list[str] = []
    for u in urls:
        m = _REPO_RE.search(u or "")
        if not m:
            continue
        owner, repo = m.group(1), m.group(2)
        if repo.endswith(".git"):
            repo = repo[:-4]
        # 跳过明显非仓库路径
        if owner.lower() in {"topics", "search", "orgs", "settings"}:
            continue
        if repo.lower() in {"issues", "pulls", "actions", "releases", "wiki"}:
            continue
        key = f"{owner}/{repo}"
        if key.lower() in seen:
            continue
        seen.add(key.lower())
        repos.append(key)
    return repos


def fetch_release_assets(
    session: requests.Session, full_name: str, *, max_assets: int = 12
) -> list[str]:
    from github_auth import api_allowed, html_release_assets, note_rate_limit_headers

    links: list[str] = []

    def _eat(payload: Any) -> None:
        nonlocal links
        releases = payload if isinstance(payload, list) else [payload]
        for rel in releases:
            if not isinstance(rel, dict):
                continue
            for asset in rel.get("assets") or []:
                url = str(asset.get("browser_download_url") or "")
                name = str(asset.get("name") or "")
                if not url or not (_is_direct(url) or _is_direct(name)):
                    continue
                try:
                    from link_curator import is_non_driver_url

                    if is_non_driver_url(url, name):
                        continue
                except Exception:
                    pass
                if url not in links:
                    links.append(url)
                if len(links) >= max_assets:
                    return

    if api_allowed():
        try:
            resp = session.get(
                f"https://api.github.com/repos/{full_name}/releases/latest",
                timeout=8,
            )
            note_rate_limit_headers(resp)
            if resp.status_code == 200:
                _eat(resp.json())
            elif resp.status_code in {403, 429}:
                logger.info("GitHub API limited — fallback HTML for %s", full_name)
            if len(links) >= max_assets:
                return links
            if resp.status_code not in {403, 429}:
                resp = session.get(
                    f"https://api.github.com/repos/{full_name}/releases",
                    params={"per_page": 4},
                    timeout=10,
                )
                note_rate_limit_headers(resp)
                if resp.status_code == 200:
                    _eat(resp.json() or [])
                elif resp.status_code == 404:
                    pass
        except Exception as exc:
            logger.debug("fetch releases api %s: %s", full_name, exc)

    if len(links) >= max_assets:
        return links
    # HTML expanded_assets：不耗 API 额度
    for url in html_release_assets(session, full_name, max_assets=max_assets):
        if url in links or not _is_direct(url):
            continue
        try:
            from link_curator import is_non_driver_url

            if is_non_driver_url(url):
                continue
        except Exception:
            pass
        links.append(url)
        if len(links) >= max_assets:
            break
    return links


def search_github_repos(
    session: requests.Session, query: str, *, per_page: int = 5
) -> list[str]:
    from github_auth import api_allowed, note_rate_limit_headers

    if not api_allowed():
        return []
    try:
        resp = session.get(
            "https://api.github.com/search/repositories",
            params={
                "q": (
                    f"{query} (driver OR firmware OR inf) windows "
                    f"(zip OR exe OR release) -valorant -ollama -bitcoin -vscode"
                ),
                "sort": "stars",
                "order": "desc",
                "per_page": per_page,
            },
            timeout=12,
        )
        note_rate_limit_headers(resp)
        if resp.status_code != 200:
            return []
        return [
            str(it.get("full_name"))
            for it in (resp.json() or {}).get("items") or []
            if it.get("full_name")
        ]
    except Exception as exc:
        logger.debug("github search %s: %s", query, exc)
        return []


def append_sources(
    sources_path: Path,
    entries: list[tuple[str, str, str, str, str, float]],
) -> int:
    if not entries:
        return 0
    # 与 seeker 并行写同一文件时串行化，避免交错损坏
    from sources_write_lock import SOURCES_LOCK

    with SOURCES_LOCK:
        existing = _load_lines(sources_path)
        lines = [
            "",
            f"# ---- GitHub矿工 {datetime.now().strftime('%Y-%m-%d %H:%M')} ----",
        ]
        n = 0
        for url, cat, device, os_ver, pain, score in entries:
            if url in existing:
                continue
            lines.append(f"{url}|{cat}|{device}|{os_ver}|{pain[:120]}|{score}")
            existing.add(url)
            n += 1
        if n:
            with sources_path.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        return n


def mine_github_for_whitelist(
    *,
    whitelist_path: Path = DEFAULT_WHITELIST,
    sources_path: Path = DEFAULT_SOURCES,
    failed_path: Path = DEFAULT_FAILED,
    limit_models: int = 30,
    max_new_links: int = 80,
    should_stop: Optional[Callable[[], bool]] = None,
    max_seconds: float = 90.0,
) -> dict[str, Any]:
    """主入口：帖子仓库 + 型号搜索 → Releases 资产直链。"""
    stop = should_stop or (lambda: False)
    from json_io import read_json
    from source_queue import sanitize_name
    from github_auth import get_github_token, token_status

    deadline = time.time() + max(30.0, float(max_seconds))
    st = token_status()
    tok_label = "yes" if st["has_token"] else "NO"
    if st["api_allowed"]:
        api_label = "ok"
    else:
        api_label = f"blocked {st['blocked_for_sec']}s"
    # 无 token 时少挖型号，避免 HTML 超时拖死整轮
    if not get_github_token():
        limit_models = min(int(limit_models), 10)
        max_seconds = min(float(max_seconds), 70.0)
        deadline = time.time() + max_seconds
    from github_auth import html_allowed

    html_on = html_allowed()
    print(
        f"[GitHub矿工] token={tok_label} api={api_label} "
        f"html={'on' if html_on else 'OFF'} budget={int(max_seconds)}s",
        flush=True,
    )
    # API+HTML 双不可用：立刻返回，别空烧 70s
    if not st["api_allowed"] and not html_on:
        print("[GitHub矿工] API/HTML 均不可用，本轮跳过", flush=True)
        return {
            "models": 0,
            "prefer_new": 0,
            "repos": 0,
            "assets_found": 0,
            "added": 0,
            "searched": 0,
            "skipped": True,
            "reason": "api_and_html_blocked",
        }

    data = read_json(whitelist_path, default={}) or {}
    raw_items = list(data.get("items") or [])
    # 优先尚未进店面的型号
    try:
        from seeker_agent import cataloged_models, _norm_model_key

        have = cataloged_models()
    except Exception:
        have = set()

        def _norm_model_key(m: str) -> str:  # type: ignore
            return (m or "").strip().lower()

    try:
        from seeker_agent import is_diggable_model
    except Exception:

        def is_diggable_model(m: str) -> bool:  # type: ignore
            return bool(m and len(str(m).strip()) >= 3)

    fresh = [
        it
        for it in raw_items
        if is_diggable_model(str(it.get("device_model") or ""))
        and _norm_model_key(str(it.get("device_model") or "")) not in have
    ]
    # PCI 硬件 ID 矿工仍可挖（DEV_ 搜索）；搜索引擎挖链会跳过
    pci_fresh = [
        it
        for it in raw_items
        if _norm_model_key(str(it.get("device_model") or "")) not in have
        and (
            "VEN_" in str(it.get("device_model") or "").upper()
            or "DEV_" in str(it.get("device_model") or "").upper()
        )
    ]
    known = [
        it
        for it in raw_items
        if _norm_model_key(str(it.get("device_model") or "")) in have
    ]
    merged_fresh = fresh + [x for x in pci_fresh if x not in fresh]
    items = (merged_fresh[: max(1, limit_models)] if merged_fresh else known[:3])
    failed = _load_lines(failed_path)
    existing = _load_lines(sources_path)
    session = _session()

    stats = {
        "models": len(items),
        "prefer_new": len(fresh),
        "repos": 0,
        "assets_found": 0,
        "added": 0,
        "searched": 0,
    }
    pending: list[tuple[str, str, str, str, str, float]] = []

    try:
        for row in items:
            if stop() or stats["added"] + len(pending) >= max_new_links:
                break
            if time.time() >= deadline:
                stats["timed_out"] = True
                print(
                    f"[GitHub矿工] 预算耗尽，截断 models_done={stats.get('models_done', 0)}",
                    flush=True,
                )
                break
            model = str(row.get("device_model") or "").strip()
            cat = sanitize_name(str(row.get("device_category") or "工控设备"))
            score = float(row.get("value_score") or 0)
            pains = row.get("pain_points") or []
            pain = "；".join(str(p) for p in pains[:2]) if pains else "github release"
            samples = [
                u
                for u in (row.get("sample_urls") or [])
                if isinstance(u, str) and u.startswith("http")
            ]

            repos = extract_repos_from_urls(samples)
            # 型号可搜时再补仓库（无 token 时搜索额度极紧，每型号最多搜 1 次）
            if len(repos) < 2 and model and len(model) >= 3 and time.time() < deadline:
                q_model = model
                m_dev = re.search(r"DEV_([0-9A-F]{4})", model, re.I)
                if m_dev:
                    q_model = f"DEV_{m_dev.group(1)}"
                if "\\" in q_model or len(q_model) > 40:
                    q_model = re.sub(r"[^A-Za-z0-9_\- ]+", " ", q_model)[:40].strip()
                if q_model:
                    stats["searched"] += 1
                    try:
                        from stop_utils import StopRequested, interruptible_sleep

                        interruptible_sleep(0.08, stop, label="github-search")
                    except StopRequested:
                        break
                    for full in search_github_repos(
                        session, q_model, per_page=3 if tok_label == "yes" else 2
                    ):
                        if full not in repos:
                            repos.append(full)

            # 搜到的娱乐/工具仓直接丢掉
            _junk_repo = re.compile(
                r"(?i)(valorant|ollama|vibepollo|vanitygen|bitcoin|powerbi|"
                r"vscode|discord|steam|photoshop|blender)"
            )
            repo_batch = [
                r
                for r in repos[:6]
                if not _junk_repo.search(r)
            ][: 4 if tok_label == "yes" else 3]
            if repo_batch and time.time() < deadline:
                from concurrent.futures import ThreadPoolExecutor, as_completed

                def _assets_one(full_name: str) -> list[str]:
                    if stop() or time.time() >= deadline:
                        return []
                    s2 = _session()
                    try:
                        return fetch_release_assets(s2, full_name, max_assets=8)
                    except Exception:
                        return []
                    finally:
                        s2.close()

                workers = min(2 if tok_label == "NO" else 3, len(repo_batch))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futs = {pool.submit(_assets_one, full): full for full in repo_batch}
                    for fut in as_completed(futs):
                        if stop() or stats["added"] + len(pending) >= max_new_links:
                            break
                        if time.time() >= deadline:
                            break
                        stats["repos"] += 1
                        try:
                            urls = fut.result(timeout=max(1.0, deadline - time.time()))
                        except Exception:
                            urls = []
                        for url in urls or []:
                            if url in existing or url in failed:
                                continue
                            pending.append(
                                (url, cat, sanitize_name(model), "通用", pain, score)
                            )
                            existing.add(url)
                            stats["assets_found"] += 1
                            if stats["added"] + len(pending) >= max_new_links:
                                break

            stats["models_done"] = int(stats.get("models_done") or 0) + 1
            if stats["models_done"] % 3 == 0 or pending:
                print(
                    f"[GitHub矿工] progress models={stats['models_done']}/{len(items)} "
                    f"assets={stats['assets_found']} pending={len(pending)}",
                    flush=True,
                )

            if len(pending) >= 8:
                n = append_sources(sources_path, pending)
                stats["added"] += n
                pending = []
                print(f"[GitHub矿工] 已写入 +{n} 累计={stats['added']}", flush=True)
    finally:
        session.close()

    if pending:
        stats["added"] += append_sources(sources_path, pending)

    print(
        f"[GitHub矿工完成] models={stats['models']} repos={stats['repos']} "
        f"assets={stats['assets_found']} added={stats['added']}",
        flush=True,
    )
    return stats
