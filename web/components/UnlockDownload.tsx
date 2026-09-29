"use client";

import { BadgeCheck, Download, ShieldCheck } from "lucide-react";
import { useState } from "react";

import { CheckoutModal } from "@/components/CheckoutModal";
import { CTA_UNLOCK, VALUE_APPLICABILITY } from "@/lib/copy";

type UnlockDownloadProps = {
  slug: string;
  fileName: string;
  fileSize: string;
  downloadUrl: string;
  priceLabel?: string;
  price?: number;
  verified: boolean;
  seoTitle: string;
};

export function UnlockDownload({
  slug,
  fileName,
  fileSize,
  downloadUrl,
  verified,
  seoTitle,
}: UnlockDownloadProps) {
  const [open, setOpen] = useState(false);

  return (
    <>
      <aside className="panel sticky top-6 space-y-4">
        <div className="flex items-start justify-between gap-3">
          <h2 className="font-display text-lg font-bold text-mist-100">
            Instant download
          </h2>
          {verified ? (
            <span className="badge-verified shrink-0">
              <BadgeCheck className="h-3.5 w-3.5" />
              Verified
            </span>
          ) : null}
        </div>

        <dl className="space-y-2 text-xs text-mist-300">
          <div className="flex justify-between gap-2">
            <dt>Package</dt>
            <dd className="truncate text-mist-100" title={fileName}>
              {fileName}
            </dd>
          </div>
          <div className="flex justify-between gap-2">
            <dt>Size</dt>
            <dd className="text-mist-100">{fileSize}</dd>
          </div>
          <div className="flex justify-between gap-2">
            <dt>Unlock</dt>
            <dd className="badge-price">4.90 USDT</dd>
          </div>
          <div className="flex justify-between gap-2">
            <dt>Network</dt>
            <dd className="text-signal">USDT · TRC20</dd>
          </div>
        </dl>

        <p className="rounded-lg border border-mist-400/15 bg-ink-900/60 px-3 py-2 text-sm text-mist-300">
          {VALUE_APPLICABILITY}
        </p>

        <button
          type="button"
          className="btn-primary w-full !py-3 text-sm"
          onClick={() => setOpen(true)}
        >
          <Download className="h-4 w-4" aria-hidden />
          {CTA_UNLOCK}
        </button>

        <ul className="space-y-1.5 text-xs text-mist-400">
          <li className="flex items-start gap-2">
            <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-signal" />
            Click download → pay USDT-TRC20 → unlock link
          </li>
          <li className="flex items-start gap-2">
            <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-signal" />
            No account required · auto-unlock after confirmation
          </li>
        </ul>
      </aside>

      <CheckoutModal
        open={open}
        onClose={() => setOpen(false)}
        item={{
          slug,
          title: seoTitle,
          downloadUrl,
        }}
      />
    </>
  );
}
