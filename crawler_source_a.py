"""
Source A 爬虫：抓取公开老旧软件/驱动列表站（OldVersion.com）。

特性：
- 每次请求随机延迟 3–10 秒
- 按数据库 source_url 去重
- 失败时指数退避重试（复用 utils.fetch）
- 将原始 HTML 写入 SQLite（status=pending）

用法：
    python crawler_source_a.py
    python crawler_source_a.py --max-items 5
    python crawler_source_a.py --list-url https://www.oldversion.com/windows/
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urldefrag, urlparse

from bs4 import BeautifulSoup
from tqdm import tqdm

from database import Database, Status
from utils import RequestError, absolute_url, create_session, fetch

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Source A 目标站点配置（公开老旧软件/驱动归档站）
# ---------------------------------------------------------------------------
SOURCE_A = {
    "name": "source_a",
    "base_url": "https://www.oldversion.com",
    "list_urls": [
        "https://www.oldversion.com/windows/",
        "https://www.oldversion.com/mac/",
        "https://www.oldversion.com/linux/",
    ],
    # 详情页链接需落在这些路径下
    "item_path_prefixes": ("/windows/", "/mac/", "/linux/"),
    # 排除分类首页等非详情链接
    "exclude_path_exact": {
        "/windows/",
        "/mac/",
        "/linux/",
        "/windows",
        "/mac",
        "/linux",
    },
}

DELAY_RANGE = (3.0, 10.0)
MAX_RETRIES = 3
BACKOFF_FACTOR = 2.0
REQUEST_TIMEOUT = 30.0

# 从标题中提取版本号，用作 device_model 的兜底
_VERSION_RE = re.compile(
    r"(?P<ver>\d+(?:\.\d+)+(?:\s*\([^)]+\))?)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ListItem:
    title: str
    source_url: str
    device_model: Optional[str] = None


def _normalize_url(url: str) -> str:
    """去掉 fragment，统一为绝对 URL。"""
    url, _frag = urldefrag(url.strip())
    return url


def _guess_device_model(title: str) -> Optional[str]:
    """从标题末尾猜测版本/型号。"""
    text = " ".join(title.split())
    match = _VERSION_RE.search(text)
    if match:
        return match.group("ver").strip()
    return text or None


def _is_item_url(url: str, base_url: str) -> bool:
    parsed = urlparse(url)
    base = urlparse(base_url)
    if parsed.netloc and parsed.netloc.lower() != base.netloc.lower():
        return False

    path = parsed.path or "/"
    if path in SOURCE_A["exclude_path_exact"]:
        return False

    prefixes: tuple[str, ...] = SOURCE_A["item_path_prefixes"]
    if not any(path.startswith(p) for p in prefixes):
        return False

    # 分类首页形如 /windows/ 已排除；要求路径比前缀更深
    for prefix in prefixes:
        if path.startswith(prefix) and len(path) > len(prefix):
            return True
    return False


def parse_list_page(html: str, page_url: str) -> list[ListItem]:
    """从列表页解析软件/驱动条目链接。"""
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    items: list[ListItem] = []

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue

        full_url = _normalize_url(absolute_url(page_url, href))
        if full_url in seen:
            continue
        if not _is_item_url(full_url, SOURCE_A["base_url"]):
            continue

        title = " ".join(anchor.get_text(" ", strip=True).split())
        if not title:
            # 用路径最后一段作标题兜底
            title = urlparse(full_url).path.rstrip("/").split("/")[-1]

        seen.add(full_url)
        items.append(
            ListItem(
                title=title,
                source_url=full_url,
                device_model=_guess_device_model(title),
            )
        )

    logger.info("列表页解析到 %s 条候选链接: %s", len(items), page_url)
    return items


def fetch_page(url: str, session) -> str:
    """带 3–10 秒随机延迟与指数退避重试的页面抓取。"""
    response = fetch(
        url,
        session=session,
        timeout=REQUEST_TIMEOUT,
        max_retries=MAX_RETRIES,
        backoff_factor=BACKOFF_FACTOR,
        delay_range=DELAY_RANGE,
    )
    response.encoding = response.apparent_encoding or response.encoding or "utf-8"
    return response.text


def collect_list_items(
    session,
    list_urls: Iterable[str],
) -> list[ListItem]:
    """抓取多个列表页并合并去重。"""
    merged: dict[str, ListItem] = {}
    for list_url in list_urls:
        try:
            html = fetch_page(list_url, session)
        except RequestError as exc:
            logger.error("列表页抓取失败（已重试耗尽）: %s | %s", list_url, exc)
            continue

        for item in parse_list_page(html, list_url):
            merged.setdefault(item.source_url, item)

    return list(merged.values())


def save_item(
    db: Database,
    item: ListItem,
    raw_content: str,
    *,
    status: Status = Status.PENDING,
) -> Optional[int]:
    """写入 SQLite；若 source_url 已存在则跳过。"""
    if db.exists_url(item.source_url):
        logger.info("已存在，跳过: %s", item.source_url)
        print(f"[跳过-已存在] 标题: {item.title} | 来源: {item.source_url}", flush=True)
        return None

    item_id = db.insert_item(
        title=item.title,
        device_model=item.device_model,
        source_url=item.source_url,
        raw_content=raw_content,
        status=status,
    )
    total = _count_scraped(db)
    logger.info("已入库 id=%s title=%s url=%s", item_id, item.title, item.source_url)
    print(f"[成功入库] 当前总数: {total} 条", flush=True)
    return item_id


def _count_scraped(db: Database) -> int:
    row = db._conn.execute("SELECT COUNT(*) AS cnt FROM scraped_items").fetchone()
    return int(row["cnt"]) if row else 0


def crawl(
    *,
    list_urls: Optional[list[str]] = None,
    max_items: Optional[int] = None,
    skip_detail: bool = False,
) -> dict[str, int]:
    """
    执行 Source A 抓取流程。

    返回统计：found / skipped / saved / failed
    """
    urls = list_urls or list(SOURCE_A["list_urls"])
    stats = {"found": 0, "skipped": 0, "saved": 0, "failed": 0}

    with Database() as db, create_session() as session:
        items = collect_list_items(session, urls)
        stats["found"] = len(items)

        if max_items is not None:
            items = items[: max(0, max_items)]

        for item in tqdm(items, desc="抓取详情页", unit="页"):
            print(
                f"[正在抓取] 标题: {item.title} | 来源: {item.source_url}",
                flush=True,
            )

            if db.exists_url(item.source_url):
                stats["skipped"] += 1
                logger.info("去重跳过: %s", item.source_url)
                print(f"[跳过-已存在] 标题: {item.title}", flush=True)
                continue

            if skip_detail:
                item_id = save_item(db, item, raw_content="", status=Status.PENDING)
                if item_id is None:
                    stats["skipped"] += 1
                else:
                    stats["saved"] += 1
                continue

            try:
                raw_html = fetch_page(item.source_url, session)
            except RequestError as exc:
                stats["failed"] += 1
                logger.error(
                    "详情页抓取失败（已指数退避重试）: %s | %s",
                    item.source_url,
                    exc,
                )
                print(f"[抓取失败] 标题: {item.title} | {exc}", flush=True)
                save_item(
                    db,
                    item,
                    raw_content=f"[fetch_error] {exc}",
                    status=Status.ERROR,
                )
                continue

            if db.exists_url(item.source_url):
                stats["skipped"] += 1
                continue

            item_id = save_item(db, item, raw_content=raw_html, status=Status.PENDING)
            if item_id is None:
                stats["skipped"] += 1
            else:
                stats["saved"] += 1

    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Source A：抓取公开老旧软件/驱动列表并写入 SQLite",
    )
    parser.add_argument(
        "--list-url",
        action="append",
        dest="list_urls",
        help="列表页 URL，可重复传入；默认使用 SOURCE_A 内置列表",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=None,
        help="最多抓取多少条详情（便于试跑）",
    )
    parser.add_argument(
        "--skip-detail",
        action="store_true",
        help="只解析列表、不抓详情页（调试用）",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="输出 DEBUG 日志",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    logger.info(
        "启动 Source A 爬虫 | delay=%s–%ss | retries=%s | backoff=%s",
        DELAY_RANGE[0],
        DELAY_RANGE[1],
        MAX_RETRIES,
        BACKOFF_FACTOR,
    )

    stats = crawl(
        list_urls=args.list_urls,
        max_items=args.max_items,
        skip_detail=args.skip_detail,
    )
    logger.info(
        "完成 | found=%(found)s skipped=%(skipped)s saved=%(saved)s failed=%(failed)s",
        stats,
    )
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
