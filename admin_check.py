"""
命令行人工抽检：随机展示清洗后待审核数据，支持一键通过或标记删除。

用法：
    python admin_check.py
    python admin_check.py --count 5
    python admin_check.py --all-approve   # 对本轮抽中的记录全部通过（仍先展示）
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from database import Database, Status

# 操作提示
HELP_LINE = "操作: [A]通过  [D]删除  [S]跳过  [O]打开说明  [Q]退出本轮"


def _truncate(text: str, width: int = 100) -> str:
    text = " ".join((text or "").split())
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def extract_download_links(raw_content: str, page_url: str, *, limit: int = 3) -> list[str]:
    """从原始 HTML 里尽量找出下载链接；找不到则返回空列表。"""
    if not raw_content or raw_content.lstrip().startswith("[fetch_error]"):
        return []

    soup = BeautifulSoup(raw_content, "html.parser")
    candidates: list[str] = []
    seen: set[str] = set()

    keyword_re = re.compile(
        r"download|\.zip$|\.exe$|\.rar$|\.7z$|\.msi$|\.dmg$|附件|下载",
        re.I,
    )

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        full = urljoin(page_url, href)
        text = anchor.get_text(" ", strip=True)
        blob = f"{full} {text}"
        if not keyword_re.search(blob):
            continue
        if full in seen:
            continue
        seen.add(full)
        candidates.append(full)
        if len(candidates) >= limit:
            break

    return candidates


def print_item(index: int, total: int, row: dict) -> None:
    page_url = row.get("source_url") or ""
    downloads = extract_download_links(row.get("raw_content") or "", page_url)

    print()
    print("=" * 60)
    print(f"[{index}/{total}]  ID={row.get('id')}  status={row.get('status')}")
    print("-" * 60)
    print(f"SEO 标题 : {_truncate(row.get('seo_title') or '(空)', 80)}")
    print(f"业务标题 : {_truncate(row.get('title') or '(空)', 80)}")
    print(f"设备型号 : {_truncate(row.get('device_model') or '(空)', 80)}")
    print(f"适用系统 : {row.get('os_version') or '(空)'}")
    print(f"来源页面 : {page_url or '(空)'}")
    if downloads:
        print("下载链接 :")
        for i, link in enumerate(downloads, 1):
            print(f"  {i}. {link}")
    else:
        print("下载链接 : (正文未识别到，请核对接来源页面)")
    print("-" * 60)
    print(HELP_LINE)


def prompt_action() -> str:
    """读取用户操作，返回 a/d/s/q。"""
    while True:
        try:
            raw = input("请选择 > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "q"

        if raw in {"a", "d", "s", "q"}:
            return raw
        if raw in {"o", "h", "?"}:
            print(HELP_LINE)
            print("  A = approve 通过审核")
            print("  D = delete  标记删除")
            print("  S = skip    跳过，保持 cleaned")
            print("  Q = quit    结束本轮抽检")
            continue
        print("无效输入，请输入 A / D / S / Q")


def review_batch(
    db: Database,
    rows: list[dict],
    *,
    auto_approve: bool = False,
) -> dict[str, int]:
    stats = {"approved": 0, "deleted": 0, "skipped": 0, "quit": 0}
    total = len(rows)

    for idx, row in enumerate(rows, start=1):
        print_item(idx, total, row)
        item_id = int(row["id"])

        if auto_approve:
            db.approve_item(item_id)
            stats["approved"] += 1
            print(f"→ 已自动通过 ID={item_id}")
            continue

        action = prompt_action()
        if action == "a":
            db.approve_item(item_id)
            stats["approved"] += 1
            print(f"→ 已通过 ID={item_id}")
        elif action == "d":
            db.soft_delete_item(item_id)
            stats["deleted"] += 1
            print(f"→ 已标记删除 ID={item_id}")
        elif action == "s":
            stats["skipped"] += 1
            print(f"→ 已跳过 ID={item_id}")
        else:
            stats["quit"] += 1
            print("→ 结束本轮抽检")
            break

    return stats


def run(
    *,
    count: int = 5,
    auto_approve: bool = False,
) -> int:
    with Database() as db:
        pending = db.count_by_status(Status.CLEANED)
        print(f"待审核（cleaned）共 {pending} 条，本轮随机抽取 {count} 条")

        rows = db.list_random_by_status(Status.CLEANED, limit=count)
        if not rows:
            print("没有可审核的清洗数据。请先运行 cleaner.py。")
            return 0

        stats = review_batch(db, rows, auto_approve=auto_approve)
        print()
        print(
            "本轮结果: "
            f"通过={stats['approved']}  "
            f"删除={stats['deleted']}  "
            f"跳过={stats['skipped']}"
            + (f"  提前退出={stats['quit']}" if stats["quit"] else "")
        )
        remain = db.count_by_status(Status.CLEANED)
        print(f"剩余待审核: {remain} 条")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="随机抽检清洗后数据，人工确认标题与下载链接",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=5,
        help="本轮随机展示条数（默认 5）",
    )
    parser.add_argument(
        "--all-approve",
        action="store_true",
        help="对本轮抽中的记录全部一键通过（仍会先打印内容）",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.count <= 0:
        print("--count 必须为正整数", file=sys.stderr)
        return 2
    return run(count=args.count, auto_approve=args.all_approve)


if __name__ == "__main__":
    sys.exit(main())
