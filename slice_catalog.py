#!/usr/bin/env python3
"""Slice commercial_catalog.json into Cloudflare Pages–safe static chunks.

Writes under web/public/data/:
  catalog_index.json   — aisle manifest + previews (small)
  catalog_search.json  — slim search docs (lazy-fetched by browser)
  slug_map.json        — slug → chunk path
  chunks/<aisle>[-pN].json — per-category slim items (each << 25MB)

Usage:
  python slice_catalog.py
  python slice_catalog.py --src data/commercial_catalog.json --out web/public/data
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_SRC = ROOT / "data" / "commercial_catalog.json"
DEFAULT_OUT = ROOT / "web" / "public" / "data"
MAX_CHUNK_BYTES = 20 * 1024 * 1024  # stay under CF Pages 25MB hard limit
PREVIEW_PER_AISLE = 8

# CN / mixed category → English aisle label (must stay aligned with web UI)
CATEGORY_LABEL: dict[str, str] = {
    "显卡": "Graphics / GPU",
    "网卡": "Network / NIC",
    "存储": "Storage / RAID",
    "存储控制器": "Storage / RAID",
    "存储_RAID": "Storage / RAID",
    "工控/板载网卡": "Industrial / NIC",
    "工控设备": "Industrial / IPC",
    "老旧采集卡/工控卡": "Industrial / Capture",
    "采集卡_板卡": "Industrial / Capture",
    "服务器": "Server Firmware",
    "服务器_整机": "Server Firmware",
    "固件": "Firmware / FPGA",
    "固件/BIOS": "Firmware / FPGA",
    "仪器": "Lab / DAQ",
    "通用": "General",
    "其他外设": "Peripherals",
    "其他": "Peripherals",
    "品牌打印机": "Printers",
    "串口_工控": "Industrial / Serial",
    "无线网卡": "Wireless / Wi-Fi",
    "嵌入式_开发板": "Embedded",
    "芯片组": "Chipset",
    "声卡": "Audio",
}


def _slugify(label: str) -> str:
    s = label.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")[:64] or "general"


def english_category(raw: str) -> str:
    raw = (raw or "").strip()
    if raw in CATEGORY_LABEL:
        return CATEGORY_LABEL[raw]
    if not re.search(r"[\u4e00-\u9fff]", raw):
        return raw or "Industrial"
    if re.search(r"显卡|gpu|graphics", raw, re.I):
        return "Graphics / GPU"
    if re.search(r"存储|raid|sas|scsi|megaraid", raw, re.I):
        return "Storage / RAID"
    if re.search(r"网卡|nic|ethernet", raw, re.I):
        return "Industrial / NIC"
    if re.search(r"无线|wifi|wi-fi", raw, re.I):
        return "Wireless / Wi-Fi"
    if re.search(r"采集|工控|daq|capture", raw, re.I):
        return "Industrial / Capture"
    if re.search(r"打印|printer", raw, re.I):
        return "Printers"
    if re.search(r"服务器|server", raw, re.I):
        return "Server Firmware"
    if re.search(r"嵌入|embedded|rk\d", raw, re.I):
        return "Embedded"
    return "Industrial"


def slim_item(it: dict[str, Any]) -> dict[str, Any] | None:
    model = str(it.get("exact_model") or it.get("model") or "").strip()
    if not model:
        return None
    raw_cat = str(it.get("hardware_category") or it.get("category") or "其他").strip()
    cat = english_category(raw_cat)
    url = str(
        it.get("original_direct_url")
        or it.get("verified_url")
        or it.get("source_url")
        or ""
    ).strip()
    if not url or url.startswith("file:") or url.startswith("/") or re.match(r"^[a-zA-Z]:[\\/]", url):
        return None
    pains = [str(p).strip() for p in (it.get("pain_points") or []) if str(p).strip()][:2]
    tags = [
        str(t).strip()
        for t in (it.get("tags") or [])
        if str(t).strip() and not re.search(r"[\u4e00-\u9fff]", str(t))
    ][:4]
    slug = str(it.get("slug") or "").strip()
    if not slug:
        slug = _slugify(f"{cat}-{model}")
    seo_title = str(it.get("seo_title") or "").strip() or f"{model} Legacy Driver Archive"
    seo_desc = str(it.get("seo_description") or it.get("description") or "").strip()
    if not seo_desc:
        seo_desc = pains[0] if pains else f"{model} — Legacy Driver Archive"
    # Cap long SEO blobs
    seo_desc = seo_desc[:280]
    out: dict[str, Any] = {
        "slug": slug[:120],
        "model": model[:160],
        "category": cat,
        "seo_title": seo_title[:90],
        "seo_description": seo_desc,
        "url": url,
        "verified": it.get("verified") is not False,
        "price": float(it.get("price") or 4.9),
        "currency": str(it.get("currency") or "USDT"),
        "content_length": int(it.get("content_length") or 0),
        "value_score": float(it.get("value_score") or 0),
        "file_name": str(it.get("file_name") or "").strip()[:120] or None,
    }
    if pains:
        out["pain_points"] = pains
    if tags:
        out["tags"] = tags
    # drop nulls
    return {k: v for k, v in out.items() if v is not None}


def search_doc(item: dict[str, Any]) -> dict[str, Any]:
    pains = item.get("pain_points") or []
    tags = item.get("tags") or []
    return {
        "s": item["slug"],
        "m": item["model"],
        "c": item["category"],
        "p": (pains[0] if pains else item.get("seo_description") or "")[:160],
        "t": tags[:4],
        "v": bool(item.get("verified", True)),
        "u": item["url"],
        "z": float(item.get("value_score") or 0),
        "b": int(item.get("content_length") or 0),
        "pr": float(item.get("price") or 4.9),
    }


def _write_json(path: Path, payload: Any) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    data = text.encode("utf-8")
    path.write_bytes(data)
    return len(data)


def split_oversized(
    aisle_id: str, items: list[dict[str, Any]]
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Split an aisle into multiple chunk files if serialized size exceeds cap."""
    probe = json.dumps({"items": items}, ensure_ascii=False, separators=(",", ":")).encode()
    if len(probe) <= MAX_CHUNK_BYTES:
        return [(aisle_id, items)]
    parts: list[tuple[str, list[dict[str, Any]]]] = []
    buf: list[dict[str, Any]] = []
    part = 1
    for it in items:
        trial = buf + [it]
        size = len(
            json.dumps({"items": trial}, ensure_ascii=False, separators=(",", ":")).encode()
        )
        if buf and size > MAX_CHUNK_BYTES:
            parts.append((f"{aisle_id}-p{part}", buf))
            part += 1
            buf = [it]
        else:
            buf = trial
    if buf:
        parts.append((f"{aisle_id}-p{part}", buf) if part > 1 or parts else (aisle_id, buf))
    return parts


def slice_catalog(
    *,
    src: Path = DEFAULT_SRC,
    out_dir: Path = DEFAULT_OUT,
    preview_per: int = PREVIEW_PER_AISLE,
) -> dict[str, Any]:
    if not src.exists():
        raise FileNotFoundError(src)
    raw = json.loads(src.read_text(encoding="utf-8"))
    rows = list(raw.get("items") or [])
    generated_at = raw.get("generated_at")

    by_aisle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    slimmed: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = slim_item(row)
        if not item:
            continue
        slimmed.append(item)
        aisle_id = _slugify(item["category"])
        by_aisle[aisle_id].append(item)

    # clear old chunks
    chunks_dir = out_dir / "chunks"
    if chunks_dir.exists():
        for old in chunks_dir.glob("*.json"):
            old.unlink(missing_ok=True)
    chunks_dir.mkdir(parents=True, exist_ok=True)

    slug_map: dict[str, str] = {}
    search_docs: list[dict[str, Any]] = []
    aisles_out: list[dict[str, Any]] = []
    chunk_files: list[dict[str, Any]] = []
    max_chunk = 0

    for aisle_id in sorted(by_aisle.keys(), key=lambda k: (-len(by_aisle[k]), k)):
        items = by_aisle[aisle_id]
        # stable-ish: higher value_score first
        items.sort(key=lambda x: float(x.get("value_score") or 0), reverse=True)
        label = items[0]["category"] if items else aisle_id
        parts = split_oversized(aisle_id, items)
        # preview from first part only
        preview = items[:preview_per]
        file_list: list[str] = []
        for part_id, part_items in parts:
            rel = f"chunks/{part_id}.json"
            nbytes = _write_json(
                out_dir / rel,
                {
                    "id": part_id,
                    "category": label,
                    "count": len(part_items),
                    "generated_at": generated_at,
                    "items": part_items,
                },
            )
            max_chunk = max(max_chunk, nbytes)
            file_list.append(f"/data/{rel.replace(chr(92), '/')}")
            chunk_files.append(
                {
                    "id": part_id,
                    "file": f"/data/{rel.replace(chr(92), '/')}",
                    "bytes": nbytes,
                    "count": len(part_items),
                }
            )
            for it in part_items:
                slug_map[it["slug"]] = f"/data/{rel.replace(chr(92), '/')}"
                search_docs.append(search_doc(it))

        aisles_out.append(
            {
                "id": aisle_id,
                "cat": label,
                "count": len(items),
                "files": file_list,
                "file": file_list[0],
                "preview": preview,
            }
        )

    index = {
        "title": "DrvVault Catalog Index",
        "mode": "chunked_static",
        "generated_at": generated_at,
        "sku_count": len(slimmed),
        "aisle_count": len(aisles_out),
        "max_chunk_bytes": max_chunk,
        "limit_bytes": 25 * 1024 * 1024,
        "aisles": aisles_out,
        "search_index": "/data/catalog_search.json",
        "slug_map": "/data/slug_map.json",
    }
    index_bytes = _write_json(out_dir / "catalog_index.json", index)
    search_bytes = _write_json(
        out_dir / "catalog_search.json",
        {
            "generated_at": generated_at,
            "count": len(search_docs),
            "docs": search_docs,
        },
    )
    slug_bytes = _write_json(
        out_dir / "slug_map.json",
        {"generated_at": generated_at, "count": len(slug_map), "map": slug_map},
    )

    # Guard: refuse if any chunk still over hard CF limit
    over = [c for c in chunk_files if c["bytes"] >= 25 * 1024 * 1024]
    if over:
        raise SystemExit(
            f"chunk still >=25MB: {over[0]['file']} ({over[0]['bytes']} bytes)"
        )

    # Replace web/data/commercial_catalog.json with a tiny pointer (avoid shipping 28MB)
    web_catalog = ROOT / "web" / "data" / "commercial_catalog.json"
    pointer = {
        "title": "DrvVault · Chunked Catalog Pointer",
        "mode": "chunked_static",
        "growth": True,
        "generated_at": generated_at,
        "sku_count": len(slimmed),
        "items": [],  # full body lives in public/data/chunks
        "public_index": "/data/catalog_index.json",
        "note": "Storefront reads public/data chunks; canonical full catalog is data/commercial_catalog.json",
    }
    web_catalog.parent.mkdir(parents=True, exist_ok=True)
    _write_json(web_catalog, pointer)

    summary = {
        "ok": True,
        "src": str(src),
        "out": str(out_dir),
        "sku_count": len(slimmed),
        "aisles": len(aisles_out),
        "chunks": len(chunk_files),
        "max_chunk_mb": round(max_chunk / 1024 / 1024, 3),
        "index_kb": round(index_bytes / 1024, 1),
        "search_mb": round(search_bytes / 1024 / 1024, 3),
        "slug_map_mb": round(slug_bytes / 1024 / 1024, 3),
        "all_under_25mb": True,
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Slice catalog for Cloudflare Pages")
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--preview", type=int, default=PREVIEW_PER_AISLE)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    summary = slice_catalog(src=args.src, out_dir=args.out, preview_per=args.preview)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(
            f"[slice] sku={summary['sku_count']} aisles={summary['aisles']} "
            f"chunks={summary['chunks']} max_chunk={summary['max_chunk_mb']}MB "
            f"search={summary['search_mb']}MB → {summary['out']}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[slice] FAIL: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
