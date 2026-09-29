"""把库里的原始字段，翻译成用户能看懂的列表信息。"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

SITE_NAMES = {
    "www.oldversion.com": "OldVersion（旧版软件归档站）",
    "oldversion.com": "OldVersion（旧版软件归档站）",
    "superuser.com": "Super User（电脑问答社区）",
    "stackoverflow.com": "Stack Overflow（编程问答）",
    "serverfault.com": "Server Fault（运维问答）",
    "www.reddit.com": "Reddit",
    "reddit.com": "Reddit",
    "github.com": "GitHub Issues",
    "api.github.com": "GitHub",
}


def site_from_url(url: str) -> str:
    if not url:
        return "未知网站"
    host = (urlparse(url).netloc or "").lower()
    if host in SITE_NAMES:
        return SITE_NAMES[host]
    for key, name in SITE_NAMES.items():
        if host.endswith(key):
            return name
    return host or "未知网站"


def guess_package_type(title: str = "", model: str = "", category: str = "") -> str:
    blob = f"{title} {model} {category}".lower()
    if re.search(r"采集|daq|工控|pci|网卡|显卡|芯片|bios|固件|firmware|driver|驱动", blob):
        if re.search(r"固件|firmware|bios", blob):
            return "固件 / BIOS 包"
        if re.search(r"采集|daq|工控|pci", blob):
            return "工控卡 / 采集卡驱动"
        if re.search(r"网卡|nic|ethernet|rtl|bcm", blob):
            return "网卡驱动"
        if re.search(r"显卡|gpu|gtx|rtx|radeon|graphics", blob):
            return "显卡驱动"
        return "硬件驱动 / 修复包"
    if re.search(r"messenger|skype|qq|游戏|game|聊天|播放器|torrent", blob):
        return "消费级软件（通常应过滤）"
    if re.search(r"directx", blob):
        return "系统运行库 / 组件"
    return "旧版软件安装包"


def guess_purpose(
    *,
    title: str = "",
    model: str = "",
    category: str = "",
    pain_point: str = "",
    package_type: str = "",
    kind: str = "scraped",
) -> str:
    if pain_point and not str(pain_point).startswith("[已丢弃]"):
        return pain_point
    if category and category not in {"其他", "其他外设"}:
        return f"属于「{category}」，用户多为找对应驱动/修复方案"
    blob = f"{title} {model}".lower()
    if "网卡" in blob or "rtl" in blob or "ethernet" in blob:
        return "让电脑/工控机联网用的网卡驱动"
    if "显卡" in blob or "gpu" in blob or "gtx" in blob or "graphics" in blob:
        return "让显示器正常输出的显卡驱动（常见 Code 43）"
    if "采集" in blob or "daq" in blob or "工控" in blob:
        return "工业现场采集/控制设备用的板卡驱动"
    if "打印机" in blob or "lbp" in blob:
        return "打印机驱动，装上才能打印"
    if kind == "forum":
        return "用户在网上求助：机器报错或缺驱动，想找到能装的包"
    if "消费级" in package_type:
        return "聊天/娱乐类旧软件，不是我们要的工业硬件驱动"
    if "运行库" in package_type:
        return "给老游戏/老程序提供运行环境的系统组件"
    return "旧版软件安装文件；需再经过滤后才可能进入硬件驱动清单"


def enrich_scraped(row: dict[str, Any]) -> dict[str, Any]:
    title = row.get("title") or ""
    model = row.get("device_model") or title or "未知型号"
    url = row.get("source_url") or ""
    pkg = guess_package_type(title, str(model), "")
    purpose = guess_purpose(
        title=title,
        model=str(model),
        package_type=pkg,
        kind="scraped",
    )
    out = dict(row)
    out.update(
        {
            "显示型号": model,
            "资源类型": pkg,
            "来源网站": site_from_url(url),
            "下载来源网站": site_from_url(url),
            "设备用途说明": purpose,
            "原始链接": url,
        }
    )
    return out


def enrich_forum(row: dict[str, Any]) -> dict[str, Any]:
    title = row.get("title") or ""
    model = row.get("device_model") or "未知型号"
    category = row.get("device_category") or ""
    url = row.get("source_url") or ""
    pkg = guess_package_type(title, str(model), str(category))
    purpose = guess_purpose(
        title=title,
        model=str(model),
        category=str(category),
        pain_point=row.get("pain_point") or "",
        package_type=pkg,
        kind="forum",
    )
    out = dict(row)
    out.update(
        {
            "显示型号": model,
            "资源类型": "需求线索（用户找驱动/修复包）",
            "设备大类": category or "未分类",
            "来源网站": site_from_url(url),
            "下载来源网站": site_from_url(url),
            "设备用途说明": purpose,
            "原始链接": url,
            "报错信息": row.get("error_codes") or "-",
        }
    )
    return out
