"""
全网挖链 Agent：根据白名单型号自动生成检索词 → 搜索引擎找结果
→ 从结果页抽取驱动/软件直链 → 写入 sources.txt。

目标：数量优先、禁止空转；不点暂停就持续换策略找包。

用法：
  python seeker_agent.py
  python seeker_agent.py --limit 10 --max-links 30
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
DEFAULT_WHITELIST = ROOT / "data" / "急需设备型号白名单.json"
DEFAULT_SOURCES = ROOT / "sources.txt"
DEFAULT_FAILED = ROOT / "failed_sources.txt"
DEFAULT_SEARCH_RULES = ROOT / "data" / "search_rules.txt"

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
    ".gz",
    ".tar",
    ".dmg",
)

# 明显不是驱动包的站（帖子/社交/短视频 CDN）
SKIP_HOST_PARTS = (
    "stackoverflow.com",
    "stackexchange.com",
    "superuser.com",
    "serverfault.com",
    "reddit.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "tiktok.com",
    "zhihu.com",
    "wikipedia.org",
    "microsoft.com/answers",
    "microsoft.com/store",
    "apps.microsoft.com",
    "douyin.com",
    "douyinstatic.com",
    "bytedance.com",
    "bytednsdoc.com",
    "huoshanstatic.com",
    "hdslb.com",
    "bilibili.com",
    "bilivideo.com",
    "iesdouyin.com",
    "snssdk.com",
    "toutiao.com",
    "ixigua.com",
    "baidu.com",
    "baike.baidu.com",
    "csdn.net",
    "jianshu.com",
    "nvidia.cn",
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            # 本网 Bing 中文结果质量更高（华军/驱动之家），英文易被导到 nvidia.cn
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
    )
    return s


def _is_direct_file(url: str) -> bool:
    path = unquote(urlparse(url).path).lower()
    if not any(path.endswith(ext) for ext in DIRECT_EXTS):
        return False
    # GitHub 非 releases/download 的 blob/raw 常是源码碎片，仍可收下
    return True


def _bad_host(url: str) -> bool:
    u = url.lower()
    return any(h in u for h in SKIP_HOST_PARTS)


def _accept_download_url(url: str) -> bool:
    """入队门槛：真实扩展名 + 非垃圾站 + 非 Linux/样例大包。"""
    if not url or not url.startswith("http"):
        return False
    if _bad_host(url):
        return False
    try:
        from link_curator import is_non_driver_url

        if is_non_driver_url(url):
            return False
    except Exception:
        pass
    low = url.lower()
    if any(
        k in low
        for k in (
            "armbian",
            "amlogic",
            "rootfs",
            "tensorrt_sample",
            "sample_data",
            "linux-x64",
            "linux-aarch64",
            "linux-arm64",
            "-linux-",
            ".tar.gz",
            "chrome_installer",
            "idman",
            "xunlei",
            "sogou",
            "qqnews",
        )
    ):
        return False
    # Windows 驱动店面：优先 zip/exe/msi/cab/iso，拒纯 .gz/.tar
    path = unquote(urlparse(url).path).lower()
    if path.endswith((".gz", ".tar", ".tgz", ".deb", ".rpm", ".appimage")):
        return False
    if "/releases/download/" in low:
        return True
    return _is_direct_file(url)


def _load_failed(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line.split("|", 1)[0].strip())
    return out


def _load_existing_sources(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line.split("|", 1)[0].strip())
    return out


def _norm_model_key(model: str) -> str:
    s = (model or "").strip().lower()
    s = s.replace("\\", "_")
    s = re.sub(r"_+", "_", s)
    s = re.sub(r"\s+", " ", s)
    return s


def is_diggable_model(model: str) -> bool:
    """False = 搜索只会空转（SDK/PCI 长 ID/过宽词），挖链应跳过。"""
    m = (model or "").strip()
    if not m or m in {"未知型号", "未知设备", "其他外设"}:
        return False
    ml = m.lower()
    if len(ml) < 4:
        return False
    if ml in {
        "nvidia error",
        "amd card",
        "amd graphics driver",
        "intel gpu",
        "intel graphics",
        "x11",
        "other",
        "windows",
        "driver",
        "nvidia sdk",
        "cuda toolkit",
        "device adapter",
    }:
        return False
    # PCI 硬件 ID / 过宽 SDK·Adapter 词：矿工可单独处理 DEV_，搜索引擎几乎 0 直链
    if "pci\\ven_" in ml or "pci/ven_" in ml or re.search(r"ven_[0-9a-f]{4}", ml):
        return False
    if re.search(r"\b(sdk|toolkit|framework|runtime)\b", ml):
        return False
    if "device adapter" in ml or ml.endswith(" adapter"):
        return False
    # 纯两位字母+数字过短噪声（如 J12）已由 len<4 拦住；再拦明显非硬件词
    if re.fullmatch(r"[a-z]{1,3}", ml):
        return False
    return True


def load_whitelist_targets(
    path: Path,
    *,
    limit: int = 40,
    skip_models: Optional[set[str]] = None,
    prefer_new: bool = True,
    round_no: int = 1,
) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    items = list(data.get("items") or [])
    skip = {_norm_model_key(m) for m in (skip_models or set()) if m}
    try:
        from dead_end_blacklist import sunk_keys

        skip |= sunk_keys(round_no=round_no)
    except Exception:
        pass
    fresh: list[dict[str, Any]] = []
    known: list[dict[str, Any]] = []
    for row in items:
        model = str(row.get("device_model") or "").strip()
        cat = str(row.get("device_category") or "").strip()
        if not is_diggable_model(model):
            continue
        if not cat or cat in {"其他", "其他外设"}:
            continue
        if _norm_model_key(model) in skip:
            known.append(row)
        else:
            fresh.append(row)
    # 优先：带 GitHub 线索的型号（矿工/挖链命中率更高）
    def _has_gh(row: dict[str, Any]) -> bool:
        for u in row.get("sample_urls") or []:
            if isinstance(u, str) and "github.com" in u.lower():
                return True
        return False

    fresh.sort(key=lambda r: (0 if _has_gh(r) else 1, -float(r.get("value_score") or 0)))
    known.sort(key=lambda r: (0 if _has_gh(r) else 1, -float(r.get("value_score") or 0)))
    # 优先新型号时：只挖未入库的；没有新型号返回空，交给流水线跳过
    if prefer_new:
        return fresh[: max(1, limit)] if fresh else []
    ordered = fresh + known
    return ordered[: max(1, limit)]


def cataloged_models() -> set[str]:
    """Already on storefront / ledger — deprioritize in ACQUIRE."""
    models: set[str] = set()
    try:
        from catalog_growth import load_ledger

        for it in load_ledger().get("items") or []:
            m = str(it.get("exact_model") or it.get("model") or "").strip()
            if m:
                models.add(_norm_model_key(m))
    except Exception:
        pass
    try:
        from json_io import read_json

        for path in (
            ROOT / "data" / "commercial_catalog.json",
            ROOT / "web" / "data" / "commercial_catalog.json",
        ):
            for it in (read_json(path, default={}) or {}).get("items") or []:
                if not isinstance(it, dict):
                    continue
                m = str(it.get("exact_model") or it.get("model") or "").strip()
                if m:
                    models.add(_norm_model_key(m))
    except Exception:
        pass
    return models


def models_with_real_direct_links() -> set[str]:
    """仅跳过已有真直链的型号；灌库兜底 URL（archive 检索页）不算到位。"""
    out: set[str] = set()
    try:
        from json_io import read_json

        for path in (
            ROOT / "data" / "commercial_catalog.json",
            ROOT / "data" / "catalog_ledger.json",
        ):
            for it in (read_json(path, default={}) or {}).get("items") or []:
                if not isinstance(it, dict):
                    continue
                if it.get("bulk_injected") or it.get("link_mode") == "archive_fallback":
                    continue
                url = str(
                    it.get("original_direct_url")
                    or it.get("verified_url")
                    or ""
                ).lower()
                if not url.startswith("http"):
                    continue
                if "/releases/download/" in url or any(
                    url.split("?", 1)[0].endswith(ext)
                    for ext in (".zip", ".exe", ".msi", ".7z", ".rar", ".cab", ".img")
                ):
                    m = str(it.get("exact_model") or it.get("model") or "").strip()
                    if m:
                        out.add(_norm_model_key(m))
    except Exception:
        pass
    return out


def build_queries(row: dict[str, Any], *, strategy: int = 0) -> list[str]:
    model = str(row.get("device_model") or "").strip()
    # PCI 长 ID → 只搜 DEV_XXXX，避免整串硬件 ID 污染查询
    m_dev = re.search(r"DEV_([0-9A-F]{4})", model, re.I)
    if m_dev:
        model = f"DEV_{m_dev.group(1)}"
    elif len(model) > 48:
        model = re.sub(r"[^A-Za-z0-9_\- ]+", " ", model)[:40].strip() or model[:40]
    pains = row.get("pain_points") or []
    pain = str(pains[0]) if pains else ""
    os_hint = "XP" if re.search(r"\bxp\b", pain, re.I) else (
        "Win7" if re.search(r"win\s*7|windows\s*7", pain, re.I) else "Windows"
    )
    families = [
        [
            f"{model} 驱动 下载 zip",
            f'"{model}" site:drivers.mydrivers.com OR site:onlinedown.net',
        ],
        [
            f"{model} driver download zip",
            f'"{model}" site:driver.zol.com.cn OR site:xiazai.zol.com.cn',
        ],
        [
            f"{model} 官方驱动 下载",
            f'"{model}" site:realtek.com OR site:drivers.amd.com',
        ],
        [
            f"{model} unlock OR firmware download zip",
            f'"{model}" site:github.com/releases',
        ],
    ]
    return families[strategy % len(families)]


def search_duckduckgo(
    session: requests.Session, query: str, *, max_results: int = 8
) -> Optional[list[str]]:
    """成功返回列表（可空）；网络/HTTP 硬失败返回 None。"""
    url = "https://html.duckduckgo.com/html/"
    try:
        resp = session.post(
            url,
            data={"q": query, "b": ""},
            timeout=12,
        )
        if resp.status_code != 200:
            logger.warning("DDG HTTP %s q=%s", resp.status_code, query)
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        links: list[str] = []
        for a in soup.select("a.result__a"):
            href = a.get("href") or ""
            # DDG 包装链接
            if "uddg=" in href:
                qs = parse_qs(urlparse(href).query)
                href = unquote((qs.get("uddg") or [""])[0])
            if href.startswith("http") and href not in links and not _bad_host(href):
                links.append(href)
            if len(links) >= max_results:
                break
        return links
    except Exception as exc:
        logger.warning("DDG 搜索失败 q=%s: %s", query, exc)
        return None


def search_bing(
    session: requests.Session, query: str, *, max_results: int = 8
) -> Optional[list[str]]:
    """成功返回列表（可空）；网络/HTTP 硬失败返回 None。"""
    url = "https://www.bing.com/search"
    try:
        resp = session.get(
            url,
            params={
                "q": query,
                "count": max_results,
                "setlang": "zh-CN",
                "cc": "CN",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning("Bing HTTP %s q=%s", resp.status_code, query)
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        links: list[str] = []
        for a in soup.select("li.b_algo h2 a"):
            href = a.get("href") or ""
            if href.startswith("http") and href not in links and not _bad_host(href):
                links.append(href)
            if len(links) >= max_results:
                break
        return links
    except Exception as exc:
        logger.warning("Bing 搜索失败 q=%s: %s", query, exc)
        return None


def search_web(
    session: requests.Session,
    query: str,
    *,
    engine: str = "ddg",
    max_results: int = 8,
    _dead: Optional[set[str]] = None,
) -> list[str]:
    """按引擎搜索；仅网络硬失败才拉黑引擎，空结果不拉黑（避免一查无结果整轮 0 hits）。"""
    order = [engine, "bing" if engine == "ddg" else "ddg"]
    # 默认优先 Bing（国内更稳），DDG 仅作备用
    if engine == "ddg":
        order = ["bing", "ddg"]
    dead = _dead if _dead is not None else set()
    for eng in order:
        if eng in dead:
            continue
        links = (
            search_bing(session, query, max_results=max_results)
            if eng == "bing"
            else search_duckduckgo(session, query, max_results=max_results)
        )
        if links is None:
            dead.add(eng)
            continue
        if links:
            return links
    return []


_HREF_FILE_RE = re.compile(
    r"https?://[^\s\"'<>]+?\.(?:zip|rar|7z|exe|msi|cab|iso|img|bin)(?:\?[^\s\"'<>]*)?",
    re.I,
)
_GH_REPO_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([^/]+)/([^/#?]+)",
    re.I,
)


def resolve_github_release_assets(
    session: requests.Session, page_url: str, *, max_assets: int = 10
) -> list[str]:
    """Search hits are often /releases HTML pages — resolve to real assets."""
    m = _GH_REPO_RE.search(page_url or "")
    if not m:
        return []
    owner, repo = m.group(1), m.group(2)
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if repo.lower() in {"issues", "pulls", "actions", "wiki", "topics"}:
        return []
    full = f"{owner}/{repo}"
    try:
        from github_miner import fetch_release_assets

        return fetch_release_assets(session, full, max_assets=max_assets)
    except Exception as exc:
        logger.debug("resolve github assets %s: %s", full, exc)
        return []


def extract_download_links(page_url: str, html: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def add(u: str) -> None:
        u = u.strip()
        if not u.startswith("http"):
            u = urljoin(page_url, u)
        if u in seen or _bad_host(u):
            return
        # GitHub UI 相对路径 /releases/download/...
        if "/releases/download/" in u and not _is_direct_file(u):
            # 无扩展名也先收下，后续 HEAD 验
            pass
        if _is_direct_file(u) or "/releases/download/" in u.lower():
            seen.add(u)
            found.append(u)

    for m in _HREF_FILE_RE.findall(html or ""):
        add(m)
    # GitHub release 页常见相对链
    for m in re.findall(
        r'href=["\']([^"\']*/releases/download/[^"\']+)["\']', html or "", re.I
    ):
        add(m)

    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return found

    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = (a.get_text() or "").lower()
        if _is_direct_file(href) or "/releases/download/" in href.lower() or any(
            k in text for k in ("download", "driver", "firmware", "mirror", "release")
        ):
            add(href)
        elif _is_direct_file(urljoin(page_url, href)):
            add(href)
    return found[:30]


def fetch_page(session: requests.Session, url: str, timeout: float = 20.0) -> str:
    resp = session.get(url, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    # 过大页面截断
    return resp.text[:800_000]


def resolve_cn_portal_downloads(
    session: requests.Session,
    page_url: str,
    html: str,
    *,
    max_links: int = 6,
) -> list[str]:
    """华军/驱动之家等：跟跳转到真实 zip/exe（本网可达方向）。"""
    low = (page_url or "").lower()
    out: list[str] = []
    seen: set[str] = set()
    candidates: list[str] = []

    # 华军 onlinedown：module=download 中间页 → 跟跳到 CDN zip
    if "onlinedown.net" in low:
        for m in re.findall(
            r'https?://www\.onlinedown\.net/[^\"\'\s<>]*module=download[^\"\'\s<>]*',
            html or "",
            flags=re.I,
        ):
            candidates.append(m.replace("&amp;", "&"))
        for m in re.findall(
            r'["\'](/iopdfbhjl/\d+\?[^\"\']*module=download[^\"\']*)["\']',
            html or "",
            flags=re.I,
        ):
            candidates.append(urljoin(page_url, m.replace("&amp;", "&")))

    # 驱动之家 mydrivers：/download/downloads.ashx?id=
    if "mydrivers.com" in low:
        for m in re.findall(
            r'(/download/downloads\.ashx\?id=\d+)',
            html or "",
            flags=re.I,
        ):
            candidates.append(urljoin("https://drivers.mydrivers.com", m))

    # 中关村 ZOL 下载站偶发直链
    if "zol.com.cn" in low:
        for m in re.findall(
            r'https?://[^\"\'\s<>]+\.(?:zip|exe|rar|7z)(?:\?[^\"\'\s<>]*)?',
            html or "",
            flags=re.I,
        ):
            if "zol-img" in m.lower() or "/product_small/" in m.lower():
                continue
            candidates.append(m)

    for cand in candidates:
        if len(out) >= max_links:
            break
        if cand in seen:
            continue
        seen.add(cand)
        try:
            resp = session.get(
                cand,
                timeout=10,
                allow_redirects=True,
                stream=True,
            )
            final = str(resp.url or cand)
            ct = (resp.headers.get("Content-Type") or "").lower()
            cd = (resp.headers.get("Content-Disposition") or "").lower()
            cl = resp.headers.get("Content-Length")
            resp.close()
            ok = False
            if _accept_download_url(final):
                ok = True
            elif "zip" in ct or "octet-stream" in ct or "exe" in ct:
                ok = True
            elif ".zip" in cd or ".exe" in cd or "filename=" in cd:
                ok = True
            # ashx 跟跳后若有体积也先收下验活
            elif "downloads.ashx" in cand and cl and int(cl) > 50_000:
                ok = True
                final = cand
            if ok and final not in out:
                # 过滤捆绑广告包域名
                fl = final.lower()
                if any(
                    b in fl
                    for b in (
                        "myapp.com",
                        "xunlei",
                        "sandai.net",
                        "doubao",
                        "ugapk.cn",
                        "qqpcmgr",
                        "ludashi.com",
                        "cdn-file-ssl.ludashi",
                        "duba.net",
                        "cmcmcdn",
                    )
                ):
                    continue
                out.append(final)
        except Exception as exc:
            logger.debug("cn portal resolve %s: %s", cand[:80], exc)
    return out


def append_sources(
    sources_path: Path,
    entries: list[tuple[str, str, str, str, str, float]],
) -> int:
    """entries: url, category, device, os, pain, score"""
    if not entries:
        return 0
    from sources_write_lock import SOURCES_LOCK

    with SOURCES_LOCK:
        existing = _load_existing_sources(sources_path)
        lines = [
            "",
            f"# ---- 挖链Agent自动导入 {datetime.now().strftime('%Y-%m-%d %H:%M')} ----",
        ]
        n = 0
        for url, cat, device, os_ver, pain, score in entries:
            if url in existing:
                continue
            lines.append(f"{url}|{cat}|{device}|{os_ver}|{pain[:120]}|{score}")
            existing.add(url)
            n += 1
        if n:
            sources_path.parent.mkdir(parents=True, exist_ok=True)
            with sources_path.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        return n


def seek_for_whitelist(
    *,
    whitelist_path: Path = DEFAULT_WHITELIST,
    sources_path: Path = DEFAULT_SOURCES,
    failed_path: Path = DEFAULT_FAILED,
    search_rules_path: Path = DEFAULT_SEARCH_RULES,
    limit_models: int = 15,
    max_new_links: int = 40,
    strategy: int = 0,
    engine: str = "ddg",
    should_stop: Optional[Callable[[], bool]] = None,
    use_ai_queries: bool = True,
) -> dict[str, Any]:
    """
    主入口：挖直链写入 sources.txt。
    strategy / engine 由流水线轮次传入，避免空转同一套查询。
    """
    stop = should_stop or (lambda: False)
    # 灌库兜底不算「已到位」——继续用 Bing/排行榜给它们找真直链
    already_real = models_with_real_direct_links()
    targets = load_whitelist_targets(
        whitelist_path,
        limit=limit_models,
        skip_models=already_real,
        prefer_new=False,
    )
    # 从店面 bulk 型号补 targets（灌库兜底继续搜真直链）
    if len(targets) < limit_models:
        try:
            from json_io import read_json

            cat = read_json(ROOT / "data" / "commercial_catalog.json", default={}) or {}
            seen = {_norm_model_key(str(r.get("device_model") or "")) for r in targets}
            for it in cat.get("items") or []:
                if not isinstance(it, dict):
                    continue
                m = str(it.get("exact_model") or it.get("model") or "").strip()
                key = _norm_model_key(m)
                if not m or key in seen or key in already_real:
                    continue
                if not (it.get("bulk_injected") or it.get("link_mode") == "archive_fallback"):
                    continue
                targets.append(
                    {
                        "device_model": m,
                        "device_category": it.get("hardware_category")
                        or it.get("category")
                        or "工控设备",
                        "value_score": float(it.get("value_score") or 50),
                        "pain_points": it.get("pain_points") or [],
                    }
                )
                seen.add(key)
                if len(targets) >= limit_models:
                    break
        except Exception:
            pass

    failed = _load_failed(failed_path)
    existing = _load_existing_sources(sources_path)
    session = _session()

    stats: dict[str, Any] = {
        "models": len(targets),
        "skip_cataloged": len(already_real),
        "queries": 0,
        "search_hits": 0,
        "pages_scanned": 0,
        "direct_found": 0,
        "added": 0,
        "strategy": strategy,
        "engine": engine,
        "mode": "rank_adaptive",
    }
    pending: list[tuple[str, str, str, str, str, float]] = []
    rule_lines = [
        f"# seeker generated_at={datetime.now().isoformat()} strategy={strategy} engine={engine}",
        "",
    ]
    dead_engines: set[str] = set()

    # AI 扩写检索词：只对未上架型号，一次批量、英文、盯直链
    ai_extra: dict[str, list[str]] = {}
    if use_ai_queries and targets:
        try:
            from ai_cleaner import get_ollama_client

            client = get_ollama_client()
            if client.probe() and not stop():
                sample = targets[: min(10, len(targets))]
                prompt = (
                    "Generate ENGLISH web search queries to find DIRECT download "
                    "files (.zip/.exe/.msi) for legacy Windows drivers/firmware.\n"
                    "Prefer queries that hit GitHub Releases or OEM archives.\n"
                    "Output JSON only: "
                    '{"queries":{"MODEL":["q1","q2","q3"]}}\n'
                    "Models:\n"
                    + "\n".join(
                        f"- {r.get('device_model')}"
                        for r in sample
                    )
                )
                data = client.chat_json(
                    "You are a search-query expert for scarce legacy Windows "
                    "driver downloads. Return JSON only. No Chinese.",
                    prompt,
                    temperature=0.25,
                )
                if data and isinstance(data.get("queries"), dict):
                    by_norm: dict[str, list[str]] = {}
                    for k, v in data["queries"].items():
                        if not isinstance(v, list):
                            continue
                        qs = [
                            str(x).strip()
                            for x in v
                            if str(x).strip() and not re.search(r"[\u4e00-\u9fff]", str(x))
                        ][:4]
                        if qs:
                            by_norm[_norm_model_key(str(k))] = qs
                    for r in sample:
                        m = str(r.get("device_model") or "").strip()
                        qs = by_norm.get(_norm_model_key(m))
                        if qs:
                            ai_extra[m] = qs
                    stats["ai_query_models"] = len(ai_extra)
                    print(
                        f"[挖链AI] expanded queries for {len(ai_extra)} models",
                        flush=True,
                    )
        except Exception as exc:
            logger.info("挖链 AI 扩写查询跳过: %s", exc)

    dry_models = 0  # 连续 0 直链型号 → 早停，别空转整表白名单
    abort_seek = False
    try:
        for row in targets:
            if abort_seek or stop() or stats["added"] + len(pending) >= max_new_links:
                break
            # 连续 4 个型号 0 直链且总 hits=0 → 早停（引擎哑火）
            if dry_models >= 4 and stats["direct_found"] == 0 and stats["search_hits"] == 0:
                print(
                    f"[挖链] 连续 {dry_models} 型号且引擎 0 hits，早停（queries={stats['queries']}）",
                    flush=True,
                )
                stats["early_stop"] = "dry_streak"
                break
            if stats["queries"] >= 4 and stats["search_hits"] == 0:
                print(
                    f"[挖链] 搜索引擎 0 hits，早停（queries={stats['queries']}）",
                    flush=True,
                )
                stats["early_stop"] = "engines_dead"
                break
            model = str(row.get("device_model") or "").strip()
            if not is_diggable_model(model):
                continue
            cat = str(row.get("device_category") or "工控设备").strip()
            score = float(row.get("value_score") or 0)
            pains = row.get("pain_points") or []
            pain = "；".join(str(p) for p in pains[:2]) if pains else "legacy driver"
            os_ver = "通用"
            # 每型号只打 1 条高意图查询（快）；有直链再加第 2 条
            queries = build_queries(row, strategy=strategy)[:1]
            for extra in ai_extra.get(model, [])[:1]:
                if extra not in queries:
                    queries.append(extra)
            queries = queries[:2]
            rule_lines.append(f"{model} | {cat} | " + " || ".join(queries))

            found_for_model = 0
            for q in queries:
                if stop() or stats["added"] + len(pending) >= max_new_links:
                    break
                if found_for_model >= 2:
                    break
                # 已有 hits 但连续型号抽不出直链 → 别空烧
                # 已有 hits 但连续型号抽不出直链 → 换 inurl 查询再试一轮，仍 0 再停
                if (
                    stats["queries"] >= 4
                    and stats["search_hits"] >= 8
                    and stats["direct_found"] == 0
                    and dry_models >= 3
                ):
                    print(
                        f"[挖链] SERP 无直链，早停（queries={stats['queries']}）",
                        flush=True,
                    )
                    stats["early_stop"] = "serp_no_direct"
                    abort_seek = True
                    break
                stats["queries"] += 1
                try:
                    from stop_utils import StopRequested, interruptible_sleep

                    interruptible_sleep(random.uniform(0.05, 0.15), stop, label="seeker")
                except StopRequested:
                    break
                hits = search_web(
                    session,
                    q,
                    engine=engine,
                    max_results=6,
                    _dead=dead_engines,
                )

                def _hit_rank(u: str) -> tuple[int, str]:
                    low = u.lower()
                    try:
                        from host_scoreboard import is_demoted, score as host_score

                        if is_demoted(u, threshold=-8.0):
                            return (95, low)  # 重度降权站几乎不点
                        hs = host_score(
                            __import__("urllib.parse", fromlist=["urlparse"]).urlparse(u).netloc
                        )
                    except Exception:
                        hs = 0.0
                    # 排行榜意图：直链 / GitHub Release 优先；archive.org 靠后
                    if "/releases/download/" in low:
                        return (0, low)
                    if _accept_download_url(u):
                        return (0, low)
                    if "github.com" in low and "/releases" in low:
                        return (1, low)
                    if any(
                        s in low
                        for s in (
                            "sourceforge.net",
                            "softpedia.com",
                            "techpowerup.com",
                            "station-drivers.com",
                            "oldversion.com",
                            "intel.com",
                            "nvidia.com",
                            "broadcom.com",
                            "realtek.com",
                            "dell.com",
                            "hp.com",
                        )
                    ):
                        return (2, low)
                    if "gitee.com" in low:
                        return (3, low)
                    if "archive.org" in low:
                        return (80, low)  # 本机常超时，别优先
                    if "github.com" in low:
                        return (4, low)
                    # 分数越好越靠前（次级键）
                    return (10 - min(9, int(hs)), low)

                hits = sorted(hits, key=_hit_rank)
                # 放宽：可点排行站落地页（≤4）+ 直链；丢掉重度降权
                hits = [h for h in hits if _hit_rank(h)[0] <= 10][:8]
                stats["search_hits"] += len(hits)
                print(f"[挖链] q=`{q}` hits={len(hits)}", flush=True)

                for hit in hits:
                    if stop():
                        break
                    if hit in existing or hit in failed:
                        continue
                    if _accept_download_url(hit):
                        pending.append((hit, cat, model, os_ver, pain, score))
                        existing.add(hit)
                        stats["direct_found"] += 1
                        found_for_model += 1
                        continue
                    if _bad_host(hit):
                        continue
                    # 落地页：驱动站 + URL 含 download/driver/release 的结果
                    if "github.com" not in hit.lower():
                        low_hit = hit.lower()
                        scrape_ok = any(
                            s in low_hit
                            for s in (
                                "sourceforge.net",
                                "softpedia.com",
                                "techpowerup.com",
                                "station-drivers.com",
                                "oldversion.com",
                                "gitee.com",
                                "intel.com",
                                "nvidia.com",
                                "broadcom.com",
                                "realtek.com",
                                "dell.com",
                                "hp.com",
                                "onlinedown.net",
                                "mydrivers.com",
                                "zol.com.cn",
                                "github.io",
                                "/download",
                                "driver",
                                "firmware",
                                "/releases",
                            )
                        )
                        if "archive.org" in low_hit:
                            try:
                                from host_scoreboard import is_demoted

                                scrape_ok = not is_demoted(hit, threshold=-6.0)
                            except Exception:
                                scrape_ok = False
                        if scrape_ok:
                            try:
                                from stop_utils import StopRequested, interruptible_sleep

                                interruptible_sleep(
                                    random.uniform(0.05, 0.12), stop, label="seeker-rank"
                                )
                                html = fetch_page(session, hit, timeout=8.0)
                                stats["pages_scanned"] += 1
                                dls = list(extract_download_links(hit, html))
                                try:
                                    dls.extend(
                                        resolve_cn_portal_downloads(
                                            session, hit, html, max_links=4
                                        )
                                    )
                                except Exception as exc:
                                    logger.debug("cn portal %s: %s", hit[:60], exc)
                                for dl in dls:
                                    if dl in existing or dl in failed:
                                        continue
                                    if not (
                                        _accept_download_url(dl)
                                        or "downloads.ashx" in dl.lower()
                                        or dl.lower().endswith(".zip")
                                        or "application/zip" in dl.lower()
                                    ):
                                        # 跟跳后的 CDN zip 已是绝对 URL
                                        if not re.search(
                                            r"\.(zip|exe|msi|rar|7z)(\?|$)",
                                            dl,
                                            re.I,
                                        ):
                                            continue
                                    try:
                                        from host_scoreboard import is_demoted

                                        if is_demoted(dl, threshold=-8.0):
                                            continue
                                    except Exception:
                                        pass
                                    pending.append(
                                        (dl, cat, model, os_ver, pain, score)
                                    )
                                    existing.add(dl)
                                    stats["direct_found"] += 1
                                    found_for_model += 1
                                    if found_for_model >= 3:
                                        break
                            except StopRequested:
                                break
                            except Exception as exc:
                                logger.debug("排行站挖链失败 %s: %s", hit, exc)
                        continue
                    for dl in resolve_github_release_assets(session, hit, max_assets=6):
                        if dl in existing or dl in failed:
                            continue
                        if not (_accept_download_url(dl) or "/releases/download/" in dl):
                            continue
                        pending.append((dl, cat, model, os_ver, pain, score))
                        existing.add(dl)
                        stats["direct_found"] += 1
                        found_for_model += 1
                        if (
                            stats["added"] + len(pending) >= max_new_links
                            or found_for_model >= 2
                        ):
                            break
                    if found_for_model >= 2:
                        break
                    # 仅 /releases 页再尝试一次 HTML（API/限流时兜底）
                    if "/releases" not in hit.lower():
                        continue
                    try:
                        from stop_utils import StopRequested, interruptible_sleep

                        interruptible_sleep(
                            random.uniform(0.05, 0.12), stop, label="seeker-page"
                        )
                        html = fetch_page(session, hit, timeout=8.0)
                        stats["pages_scanned"] += 1
                        for dl in extract_download_links(hit, html):
                            if dl in existing or dl in failed:
                                continue
                            if not (
                                _accept_download_url(dl)
                                or "/releases/download/" in dl.lower()
                            ):
                                continue
                            pending.append((dl, cat, model, os_ver, pain, score))
                            existing.add(dl)
                            stats["direct_found"] += 1
                            found_for_model += 1
                            if (
                                stats["added"] + len(pending) >= max_new_links
                                or found_for_model >= 2
                            ):
                                break
                    except StopRequested:
                        break
                    except Exception as exc:
                        logger.debug("页面挖链失败 %s: %s", hit, exc)
                        continue

            if found_for_model == 0:
                dry_models += 1
            else:
                dry_models = 0

            # 每型号扫完就写入 sources 一批，降低中断损失
            if pending:
                n = append_sources(sources_path, pending)
                stats["added"] += n
                pending = []
                print(f"[挖链] 已写入 sources +{n} 累计={stats['added']}", flush=True)
    finally:
        session.close()

    if pending:
        stats["added"] += append_sources(sources_path, pending)

    search_rules_path.parent.mkdir(parents=True, exist_ok=True)
    search_rules_path.write_text("\n".join(rule_lines) + "\n", encoding="utf-8")
    print(
        f"[挖链完成] models={stats['models']} queries={stats['queries']} "
        f"pages={stats['pages_scanned']} direct={stats['direct_found']} "
        f"added={stats['added']} engine={engine} strategy={strategy}",
        flush=True,
    )
    return stats


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="全网挖链 Agent")
    p.add_argument("--limit", type=int, default=15, help="最多处理白名单型号数")
    p.add_argument("--max-links", type=int, default=40, help="本轮最多新直链")
    p.add_argument("--strategy", type=int, default=0, help="查询策略轮换 0-3")
    p.add_argument("--engine", choices=("ddg", "bing"), default="ddg")
    p.add_argument("--no-ai", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    seek_for_whitelist(
        limit_models=max(1, args.limit),
        max_new_links=max(1, args.max_links),
        strategy=args.strategy,
        engine=args.engine,
        use_ai_queries=not args.no_ai,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
