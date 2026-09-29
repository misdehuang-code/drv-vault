"""
[已废弃本地下载] bulk_harvest 兼容层 — Link Curation 纯血版。

重型下载/解压/落盘代码已永久移除（见 legacy/ARCHIVE_NOTE.py）。
白名单同步请直接使用 source_queue，或本模块 --sync-only。

正确入口：
  python commercial_pipeline.py / python admin_server.py
  python link_curator.py
  python source_queue.py
  python cleanup_legacy_vault.py --apply
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Optional

from source_queue import (  # noqa: F401
    DEFAULT_FAILED,
    DEFAULT_LONGTAIL_CACHE,
    DEFAULT_SEARCH_RULES,
    DEFAULT_SOURCES,
    DEFAULT_WHITELIST,
    dedupe_sources_file,
    enqueue_scraped_for_whitelist,
    ensure_sample_sources,
    is_direct_file_url,
    is_page_clue_url,
    load_failed_urls,
    sanitize_name,
    sync_whitelist_to_sources,
)

_is_direct_file_url = is_direct_file_url
_is_page_clue_url = is_page_clue_url

ROOT = Path(__file__).resolve().parent
DEFAULT_INDEX = ROOT / "vault_index.json"
# 仅用于拒绝旧 CLI 参数；绝不 mkdir 成下载盘
DEFAULT_VAULT = ROOT / "data" / "vault"
DEFAULT_STATE = ROOT / "data" / ".harvest_state.json.disabled"

logger = logging.getLogger(__name__)

_REMOVED_MSG = (
    "本地下载/解压已永久移除（Link Curation 纯血版）。"
    "请使用 python link_curator.py 验链，或后台商业流水线。"
)


def run_harvest(*_a: Any, **_k: Any) -> int:
    logger.error(_REMOVED_MSG)
    print(f"[已移除] {_REMOVED_MSG}", flush=True)
    return 2


def build_index(*_a: Any, **_k: Any) -> dict[str, Any]:
    raise RuntimeError(_REMOVED_MSG)


def download_with_resume(*_a: Any, **_k: Any) -> Path:
    raise RuntimeError(_REMOVED_MSG)


def process_one(*_a: Any, **_k: Any) -> dict[str, Any]:
    raise RuntimeError(_REMOVED_MSG)


def extract_archive(*_a: Any, **_k: Any) -> bool:
    raise RuntimeError(_REMOVED_MSG)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    argv = list(argv) if argv is not None else sys.argv[1:]
    p = argparse.ArgumentParser(
        description="[兼容壳] 仅 --sync-only；验链请用 link_curator"
    )
    p.add_argument("--sync-only", action="store_true")
    p.add_argument("--from-whitelist", action="store_true")
    p.add_argument("--min-score", type=float, default=0.0)
    p.add_argument("--whitelist", type=Path, default=DEFAULT_WHITELIST)
    p.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    p.add_argument("--failed", type=Path, default=DEFAULT_FAILED)
    p.add_argument("-v", "--verbose", action="store_true")
    # 吞掉全部旧下载参数
    for flag in (
        "--workers",
        "--rate-mbps",
        "--vault",
        "--index",
        "--state",
        "--ai-limit",
        "--longtail-limit",
        "--ollama-url",
        "--ollama-model",
    ):
        p.add_argument(flag, default=None)
    for flag in (
        "--no-extract",
        "--index-only",
        "--ai-enrich",
        "--no-ai",
        "--longtail",
        "--longtail-only",
    ):
        p.add_argument(flag, action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.sync_only or args.from_whitelist:
        from source_queue import main as sq_main

        return sq_main(
            [
                "--min-score",
                str(args.min_score),
                "--whitelist",
                str(args.whitelist),
                "--sources",
                str(args.sources),
                "--failed",
                str(args.failed),
            ]
        )

    print(f"[已移除] {_REMOVED_MSG}", flush=True)
    print("白名单同步: python source_queue.py  或  python bulk_harvest.py --sync-only", flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
