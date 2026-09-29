/** Client-side order helpers for USDT TxID unlock flow. */

export type LocalClaim = {
  orderId: string;
  slug: string;
  txId: string;
  contact: string;
  contactToken: string;
  amount: number;
  createdAt: string;
  status: "verifying" | "unlocked";
};

const CLAIM_PREFIX = "drvvault:claim:";
const UNLOCK_PREFIX = "drvvault:unlocked:";

export function claimStorageKey(slug: string) {
  return `${CLAIM_PREFIX}${slug}`;
}

export function unlockStorageKey(slug: string) {
  return `${UNLOCK_PREFIX}${slug}`;
}

export function makeOrderId(slug: string): string {
  const stem = slug.replace(/[^a-z0-9]+/gi, "").slice(0, 8).toUpperCase() || "DRV";
  const rand = Math.random().toString(36).slice(2, 8).toUpperCase();
  const t = Date.now().toString(36).toUpperCase().slice(-4);
  return `DRV-${stem}-${t}${rand}`;
}

export function normalizeContact(contact: string): string {
  return contact.trim().toLowerCase().replace(/^@/, "");
}

export function normalizeTxId(txid: string): string {
  return txid.trim().replace(/\s+/g, "");
}

export async function hashContact(contact: string): Promise<string> {
  const normalized = normalizeContact(contact);
  const data = new TextEncoder().encode(`drvvault:v1:${normalized}`);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export function isLikelyTronTxId(txid: string): boolean {
  const t = normalizeTxId(txid);
  // Tron txids are 64 hex chars
  return /^[a-fA-F0-9]{64}$/.test(t);
}

export function loadLocalClaim(slug: string): LocalClaim | null {
  try {
    const raw = window.localStorage.getItem(claimStorageKey(slug));
    if (!raw) return null;
    return JSON.parse(raw) as LocalClaim;
  } catch {
    return null;
  }
}

export function saveLocalClaim(claim: LocalClaim) {
  window.localStorage.setItem(claimStorageKey(claim.slug), JSON.stringify(claim));
}

export function markLocalUnlocked(slug: string) {
  window.localStorage.setItem(unlockStorageKey(slug), "1");
}

export function isLocalUnlocked(slug: string): boolean {
  try {
    return window.localStorage.getItem(unlockStorageKey(slug)) === "1";
  } catch {
    return false;
  }
}

export type VerifiedOrder = {
  order_id: string;
  slug: string;
  txid: string;
  contact_token: string;
  amount?: number;
  confirmed_at?: string;
};

export type VerifiedLedger = {
  updated_at?: string | null;
  orders?: VerifiedOrder[];
};

export async function fetchVerifiedLedger(
  url: string,
): Promise<VerifiedLedger> {
  const u = new URL(url, window.location.origin);
  u.searchParams.set("_", String(Date.now()));
  const res = await fetch(u.toString(), { cache: "no-store" });
  if (!res.ok) return { orders: [] };
  return (await res.json()) as VerifiedLedger;
}

export function matchUnlock(opts: {
  ledger: VerifiedLedger;
  slug: string;
  orderId?: string;
  contactToken?: string;
  txId?: string;
}): VerifiedOrder | null {
  const orders = opts.ledger.orders || [];
  for (const row of orders) {
    if (row.slug !== opts.slug) continue;
    if (opts.orderId && row.order_id === opts.orderId) return row;
    if (opts.txId && normalizeTxId(row.txid) === normalizeTxId(opts.txId)) {
      return row;
    }
    if (
      opts.contactToken &&
      row.contact_token &&
      row.contact_token === opts.contactToken
    ) {
      return row;
    }
  }
  return null;
}
