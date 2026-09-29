"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ArrowLeft, MonitorSmartphone } from "lucide-react";

import { UnlockDownload } from "@/components/UnlockDownload";
import {
  getDriverBySlugClient,
  type DriverItem,
} from "@/lib/catalogClient";
import { VALUE_APPLICABILITY } from "@/lib/copy";

function resolveSlug(param: string | string[] | undefined): string {
  const raw = Array.isArray(param) ? param[0] : param || "";
  // CF Pages rewrite lands on /drivers/_ but browser path stays real slug
  if (typeof window !== "undefined") {
    const m = window.location.pathname.match(/\/drivers\/([^/?#]+)/);
    if (m?.[1] && m[1] !== "_" && m[1] !== "__spa__") {
      try {
        return decodeURIComponent(m[1]);
      } catch {
        return m[1];
      }
    }
  }
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

export function DriverDetailClient() {
  const params = useParams<{ slug: string }>();
  const [item, setItem] = useState<DriverItem | null | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    const slug = resolveSlug(params?.slug);
    if (!slug || slug === "_" || slug === "__spa__") {
      setItem(null);
      return;
    }
    (async () => {
      try {
        const found = await getDriverBySlugClient(slug);
        if (!cancelled) setItem(found || null);
      } catch {
        if (!cancelled) setItem(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [params?.slug]);

  if (item === undefined) {
    return (
      <main className="mx-auto max-w-3xl px-4 py-16 text-sm text-mist-400">
        Loading package…
      </main>
    );
  }

  if (!item) {
    return (
      <main className="mx-auto max-w-3xl px-4 py-16">
        <p className="text-mist-300">Package not found.</p>
        <Link href="/" className="mt-4 inline-flex text-signal hover:underline">
          ← Back to catalog
        </Link>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-3xl px-4 py-10 animate-rise">
      <Link
        href="/"
        className="mb-6 inline-flex items-center gap-2 text-sm text-mist-400 hover:text-signal"
      >
        <ArrowLeft className="h-4 w-4" aria-hidden />
        Back to catalog
      </Link>

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <span className="inline-flex items-center gap-1 rounded-md border border-mist-400/20 bg-ink-700/80 px-2 py-0.5 text-[0.68rem] tracking-wide text-mist-300">
          <MonitorSmartphone className="h-3 w-3" aria-hidden />
          {item.category}
        </span>
        {item.verified ? (
          <span className="badge-verified">Verified</span>
        ) : null}
      </div>

      <h1 className="font-display text-3xl font-extrabold tracking-tight text-mist-100">
        {item.displayTitle}
      </h1>
      <p className="mt-2 text-sm text-mist-400">Legacy Driver Archive</p>
      <p className="mt-4 text-base leading-relaxed text-mist-200">
        {item.cardPain}
      </p>
      <p className="mt-3 text-sm text-mist-400">{VALUE_APPLICABILITY}</p>

      <div className="mt-8">
        <UnlockDownload
          slug={item.slug}
          fileName={item.fileName}
          fileSize={item.fileSize}
          downloadUrl={item.downloadUrl}
          priceLabel={item.priceLabel}
          price={item.price}
          verified={item.verified}
          seoTitle={item.seoTitle}
        />
      </div>
    </main>
  );
}
