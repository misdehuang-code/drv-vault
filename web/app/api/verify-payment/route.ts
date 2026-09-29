import fs from "fs";
import path from "path";
import { NextResponse } from "next/server";

import { getDriverBySlug } from "@/data/drivers";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const USDT_TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t";
const DEFAULT_ADDRESS = "TEYwRybCAZTWhbXB8yQiwioRnmHGyGyREP";
const DEFAULT_AMOUNT = 4.9;
const DEFAULT_TOLERANCE = 0.05;
const DEFAULT_WINDOW_SEC = 180;

type ClaimStore = {
  updated_at?: string;
  claims: Record<
    string,
    { slug: string; amount: number; claimed_at: string; block_timestamp?: number }
  >;
};

function walletAddress() {
  return (
    process.env.USDT_TRC20_ADDRESS ||
    process.env.NEXT_PUBLIC_USDT_TRC20_ADDRESS ||
    DEFAULT_ADDRESS
  ).trim();
}

function expectedAmount() {
  const n = Number(process.env.USDT_AMOUNT || process.env.NEXT_PUBLIC_USDT_AMOUNT);
  return Number.isFinite(n) && n > 0 ? n : DEFAULT_AMOUNT;
}

function windowSec() {
  const n = Number(process.env.USDT_VERIFY_WINDOW_SEC);
  return Number.isFinite(n) && n > 0 ? n : DEFAULT_WINDOW_SEC;
}

function tolerance() {
  const n = Number(process.env.USDT_AMOUNT_TOLERANCE);
  return Number.isFinite(n) && n > 0 ? n : DEFAULT_TOLERANCE;
}

function claimedPaths(): string[] {
  return [
    path.join(process.cwd(), "..", "data", "claimed_usdt_payments.json"),
    path.join(process.cwd(), "data", "claimed_usdt_payments.json"),
  ];
}

function loadClaims(): { file: string; store: ClaimStore } {
  for (const file of claimedPaths()) {
    try {
      if (fs.existsSync(file)) {
        const raw = fs.readFileSync(file, "utf-8").replace(/^\uFEFF/, "");
        const store = JSON.parse(raw) as ClaimStore;
        return {
          file,
          store: { ...store, claims: store.claims || {} },
        };
      }
    } catch {
      /* next */
    }
  }
  return {
    file: claimedPaths()[0],
    store: { claims: {} },
  };
}

function saveClaims(file: string, store: ClaimStore) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  store.updated_at = new Date().toISOString();
  fs.writeFileSync(file, `${JSON.stringify(store, null, 2)}\n`, "utf-8");
}

async function fetchRecentUsdtIn(address: string) {
  const base = (process.env.TRONGRID_API || "https://api.trongrid.io").replace(
    /\/$/,
    "",
  );
  const url = new URL(`${base}/v1/accounts/${encodeURIComponent(address)}/transactions/trc20`);
  url.searchParams.set("only_to", "true");
  url.searchParams.set("limit", "30");
  url.searchParams.set("contract_address", USDT_TRC20);

  const headers: Record<string, string> = {
    Accept: "application/json",
    "User-Agent": "DrvVault-Verify/1.0",
  };
  const key = (process.env.TRONGRID_API_KEY || "").trim();
  if (key) headers["TRON-PRO-API-KEY"] = key;

  const res = await fetch(url.toString(), { headers, cache: "no-store" });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`trongrid_http_${res.status}: ${body.slice(0, 200)}`);
  }
  const payload = (await res.json()) as { data?: Array<Record<string, unknown>> };
  return payload.data || [];
}

function txAmountUsdt(row: Record<string, unknown>): number {
  const value = Number(row.value || 0);
  const info = (row.token_info || {}) as { decimals?: number };
  const decimals = Number(info.decimals ?? 6);
  return value / 10 ** decimals;
}

export async function POST(req: Request) {
  let body: { slug?: string; amount?: number } = {};
  try {
    body = (await req.json()) as { slug?: string; amount?: number };
  } catch {
    /* empty */
  }

  const slug = String(body.slug || "").trim();
  if (!slug) {
    return NextResponse.json(
      { success: false, error: "missing_slug" },
      { status: 400 },
    );
  }

  const addr = walletAddress();
  const expect = Number(body.amount) > 0 ? Number(body.amount) : expectedAmount();
  const window = windowSec();
  const tol = tolerance();
  const lo = expect - tol;
  const hi = expect + tol;
  const minTs = Date.now() - window * 1000;

  let rows: Array<Record<string, unknown>>;
  try {
    rows = await fetchRecentUsdtIn(addr);
  } catch (err) {
    return NextResponse.json(
      { success: false, error: String(err instanceof Error ? err.message : err) },
      { status: 502 },
    );
  }

  const { file, store } = loadClaims();
  const claims = store.claims || {};

  const candidates: Array<{
    ts: number;
    row: Record<string, unknown>;
    amount: number;
  }> = [];

  for (const row of rows) {
    const txid = String(row.transaction_id || "").trim();
    if (!txid || claims[txid]) continue;
    const to = String(row.to || "").trim();
    if (to && to !== addr) continue;
    const ts = Number(row.block_timestamp || 0);
    if (ts < minTs) continue;
    const amount = txAmountUsdt(row);
    if (amount < lo || amount > hi) continue;
    candidates.push({ ts, row, amount });
  }

  if (candidates.length === 0) {
    return NextResponse.json(
      {
        success: false,
        error: "no_matching_payment",
        hint: `No unclaimed ~${expect} USDT-TRC20 inbound in the last ${window}s. Wait for confirmation, then retry.`,
        window_sec: window,
        expected_amount: expect,
        address: addr,
      },
      { status: 402 },
    );
  }

  candidates.sort((a, b) => b.ts - a.ts);
  const hit = candidates[0];
  const txid = String(hit.row.transaction_id);
  const driver = getDriverBySlug(slug);
  const verifiedUrl = driver?.downloadUrl || "";

  claims[txid] = {
    slug,
    amount: hit.amount,
    claimed_at: new Date().toISOString(),
    block_timestamp: hit.ts,
  };
  store.claims = claims;
  saveClaims(file, store);

  return NextResponse.json({
    success: true,
    txid,
    amount: hit.amount,
    slug,
    verified_url: verifiedUrl,
    block_timestamp: hit.ts,
    address: addr,
  });
}
