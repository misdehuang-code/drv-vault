"""统一 JSON 读写：兼容 Windows PowerShell 写入的 UTF-8 BOM。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return default
    return json.loads(text)


def write_json(path: Path, data: Any, *, indent: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=indent) + "\n",
        encoding="utf-8",  # 无 BOM，跨平台安全
    )
