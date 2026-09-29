"""盲猜规则矩阵已停用。

商业流水线请使用 structured_index_harvest（真实索引源）。
历史实现见 pattern_discovery_legacy.py。
"""

from __future__ import annotations

import warnings
from typing import Any


def run_pattern_discovery(*_a: Any, **_kw: Any) -> dict[str, Any]:
    warnings.warn(
        "pattern_discovery 盲猜模式已停用，请使用 structured_index_harvest",
        DeprecationWarning,
        stacklevel=2,
    )
    return {
        "added": 0,
        "generated": 0,
        "models": 0,
        "skipped_dead": 0,
        "skipped": True,
        "reason": "disabled_blind_guess",
    }


__all__ = ["run_pattern_discovery"]
