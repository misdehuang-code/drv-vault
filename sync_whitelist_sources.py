"""
把 data/急需设备型号白名单.json 同步进 sources.txt 验链队列。

用法：
  python sync_whitelist_sources.py
  python sync_whitelist_sources.py --min-score 10
"""

from __future__ import annotations

import sys

from source_queue import main


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
