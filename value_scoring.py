"""
工业/硬件高价值驱动：黑名单过滤 + 白名单加权打分（供 cleaner / cleaner_forum 共用）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

# 命中任一黑名单 → value_score=0，直接丢弃（消费级娱乐/聊天等）
BLACKLIST_KEYWORDS: tuple[str, ...] = (
    "msn",
    "messenger",
    "qq",
    "微信",
    "wechat",
    "skype",
    "yahoo messenger",
    "icq",
    "游戏",
    "game",
    "gaming",
    "steam",
    "epic games",
    "娱乐",
    "music",
    "mp3",
    "播放器",
    "player",
    "聊天",
    "聊天工具",
    "社交",
    "social",
    "manycam",
    "camfrog",
    "bearshare",
    "迅雷",
    "thunder",
    "bt下载",
    "影音",
    "movie",
    "视频播放",
    "动漫",
    "小说",
    "表情包",
    "网游",
)

# 工业/硬件白名单：关键词 → 权重
WHITELIST_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("工控", 4.0),
    ("工业", 3.5),
    ("采集卡", 4.5),
    ("daq", 4.0),
    ("运动控制", 4.0),
    ("plc", 3.5),
    ("pci", 3.0),
    ("pcie", 3.0),
    ("isa总线", 3.5),
    ("固件", 3.5),
    ("firmware", 3.5),
    ("bios", 3.5),
    ("uefi", 2.5),
    ("驱动", 2.5),
    ("driver", 2.5),
    ("chipset", 3.0),
    ("芯片组", 3.0),
    ("主板", 3.0),
    ("网卡", 2.5),
    ("nic", 2.0),
    ("ethernet", 2.0),
    ("串口", 2.5),
    ("rs232", 2.5),
    ("rs485", 3.0),
    ("modbus", 3.0),
    ("研华", 4.0),
    ("advantech", 4.0),
    ("研祥", 3.5),
    ("凌华", 3.5),
    ("adlink", 3.5),
    ("ven_", 3.0),
    ("dev_", 2.5),
    ("老旧设备", 2.0),
    ("legacy", 2.0),
    ("xp", 1.5),
    ("win7", 1.2),
    ("code 43", 2.0),
    ("error 43", 2.0),
    ("找不到驱动", 2.5),
    ("missing driver", 2.5),
    ("下架", 2.0),
    ("停产", 2.5),
    ("显卡", 1.5),
    ("graphics", 1.5),
    ("打印机", 2.0),
)

CATEGORY_BONUS: dict[str, float] = {
    "老旧采集卡/工控卡": 5.0,
    "工控设备": 4.5,
    "工控/板载网卡": 3.5,
    "串口/工控通信": 3.5,
    "主板芯片组": 3.0,
    "无线网卡": 2.0,
    "显卡": 1.5,
    "品牌打印机": 2.0,
    "声卡": 1.0,
    "其他外设": 0.5,
    "其他": 0.0,
}

VALUE_SCORE_CAP = 100.0


@dataclass
class ScoreBreakdown:
    value_score: float
    blacklisted: bool
    blacklist_hits: list[str] = field(default_factory=list)
    whitelist_hits: list[dict[str, Any]] = field(default_factory=list)
    category_bonus: float = 0.0
    mention_bonus: float = 0.0
    raw_weight_sum: float = 0.0


def text_blob(*parts: Any) -> str:
    return " ".join(str(p) for p in parts if p)


def match_blacklist(text: str) -> list[str]:
    lower = text.lower()
    hits: list[str] = []
    for word in BLACKLIST_KEYWORDS:
        if word.lower() in lower:
            hits.append(word)
    return hits


def match_whitelist(text: str) -> list[dict[str, Any]]:
    lower = text.lower()
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for word, weight in WHITELIST_WEIGHTS:
        key = word.lower()
        if key in seen:
            continue
        if key in lower:
            seen.add(key)
            hits.append({"keyword": word, "weight": weight})
    return hits


def is_blacklisted(*parts: Any) -> tuple[bool, list[str]]:
    hits = match_blacklist(text_blob(*parts))
    return (bool(hits), hits)


def compute_value_score(
    *,
    title: str = "",
    raw_content: str = "",
    device_model: str = "",
    device_category: str = "",
    pain_point: str = "",
    keyword: str = "",
    error_codes: str = "",
    confidence: float = 0.0,
    extra: str = "",
) -> ScoreBreakdown:
    """
    单条价值分：
    - 黑名单命中 → 0（必须丢弃）
    - 工业/硬件白名单关键词加权求和
    - 大类加成 + 型号明确性 + 置信度微加成
    """
    blob = text_blob(
        title,
        raw_content,
        device_model,
        device_category,
        pain_point,
        keyword,
        error_codes,
        extra,
    )
    black_hits = match_blacklist(blob)
    if black_hits:
        return ScoreBreakdown(
            value_score=0.0,
            blacklisted=True,
            blacklist_hits=black_hits,
        )

    white_hits = match_whitelist(blob)
    raw_sum = sum(h["weight"] for h in white_hits)
    cat = (device_category or "").strip()
    cat_bonus = CATEGORY_BONUS.get(cat, 0.5 if cat else 0.0)

    model = (device_model or "").strip()
    model_bonus = 0.0
    if model and model not in {"未知型号", "unknown", "n/a"}:
        model_bonus = 1.5
        if re.search(r"VEN_|DEV_|RTL|BCM|PCI|DAQ", model, re.I):
            model_bonus += 1.5

    conf_bonus = max(0.0, min(1.0, confidence)) * 2.0
    total = raw_sum + cat_bonus + model_bonus + conf_bonus
    if raw_sum <= 0 and cat_bonus < 2.0:
        total *= 0.25

    total = round(min(VALUE_SCORE_CAP, max(0.0, total)), 2)
    return ScoreBreakdown(
        value_score=total,
        blacklisted=False,
        whitelist_hits=white_hits,
        category_bonus=cat_bonus,
        raw_weight_sum=round(raw_sum, 2),
    )


def score_from_row(
    row: dict[str, Any],
    *,
    device_category: str = "",
    device_model: str = "",
    pain_point: str = "",
    confidence: float = 0.0,
) -> ScoreBreakdown:
    return compute_value_score(
        title=row.get("title") or "",
        raw_content=row.get("raw_content") or "",
        device_model=device_model or (row.get("device_model") or ""),
        device_category=device_category or (row.get("device_category") or ""),
        pain_point=pain_point or (row.get("pain_point") or ""),
        keyword=row.get("keyword") or "",
        error_codes=row.get("error_codes") or "",
        confidence=confidence,
        extra=row.get("seo_title") or "",
    )


def aggregate_model_value_score(rows: list[dict[str, Any]]) -> ScoreBreakdown:
    """型号维度聚合：峰值与均值加权 + 提及加成。"""
    if not rows:
        return ScoreBreakdown(value_score=0.0, blacklisted=True)

    scores = [float(r.get("value_score") or 0.0) for r in rows]
    if max(scores) <= 0:
        return ScoreBreakdown(value_score=0.0, blacklisted=True)

    avg_score = sum(scores) / len(scores)
    peak = max(scores)
    mention_bonus = min(8.0, (len(rows) - 1) * 1.2)
    total = peak * 0.65 + avg_score * 0.35 + mention_bonus
    total = round(min(VALUE_SCORE_CAP, total), 2)

    merged_hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        blob = text_blob(
            row.get("title"),
            row.get("raw_content"),
            row.get("device_model"),
            row.get("device_category"),
            row.get("pain_point"),
            row.get("keyword"),
            row.get("error_codes"),
        )
        for hit in match_whitelist(blob):
            key = hit["keyword"].lower()
            if key not in seen:
                seen.add(key)
                merged_hits.append(hit)

    cat = (rows[0].get("device_category") or "").strip()
    return ScoreBreakdown(
        value_score=total,
        blacklisted=False,
        whitelist_hits=merged_hits[:12],
        category_bonus=CATEGORY_BONUS.get(cat, 0.0),
        mention_bonus=round(mention_bonus, 2),
        raw_weight_sum=round(sum(h["weight"] for h in merged_hits), 2),
    )
