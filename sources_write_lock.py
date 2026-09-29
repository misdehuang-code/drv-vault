"""进程内串行化对 sources.txt 的追加写，供 ACQUIRE 并行矿工/挖链共用。"""

from __future__ import annotations

import threading

SOURCES_LOCK = threading.Lock()
