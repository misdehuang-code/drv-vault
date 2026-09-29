"""USDT-TRC20 smart reconcile: detect recent unclaimed ~4.90 deposits via TronGrid."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
CLAIMED_PATH = ROOT / "data" / "claimed_usdt_payments.json"
CATALOG_PATH = ROOT / "data" / "commercial_catalog.json"

# Official USDT (TRC20) contract on Tron mainnet
USDT_TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
DEFAULT_ADDRESS = "TEYwRybCAZTWhbXB8yQiwioRnmHGyGyREP"
DEFAULT_AMOUNT = 4.9
DEFAULT_TOLERANCE = 0.05
DEFAULT_WINDOW_SEC = 180  # 3 minutes


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        n = float(raw)
        return n if n > 0 else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        n = int(raw)
        return n if n > 0 else default
    except ValueError:
        return default


def wallet_address() -> str:
    return (
        os.environ.get("USDT_TRC20_ADDRESS")
        or os.environ.get("NEXT_PUBLIC_USDT_TRC20_ADDRESS")
        or DEFAULT_ADDRESS
    ).strip()


def expected_amount() -> float:
    return _env_float("USDT_AMOUNT", _env_float("NEXT_PUBLIC_USDT_AMOUNT", DEFAULT_AMOUNT))


def load_claimed() -> dict[str, Any]:
    if not CLAIMED_PATH.exists():
        return {"claims": {}}
    try:
        return json.loads(CLAIMED_PATH.read_text(encoding="utf-8").lstrip("\ufeff"))
    except Exception:
        return {"claims": {}}


def save_claimed(store: dict[str, Any]) -> None:
    CLAIMED_PATH.parent.mkdir(parents=True, exist_ok=True)
    store["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    CLAIMED_PATH.write_text(
        json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def ascii_slug_parts(category: str, model: str, file_name: str, fallback: str = "") -> str:
    def ascii_slug(s: str) -> str:
        s = s.lower()
        out = []
        for ch in s.replace("\\", "-").replace("/", "-"):
            if ("a" <= ch <= "z") or ("0" <= ch <= "9") or ch == "-":
                out.append(ch)
            else:
                out.append("-")
        cleaned = "".join(out)
        while "--" in cleaned:
            cleaned = cleaned.replace("--", "-")
        return cleaned.strip("-")

    stem = ascii_slug(Path(file_name or "pack").stem)[:28]
    base = ascii_slug((fallback or "").strip()) or ascii_slug(f"{category}-{model}") or "sku"
    if stem and stem[:8] not in base:
        base = f"{base}-{stem}"
    return base[:96]


def lookup_verified_url(slug: str) -> Optional[str]:
    if not CATALOG_PATH.exists():
        return None
    try:
        data = json.loads(CATALOG_PATH.read_text(encoding="utf-8").lstrip("\ufeff"))
    except Exception:
        return None

    slug_l = slug.strip().lower()
    for item in data.get("items") or []:
        computed = ascii_slug_parts(
            str(item.get("hardware_category") or ""),
            str(item.get("exact_model") or ""),
            str(item.get("file_name") or ""),
            str(item.get("slug") or ""),
        )
        raw_slug = str(item.get("slug") or "").strip().lower()
        if computed == slug_l or raw_slug == slug_l:
            url = (item.get("verified_url") or item.get("source_url") or "").strip()
            if url:
                return url
    return None


def _tron_headers() -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": "DrvVault-Verify/1.0"}
    key = (os.environ.get("TRONGRID_API_KEY") or "").strip()
    if key:
        headers["TRON-PRO-API-KEY"] = key
    return headers


def fetch_recent_usdt_in(address: str, limit: int = 30) -> list[dict[str, Any]]:
    base = (os.environ.get("TRONGRID_API") or "https://api.trongrid.io").rstrip("/")
    qs = urllib.parse.urlencode(
        {
            "only_to": "true",
            "limit": str(limit),
            "contract_address": USDT_TRC20,
        }
    )
    url = f"{base}/v1/accounts/{urllib.parse.quote(address)}/transactions/trc20?{qs}"
    req = urllib.request.Request(url, headers=_tron_headers(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")[:300]
        raise RuntimeError(f"trongrid_http_{exc.code}: {body}") from exc
    except Exception as exc:
        raise RuntimeError(f"trongrid_error: {exc}") from exc

    return list(payload.get("data") or [])


def _tx_amount_usdt(row: dict[str, Any]) -> float:
    raw = row.get("value") or "0"
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    decimals = 6
    info = row.get("token_info") or {}
    try:
        decimals = int(info.get("decimals") if info.get("decimals") is not None else 6)
    except (TypeError, ValueError):
        decimals = 6
    return value / (10 ** decimals)


def find_unclaimed_payment(
    *,
    slug: str,
    address: Optional[str] = None,
    amount: Optional[float] = None,
    window_sec: Optional[int] = None,
    tolerance: Optional[float] = None,
) -> dict[str, Any]:
    """Scan recent USDT-TRC20 inbound txs; claim the newest matching unclaimed one."""
    addr = (address or wallet_address()).strip()
    expect = float(amount if amount is not None else expected_amount())
    window = int(window_sec if window_sec is not None else _env_int("USDT_VERIFY_WINDOW_SEC", DEFAULT_WINDOW_SEC))
    tol = float(tolerance if tolerance is not None else _env_float("USDT_AMOUNT_TOLERANCE", DEFAULT_TOLERANCE))
    slug = (slug or "").strip()
    if not slug:
        return {"success": False, "error": "missing_slug"}

    now_ms = int(time.time() * 1000)
    min_ts = now_ms - window * 1000
    lo, hi = expect - tol, expect + tol

    try:
        rows = fetch_recent_usdt_in(addr)
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}

    store = load_claimed()
    claims: dict[str, Any] = dict(store.get("claims") or {})

    candidates: list[tuple[int, dict[str, Any], float]] = []
    for row in rows:
        txid = str(row.get("transaction_id") or "").strip()
        if not txid:
            continue
        if txid in claims:
            continue
        to_addr = str(row.get("to") or "").strip()
        if to_addr and to_addr != addr:
            continue
        ts = int(row.get("block_timestamp") or 0)
        if ts < min_ts:
            continue
        amt = _tx_amount_usdt(row)
        if amt < lo or amt > hi:
            continue
        candidates.append((ts, row, amt))

    if not candidates:
        return {
            "success": False,
            "error": "no_matching_payment",
            "hint": f"No unclaimed ~{expect} USDT-TRC20 inbound in the last {window}s. Wait for confirmation, then retry.",
            "window_sec": window,
            "expected_amount": expect,
            "address": addr,
        }

    # Newest first
    candidates.sort(key=lambda x: x[0], reverse=True)
    ts, row, amt = candidates[0]
    txid = str(row["transaction_id"])
    verified_url = lookup_verified_url(slug) or ""

    claims[txid] = {
        "slug": slug,
        "amount": amt,
        "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "block_timestamp": ts,
        "from": row.get("from"),
    }
    store["claims"] = claims
    save_claimed(store)

    return {
        "success": True,
        "txid": txid,
        "amount": amt,
        "slug": slug,
        "verified_url": verified_url,
        "block_timestamp": ts,
        "address": addr,
    }


def verify_payment_request(body: dict[str, Any]) -> dict[str, Any]:
    return find_unclaimed_payment(
        slug=str(body.get("slug") or ""),
        address=(str(body["address"]).strip() if body.get("address") else None),
        amount=float(body["amount"]) if body.get("amount") is not None else None,
    )
