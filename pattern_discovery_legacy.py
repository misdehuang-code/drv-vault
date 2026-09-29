"""本地硬件路径规则矩阵：不依赖外部搜索引擎，按厂商遗留目录规律生成直链候选。

产物直接写入 sources.txt，交 FULFILL 做 HEAD 验活。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
DEFAULT_WHITELIST = ROOT / "data" / "急需设备型号白名单.json"
DEFAULT_SOURCES = ROOT / "sources.txt"
DEFAULT_FAILED = ROOT / "failed_sources.txt"

# 已知高命中 GitHub Releases（Unlock / 开源驱动）— 本地枚举 tag+asset，无搜索 API
_CMP_REPOS = (
    (
        "WildFlash1st/cmp90hx-unlock-for-windows",
        ("v3.04", "v3.03", "v3.0", "v2.1", "v2.0", "v1.5", "v1.0"),
        (
            "cmp90-unlock-{tag}.zip",
            "cmp90hx-unlock-{tag}.zip",
            "CMP90HX-Unlock-{tag}.zip",
            "CMP90-Unlock-{tag}.zip",
            "unlock.zip",
            "release.zip",
            "windows.zip",
        ),
    ),
    (
        "PZH1gdmu/CMP40HX-Unlock",
        ("v1.0", "v1.1", "v1.2", "v2.0", "v2.1", "latest"),
        (
            "CMP40HX-Unlock.zip",
            "CMP40HX-Unlock-{tag}.zip",
            "cmp40hx-unlock.zip",
            "unlock.zip",
            "driver.zip",
            "release.zip",
        ),
    ),
    (
        "pearlfortune/cmpunlocker",
        ("v0.1.28", "v0.1.27", "v0.1.20", "v0.1.10"),
        (
            "cmpunlocker-{tag}-windows-x64.zip",
            "cmpunlocker-{tag}-win64.zip",
            "cmpunlocker.zip",
        ),
    ),
)

_RTL_REPOS = (
    (
        "chunyouwei/Realtek-PCIe-GBE-Family-Controller",
        ("v10.056", "v10.052", "v10.051", "v10.045", "v10.038", "v1.0"),
        (
            "Install_{tag}.zip",
            "Install_Win10_Win11.zip",
            "Realtek_PCIe_FE_GBE_Family_Win10_Win11_{tag}.zip",
            "driver.zip",
            "RTL8168.zip",
            "RTL8111.zip",
        ),
    ),
)

_RK_REPOS = (
    (
        "worproject/Rockchip-Windows-Drivers",
        ("v0.2", "v0.1.1", "v0.1", "v0.0.9", "latest"),
        (
            "RkDrvPkg_ARM64.zip",
            "Rockchip-Windows-Drivers.zip",
            "Rockchip-Windows-Drivers-{tag}.zip",
            "drivers.zip",
            "rk3588.zip",
        ),
    ),
)

# 厂商官方 / 镜像遗留路径模板（占位符本地展开）
_VENDOR_FTP_PATTERNS: list[tuple[re.Pattern[str], list[str]]] = [
    # Realtek PCIe GBE / RTL8168 家族
    (
        re.compile(r"(?i)rtl\s*81|8168|8111|realtek.*(?:gbe|lan|网卡)"),
        [
            "https://www.realtek.com/Download?type=download&id=10",
            "https://github.com/chunyouwei/Realtek-PCIe-GBE-Family-Controller/releases/download/v10.056/Install_Win10_Win11.zip",
            "https://github.com/chunyouwei/Realtek-PCIe-GBE-Family-Controller/releases/download/v10.051/Install_Win10_Win11.zip",
            "https://archive.org/download/realtek-pcie-gbe-family-controller-windows-10/Realtek_PCIe_FE_GBE_Family_Controller_10051_20211102.zip",
            "https://archive.org/download/realtek-lan-driver-win7/Realtek_Win7_8168.zip",
        ],
    ),
    # NVIDIA CMP / Quadro unlock 包
    (
        re.compile(r"(?i)\bcmp\b|quadro.*unlock|10de.*(?:1b|1e|1c)"),
        [
            "https://github.com/WildFlash1st/cmp90hx-unlock-for-windows/releases/download/v3.04/cmp90-unlock-v3.04.zip",
            "https://github.com/WildFlash1st/cmp90hx-unlock-for-windows/releases/download/v3.0/cmp90-unlock-v3.0.zip",
            "https://github.com/WildFlash1st/cmp90hx-unlock-for-windows/releases/download/v2.0/cmp90-unlock-v2.0.zip",
        ],
    ),
    # Intel HD Graphics 4600 / Haswell / HD 4000 / UHD
    (
        re.compile(
            r"(?i)hd\s*graphics\s*(?:4600|4000)|haswell|intel.*(?:4600|4000)|uhd\s*0x4626|intel\s*n100|intel\s*gpu"
        ),
        [
            "https://downloadmirror.intel.com/25143/eng/win64_15.36.39.5177.exe",
            "https://downloadmirror.intel.com/25142/eng/win32_15.36.39.5177.exe",
            "https://downloadmirror.intel.com/25143/eng/win64_15.36.33.4412.exe",
            "https://downloadmirror.intel.com/25143/eng/win64_15.40.42.5063.exe",
            "https://downloadmirror.intel.com/78490/eng/gfx_win_101.5518.exe",
            "https://archive.org/download/intel-hd-graphics-4600-win7/win64_15.36.33.4412.exe",
            "https://archive.org/download/intel-graphics-driver-15.40/win64_15.40.42.5063.exe",
            "https://archive.org/download/intel-hd-graphics-4000-win7/win64_15.33.42.64.exe",
        ],
    ),
    # NI-DAQ / nidaqmx
    (
        re.compile(r"(?i)nidaq|ni[\s_-]?daq|daqmx"),
        [
            "https://download.ni.com/support/softlib/multifunction/daq/daqmx/Windows/19.5/NIDAQmx1950f0.exe",
            "https://download.ni.com/support/softlib/multifunction/daq/daqmx/Windows/19.0/NIDAQmx1900f0.exe",
            "https://download.ni.com/support/softlib/multifunction/daq/daqmx/Windows/18.5/NIDAQmx1850f0.exe",
            "https://download.ni.com/support/softlib/multifunction/daq/daqmx/Windows/17.1/NIDAQmx1710f0.exe",
            "https://download.ni.com/support/softlib/multifunction/daq/daqmx/Windows/17.0/NIDAQmx1700f0.exe",
            "https://archive.org/download/ni-daqmx-17.1/NIDAQmx1710f0.exe",
            "https://archive.org/download/ni-daqmx-19.5/NIDAQmx1950f0.exe",
        ],
    ),
    # Rockchip RK3588 Windows
    (
        re.compile(r"(?i)rk3588|rockchip.*3588"),
        [
            "https://github.com/worproject/Rockchip-Windows-Drivers/releases/download/v0.2/RkDrvPkg_ARM64.zip",
            "https://github.com/worproject/Rockchip-Windows-Drivers/releases/download/v0.1/RkDrvPkg_ARM64.zip",
        ],
    ),
    # Cypress FX2 / EZ-USB (VID_04B4)
    (
        re.compile(r"(?i)04b4|8613|cypress|ez[\s_-]?usb|fx2"),
        [
            "https://www.cypress.com/file/157911/download",
            "https://archive.org/download/cypress-cy3684-ez-usb-fx2lp/CY3684_EZ-USB_FX2LP.zip",
            "https://github.com/funneld/FX2-Firmware/releases/download/v1.0/fx2.zip",
        ],
    ),
    # AMD PCI VEN_1002 legacy
    (
        re.compile(r"(?i)ven_1002|radeon|amd.*(?:gpu|graphics)"),
        [
            "https://drivers.amd.com/drivers/windows/radeon-software-adrenalin-22.6.1-minimalsetup-220615_web.exe",
            "https://archive.org/download/amd-radeon-legacy-win7/AMD_Catalyst_15.7.1_Win7.exe",
        ],
    ),
    # Siglent SPD power supply SCPI/GUI
    (
        re.compile(r"(?i)spd3303|siglent.*spd|scpi.*instrument"),
        [
            "https://github.com/little-did-I-know/SCPI-Instrument-Control/releases/download/v7.0.1/SiglentGUI-v7.0.1-Windows-x64.zip",
            "https://github.com/little-did-I-know/SCPI-Instrument-Control/releases/download/v7.0.0/SiglentGUI-v7.0.0-Windows-x64.zip",
        ],
    ),
]


def _norm_key(model: str) -> str:
    s = (model or "").strip().lower()
    s = s.replace("\\", "_")
    s = re.sub(r"_+", "_", s)
    s = re.sub(r"\s+", " ", s)
    return s


def _load_existing(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out: set[str] = set()
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.add(line.split("|", 1)[0].strip())
    return out


def _expand_github_assets(
    repo: str, tags: Iterable[str], name_tmpls: Iterable[str]
) -> list[str]:
    urls: list[str] = []
    for tag in tags:
        tag_clean = tag.lstrip("v")
        for tmpl in name_tmpls:
            name = (
                tmpl.replace("{tag}", tag)
                .replace("{ver}", tag_clean)
                .replace("{version}", tag_clean)
            )
            urls.append(
                f"https://github.com/{repo}/releases/download/{tag}/{name}"
            )
    return urls


def _pci_hints(model: str) -> list[str]:
    """从 PCI\\VEN_xxxx&DEV_yyyy 抽提示词，映射厂商规则。"""
    m = model or ""
    hints: list[str] = [m]
    ven = re.search(r"VEN_([0-9A-Fa-f]{4})", m)
    dev = re.search(r"DEV_([0-9A-Fa-f]{4})", m)
    if ven:
        code = ven.group(1).upper()
        hints.append(f"VEN_{code}")
        vendor_map = {
            "10DE": "NVIDIA CMP",
            "1002": "AMD Radeon VEN_1002",
            "8086": "Intel HD Graphics",
            "10EC": "RTL8168",
            "14E4": "Broadcom",
            "04B4": "Cypress FX2 VID_04B4",
        }
        if code in vendor_map:
            hints.append(vendor_map[code])
    if dev:
        hints.append(f"DEV_{dev.group(1).upper()}")
    return hints


def _live_github_release_urls(repos: list[str], *, max_per_repo: int = 8) -> list[str]:
    """对已知仓库用 HTML expanded_assets 拉真实直链（不走搜索引擎 / 可不依赖 API token）。"""
    out: list[str] = []
    try:
        from github_auth import html_allowed, html_release_assets
    except Exception:
        return out
    if not html_allowed():
        return out
    import requests as _rq

    s = _rq.Session()
    s.trust_env = False
    try:
        for full in repos:
            if "/" not in full:
                continue
            try:
                for u in html_release_assets(s, full, max_assets=max_per_repo):
                    if u and u not in out:
                        out.append(u)
            except Exception as exc:
                logger.debug("live release %s: %s", full, exc)
    finally:
        s.close()
    return out


def candidates_for_model(
    model: str,
    *,
    category: str = "",
    max_urls: int = 24,
    live_github: bool = True,
) -> list[str]:
    """按规则矩阵为单型号生成直链候选（去重保序）。"""
    blob = f"{model} {category}"
    seen: set[str] = set()
    out: list[str] = []

    def add(u: str) -> None:
        u = (u or "").strip()
        if not u.startswith("http") or u in seen:
            return
        seen.add(u)
        out.append(u)

    # 0) 已知仓库 → HTML 拉真实 asset（命中率远高于猜文件名）
    live_repos: list[str] = []
    low = blob.lower()
    if re.search(r"(?i)\bcmp\b|10de", low):
        live_repos.extend(r[0] for r in _CMP_REPOS)
    if re.search(r"(?i)rtl\s*81|8168|8111", low):
        live_repos.extend(r[0] for r in _RTL_REPOS)
    if re.search(r"(?i)rk3588|rockchip", low):
        live_repos.extend(r[0] for r in _RK_REPOS)
    if re.search(r"(?i)spd3303|siglent|scpi", low):
        live_repos.append("little-did-I-know/SCPI-Instrument-Control")
    if live_github and live_repos:
        for u in _live_github_release_urls(live_repos[:3], max_per_repo=8):
            add(u)
            if len(out) >= max_urls:
                return out

    # 1) 正则命中的厂商静态路径
    for rx, urls in _VENDOR_FTP_PATTERNS:
        if rx.search(blob):
            for u in urls:
                add(u)
                if len(out) >= max_urls:
                    return out

    # 2) PCI ID → 厂商提示后再跑一遍规则
    for hint in _pci_hints(model):
        if hint == model:
            continue
        for rx, urls in _VENDOR_FTP_PATTERNS:
            if rx.search(hint):
                for u in urls:
                    add(u)
                    if len(out) >= max_urls:
                        return out

    # 3) 关键字触发 GitHub 枚举矩阵（扩大 tag 覆盖，喂饱异步验活队列）
    low = blob.lower()
    if re.search(r"(?i)\bcmp\b|10de", low):
        for repo, tags, tmpls in _CMP_REPOS:
            for u in _expand_github_assets(repo, tags, tmpls):
                add(u)
                if len(out) >= max_urls:
                    return out
    if re.search(r"(?i)rtl\s*81|8168|8111", low):
        for repo, tags, tmpls in _RTL_REPOS:
            for u in _expand_github_assets(repo, tags, tmpls):
                add(u)
                if len(out) >= max_urls:
                    return out
    if re.search(r"(?i)rk3588|rockchip", low):
        for repo, tags, tmpls in _RK_REPOS:
            for u in _expand_github_assets(repo, tags, tmpls):
                add(u)
                if len(out) >= max_urls:
                    return out
    # 额外：Intel / NI / NVIDIA 路径再扩变体，喂饱异步并发队列
    if re.search(r"(?i)hd\s*graphics|haswell|intel.*(?:4600|4000)|uhd|n100", low):
        for build in (
            "15.36.39.5177",
            "15.36.33.4412",
            "15.40.42.5063",
            "15.33.42.64",
            "15.40.36.4703",
            "15.36.26.4312",
        ):
            add(f"https://downloadmirror.intel.com/25143/eng/win64_{build}.exe")
            add(f"https://downloadmirror.intel.com/25142/eng/win32_{build}.exe")
            if len(out) >= max_urls:
                return out
    if re.search(r"(?i)nidaq|daqmx", low):
        for ver, fname in (
            ("19.5", "NIDAQmx1950f0.exe"),
            ("19.0", "NIDAQmx1900f0.exe"),
            ("18.5", "NIDAQmx1850f0.exe"),
            ("18.0", "NIDAQmx1800f0.exe"),
            ("17.1", "NIDAQmx1710f0.exe"),
            ("17.0", "NIDAQmx1700f0.exe"),
            ("16.0", "NIDAQmx1600f0.exe"),
        ):
            add(
                f"https://download.ni.com/support/softlib/multifunction/daq/daqmx/Windows/{ver}/{fname}"
            )
            if len(out) >= max_urls:
                return out
    if re.search(r"(?i)\bcmp\b|10de|geforce|gtx|rtx|quadro", low):
        for repo, tags, tmpls in _CMP_REPOS:
            for u in _expand_github_assets(repo, tags, tmpls):
                add(u)
                if len(out) >= max_urls:
                    return out

    # 4) archive.org：仅对已知高价值型号生成（禁止对任意噪声型号盲目猜路径）
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", model).strip("-").lower()[:48]
    if slug and re.search(
        r"(?i)rtl81|8168|cmp|hd.?graphics|nidaq|rk3588|siglent|cypress|04b4|radeon|gtx|rtx",
        blob,
    ):
        add(f"https://archive.org/download/{slug}-windows-driver/{slug}-driver.zip")
        add(f"https://archive.org/download/{slug}-legacy-driver/{slug}_win7.zip")
        add(f"https://archive.org/download/{slug}-driver-archive/{slug}.zip")

    return out[:max_urls]


def run_pattern_discovery(
    *,
    whitelist_path: Path = DEFAULT_WHITELIST,
    sources_path: Path = DEFAULT_SOURCES,
    failed_path: Path = DEFAULT_FAILED,
    limit_models: int = 20,
    max_new_links: int = 80,
    max_per_model: int = 12,
    round_no: int = 1,
    skip_models: Optional[set[str]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """主入口：规则矩阵 → sources.txt。"""
    stop = should_stop or (lambda: False)
    from dead_end_blacklist import filter_active_models, note_model_outcome

    if not whitelist_path.exists():
        return {"added": 0, "generated": 0, "models": 0, "skipped_dead": 0}

    data = json.loads(whitelist_path.read_text(encoding="utf-8-sig"))
    raw_items = list(data.get("items") or [])
    skip = {_norm_key(m) for m in (skip_models or set()) if m}

    active, sunk_n = filter_active_models(raw_items, round_no=round_no)
    # 轮次偏移轮转，避免每轮只扫白名单头部
    pool: list[dict[str, Any]] = []
    for row in active:
        model = str(row.get("device_model") or "").strip()
        if not model or _norm_key(model) in skip:
            continue
        pool.append(row)
    if pool:
        offset = ((max(1, round_no) - 1) * max(1, limit_models // 2)) % len(pool)
        pool = pool[offset:] + pool[:offset]
    targets = pool[: max(1, limit_models)]

    failed = _load_existing(failed_path)
    existing = _load_existing(sources_path)
    pending: list[tuple[str, str, str, str, str, float]] = []
    generated = 0
    models_hit = 0
    models_miss: list[str] = []

    for row in targets:
        if stop() or generated >= max_new_links:
            break
        model = str(row.get("device_model") or "").strip()
        cat = str(row.get("device_category") or "工控设备").strip()
        score = float(row.get("value_score") or 0)
        pains = row.get("pain_points") or []
        pain = "；".join(str(p) for p in pains[:2]) if pains else "pattern-matrix"
        urls = candidates_for_model(model, category=cat, max_urls=max_per_model)
        if not urls:
            # 规则矩阵对该型号无覆盖 → 记空转，可能沉底
            models_miss.append(model)
            note_model_outcome(model, round_no=round_no, yielded=False)
            continue
        got = 0
        already_seen = 0
        for url in urls:
            if stop() or generated >= max_new_links:
                break
            if url in existing or url in failed:
                already_seen += 1
                continue
            # 规则矩阵允许官方遗留域（即使 FULFILL 黑名单里有长期 403 的域，仍可探）
            try:
                from link_curator import _NON_DRIVER_RE, is_direct_file_url

                if _NON_DRIVER_RE.search(url or ""):
                    continue
                if not (
                    is_direct_file_url(url)
                    or "/releases/download/" in url.lower()
                    or "archive.org/download/" in url.lower()
                    or "downloadmirror.intel.com" in url.lower()
                    or "download.ni.com" in url.lower()
                    or "cypress.com" in url.lower()
                ):
                    low = url.lower()
                    if not any(
                        h in low
                        for h in (
                            "github.com/",
                            "archive.org/",
                            "downloadmirror.intel.com",
                            "download.ni.com",
                            "realtek.com",
                            "cypress.com",
                            "drivers.amd.com",
                        )
                    ):
                        continue
            except Exception:
                pass
            pending.append((url, cat, model, "通用", pain, score))
            existing.add(url)
            generated += 1
            got += 1
        if got:
            models_hit += 1
            note_model_outcome(model, round_no=round_no, yielded=True)
        elif already_seen:
            # 规则已覆盖且候选都验过/在队列 — 不算死胡同
            models_hit += 1
            note_model_outcome(model, round_no=round_no, yielded=True)
        else:
            models_miss.append(model)
            note_model_outcome(model, round_no=round_no, yielded=False)

    added = 0
    if pending:
        from seeker_agent import append_sources

        added = append_sources(sources_path, pending)

    print(
        f"[规则矩阵] models={len(targets)} hit={models_hit} "
        f"generated={generated} added={added} sunk_skip={sunk_n}",
        flush=True,
    )
    return {
        "models": len(targets),
        "models_hit": models_hit,
        "generated": generated,
        "added": added,
        "skipped_dead": sunk_n,
        "miss_models": models_miss[:20],
        "generated_at": datetime.now().isoformat(),
    }
