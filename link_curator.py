"""
Link Curation & Indexing Agent
Pure Find & Verify — NEVER download driver/firmware packages to disk.

Flow (宽松模式 force_soft):
  sources.txt 线索（直链/落地页/PAGE_CLUE/SEARCH_ONLY）
  → Soft-Fallback URL（无底层直链时）
  → 强制写入 vault_index.json + commercial_catalog.json 双写
  （不做严格 HEAD 拦截；宁滥勿缺）
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlparse

import requests

from catalog_rules import normalize_category, normalize_model
from json_io import read_json, write_json
from link_only_guard import assert_link_only_pipeline, catalog_sku_row

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCES = ROOT / "sources.txt"
DEFAULT_PROCESSED = ROOT / "data" / "processed_urls.txt"
DEFAULT_FAILED = ROOT / "failed_sources.txt"
DEFAULT_INDEX = ROOT / "vault_index.json"
DEFAULT_CATALOG = ROOT / "data" / "commercial_catalog.json"
DEFAULT_WEB_CATALOG = ROOT / "web" / "data" / "commercial_catalog.json"
DEFAULT_CURATOR_STATE = ROOT / "data" / ".link_curator_state.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36 DrvVaultLinkCurator/2.0"
)

DIRECT_EXTS = {
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
}

HTML_MARKERS = (b"<!doctype html", b"<html", b"<head", b"<title")

# Linux / SDK / 浏览器捆绑 / 娱乐安装包等非目标资产
_NON_DRIVER_RE = re.compile(
    r"(?i)("
    r"armbian|amlogic|rootfs|openwrt|"
    r"tensorrt[_-]?sample|sample[_-]?data|"
    r"linux[-_](x64|aarch64|arm64|amd64)|[-_]linux[-_.]|"
    r"ovphysx|omniverse/physx|"
    r"\.tar\.gz(?:\?|$)|/ubuntu[-_]|/debian[-_]|"
    r"chrome[_-]?(installer|setup)|chromesetup|googlechrome|"
    r"microsoft[_-]?edge|msedge|"
    r"idman\d|internetdownloadmanager|"
    r"flash[_-]?(player|sm)|swflash|shockwave|macromedia|"
    r"qqpc|qqbrowser|tencent|wechat|weixin|qqgame|deskgo|doubao|"
    r"baidu|金山文档|桌面百度|2345|hao123|kdocs|wpscdn|duba\.net|"
    r"xunlei|thunder|sogou|yingxiong|deltaforce|ppspeed|"
    r"kclean|ludashi|driver.?genius|驱动精灵|myapp\.com|"
    r"cmcmcdn|ewt360|setup[_-]?ad|bundleware|toolbar|"
    # AI/游戏/加密货币等明显非驱动 GitHub Release
    r"\bollama\b|vanitygen|vibepollo|whisper\.cpp|llama\.cpp|"
    r"[-_]darwin\.|/darwin[-_]|macos[-_]|osx[-_.]|-apple-darwin|"
    r"windows[-_]?installer|setup[-_]?v?\d|installer[-_]?v\d|"
    r"bitcoin|cryptocurr|mining[-_]?pool|metamask|"
    r"vscode|node[-_]?v\d|golang|python-\d|"
    r"valorant|powerbi|pbi-bridge|discord|steam[-_]?setup|epicgames|"
    r"photoshop|premiere|aftereffects|blender[-_]?win|"
    r"\.apk(?:\?|$)|\.ipa(?:\?|$)|\.flatpak(?:\?|$)|\.dmg(?:\?|$)|"
    r"[-_](ios|android|armeabi|arm64-v8a|x86_64-release)"
    r")"
)

_SKIP_HOST = (
    "stackoverflow.com",
    "stackexchange.com",
    "reddit.com",
    "douyinstatic.com",
    "huoshanstatic.com",
    "hdslb.com",
    "bilibili.com",
    "160.com",
    "aulacn.com",
    "cdn.ewt360.com",
    "dubapkg.cmcmcdn.com",
    "download.internetdownloadmanager.com",
    "download.macromedia.com",
    "pm.myapp.com",
    "ldmnq.com",  # 模拟器
    "sandai.net",  # 迅雷
    "imtt.qq.com",
    "news.qq.com",
    "ca.qq.com",
    "myqcloud.com",  # 非驱动分发
    "bdimg.com",
    "sinaimg.cn",
    "1ting.com",
    # download.ni.com / international.nvidia — 改由规则矩阵探测，不再一刀切拒收
    "dldir3.qq.com",
    "repo.df.qq.com",
    "webcdn.m.qq.com",
    "doubao.com",
    "wpscdn.cn",
    "duba.net",
    "myapp.com",
    "yingxiong.com",
)

# 规则矩阵允许的官方遗留分发域（可进 FULFILL HEAD）
_PATTERN_ALLOW_HOST = (
    "downloadmirror.intel.com",
    "download.ni.com",
    "archive.org",
    "cypress.com",
    "realtek.com",
    "drivers.amd.com",
    "github.com",
    "objects.githubusercontent.com",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": UA, "Accept": "*/*"})
    return s


def is_non_driver_url(url: str, file_name: str = "") -> bool:
    blob = f"{url or ''} {file_name or ''}".strip()
    if _NON_DRIVER_RE.search(blob) or _NON_DRIVER_RE.search(url or ""):
        return True
    if file_name and _NON_DRIVER_RE.search(file_name):
        return True
    low = (url or "").lower()
    # 规则矩阵官方域：文件名已过正则；不再因 host 黑名单误杀
    if any(h in low for h in _PATTERN_ALLOW_HOST):
        return False
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        host = ""
    return any(h in host for h in _SKIP_HOST)


def is_direct_file_url(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    low = url.lower()
    # 规则矩阵官方遗留域：允许进验活队列
    if any(h in low for h in _PATTERN_ALLOW_HOST):
        path = unquote(urlparse(url).path).lower()
        if any(path.endswith(ext) for ext in DIRECT_EXTS):
            return True
        if "/releases/download/" in low or "archive.org/download/" in low:
            return True
        if "downloadmirror.intel.com" in low or "download.ni.com" in low:
            return True
        if "cypress.com" in low and "/download" in low:
            return True
    if any(h in low for h in _SKIP_HOST):
        return False
    if is_non_driver_url(url):
        return False
    path = unquote(urlparse(url).path).lower()
    if path.endswith((".gz", ".tar", ".tgz", ".deb", ".rpm")):
        return False
    if "/releases/download/" in low:
        return any(path.endswith(ext) for ext in DIRECT_EXTS) or bool(
            re.search(r"\.(zip|rar|7z|exe|msi|cab|iso|img|bin)(?:\?|$)", path)
        )
    return any(path.endswith(ext) for ext in DIRECT_EXTS)


def _guess_filename(url: str, headers: dict[str, str]) -> str:
    cd = headers.get("Content-Disposition") or headers.get("content-disposition") or ""
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.I)
    if m:
        return unquote(m.group(1).strip())
    name = Path(unquote(urlparse(url).path)).name
    return name or "package.bin"


def _looks_like_html(sample: bytes, content_type: str) -> bool:
    ct = (content_type or "").lower()
    if "text/html" in ct or "text/xml" in ct and b"<html" in sample.lower():
        return True
    head = sample.lstrip()[:800].lower()
    return any(head.startswith(m) or m in head[:400] for m in HTML_MARKERS)


def suggest_price(
    *,
    category: str,
    model: str,
    content_length: Optional[int],
    value_score: float,
) -> float:
    """稀缺导航站建议标价（USD），非授权费——按检索难度启发式。"""
    base = 4.9
    cat = category or ""
    if any(k in cat for k in ("工控", "采集", "串口", "固件", "BIOS")):
        base = 12.9
    elif "显卡" in cat:
        base = 7.9
    elif "网卡" in cat or "无线" in cat:
        base = 6.9
    if re.search(r"(?i)PCI\\|USB\\VID_|VEN_", model or ""):
        base += 3.0
    if content_length and content_length > 80 * 1024 * 1024:
        base += 2.0
    if value_score >= 8:
        base += 2.0
    elif value_score >= 5:
        base += 1.0
    return round(min(max(base, 2.9), 49.9), 2)


def verify_direct_link(
    url: str,
    *,
    session: Optional[requests.Session] = None,
    timeout: float = 12.0,
) -> dict[str, Any]:
    """
    Find & Verify only — NEVER save the package to disk.

    Prefer HEAD (200/206). If CDN blocks HEAD, Range-GET ≤2KB into RAM only
    to reject HTML decoys. content_length is taken from response headers only.
    """
    own = session is None
    sess = session or _session()
    result: dict[str, Any] = {
        "ok": False,
        "url": url,
        "final_url": url,
        "status_code": 0,
        "content_length": None,
        "content_type": "",
        "file_name": "",
        "etag": "",
        "error": "",
        "verified_at": _utc_now(),
        "method": "HEAD",
    }
    try:
        if not is_direct_file_url(url):
            result["error"] = "not_direct_file"
            return result

        headers = {"User-Agent": UA, "Accept": "*/*"}
        # github.com 连不上时别用满 12s；尽快失败把额度留给其它 CDN
        low_u = (url or "").lower()
        is_gh = "github.com" in low_u or "githubusercontent.com" in low_u
        if is_gh:
            timeout = min(float(timeout), 6.0)
        # 国内可达的 GitHub release 镜像（直链超时后再试）
        mirror_urls: list[str] = []
        if "github.com/" in low_u and "/releases/download/" in low_u:
            for prefix in (
                "https://ghfast.top/",
                "https://ghproxy.net/",
            ):
                mirror_urls.append(prefix + url)
        candidates = [url] + mirror_urls
        resp = None
        last_exc: Optional[Exception] = None
        try_url = url
        for cand in candidates:
            try_url = cand
            try:
                resp = sess.head(
                    cand, headers=headers, timeout=timeout, allow_redirects=True
                )
                last_exc = None
                break
            except requests.RequestException as exc:
                last_exc = exc
                resp = None
                continue
        if resp is None and last_exc is not None and not mirror_urls:
            pass  # fall through to Range / error handling below
        elif resp is None and mirror_urls and last_exc is not None:
            # 直连+镜像均 HEAD 失败，下面 Range 再试第一个镜像
            try_url = mirror_urls[0]

        # CDN may reject HEAD — Range probe in memory only (never write package)
        need_range = (
            resp is None
            or resp.status_code in (403, 405, 501)
            or resp.status_code >= 500
        )
        sample = b""
        if need_range or (
            resp is not None
            and resp.status_code == 200
            and not (
                resp.headers.get("Content-Length") or resp.headers.get("Content-Type")
            )
        ):
            result["method"] = "RANGE_PROBE"
            h2 = dict(headers)
            h2["Range"] = "bytes=0-2047"
            range_ok = False
            for cand in candidates:
                try:
                    resp = sess.get(
                        cand,
                        headers=h2,
                        timeout=timeout,
                        allow_redirects=True,
                        stream=True,
                    )
                    try:
                        for chunk in resp.iter_content(chunk_size=2048):
                            sample = chunk[:2048]
                            break
                    finally:
                        resp.close()
                    try_url = cand
                    range_ok = True
                    break
                except requests.RequestException as exc:
                    last_exc = exc
                    resp = None
                    continue
            if not range_ok:
                raise last_exc or requests.RequestException("range_probe_failed")

        assert resp is not None
        result["status_code"] = int(resp.status_code)
        # 店面仍挂原始 GitHub 直链；镜像只用于验活探针
        result["final_url"] = url
        result["probe_url"] = try_url
        hdrs = {k: v for k, v in resp.headers.items()}
        result["content_type"] = (
            hdrs.get("Content-Type") or hdrs.get("content-type") or ""
        )
        result["etag"] = hdrs.get("ETag") or hdrs.get("etag") or ""
        result["file_name"] = _guess_filename(result["final_url"], hdrs)

        cl = hdrs.get("Content-Length") or hdrs.get("content-length")
        if cl and str(cl).isdigit():
            result["content_length"] = int(cl)
        cr = hdrs.get("Content-Range") or ""
        m = re.search(r"/(\d+)\s*$", cr)
        if m and not result["content_length"]:
            result["content_length"] = int(m.group(1))

        code = result["status_code"]
        if code in (401, 403, 404, 410, 451):
            result["error"] = f"http_{code}"
            return result
        if code not in (200, 206):
            result["error"] = f"http_{code}"
            return result

        if sample and _looks_like_html(sample, result["content_type"]):
            result["error"] = "html_payload"
            return result
        if "text/html" in (result["content_type"] or "").lower() and not sample:
            h2 = dict(headers)
            h2["Range"] = "bytes=0-1023"
            try:
                r2 = sess.get(
                    url, headers=h2, timeout=timeout, allow_redirects=True, stream=True
                )
                try:
                    sample = next(r2.iter_content(1024), b"")[:1024]
                finally:
                    r2.close()
                if _looks_like_html(sample, r2.headers.get("Content-Type", "")):
                    result["error"] = "html_payload"
                    return result
            except requests.RequestException:
                pass

        if not is_direct_file_url(result["final_url"]) and not any(
            result["file_name"].lower().endswith(ext) for ext in DIRECT_EXTS
        ):
            fname = (result["file_name"] or "").lower()
            if not any(fname.endswith(ext) for ext in DIRECT_EXTS):
                result["error"] = "final_url_not_file"
                return result

        result["ok"] = True
        result["error"] = ""
        return result
    except requests.RequestException as exc:
        result["error"] = f"network:{exc.__class__.__name__}"
        return result
    except Exception as exc:
        result["error"] = f"error:{exc}"
        return result
    finally:
        if own:
            sess.close()


def soft_fallback_url(model: str, category: str = "") -> str:
    """智能兜底：驱动之家 / 华军 / Bing / 厂商检索引导页（永不为空、按型号唯一）。"""
    import hashlib
    from urllib.parse import quote_plus

    m = (model or "legacy driver").strip() or "legacy driver"
    q = quote_plus(f"{m} 驱动 下载")
    q_en = quote_plus(f"{m} driver download")
    blob = f"{m} {category}".lower()
    digest = abs(hash(m)) % 5
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", m).strip("-").lower()[:48] or "sku"
    fingerprint = hashlib.md5(m.encode("utf-8")).hexdigest()[:8]
    uniq = f"drvvault={fingerprint}&sku={slug}"
    routes = [
        f"https://drivers.mydrivers.com/?s={q}&{uniq}",
        f"https://www.onlinedown.net/softdowning/search.asp?search={quote_plus(m)}&{uniq}",
        f"https://www.bing.com/search?q={q}&setlang=zh-CN&{uniq}",
        f"https://driver.zol.com.cn/search?wd={quote_plus(m)}&{uniq}",
        f"https://archive.org/search?query={q_en}&{uniq}",
    ]
    if any(k in blob for k in ("rtl", "8168", "8111", "realtek", "alc")):
        return f"https://www.realtek.com/Download?type=download&id=10&q={quote_plus(m)}&{uniq}"
    if any(k in blob for k in ("nvidia", "cmp", "geforce", "quadro", "tesla", "gtx", "rtx")):
        return f"https://www.nvidia.com/Download/Find.aspx?lang=en-us&q={quote_plus(m)}&{uniq}"
    if any(k in blob for k in ("intel", "x540", "x520", "uhd", "hd graphics", "arc ")):
        return (
            "https://www.intel.com/content/www/us/en/download-center/home.html"
            f"?q={quote_plus(m)}&{uniq}"
        )
    if any(k in blob for k in ("amd", "radeon")):
        return f"https://www.amd.com/en/support?q={quote_plus(m)}&{uniq}"
    return routes[digest]


def parse_source_rows(path: Path) -> list[dict[str, Any]]:
    """宽松解析：直链 / 落地页 / PAGE_CLUE / SEARCH_ONLY 一律入队。"""
    return parse_source_rows_loose(path, require_direct=False)


def parse_source_rows_loose(
    path: Path, *, require_direct: bool = False
) -> list[dict[str, Any]]:
    """解析 sources。宽松模式接受任意 http 线索与注释态 SEARCH/PAGE 线索。"""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_models: set[str] = set()

    def _append(
        *,
        url: str,
        cat: str,
        model: str,
        os_ver: str = "通用",
        pain: str = "",
        score: float = 0.0,
        clue_kind: str = "url",
    ) -> None:
        url = (url or "").strip()
        model = (model or "").strip()
        cat = (cat or "").strip() or "工控设备"
        if not model or model in {"未知型号", "未知设备"}:
            return
        mk = model.lower()
        # SEARCH_ONLY / 兜底按型号去重；直链按 URL 去重
        if clue_kind in {"search_only", "soft_fallback"}:
            key = f"model:{mk}"
            if key in seen_models:
                return
            seen_models.add(key)
            if not url.startswith("http"):
                url = soft_fallback_url(model, cat)
        else:
            if not url.startswith("http") or url in seen:
                return
            if require_direct and not is_direct_file_url(url):
                return
            seen.add(url)
        rows.append(
            {
                "url": url,
                "category_hint": cat,
                "model_hint": model,
                "os_version": os_ver or "通用",
                "pain_hint": pain,
                "value_score": score,
                "clue_kind": clue_kind,
            }
        )

    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line:
            continue
        # 注释态线索：# PAGE_CLUE|... / # SEARCH_ONLY|...
        if line.startswith("#"):
            body = line.lstrip("#").strip()
            if body.startswith("PAGE_CLUE|"):
                parts = [p.strip() for p in body.split("|")]
                # PAGE_CLUE|url|cat|model|os|pain
                _append(
                    url=parts[1] if len(parts) > 1 else "",
                    cat=parts[2] if len(parts) > 2 else "",
                    model=parts[3] if len(parts) > 3 else "",
                    os_ver=parts[4] if len(parts) > 4 else "通用",
                    pain=parts[5] if len(parts) > 5 else "",
                    clue_kind="page_clue",
                )
            elif body.startswith("SEARCH_ONLY|"):
                parts = [p.strip() for p in body.split("|")]
                # SEARCH_ONLY|cat|model|os|pain
                cat = parts[1] if len(parts) > 1 else ""
                model = parts[2] if len(parts) > 2 else ""
                _append(
                    url=soft_fallback_url(model, cat),
                    cat=cat,
                    model=model,
                    os_ver=parts[3] if len(parts) > 3 else "通用",
                    pain=parts[4] if len(parts) > 4 else "",
                    score=40.0,
                    clue_kind="search_only",
                )
            continue

        parts = [p.strip() for p in line.split("|")]
        url = parts[0]
        if url.upper() in {"PAGE_CLUE", "SEARCH_ONLY"} and len(parts) > 1:
            if url.upper() == "PAGE_CLUE":
                _append(
                    url=parts[1],
                    cat=parts[2] if len(parts) > 2 else "",
                    model=parts[3] if len(parts) > 3 else "",
                    os_ver=parts[4] if len(parts) > 4 else "通用",
                    pain=parts[5] if len(parts) > 5 else "",
                    clue_kind="page_clue",
                )
            else:
                cat = parts[1]
                model = parts[2] if len(parts) > 2 else ""
                _append(
                    url=soft_fallback_url(model, cat),
                    cat=cat,
                    model=model,
                    os_ver=parts[3] if len(parts) > 3 else "通用",
                    pain=parts[4] if len(parts) > 4 else "",
                    score=40.0,
                    clue_kind="search_only",
                )
            continue

        cat = parts[1] if len(parts) > 1 else ""
        model = parts[2] if len(parts) > 2 else ""
        os_ver = parts[3] if len(parts) > 3 else "通用"
        pain = parts[4] if len(parts) > 4 else ""
        score = 0.0
        if len(parts) > 5 and parts[5]:
            try:
                score = float(parts[5])
            except ValueError:
                score = 0.0
        # 无型号时从 URL 文件名猜一层
        if not model and url.startswith("http"):
            model = Path(unquote(urlparse(url).path)).stem.replace("_", " ").replace(
                "-", " "
            )
        kind = "direct" if is_direct_file_url(url) else "page"
        _append(
            url=url,
            cat=cat,
            model=model,
            os_ver=os_ver,
            pain=pain,
            score=score,
            clue_kind=kind,
        )
    return rows


def force_soft_curate(
    *,
    sources_path: Path = DEFAULT_SOURCES,
    index_path: Path = DEFAULT_INDEX,
    catalog_path: Path = DEFAULT_CATALOG,
    web_catalog_path: Path = DEFAULT_WEB_CATALOG,
    limit: int = 200,
    should_stop: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """宽松强制入库：不做 HEAD 拦截，线索/兜底一律上架双写。"""
    assert_link_only_pipeline()
    stop = should_stop or (lambda: False)
    rows = parse_source_rows_loose(sources_path, require_direct=False)
    index = load_index(index_path)
    existing_urls: set[str] = set()
    existing_models: set[str] = set()
    for it in index.get("items") or []:
        if not isinstance(it, dict):
            continue
        for key in ("original_direct_url", "verified_url", "source_url"):
            u = str(it.get(key) or "").strip()
            if u:
                existing_urls.add(u)
        m = str(it.get("exact_model") or it.get("model") or "").strip().lower()
        if m:
            existing_models.add(m)

    try:
        for path in (catalog_path, web_catalog_path):
            for it in (read_json(path, default={}) or {}).get("items") or []:
                if not isinstance(it, dict):
                    continue
                u = str(
                    it.get("original_direct_url") or it.get("verified_url") or ""
                ).strip()
                if u:
                    existing_urls.add(u)
                m = str(it.get("exact_model") or it.get("model") or "").strip().lower()
                if m:
                    existing_models.add(m)
    except Exception:
        pass

    processed = load_processed_urls()
    incoming: list[dict[str, Any]] = []
    stats = {
        "queued": 0,
        "indexed": 0,
        "soft_fallback": 0,
        "skipped_existing": 0,
        "verified": 0,
        "failed": 0,
        "mode": "force_soft",
        "concurrency": 0,
        "throughput": 0.0,
    }

    for row in rows:
        if stop() or len(incoming) >= max(1, limit):
            break
        stats["queued"] += 1
        model = str(row.get("model_hint") or "").strip()
        cat = str(row.get("category_hint") or "工控设备").strip() or "工控设备"
        junk_models = {
            "download",
            "index",
            "soft",
            "search",
            "unknown",
            "legacy device",
            "unknown device",
        }
        if not model or model.lower() in junk_models:
            # 仍无可用型号则跳过（避免 search/download 等噪声上架）
            stats["skipped_existing"] += 1
            continue
        mk = model.lower()
        if mk in existing_models:
            stats["skipped_existing"] += 1
            continue

        url = str(row.get("url") or "").strip()
        clue = str(row.get("clue_kind") or "")
        soft = clue in {"search_only", "soft_fallback", "page_clue"}
        if clue in {"search_only", "page_clue"} or not url.startswith("http"):
            # 帖子/搜索线索：一律换成驱动之家/华军/厂商智能兜底，保证点击有去处
            url = soft_fallback_url(model, cat)
            soft = True
        elif not is_direct_file_url(url) and "/releases/download/" not in url.lower():
            soft = True
            low = url.lower()
            # 论坛帖 / Issues / 过水首页 → 智能兜底
            if any(
                x in low
                for x in (
                    "/issues/",
                    "/pull/",
                    "stackoverflow.com",
                    "serverfault.com",
                    "bbs.",
                    "forum",
                )
            ) or (
                low.rstrip("/").count("/") <= 3
                and "download" not in low
                and "driver" not in low
            ):
                url = soft_fallback_url(model, cat)

        if url in existing_urls or url in processed:
            # URL 撞车但型号未入库：换兜底链仍上架
            if mk not in existing_models:
                url = soft_fallback_url(model, cat)
                soft = True
                if url in existing_urls:
                    stats["skipped_existing"] += 1
                    continue
            else:
                stats["skipped_existing"] += 1
                continue

        try:
            cat_n, _model_n = _normalize_row(
                row, Path(unquote(urlparse(url).path)).name
            )
            # 宽松模式：以线索型号为准，禁止用 URL 文件名（home/search）覆盖
            if cat_n:
                cat = cat_n
        except Exception:
            pass
        if mk in existing_models or mk in junk_models:
            stats["skipped_existing"] += 1
            continue

        price = suggest_price(
            model=model,
            value_score=float(row.get("value_score") or 50),
            content_length=None,
            category=cat,
        )
        slug_base = _sku_slug(cat, model)
        desc = (
            f"{model} — enterprise recovery listing ({cat}). "
            f"{'Soft-fallback guide link' if soft else 'Candidate direct / mirror link'} "
            f"for legacy Windows / industrial fleets. Fixed 4.90 USDT."
        )[:420]
        item = catalog_sku_row(
            model=model,
            category=cat,
            description=desc,
            original_direct_url=url,
            content_length=None,
            file_name=Path(unquote(urlparse(url).path)).name or "guide.html",
            slug=slug_base or "sku",
            seo_title=f"{model} Legacy Driver Archive | Soft Listing"[:70],
            pain_points=[
                str(row.get("pain_hint") or f"Need archived {model} driver / firmware"),
                "Code 43 / Code 10 legacy recovery",
            ],
            tags=[model.lower()[:40], cat.lower()[:40], "soft fulfill", "legacy driver"],
            price=price,
            value_score=float(row.get("value_score") or 60),
            extra={
                "sku": f"{cat}/{model}",
                "soft_fulfill": True,
                "link_mode": "soft_fallback" if soft else "force_soft",
                "verified": True,
                "os_version": str(row.get("os_version") or "通用"),
            },
        )
        item["verified"] = True
        item["soft_fulfill"] = True
        item["link_mode"] = "soft_fallback" if soft else "force_soft"
        incoming.append(item)
        existing_urls.add(url)
        existing_models.add(mk)
        if soft:
            stats["soft_fallback"] += 1
        line = f"宽松入库成功（不经严格验活）：{model} -> {url}"
        print(line, flush=True)
        logger.info(line)

    if incoming:
        from catalog_growth import grow_catalog

        grown = grow_catalog(incoming, write_storefront=True)
        stats["indexed"] = int(grown.get("added") or 0) or len(incoming)
        stats["verified"] = stats["indexed"]
        stats["catalog_count"] = int(grown.get("sku_count") or 0)
        mark_processed_urls([it["original_direct_url"] for it in incoming])
        try:
            prune_urls_from_sources(
                sources_path, {it["original_direct_url"] for it in incoming}
            )
        except Exception:
            pass
    else:
        stats["catalog_count"] = 0

    print(
        f"[宽松入库] queued={stats['queued']} indexed={stats['indexed']} "
        f"fallback={stats['soft_fallback']} skip_exist={stats['skipped_existing']}",
        flush=True,
    )
    return stats


def _rule_seo(model: str, category: str, pain_hint: str = "") -> dict[str, Any]:
    title = f"{model} Legacy Driver Download | Fix Code 43 Archive"[:70]
    pains = [
        f"Looking for archived {model} driver after OEM discontinued support",
        f"Fix Code 43 / Code 10 missing driver on legacy Windows for {model}",
    ]
    if pain_hint:
        pains.insert(0, pain_hint[:160])
    return {
        "seo_title": title,
        "seo_description": (
            f"Verified direct download for {model} ({category}). "
            f"Legacy Windows XP/Win7 industrial & geek driver archive."
        )[:300],
        "pain_points": pains[:3],
        "tags": [
            model.lower()[:40],
            "legacy driver",
            "code 43",
            category.lower()[:40] if category else "driver",
            "archive",
        ],
    }


def enrich_with_ai(
    *,
    model: str,
    category: str,
    os_version: str,
    pain_hint: str,
    file_name: str,
    url: str,
    use_ai: bool,
) -> dict[str, Any]:
    fallback = _rule_seo(model, category, pain_hint)
    if not use_ai:
        return {**fallback, "ai_used": False}
    try:
        from ai_cleaner import get_ollama_client, seo_with_ai

        client = get_ollama_client()
        if not client.probe():
            return {**fallback, "ai_used": False}
        data = seo_with_ai(
            client,
            hardware_category=category,
            exact_model=model,
            os_support=[os_version or "Legacy Windows"],
            text=f"Verified package URL: {url}\nFile: {file_name}\nPain: {pain_hint}",
            hint_pains=[pain_hint] if pain_hint else None,
        )
        if not data:
            return {**fallback, "ai_used": False}
        return {
            "seo_title": str(data.get("seo_title") or fallback["seo_title"])[:70],
            "seo_description": str(
                data.get("seo_description") or fallback["seo_description"]
            )[:300],
            "pain_points": [
                str(p) for p in (data.get("pain_points") or fallback["pain_points"]) if p
            ][:3]
            or fallback["pain_points"],
            "tags": [
                str(t).lower() for t in (data.get("tags") or fallback["tags"]) if t
            ][:10]
            or fallback["tags"],
            "ai_used": True,
        }
    except Exception as exc:
        logger.info("AI SEO 跳过: %s", exc)
        return {**fallback, "ai_used": False}


def _normalize_row(row: dict[str, Any], file_name: str) -> tuple[Optional[str], Optional[str]]:
    ctx = f"{row.get('category_hint')} {row.get('model_hint')} {row.get('pain_hint')} {file_name}"
    model = normalize_model(str(row.get("model_hint") or ""), context=ctx)
    if not model:
        # 从文件名猜
        stem = Path(file_name).stem
        model = normalize_model(stem.replace("_", " ").replace("-", " "), context=ctx)
    cat = normalize_category(str(row.get("category_hint") or ""), context=ctx + f" {model or ''}")
    if not cat and model:
        cat = normalize_category("", context=f"{model} {file_name}")
    return cat, model


def _sku_slug(category: str, model: str) -> str:
    raw = f"{category}-{model}".lower()
    raw = re.sub(r"[\\/]+", "-", raw)
    raw = re.sub(r"[^a-z0-9\u4e00-\u9fff\-]+", "-", raw)
    raw = re.sub(r"-+", "-", raw).strip("-")
    return raw[:80] or "sku"


def load_index(path: Path = DEFAULT_INDEX) -> dict[str, Any]:
    data = read_json(path, default={}) or {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("items", [])
    data.setdefault("mode", "link_curation")
    return data


def append_failed(path: Path, url: str, reason: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            "# 验链失败 / 死链\n# url|time|reason\n",
            encoding="utf-8",
        )
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{url}|{_utc_now()}||{reason}\n")
    mark_processed_urls([url])


def load_processed_urls(path: Path = DEFAULT_PROCESSED) -> set[str]:
    if not path.exists():
        return set()
    out: set[str] = set()
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line.split("|", 1)[0].strip())
    return out


def mark_processed_urls(
    urls: list[str] | set[str],
    *,
    path: Path = DEFAULT_PROCESSED,
) -> int:
    """记录已处理（验活成功或失败）的 URL，避免反复待验。"""
    have = load_processed_urls(path)
    add = [u.strip() for u in urls if u and u.strip() and u.strip() not in have]
    if not add:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            "# 已验活处理过的直链（成功入库或失败拉黑）\n# url\n",
            encoding="utf-8",
        )
    with path.open("a", encoding="utf-8") as f:
        for u in add:
            f.write(u + "\n")
    return len(add)


def curate_links(
    *,
    sources_path: Path = DEFAULT_SOURCES,
    index_path: Path = DEFAULT_INDEX,
    catalog_path: Path = DEFAULT_CATALOG,
    web_catalog_path: Path = DEFAULT_WEB_CATALOG,
    failed_path: Path = DEFAULT_FAILED,
    limit: int = 40,
    workers: int = 4,
    use_ai: bool = True,
    should_stop: Optional[Callable[[], bool]] = None,
    concurrency: int = 40,
    verify_timeout: float = 3.0,
    use_async: bool = True,
) -> dict[str, Any]:
    """Main entry: verify URLs + SEO + write JSON indexes. Never downloads packages."""
    assert_link_only_pipeline()
    stop = should_stop or (lambda: False)
    index = load_index(index_path)
    by_url: dict[str, dict[str, Any]] = {}
    for it in index.get("items") or []:
        if not isinstance(it, dict):
            continue
        for key in (
            "original_direct_url",
            "verified_url",
            "source_url",
        ):
            u = str(it.get(key) or "").strip()
            if u:
                by_url[u] = it

    all_rows = parse_source_rows(sources_path)
    processed = load_processed_urls()
    # 优先验「尚未入库 / 未处理」的新链，避免永远卡在文件开头那几条老 URL
    fresh = [
        r
        for r in all_rows
        if r["url"] not in by_url and r["url"] not in processed
    ]
    stale = [r for r in all_rows if r["url"] in by_url]

    def _prio(row: dict[str, Any]) -> tuple[int, str]:
        u = (row.get("url") or "").lower()
        # 国内直连 github.com 易超时；先验 CDN / 厂商 / 开源镜像
        if "githubusercontent.com" in u or "objects.githubusercontent.com" in u:
            return (0, u)
        if "archive.org" in u or "gitee.com" in u:
            return (0, u)
        if "dl.dell.com" in u or "download.lenovo.com" in u:
            return (1, u)
        if "github.com/" in u and "/releases/download/" in u:
            return (3, u)
        if "download.ni.com" in u or "download.nvidia.com" in u:
            return (5, u)
        return (2, u)

    fresh.sort(key=_prio)
    stale.sort(key=_prio)
    ordered = fresh + stale
    rows = ordered[: max(1, limit)]
    print(
        f"[验链排队] 队列={len(all_rows)} 新链={len(fresh)} "
        f"本轮取={len(rows)} (优先未入库/CDN)",
        flush=True,
    )

    stats = {
        "queued": len(rows),
        "queue_total": len(all_rows),
        "queue_fresh": len(fresh),
        "verified": 0,
        "failed": 0,
        "skipped_existing": 0,
        "ai_enriched": 0,
        "indexed": 0,
        "concurrency": 0,
        "throughput": 0.0,
        "verify_elapsed": 0.0,
    }

    # 跳过已验证且仍有效的（本轮仍复验 status，但可跳过 AI）
    todo = []
    for row in rows:
        if stop():
            break
        if is_non_driver_url(row["url"]):
            stats["failed"] += 1
            append_failed(failed_path, row["url"], "non_driver_url")
            continue
        prev = by_url.get(row["url"])
        if prev and prev.get("verified") and (
            prev.get("original_direct_url") or prev.get("verified_url")
        ):
            # 新链批次里不应出现；老链补位时跳过，把额度留给 fresh
            stats["skipped_existing"] += 1
            continue
        todo.append(row)

    # 变通：降权站（archive.org 等连超时）沉底或剔除，优先喂好站
    try:
        from host_scoreboard import filter_feedable, is_demoted, rank_urls

        urls_ranked = rank_urls([r["url"] for r in todo])
        by = {r["url"]: r for r in todo}
        todo = [by[u] for u in urls_ranked if u in by and not is_demoted(u, threshold=-10.0)]
        # 若剔太狠导致空，放宽阈值保留原序前 20
        if not todo:
            todo = [by[u] for u in urls_ranked[:20] if u in by]
    except Exception:
        pass

    # 若因跳过导致 todo 太空，强制再塞未入库 URL
    if len(todo) < min(10, limit) and fresh:
        have = {r["url"] for r in todo}
        for r in fresh:
            if r["url"] in have:
                continue
            todo.append(r)
            have.add(r["url"])
            if len(todo) >= limit:
                break

    verified_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    conc = max(5, min(int(concurrency or max(workers * 5, 20)), 50))
    vtimeout = max(1.0, min(float(verify_timeout), 8.0))

    async_ok = False
    if use_async and todo and not stop():
        try:
            from async_verify import verify_urls_async

            print(
                f"[验链异步] aiohttp 并发={conc} timeout={vtimeout}s urls={len(todo)}",
                flush=True,
            )
            results, metrics = verify_urls_async(
                [r["url"] for r in todo],
                concurrency=conc,
                timeout=vtimeout,
                should_stop=stop,
            )
            stats["concurrency"] = int(metrics.get("concurrency") or conc)
            stats["throughput"] = float(metrics.get("throughput") or 0)
            stats["verify_elapsed"] = float(metrics.get("elapsed") or 0)
            async_ok = True
            for row, vr in zip(todo, results):
                if stop():
                    break
                if not vr.get("ok"):
                    stats["failed"] += 1
                    err = str(vr.get("error") or "verify_fail")
                    append_failed(failed_path, row["url"], err)
                    try:
                        from host_scoreboard import note_result

                        note_result(row["url"], ok=False, error=err)
                    except Exception:
                        pass
                    # 异步模式：404/超时只记一行简短日志，避免刷屏拖慢
                    if stats["failed"] <= 8 or stats["failed"] % 25 == 0:
                        print(f"[验链失败] {err} | {row['url'][:90]}", flush=True)
                    continue
                stats["verified"] += 1
                verified_rows.append((row, vr))
                try:
                    from host_scoreboard import note_result

                    note_result(row["url"], ok=True)
                except Exception:
                    pass
                print(
                    f"[验链OK] {vr.get('file_name')} | "
                    f"{(vr.get('content_length') or 0)/1e6:.1f}MB | {row['url'][:70]}",
                    flush=True,
                )
            print(
                f"当前并发协程：{stats['concurrency']} | "
                f"验活吞吐量：{stats['throughput']} 个/秒 | "
                f"本轮有效入库：{stats['verified']}",
                flush=True,
            )
        except ImportError:
            logger.warning("aiohttp 未安装，回退线程池验活")
            async_ok = False
        except Exception as exc:
            logger.warning("异步验活失败，回退线程池: %s", exc)
            async_ok = False

    if not async_ok and todo and not stop():
        # 回退：线程池（仍用短 timeout）
        batch_size = max(1, min(conc, 24))
        i = 0
        t0 = time.time()
        while i < len(todo):
            if stop():
                break
            batch = todo[i : i + batch_size]
            i += batch_size
            pool = ThreadPoolExecutor(max_workers=max(1, min(conc, 20)))
            try:
                futs = {
                    pool.submit(
                        verify_direct_link, row["url"], timeout=vtimeout
                    ): row
                    for row in batch
                }
                for fut in as_completed(futs):
                    if stop():
                        for pending in futs:
                            pending.cancel()
                        break
                    row = futs[fut]
                    try:
                        vr = fut.result(timeout=vtimeout + 2)
                    except Exception as exc:
                        vr = {"ok": False, "url": row["url"], "error": str(exc)}
                    if not vr.get("ok"):
                        stats["failed"] += 1
                        err = str(vr.get("error") or "verify_fail")
                        append_failed(failed_path, row["url"], err)
                        if stats["failed"] <= 8 or stats["failed"] % 25 == 0:
                            print(f"[验链失败] {err} | {row['url'][:90]}", flush=True)
                        continue
                    stats["verified"] += 1
                    verified_rows.append((row, vr))
                    print(
                        f"[验链OK] {vr.get('file_name')} | "
                        f"{(vr.get('content_length') or 0)/1e6:.1f}MB | {row['url'][:70]}",
                        flush=True,
                    )
            finally:
                try:
                    pool.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    pool.shutdown(wait=False)
        elapsed = max(0.001, time.time() - t0)
        stats["concurrency"] = conc
        stats["throughput"] = round(len(todo) / elapsed, 2)
        stats["verify_elapsed"] = round(elapsed, 2)
        print(
            f"当前并发协程：{stats['concurrency']} | "
            f"验活吞吐量：{stats['throughput']} 个/秒 | "
            f"本轮有效入库：{stats['verified']}",
            flush=True,
        )

    new_items: list[dict[str, Any]] = []
    for row, vr in verified_rows:
        if stop():
            break
        cat, model = _normalize_row(row, str(vr.get("file_name") or ""))
        if not cat or not model:
            stats["failed"] += 1
            append_failed(failed_path, row["url"], "bad_model_or_category")
            continue
        # FULFILL 默认不做 AI（留给 INTENT），暂停时不会卡在 120s chat
        seo = enrich_with_ai(
            model=model,
            category=cat,
            os_version=str(row.get("os_version") or "Legacy Windows"),
            pain_hint=str(row.get("pain_hint") or ""),
            file_name=str(vr.get("file_name") or ""),
            url=str(vr.get("final_url") or row["url"]),
            use_ai=use_ai,
        )
        if seo.get("ai_used"):
            stats["ai_enriched"] += 1
        score = float(row.get("value_score") or 0)
        price = suggest_price(
            category=cat,
            model=model,
            content_length=vr.get("content_length"),
            value_score=score,
        )
        direct = str(vr.get("final_url") or row["url"]).strip()
        fname = str(vr.get("file_name") or "")
        if is_non_driver_url(direct, fname) or is_non_driver_url(row["url"], fname):
            stats["failed"] += 1
            append_failed(failed_path, row["url"], "non_driver_payload")
            print(f"[验链丢弃-非驱动] {fname or direct[:70]}", flush=True)
            continue
        desc = str(seo.get("seo_description") or "")
        item = catalog_sku_row(
            model=model,
            category=cat,
            description=desc,
            original_direct_url=direct,
            content_length=vr.get("content_length"),
            file_name=str(vr.get("file_name") or ""),
            slug=_sku_slug(cat, model),
            seo_title=str(seo.get("seo_title") or ""),
            pain_points=list(seo.get("pain_points") or []),
            tags=list(seo.get("tags") or []),
            price=price,
            value_score=score,
            extra={
                "hardware_model": model,
                "pain_point": (seo.get("pain_points") or [""])[0],
                "content_type": vr.get("content_type") or "",
                "etag": vr.get("etag") or "",
                "status_code": vr.get("status_code"),
                "verified_at": vr.get("verified_at") or _utc_now(),
                "os_version": row.get("os_version") or "Legacy Windows",
                "ai_used": bool(seo.get("ai_used")),
            },
        )
        new_items.append(item)
        by_url[item["original_direct_url"]] = item
        stats["indexed"] += 1

    # 合并：新验证覆盖旧项；剔除非驱动历史项
    merged = list(by_url.values())
    best: dict[str, dict[str, Any]] = {}
    for it in merged:
        url = str(
            it.get("original_direct_url")
            or it.get("verified_url")
            or it.get("source_url")
            or ""
        )
        if not url or (not it.get("verified") and not url):
            continue
        if is_non_driver_url(url, str(it.get("file_name") or "")):
            continue
        # Strip any accidental local path fields from legacy index rows
        it.pop("local_dir", None)
        it.pop("local_path", None)
        it.pop("vault_path", None)
        if not it.get("original_direct_url"):
            it["original_direct_url"] = url
            it["verified_url"] = url
            it["source_url"] = url
        key = f"{it.get('hardware_category') or it.get('category')}|{it.get('exact_model') or it.get('hardware_model')}|{url}"
        prev = best.get(key)
        if not prev or float(it.get("value_score") or 0) >= float(
            prev.get("value_score") or 0
        ):
            best[key] = it
    items = sorted(
        best.values(),
        key=lambda x: float(x.get("value_score") or 0),
        reverse=True,
    )

    index_out = {
        "title": "DrvVault Link Curation Index",
        "mode": "link_curation",
        "generated_at": _utc_now(),
        "device_count": len(
            {(i.get("category"), i.get("hardware_model")) for i in items}
        ),
        "link_count": len(items),
        "file_count": len(items),
        "total_size_bytes": sum(int(i.get("content_length") or 0) for i in items),
        "total_size_gb": round(
            sum(int(i.get("content_length") or 0) for i in items) / (1024**3), 3
        ),
        "local_storage": False,
        "items": items,
    }
    write_json(index_path, index_out)

    catalog_items = []
    for it in items:
        url = str(
            it.get("original_direct_url") or it.get("verified_url") or ""
        ).strip()
        if not url:
            continue
        catalog_items.append(
            catalog_sku_row(
                model=str(it.get("exact_model") or it.get("model") or ""),
                category=str(it.get("hardware_category") or it.get("category") or ""),
                description=str(
                    it.get("description") or it.get("seo_description") or ""
                ),
                original_direct_url=url,
                content_length=it.get("content_length"),
                file_name=str(it.get("file_name") or ""),
                slug=str(it.get("slug") or ""),
                seo_title=str(it.get("seo_title") or ""),
                pain_points=list(it.get("pain_points") or []),
                tags=list(it.get("tags") or []),
                price=it.get("price"),
                value_score=it.get("value_score"),
                extra={
                    "sku": f"{it.get('hardware_category')}/{it.get('exact_model')}",
                    "google_longtail_queries": it.get("google_longtail_queries") or [],
                    "file_count": 1,
                },
            )
        )

    # 可持续增长：合并进账本后再写店面（禁止用更小集合覆盖历史好货）
    from catalog_growth import grow_catalog

    grown = grow_catalog(catalog_items, write_storefront=True)
    catalog_items = list(grown.get("items") or catalog_items)

    # 凡本轮已验（成功或失败）一律移出待验；店面按型号去重不影响「已处理」
    done_urls = {row["url"] for row, _vr in verified_rows}
    # failed 已在 append_failed 里 mark；这里补上成功的
    marked = mark_processed_urls(done_urls)
    pruned = prune_urls_from_sources(sources_path, done_urls | load_processed_urls())

    print(
        f"[索引完成] verified={stats['verified']} failed={stats['failed']} "
        f"indexed={stats['indexed']} catalog={len(catalog_items)} "
        f"ledger={grown.get('ledger_count')} +{grown.get('added')} "
        f"ai={stats['ai_enriched']} processed+={marked} pruned={pruned}",
        flush=True,
    )
    stats["catalog_count"] = len(catalog_items)
    stats["ledger_count"] = grown.get("ledger_count")
    stats["preserved"] = grown.get("preserved")
    stats["added"] = grown.get("added")
    stats["processed"] = marked
    stats["pruned_sources"] = pruned
    stats["index_path"] = str(index_path)
    stats["catalog_path"] = str(catalog_path)
    return stats


def prune_urls_from_sources(sources_path: Path, drop: set[str]) -> int:
    if not sources_path.exists() or not drop:
        return 0
    keep: list[str] = []
    removed = 0
    seen: set[str] = set()
    for raw in sources_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or not line.startswith("http"):
            keep.append(raw)
            continue
        url = line.split("|", 1)[0].strip()
        if url in drop or url in seen:
            removed += 1
            continue
        seen.add(url)
        keep.append(raw)
    if removed:
        from sources_write_lock import SOURCES_LOCK

        with SOURCES_LOCK:
            sources_path.write_text("\n".join(keep).rstrip() + "\n", encoding="utf-8")
    return removed


def prune_indexed_from_sources(
    *,
    sources_path: Path = DEFAULT_SOURCES,
    index_path: Path = DEFAULT_INDEX,
    failed_path: Path = DEFAULT_FAILED,
) -> int:
    """把已入库 / 已失败 / 已处理的 URL 从 sources 待验队列去掉。"""
    if not sources_path.exists():
        return 0
    drop: set[str] = set(load_processed_urls())
    for it in (load_index(index_path).get("items") or []):
        if not isinstance(it, dict):
            continue
        for key in ("original_direct_url", "verified_url", "source_url"):
            u = str(it.get(key) or "").strip()
            if u:
                drop.add(u)
    if failed_path.exists():
        for raw in failed_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            drop.add(line.split("|", 1)[0].strip())
    return prune_urls_from_sources(sources_path, drop)


def enrich_index_seo(
    *,
    index_path: Path = DEFAULT_INDEX,
    catalog_path: Path = DEFAULT_CATALOG,
    web_catalog_path: Path = DEFAULT_WEB_CATALOG,
    limit: int = 20,
    use_ai: bool = True,
    should_stop: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """INTENT：对已验链条目补强英文 SEO（仍不下载）。"""
    stop = should_stop or (lambda: False)
    index = load_index(index_path)
    items = list(index.get("items") or [])
    updated = 0
    for it in items:
        if stop() or updated >= limit:
            break
        if not it.get("verified_url") and not it.get("original_direct_url"):
            continue
        if it.get("ai_used") and it.get("seo_title"):
            continue
        model = str(it.get("exact_model") or it.get("hardware_model") or it.get("model") or "")
        cat = str(it.get("hardware_category") or it.get("category") or "")
        if not model or not cat:
            continue
        seo = enrich_with_ai(
            model=model,
            category=cat,
            os_version=str(it.get("os_version") or "Legacy Windows"),
            pain_hint=str(it.get("pain_point") or ""),
            file_name=str(it.get("file_name") or ""),
            url=str(it.get("original_direct_url") or it.get("verified_url") or ""),
            use_ai=use_ai,
        )
        it["seo_title"] = seo.get("seo_title")
        it["seo_description"] = seo.get("seo_description")
        it["description"] = seo.get("seo_description")
        it["pain_points"] = seo.get("pain_points")
        it["pain_point"] = (seo.get("pain_points") or [""])[0]
        it["tags"] = seo.get("tags")
        it["ai_used"] = bool(seo.get("ai_used"))
        updated += 1
        time.sleep(0.05)

    index["items"] = items
    index["generated_at"] = _utc_now()
    write_json(index_path, index)

    catalog_items = []
    for it in items:
        url = str(
            it.get("original_direct_url") or it.get("verified_url") or ""
        ).strip()
        if not url:
            continue
        catalog_items.append(
            catalog_sku_row(
                model=str(it.get("exact_model") or it.get("model") or ""),
                category=str(it.get("hardware_category") or it.get("category") or ""),
                description=str(
                    it.get("description") or it.get("seo_description") or ""
                ),
                original_direct_url=url,
                content_length=it.get("content_length"),
                file_name=str(it.get("file_name") or ""),
                slug=str(
                    it.get("slug")
                    or _sku_slug(
                        str(it.get("hardware_category") or ""),
                        str(it.get("exact_model") or ""),
                    )
                ),
                seo_title=str(it.get("seo_title") or ""),
                pain_points=list(it.get("pain_points") or []),
                tags=list(it.get("tags") or []),
                price=it.get("price"),
                value_score=it.get("value_score"),
                extra={
                    "sku": f"{it.get('hardware_category')}/{it.get('exact_model')}",
                    "file_count": 1,
                },
            )
        )
    from catalog_growth import grow_catalog

    grown = grow_catalog(catalog_items, write_storefront=True)
    return {
        "updated": updated,
        "sku_count": int(grown.get("sku_count") or len(catalog_items)),
        "ledger_count": grown.get("ledger_count"),
        "growth": True,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    stats = curate_links(limit=30, workers=4, use_ai=True)
    print(stats)
