"""验活主机计分板：成功加分、超时/404 降权，ACQUIRE 变通喂料用。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "data" / "host_scoreboard.json"

# 冷启动：已知当前网络下常超时的源先降权
DEFAULT_DEMOTE = {
    "archive.org": {"fail": 8, "ok": 0, "timeout": 8},
    "download.ni.com": {"fail": 6, "ok": 0, "timeout": 0},
}


def _load() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"hosts": dict(DEFAULT_DEMOTE), "updated_at": 0}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"hosts": dict(DEFAULT_DEMOTE), "updated_at": 0}
        data.setdefault("hosts", {})
        return data
    except Exception:
        return {"hosts": dict(DEFAULT_DEMOTE), "updated_at": 0}


def _save(data: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = time.time()
    STATE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def host_of(url: str) -> str:
    try:
        h = (urlparse(url).netloc or "").lower()
        if h.startswith("www."):
            h = h[4:]
        return h
    except Exception:
        return ""


def note_result(url: str, *, ok: bool, error: str = "") -> None:
    h = host_of(url)
    if not h:
        return
    data = _load()
    row = data["hosts"].setdefault(h, {"ok": 0, "fail": 0, "timeout": 0})
    if ok:
        row["ok"] = int(row.get("ok") or 0) + 1
    else:
        row["fail"] = int(row.get("fail") or 0) + 1
        err = (error or "").lower()
        if "timeout" in err or "timed out" in err:
            row["timeout"] = int(row.get("timeout") or 0) + 1
    _save(data)


def score(host: str) -> float:
    """越高越好；负分=应降权/跳过。"""
    h = (host or "").lower()
    if h.startswith("www."):
        h = h[4:]
    data = _load()
    row = (data.get("hosts") or {}).get(h) or {}
    ok = float(row.get("ok") or 0)
    fail = float(row.get("fail") or 0)
    timeout = float(row.get("timeout") or 0)
    return ok * 3.0 - fail * 1.0 - timeout * 2.5


def is_demoted(url_or_host: str, *, threshold: float = -6.0) -> bool:
    h = host_of(url_or_host) if "://" in (url_or_host or "") else (url_or_host or "")
    if not h:
        return False
    return score(h) <= threshold


def rank_urls(urls: list[str]) -> list[str]:
    """好站靠前，已降权站沉底。"""
    return sorted(
        urls,
        key=lambda u: (
            1 if is_demoted(u) else 0,
            -score(host_of(u)),
            u.lower(),
        ),
    )


def filter_feedable(urls: list[str]) -> list[str]:
    """喂验活队列前丢掉重度降权站（避免空转烧额度）。"""
    out = []
    for u in urls:
        if is_demoted(u, threshold=-8.0):
            continue
        out.append(u)
    return out
