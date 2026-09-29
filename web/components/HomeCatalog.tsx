"use client";

import { useEffect, useMemo, useState } from "react";
import { ChevronDown, FolderOpen, Search, ShieldCheck, Zap } from "lucide-react";

import { DriverList } from "@/components/DriverList";
import type { DriverItem } from "@/lib/catalogClient";
import {
  loadCatalogIndex,
  loadChunk,
  searchCatalog,
  slimToDriver,
  type CatalogAisle,
  type SlimCatalogItem,
} from "@/lib/catalogClient";

const PAGE_SIZE = 36;
const SEARCH_RESULT_CAP = 36;

export function HomeCatalog() {
  const [totalCount, setTotalCount] = useState(0);
  const [aisles, setAisles] = useState<CatalogAisle[]>([]);
  const [bootError, setBootError] = useState<string | null>(null);
  const [booting, setBooting] = useState(true);

  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [openGroup, setOpenGroup] = useState<string | null>(null);
  const [searchHits, setSearchHits] = useState<DriverItem[]>([]);
  const [searchingNet, setSearchingNet] = useState(false);
  /** Full slim rows per aisle (loaded once on expand). */
  const [aisleRaw, setAisleRaw] = useState<Record<string, SlimCatalogItem[]>>(
    {},
  );
  const [aisleLoading, setAisleLoading] = useState<string | null>(null);
  /** How many cards currently shown per aisle id. */
  const [aisleShown, setAisleShown] = useState<Record<string, number>>({});

  const q = query.trim().toLowerCase();
  const searching = q.length >= 2;

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const idx = await loadCatalogIndex();
        if (cancelled) return;
        setTotalCount(idx.sku_count || 0);
        setAisles(idx.aisles || []);
        setBootError(null);
      } catch (err) {
        if (!cancelled) {
          setBootError(err instanceof Error ? err.message : "catalog load failed");
        }
      } finally {
        if (!cancelled) setBooting(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!searching) {
      setSearchHits([]);
      setSearchingNet(false);
      return;
    }
    let cancelled = false;
    const timer = setTimeout(async () => {
      setSearchingNet(true);
      try {
        const hits = await searchCatalog(query.trim(), {
          category: category || undefined,
          limit: SEARCH_RESULT_CAP,
        });
        if (!cancelled) setSearchHits(hits);
      } catch {
        if (!cancelled) setSearchHits([]);
      } finally {
        if (!cancelled) setSearchingNet(false);
      }
    }, 160);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [query, category, searching]);

  // Load the entire aisle chunk when expanded (then paginate in the UI).
  useEffect(() => {
    if (!openGroup || searching) return;
    const aisle = aisles.find((a) => a.cat === openGroup || a.id === openGroup);
    if (!aisle) return;
    if (aisleRaw[aisle.id]) return;
    let cancelled = false;
    setAisleLoading(aisle.id);
    (async () => {
      try {
        const files = aisle.files?.length ? aisle.files : [aisle.file];
        const all: SlimCatalogItem[] = [];
        for (const f of files) {
          const rows = await loadChunk(f);
          all.push(...rows);
        }
        if (!cancelled) {
          setAisleRaw((prev) => ({ ...prev, [aisle.id]: all }));
          setAisleShown((prev) => ({
            ...prev,
            [aisle.id]: Math.min(PAGE_SIZE, all.length),
          }));
        }
      } catch {
        /* keep index preview */
      } finally {
        if (!cancelled) setAisleLoading((cur) => (cur === aisle.id ? null : cur));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [openGroup, aisles, aisleRaw, searching]);

  const categories = useMemo(() => aisles.map((a) => a.cat), [aisles]);

  const searchGroups = useMemo(() => {
    const map = new Map<string, DriverItem[]>();
    for (const item of searchHits) {
      const key = item.category || "General";
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(item);
    }
    return Array.from(map.entries()).map(([cat, rows]) => ({
      cat,
      label: cat,
      count: rows.length,
      rows,
    }));
  }, [searchHits]);

  if (booting) {
    return (
      <main className="animate-rise py-16 text-sm text-mist-400">
        Loading archive index…
      </main>
    );
  }

  if (bootError) {
    return (
      <main className="panel text-mist-300">
        Catalog index unavailable ({bootError}). Run{" "}
        <span className="font-mono text-signal">python slice_catalog.py</span>{" "}
        then refresh.
      </main>
    );
  }

  return (
    <main>
      <section className="relative mb-10 animate-rise">
        <p className="mb-3 text-[0.7rem] uppercase tracking-[0.18em] text-signal">
          Enterprise Recovery Vault
        </p>
        <h1 className="max-w-4xl font-display text-[clamp(2.2rem,5.5vw,3.5rem)] font-extrabold leading-[1.05] tracking-tight text-mist-100 text-balance">
          Drv<span className="text-signal">Vault</span>
        </h1>
        <p className="mt-4 max-w-2xl font-display text-xl font-semibold leading-snug text-mist-200 text-balance sm:text-2xl">
          Professional recovery archive for industrial &amp; legacy hardware
        </p>
        <p className="mt-3 max-w-xl text-sm leading-relaxed text-mist-300 sm:text-base">
          {totalCount.toLocaleString()}+ archived packages. Open an aisle to
          browse, or search the full index.
        </p>

        <div className="mt-8">
          <form
            className="relative w-full max-w-2xl"
            role="search"
            onSubmit={(e) => e.preventDefault()}
          >
            <Search
              className="pointer-events-none absolute left-4 top-1/2 h-5 w-5 -translate-y-1/2 text-mist-400"
              aria-hidden
            />
            <input
              type="search"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                if (e.target.value.trim().length >= 2) setOpenGroup(null);
              }}
              placeholder="Type a model: CMP 40HX, X540, RTL8168, LSI 9211…"
              autoComplete="off"
              autoFocus
              className="w-full rounded-xl border border-mist-400/20 bg-ink-800/90 py-4 pl-12 pr-4 text-base text-mist-100 shadow-card outline-none transition placeholder:text-mist-400 focus:border-signal/50 focus:shadow-glow"
            />
          </form>
          <p className="mt-2 text-xs text-mist-400">
            Enter at least 2 characters to search all{" "}
            {totalCount.toLocaleString()} packages.
          </p>
        </div>

        <ul className="mt-6 flex flex-wrap gap-5 text-xs text-mist-400">
          <li className="inline-flex items-center gap-1.5">
            <Zap className="h-3.5 w-3.5 text-signal" aria-hidden />
            Chunked static catalog
          </li>
          <li className="inline-flex items-center gap-1.5">
            <ShieldCheck className="h-3.5 w-3.5 text-signal" aria-hidden />
            Zero storage · No registration
          </li>
        </ul>
      </section>

      {categories.length > 0 ? (
        <div
          className="mb-6 flex flex-wrap gap-2 animate-rise"
          aria-label="Category filters"
        >
          <button
            type="button"
            className={`rounded-lg border px-3 py-1.5 text-xs font-medium transition ${
              !category
                ? "border-signal/40 bg-signal/10 text-signal"
                : "border-mist-400/20 text-mist-300 hover:border-signal/30"
            }`}
            onClick={() => {
              setCategory("");
              setOpenGroup(null);
            }}
          >
            All aisles
          </button>
          {categories.map((c) => (
            <button
              key={c}
              type="button"
              className={`rounded-lg border px-3 py-1.5 text-xs font-medium transition ${
                category === c
                  ? "border-signal/40 bg-signal/10 text-signal"
                  : "border-mist-400/20 text-mist-300 hover:border-signal/30"
              }`}
              onClick={() => {
                setCategory(c);
                setOpenGroup(c);
              }}
            >
              {c}
            </button>
          ))}
        </div>
      ) : null}

      {searching ? (
        <section className="animate-rise" aria-live="polite">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-2 text-xs text-mist-400">
            <span>
              {searchingNet
                ? "Searching…"
                : `${searchHits.length}${
                    searchHits.length >= SEARCH_RESULT_CAP ? "+" : ""
                  } match${searchHits.length === 1 ? "" : "es"} for `}
              {!searchingNet ? (
                <>
                  <span className="font-mono text-mist-200">
                    &ldquo;{query.trim()}&rdquo;
                  </span>
                  {searchHits.length >= SEARCH_RESULT_CAP
                    ? ` · showing top ${SEARCH_RESULT_CAP}`
                    : null}
                </>
              ) : null}
            </span>
            <button
              type="button"
              className="text-signal hover:underline"
              onClick={() => {
                setQuery("");
                setCategory("");
                setOpenGroup(null);
              }}
            >
              Clear search
            </button>
          </div>

          {!searchingNet && searchHits.length === 0 ? (
            <div className="panel text-mist-300">
              No packages for that keyword. Try a shorter model code (e.g.{" "}
              <span className="font-mono text-signal">9211</span>,{" "}
              <span className="font-mono text-signal">X540</span>,{" "}
              <span className="font-mono text-signal">CMP</span>).
            </div>
          ) : (
            <div className="space-y-8">
              {searchGroups.map((g) => (
                <div key={g.cat}>
                  <h2 className="mb-3 flex items-center gap-2 font-display text-sm font-semibold tracking-wide text-mist-200">
                    <FolderOpen className="h-4 w-4 text-signal" aria-hidden />
                    {g.label}
                    <span className="font-mono text-xs font-normal text-mist-400">
                      {g.count}
                    </span>
                  </h2>
                  <DriverList items={g.rows} />
                </div>
              ))}
            </div>
          )}
        </section>
      ) : (
        <section className="animate-rise" aria-label="Catalog aisles">
          <p className="mb-4 text-xs text-mist-400">
            {totalCount.toLocaleString()} packages in {aisles.length} aisles —
            open one to browse (paginated), or search for an exact model.
          </p>

          <ul className="space-y-2">
            {aisles
              .filter((g) => !category || g.cat === category)
              .map((g) => {
                const open = openGroup === g.cat || openGroup === g.id;
                const raw = aisleRaw[g.id];
                const loading = aisleLoading === g.id;
                const shown = aisleShown[g.id] || PAGE_SIZE;
                const visible = raw
                  ? raw.slice(0, shown).map((row) => slimToDriver(row))
                  : g.preview.map((row) => slimToDriver(row));
                const loadedCount = raw?.length ?? 0;
                const canMore = raw ? shown < raw.length : false;
                return (
                  <li
                    key={g.id}
                    className="overflow-hidden rounded-xl border border-mist-400/15 bg-ink-800/50"
                  >
                    <button
                      type="button"
                      className="flex w-full items-center justify-between gap-3 px-4 py-3.5 text-left transition hover:bg-ink-700/40"
                      aria-expanded={open}
                      onClick={() =>
                        setOpenGroup((cur) =>
                          cur === g.cat || cur === g.id ? null : g.cat,
                        )
                      }
                    >
                      <span className="flex min-w-0 items-center gap-2.5">
                        <FolderOpen
                          className="h-4 w-4 shrink-0 text-signal"
                          aria-hidden
                        />
                        <span className="truncate font-display text-sm font-semibold text-mist-100">
                          {g.cat}
                        </span>
                      </span>
                      <span className="flex shrink-0 items-center gap-3">
                        <span className="font-mono text-xs text-mist-400">
                          {g.count}
                        </span>
                        <ChevronDown
                          className={`h-4 w-4 text-mist-400 transition ${
                            open ? "rotate-180 text-signal" : ""
                          }`}
                          aria-hidden
                        />
                      </span>
                    </button>

                    {open ? (
                      <div className="border-t border-mist-400/10 px-4 pb-4 pt-3">
                        <p className="mb-3 text-xs text-mist-400">
                          {loading
                            ? `Loading ${g.count.toLocaleString()} packages…`
                            : `Showing ${visible.length.toLocaleString()} of ${g.count.toLocaleString()}${
                                loadedCount && loadedCount !== g.count
                                  ? ` (chunk ${loadedCount.toLocaleString()})`
                                  : ""
                              }.`}
                        </p>
                        <DriverList items={visible} />
                        {canMore ? (
                          <div className="mt-4 flex justify-center">
                            <button
                              type="button"
                              className="rounded-lg border border-signal/40 bg-signal/10 px-4 py-2 text-sm text-signal transition hover:bg-signal/20"
                              onClick={() =>
                                setAisleShown((prev) => ({
                                  ...prev,
                                  [g.id]: Math.min(
                                    (prev[g.id] || PAGE_SIZE) + PAGE_SIZE,
                                    raw!.length,
                                  ),
                                }))
                              }
                            >
                              Load more (
                              {Math.min(PAGE_SIZE, raw!.length - shown)} more)
                            </button>
                          </div>
                        ) : null}
                      </div>
                    ) : null}
                  </li>
                );
              })}
          </ul>
        </section>
      )}
    </main>
  );
}
