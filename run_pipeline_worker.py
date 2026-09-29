#!/usr/bin/env python3
"""Subprocess entry for commercial pipeline — admin can kill this process on Stop.

Usage:
  python run_pipeline_worker.py --sources github,reddit --continuous 1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    # Windows 子进程默认 GBK，会把中文/emoji 打成乱码并抛 UnicodeEncodeError
    import os

    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        from github_auth import get_github_token, load_dotenv_file

        load_dotenv_file()
        get_github_token()
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    p = argparse.ArgumentParser(description="Commercial Find&Verify pipeline worker")
    p.add_argument("--sources", default="github,reddit")
    p.add_argument("--max-per-query", type=int, default=5)
    p.add_argument("--clean-forum-limit", type=int, default=80)
    p.add_argument("--harvest-workers", type=int, default=4)
    p.add_argument("--round-sleep", type=float, default=12.0)
    p.add_argument("--continuous", type=int, default=1)
    p.add_argument("--use-ai", type=int, default=1)
    p.add_argument("--offline", type=int, default=0)
    args = p.parse_args(argv)

    from commercial_pipeline import PipelineStopped, run_commercial_daemon

    def append(line: str) -> None:
        print(line, flush=True)

    def on_phase(code: str, title: str, step: int, total: int) -> None:
        # Machine-readable line for admin status parser
        print(f"__PHASE__ {code} {step}/{total} {title}", flush=True)
        append(f"[{step}/{total}] {code} · {title}")

    def on_round_start(round_no: int) -> None:
        print(f"__ROUND__ {round_no}", flush=True)
        append(f"######## 商业轮次 #{round_no} 开始 ########")

    try:
        result = run_commercial_daemon(
            continuous=bool(args.continuous),
            round_sleep_sec=float(args.round_sleep),
            max_per_query=max(1, int(args.max_per_query)),
            sources=str(args.sources),
            clean_forum_limit=max(1, int(args.clean_forum_limit)),
            use_ai=bool(args.use_ai) and not bool(args.offline),
            harvest_workers=max(1, int(args.harvest_workers)),
            harvest_rate_mbps=0.0,
            should_stop=None,  # hard-killed by parent on Stop
            append_log=append,
            on_phase=on_phase,
            on_round_start=on_round_start,
        )
    except PipelineStopped as exc:
        print(f"[停止] {exc}", flush=True)
        print("__RESULT__ " + json.dumps({"ok": True, "paused": True}, ensure_ascii=False), flush=True)
        return 0
    except KeyboardInterrupt:
        print("[停止] KeyboardInterrupt", flush=True)
        print("__RESULT__ " + json.dumps({"ok": True, "paused": True}, ensure_ascii=False), flush=True)
        return 0

    print("__RESULT__ " + json.dumps(result, ensure_ascii=False, default=str), flush=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
