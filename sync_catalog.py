#!/usr/bin/env python3
"""Strict catalog dedupe + dual-write to web/data (+ optional Cloudflare Pages deploy).

Usage:
  python sync_catalog.py              # dedupe + write data/ + web/data/
  python sync_catalog.py --deploy     # then npm build (CF_PAGES) + wrangler pages deploy
  python sync_catalog.py --status     # print JSON sync status
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
COMMERCIAL = DATA / "commercial_catalog.json"
WEB_CATALOG = ROOT / "web" / "data" / "commercial_catalog.json"
VAULT_INDEX = ROOT / "vault_index.json"
SYNC_STATE = DATA / "cf_sync_state.json"
WEB_DIR = ROOT / "web"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8").lstrip("\ufeff"))
    except Exception:
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ascii_slug(text: str) -> str:
    s = (text or "").lower()
    out: list[str] = []
    for ch in s.replace("\\", "-").replace("/", "-"):
        if ("a" <= ch <= "z") or ("0" <= ch <= "9") or ch == "-":
            out.append(ch)
        else:
            out.append("-")
    cleaned = "".join(out)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-")


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
        # drop tracking query noise but keep meaningful query for some CDNs
        query = p.query
        if query and any(k in query.lower() for k in ("utm_", "fbclid", "gclid")):
            parts = [kv for kv in query.split("&") if not kv.lower().startswith(("utm_", "fbclid", "gclid"))]
            query = "&".join(parts)
        return urlunparse((scheme, netloc, path, "", query, ""))
    except Exception:
        return raw.rstrip("/").lower()


def model_key(item: dict[str, Any]) -> str:
    cat = str(item.get("hardware_category") or item.get("category") or "").strip().lower()
    model = str(item.get("exact_model") or item.get("hardware_model") or "").strip().lower()
    return f"{cat}|{model}"


def item_slug(item: dict[str, Any]) -> str:
    existing = ascii_slug(str(item.get("slug") or ""))
    if existing:
        stem = ascii_slug(Path(str(item.get("file_name") or "pack")).stem)[:28]
        if stem and stem[:8] not in existing:
            return f"{existing}-{stem}"[:96]
        return existing[:96]
    cat = str(item.get("hardware_category") or item.get("category") or "")
    model = str(item.get("exact_model") or item.get("hardware_model") or "")
    stem = ascii_slug(Path(str(item.get("file_name") or "pack")).stem)[:28]
    base = ascii_slug(f"{cat}-{model}") or "sku"
    if stem and stem[:8] not in base:
        base = f"{base}-{stem}"
    return base[:96]


def score(item: dict[str, Any]) -> float:
    try:
        return float(item.get("value_score") or 0)
    except (TypeError, ValueError):
        return 0.0


def collect_raw_items() -> list[dict[str, Any]]:
    """Prefer vault_index verified items; merge catalog extras by URL."""
    by_url: dict[str, dict[str, Any]] = {}

    def eat(rows: list[dict[str, Any]]) -> None:
        for it in rows:
            url = str(
                it.get("original_direct_url")
                or it.get("verified_url")
                or it.get("source_url")
                or ""
            ).strip()
            if not url:
                continue
            if it.get("verified") is False:
                continue
            key = normalize_url(url)
            if not key:
                continue
            prev = by_url.get(key)
            if not prev or score(it) >= score(prev):
                row = dict(it)
                row["original_direct_url"] = url
                row["verified_url"] = url
                row["source_url"] = url
                row["_norm_url"] = key
                row.pop("local_dir", None)
                row.pop("local_path", None)
                by_url[key] = row

    idx = _read_json(VAULT_INDEX)
    eat(list(idx.get("items") or []))
    cat = _read_json(COMMERCIAL)
    eat(list(cat.get("items") or []))
    web = _read_json(WEB_CATALOG)
    eat(list(web.get("items") or []))
    return list(by_url.values())


def strict_dedupe(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Dedupe by URL (already unique), then model, then slug — keep highest value_score."""
    stats = {"input": len(items), "url_dupes": 0, "model_dupes": 0, "slug_dupes": 0}

    # URL layer
    by_url: dict[str, dict[str, Any]] = {}
    for it in items:
        key = it.get("_norm_url") or normalize_url(
            str(
                it.get("original_direct_url")
                or it.get("verified_url")
                or it.get("source_url")
                or ""
            )
        )
        if not key:
            continue
        prev = by_url.get(key)
        if prev:
            stats["url_dupes"] += 1
            if score(it) < score(prev):
                continue
        by_url[key] = it
    stage1 = list(by_url.values())

    # Model layer
    by_model: dict[str, dict[str, Any]] = {}
    for it in stage1:
        mk = model_key(it)
        if not mk or mk == "|":
            # keep orphans under unique url key
            mk = f"url:{it.get('_norm_url')}"
        prev = by_model.get(mk)
        if prev:
            stats["model_dupes"] += 1
            if score(it) < score(prev):
                continue
        by_model[mk] = it
    stage2 = list(by_model.values())

    # Slug layer
    by_slug: dict[str, dict[str, Any]] = {}
    for it in stage2:
        slug = item_slug(it)
        prev = by_slug.get(slug)
        if prev:
            stats["slug_dupes"] += 1
            if score(it) < score(prev):
                continue
        row = dict(it)
        row["slug"] = slug
        cat = str(row.get("hardware_category") or row.get("category") or "").strip()
        model = str(
            row.get("exact_model") or row.get("hardware_model") or row.get("model") or ""
        ).strip()
        url = str(
            row.get("original_direct_url")
            or row.get("verified_url")
            or row.get("source_url")
            or ""
        ).strip()
        row["model"] = model
        row["hardware_category"] = cat
        row["exact_model"] = model
        row["category"] = cat
        row["hardware_model"] = model
        row["sku"] = f"{cat}/{model}" if cat and model else slug
        row["verified"] = True
        row["original_direct_url"] = url
        row["verified_url"] = url
        row["source_url"] = url
        row["description"] = str(
            row.get("description") or row.get("seo_description") or ""
        )
        row["price"] = float(row.get("price") or 4.9)
        row["currency"] = "USDT"
        row["local_storage"] = False
        row.pop("local_dir", None)
        row.pop("local_path", None)
        row.pop("vault_path", None)
        row.pop("_norm_url", None)
        by_slug[slug] = row

    cleaned = sorted(by_slug.values(), key=score, reverse=True)
    stats["output"] = len(cleaned)
    stats["removed"] = stats["input"] - stats["output"]
    return cleaned, stats


def fingerprint_items(items: list[dict[str, Any]]) -> str:
    parts = []
    for it in sorted(items, key=lambda x: str(x.get("slug") or "")):
        parts.append(
            f"{it.get('slug')}|{normalize_url(str(it.get('original_direct_url') or it.get('verified_url') or ''))}|{it.get('price')}"
        )
    raw = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load_sync_state() -> dict[str, Any]:
    return _read_json(SYNC_STATE) or {
        "catalog_fingerprint": None,
        "sku_count": 0,
        "urls": [],
        "synced_at": None,
        "deploy_ok": False,
    }


def save_sync_state(state: dict[str, Any]) -> None:
    _write_json(SYNC_STATE, state)


def build_catalog(items: list[dict[str, Any]], dedupe_stats: dict[str, int]) -> dict[str, Any]:
    return {
        "title": "DrvVault · Verified Direct Link Catalog",
        "mode": "link_curation",
        "generated_at": _utc_now(),
        "sku_count": len(items),
        "removed_non_commercial": int(dedupe_stats.get("removed") or 0),
        "dedupe": dedupe_stats,
        "local_storage": False,
        "items": items,
    }


def sync_catalog(*, deploy: bool = False) -> dict[str, Any]:
    """Merge-grow local catalogs then optional Cloudflare deploy.

    Never shrinks the commercial catalog: historical keepable SKUs are
    preserved in catalog_ledger.json and always merged forward.
    """
    from catalog_growth import grow_catalog, seed_ledger_from_disk

    # Ensure ledger has everything currently on disk before merge
    seed_ledger_from_disk()
    raw = collect_raw_items()
    cleaned, stats = strict_dedupe(raw)
    grown = grow_catalog(cleaned, write_storefront=True)
    final_items = list(grown.get("items") or cleaned)
    catalog = build_catalog(final_items, stats)
    catalog["growth"] = True
    catalog["ledger_count"] = grown.get("ledger_count")
    catalog["preserved"] = grown.get("preserved")
    catalog["added"] = grown.get("added")
    catalog["dedupe"] = {
        **(stats or {}),
        **(grown.get("dedupe") or {}),
    }
    _write_json(COMMERCIAL, catalog)
    _write_json(WEB_CATALOG, catalog)

    idx = _read_json(VAULT_INDEX) or {}
    idx["mode"] = "link_curation"
    idx["local_storage"] = False
    idx["commercial"] = True
    idx["growth"] = True
    idx["generated_at"] = catalog["generated_at"]
    idx["items"] = final_items
    idx["device_count"] = len(final_items)
    idx["link_count"] = len(final_items)
    idx["file_count"] = len(final_items)
    idx["ledger_count"] = grown.get("ledger_count")
    _write_json(VAULT_INDEX, idx)

    fp = fingerprint_items(final_items)
    result: dict[str, Any] = {
        "ok": True,
        "sku_count": len(final_items),
        "ledger_count": grown.get("ledger_count"),
        "added": grown.get("added"),
        "preserved": grown.get("preserved"),
        "growth": True,
        "dedupe": stats,
        "fingerprint": fp,
        "wrote": [str(COMMERCIAL), str(WEB_CATALOG)],
        "deploy_ok": False,
        "deploy_skipped": not deploy,
        "deploy_log": "",
    }

    if deploy:
        dep = run_cloudflare_deploy()
        result["deploy_ok"] = bool(dep.get("ok"))
        result["deploy_log"] = str(dep.get("log") or "")
        result["deploy_error"] = dep.get("error")
        if dep.get("ok"):
            state = {
                "catalog_fingerprint": fp,
                "sku_count": len(final_items),
                "urls": [
                    normalize_url(
                        str(
                            i.get("original_direct_url")
                            or i.get("verified_url")
                            or ""
                        )
                    )
                    for i in final_items
                ],
                "synced_at": _utc_now(),
                "deploy_ok": True,
                "last_error": None,
                "growth": True,
            }
            save_sync_state(state)
        else:
            st = load_sync_state()
            st["last_error"] = dep.get("error") or "deploy_failed"
            st["last_local_sync_at"] = _utc_now()
            st["pending_fingerprint"] = fp
            save_sync_state(st)
    else:
        st = load_sync_state()
        st["last_local_sync_at"] = _utc_now()
        st["pending_fingerprint"] = fp
        save_sync_state(st)

    return result


def run_cloudflare_deploy() -> dict[str, Any]:
    """Build Next static export (CF_PAGES=1) and wrangler pages deploy."""
    project = (
        os.environ.get("CLOUDFLARE_PAGES_PROJECT")
        or os.environ.get("CF_PAGES_PROJECT")
        or "drvvault"
    ).strip()
    dist_name = (os.environ.get("CF_PAGES_DIST") or "out").strip()
    dist = WEB_DIR / dist_name
    logs: list[str] = []

    custom = (os.environ.get("CF_DEPLOY_CMD") or "").strip()
    if custom:
        logs.append(f"$ {custom}")
        try:
            proc = subprocess.run(
                custom,
                shell=True,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=int(os.environ.get("CF_DEPLOY_TIMEOUT", "900")),
            )
            logs.append(proc.stdout or "")
            logs.append(proc.stderr or "")
            if proc.returncode != 0:
                return {"ok": False, "error": f"custom_deploy_exit_{proc.returncode}", "log": "\n".join(logs)}
            return {"ok": True, "log": "\n".join(logs)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "log": "\n".join(logs)}

    api_dir = WEB_DIR / "app" / "api"
    api_bak = WEB_DIR / ".api_backup_cf"
    moved_api = False
    try:
        # npm run build → scripts/build-static.mjs
        # (slice catalog, stash app/api, next export → out/, restore api, 25MB guard)
        env = os.environ.copy()
        env["CF_PAGES"] = "1"
        env["NEXT_TELEMETRY_DISABLED"] = "1"
        logs.append("[cf] npm run build (static export + catalog slice)")
        build = subprocess.run(
            ["npm", "run", "build"],
            cwd=str(WEB_DIR),
            capture_output=True,
            text=True,
            timeout=900,
            env=env,
            shell=os.name == "nt",
        )
        logs.append(build.stdout or "")
        logs.append(build.stderr or "")
        if build.returncode != 0:
            return {"ok": False, "error": "next_build_failed", "log": "\n".join(logs)}

        if not dist.exists():
            return {
                "ok": False,
                "error": f"missing_dist:{dist}",
                "log": "\n".join(logs),
            }

        cmd = [
            "npx",
            "--yes",
            "wrangler",
            "pages",
            "deploy",
            str(dist),
            "--project-name",
            project,
            "--commit-dirty=true",
        ]
        logs.append("$ " + " ".join(cmd))
        dep = subprocess.run(
            cmd,
            cwd=str(WEB_DIR),
            capture_output=True,
            text=True,
            timeout=900,
            shell=os.name == "nt",
        )
        logs.append(dep.stdout or "")
        logs.append(dep.stderr or "")
        if dep.returncode != 0:
            return {"ok": False, "error": f"wrangler_exit_{dep.returncode}", "log": "\n".join(logs)}
        return {"ok": True, "log": "\n".join(logs), "project": project}
    except FileNotFoundError as exc:
        return {"ok": False, "error": f"missing_tool:{exc}", "log": "\n".join(logs)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "deploy_timeout", "log": "\n".join(logs)}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "log": "\n".join(logs)}
    finally:
        if moved_api and api_bak.exists():
            if api_dir.exists():
                shutil.rmtree(api_dir, ignore_errors=True)
            api_bak.rename(api_dir)
            logs.append("[cf] restored app/api")


def sync_status() -> dict[str, Any]:
    """Compare live catalog fingerprint vs last successful Cloudflare sync."""
    raw = collect_raw_items()
    cleaned, stats = strict_dedupe(raw)
    fp = fingerprint_items(cleaned)
    state = load_sync_state()
    last_fp = state.get("catalog_fingerprint")
    last_urls = set(state.get("urls") or [])
    cur_urls = {
        normalize_url(str(i.get("verified_url") or "")) for i in cleaned if i.get("verified_url")
    }
    new_urls = cur_urls - last_urls
    web = _read_json(WEB_CATALOG)
    data = _read_json(COMMERCIAL)
    web_count = len(web.get("items") or [])
    data_count = len(data.get("items") or [])
    web_fp = fingerprint_items(list(web.get("items") or [])) if web.get("items") else None
    data_fp = fingerprint_items(list(data.get("items") or [])) if data.get("items") else None

    dirty_local = (data_fp != web_fp) or (data_count != web_count)
    needs_cloud = (not state.get("deploy_ok")) or (last_fp != fp)
    # If cleaned preview differs from committed commercial file, also dirty
    if data_fp and data_fp != fp:
        dirty_local = True

    needs_sync = bool(needs_cloud or dirty_local)
    new_items_count = len(new_urls)
    if needs_sync and new_items_count == 0 and data_count != int(state.get("sku_count") or 0):
        new_items_count = abs(data_count - int(state.get("sku_count") or 0))

    # mtime hint
    mtimes = {}
    for label, path in (
        ("commercial", COMMERCIAL),
        ("web", WEB_CATALOG),
        ("vault_index", VAULT_INDEX),
    ):
        if path.exists():
            mtimes[label] = path.stat().st_mtime

    return {
        "needs_sync": needs_sync,
        "new_items_count": int(new_items_count),
        "sku_count": len(cleaned),
        "data_sku_count": data_count,
        "web_sku_count": web_count,
        "fingerprint": fp,
        "last_fingerprint": last_fp,
        "last_synced_at": state.get("synced_at"),
        "deploy_ok": bool(state.get("deploy_ok")),
        "last_error": state.get("last_error"),
        "dirty_local": dirty_local,
        "dedupe_preview": stats,
        "mtimes": mtimes,
        "button_label": (
            "Sync to Cloudflare"
            if needs_sync
            else "Up to Date"
        ),
        "light": False,
    }


def _sku_count_from_head(path: Path) -> int:
    try:
        with path.open("rb") as fh:
            head = fh.read(2048).decode("utf-8", errors="ignore")
        m = re.search(r'"sku_count"\s*:\s*(\d+)', head)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    try:
        data = _read_json(path) or {}
        n = int(data.get("sku_count") or 0)
        return n or len(data.get("items") or [])
    except Exception:
        return 0


def sync_status_light() -> dict[str, Any]:
    """Poll-safe sync hint — no full catalog fingerprint / dual JSON load."""
    state = load_sync_state()
    data_count = _sku_count_from_head(COMMERCIAL)
    web_count = _sku_count_from_head(WEB_CATALOG)
    last_sku = int(state.get("sku_count") or 0)
    dirty_local = data_count != web_count
    needs_cloud = (not state.get("deploy_ok")) or (
        last_sku > 0 and data_count != last_sku
    )
    needs_sync = bool(needs_cloud or dirty_local)
    new_items_count = abs(data_count - last_sku) if needs_sync else 0
    mtimes = {}
    for label, path in (
        ("commercial", COMMERCIAL),
        ("web", WEB_CATALOG),
        ("vault_index", VAULT_INDEX),
    ):
        if path.exists():
            mtimes[label] = path.stat().st_mtime
    return {
        "needs_sync": needs_sync,
        "new_items_count": int(new_items_count),
        "sku_count": data_count,
        "data_sku_count": data_count,
        "web_sku_count": web_count,
        "fingerprint": None,
        "last_fingerprint": state.get("catalog_fingerprint"),
        "last_synced_at": state.get("synced_at"),
        "deploy_ok": bool(state.get("deploy_ok")),
        "last_error": state.get("last_error"),
        "dirty_local": dirty_local,
        "mtimes": mtimes,
        "button_label": (
            "Sync to Cloudflare" if needs_sync else "Up to Date"
        ),
        "light": True,
    }


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Dedupe catalog and sync to Cloudflare Pages")
    p.add_argument("--deploy", action="store_true", help="Also build + wrangler pages deploy")
    p.add_argument("--status", action="store_true", help="Print sync status JSON")
    p.add_argument("--json", action="store_true", help="Print result as JSON")
    args = p.parse_args(argv)

    if args.status:
        print(json.dumps(sync_status(), ensure_ascii=False, indent=2))
        return 0

    result = sync_catalog(deploy=args.deploy)
    if args.json or args.deploy:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"OK sku={result['sku_count']} removed={result['dedupe'].get('removed')} "
            f"→ {WEB_CATALOG}"
        )
    return 0 if result.get("ok") and (not args.deploy or result.get("deploy_ok")) else 1


if __name__ == "__main__":
    sys.exit(main())
