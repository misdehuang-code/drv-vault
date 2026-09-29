import { NextRequest, NextResponse } from "next/server";

import { searchDrivers } from "@/data/drivers";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const q = (req.nextUrl.searchParams.get("q") || "").trim();
  const category = (req.nextUrl.searchParams.get("category") || "").trim();
  const limitRaw = Number(req.nextUrl.searchParams.get("limit") || 36);
  const limit = Number.isFinite(limitRaw)
    ? Math.max(1, Math.min(60, limitRaw))
    : 36;

  if (q.length < 2) {
    return NextResponse.json({ items: [], total: 0, q, category });
  }

  const items = searchDrivers(q, { category: category || undefined, limit });
  return NextResponse.json({
    items,
    total: items.length,
    capped: items.length >= limit,
    q,
    category,
  });
}
