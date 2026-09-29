"""
清洗脚本：读取 status='pending' 的原始数据，提取设备型号、适用系统，
生成 SEO 规范标题；并经「工业/硬件高价值驱动」黑名单过滤与打分。

- 消费级娱乐软件（MSN/QQ/游戏/影音/聊天等）→ 直接丢弃（deleted, score=0）
- 工业/硬件关键词加权 → 写入 value_score
- 仅高价值记录可进入 cleaned，供后续审核

用法：
    python cleaner.py
    python cleaner.py --limit 50
    python cleaner.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup
from tqdm import tqdm

from database import Database, Status
from value_scoring import is_blacklisted, score_from_row

logger = logging.getLogger(__name__)

SEO_TITLE_MIN = 25
SEO_TITLE_MAX = 60

_OS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"windows\s*11|\bwin\s*11\b", re.I), "Win11"),
    (re.compile(r"windows\s*10|\bwin\s*10\b", re.I), "Win10"),
    (re.compile(r"windows\s*8\.1|\bwin\s*8\.1\b", re.I), "Win8.1"),
    (re.compile(r"windows\s*8(?!\.1)|\bwin\s*8\b(?!\.1)", re.I), "Win8"),
    (re.compile(r"windows\s*7|\bwin\s*7\b|\bwin7\b", re.I), "Win7"),
    (re.compile(r"windows\s*vista|\bvista\b", re.I), "Vista"),
    (re.compile(r"windows\s*xp|\bwin\s*xp\b|\bxp\b", re.I), "XP"),
    (re.compile(r"windows\s*2000|\bwin2k\b|\bwin\s*2000\b", re.I), "Win2000"),
    (re.compile(r"windows\s*98|\bwin98\b", re.I), "Win98"),
    (re.compile(r"windows\s*me\b|\bwin\s*me\b", re.I), "WinME"),
    (re.compile(r"\bmacos\b|\bmac\s*os\s*x\b|\bos\s*x\b|\bmacintosh\b", re.I), "macOS"),
    (re.compile(r"\blinux\b|\bubuntu\b|\bdebian\b", re.I), "Linux"),
]

_URL_OS_HINTS: list[tuple[str, str]] = [
    ("/windows/", "Win7"),
    ("/mac/", "macOS"),
    ("/linux/", "Linux"),
]

_VERSION_TAIL_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<ver>\d+(?:\.\d+)+(?:\s*\([^)]+\))?)$",
    re.IGNORECASE,
)
_MODEL_TOKEN_RE = re.compile(r"\b([A-Z]{1,6}[-_]?\d{1,5}[A-Z0-9]*)\b")
_NOISE_TITLE_RE = re.compile(
    r"\s*[-_|–—]\s*(oldversion\.com|download|free\s*download).*$",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class CleanResult:
    device_model: str
    os_version: str
    seo_title: str
    title: str
    value_score: float = 0.0
    discarded: bool = False
    discard_reason: str = ""


def _plain_text(html: str) -> str:
    if not html:
        return ""
    if html.lstrip().startswith("[fetch_error]"):
        return html
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return _WHITESPACE_RE.sub(" ", soup.get_text(" ", strip=True))


def _page_heading(html: str) -> Optional[str]:
    if not html or html.lstrip().startswith("[fetch_error]"):
        return None
    soup = BeautifulSoup(html, "html.parser")
    for selector in ("h1", "title"):
        node = soup.find(selector)
        if node:
            text = _WHITESPACE_RE.sub(" ", node.get_text(" ", strip=True))
            text = _NOISE_TITLE_RE.sub("", text).strip(" -_|")
            if text:
                return text
    return None


def extract_os_versions(
    *,
    title: str = "",
    device_model: str = "",
    raw_content: str = "",
    source_url: str = "",
) -> list[str]:
    blob = " ".join(
        part for part in (title, device_model, _plain_text(raw_content), source_url) if part
    )
    found: list[str] = []
    seen: set[str] = set()
    for pattern, label in _OS_PATTERNS:
        if pattern.search(blob) and label not in seen:
            seen.add(label)
            found.append(label)

    if found:
        return found

    path = urlparse(source_url).path.lower()
    for hint, label in _URL_OS_HINTS:
        if hint in path and label not in seen:
            if label == "Win7":
                return ["XP", "Win7", "Win10"]
            return [label]

    return ["通用"]


def _normalize_model(text: str) -> str:
    text = _WHITESPACE_RE.sub(" ", text).strip(" -_|./")
    text = _NOISE_TITLE_RE.sub("", text).strip(" -_|")
    return text


def extract_device_model(
    *,
    title: Optional[str],
    device_model: Optional[str],
    raw_content: str = "",
    source_url: str = "",
) -> str:
    if device_model:
        cleaned = _normalize_model(device_model)
        if cleaned and not re.fullmatch(r"\d+(?:\.\d+)+", cleaned):
            return cleaned

    heading = _page_heading(raw_content) or ""
    raw_title = _normalize_model(title or "")

    for candidate in (heading, raw_title):
        if not candidate:
            continue
        match = _VERSION_TAIL_RE.match(candidate)
        if match:
            return f"{match.group('name').strip()} {match.group('ver').strip()}"
        tokens = _MODEL_TOKEN_RE.findall(candidate)
        if tokens:
            return candidate if len(candidate) <= 80 else tokens[0]
        if candidate:
            return candidate

    path = unquote(urlparse(source_url).path).rstrip("/")
    slug = path.split("/")[-1] if path else ""
    slug = re.sub(r"\.html?$", "", slug, flags=re.I)
    slug = slug.replace("-", " ").replace("_", " ").strip()
    if slug:
        return _normalize_model(slug)
    if device_model:
        return _normalize_model(device_model)
    return "未知型号"


_OS_DISPLAY_ORDER = [
    "Win98", "WinME", "Win2000", "XP", "Vista", "Win7",
    "Win8", "Win8.1", "Win10", "Win11", "macOS", "Linux", "通用",
]


def format_os_version(os_list: list[str]) -> str:
    if not os_list:
        return "通用"
    normalized: list[str] = []
    for item in os_list:
        label = "XP" if item in {"WinXP", "XP"} else item
        if label not in normalized:
            normalized.append(label)
    order = {name: idx for idx, name in enumerate(_OS_DISPLAY_ORDER)}
    normalized.sort(key=lambda x: order.get(x, len(order)))
    return "/".join(normalized)


def build_seo_title(device_model: str, os_version: str) -> str:
    model = _normalize_model(device_model) or "软件"
    os_part = os_version if os_version and os_version != "通用" else "多系统"
    driver_hint = re.search(
        r"驱动|driver|网卡|显卡|芯片|chipset|adapter|固件|bios|pci|采集|主板",
        model,
        re.I,
    )
    noun = "驱动下载" if driver_hint else "下载"
    candidates = [
        f"{model} {noun} - 支持{os_part}",
        f"{model} 官方{noun} | 适用{os_part}",
        f"{model} - {os_part}兼容版{noun}",
    ]
    for title in candidates:
        title = _WHITESPACE_RE.sub(" ", title).strip()
        if SEO_TITLE_MIN <= len(title) <= SEO_TITLE_MAX:
            return title
    title = candidates[0]
    if len(title) > SEO_TITLE_MAX:
        keep = SEO_TITLE_MAX - len(f" {noun} - 支持{os_part}")
        short_model = model if keep < 8 else model[:keep].rstrip()
        title = f"{short_model} {noun} - 支持{os_part}"
        if len(title) > SEO_TITLE_MAX:
            title = title[: SEO_TITLE_MAX - 1].rstrip() + "…"
    elif len(title) < SEO_TITLE_MIN:
        title = f"{model} 老旧版本{noun} - 支持{os_part}免费获取"
        if len(title) > SEO_TITLE_MAX:
            title = title[:SEO_TITLE_MAX]
    return _WHITESPACE_RE.sub(" ", title).strip()


def clean_item(row: dict) -> CleanResult:
    """清洗 + 高价值过滤打分。"""
    title = row.get("title") or ""
    raw_content = row.get("raw_content") or ""
    source_url = row.get("source_url") or ""
    existing_model = row.get("device_model")
    plain = _plain_text(raw_content)

    blocked, hits = is_blacklisted(title, plain, existing_model, source_url)
    if blocked:
        return CleanResult(
            device_model=existing_model or title or "未知型号",
            os_version="",
            seo_title="",
            title=title,
            value_score=0.0,
            discarded=True,
            discard_reason="黑名单: " + ", ".join(hits[:6]),
        )

    device_model = extract_device_model(
        title=title,
        device_model=existing_model,
        raw_content=raw_content,
        source_url=source_url,
    )
    os_list = extract_os_versions(
        title=title,
        device_model=device_model,
        raw_content=raw_content,
        source_url=source_url,
    )
    os_version = format_os_version(os_list)
    seo_title = build_seo_title(device_model, os_version)

    scored = score_from_row(
        {
            **row,
            "title": title,
            "raw_content": plain,
            "device_model": device_model,
            "seo_title": seo_title,
        },
        device_model=device_model,
        confidence=0.5,
    )

    if scored.blacklisted or scored.value_score <= 0:
        return CleanResult(
            device_model=device_model,
            os_version=os_version,
            seo_title=seo_title,
            title=device_model,
            value_score=0.0,
            discarded=True,
            discard_reason="无工业/硬件高价值信号，已过滤",
        )

    return CleanResult(
        device_model=device_model,
        os_version=os_version,
        seo_title=seo_title,
        title=device_model,
        value_score=scored.value_score,
        discarded=False,
    )


def run_cleaner(*, limit: int = 100, dry_run: bool = False) -> dict[str, int]:
    stats = {"total": 0, "cleaned": 0, "discarded": 0, "failed": 0}

    with Database() as db:
        rows = db.list_by_status(Status.PENDING, limit=limit)
        stats["total"] = len(rows)

        for row in tqdm(rows, desc="清洗驱动页", unit="条"):
            item_id = row["id"]
            try:
                print(
                    f"[正在清洗] 标题: {(row.get('title') or '')[:80]} | id={item_id}",
                    flush=True,
                )
                result = clean_item(row)
                if result.discarded:
                    logger.info(
                        "丢弃 id=%s score=0 reason=%s title=%s",
                        item_id,
                        result.discard_reason,
                        (row.get("title") or "")[:60],
                    )
                    if not dry_run:
                        db.discard_item(
                            item_id,
                            reason=result.discard_reason,
                            title=result.title or row.get("title"),
                            device_model=result.device_model,
                        )
                    stats["discarded"] += 1
                    continue

                logger.info(
                    "清洗 id=%s model=%s os=%s score=%.2f seo=%s",
                    item_id,
                    result.device_model,
                    result.os_version,
                    result.value_score,
                    result.seo_title,
                )
                if not dry_run:
                    db.mark_cleaned(
                        item_id,
                        device_model=result.device_model,
                        os_version=result.os_version,
                        seo_title=result.seo_title,
                        title=result.title,
                        value_score=result.value_score,
                    )
                stats["cleaned"] += 1
            except Exception as exc:
                stats["failed"] += 1
                logger.exception("清洗失败 id=%s: %s", item_id, exc)
                if not dry_run:
                    db.update_status(item_id, Status.ERROR, value_score=0.0)

    return stats


def rescore_cleaned(*, dry_run: bool = False) -> dict[str, int]:
    """对已 cleaned 的驱动页按新模型重打分；黑名单/零分改为 deleted。"""
    stats = {"total": 0, "kept": 0, "discarded": 0}
    with Database() as db:
        rows = db.list_by_status(Status.CLEANED, limit=10000)
        stats["total"] = len(rows)
        for row in tqdm(rows, desc="重算价值分", unit="条"):
            item_id = row["id"]
            # 用标题+正文重新判定（兼容旧清洗结果）
            result = clean_item({**row, "status": "pending"})
            if result.discarded:
                logger.info(
                    "重算丢弃 id=%s reason=%s title=%s",
                    item_id,
                    result.discard_reason,
                    (row.get("title") or "")[:60],
                )
                if not dry_run:
                    db.discard_item(
                        item_id,
                        reason=result.discard_reason,
                        title=row.get("title"),
                        device_model=row.get("device_model"),
                    )
                stats["discarded"] += 1
            else:
                logger.info(
                    "重算保留 id=%s score=%.2f model=%s",
                    item_id,
                    result.value_score,
                    result.device_model,
                )
                if not dry_run:
                    db.mark_cleaned(
                        item_id,
                        device_model=result.device_model,
                        os_version=result.os_version or (row.get("os_version") or "通用"),
                        seo_title=result.seo_title or (row.get("seo_title") or ""),
                        title=result.title,
                        value_score=result.value_score,
                    )
                stats["kept"] += 1
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="清洗 pending 数据（含工业硬件高价值过滤打分）并生成 SEO 标题",
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--rescore",
        action="store_true",
        help="对已 cleaned 数据按黑名单/价值分重算，娱乐软件会被丢弃",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.rescore:
        stats = rescore_cleaned(dry_run=args.dry_run)
        suffix = " (dry-run)" if args.dry_run else ""
        logger.info(
            "重算结束%s: total=%s kept=%s discarded=%s",
            suffix,
            stats["total"],
            stats["kept"],
            stats["discarded"],
        )
        return 0

    stats = run_cleaner(limit=args.limit, dry_run=args.dry_run)
    suffix = " (dry-run)" if args.dry_run else ""
    logger.info(
        "清洗结束%s: total=%s cleaned=%s discarded=%s failed=%s",
        suffix,
        stats["total"],
        stats["cleaned"],
        stats["discarded"],
        stats["failed"],
    )
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
