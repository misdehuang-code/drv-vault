/** USDT-TRC20 wallet checkout config. */

const DEFAULT_ADDRESS = "TEYwRybCAZTWhbXB8yQiwioRnmHGyGyREP";
const DEFAULT_AMOUNT = 4.9;

function envAmount(): number {
  const raw = (process.env.NEXT_PUBLIC_USDT_AMOUNT || "").trim();
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? n : DEFAULT_AMOUNT;
}

export const PAYMENT = {
  network: "TRC20" as const,
  asset: "USDT" as const,
  amount: envAmount(),
  walletAddress: (
    process.env.NEXT_PUBLIC_USDT_TRC20_ADDRESS || DEFAULT_ADDRESS
  ).trim(),
};

export function unlockPriceLabel(amount = PAYMENT.amount): string {
  return `${amount.toFixed(2)} USDT`;
}

export function transferNotice(amount = PAYMENT.amount): string {
  return `Send exactly ${unlockPriceLabel(amount)} on the TRON (TRC20) network. Keep a small TRX balance for network fees.`;
}
