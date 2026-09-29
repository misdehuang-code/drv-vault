#!/usr/bin/env python3
"""Confirm a USDT-TRC20 TxID and publish it to the storefront unlock ledger.

MVP flow
--------
1. Buyer pays USDT-TRC20 and submits TxID + contact on the product page.
2. Claim lands in data/payment_claims.json (when Next /api/claims is available)
   and/or in the buyer's browser localStorage (order ID shown on screen).
3. You verify the TxID on Tronscan, then run:

   python scripts/confirm_usdt_payment.py --txid <64hex> --slug <sku-slug>
   # or pick a pending claim:
   python scripts/confirm_usdt_payment.py --from-claim <order_id>

4. Redeploy / sync web/public/verified_payments.json (Cloudflare Pages).
   Buyer taps "Check unlock status" or re-enters order ID / contact → unlock.

Contact tokens are SHA-256("drvvault:v1:" + lowercased contact without leading @)
so the public ledger never stores raw email/Telegram.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLAIMS = ROOT / "data" / "payment_claims.json"
LEDGER = ROOT / "web" / "public" / "verified_payments.json"
TX_RE = re.compile(r"^[a-fA-F0-9]{64}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def contact_token(contact: str) -> str:
    normalized = contact.strip().lower().lstrip("@")
    raw = f"drvvault:v1:{normalized}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return json.loads(json.dumps(fallback))
    return json.loads(path.read_text(encoding="utf-8").lstrip("\ufeff"))


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def list_pending() -> None:
    store = load_json(CLAIMS, {"claims": []})
    claims = store.get("claims") or []
    pending = [c for c in claims if (c.get("status") or "pending") == "pending"]
    if not pending:
        print("No pending claims in", CLAIMS)
        return
    print(f"{len(pending)} pending claim(s):")
    for c in pending:
        print(
            f"  {c.get('order_id')}\t{c.get('slug')}\t{c.get('txid')}\t{c.get('contact')}\t{c.get('amount')}"
        )


def confirm(
    *,
    txid: str,
    slug: str,
    order_id: str,
    token: str,
    amount: float,
) -> None:
    if not TX_RE.match(txid):
        raise SystemExit("txid must be 64 hex characters")
    if not slug.strip():
        raise SystemExit("slug is required")
    if not order_id.strip():
        raise SystemExit("order_id is required")
    if len(token) != 64:
        raise SystemExit("contact_token must be sha256 hex")

    ledger = load_json(LEDGER, {"updated_at": None, "orders": []})
    orders = list(ledger.get("orders") or [])
    for row in orders:
        if row.get("txid") == txid or row.get("order_id") == order_id:
            print("Already confirmed:", row.get("order_id"))
            return

    row = {
        "order_id": order_id,
        "slug": slug,
        "txid": txid,
        "contact_token": token,
        "amount": amount,
        "confirmed_at": utc_now(),
    }
    orders.append(row)
    ledger["orders"] = orders
    ledger["updated_at"] = row["confirmed_at"]
    save_json(LEDGER, ledger)

    # Mark claim confirmed if present
    store = load_json(CLAIMS, {"claims": []})
    changed = False
    for c in store.get("claims") or []:
        if c.get("order_id") == order_id or c.get("txid") == txid:
            c["status"] = "confirmed"
            c["confirmed_at"] = row["confirmed_at"]
            changed = True
    if changed:
        store["updated_at"] = row["confirmed_at"]
        save_json(CLAIMS, store)

    print("Confirmed →", LEDGER)
    print(json.dumps(row, ensure_ascii=False, indent=2))
    print("Buyer can now Check unlock status on the SKU page.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Confirm USDT-TRC20 unlock payments")
    p.add_argument("--list", action="store_true", help="List pending claims")
    p.add_argument("--from-claim", metavar="ORDER_ID", help="Confirm a pending claim by order id")
    p.add_argument("--txid", help="64-hex TRON TxID")
    p.add_argument("--slug", help="SKU slug on the storefront")
    p.add_argument("--order-id", help="Order id (DRV-…)")
    p.add_argument("--contact", help="Buyer email or Telegram (hashed into ledger)")
    p.add_argument("--contact-token", help="Precomputed sha256 token")
    p.add_argument("--amount", type=float, default=4.9)
    args = p.parse_args(argv)

    if args.list:
        list_pending()
        return 0

    if args.from_claim:
        store = load_json(CLAIMS, {"claims": []})
        hit = next(
            (c for c in (store.get("claims") or []) if c.get("order_id") == args.from_claim),
            None,
        )
        if not hit:
            raise SystemExit(f"claim not found: {args.from_claim}")
        confirm(
            txid=str(hit["txid"]),
            slug=str(hit["slug"]),
            order_id=str(hit["order_id"]),
            token=str(hit.get("contact_token") or contact_token(str(hit.get("contact") or ""))),
            amount=float(hit.get("amount") or args.amount),
        )
        return 0

    if not args.txid or not args.slug:
        p.print_help()
        return 2

    token = args.contact_token or (contact_token(args.contact) if args.contact else "")
    if not token:
        raise SystemExit("provide --contact or --contact-token")

    order_id = args.order_id or f"DRV-MANUAL-{args.txid[:8].upper()}"
    confirm(
        txid=args.txid.strip(),
        slug=args.slug.strip(),
        order_id=order_id.strip(),
        token=token.strip(),
        amount=float(args.amount),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
