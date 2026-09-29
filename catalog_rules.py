"""
资产分类与型号硬规则：禁止「未知型号」「其他外设」等笼统标签。
供 cleaner_forum / ai_cleaner / 白名单 / vault 共用。
"""

from __future__ import annotations

import re
from typing import Optional

# 允许的硬件大类（展示名必须落在此集合；禁止「其他外设」）
ALLOWED_CATEGORIES: tuple[str, ...] = (
    "显卡",
    "工控/板载网卡",
    "无线网卡",
    "老旧采集卡/工控卡",
    "工控设备",
    "品牌打印机",
    "主板芯片组",
    "声卡",
    "串口/工控通信",
    "固件/BIOS",
    "存储控制器",
    "摄像头/采集",
)

# 别名归一
_CATEGORY_ALIASES: dict[str, str] = {
    "核显/独显": "显卡",
    "独显": "显卡",
    "核显": "显卡",
    "网卡": "工控/板载网卡",
    "板载网卡": "工控/板载网卡",
    "工控网卡": "工控/板载网卡",
    "采集卡": "老旧采集卡/工控卡",
    "工控卡": "老旧采集卡/工控卡",
    "老旧采集卡": "老旧采集卡/工控卡",
    "打印机": "品牌打印机",
    "主板": "主板芯片组",
    "芯片组": "主板芯片组",
    "串口": "串口/工控通信",
    "bios": "固件/BIOS",
    "固件": "固件/BIOS",
    "firmware": "固件/BIOS",
    "存储": "存储控制器",
    "摄像头": "摄像头/采集",
}

_CATEGORY_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"bios|uefi|ami\s*bios|award|固件", re.I), "固件/BIOS"),
    (re.compile(r"打印机|LBP|LaserJet|Canon|Epson|HP\s*Laser|Printer", re.I), "品牌打印机"),
    (re.compile(r"采集卡|DAQ|数据采集|运动控制|工控卡|PCI[eE]?\s*卡|NIDAQ|VXI", re.I), "老旧采集卡/工控卡"),
    (re.compile(r"工控|Advantech|研华|研祥|ADLINK|凌华|PLC", re.I), "工控设备"),
    (re.compile(r"RTL|Realtek|网卡|Ethernet|NIC|LAN|板载网", re.I), "工控/板载网卡"),
    (re.compile(r"Wi-?Fi|无线|WLAN|Atheros|无线网卡", re.I), "无线网卡"),
    (re.compile(r"GTX|RTX|Radeon|GeForce|显卡|Graphics|GPU|显示适配器|CMP\d", re.I), "显卡"),
    (re.compile(r"芯片组|Chipset|主板|motherboard", re.I), "主板芯片组"),
    (re.compile(r"声卡|Audio|HD\s*Audio|Sound", re.I), "声卡"),
    (re.compile(r"RS232|串口|COM\s*\d|USB.?Serial|UART", re.I), "串口/工控通信"),
    (re.compile(r"NVMe|AHCI|SATA|RAID|存储控制器|SCSI", re.I), "存储控制器"),
    (re.compile(r"摄像头|Camera|UVC|webcam|采集", re.I), "摄像头/采集"),
]

_MODEL_FALLBACK = re.compile(
    r"(?:PCI\\VEN_[0-9A-F]{4}&DEV_[0-9A-F]{4}(?:&SUBSYS_[0-9A-F]{8})?"
    r"|USB\\VID_[0-9A-F]{4}&PID_[0-9A-F]{4}"
    r"|RTL\s?8\d{3}[A-Z0-9]*"
    r"|BCM\d{4,5}[A-Z0-9]*"
    r"|GTX\s?\d{3,4}\s?(?:Ti|SUPER)?"
    r"|RTX\s?\d{3,4}\s?(?:Ti|SUPER)?"
    r"|RX\s?\d{3,4}"
    r"|HD\s?Graphics\s?\d{3,4}"
    r"|UHD\s?Graphics\s?\d{3,4}"
    r"|Intel\s+N\d{2,4}"
    r"|NVIDIA\s+CMP\s*\d*\w*"
    r"|LBP\d{3,5}"
    r"|RK\d{4}"
    r"|STM32\w*"
    r"|NIDAQ[\w\-\s]{0,20}"
    r"|[A-Z]{2,6}\-?\d{2,5}[A-Z]{0,3})",
    re.I,
)

_FORBIDDEN_MODEL_EXACT = {
    "",
    "未知型号",
    "未知设备",
    "unknown",
    "n/a",
    "na",
    "none",
    "null",
    "其他",
    "其他外设",
    "amd64",
    "arm64",
    "x86",
    "x64",
    "sha-256",
    "sha256",
    "sha1",
    "md5",
}

_FORBIDDEN_MODEL_RE = re.compile(
    r"^(unknown|n/?a|其他|未[知知名]|generic|test|sample|null)$",
    re.I,
)

# 短语/工单号/过泛 NIC 描述：不是可检索驱动 SKU
_FORBIDDEN_MODEL_PHRASE_RE = re.compile(
    r"(?i)^("
    r"nic(\s+\w+){1,6}"
    r"|idf[a-z]*-\d+"
    r"|nicely\b.*"
    r"|intel\s+graphics\s+control"
    r"|national\s+instruments$"  # 过宽品牌，需落到具体 DAQ 型号
    r")$"
)

_KNOWN_BRAND_MODEL_RE = re.compile(
    r"(?i)^(nvidia|intel|amd|realtek|broadcom|qualcomm|mediatek|rockchip)\s+.+"
)


def infer_category(text: str, hint: str = "") -> Optional[str]:
    blob = f"{hint} {text}"
    for pat, label in _CATEGORY_RULES:
        if pat.search(blob):
            return label
    return None


def normalize_category(raw: str, *, context: str = "") -> Optional[str]:
    """返回规范大类；无法归类则 None（禁止落到「其他外设」）。"""
    name = (raw or "").strip()
    if name in _CATEGORY_ALIASES:
        name = _CATEGORY_ALIASES[name]
    low = name.lower()
    for k, v in _CATEGORY_ALIASES.items():
        if k.lower() == low:
            name = v
            break
    if name in ALLOWED_CATEGORIES:
        return name
    # 禁止笼统类
    if name in {"其他", "其他外设", "其它", "其它外设", "未分类", "unknown"}:
        name = ""
    inferred = infer_category(context, name)
    if inferred:
        return inferred
    return None


def extract_model_candidate(text: str) -> Optional[str]:
    if not text:
        return None
    m = _MODEL_FALLBACK.search(text)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(0)).strip()


def _is_plausible_driver_model(model: str) -> bool:
    """可检索的驱动型号：硬件 ID / 含数字芯片名 / 品牌+型号；拒绝英文短语。"""
    m = (model or "").strip()
    if not m or len(m) < 2:
        return False
    if _FORBIDDEN_MODEL_PHRASE_RE.match(m):
        return False
    # 硬件 ID
    if re.search(r"(?i)(?:PCI\\|USB\\VID_)", m):
        return True
    # 工控采集常见族
    if re.search(r"(?i)\b(nidaqmx|nidaq|daqmx|vxi-?11)\b", m):
        return True
    # 纯英文短语（≥3 词且无数字）几乎一定是句子碎片
    words = m.split()
    if len(words) >= 3 and not re.search(r"\d", m) and not _KNOWN_BRAND_MODEL_RE.match(m):
        return False
    if re.search(r"\d", m):
        return True
    if _KNOWN_BRAND_MODEL_RE.match(m):
        return True
    # 短芯片代号：nicaud / GT911 / ST7789
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]{1,18}", m) and len(m) <= 20:
        return True
    return False


def normalize_model(raw: str, *, context: str = "") -> Optional[str]:
    """返回可用型号；垃圾/未知返回 None。"""
    model = re.sub(r"\s+", " ", (raw or "").split("|")[0].strip())
    # 去掉明显噪声
    if model.lower() in _FORBIDDEN_MODEL_EXACT or _FORBIDDEN_MODEL_RE.match(model):
        model = ""
    if len(model) < 2:
        model = ""
    # 纯架构/哈希词
    if re.fullmatch(r"(?i)(amd64|arm64|x86_64|x64|ia32|sha\-?\d+|md5)", model or ""):
        model = ""
    if not model:
        cand = extract_model_candidate(context)
        if cand and cand.lower() not in _FORBIDDEN_MODEL_EXACT:
            model = cand
    if not model:
        return None
    if model.lower() in _FORBIDDEN_MODEL_EXACT or _FORBIDDEN_MODEL_RE.match(model):
        return None
    # 过泛品牌词且无后缀型号
    if re.fullmatch(r"(?i)(intel|amd|nvidia|realtek|broadcom|microsoft|windows)", model):
        cand = extract_model_candidate(context)
        if cand and cand.lower() != model.lower():
            model = cand
        else:
            return None
    if not _is_plausible_driver_model(model):
        cand = extract_model_candidate(context)
        if cand and _is_plausible_driver_model(cand):
            return cand[:120]
        return None
    return model[:120]


def model_key(model: str) -> str:
    key = re.sub(r"\s+", " ", (model or "").strip()).lower()
    key = key.replace("－", "-").replace("—", "-")
    return key


def is_complete_asset(category: Optional[str], model: Optional[str]) -> bool:
    return bool(category and model and category in ALLOWED_CATEGORIES)


CATEGORY_PROMPT_LIST = "、".join(ALLOWED_CATEGORIES)
