"use client";

import type { ReactNode } from "react";
import Link from "next/link";
import { BadgeCheck, Cpu, Download } from "lucide-react";
import { useState } from "react";

import type { DriverItem } from "@/lib/catalogClient";
import { CheckoutModal, type CheckoutItem } from "@/components/CheckoutModal";
import { CTA_UNLOCK } from "@/lib/copy";

function highlightPain(text: string): ReactNode {
  const parts = text.split(/(Code\s*\d{1,3})/gi);
  return parts.map((part, i) =>
    /^Code\s*\d{1,3}$/i.test(part) ? (
      <mark
        key={`${part}-${i}`}
        className="rounded bg-warn/20 px-1 font-mono text-warn"
      >
        {part}
      </mark>
    ) : (
      <span key={`${part}-${i}`}>{part}</span>
    ),
  );
}

export function DriverList({ items }: { items: DriverItem[] }) {
  const [checkout, setCheckout] = useState<CheckoutItem | null>(null);

  if (items.length === 0) {
    return (
      <div className="panel text-mist-300">
        No matching packages. Try a hardware model, brand, or{" "}
        <span className="font-mono text-warn">Code 43</span>.
      </div>
    );
  }

  return (
    <>
      <section
        id="catalog"
        className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 animate-rise-delay"
        aria-label="Enterprise recovery catalog"
      >
        {items.map((item) => (
          <article
            key={item.slug}
            className="group flex flex-col rounded-xl border border-mist-400/15 bg-ink-800/60 p-4 shadow-card backdrop-blur-sm transition duration-200 hover:-translate-y-0.5 hover:border-signal/35 hover:shadow-glow"
          >
            <Link href={`/drivers/${item.slug}`} className="block flex-1">
              <div className="mb-3 flex flex-wrap items-center gap-2">
                <span className="inline-flex items-center gap-1 rounded-md border border-mist-400/20 bg-ink-700/80 px-2 py-0.5 text-[0.68rem] tracking-wide text-mist-300">
                  <Cpu className="h-3 w-3" aria-hidden />
                  {item.category}
                </span>
                {item.verified ? (
                  <span className="badge-verified">
                    <BadgeCheck className="h-3.5 w-3.5" aria-hidden />
                    Verified
                  </span>
                ) : null}
              </div>

              <h2 className="font-display text-lg font-bold leading-snug tracking-tight text-mist-100 group-hover:text-signal">
                {item.displayTitle}
              </h2>
              <p className="mt-1 text-xs text-mist-400">Legacy Driver Archive</p>

              <p className="mt-3 line-clamp-3 text-sm leading-relaxed text-mist-200">
                {highlightPain(item.cardPain)}
              </p>
            </Link>

            <div className="mt-auto border-t border-mist-400/10 pt-3 mt-4">
              <button
                type="button"
                className="btn-primary w-full !px-3 !py-2.5 text-xs sm:text-[0.8rem]"
                onClick={() =>
                  setCheckout({
                    slug: item.slug,
                    title: item.displayTitle,
                    downloadUrl: item.downloadUrl,
                  })
                }
              >
                <Download className="h-3.5 w-3.5" aria-hidden />
                {CTA_UNLOCK}
              </button>
            </div>
          </article>
        ))}
      </section>

      <CheckoutModal
        open={Boolean(checkout)}
        item={checkout}
        onClose={() => setCheckout(null)}
      />
    </>
  );
}
