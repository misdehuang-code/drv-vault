"""
定向检索公开问答/开源技术社区，抓取驱动相关帖子，
提取设备型号与报错代码，写入 SQLite（forum_posts 表）。

数据源（均为公开 API / 公开 JSON，礼貌限速）：
- Stack Exchange（Super User / Stack Overflow / Server Fault）
- Reddit（公开 search.json）
- GitHub Issues Search（公开 REST API）

关键词：
  驱动下载、找不到驱动、Error 43、老旧设备 驱动、工控卡 报错

用法：
    python crawler_forums.py
    python crawler_forums.py --max-per-query 10
    python crawler_forums.py --sources stackexchange,reddit
    python crawler_forums.py --dry-run
"""

from __future__ import annotations

import argparse
import html
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional, Callable

from bs4 import BeautifulSoup
from tqdm import tqdm

from database import Database, Status
from utils import RequestError, create_session, fetch

logger = logging.getLogger(__name__)

# Stack Exchange 无 key 时配额很紧；429 后本进程冷却一段时间
_SE_COOLDOWN_UNTIL = 0.0

# ---------------------------------------------------------------------------
# 检索配置
# ---------------------------------------------------------------------------
KEYWORDS = [
    "驱动下载",
    "找不到驱动",
    "Error 43",
    "老旧设备 驱动",
    "工控卡 报错",
]

# 国际社区补充英文等价检索词（同一关键词归并入库）
QUERY_VARIANTS: dict[str, list[str]] = {
    "驱动下载": ["驱动下载", "driver download windows"],
    "找不到驱动": [
        "找不到驱动",
        "driver not found",
        "missing driver windows",
    ],
    "Error 43": ["Error 43", "Code 43 graphics driver", "Windows has stopped this device Code 43"],
    "老旧设备 驱动": [
        "老旧设备 驱动",
        "legacy hardware driver",
        "old device driver windows",
    ],
    "工控卡 报错": [
        "工控卡 报错",
        "industrial PCIe card driver error",
        "DAQ card error windows",
        "motion control card driver",
    ],
}

STACK_SITES = ("superuser", "stackoverflow", "serverfault")
REDDIT_SUBREDDITS = (
    "techsupport",
    "Windows10",
    "Windows11",
    "hardware",
    "sysadmin",
)

DELAY_RANGE = (3.0, 8.0)
MAX_RETRIES = 3
BACKOFF_FACTOR = 2.0
TIMEOUT = 25.0
# Reddit 在国内常连不上：更短超时、连失败立刻放弃该源
REDDIT_TIMEOUT = 8.0
REDDIT_MAX_RETRIES = 1

# ---------------------------------------------------------------------------
# 提取规则：报错码 / 设备型号
# ---------------------------------------------------------------------------
_ERROR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bCode\s*[:#]?\s*43\b", re.I),
    re.compile(r"\bError\s*[:#]?\s*43\b", re.I),
    re.compile(r"\b错误\s*[:：]?\s*43\b"),
    re.compile(r"\b错误代码\s*[:：]?\s*\d{1,3}\b"),
    re.compile(r"\bCode\s*[:#]?\s*(\d{1,3})\b", re.I),
    re.compile(r"\bError\s*[:#]?\s*(0x[0-9A-Fa-f]{2,8}|\d{1,10})\b", re.I),
    re.compile(r"\b0x[0-9A-Fa-f]{8}\b"),
    re.compile(r"\bNTSTATUS\s*[:=]?\s*0x[0-9A-Fa-f]+\b", re.I),
    re.compile(r"\bHRESULT\s*[:=]?\s*0x[0-9A-Fa-f]+\b", re.I),
    re.compile(r"\bBSOD\s*[:=]?\s*[A-Z_]{4,}\b", re.I),
    re.compile(r"\b(?:IRQL_NOT_LESS_OR_EQUAL|SYSTEM_SERVICE_EXCEPTION|VIDEO_TDR_FAILURE)\b", re.I),
    re.compile(r"\b(?:蓝屏|报错|故障)\s*[:：]?\s*[A-Za-z0-9_\-]{2,40}"),
]

_MODEL_PATTERNS: list[re.Pattern[str]] = [
    # PCI / USB 硬件 ID
    re.compile(r"\bPCI\\VEN_[0-9A-F]{4}&DEV_[0-9A-F]{4}(?:&SUBSYS_[0-9A-F]{8})?", re.I),
    re.compile(r"\bUSB\\VID_[0-9A-F]{4}&PID_[0-9A-F]{4}\b", re.I),
    re.compile(r"\bVEN_[0-9A-F]{4}&DEV_[0-9A-F]{4}\b", re.I),
    # 常见网卡 / 显卡 / 芯片命名
    re.compile(r"\b(?:RTL|Realtek)\s*8\d{3}[A-Z0-9]*\b", re.I),
    re.compile(r"\b(?:BCM|Broadcom)\s*\d{4,5}[A-Z0-9]*\b", re.I),
    re.compile(r"\b(?:Intel)\s*(?:HD|UHD|Iris)\s*(?:Graphics\s*)?\d{3,4}\b", re.I),
    re.compile(r"\b(?:GTX|RTX|RX)\s*\d{3,4}\s*(?:Ti|SUPER)?\b", re.I),
    re.compile(r"\b(?:GeForce|Radeon)\s+(?:GTX|RTX|RX)?\s*\d{3,4}\s*(?:Ti|SUPER)?\b", re.I),
    re.compile(
        r"\b(?:Intel|AMD|NVIDIA|Realtek|Broadcom|Qualcomm|Atheros)"
        r"(?:\s+(?:HD|UHD|Iris|Graphics|Ethernet|Wi-?Fi|Wireless))? "
        r"[A-Za-z0-9\-]{2,20}\b",
        re.I,
    ),
    # 工控 / 采集卡常见写法
    re.compile(r"\b(?:研华|Advantech|研祥|凌华|ADLINK|NI|National Instruments)[\w\-\s]{0,20}\b", re.I),
    re.compile(r"\b(?:PCI|PCIe|ISA)\-?\s?(?:总线)?(?:采集|运动|工控)?卡\s*[A-Za-z0-9\-]*\b"),
    re.compile(r"\b[A-Z]{1,6}\-?\d{2,5}[A-Z]{0,3}\b"),
]


@dataclass
class ForumHit:
    source: str
    keyword: str
    title: str
    source_url: str
    raw_content: str
    device_model: str = ""
    error_codes: str = ""
    tags: list[str] = field(default_factory=list)


def _html_to_text(raw: str) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    if "<" in text and ">" in text:
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def extract_error_codes(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for pattern in _ERROR_PATTERNS:
        for match in pattern.finditer(text):
            value = re.sub(r"\s+", " ", match.group(0)).strip()
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(value)
    return found[:12]


def extract_device_models(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    # 过短/过泛的 token 过滤
    noise = {
        "windows",
        "error",
        "code",
        "driver",
        "device",
        "pcie",
        "pci",
        "usb",
        "http",
        "https",
    }
    for pattern in _MODEL_PATTERNS:
        for match in pattern.finditer(text):
            value = re.sub(r"\s+", " ", match.group(0)).strip(" -_,./")
            if len(value) < 3 or len(value) > 80:
                continue
            key = value.lower()
            if key in noise or key in seen:
                continue
            # 纯数字跳过
            if re.fullmatch(r"\d+", value):
                continue
            seen.add(key)
            found.append(value)
    return found[:10]


def enrich_hit(hit: ForumHit) -> ForumHit:
    blob = f"{hit.title}\n{hit.raw_content}"
    models = extract_device_models(blob)
    errors = extract_error_codes(blob)
    hit.device_model = " | ".join(models)
    hit.error_codes = " | ".join(errors)
    return hit


def api_get(session, url: str, params: Optional[dict] = None) -> dict | list:
    response = fetch(
        url,
        session=session,
        params=params,
        timeout=TIMEOUT,
        max_retries=MAX_RETRIES,
        backoff_factor=BACKOFF_FACTOR,
        delay_range=DELAY_RANGE,
        headers={"Accept": "application/json"},
    )
    return response.json()


# ---------------------------------------------------------------------------
# Stack Exchange
# ---------------------------------------------------------------------------
def _se_in_cooldown() -> bool:
    return time.time() < _SE_COOLDOWN_UNTIL


def _trip_se_cooldown(seconds: float = 900.0) -> None:
    global _SE_COOLDOWN_UNTIL
    _SE_COOLDOWN_UNTIL = max(_SE_COOLDOWN_UNTIL, time.time() + seconds)
    logger.warning(
        "StackExchange 进入冷却 %.0f 分钟（429/配额），本轮改走 GitHub 等源",
        seconds / 60.0,
    )


def search_stackexchange(
    session,
    *,
    keyword: str,
    query: str,
    max_items: int,
) -> list[ForumHit]:
    if _se_in_cooldown():
        logger.info("StackExchange 冷却中，跳过 q=%s", query)
        return []

    hits: list[ForumHit] = []
    # 限流友好：每查询只打 1 个站，站点轮换
    site = STACK_SITES[hash(query) % len(STACK_SITES)]
    # 注意：search/advanced + filter=withbody 在无 key 时常 400；用安全默认过滤器
    params = {
        "order": "desc",
        "sort": "relevance",
        "q": query,
        "site": site,
        "pagesize": min(max(1, max_items), 10),
        "page": 1,
        # default 过滤器稳定；正文用 excerpt / title 足够做需求挖掘
        "filter": "default",
    }
    url = "https://api.stackexchange.com/2.3/search/advanced"
    try:
        response = fetch(
            url,
            session=session,
            params=params,
            timeout=TIMEOUT,
            max_retries=1,  # 400/429 不值得连打
            backoff_factor=BACKOFF_FACTOR,
            delay_range=(5.0, 9.0),
            headers={"Accept": "application/json"},
            raise_for_status=False,
        )
    except RequestError as exc:
        if getattr(exc, "status_code", None) == 429 or "429" in str(exc):
            _trip_se_cooldown(1800.0)
            logger.warning("StackExchange 429，冷却 30 分钟，本查询跳过")
            return []
        logger.error("StackExchange 失败 site=%s q=%s: %s", site, query, exc)
        return []

    if response.status_code == 429:
        _trip_se_cooldown(1800.0)
        logger.warning("StackExchange 429，冷却 30 分钟，本查询跳过")
        return []
    if response.status_code == 400:
        logger.warning(
            "StackExchange 400（参数被拒）site=%s q=%s，跳过该查询",
            site,
            query,
        )
        return []
    if response.status_code != 200:
        logger.error(
            "StackExchange HTTP %s site=%s q=%s",
            response.status_code,
            site,
            query,
        )
        return []

    try:
        data = response.json()
    except Exception as exc:
        logger.error("StackExchange JSON 解析失败: %s", exc)
        return []

    if isinstance(data, dict):
        remaining = data.get("quota_remaining")
        if isinstance(remaining, int) and remaining <= 2:
            _trip_se_cooldown(1200.0)
        if data.get("error_id") or data.get("error_message"):
            logger.warning(
                "StackExchange API 错误: %s %s",
                data.get("error_id"),
                data.get("error_message"),
            )
            return []

    items = data.get("items") if isinstance(data, dict) else None
    if not items:
        logger.info("StackExchange 无结果 site=%s q=%s", site, query)
        return []

    for item in items:
        title = _html_to_text(item.get("title") or "")
        # default 过滤器通常有 excerpt，没有完整 body
        body = _html_to_text(
            item.get("body") or item.get("excerpt") or item.get("body_markdown") or ""
        )
        link = item.get("link") or ""
        if not link:
            continue
        hit = ForumHit(
            source=f"stackexchange:{site}",
            keyword=keyword,
            title=title,
            source_url=link,
            raw_content=(body or title)[:8000],
            tags=list(item.get("tags") or []),
        )
        hits.append(enrich_hit(hit))
        if len(hits) >= max_items:
            break

    return hits


# ---------------------------------------------------------------------------
# Reddit
# ---------------------------------------------------------------------------
def search_reddit(
    session,
    *,
    keyword: str,
    query: str,
    max_items: int,
) -> list[ForumHit]:
    hits: list[ForumHit] = []
    # 全站搜索 + 若干技术版块
    targets: list[tuple[str, str]] = [
        ("reddit:all", f"https://www.reddit.com/search.json"),
    ]
    for sub in REDDIT_SUBREDDITS:
        targets.append(
            (f"reddit:r/{sub}", f"https://www.reddit.com/r/{sub}/search.json")
        )

    for source_name, url in targets:
        if len(hits) >= max_items:
            break
        params = {
            "q": query,
            "sort": "relevance",
            "limit": min(25, max_items),
            "type": "link",
            "restrict_sr": "true" if "/r/" in url else "false",
        }
        try:
            response = fetch(
                url,
                session=session,
                params=params,
                timeout=REDDIT_TIMEOUT,
                max_retries=REDDIT_MAX_RETRIES,
                backoff_factor=BACKOFF_FACTOR,
                delay_range=(0.2, 0.6),
                headers={"Accept": "application/json"},
            )
            data = response.json()
        except RequestError as exc:
            # 主机连不上：后面几个 subreddit 也会挂，直接放弃本轮 Reddit
            logger.error(
                "Reddit 不可达，跳过剩余版块 %s q=%s: %s",
                source_name,
                query,
                exc,
            )
            raise

        children = (
            ((data or {}).get("data") or {}).get("children")
            if isinstance(data, dict)
            else None
        )
        if not children:
            continue

        for child in children:
            post = (child or {}).get("data") or {}
            title = post.get("title") or ""
            selftext = post.get("selftext") or ""
            permalink = post.get("permalink") or ""
            if not permalink:
                continue
            source_url = "https://www.reddit.com" + permalink
            hit = ForumHit(
                source=source_name,
                keyword=keyword,
                title=title,
                source_url=source_url,
                raw_content=_html_to_text(f"{title}\n{selftext}")[:8000],
            )
            hits.append(enrich_hit(hit))
            if len(hits) >= max_items:
                break

    return hits


# ---------------------------------------------------------------------------
# GitHub Issues（公开搜索 API）
# ---------------------------------------------------------------------------
def search_github_issues(
    session,
    *,
    keyword: str,
    query: str,
    max_items: int,
) -> list[ForumHit]:
    hits: list[ForumHit] = []
    # 限定与驱动/Windows 更相关的 issue
    q = f"{query} driver OR 驱动 in:title,body"
    url = "https://api.github.com/search/issues"
    params = {
        "q": q,
        "sort": "relevance",
        "order": "desc",
        "per_page": min(max_items, 20),
    }
    try:
        data = api_get(
            session,
            url,
            params,
        )
    except RequestError as exc:
        logger.error("GitHub Issues 失败 q=%s: %s", query, exc)
        return hits

    items = data.get("items") if isinstance(data, dict) else None
    if not items:
        return hits

    for item in items[:max_items]:
        title = item.get("title") or ""
        body = item.get("body") or ""
        html_url = item.get("html_url") or ""
        if not html_url:
            continue
        hit = ForumHit(
            source="github:issues",
            keyword=keyword,
            title=title,
            source_url=html_url,
            raw_content=_html_to_text(f"{title}\n{body}")[:8000],
        )
        hits.append(enrich_hit(hit))

    return hits


SOURCE_HANDLERS = {
    "stackexchange": search_stackexchange,
    "reddit": search_reddit,
    "github": search_github_issues,
}


def collect_hits(
    session,
    *,
    sources: Iterable[str],
    max_per_query: int,
    polite: bool = False,
    should_stop: Optional[Callable[[], bool]] = None,
) -> list[ForumHit]:
    all_hits: list[ForumHit] = []
    seen_urls: set[str] = set()
    disabled_sources: set[str] = set()
    source_list = [s.strip() for s in sources if str(s).strip()]

    # GitHub 优先：不依赖 SE 配额，也更常带仓库线索
    if "github" in source_list:
        source_list = ["github"] + [s for s in source_list if s != "github"]

    keywords = KEYWORDS
    if polite:
        # 商业产线：每词只取 1 条英文变体，大幅降低 SE 请求数
        keywords = KEYWORDS[:6]

    for keyword in keywords:
        if should_stop and should_stop():
            logger.info("collect_hits: 用户暂停，提前结束检索")
            break
        variants = QUERY_VARIANTS.get(keyword, [keyword])
        if polite:
            # 优先英文（海外极客/驱动包相关）
            eng = [v for v in variants if not re.search(r"[\u4e00-\u9fff]", v)]
            variants = (eng or variants)[:1]
        for query in variants:
            if should_stop and should_stop():
                break
            for source in source_list:
                if should_stop and should_stop():
                    break
                if source in disabled_sources:
                    continue
                if source == "stackexchange" and _se_in_cooldown():
                    disabled_sources.add("stackexchange")
                    logger.warning("StackExchange 仍在冷却，本轮不再请求")
                    continue
                handler = SOURCE_HANDLERS.get(source)
                if not handler:
                    logger.warning("未知数据源: %s", source)
                    continue
                logger.info("检索 source=%s keyword=%s query=%s", source, keyword, query)
                try:
                    batch = handler(
                        session,
                        keyword=keyword,
                        query=query,
                        max_items=max_per_query,
                    )
                except RequestError as exc:
                    logger.error(
                        "数据源 %s 本轮跳过后续检索: %s",
                        source,
                        exc,
                    )
                    disabled_sources.add(source)
                    continue
                except Exception:
                    logger.exception("检索异常 source=%s query=%s", source, query)
                    continue

                for hit in batch:
                    if hit.source_url in seen_urls:
                        continue
                    seen_urls.add(hit.source_url)
                    all_hits.append(hit)

    if disabled_sources:
        logger.warning("本轮已跳过数据源: %s", ", ".join(sorted(disabled_sources)))
    return all_hits


def save_hits(
    db: Database,
    hits: list[ForumHit],
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    stats = {"seen": len(hits), "saved": 0, "skipped": 0, "empty_extract": 0}

    def _safe(msg: str) -> None:
        try:
            print(msg, flush=True)
        except UnicodeEncodeError:
            print(msg.encode("utf-8", "replace").decode("utf-8", "replace"), flush=True)

    for hit in tqdm(hits, desc="论坛帖入库", unit="帖"):
        title = (hit.title or "")[:80]
        # 去掉易炸 GBK 控制台的 emoji / 特殊符号
        title = re.sub(r"[\U00010000-\U0010ffff]", "", title)
        _safe(f"[正在抓取] 标题: {title} | 来源: {hit.source}")
        if not hit.device_model and not hit.error_codes:
            stats["empty_extract"] += 1
        if dry_run:
            logger.info(
                "[dry-run] %s | %s | model=%s | errors=%s",
                hit.source,
                hit.title[:60],
                hit.device_model or "-",
                hit.error_codes or "-",
            )
            _safe(
                f"[dry-run] 标题: {title} | model={hit.device_model or '-'}"
            )
            stats["saved"] += 1
            continue

        item_id = db.insert_forum_post(
            source=hit.source,
            keyword=hit.keyword,
            title=hit.title,
            source_url=hit.source_url,
            device_model=hit.device_model or None,
            error_codes=hit.error_codes or None,
            raw_content=hit.raw_content,
            status=Status.PENDING,
        )
        if item_id is None:
            stats["skipped"] += 1
            _safe(f"[跳过-已存在] 标题: {title}")
        else:
            stats["saved"] += 1
            total = db.count_forum_posts()
            logger.info(
                "入库 id=%s model=%s errors=%s url=%s",
                item_id,
                hit.device_model or "-",
                hit.error_codes or "-",
                hit.source_url,
            )
            _safe(f"[成功入库] 当前总数: {total} 条")
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从公开问答/论坛定向检索驱动相关帖并提取型号与报错码",
    )
    parser.add_argument(
        "--sources",
        default="stackexchange,reddit,github",
        help="数据源，逗号分隔：stackexchange,reddit,github",
    )
    parser.add_argument(
        "--max-per-query",
        type=int,
        default=8,
        help="每个「数据源×检索词」最多抓取条数（默认 8）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印结果，不写数据库",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="缩短请求间隔（调试用，生产请勿开启）",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    global DELAY_RANGE
    if args.fast:
        DELAY_RANGE = (0.8, 1.5)

    sources = [s.strip().lower() for s in args.sources.split(",") if s.strip()]
    unknown = [s for s in sources if s not in SOURCE_HANDLERS]
    if unknown:
        logger.error("不支持的数据源: %s", ", ".join(unknown))
        return 2

    logger.info(
        "启动论坛线索抓取 | sources=%s | keywords=%s | max_per_query=%s",
        sources,
        KEYWORDS,
        args.max_per_query,
    )

    session = create_session(
        {
            "User-Agent": "DrvVaultForumBot/0.1 (research; public-api; contact=local)",
            "Accept": "application/json",
        }
    )

    try:
        hits = collect_hits(
            session,
            sources=sources,
            max_per_query=max(1, args.max_per_query),
        )
        with Database() as db:
            stats = save_hits(db, hits, dry_run=args.dry_run)
            total = db.count_forum_posts() if not args.dry_run else stats["saved"]
    finally:
        session.close()

    logger.info(
        "完成 | hits=%s saved=%s skipped=%s empty_extract=%s db_total=%s",
        stats["seen"],
        stats["saved"],
        stats["skipped"],
        stats["empty_extract"],
        total,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
