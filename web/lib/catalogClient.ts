/**
 * Client-side catalog access against /data/* static chunks (CF Pages safe).
 * No Node fs — works in browser and static export.
 */

export type SlimCatalogItem = {
  slug: string;
  model: string;
  category: string;
  seo_title: string;
  seo_description: string;
  url: string;
  verified: boolean;
  price: number;
  currency: string;
  content_length: number;
  value_score: number;
  file_name?: string;
  pain_points?: string[];
  tags?: string[];
};

export type CatalogAisle = {
  id: string;
  cat: string;
  count: number;
  file: string;
  files: string[];
  preview: SlimCatalogItem[];
};

export type CatalogIndex = {
  sku_count: number;
  generated_at?: string | null;
  aisles: CatalogAisle[];
  search_index: string;
  slug_map: string;
};

export type SearchDoc = {
  s: string;
  m: string;
  c: string;
  p: string;
  t?: string[];
  v: boolean;
  u: string;
  z: number;
  b: number;
  pr: number;
};

/** Shape expected by DriverList / CheckoutModal */
export type DriverItem = {
  slug: string;
  seoTitle: string;
  title: string;
  displayTitle: string;
  cardPain: string;
  deviceModel: string;
  category: string;
  osVersion: string;
  summary: string;
  seoDescription: string;
  painPoints: string[];
  fixNotes: string[];
  tags: string[];
  longtailQueries: string[];
  downloadUrl: string;
  fileName: string;
  fileSize: string;
  contentLength: number;
  fileCount: number;
  valueScore: number;
  price: number;
  currency: string;
  priceLabel: string;
  verified: boolean;
  updatedAt: string;
};

function formatBytes(n: number): string {
  if (!n || n < 0) return "Verified pack";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function slimToDriver(item: SlimCatalogItem, updatedAt?: string): DriverItem {
  const model = item.model;
  const pain =
    (item.pain_points && item.pain_points[0]) ||
    item.seo_description ||
    `${model} — Legacy Driver Archive`;
  return {
    slug: item.slug,
    seoTitle: item.seo_title,
    title: `Legacy Driver Archive · ${model}`,
    displayTitle: model,
    cardPain: pain,
    deviceModel: model,
    category: item.category,
    osVersion: "Windows XP / 7 / 10 · Industrial",
    summary: pain,
    seoDescription: item.seo_description || pain,
    painPoints: item.pain_points?.length
      ? item.pain_points
      : [pain, "Code 43 hardware failure / device disabled recovery"],
    fixNotes: [
      "Confirm the hardware ID in Device Manager before installing.",
      "Uninstall the failing driver, reboot, then install this package.",
    ],
    tags: item.tags || [],
    longtailQueries: [],
    downloadUrl: item.url,
    fileName: item.file_name || `${model.replace(/[\\/:*?"<>|]+/g, "_")}-driver-pack`,
    fileSize: formatBytes(item.content_length || 0),
    contentLength: item.content_length || 0,
    fileCount: 1,
    valueScore: item.value_score || 0,
    price: item.price || 4.9,
    currency: item.currency || "USDT",
    priceLabel: "4.90 USDT",
    verified: item.verified !== false,
    updatedAt: (updatedAt || new Date().toISOString()).slice(0, 10),
  };
}

export function searchDocToDriver(doc: SearchDoc): DriverItem {
  return slimToDriver({
    slug: doc.s,
    model: doc.m,
    category: doc.c,
    seo_title: `${doc.m} Legacy Driver Archive`,
    seo_description: doc.p,
    url: doc.u,
    verified: doc.v,
    price: doc.pr || 4.9,
    currency: "USDT",
    content_length: doc.b || 0,
    value_score: doc.z || 0,
    pain_points: doc.p ? [doc.p] : [],
    tags: doc.t || [],
  });
}

let _indexPromise: Promise<CatalogIndex> | null = null;
let _searchPromise: Promise<SearchDoc[]> | null = null;
let _slugMapPromise: Promise<Record<string, string>> | null = null;
const _chunkCache = new Map<string, Promise<SlimCatalogItem[]>>();

async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { cache: "force-cache" });
  if (!res.ok) throw new Error(`fetch ${url} → ${res.status}`);
  return (await res.json()) as T;
}

export function loadCatalogIndex(): Promise<CatalogIndex> {
  if (!_indexPromise) {
    _indexPromise = fetchJson<CatalogIndex>("/data/catalog_index.json").catch((err) => {
      _indexPromise = null;
      throw err;
    });
  }
  return _indexPromise;
}

export function loadSearchDocs(): Promise<SearchDoc[]> {
  if (!_searchPromise) {
    _searchPromise = fetchJson<{ docs: SearchDoc[] }>("/data/catalog_search.json")
      .then((d) => d.docs || [])
      .catch((err) => {
        _searchPromise = null;
        throw err;
      });
  }
  return _searchPromise;
}

export function loadSlugMap(): Promise<Record<string, string>> {
  if (!_slugMapPromise) {
    _slugMapPromise = fetchJson<{ map: Record<string, string> }>("/data/slug_map.json")
      .then((d) => d.map || {})
      .catch((err) => {
        _slugMapPromise = null;
        throw err;
      });
  }
  return _slugMapPromise;
}

export function loadChunk(file: string): Promise<SlimCatalogItem[]> {
  const key = file;
  let p = _chunkCache.get(key);
  if (!p) {
    p = fetchJson<{ items: SlimCatalogItem[] }>(file)
      .then((d) => d.items || [])
      .catch((err) => {
        _chunkCache.delete(key);
        throw err;
      });
    _chunkCache.set(key, p);
  }
  return p;
}

export async function searchCatalog(
  query: string,
  opts?: { category?: string; limit?: number },
): Promise<DriverItem[]> {
  const q = query.trim().toLowerCase();
  if (q.length < 2) return [];
  const limit = opts?.limit ?? 36;
  const category = (opts?.category || "").trim();
  const docs = await loadSearchDocs();
  const tokens = q.split(/\s+/).filter(Boolean);
  const hits: DriverItem[] = [];
  for (const doc of docs) {
    if (category && doc.c !== category) continue;
    const hay = [doc.m, doc.c, doc.p, ...(doc.t || [])].join(" ").toLowerCase();
    if (!hay.includes(q) && !tokens.every((t) => hay.includes(t))) continue;
    hits.push(searchDocToDriver(doc));
    if (hits.length >= limit) break;
  }
  return hits;
}

export async function getDriverBySlugClient(
  slug: string,
): Promise<DriverItem | undefined> {
  let decoded = slug;
  try {
    decoded = decodeURIComponent(slug);
  } catch {
    /* keep */
  }
  // SPA rewrite placeholder
  if (decoded === "_" || decoded === "__spa__") return undefined;

  const map = await loadSlugMap();
  const file = map[decoded] || map[slug];
  if (!file) return undefined;
  const items = await loadChunk(file);
  const found = items.find((i) => i.slug === decoded || i.slug === slug);
  return found ? slimToDriver(found) : undefined;
}
