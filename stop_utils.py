"""Cooperative pause helpers — keep Find & Verify responsive to admin 「暂停」."""

from __future__ import annotations

import time
from typing import Callable, Optional

StopFn = Optional[Callable[[], bool]]


class StopRequested(Exception):
    """Raised when the user asked the pipeline to pause."""


def stopped(stop: StopFn) -> bool:
    return bool(stop and stop())


def interruptible_sleep(
    seconds: float,
    stop: StopFn = None,
    *,
    slice_sec: float = 0.25,
    label: str = "",
) -> None:
    """Sleep in short slices so pause is honored within ~slice_sec."""
    end = time.time() + max(0.0, float(seconds))
    step = max(0.05, float(slice_sec))
    while time.time() < end:
        if stopped(stop):
            raise StopRequested(label or "paused during sleep")
        time.sleep(min(step, end - time.time()))
