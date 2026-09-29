import { NextResponse } from "next/server";
import fs from "fs";
import path from "path";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type ClaimBody = {
  order_id?: string;
  slug?: string;
  txid?: string;
  contact?: string;
  contact_token?: string;
  amount?: number;
};

function claimsPath(): string {
  const candidates = [
    path.join(process.cwd(), "..", "data", "payment_claims.json"),
    path.join(process.cwd(), "data", "payment_claims.json"),
  ];
  for (const p of candidates) {
    if (fs.existsSync(path.dirname(p))) return p;
  }
  return candidates[0];
}

function readClaims(file: string): { updated_at: string | null; claims: ClaimBody[] } {
  try {
    if (!fs.existsSync(file)) {
      return { updated_at: null, claims: [] };
    }
    const raw = fs.readFileSync(file, "utf-8").replace(/^\uFEFF/, "");
    return JSON.parse(raw) as { updated_at: string | null; claims: ClaimBody[] };
  } catch {
    return { updated_at: null, claims: [] };
  }
}

/** Accept MVP payment claims (TxID + contact) for local/admin review. */
export async function POST(req: Request) {
  let body: ClaimBody;
  try {
    body = (await req.json()) as ClaimBody;
  } catch {
    return NextResponse.json({ ok: false, error: "invalid_json" }, { status: 400 });
  }

  const orderId = String(body.order_id || "").trim();
  const slug = String(body.slug || "").trim();
  const txid = String(body.txid || "").trim();
  const contact = String(body.contact || "").trim();
  const contactToken = String(body.contact_token || "").trim();
  const amount = Number(body.amount || 0);

  if (!orderId || !slug || !txid || !contact || !contactToken) {
    return NextResponse.json({ ok: false, error: "missing_fields" }, { status: 400 });
  }
  if (!/^[a-fA-F0-9]{64}$/.test(txid)) {
    return NextResponse.json({ ok: false, error: "invalid_txid" }, { status: 400 });
  }

  const file = claimsPath();
  const store = readClaims(file);
  const existing = (store.claims || []).find(
    (c) => c.txid === txid || c.order_id === orderId,
  );
  if (existing) {
    return NextResponse.json({
      ok: true,
      duplicate: true,
      order_id: existing.order_id,
    });
  }

  const row = {
    order_id: orderId,
    slug,
    txid,
    contact,
    contact_token: contactToken,
    amount,
    status: "pending",
    created_at: new Date().toISOString(),
  };
  store.claims = [...(store.claims || []), row];
  store.updated_at = row.created_at;

  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, `${JSON.stringify(store, null, 2)}\n`, "utf-8");

  return NextResponse.json({ ok: true, order_id: orderId });
}

export async function GET() {
  const file = claimsPath();
  const store = readClaims(file);
  return NextResponse.json({
    ok: true,
    count: (store.claims || []).length,
    updated_at: store.updated_at,
  });
}
