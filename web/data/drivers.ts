import fs from "fs";
import path from "path";

import {
  commercialPain,
  commercialSubtitle,
  commercialTitle,
  normalizeCodeTerms,
  sanitizeCopy,
  VALUE_APPLICABILITY,
} from "@/lib/copy";
import { PAYMENT } from "@/lib/payment";

export type DriverItem = {
  slug: string;
  seoTitle: string;
  title: string;
  /** Commercial card / detail headline */
  displayTitle: string;
  /** One-line applicability / pain for cards */
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

type CatalogItem = {
  sku?: string;
  slug?: string;
  hardware_category?: string;
  exact_model?: string;
  model?: string;
  category?: string;
  seo_title?: string;
  seo_description?: string;
  pain_points?: string[];
  tags?: string[];
  google_longtail_queries?: string[];
  file_count?: number;
  content_length?: number;
  value_score?: number;
  original_direct_url?: string;
  verified_url?: string;
  source_url?: string;
  price?: number;
  currency?: string;
  file_name?: string;
  verified?: boolean;
  os_version?: string;
  description?: string;
  local_storage?: boolean;
};

type CatalogFile = {
  generated_at?: string | null;
  sku_count?: number;
  mode?: string;
  local_storage?: boolean;
  items?: CatalogItem[];
};

function catalogPaths(): string[] {
  // Prefer the canonical full catalog (repo data/), never the web/data pointer stub.
  return [
    path.join(process.cwd(), "..", "data", "commercial_catalog.json"),
    path.join(process.cwd(), "data", "commercial_catalog.json"),
  ];
}

type CatalogCache = {
  path: string;
  mtimeMs: number;
  file: CatalogFile;
  drivers: DriverItem[];
};

let _catalogCache: CatalogCache | null = null;

function resolveCatalogFile(): { path: string; mtimeMs: number } | null {
  for (const p of catalogPaths()) {
    try {
      if (!fs.existsSync(p)) continue;
      // Skip the tiny chunked pointer stub in web/data
      const st = fs.statSync(p);
      if (st.size < 4096) {
        const peek = fs.readFileSync(p, "utf-8").slice(0, 400);
        if (peek.includes("chunked_static") || peek.includes("public_index")) {
          continue;
        }
      }
      return { path: p, mtimeMs: st.mtimeMs };
    } catch {
      /* try next */
    }
  }
  return null;
}

function loadCatalog(): CatalogFile {
  const resolved = resolveCatalogFile();
  if (!resolved) return { items: [] };
  if (
    _catalogCache &&
    _catalogCache.path === resolved.path &&
    _catalogCache.mtimeMs === resolved.mtimeMs
  ) {
    return _catalogCache.file;
  }
  try {
    const text = fs.readFileSync(resolved.path, "utf-8").replace(/^\uFEFF/, "");
    const file = JSON.parse(text) as CatalogFile;
    // drivers rebuilt lazily in getAllDrivers
    _catalogCache = {
      path: resolved.path,
      mtimeMs: resolved.mtimeMs,
      file,
      drivers: [],
    };
    return file;
  } catch {
    return { items: [] };
  }
}

function asciiSlug(input: string): string {
  return input
    .toLowerCase()
    .replace(/[\\/]+/g, "-")
    .replace(/[^a-z0-9\-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

function toSlug(category: string, model: string, fileName: string, fallback?: string): string {
  const stem = asciiSlug((fileName || "").replace(/\.[^.]+$/, "")).slice(0, 28);
  const fromFallback = asciiSlug((fallback || "").trim());
  const fromParts = asciiSlug(`${category}-${model}`);

  let base = fromFallback || fromParts || "sku";
  if (stem && !base.includes(stem.slice(0, 8))) {
    base = `${base}-${stem}`;
  }
  return (base || "sku").slice(0, 96);
}

function formatBytes(bytes: number): string {
  if (!bytes || bytes <= 0) return "Size pending";
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function englishCategory(category: string): string {
  const raw = (category || "").trim();
  const map: Record<string, string> = {
    显卡: "Graphics",
    "老旧采集卡/工控卡": "Industrial / Capture",
    工控设备: "Industrial",
    "工控/板载网卡": "Industrial / NIC",
    存储控制器: "Storage / RAID",
    存储_RAID: "Storage / RAID",
    存储: "Storage / RAID",
    其他外设: "Peripherals",
    其他: "Peripherals",
  };
  if (map[raw]) return map[raw];
  // Already English / mixed — keep ASCII words only
  if (!/[\u4e00-\u9fff]/.test(raw)) return raw || "Industrial";
  if (/显卡|gpu|graphics/i.test(raw)) return "Graphics";
  if (/存储|raid|sas|scsi|megaraid/i.test(raw)) return "Storage / RAID";
  if (/网卡|nic|ethernet|usb/i.test(raw)) return "Industrial / NIC";
  if (/采集|工控|daq|capture/i.test(raw)) return "Industrial / Capture";
  return "Industrial";
}

function mapItem(raw: CatalogItem, generatedAt?: string | null): DriverItem | null {
  const category = englishCategory(
    (raw.hardware_category || raw.category || "").trim(),
  );
  const model = (raw.exact_model || raw.model || "").trim();
  if (!category || !model) return null;
  if (model === "未知型号" || category === "Peripherals") return null;

  const pains = (raw.pain_points || [])
    .map(String)
    .map(normalizeCodeTerms)
    .map(sanitizeCopy)
    .filter((p) => p && !/[\u4e00-\u9fff]/.test(p));
  const painBlob = pains.join(" ");
  const displayTitle = commercialTitle(model, category, painBlob);
  const cardPain = commercialPain(model, pains);

  const seoTitle = sanitizeCopy(
    (raw.seo_title || "").trim() ||
      `${model} Legacy Driver Archive | Code 43 Recovery`,
  ).slice(0, 70);
  const seoDescription = sanitizeCopy(
    (raw.seo_description || "").trim() ||
      pains[0] ||
      `${model} — Legacy Driver Archive. Code 43 / Code 10 recovery for industrial and legacy Windows.`,
  );

  const day = String(generatedAt || new Date().toISOString()).slice(0, 10);
  const downloadUrl = (
    raw.original_direct_url ||
    raw.verified_url ||
    raw.source_url ||
    ""
  ).trim();
  if (!downloadUrl) return null;
  // Reject any accidental local filesystem paths
  if (
    downloadUrl.startsWith("file:") ||
    downloadUrl.startsWith("/") ||
    /^[a-zA-Z]:[\\/]/.test(downloadUrl)
  ) {
    return null;
  }

  const bytes = Number(raw.content_length || 0);
  const fileName =
    (raw.file_name || "").trim() ||
    `${model.replace(/[\\/:*?"<>|]+/g, "_")}-driver-pack`;
  const currency = "USDT";
  const price = PAYMENT.amount;

  const tags = (raw.tags || [])
    .map(String)
    .map(sanitizeCopy)
    .filter(
      (t) =>
        t &&
        !/^local_storage/i.test(t) &&
        t.length < 40 &&
        !/[\u4e00-\u9fff]/.test(t),
    )
    .slice(0, 6);

  return {
    slug: toSlug(category, model, fileName, raw.slug),
    seoTitle,
    title: commercialSubtitle(model),
    displayTitle,
    cardPain,
    deviceModel: model,
    category,
    osVersion: (raw.os_version || "").trim() || "Windows XP / 7 / 10 · Industrial",
    summary: cardPain,
    seoDescription,
    painPoints: pains.length
      ? pains
      : [cardPain, "Code 43 hardware failure / device disabled recovery"],
    fixNotes: [
      VALUE_APPLICABILITY,
      "Confirm the hardware ID in Device Manager before installing.",
      "Uninstall the failing driver, reboot, then install this package.",
    ],
    tags,
    longtailQueries: (raw.google_longtail_queries || [])
      .map(String)
      .map(sanitizeCopy)
      .filter((q) => q && !/[\u4e00-\u9fff]/.test(q)),
    downloadUrl,
    fileName,
    fileSize: formatBytes(bytes),
    contentLength: bytes,
    fileCount: Number(raw.file_count || 1),
    valueScore: Number(raw.value_score || 0),
    price,
    currency,
    priceLabel: "4.90 USDT",
    verified: raw.verified !== false,
    updatedAt: day,
  };
}

export function getCatalogMeta(): {
  generatedAt: string | null;
  skuCount: number;
  mode: string;
  localStorage: boolean;
} {
  const cat = loadCatalog();
  return {
    generatedAt: cat.generated_at || null,
    skuCount: cat.sku_count || (cat.items || []).length,
    mode: cat.mode || "link_curation",
    localStorage: Boolean(cat.local_storage),
  };
}

export function getAllDrivers(): DriverItem[] {
  const resolved = resolveCatalogFile();
  if (!resolved) return [];
  if (
    _catalogCache &&
    _catalogCache.path === resolved.path &&
    _catalogCache.mtimeMs === resolved.mtimeMs &&
    _catalogCache.drivers.length > 0
  ) {
    return _catalogCache.drivers;
  }
  const cat = loadCatalog();
  const seen = new Set<string>();
  const items = (cat.items || [])
    .map((row) => mapItem(row, cat.generated_at))
    .filter((x): x is DriverItem => Boolean(x))
    .filter((item) => {
      if (seen.has(item.slug)) return false;
      seen.add(item.slug);
      return true;
    })
    .sort((a, b) => b.valueScore - a.valueScore);
  if (_catalogCache && _catalogCache.path === resolved.path) {
    _catalogCache.drivers = items;
  }
  return items;
}

export function getDriverBySlug(slug: string): DriverItem | undefined {
  let decoded = slug;
  try {
    decoded = decodeURIComponent(slug);
  } catch {
    /* keep raw */
  }
  // Prefer warm full cache; otherwise map only until match (no full 17k map).
  if (
    _catalogCache &&
    _catalogCache.drivers.length > 0 &&
    resolveCatalogFile()?.mtimeMs === _catalogCache.mtimeMs
  ) {
    return _catalogCache.drivers.find(
      (item) => item.slug === slug || item.slug === decoded,
    );
  }
  const cat = loadCatalog();
  for (const row of cat.items || []) {
    const item = mapItem(row, cat.generated_at);
    if (item && (item.slug === slug || item.slug === decoded)) return item;
  }
  return undefined;
}

export function getCategories(): string[] {
  return getHomeAisles(0).categories;
}

export type HomeAisle = {
  cat: string;
  count: number;
  preview: DriverItem[];
};

/** Homepage only — count raw rows, mapItem just for aisle previews. */
export function getHomeAisles(previewPer = 8): {
  total: number;
  categories: string[];
  aisles: HomeAisle[];
} {
  const cat = loadCatalog();
  const rows = cat.items || [];
  const total = Number(cat.sku_count || 0) || rows.length;
  type Bucket = { count: number; previewRaw: CatalogItem[] };
  const map = new Map<string, Bucket>();

  for (const row of rows) {
    const model = String(row.exact_model || row.model || "").trim();
    if (!model) continue;
    const rawCat = String(
      row.hardware_category || row.category || "其他",
    ).trim();
    const key = englishCategory(rawCat);
    let bucket = map.get(key);
    if (!bucket) {
      bucket = { count: 0, previewRaw: [] };
      map.set(key, bucket);
    }
    bucket.count += 1;
    if (previewPer > 0 && bucket.previewRaw.length < previewPer) {
      bucket.previewRaw.push(row);
    }
  }

  const aisles = Array.from(map.entries())
    .map(([catKey, bucket]) => ({
      cat: catKey,
      count: bucket.count,
      preview: bucket.previewRaw
        .map((row) => mapItem(row, cat.generated_at))
        .filter((x): x is DriverItem => Boolean(x)),
    }))
    .sort((a, b) => b.count - a.count || a.cat.localeCompare(b.cat));

  return {
    total,
    categories: aisles.map((a) => a.cat),
    aisles,
  };
}

type SearchDoc = {
  category: string;
  hay: string;
  raw: CatalogItem;
};

let _searchIndex: {
  path: string;
  mtimeMs: number;
  docs: SearchDoc[];
} | null = null;

function getSearchIndex(): SearchDoc[] {
  const resolved = resolveCatalogFile();
  if (!resolved) return [];
  if (
    _searchIndex &&
    _searchIndex.path === resolved.path &&
    _searchIndex.mtimeMs === resolved.mtimeMs
  ) {
    return _searchIndex.docs;
  }
  const cat = loadCatalog();
  const docs: SearchDoc[] = [];
  for (const row of cat.items || []) {
    const model = String(row.exact_model || row.model || "").trim();
    if (!model) continue;
    const category = englishCategory(
      String(row.hardware_category || row.category || "其他").trim(),
    );
    const hay = [
      model,
      category,
      row.seo_title || "",
      row.seo_description || "",
      row.description || "",
      ...(row.tags || []),
      ...(row.pain_points || []),
      ...(row.google_longtail_queries || []),
    ]
      .join(" ")
      .toLowerCase();
    docs.push({ category, hay, raw: row });
  }
  _searchIndex = {
    path: resolved.path,
    mtimeMs: resolved.mtimeMs,
    docs,
  };
  return docs;
}

/** Normalize Code 10 / code43 / Error 28 style queries. */
function expandErrorTokens(q: string): string[] {
  const tokens = q.toLowerCase().split(/\s+/).filter(Boolean);
  const extra: string[] = [];
  for (const t of tokens) {
    const m = t.match(/^(?:code|error|err)[-_]?(\d{1,3})$/i);
    if (m) {
      extra.push(`code ${m[1]}`, `code${m[1]}`, `error ${m[1]}`);
    }
    if (/^\d{1,3}$/.test(t) && ["10", "28", "31", "43", "52"].includes(t)) {
      extra.push(`code ${t}`, `code${t}`);
    }
  }
  return [...tokens, ...extra];
}

export function searchDrivers(
  query: string,
  opts?: { category?: string; limit?: number },
): DriverItem[] {
  const q = query.trim().toLowerCase();
  const limit = opts?.limit ?? 36;
  const category = (opts?.category || "").trim();
  const cat = loadCatalog();
  const docs = getSearchIndex();
  if (!q) {
    const base = category
      ? docs.filter((d) => d.category === category)
      : docs;
    return base
      .slice(0, limit)
      .map((d) => mapItem(d.raw, cat.generated_at))
      .filter((x): x is DriverItem => Boolean(x));
  }

  const tokens = expandErrorTokens(q);
  const hits: DriverItem[] = [];
  for (const doc of docs) {
    if (category && doc.category !== category) continue;
    const ok =
      doc.hay.includes(q) || tokens.every((token) => doc.hay.includes(token));
    if (!ok) continue;
    const item = mapItem(doc.raw, cat.generated_at);
    if (!item) continue;
    hits.push(item);
    if (hits.length >= limit) break;
  }
  return hits;
}
