"""Hard guard: Link Curation is Find & Verify only — never download packages to disk."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
FORBIDDEN_DOWNLOAD_ROOTS = (
    ROOT / "data" / "vault",
    ROOT / "vault",
    ROOT / "downloads",
    ROOT / "packages",
)


def purge_forbidden_download_dirs() -> dict[str, int]:
    """Delete any legacy package download directories if they reappear."""
    removed = 0
    for path in FORBIDDEN_DOWNLOAD_ROOTS:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
            logger.warning("Purged forbidden download dir: %s", path)
    return {"purged_dirs": removed}


def assert_link_only_pipeline() -> None:
    """Call at pipeline start — ensure no package dump directories exist."""
    purge_forbidden_download_dirs()


def catalog_sku_row(
    *,
    model: str,
    category: str,
    description: str,
    original_direct_url: str,
    content_length: int | None = None,
    file_name: str = "",
    slug: str = "",
    seo_title: str = "",
    pain_points: list | None = None,
    tags: list | None = None,
    price: float | None = None,
    value_score: float | None = None,
    extra: dict | None = None,
) -> dict:
    """Pure navigation SKU — no local paths."""
    url = (original_direct_url or "").strip()
    row = {
        "model": model,
        "exact_model": model,
        "category": category,
        "hardware_category": category,
        "description": description or "",
        "seo_description": description or "",
        "seo_title": seo_title or f"{model} Legacy Driver Archive",
        "original_direct_url": url,
        "verified_url": url,  # alias for older readers
        "source_url": url,
        "content_length": content_length,  # HTTP Content-Length only
        "file_name": file_name or "",
        "slug": slug,
        "pain_points": pain_points or [],
        "tags": tags or [],
        "price": price,
        "currency": "USDT",
        "verified": True,
        "value_score": value_score,
        "local_storage": False,
    }
    if extra:
        for k, v in extra.items():
            if k in ("local_dir", "local_path", "vault_path", "file_path"):
                continue
            if k not in row or row[k] in (None, "", []):
                row[k] = v
    # Never persist local filesystem paths
    row.pop("local_dir", None)
    row.pop("local_path", None)
    row.pop("vault_path", None)
    return row
