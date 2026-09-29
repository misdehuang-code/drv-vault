/** Browser-side USDT-TRC20 payment detect via public TronGrid API. */

import { PAYMENT } from "@/lib/payment";

export const USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t";
export const VERIFY_WINDOW_MS = 3 * 60 * 1000;
export const AMOUNT_TOLERANCE = 0.05;

export type TronTrc20Tx = {
  transaction_id?: string;
  to?: string;
  from?: string;
  value?: string;
  block_timestamp?: number;
  token_info?: { decimals?: number; symbol?: string };
};

export type ClientVerifyHit = {
  txid: string;
  amount: number;
  blockTimestamp: number;
};

const CLAIMED_TX_KEY = "drvvault:claimed-txids";

function loadClaimed(): Record<string, string> {
  try {
    const raw = window.localStorage.getItem(CLAIMED_TX_KEY);
    if (!raw) return {};
    return JSON.parse(raw) as Record<string, string>;
  } catch {
    return {};
  }
}

function saveClaimed(map: Record<string, string>) {
  window.localStorage.setItem(CLAIMED_TX_KEY, JSON.stringify(map));
}

export function markTxClaimed(txid: string, slug: string) {
  const map = loadClaimed();
  map[txid] = slug;
  saveClaimed(map);
}

function txAmountUsdt(row: TronTrc20Tx): number {
  const value = Number(row.value || 0);
  const decimals = Number(row.token_info?.decimals ?? 6);
  if (!Number.isFinite(value) || !Number.isFinite(decimals)) return 0;
  return value / 10 ** decimals;
}

export function trongridTrc20Url(address = PAYMENT.walletAddress): string {
  const base = (
    process.env.NEXT_PUBLIC_TRONGRID_API || "https://api.trongrid.io"
  ).replace(/\/$/, "");
  const u = new URL(
    `${base}/v1/accounts/${encodeURIComponent(address)}/transactions/trc20`,
  );
  u.searchParams.set("limit", "5");
  u.searchParams.set("contract_address", USDT_TRC20_CONTRACT);
  u.searchParams.set("only_to", "true");
  return u.toString();
}

/** Fetch recent inbound USDT and return newest matching unclaimed ~4.90 payment. */
export async function detectRecentUsdtPayment(opts?: {
  amount?: number;
  address?: string;
  slug?: string;
}): Promise<ClientVerifyHit | null> {
  const address = (opts?.address || PAYMENT.walletAddress).trim();
  const expect = opts?.amount ?? PAYMENT.amount;
  const lo = expect - AMOUNT_TOLERANCE;
  const hi = expect + AMOUNT_TOLERANCE;
  const minTs = Date.now() - VERIFY_WINDOW_MS;
  const claimed = loadClaimed();

  const res = await fetch(trongridTrc20Url(address), {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  if (!res.ok) {
    throw new Error(`TronGrid ${res.status}`);
  }
  const payload = (await res.json()) as { data?: TronTrc20Tx[] };
  const rows = payload.data || [];

  const hits: ClientVerifyHit[] = [];
  for (const row of rows) {
    const txid = String(row.transaction_id || "").trim();
    if (!txid) continue;
    if (claimed[txid] && claimed[txid] !== opts?.slug) continue;
    if (claimed[txid] === opts?.slug) {
      // already used for this slug — still treat as hit for re-open
      const amount = txAmountUsdt(row);
      hits.push({
        txid,
        amount,
        blockTimestamp: Number(row.block_timestamp || 0),
      });
      continue;
    }
    const to = String(row.to || "").trim();
    if (to && to !== address) continue;
    const ts = Number(row.block_timestamp || 0);
    if (ts < minTs) continue;
    const amount = txAmountUsdt(row);
    if (amount < lo || amount > hi) continue;
    hits.push({ txid, amount, blockTimestamp: ts });
  }

  if (hits.length === 0) return null;
  hits.sort((a, b) => b.blockTimestamp - a.blockTimestamp);
  return hits[0];
}
