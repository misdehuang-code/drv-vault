import type { MetadataRoute } from "next";
import fs from "fs";
import path from "path";

export const dynamic = "force-static";

/** Sitemap stays tiny — do not enumerate 17k SKUs (CF + crawl budget). */
export default function sitemap(): MetadataRoute.Sitemap {
  const base = process.env.NEXT_PUBLIC_SITE_URL || "https://drvvault.pages.dev";
  let lastMod = new Date();
  try {
    const idxPath = path.join(process.cwd(), "public", "data", "catalog_index.json");
    if (fs.existsSync(idxPath)) {
      const idx = JSON.parse(fs.readFileSync(idxPath, "utf-8")) as {
        generated_at?: string;
      };
      if (idx.generated_at) lastMod = new Date(idx.generated_at);
    }
  } catch {
    /* ignore */
  }
  return [
    {
      url: base,
      lastModified: lastMod,
      changeFrequency: "daily",
      priority: 1,
    },
  ];
}
