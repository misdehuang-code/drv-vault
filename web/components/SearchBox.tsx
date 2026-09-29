"use client";

import { Search } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState, useTransition } from "react";

type SearchBoxProps = {
  defaultQuery?: string;
  /** When true, filter live via URL without full page reload feel */
  live?: boolean;
};

export function SearchBox({ defaultQuery = "", live = true }: SearchBoxProps) {
  const router = useRouter();
  const [value, setValue] = useState(defaultQuery);
  const [pending, startTransition] = useTransition();

  useEffect(() => {
    setValue(defaultQuery);
  }, [defaultQuery]);

  useEffect(() => {
    if (!live) return;
    const t = window.setTimeout(() => {
      const next = value.trim();
      const cur = defaultQuery.trim();
      if (next === cur) return;
      startTransition(() => {
        router.replace(next ? `/?q=${encodeURIComponent(next)}` : "/");
      });
    }, 280);
    return () => window.clearTimeout(t);
  }, [value, live, defaultQuery, router]);

  return (
    <form
      className="relative w-full max-w-2xl"
      action="/"
      method="get"
      role="search"
      onSubmit={(e) => {
        if (live) {
          e.preventDefault();
          const next = value.trim();
          startTransition(() => {
            router.push(next ? `/?q=${encodeURIComponent(next)}` : "/");
          });
        }
      }}
    >
      <label className="sr-only" htmlFor="q">
        Search by hardware model, brand, or error code
      </label>
      <Search
        className="pointer-events-none absolute left-4 top-1/2 h-5 w-5 -translate-y-1/2 text-mist-400"
        aria-hidden
      />
      <input
        id="q"
        name="q"
        type="search"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="Model, brand, or Code 10 / 28 / 43…"
        autoComplete="off"
        className="w-full rounded-xl border border-mist-400/20 bg-ink-800/90 py-4 pl-12 pr-28 text-base text-mist-100 shadow-card outline-none transition placeholder:text-mist-400 focus:border-signal/50 focus:shadow-glow"
      />
      <button
        type="submit"
        className="absolute right-2 top-1/2 -translate-y-1/2 rounded-lg bg-signal px-4 py-2 text-sm font-semibold text-ink-950 transition hover:bg-signal-dim"
        disabled={pending}
      >
        {pending ? "…" : "Search"}
      </button>
    </form>
  );
}
