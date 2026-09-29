"use client";

import { createPortal } from "react-dom";
import {
  AlertTriangle,
  Check,
  Copy,
  ExternalLink,
  Loader2,
  ShieldCheck,
  Wallet,
  X,
} from "lucide-react";
import { QRCodeSVG } from "qrcode.react";
import { useCallback, useEffect, useState } from "react";

import { PAYMENT, unlockPriceLabel } from "@/lib/payment";
import {
  detectRecentUsdtPayment,
  markTxClaimed,
} from "@/lib/tronVerify";

export type CheckoutItem = {
  slug: string;
  title: string;
  downloadUrl: string;
};

type CheckoutModalProps = {
  item: CheckoutItem | null;
  open: boolean;
  onClose: () => void;
};

const WAIT_HINT =
  "Payment not detected yet. Wait ~10 seconds for TRON confirmation, then try again.";

function unlockKey(slug: string) {
  return `drvvault:auto-unlock:${slug}`;
}

function shortAddr(addr: string) {
  if (addr.length < 16) return addr;
  return `${addr.slice(0, 6)}…${addr.slice(-6)}`;
}

export function CheckoutModal({ item, open, onClose }: CheckoutModalProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [unlockedUrl, setUnlockedUrl] = useState("");
  const [txid, setTxid] = useState("");
  const [copiedAddr, setCopiedAddr] = useState(false);
  const [copiedAmount, setCopiedAmount] = useState(false);
  const [copiedLink, setCopiedLink] = useState(false);
  const [mounted, setMounted] = useState(false);

  const amountExact = PAYMENT.amount.toFixed(2);
  const amountLabel = unlockPriceLabel(PAYMENT.amount);

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (!open || !item) return;
    setError("");
    setBusy(false);
    setCopiedAddr(false);
    setCopiedAmount(false);
    setCopiedLink(false);
    try {
      const raw = window.localStorage.getItem(unlockKey(item.slug));
      if (raw) {
        const parsed = JSON.parse(raw) as { url?: string; txid?: string };
        if (parsed.url) {
          setUnlockedUrl(parsed.url);
          setTxid(parsed.txid || "");
          return;
        }
      }
    } catch {
      /* ignore */
    }
    setUnlockedUrl("");
    setTxid("");
  }, [open, item]);

  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = prev;
      window.removeEventListener("keydown", onKey);
    };
  }, [open, onClose]);

  const copyText = useCallback(
    async (value: string, kind: "addr" | "amount" | "link") => {
      try {
        await navigator.clipboard.writeText(value);
        if (kind === "addr") {
          setCopiedAddr(true);
          setTimeout(() => setCopiedAddr(false), 1800);
        } else if (kind === "amount") {
          setCopiedAmount(true);
          setTimeout(() => setCopiedAmount(false), 1800);
        } else {
          setCopiedLink(true);
          setTimeout(() => setCopiedLink(false), 1800);
        }
      } catch {
        setError("Copy failed — select the text manually.");
      }
    },
    [],
  );

  async function onPaid() {
    if (!item) return;
    setBusy(true);
    setError("");
    try {
      const hit = await detectRecentUsdtPayment({
        slug: item.slug,
        amount: PAYMENT.amount,
        address: PAYMENT.walletAddress,
      });
      if (!hit) {
        setError(WAIT_HINT);
        return;
      }
      const url = (item.downloadUrl || "").trim();
      if (!url) {
        setError("Payment matched, but this item has no download URL.");
        return;
      }
      markTxClaimed(hit.txid, item.slug);
      setUnlockedUrl(url);
      setTxid(hit.txid);
      try {
        window.localStorage.setItem(
          unlockKey(item.slug),
          JSON.stringify({ url, txid: hit.txid, at: Date.now() }),
        );
      } catch {
        /* ignore */
      }
    } catch {
      setError(WAIT_HINT);
    } finally {
      setBusy(false);
    }
  }

  if (!open || !item || !mounted) return null;

  const unlocked = Boolean(unlockedUrl);

  return createPortal(
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Secure checkout"
    >
      <button
        type="button"
        className="absolute inset-0 bg-ink-950/85 backdrop-blur-sm"
        aria-label="Close checkout"
        onClick={onClose}
      />

      <div className="relative z-10 flex max-h-[min(90vh,720px)] w-full max-w-lg flex-col overflow-hidden rounded-2xl border border-mist-400/20 bg-ink-800 shadow-glow">
        <div className="flex items-start justify-between gap-3 border-b border-mist-400/10 px-5 py-4">
          <div className="min-w-0">
            <p className="font-mono text-[0.65rem] uppercase tracking-[0.18em] text-mist-400">
              Secure checkout
            </p>
            <h2 className="mt-1 truncate font-display text-lg font-bold text-mist-100">
              {item.title}
            </h2>
            <p className="mt-0.5 text-xs text-mist-400">
              Pay once · Instant digital delivery
            </p>
          </div>
          <button
            type="button"
            className="shrink-0 rounded-lg border border-mist-400/20 p-1.5 text-mist-400 hover:text-mist-100"
            onClick={onClose}
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="overflow-y-auto px-5 py-4">
          {!unlocked ? (
            <div className="space-y-5">
              <div className="flex items-end justify-between gap-3">
                <div>
                  <p className="text-xs text-mist-400">Total due</p>
                  <p className="mt-0.5 font-display text-3xl font-bold tracking-tight text-mist-100">
                    {amountExact}
                    <span className="ml-1.5 text-base font-semibold text-mist-300">
                      USDT
                    </span>
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => copyText(amountExact, "amount")}
                  className="btn-ghost !px-3 !py-2 text-xs"
                >
                  {copiedAmount ? (
                    <Check className="h-3.5 w-3.5 text-signal" />
                  ) : (
                    <Copy className="h-3.5 w-3.5" />
                  )}
                  {copiedAmount ? "Copied" : "Copy amount"}
                </button>
              </div>

              <div className="flex gap-2.5 rounded-lg border border-warn/35 bg-warn/10 px-3 py-2.5">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warn" />
                <div className="text-xs leading-relaxed text-mist-200">
                  <p className="font-semibold text-warn">
                    Network: TRON · USDT-TRC20 only
                  </p>
                  <p className="mt-0.5 text-mist-300">
                    Send the exact amount. Wrong network or wrong amount may
                    result in permanent loss of funds.
                  </p>
                </div>
              </div>

              <ol className="space-y-2 text-sm text-mist-300">
                <li className="flex gap-3">
                  <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-ink-950 font-mono text-[0.7rem] text-signal">
                    1
                  </span>
                  <span>
                    Open your wallet (Trust, Binance, OKX, TronLink, etc.)
                  </span>
                </li>
                <li className="flex gap-3">
                  <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-ink-950 font-mono text-[0.7rem] text-signal">
                    2
                  </span>
                  <span>
                    Send{" "}
                    <strong className="text-mist-100">{amountLabel}</strong> on{" "}
                    <strong className="text-mist-100">TRC20</strong> to the
                    address below. Keep a little TRX for gas.
                  </span>
                </li>
                <li className="flex gap-3">
                  <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-ink-950 font-mono text-[0.7rem] text-signal">
                    3
                  </span>
                  <span>
                    Tap{" "}
                    <strong className="text-mist-100">
                      I&apos;ve sent the payment
                    </strong>{" "}
                    after the transfer broadcasts.
                  </span>
                </li>
              </ol>

              <div className="rounded-xl border border-mist-400/15 bg-ink-900/80 p-4">
                <div className="mb-3 flex items-center justify-between gap-2">
                  <p className="flex items-center gap-1.5 text-xs font-medium text-mist-300">
                    <Wallet className="h-3.5 w-3.5 text-signal" />
                    Deposit address
                  </p>
                  <span className="rounded-md border border-signal/25 bg-signal/10 px-2 py-0.5 font-mono text-[0.65rem] uppercase tracking-wider text-signal">
                    TRC20
                  </span>
                </div>

                <div className="mx-auto mb-3 w-fit rounded-lg bg-white p-3">
                  <QRCodeSVG
                    value={PAYMENT.walletAddress}
                    size={148}
                    level="M"
                    bgColor="#ffffff"
                    fgColor="#05070b"
                    includeMargin={false}
                  />
                </div>
                <p className="mb-2 text-center font-mono text-[0.65rem] text-mist-400">
                  Scan with your wallet · {shortAddr(PAYMENT.walletAddress)}
                </p>

                <div className="rounded-lg border border-mist-400/15 bg-ink-950 px-3 py-2.5">
                  <p className="break-all font-mono text-[0.72rem] leading-relaxed text-mist-100">
                    {PAYMENT.walletAddress}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => copyText(PAYMENT.walletAddress, "addr")}
                  className="btn-primary mt-3 w-full !py-2.5"
                >
                  {copiedAddr ? (
                    <Check className="h-4 w-4" />
                  ) : (
                    <Copy className="h-4 w-4" />
                  )}
                  {copiedAddr ? "Address copied" : "Copy deposit address"}
                </button>
              </div>

              {error ? (
                <p className="rounded-lg border border-warn/30 bg-warn/10 px-3 py-2.5 text-sm text-warn">
                  {error}
                </p>
              ) : null}

              <button
                type="button"
                className="btn-primary w-full !py-3.5 text-base"
                onClick={onPaid}
                disabled={busy}
              >
                {busy ? (
                  <Loader2 className="h-5 w-5 animate-spin" />
                ) : (
                  <ShieldCheck className="h-5 w-5" />
                )}
                {busy ? "Checking blockchain…" : "I've sent the payment"}
              </button>

              <p className="flex items-start gap-2 text-center text-[0.7rem] leading-relaxed text-mist-400 sm:text-left">
                <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-signal" />
                We auto-detect a matching {amountLabel} deposit from the last
                few minutes. No account or email required.
              </p>
            </div>
          ) : (
            <div className="space-y-4">
              <div className="rounded-xl border border-signal/35 bg-signal/10 px-4 py-4">
                <p className="flex items-center gap-2 font-display text-lg font-bold text-signal">
                  <Check className="h-5 w-5" />
                  Payment confirmed
                </p>
                <p className="mt-1 text-sm text-mist-200">
                  Your verified download is ready. Save the link — it won&apos;t
                  expire on this device.
                </p>
                {txid ? (
                  <p className="mt-2 break-all font-mono text-[0.65rem] text-mist-400">
                    Transaction · {txid}
                  </p>
                ) : null}
              </div>

              <div className="rounded-xl border border-mist-400/15 bg-ink-950 px-3 py-3">
                <p className="mb-1 font-mono text-[0.65rem] uppercase tracking-wider text-mist-400">
                  Download URL
                </p>
                <p className="break-all font-mono text-xs text-signal">
                  {unlockedUrl}
                </p>
              </div>

              <div className="flex flex-col gap-2 sm:flex-row">
                <button
                  type="button"
                  className="btn-ghost flex-1"
                  onClick={() => copyText(unlockedUrl, "link")}
                >
                  {copiedLink ? (
                    <Check className="h-4 w-4 text-signal" />
                  ) : (
                    <Copy className="h-4 w-4" />
                  )}
                  {copiedLink ? "Link copied" : "Copy link"}
                </button>
                <a
                  className="btn-primary flex-1"
                  href={unlockedUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <ExternalLink className="h-4 w-4" />
                  Open download
                </a>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
