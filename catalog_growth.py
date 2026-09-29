"""Catalog growth ledger — storefront SKUs only grow (junk may be purged).

Never let MERCHANDIZE / Sync shrink the commercial catalog by overwriting
with a smaller ephemeral index. Historical verified links are kept in
data/catalog_ledger.json and always merged forward.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlparse, urlunparse

from json_io import read_json, write_json

ROOT = Path(__file__).resolve().parent
LEDGER_PATH = ROOT / "data" / "catalog_ledger.json"
COMMERCIAL = ROOT / "data" / "commercial_catalog.json"
WEB_CATALOG = ROOT / "web" / "data" / "commercial_catalog.json"
VAULT_INDEX = ROOT / "vault_index.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        p = urlparse(raw)
        scheme = (p.scheme or "https").lower()
        netloc = (p.netloc or "").lower()
        path = re.sub(r"/+", "/", p.path or "")
        if path.endswith("/") and len(path) > 1:
            path = path.rstrip("/")
        query = p.query
        if query and any(k in query.lower() for k in ("utm_", "fbclid", "gclid")):
            parts = [
                kv
                for kv in query.split("&")
                if kv
                and not kv.lower().startswith(("utm_", "fbclid=", "gclid="))
            ]
            query = "&".join(parts)
        return urlunparse((scheme, netloc, path, "", query, ""))
    except Exception:
        return raw


def item_url(it: dict[str, Any]) -> str:
    return str(
        it.get("original_direct_url")
        or it.get("verified_url")
        or it.get("source_url")
        or ""
    ).strip()


def score(it: dict[str, Any]) -> float:
    try:
        return float(it.get("value_score") or 0)
    except (TypeError, ValueError):
        return 0.0


def is_keepable(it: dict[str, Any]) -> bool:
    """Good commercial SKU worth preserving forever (until proven junk)."""
    if not isinstance(it, dict):
        return False
    if it.get("verified") is False:
        return False
    url = item_url(it)
    if not url.startswith("http"):
        return False
    if url.startswith(("file:", "/", "\\\\")):
        return False
    # 宽松入库 / 智能兜底：不因「非直链」拦截
    if it.get("soft_fulfill") or it.get("link_mode") in {
        "soft_fallback",
        "archive_fallback",
        "force_soft",
    }:
        model = str(
            it.get("exact_model") or it.get("model") or it.get("hardware_model") or ""
        ).strip()
        cat = str(
            it.get("hardware_category") or it.get("category") or ""
        ).strip()
        return bool(model and cat and model != "未知型号")
    try:
        from link_curator import is_non_driver_url

        if is_non_driver_url(url, str(it.get("file_name") or "")):
            return False
    except Exception:
        pass
    model = str(
        it.get("exact_model") or it.get("model") or it.get("hardware_model") or ""
    ).strip()
    cat = str(
        it.get("hardware_category") or it.get("category") or ""
    ).strip()
    return bool(model and cat and model != "未知型号")


def load_ledger(path: Path = LEDGER_PATH) -> dict[str, Any]:
    data = read_json(path, default={}) or {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("title", "DrvVault Catalog Growth Ledger")
    data.setdefault("mode", "append_only")
    data.setdefault("items", [])
    return data


def save_ledger(items: list[dict[str, Any]], path: Path = LEDGER_PATH) -> dict[str, Any]:
    payload = {
        "title": "DrvVault Catalog Growth Ledger",
        "mode": "append_only",
        "updated_at": _utc_now(),
        "sku_count": len(items),
        "local_storage": False,
        "items": items,
    }
    write_json(path, payload)
    return payload


def merge_items(*groups: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Union by normalized URL; higher value_score wins on conflict."""
    by_url: dict[str, dict[str, Any]] = {}
    for group in groups:
        for it in group or []:
            if not is_keepable(it):
                continue
            url = item_url(it)
            key = normalize_url(url)
            if not key:
                continue
            row = dict(it)
            row["original_direct_url"] = url
            row["verified_url"] = url
            row["source_url"] = url
            row["verified"] = True
            row["local_storage"] = False
            row.pop("local_dir", None)
            row.pop("local_path", None)
            row.pop("vault_path", None)
            prev = by_url.get(key)
            if not prev or score(row) >= score(prev):
                by_url[key] = row
    return sorted(by_url.values(), key=score, reverse=True)


def model_key(it: dict[str, Any]) -> str:
    """Stable storefront identity: normalized category|model (fallback URL)."""
    raw_model = str(
        it.get("exact_model") or it.get("model") or it.get("hardware_model") or ""
    ).strip()
    raw_cat = str(
        it.get("hardware_category") or it.get("category") or ""
    ).strip()
    try:
        from catalog_rules import normalize_category, normalize_model

        model = normalize_model(raw_model, context=raw_cat) or raw_model
        cat = normalize_category(raw_cat, context=model) or raw_cat
    except Exception:
        model, cat = raw_model, raw_cat
    model = re.sub(r"\s+", " ", model).strip()
    cat = re.sub(r"\s+", " ", cat).strip()
    if not model:
        return f"url:{normalize_url(item_url(it))}"
    return f"{cat.lower()}|{model.lower()}"


def dedupe_storefront(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Strict dedupe: URL unique already assumed; one card per normalized model."""
    stats = {"input": len(items), "model_dupes": 0, "slug_dupes": 0, "output": 0}
    by_model: dict[str, dict[str, Any]] = {}
    for it in items:
        key = model_key(it)
        prev = by_model.get(key)
        if prev:
            stats["model_dupes"] += 1
            if score(it) < score(prev):
                continue
        # normalize fields onto the winning row
        row = dict(it)
        parts = key.split("|", 1)
        if len(parts) == 2 and not key.startswith("url:"):
            row["hardware_category"] = row.get("hardware_category") or parts[0]
            row["category"] = row.get("category") or parts[0]
            row["exact_model"] = row.get("exact_model") or parts[1]
            row["model"] = row.get("model") or parts[1]
            row["hardware_model"] = row.get("hardware_model") or parts[1]
        by_model[key] = row

    # second pass: slug collision (rare naming edge cases)
    by_slug: dict[str, dict[str, Any]] = {}
    for it in by_model.values():
        slug = str(it.get("slug") or "").strip().lower()
        if not slug:
            mk = model_key(it)
            slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", mk).strip("-")[:96] or "sku"
            it["slug"] = slug
        prev = by_slug.get(slug)
        if prev:
            stats["slug_dupes"] += 1
            if score(it) < score(prev):
                continue
        by_slug[slug] = it

    out = sorted(by_slug.values(), key=score, reverse=True)
    stats["output"] = len(out)
    stats["removed"] = stats["input"] - stats["output"]
    return out, stats


def seed_ledger_from_disk() -> list[dict[str, Any]]:
    """Bootstrap / refresh ledger from every known local catalog source."""
    ledger = load_ledger()
    groups = [
        list(ledger.get("items") or []),
        list((read_json(COMMERCIAL, default={}) or {}).get("items") or []),
        list((read_json(WEB_CATALOG, default={}) or {}).get("items") or []),
        list((read_json(VAULT_INDEX, default={}) or {}).get("items") or []),
    ]
    merged = merge_items(*groups)
    # Ledger also storefront-deduped: 可持续增长的是「唯一型号」，不是重复链接堆
    storefront, _ = dedupe_storefront(merged)
    save_ledger(storefront)
    return storefront


def grow_catalog(
    incoming: Optional[list[dict[str, Any]]] = None,
    *,
    write_storefront: bool = True,
) -> dict[str, Any]:
    """Merge + strict dedupe: grow unique models, never keep URL/model duplicates."""
    incoming = list(incoming or [])
    prior = seed_ledger_from_disk()
    prior_n = len(prior)

    merged_urls = merge_items(prior, incoming)
    storefront, dedupe_stats = dedupe_storefront(merged_urls)

    # Growth guard: unique-model count must not drop below prior (junk already filtered)
    if len(storefront) < prior_n:
        # Re-merge prior winners so a bad incoming batch cannot wipe history
        storefront, dedupe_stats = dedupe_storefront(merge_items(prior, storefront))

    save_ledger(storefront)

    result = {
        "ok": True,
        "ledger_count": len(storefront),
        "sku_count": len(storefront),
        "added": max(0, len(storefront) - prior_n),
        "preserved": prior_n,
        "dedupe": dedupe_stats,
        "items": storefront,
        "ledger_items": storefront,
        "growth": True,
    }

    if write_storefront:
        catalog = {
            "title": "DrvVault · Verified Direct Link Catalog",
            "mode": "link_curation",
            "growth": True,
            "generated_at": _utc_now(),
            "sku_count": len(storefront),
            "ledger_count": len(storefront),
            "dedupe": dedupe_stats,
            "local_storage": False,
            "items": storefront,
        }
        write_json(COMMERCIAL, catalog)
        write_json(WEB_CATALOG, catalog)
        idx = read_json(VAULT_INDEX, default={}) or {}
        idx.update(
            {
                "mode": "link_curation",
                "local_storage": False,
                "commercial": True,
                "growth": True,
                "generated_at": catalog["generated_at"],
                "items": storefront,
                "device_count": len(storefront),
                "link_count": len(storefront),
                "file_count": len(storefront),
                "ledger_count": len(storefront),
                "dedupe": dedupe_stats,
            }
        )
        write_json(VAULT_INDEX, idx)
        # Cloudflare Pages: keep public/data chunks under 25MB each
        try:
            from slice_catalog import slice_catalog

            sliced = slice_catalog()
            result["slice"] = {
                "sku_count": sliced.get("sku_count"),
                "chunks": sliced.get("chunks"),
                "max_chunk_mb": sliced.get("max_chunk_mb"),
            }
        except Exception as exc:
            result["slice_error"] = str(exc)
        result["wrote"] = [
            str(COMMERCIAL),
            str(WEB_CATALOG),
            str(VAULT_INDEX),
            str(LEDGER_PATH),
        ]

    return result
