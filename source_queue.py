"""
直链队列工具（Link Curation 纯血版）。

职责：白名单 → sources.txt、去重、失败名单、归档站回填。
不再包含本地下载/解压/vault 落盘。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCES = ROOT / "sources.txt"
DEFAULT_FAILED = ROOT / "failed_sources.txt"
DEFAULT_WHITELIST = ROOT / "data" / "急需设备型号白名单.json"
DEFAULT_SEARCH_RULES = ROOT / "data" / "search_rules.txt"
DEFAULT_LONGTAIL_CACHE = ROOT / "data" / "longtail_queries.json"

DIRECT_FILE_EXTS = {
    ".zip",
    ".rar",
    ".7z",
    ".iso",
    ".exe",
    ".msi",
    ".cab",
    ".img",
    ".bin",
    ".inf",
    ".sys",
}


def sanitize_name(name: str) -> str:
    name = (name or "").strip() or "unknown"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:120] or "unknown"


def is_direct_file_url(url: str) -> bool:
    try:
        from link_curator import is_direct_file_url as _lc

        return _lc(url)
    except Exception:
        path = unquote(urlparse(url).path).lower()
        return any(path.endswith(ext) for ext in DIRECT_FILE_EXTS)


def is_page_clue_url(url: str) -> bool:
    """论坛/Issue 帖页：只能当线索，不能当驱动直链。"""
    u = (url or "").lower()
    if not u.startswith("http"):
        return True
    if "/releases/download/" in u or is_direct_file_url(url):
        return False
    page_hosts = (
        "stackoverflow.com",
        "stackexchange.com",
        "superuser.com",
        "serverfault.com",
        "reddit.com",
        "www.reddit.com",
    )
    if any(h in u for h in page_hosts):
        return True
    if "github.com/" in u and any(
        x in u for x in ("/issues/", "/pull/", "/discussions/", "/commit/")
    ):
        return True
    return False


def load_failed_urls(path: Path) -> set[str]:
    if not path.exists():
        return set()
    urls: set[str] = set()
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        url = line.split("|", 1)[0].strip()
        if url.startswith(("http://", "https://")):
            urls.add(url)
    return urls


def dedupe_sources_file(path: Path) -> dict[str, int]:
    """sources.txt 按 URL 去重，保留首次出现；注释原样保留。"""
    if not path.exists():
        return {"before": 0, "after": 0, "removed": 0}
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    seen: set[str] = set()
    out: list[str] = []
    removed = 0
    active_before = 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            out.append(raw)
            continue
        active_before += 1
        url = line.split("|", 1)[0].strip()
        if url in seen:
            removed += 1
            continue
        seen.add(url)
        out.append(raw)
    path.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
    return {
        "before": active_before,
        "after": active_before - removed,
        "removed": removed,
    }


def ensure_sample_sources(path: Path) -> None:
    if path.exists():
        return
    path.write_text(
        "# url|hardware_category|device_name|os_version|pain_points|value_score\n"
        "# Link Curation 队列：只放真实可验的驱动/固件直链\n",
        encoding="utf-8",
    )


def sync_whitelist_to_sources(
    whitelist_path: Path,
    sources_path: Path,
    *,
    search_rules_path: Path = DEFAULT_SEARCH_RULES,
    min_score: float = 0.0,
    failed_urls: Optional[set[str]] = None,
    enqueue_pages: bool = False,
) -> dict[str, int]:
    """白名单 sample_urls → sources.txt（仅直链）；帖子线索只写注释 + 检索规则。"""
    if not whitelist_path.exists():
        raise FileNotFoundError(f"找不到白名单: {whitelist_path}")

    data = json.loads(whitelist_path.read_text(encoding="utf-8-sig"))
    items = list(data.get("items") or [])
    failed_urls = failed_urls or set()

    existing_urls: set[str] = set()
    existing_lines: list[str] = []
    if sources_path.exists():
        for raw in sources_path.read_text(encoding="utf-8-sig").splitlines():
            existing_lines.append(raw)
            line = raw.strip()
            if line and not line.startswith("#"):
                existing_urls.add(line.split("|", 1)[0].strip())

    added_direct = 0
    added_page = 0
    skipped_page = 0
    search_lines: list[str] = [
        "# 由急需设备型号白名单自动生成的检索规则",
        f"# generated_at: {datetime.now(timezone.utc).isoformat()}",
        "",
    ]
    new_source_lines: list[str] = [
        "",
        f"# ---- 白名单自动导入 {datetime.now().strftime('%Y-%m-%d %H:%M')} ----",
    ]

    for row in items:
        score = float(row.get("value_score") or 0)
        if score < min_score:
            continue
        category = sanitize_name(str(row.get("device_category") or "未分类"))
        device = sanitize_name(str(row.get("device_model") or "未知设备"))
        pains = row.get("pain_points") or []
        pain_text = "；".join(str(p) for p in pains[:2]) if pains else ""
        os_version = "通用"
        blob = " ".join([device, category, pain_text])
        if re.search(r"\bxp\b", blob, re.I):
            os_version = "XP"
        elif re.search(r"win\s*7|windows\s*7", blob, re.I):
            os_version = "Win7"

        search_lines.append(
            f"{device} | {category} | score={score} | "
            f"query=`{device} driver download` OR `{device} 驱动 下载` OR `{device} {os_version}`"
        )
        if pain_text:
            search_lines.append(f"  pain: {pain_text}")

        urls = [
            u
            for u in (row.get("sample_urls") or [])
            if isinstance(u, str) and u.startswith("http")
        ]
        if not urls:
            new_source_lines.append(
                f"# SEARCH_ONLY|{category}|{device}|{os_version}|{pain_text[:80]}"
            )
            continue

        queued_any = False
        for url in urls:
            if url in existing_urls or url in failed_urls:
                continue
            if is_page_clue_url(url) and not enqueue_pages:
                skipped_page += 1
                new_source_lines.append(
                    f"# PAGE_CLUE|{url}|{category}|{device}|{os_version}|{pain_text[:80]}"
                )
                continue
            line = f"{url}|{category}|{device}|{os_version}|{pain_text[:120]}|{score}"
            new_source_lines.append(line)
            existing_urls.add(url)
            queued_any = True
            if is_direct_file_url(url):
                added_direct += 1
            else:
                added_page += 1
        if not queued_any and urls:
            new_source_lines.append(
                f"# SEARCH_ONLY|{category}|{device}|{os_version}|无直链，仅有帖子线索"
            )

    if len(new_source_lines) > 2:
        with sources_path.open("a", encoding="utf-8") as f:
            if existing_lines and existing_lines[-1].strip():
                f.write("\n")
            f.write("\n".join(new_source_lines) + "\n")
    else:
        ensure_sample_sources(sources_path)

    search_rules_path.parent.mkdir(parents=True, exist_ok=True)
    search_rules_path.write_text("\n".join(search_lines) + "\n", encoding="utf-8")

    stats = {
        "whitelist_items": len(items),
        "added_direct": added_direct,
        "added_page": added_page,
        "added_total": added_direct + added_page,
        "skipped_page_clues": skipped_page,
    }
    print(
        f"[闭环] 白名单 {stats['whitelist_items']} 条 -> 新增验链队列 "
        f"{stats['added_total']}（直链 {added_direct} / 其它 {added_page} / "
        f"跳过帖子线索 {skipped_page}）",
        flush=True,
    )
    print(f"[闭环] 检索规则: {search_rules_path}", flush=True)
    print(f"[闭环] 直链队列: {sources_path}", flush=True)
    return stats


def enqueue_scraped_for_whitelist(
    whitelist_path: Path,
    sources_path: Path,
    *,
    failed_urls: Optional[set[str]] = None,
    limit: int = 40,
) -> dict[str, int]:
    """从 scraped_items 按型号匹配，把归档站直链补进 sources.txt。"""
    from database import Database, Status

    failed_urls = failed_urls or set()
    if not whitelist_path.exists():
        return {"added": 0, "matched": 0}

    data = json.loads(whitelist_path.read_text(encoding="utf-8-sig"))
    models = []
    for row in data.get("items") or []:
        m = str(row.get("device_model") or "").strip()
        if m and m not in {"未知型号", "unknown", "未知设备"}:
            models.append(
                (
                    m.lower(),
                    sanitize_name(str(row.get("device_category") or "未分类")),
                    sanitize_name(m),
                    float(row.get("value_score") or 0),
                )
            )
    if not models:
        return {"added": 0, "matched": 0}

    existing: set[str] = set()
    if sources_path.exists():
        for raw in sources_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                existing.add(line.split("|", 1)[0].strip())

    added = 0
    matched = 0
    lines: list[str] = [
        "",
        f"# ---- 归档站库匹配白名单 {datetime.now().strftime('%Y-%m-%d %H:%M')} ----",
    ]
    with Database() as db:
        rows = db._conn.execute(
            """
            SELECT title, device_model, source_url, status, value_score
            FROM scraped_items
            WHERE status IN (?, ?)
            ORDER BY COALESCE(value_score, 0) DESC, id DESC
            LIMIT 800
            """,
            (Status.CLEANED.value, Status.PENDING.value),
        ).fetchall()

    for row in rows:
        if added >= limit:
            break
        title = str(row["title"] or "")
        model = str(row["device_model"] or "")
        url = str(row["source_url"] or "")
        if not url.startswith("http") or url in existing or url in failed_urls:
            continue
        if is_page_clue_url(url):
            continue
        blob = f"{title} {model}".lower()
        hit = None
        for key, category, device, score in models:
            if key in blob or (len(key) >= 4 and key in url.lower()):
                hit = (category, device, score)
                break
        if not hit:
            continue
        matched += 1
        category, device, score = hit
        lines.append(f"{url}|{category}|{device}|通用|白名单型号归档站匹配|{score}")
        existing.add(url)
        added += 1

    if added:
        sources_path.parent.mkdir(parents=True, exist_ok=True)
        with sources_path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    print(
        f"[变通] 归档站匹配白名单: matched={matched} 新入队={added}",
        flush=True,
    )
    return {"added": added, "matched": matched}


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="直链队列同步（Link Curation）")
    p.add_argument("--whitelist", type=Path, default=DEFAULT_WHITELIST)
    p.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    p.add_argument("--failed", type=Path, default=DEFAULT_FAILED)
    p.add_argument("--search-rules", type=Path, default=DEFAULT_SEARCH_RULES)
    p.add_argument("--min-score", type=float, default=0.0)
    p.add_argument("--sync-only", action="store_true", default=True)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    failed = load_failed_urls(args.failed)
    sync_whitelist_to_sources(
        args.whitelist,
        args.sources,
        search_rules_path=args.search_rules,
        min_score=args.min_score,
        failed_urls=failed,
        enqueue_pages=False,
    )
    dedupe_sources_file(args.sources)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
