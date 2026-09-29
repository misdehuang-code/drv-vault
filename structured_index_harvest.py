"""高密度结构化索引抓取（Upstream Index & Sitemaps）。

废除盲猜路径：只从真实公开索引源提取直链：
- GitHub Releases HTML / Atom / expanded_assets
- 开源固件目录索引（Apache/Nginx Index of）
- sitemap.xml
- archive.org 元数据 API（files 列表，非路径猜测）

产物写入 sources.txt，交 FULFILL 异步 HEAD 验活。
"""

from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse, unquote

import requests

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
DEFAULT_WHITELIST = ROOT / "data" / "急需设备型号白名单.json"
DEFAULT_SOURCES = ROOT / "sources.txt"
DEFAULT_FAILED = ROOT / "failed_sources.txt"
DEFAULT_STATE = ROOT / "data" / "structured_index_state.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36 DrvVaultStructIndex/1.0"
)

DIRECT_EXTS = (
    ".zip",
    ".rar",
    ".7z",
    ".exe",
    ".msi",
    ".cab",
    ".iso",
    ".img",
    ".bin",
    ".inf",
    ".sys",
    ".rom",
    ".fw",
    ".ucode",
)

# 高价值结构化上游：全部是真实索引页 / API，禁止路径拼凑
UPSTREAM_INDEXES: list[dict[str, Any]] = [
    # ——— GitHub Releases 动态列表（HTML + expanded_assets）———
    {
        "kind": "github_releases",
        "repo": "WildFlash1st/cmp90hx-unlock-for-windows",
        "label": "CMP90HX Unlock",
        "category": "显卡",
        "model": "NVIDIA CMP",
        "keywords": ("cmp", "90hx", "10de"),
        "max_assets": 16,
    },
    {
        "kind": "github_releases",
        "repo": "PZH1gdmu/CMP40HX-Unlock",
        "label": "CMP40HX Unlock",
        "category": "显卡",
        "model": "NVIDIA CMP",
        "keywords": ("cmp", "40hx"),
        "max_assets": 12,
    },
    {
        "kind": "github_releases",
        "repo": "pearlfortune/cmpunlocker",
        "label": "cmpunlocker",
        "category": "显卡",
        "model": "NVIDIA CMP",
        "keywords": ("cmp", "unlock"),
        "max_assets": 12,
    },
    {
        "kind": "github_releases",
        "repo": "chunyouwei/Realtek-PCIe-GBE-Family-Controller",
        "label": "Realtek PCIe GBE",
        "category": "网卡",
        "model": "RTL8168",
        "keywords": ("rtl", "8168", "8111", "realtek"),
        "max_assets": 16,
    },
    {
        "kind": "github_releases",
        "repo": "worproject/Rockchip-Windows-Drivers",
        "label": "Rockchip Windows Drivers",
        "category": "SoC",
        "model": "RK3588",
        "keywords": ("rk3588", "rockchip"),
        "max_assets": 12,
    },
    {
        "kind": "github_releases",
        "repo": "little-did-I-know/SCPI-Instrument-Control",
        "label": "Siglent SCPI GUI",
        "category": "仪器",
        "model": "SPD3303X",
        "keywords": ("siglent", "spd3303", "scpi"),
        "max_assets": 12,
    },
    {
        "kind": "github_releases",
        "repo": "funneld/FX2-Firmware",
        "label": "FX2 Firmware",
        "category": "USB",
        "model": "USB\\VID_04B4&PID_8613",
        "keywords": ("fx2", "04b4", "cypress"),
        "max_assets": 8,
    },
    # ——— Atom 发布流（结构化 XML，非盲猜）———
    {
        "kind": "github_releases",
        "repo": "nirinias/CMP-Unlocker",
        "label": "CMP Unlocker alt",
        "category": "显卡",
        "model": "NVIDIA CMP",
        "keywords": ("cmp", "unlock"),
        "max_assets": 10,
    },
    {
        "kind": "github_releases",
        "repo": "geerlingguy/raspberry-pi-pcie-devices",
        "label": "RPi PCIe devices",
        "category": "工控",
        "model": "RK3588",
        "keywords": ("pcie", "rockchip"),
        "max_assets": 8,
    },
    {
        "kind": "github_atom",
        "url": "https://github.com/PZH1gdmu/CMP40HX-Unlock/releases.atom",
        "repo": "PZH1gdmu/CMP40HX-Unlock",
        "label": "CMP40 Atom",
        "category": "显卡",
        "model": "NVIDIA CMP",
        "max_assets": 10,
    },
    {
        "kind": "github_atom",
        "url": "https://github.com/chunyouwei/Realtek-PCIe-GBE-Family-Controller/releases.atom",
        "repo": "chunyouwei/Realtek-PCIe-GBE-Family-Controller",
        "label": "RTL Atom",
        "category": "网卡",
        "model": "RTL8168",
        "max_assets": 10,
    },
    {
        "kind": "github_atom",
        "url": "https://github.com/worproject/Rockchip-Windows-Drivers/releases.atom",
        "repo": "worproject/Rockchip-Windows-Drivers",
        "label": "RK Atom",
        "category": "SoC",
        "model": "RK3588",
        "max_assets": 8,
    },
    # ——— 固件目录索引（Index of）———
    # 本网可达：kernel.org firmware 每轮强制喂最新包（不依赖 GitHub）
    {
        "kind": "dir_index",
        "url": "https://cdn.kernel.org/pub/linux/kernel/firmware/",
        "label": "kernel.org firmware",
        "category": "固件",
        "model": "linux-firmware",
        "max_links": 6,
        "depth": 0,
        "prefer_newest": True,
    },
    # Realtek 官方下载页（结构化入口，落地再抽）
    {
        "kind": "dir_index",
        "url": "https://www.realtek.com/Download?type=download&id=10",
        "label": "Realtek official download",
        "category": "网卡",
        "model": "RTL8168",
        "max_links": 8,
        "depth": 0,
    },
]


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    return s


def _load_url_set(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out: set[str] = set()
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line.split("|", 1)[0].strip())
    return out


def _is_driverish(url: str) -> bool:
    low = (url or "").lower()
    if not low.startswith("http"):
        return False
    path = urlparse(low).path
    name = path.rsplit("/", 1)[-1]
    # 校验和 / 签名 / 源码包
    if name in {"sha256sums", "sha256sum", "md5sums", "checksums"}:
        return False
    if name.endswith((".sig", ".asc", ".txt", ".md", ".json", ".xml")):
        return False
    if "/releases/download/" in low:
        # 排除明显非 Windows 驱动包
        if any(
            k in low
            for k in (
                "linux-x64",
                "linux-aarch64",
                "-linux-",
                ".tar.gz",
                ".deb",
                ".rpm",
                ".sig",
                ".asc",
                "sample_data",
                "rootfs",
                "macos",
                "-darwin-",
                "sha256sums",
            )
        ):
            return False
        return True
    if any(path.endswith(ext) for ext in DIRECT_EXTS):
        return True
    # 固件归档：仅收 linux-firmware-YYYYMMDD 最新形态
    if re.search(r"linux-firmware-\d{8}\.tar\.(?:gz|xz)$", path):
        return True
    return False


def _looks_like_html_listing(text: str) -> bool:
    low = (text or "")[:2000].lower()
    return "index of" in low or "<a href=" in low


def _extract_hrefs(html: str, base: str) -> list[str]:
    out: list[str] = []
    for href in re.findall(r'href=["\']([^"\']+)["\']', html or "", flags=re.I):
        href = href.strip()
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        if href.startswith("?"):
            continue
        full = urljoin(base, href)
        out.append(full)
    return out


def harvest_dir_index(
    session: requests.Session,
    url: str,
    *,
    max_links: int = 40,
    depth: int = 0,
    timeout: float = 10.0,
    prefer_newest: bool = False,
) -> list[str]:
    """解析 Apache/Nginx 目录索引，仅收真实 href。"""
    found: list[str] = []
    seen: set[str] = set()
    queue: list[tuple[str, int]] = [(url, 0)]
    while queue and len(found) < max(max_links * 3, max_links):
        cur, d = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        try:
            resp = session.get(cur, timeout=timeout, allow_redirects=True)
            if resp.status_code != 200:
                continue
            text = resp.text[:600_000]
        except Exception as exc:
            logger.debug("dir_index %s: %s", cur, exc)
            continue
        if not _looks_like_html_listing(text):
            if _is_driverish(cur):
                found.append(cur)
            continue
        for href in _extract_hrefs(text, cur):
            if href.rstrip("/").endswith(".."):
                continue
            low = href.lower()
            if _is_driverish(href):
                if href not in found:
                    found.append(href)
            elif d < depth and low.endswith("/") and urlparse(href).netloc == urlparse(cur).netloc:
                if href not in seen:
                    queue.append((href, d + 1))
    if prefer_newest:
        # 文件名里带日期/版本的排后面（更新），取末尾
        found = sorted(found, key=lambda u: u.lower())
        return found[-max_links:]
    return found[:max_links]


def harvest_sitemap(
    session: requests.Session,
    url: str,
    *,
    max_links: int = 80,
    timeout: float = 12.0,
) -> list[str]:
    """解析 sitemap.xml / sitemap index，提取 loc 中的驱动直链。"""
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True)
        if resp.status_code != 200:
            return []
        raw = resp.content[:2_000_000]
    except Exception as exc:
        logger.debug("sitemap %s: %s", url, exc)
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        # 宽松：正则抽 loc
        text = raw.decode("utf-8", errors="ignore")
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text, flags=re.I)
        return [u.strip() for u in locs if _is_driverish(u.strip())][:max_links]

    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"
    locs: list[str] = []
    # sitemap index → 递归一层
    for sm in root.findall(f"{ns}sitemap")[:8]:
        loc_el = sm.find(f"{ns}loc")
        if loc_el is not None and (loc_el.text or "").strip():
            child = (loc_el.text or "").strip()
            locs.extend(
                harvest_sitemap(session, child, max_links=max_links // 2, timeout=timeout)
            )
            if len(locs) >= max_links:
                return locs[:max_links]
    for url_el in root.findall(f"{ns}url"):
        loc_el = url_el.find(f"{ns}loc")
        if loc_el is None:
            continue
        u = (loc_el.text or "").strip()
        if _is_driverish(u):
            locs.append(u)
        if len(locs) >= max_links:
            break
    return locs[:max_links]


def harvest_github_releases(
    session: requests.Session,
    repo: str,
    *,
    max_assets: int = 16,
) -> list[str]:
    """GitHub Releases 页 → expanded_assets 真包（复用 github_auth）。"""
    del session
    try:
        from github_auth import html_allowed, html_release_assets
    except Exception:
        return []
    if not html_allowed():
        return []
    s = _session()
    try:
        urls = html_release_assets(s, repo, max_assets=max_assets)
        return [u for u in urls if _is_driverish(u)]
    finally:
        s.close()


def harvest_github_atom(
    session: requests.Session,
    atom_url: str,
    repo: str,
    *,
    max_assets: int = 10,
    timeout: float = 10.0,
) -> list[str]:
    """解析 releases.atom，再对每个 tag 拉 expanded_assets 真链。"""
    try:
        resp = session.get(atom_url, timeout=timeout, allow_redirects=True)
        if resp.status_code != 200:
            return []
        text = resp.text[:400_000]
    except Exception as exc:
        logger.debug("atom %s: %s", atom_url, exc)
        return []
    # Atom entry link → .../releases/tag/TAG
    tags: list[str] = []
    for m in re.finditer(
        rf"github\.com/{re.escape(repo)}/releases/tag/([^\"'/\s<]+)",
        text,
        flags=re.I,
    ):
        tag = unquote(m.group(1))
        if tag not in tags:
            tags.append(tag)
    out: list[str] = []
    seen: set[str] = set()
    owner, name = repo.split("/", 1)
    for tag in tags[:4]:
        exp = f"https://github.com/{owner}/{name}/releases/expanded_assets/{tag}"
        try:
            r2 = session.get(exp, timeout=timeout, allow_redirects=True)
            if r2.status_code != 200:
                continue
            body = r2.text
        except Exception:
            continue
        for href in re.findall(
            r'href=["\']([^"\']*/releases/download/[^"\']+)["\']',
            body,
            flags=re.I,
        ):
            url = href.strip()
            if url.startswith("/"):
                url = "https://github.com" + url
            if not _is_driverish(url) or url in seen:
                continue
            seen.add(url)
            out.append(url)
            if len(out) >= max_assets:
                return out
    return out


def harvest_archive_metadata(
    session: requests.Session,
    identifier: str,
    *,
    max_files: int = 20,
    timeout: float = 12.0,
) -> list[str]:
    """archive.org metadata API → files[].name 真实文件列表。"""
    api = f"https://archive.org/metadata/{identifier}"
    try:
        resp = session.get(api, timeout=timeout, allow_redirects=True)
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception as exc:
        logger.debug("archive metadata %s: %s", identifier, exc)
        return []
    files = data.get("files") or []
    out: list[str] = []
    for f in files:
        name = str((f or {}).get("name") or "").strip()
        if not name:
            continue
        # 跳过元数据/衍生
        if name.endswith((".xml", ".sqlite", ".gz", ".torrent", "_meta.xml")):
            # .gz 可能是固件包；仅当扩展名在白名单且非 meta
            if not any(name.lower().endswith(ext) for ext in DIRECT_EXTS):
                if not re.search(r"linux-firmware-[\d.]+\.tar\.(?:gz|xz)$", name, re.I):
                    continue
        url = f"https://archive.org/download/{identifier}/{name}"
        if _is_driverish(url) or any(name.lower().endswith(ext) for ext in DIRECT_EXTS):
            out.append(url)
        if len(out) >= max_files:
            break
    return out[:max_files]


def _repos_from_whitelist_and_sources(
    whitelist_path: Path, sources_path: Path, *, limit: int = 12
) -> list[tuple[str, str, str]]:
    """从白名单帖子线索 / sources PAGE_CLUE 抽真实 GitHub 仓库再挖 Releases。"""
    from github_miner import extract_repos_from_urls

    urls: list[str] = []
    if whitelist_path.exists():
        try:
            data = json.loads(whitelist_path.read_text(encoding="utf-8-sig"))
            for row in data.get("items") or []:
                for key in ("source_url", "reference_url", "url", "post_url"):
                    u = str(row.get(key) or "").strip()
                    if u.startswith("http"):
                        urls.append(u)
                for u in row.get("urls") or []:
                    if str(u).startswith("http"):
                        urls.append(str(u))
        except Exception:
            pass
    if sources_path.exists():
        for raw in sources_path.read_text(encoding="utf-8-sig").splitlines()[:5000]:
            line = raw.strip()
            if "github.com" not in line.lower():
                continue
            # PAGE_CLUE|url|... 或纯 url
            if line.startswith("PAGE_CLUE|"):
                parts = line.split("|")
                if len(parts) > 1 and parts[1].startswith("http"):
                    urls.append(parts[1])
            elif line.startswith("http"):
                urls.append(line.split("|", 1)[0])
    repos = extract_repos_from_urls(urls)
    out: list[tuple[str, str, str]] = []
    for repo in repos[:limit]:
        out.append((repo, "工控设备", repo.split("/")[-1][:40]))
    return out


def _match_whitelist_model(
    url: str, keywords: tuple[str, ...] | list[str], fallback_model: str, whitelist_models: list[dict[str, Any]]
) -> tuple[str, str, float]:
    blob = f"{url} {' '.join(keywords)} {fallback_model}".lower()
    best = (fallback_model, "工控设备", 12.0)
    for row in whitelist_models:
        model = str(row.get("device_model") or "").strip()
        if not model:
            continue
        cat = str(row.get("device_category") or "工控设备").strip()
        score = float(row.get("value_score") or 0) or 12.0
        key = model.lower()
        if key in blob or any(k and k in key for k in keywords):
            return model, cat, score
        # PCI / 关键字交叉
        for k in keywords:
            if k and k.lower() in key:
                return model, cat, score
    return best


def run_structured_index_harvest(
    *,
    whitelist_path: Path = DEFAULT_WHITELIST,
    sources_path: Path = DEFAULT_SOURCES,
    failed_path: Path = DEFAULT_FAILED,
    max_new_links: int = 200,
    round_no: int = 1,
    should_stop: Optional[Callable[[], bool]] = None,
    indexes: Optional[list[dict[str, Any]]] = None,
    max_seconds: float = 22.0,
) -> dict[str, Any]:
    """主入口：遍历结构化上游 → sources.txt。硬超时，避免空烧半分钟。"""
    stop = should_stop or (lambda: False)
    session = _session()
    failed = _load_url_set(failed_path)
    existing = _load_url_set(sources_path)
    whitelist_models: list[dict[str, Any]] = []
    if whitelist_path.exists():
        try:
            whitelist_models = list(
                (json.loads(whitelist_path.read_text(encoding="utf-8-sig")).get("items") or [])
            )
        except Exception:
            whitelist_models = []

    catalog = list(indexes or UPSTREAM_INDEXES)
    if catalog:
        offset = ((max(1, round_no) - 1) * 3) % len(catalog)
        catalog = catalog[offset:] + catalog[:offset]

    # GitHub HTML 熔断时仍保留 releases（html_release_assets 会走镜像）
    html_ok = True
    try:
        from github_auth import html_allowed, reset_html_circuit

        reset_html_circuit()
        html_ok = bool(html_allowed())
    except Exception:
        html_ok = True
    # 不再因熔断清空 github 源

    scraped = 0
    pending: list[tuple[str, str, str, str, str, float]] = []
    by_source: dict[str, int] = {}
    errors: list[str] = []
    budget = max(8.0, float(max_seconds))

    def accept(url: str, *, model: str, category: str, label: str, keywords: tuple = ()) -> None:
        nonlocal scraped
        if stop() or len(pending) >= max_new_links:
            return
        url = (url or "").strip()
        if not url or url in existing or url in failed:
            return
        if not _is_driverish(url):
            try:
                from link_curator import is_direct_file_url

                if not is_direct_file_url(url):
                    return
            except Exception:
                return
        m, cat, score = _match_whitelist_model(url, keywords, model, whitelist_models)
        pending.append(
            (
                url,
                cat or category,
                m,
                "通用",
                f"structured-index:{label}",
                score,
            )
        )
        existing.add(url)
        scraped += 1
        by_source[label] = by_source.get(label, 0) + 1

    t0 = time.time()
    for src in catalog:
        if stop() or len(pending) >= max_new_links:
            break
        if time.time() - t0 >= budget:
            errors.append("budget_exhausted")
            break
        kind = str(src.get("kind") or "")
        label = str(src.get("label") or kind)
        model = str(src.get("model") or "unknown")
        category = str(src.get("category") or "工控设备")
        keywords = tuple(src.get("keywords") or ())
        try:
            urls: list[str] = []
            if kind == "github_releases":
                urls = harvest_github_releases(
                    session,
                    str(src["repo"]),
                    max_assets=min(8, int(src.get("max_assets") or 8)),
                )
            elif kind == "github_atom":
                urls = harvest_github_atom(
                    session,
                    str(src["url"]),
                    str(src.get("repo") or ""),
                    max_assets=min(6, int(src.get("max_assets") or 6)),
                    timeout=5.0,
                )
            elif kind == "dir_index":
                urls = harvest_dir_index(
                    session,
                    str(src["url"]),
                    max_links=int(src.get("max_links") or 40),
                    depth=int(src.get("depth") or 0),
                    prefer_newest=bool(src.get("prefer_newest")),
                    timeout=6.0,
                )
            elif kind == "sitemap":
                urls = harvest_sitemap(
                    session,
                    str(src["url"]),
                    max_links=int(src.get("max_links") or 80),
                )
            elif kind == "archive_metadata":
                urls = harvest_archive_metadata(
                    session,
                    str(src["identifier"]),
                    max_files=int(src.get("max_files") or 16),
                )
            else:
                continue
            for u in urls:
                accept(u, model=model, category=category, label=label, keywords=keywords)
        except Exception as exc:
            errors.append(f"{label}:{exc}")
            logger.debug("index %s failed: %s", label, exc)

    # 额外线索仓库：预算不足则跳过
    if not stop() and len(pending) < max_new_links and time.time() - t0 < budget * 0.85:
        for repo, cat, model in _repos_from_whitelist_and_sources(
            whitelist_path, sources_path, limit=8
        ):
            if stop() or len(pending) >= max_new_links:
                break
            # 跳过已在固定目录里的仓库
            if any(
                str(s.get("repo") or "").lower() == repo.lower()
                for s in UPSTREAM_INDEXES
                if s.get("repo")
            ):
                continue
            try:
                urls = harvest_github_releases(session, repo, max_assets=8)
                for u in urls:
                    accept(
                        u,
                        model=model,
                        category=cat,
                        label=f"clue:{repo}",
                        keywords=(),
                    )
            except Exception as exc:
                errors.append(f"clue:{repo}:{exc}")

    added = 0
    if pending:
        from seeker_agent import append_sources

        # 改注释头，标明结构化来源
        added = append_sources(sources_path, pending)

    elapsed = round(time.time() - t0, 1)
    print(
        f"[结构化索引] sources={len(catalog)} scraped={scraped} "
        f"added={added} elapsed={elapsed}s",
        flush=True,
    )
    if by_source:
        top = sorted(by_source.items(), key=lambda x: -x[1])[:6]
        print(
            "[结构化索引] 命中源: "
            + ", ".join(f"{k}={v}" for k, v in top),
            flush=True,
        )

    try:
        DEFAULT_STATE.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_STATE.write_text(
            json.dumps(
                {
                    "round": round_no,
                    "scraped": scraped,
                    "added": added,
                    "by_source": by_source,
                    "at": datetime.now().isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass

    return {
        "scraped": scraped,
        "added": added,
        "pending": len(pending),
        "sources_scanned": len(catalog),
        "by_source": by_source,
        "errors": errors[:10],
        "elapsed_sec": elapsed,
        "mode": "structured_index",
        "generated_at": datetime.now().isoformat(),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    stats = run_structured_index_harvest(max_new_links=60, round_no=1)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
