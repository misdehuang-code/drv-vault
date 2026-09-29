"""智能死胡同沉底：反复 0 产出的型号/线索跳过至少 20 轮。"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
DEFAULT_BLACKLIST = ROOT / "data" / "dead_end_blacklist.json"
SKIP_ROUNDS = 20
# 连续空产出多少次后沉底
FAIL_BEFORE_SINK = 2


def _norm_key(model: str) -> str:
    s = (model or "").strip().lower()
    s = s.replace("\\", "_")
    s = re.sub(r"_+", "_", s)
    s = re.sub(r"\s+", " ", s)
    return s


def load_blacklist(path: Path = DEFAULT_BLACKLIST) -> dict[str, Any]:
    if not path.exists():
        return {"items": {}, "updated_at": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            return {"items": {}, "updated_at": None}
        data.setdefault("items", {})
        return data
    except Exception as exc:
        logger.debug("load blacklist: %s", exc)
        return {"items": {}, "updated_at": None}


def save_blacklist(data: dict[str, Any], path: Path = DEFAULT_BLACKLIST) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone

    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def is_sunk(model: str, *, round_no: int, path: Path = DEFAULT_BLACKLIST) -> bool:
    key = _norm_key(model)
    if not key:
        return False
    items = load_blacklist(path).get("items") or {}
    row = items.get(key) or {}
    try:
        until = int(row.get("skip_until_round") or 0)
    except (TypeError, ValueError):
        until = 0
    return until > int(round_no)


def sunk_keys(*, round_no: int, path: Path = DEFAULT_BLACKLIST) -> set[str]:
    items = load_blacklist(path).get("items") or {}
    out: set[str] = set()
    for key, row in items.items():
        if not isinstance(row, dict):
            continue
        try:
            until = int(row.get("skip_until_round") or 0)
        except (TypeError, ValueError):
            until = 0
        if until > int(round_no):
            out.add(key)
    return out


def filter_active_models(
    items: list[dict[str, Any]],
    *,
    round_no: int,
    path: Path = DEFAULT_BLACKLIST,
) -> tuple[list[dict[str, Any]], int]:
    """返回 (未沉底列表, 本轮规避数)。"""
    sunk = sunk_keys(round_no=round_no, path=path)
    active: list[dict[str, Any]] = []
    skipped = 0
    for row in items:
        model = str(row.get("device_model") or "").strip()
        if _norm_key(model) in sunk:
            skipped += 1
            continue
        active.append(row)
    return active, skipped


def note_model_outcome(
    model: str,
    *,
    round_no: int,
    yielded: bool,
    path: Path = DEFAULT_BLACKLIST,
) -> dict[str, Any]:
    """记录本轮是否产出；连续失败则沉底 SKIP_ROUNDS 轮。"""
    key = _norm_key(model)
    if not key:
        return {}
    data = load_blacklist(path)
    items: dict[str, Any] = dict(data.get("items") or {})
    row = dict(items.get(key) or {})
    row["model"] = model
    row["last_round"] = int(round_no)
    if yielded:
        row["fail_streak"] = 0
        # 成功则解除沉底
        row["skip_until_round"] = 0
        row["status"] = "active"
    else:
        streak = int(row.get("fail_streak") or 0) + 1
        row["fail_streak"] = streak
        if streak >= FAIL_BEFORE_SINK:
            row["skip_until_round"] = int(round_no) + SKIP_ROUNDS
            row["status"] = "sunk"
            row["sunk_at_round"] = int(round_no)
        else:
            row["status"] = "warming"
    items[key] = row
    data["items"] = items
    save_blacklist(data, path)
    return row


def count_sunk_skipped(*, round_no: int, path: Path = DEFAULT_BLACKLIST) -> int:
    return len(sunk_keys(round_no=round_no, path=path))
