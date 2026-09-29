"""
论坛求助帖 AI 清洗：识别设备大类、具体型号、痛点/难找原因，
并经「工业/硬件高价值驱动」打分筛选后，生成《急需设备型号白名单.json》。

筛选与打分：
1. 黑名单关键词（MSN/QQ/游戏/娱乐等）→ value_score=0，直接丢弃
2. 工业/硬件白名单关键词加权（驱动/工控/pci/采集卡/固件/bios 等）
3. 白名单 JSON 按 value_score 降序，最高价值硬件需求排最前

环境变量（OpenAI 兼容接口）：
  AI_API_KEY / OPENAI_API_KEY     必填（除非 --offline）
  AI_BASE_URL / OPENAI_BASE_URL   默认 https://api.openai.com/v1
  AI_MODEL / OPENAI_MODEL         默认 gpt-4o-mini

用法：
    python cleaner_forum.py
    python cleaner_forum.py --limit 20
    python cleaner_forum.py --offline
    python cleaner_forum.py --whitelist-only
    python cleaner_forum.py --rescore   # 对已 cleaned 记录重算分数并重建白名单
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import requests
from tqdm import tqdm

from database import Database, Status
from catalog_rules import (
    CATEGORY_PROMPT_LIST,
    is_complete_asset,
    model_key,
    normalize_category,
    normalize_model,
)
from value_scoring import (
    ScoreBreakdown,
    aggregate_model_value_score,
    score_from_row,
    text_blob,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
DEFAULT_WHITELIST_PATH = ROOT / "data" / "急需设备型号白名单.json"

SYSTEM_PROMPT = """你是「老旧驱动/工控外设」需求挖掘助手。
根据用户求助帖，提取结构化信息，只输出 JSON，不要 Markdown。

字段要求：
- device_category: 必须是以下之一（禁止「其他」「其他外设」）：
  显卡、工控/板载网卡、无线网卡、老旧采集卡/工控卡、工控设备、品牌打印机、主板芯片组、声卡、串口/工控通信、固件/BIOS、存储控制器、摄像头/采集
- device_model: 必须是具体可检索型号（如 RTL8168、GTX 1060、PCI\\VEN_xxxx&DEV_xxxx、Intel HD Graphics 4600）。
  严禁输出：未知型号、unknown、AMD64、ARM64、SHA-256、其他外设。
  若正文只有硬件 ID，就用工控 ID 作为型号。
- pain_point: 用户痛点/报错原因（1-2 句中文，尽量完整）
- error_codes: 字符串数组；没有则 []
- confidence: 0~1

若无法确定具体型号与合法大类，输出：
{"device_category":"","device_model":"","pain_point":"信息不足无法定位型号","error_codes":[],"confidence":0}
系统会丢弃空型号记录，不要硬凑「未知型号」。
"""


# 黑名单/白名单/打分算法统一见 value_scoring.py（与 cleaner.py 共用）


@dataclass
class AnalysisResult:
    device_category: str
    device_model: str
    pain_point: str
    error_codes: list[str] = field(default_factory=list)
    confidence: float = 0.0
    value_score: float = 0.0
    score_detail: Optional[ScoreBreakdown] = None
    discarded: bool = False


def _env(name: str, *aliases: str, default: str = "") -> str:
    for key in (name, *aliases):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return default


def load_ai_config() -> dict[str, str]:
    return {
        "api_key": _env("AI_API_KEY", "OPENAI_API_KEY"),
        "base_url": _env(
            "AI_BASE_URL",
            "OPENAI_BASE_URL",
            default="https://api.openai.com/v1",
        ).rstrip("/"),
        "model": _env("AI_MODEL", "OPENAI_MODEL", default="gpt-4o-mini"),
    }


def _strip_json_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_ai_json(text: str) -> AnalysisResult:
    data = json.loads(_strip_json_fence(text))
    codes = data.get("error_codes") or []
    if isinstance(codes, str):
        codes = [c.strip() for c in re.split(r"[|,;/]+", codes) if c.strip()]
    blob = " ".join(
        [
            str(data.get("device_category") or ""),
            str(data.get("device_model") or ""),
            str(data.get("pain_point") or ""),
        ]
    )
    category = normalize_category(str(data.get("device_category") or ""), context=blob)
    model = normalize_model(str(data.get("device_model") or ""), context=blob)
    return AnalysisResult(
        device_category=category or "",
        device_model=model or "",
        pain_point=str(data.get("pain_point") or "信息不足").strip() or "信息不足",
        error_codes=[str(c).strip() for c in codes if str(c).strip()],
        confidence=float(data.get("confidence") or 0.0),
    )


def analyze_with_ollama(
    *,
    title: str,
    body: str,
    keyword: str,
    error_codes_hint: str,
    timeout: float = 25.0,
) -> AnalysisResult:
    """本地 Ollama 清洗论坛帖（与 bulk AI 同源）。"""
    from ai_cleaner import get_ollama_client

    client = get_ollama_client(timeout=timeout)
    user_prompt = (
        f"检索关键词: {keyword}\n"
        f"已粗提报错码: {error_codes_hint or '无'}\n"
        f"允许大类: {CATEGORY_PROMPT_LIST}\n"
        f"标题: {title or '无'}\n"
        f"正文:\n{(body or '')[:6000]}\n"
    )
    data = client.chat_json(SYSTEM_PROMPT, user_prompt, temperature=0.15)
    if not data:
        raise RuntimeError("Ollama 无响应")
    # 复用解析
    return parse_ai_json(json.dumps(data, ensure_ascii=False))


def analyze_with_ai(
    *,
    title: str,
    body: str,
    keyword: str,
    error_codes_hint: str,
    config: dict[str, str],
    timeout: float = 60.0,
) -> AnalysisResult:
    if not config["api_key"]:
        raise RuntimeError("未配置 AI_API_KEY / OPENAI_API_KEY")

    user_prompt = (
        f"检索关键词: {keyword}\n"
        f"已粗提报错码: {error_codes_hint or '无'}\n"
        f"标题: {title or '无'}\n"
        f"正文:\n{(body or '')[:6000]}\n"
    )
    url = f"{config['base_url']}/chat/completions"
    payload = {
        "model": config["model"],
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {config['api_key']}",
        "Content-Type": "application/json",
    }
    session = requests.Session()
    session.trust_env = False
    try:
        resp = session.post(url, headers=headers, json=payload, timeout=timeout)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return parse_ai_json(content)
    finally:
        session.close()


_CATEGORY_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"打印机|LBP|LaserJet|Canon|Epson|HP\s*Laser", re.I), "品牌打印机"),
    (re.compile(r"采集卡|DAQ|数据采集|运动控制|工控卡|PCI[eE]?\s*卡", re.I), "老旧采集卡/工控卡"),
    (re.compile(r"工控|Advantech|研华|研祥|ADLINK|凌华", re.I), "工控设备"),
    (re.compile(r"RTL|Realtek|网卡|Ethernet|NIC", re.I), "工控/板载网卡"),
    (re.compile(r"Wi-?Fi|无线|BCM|Atheros|无线网卡", re.I), "无线网卡"),
    (
        re.compile(r"GTX|RTX|Radeon|GeForce|显卡|Graphics|GPU|显示适配器", re.I),
        "显卡",
    ),
    (re.compile(r"芯片组|Chipset|主板", re.I), "主板芯片组"),
    (re.compile(r"声卡|Audio|Realtek High Definition", re.I), "声卡"),
    (re.compile(r"RS232|串口|COM\s*\d", re.I), "串口/工控通信"),
]

_MODEL_FALLBACK = re.compile(
    r"(?:PCI\\VEN_[0-9A-F]{4}&DEV_[0-9A-F]{4}"
    r"|RTL\s?8\d{3}[A-Z0-9]*"
    r"|GTX\s?\d{3,4}"
    r"|RTX\s?\d{3,4}"
    r"|LBP\d{3,5}"
    r"|BCM\d{4,5}"
    r"|HD\s?Graphics\s?\d{3,4})",
    re.I,
)


def analyze_offline(row: dict[str, Any]) -> AnalysisResult:
    blob = text_blob(
        row.get("title"),
        row.get("raw_content"),
        row.get("device_model"),
        row.get("error_codes"),
        row.get("keyword"),
    )
    category = normalize_category("", context=blob) or ""
    model = normalize_model(
        (row.get("device_model") or "").split("|")[0].strip(),
        context=blob,
    ) or ""

    codes = []
    raw_codes = row.get("error_codes") or ""
    if raw_codes:
        codes = [c.strip() for c in re.split(r"\s*\|\s*", raw_codes) if c.strip()]

    pains: list[str] = []
    if re.search(r"找不到|找不到驱动|no driver|missing driver|下架|停产", blob, re.I):
        pains.append("官方驱动难寻或已下架")
    if re.search(r"Error\s*43|Code\s*43|错误\s*43", blob, re.I):
        pains.append("出现 Code 43，设备被系统禁用")
    if re.search(r"XP|Win7|老旧|legacy|旧设备", blob, re.I):
        pains.append("系统过旧，新驱动不兼容")
    if re.search(r"工控|采集|产线|机器", blob, re.I):
        pains.append("工控场景停机风险高，急需可用驱动")
    if not pains:
        pains.append("求助帖反馈驱动安装或匹配困难")

    conf = 0.45 if is_complete_asset(category, model) else 0.1
    return AnalysisResult(
        device_category=category,
        device_model=model,
        pain_point="；".join(pains),
        error_codes=codes,
        confidence=conf,
    )


def apply_value_filter(row: dict[str, Any], result: AnalysisResult) -> AnalysisResult:
    """对分析结果打分；黑名单或信息不完整直接丢弃。"""
    # 分类/型号不完整 → 强制丢弃，禁止「未知/其他外设」入库
    if not is_complete_asset(result.device_category, result.device_model):
        result.value_score = 0.0
        result.discarded = True
        result.confidence = min(result.confidence, 0.05)
        result.pain_point = (result.pain_point or "") + "（型号/分类不完整已丢弃）"
        return result

    detail = score_from_row(
        row,
        device_category=result.device_category,
        device_model=result.device_model,
        pain_point=result.pain_point,
        confidence=result.confidence,
    )
    result.score_detail = detail
    result.value_score = detail.value_score
    result.discarded = detail.blacklisted or detail.value_score <= 0
    return result


def normalize_model_key(model: str) -> str:
    return model_key(model) or ""


def heat_score(rows: list[dict[str, Any]]) -> float:
    """辅助热度（保留字段，排序以 value_score 为准）。"""
    score = float(len(rows))
    for row in rows:
        blob = f"{row.get('pain_point') or ''} {row.get('error_codes') or ''} {row.get('keyword') or ''}"
        if re.search(r"43", blob):
            score += 1.5
        if re.search(r"找不到|下架|停产|急需|工控", blob):
            score += 1.0
        if (row.get("keyword") or "") in {"Error 43", "工控卡 报错", "找不到驱动"}:
            score += 0.5
    return round(score, 2)


def build_whitelist(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """只纳入完整型号+合法大类且 value_score>0；同型号去重聚合。"""
    eligible: list[dict[str, Any]] = []
    for r in rows:
        if float(r.get("value_score") or 0) <= 0:
            continue
        cat = normalize_category(
            str(r.get("device_category") or ""),
            context=f"{r.get('device_model')} {r.get('pain_point')}",
        )
        model = normalize_model(
            str(r.get("device_model") or ""),
            context=f"{r.get('title')} {r.get('pain_point')} {r.get('error_codes')}",
        )
        if not is_complete_asset(cat, model):
            continue
        item = dict(r)
        item["device_category"] = cat
        item["device_model"] = model
        eligible.append(item)
    discarded_count = len(rows) - len(eligible)

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in eligible:
        key = normalize_model_key(str(row.get("device_model") or ""))
        if not key:
            continue
        groups[key].append(row)

    items: list[dict[str, Any]] = []
    for _key, group in groups.items():
        sample = group[0]
        pain_points: list[str] = []
        seen_pain: set[str] = set()
        error_set: list[str] = []
        seen_err: set[str] = set()
        urls: list[str] = []

        for row in group:
            pain = (row.get("pain_point") or "").strip()
            if pain and pain not in seen_pain:
                seen_pain.add(pain)
                pain_points.append(pain)
            for code in re.split(r"\s*\|\s*", row.get("error_codes") or ""):
                code = code.strip()
                if code and code.lower() not in seen_err:
                    seen_err.add(code.lower())
                    error_set.append(code)
            url = row.get("source_url") or ""
            if url and url not in urls:
                urls.append(url)

        agg = aggregate_model_value_score(group)
        model_name = str(sample.get("device_model") or "").strip()
        category = str(sample.get("device_category") or "").strip()
        if not is_complete_asset(category, model_name):
            continue
        items.append(
            {
                "device_category": category,
                "device_model": model_name,
                "value_score": agg.value_score,
                "heat_score": heat_score(group),
                "mention_count": len(group),
                "whitelist_hits": [h["keyword"] for h in agg.whitelist_hits],
                "pain_points": pain_points[:5],
                "error_codes": error_set[:8],
                "sample_urls": urls[:5],
            }
        )

    items.sort(
        key=lambda x: (
            x["value_score"],
            x["heat_score"],
            x["mention_count"],
            x["device_model"],
        ),
        reverse=True,
    )
    for idx, item in enumerate(items, start=1):
        item["rank"] = idx

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "title": "急需设备型号白名单",
        "sort_by": "value_score_desc",
        "scoring_model": "industrial_hardware_v1_strict_catalog",
        "total_cleaned_posts": len(rows),
        "eligible_posts": len(eligible),
        "discarded_posts": discarded_count,
        "total_models": len(items),
        "items": items,
    }


def write_whitelist(data: dict[str, Any], path: Path) -> Path:
    from json_io import write_json

    write_json(path, data)
    return path


def _persist_result(
    db: Database,
    item_id: int,
    row: dict[str, Any],
    result: AnalysisResult,
) -> str:
    """写入分析结果；返回 cleaned / discarded / error。"""
    result = apply_value_filter(row, result)
    merged_codes = result.error_codes
    if not merged_codes and row.get("error_codes"):
        merged_codes = [
            c.strip()
            for c in re.split(r"\s*\|\s*", row["error_codes"])
            if c.strip()
        ]
    codes_text = " | ".join(merged_codes) if merged_codes else None

    if result.discarded:
        reason = "黑名单过滤"
        if result.score_detail and result.score_detail.blacklist_hits:
            reason = "黑名单: " + ", ".join(result.score_detail.blacklist_hits[:6])
        db.update_forum_analysis(
            item_id,
            device_category=result.device_category,
            device_model=result.device_model,
            pain_point=f"[已丢弃] {reason}；原痛点: {result.pain_point}",
            error_codes=codes_text,
            value_score=0.0,
            status=Status.DELETED,
        )
        return "discarded"

    db.update_forum_analysis(
        item_id,
        device_category=result.device_category,
        device_model=result.device_model,
        pain_point=result.pain_point,
        error_codes=codes_text,
        value_score=result.value_score,
        status=Status.CLEANED,
    )
    return "cleaned"


def run_analysis(
    db: Database,
    *,
    limit: int,
    offline: bool,
    config: dict[str, str],
    should_stop: Optional[Callable[[], bool]] = None,
) -> dict[str, int]:
    rows = db.list_forum_posts(status=Status.PENDING, limit=limit)
    stats = {"total": len(rows), "cleaned": 0, "discarded": 0, "failed": 0}

    for row in tqdm(rows, desc="AI/规则清洗论坛帖", unit="帖"):
        if should_stop and should_stop():
            logger.info("run_analysis: 用户暂停，提前结束清洗")
            stats["paused"] = 1
            break
        item_id = int(row["id"])
        try:
            print(
                f"[正在清洗] 标题: {(row.get('title') or '')[:80]} | 来源: {row.get('source') or '-'}",
                flush=True,
            )
            # 分析前先做黑名单快筛，省 AI 调用
            pre = score_from_row(row)
            if pre.blacklisted:
                db.update_forum_analysis(
                    item_id,
                    device_category="娱乐/非目标软件",
                    device_model=row.get("device_model") or "未知型号",
                    pain_point="[已丢弃] 黑名单: "
                    + ", ".join(pre.blacklist_hits[:6]),
                    value_score=0.0,
                    status=Status.DELETED,
                )
                stats["discarded"] += 1
                logger.info(
                    "丢弃 id=%s 黑名单命中=%s",
                    item_id,
                    pre.blacklist_hits[:6],
                )
                continue

            # prefer: 显式 offline → 规则；否则 Ollama → 云端 → 规则
            if offline:
                result = analyze_offline(row)
            else:
                used = False
                try:
                    from ai_cleaner import get_ollama_client

                    if get_ollama_client().probe():
                        result = analyze_with_ollama(
                            title=row.get("title") or "",
                            body=row.get("raw_content") or "",
                            keyword=row.get("keyword") or "",
                            error_codes_hint=row.get("error_codes") or "",
                            timeout=float(
                                __import__("os").environ.get(
                                    "PIPELINE_OLLAMA_TIMEOUT", "25"
                                )
                            ),
                        )
                        used = True
                except Exception as exc:
                    logger.info("Ollama 论坛清洗不可用 id=%s: %s", item_id, exc)
                if not used and config.get("api_key"):
                    result = analyze_with_ai(
                        title=row.get("title") or "",
                        body=row.get("raw_content") or "",
                        keyword=row.get("keyword") or "",
                        error_codes_hint=row.get("error_codes") or "",
                        config=config,
                    )
                    used = True
                if not used:
                    result = analyze_offline(row)

            outcome = _persist_result(db, item_id, row, result)
            if outcome == "cleaned":
                stats["cleaned"] += 1
                logger.info(
                    "已清洗 id=%s [%s] %s | score=%.2f | %s",
                    item_id,
                    result.device_category,
                    result.device_model,
                    result.value_score,
                    result.pain_point[:50],
                )
            else:
                stats["discarded"] += 1
                logger.info(
                    "丢弃 id=%s score=0 model=%s",
                    item_id,
                    result.device_model,
                )
        except Exception as exc:
            stats["failed"] += 1
            logger.exception("分析失败 id=%s: %s", item_id, exc)
            db.update_forum_analysis(
                item_id,
                device_category="",
                device_model="",
                pain_point=f"分析失败: {exc}",
                value_score=0.0,
                status=Status.DELETED,
            )

    return stats


def rescore_cleaned(db: Database) -> dict[str, int]:
    """对已 cleaned 记录重算；信息不完整或黑名单 → deleted。"""
    rows = db.list_forum_cleaned(limit=10000)
    stats = {"total": len(rows), "updated": 0, "discarded": 0}
    for row in tqdm(rows, desc="重算论坛价值分", unit="帖"):
        item_id = int(row["id"])
        blob = f"{row.get('title')} {row.get('pain_point')} {row.get('error_codes')}"
        cat = normalize_category(str(row.get("device_category") or ""), context=blob)
        model = normalize_model(str(row.get("device_model") or ""), context=blob)
        if not is_complete_asset(cat, model):
            db.update_forum_analysis(
                item_id,
                device_category=cat or "",
                device_model=model or "",
                pain_point=f"[已丢弃-重算] 分类/型号不完整；原: {row.get('pain_point') or ''}",
                value_score=0.0,
                status=Status.DELETED,
            )
            stats["discarded"] += 1
            continue
        detail = score_from_row(
            row,
            device_category=cat,
            device_model=model,
            pain_point=row.get("pain_point") or "",
            confidence=0.5,
        )
        if detail.blacklisted or detail.value_score <= 0:
            db.update_forum_analysis(
                item_id,
                device_category=cat,
                device_model=model,
                pain_point=f"[已丢弃-重算] 黑名单/零分；原: {row.get('pain_point') or ''}",
                value_score=0.0,
                status=Status.DELETED,
            )
            stats["discarded"] += 1
        else:
            db.update_forum_analysis(
                item_id,
                device_category=cat,
                device_model=model,
                pain_point=row.get("pain_point") or "",
                error_codes=row.get("error_codes"),
                value_score=detail.value_score,
                status=Status.CLEANED,
            )
            stats["updated"] += 1
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AI 清洗论坛求助帖（工业硬件价值打分）并生成急需设备型号白名单",
    )
    parser.add_argument("--limit", type=int, default=50, help="本次最多分析 pending 条数")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="不调用 AI，使用本地规则提取（无密钥/调试）",
    )
    parser.add_argument(
        "--whitelist-only",
        action="store_true",
        help="跳过分析，仅根据已 cleaned 数据重算白名单",
    )
    parser.add_argument(
        "--rescore",
        action="store_true",
        help="对已 cleaned 数据按新模型重打分，并重建白名单",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_WHITELIST_PATH,
        help="白名单 JSON 输出路径",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = load_ai_config()
    with Database() as db:
        stats: dict[str, int] = {"failed": 0}

        if args.rescore:
            stats = rescore_cleaned(db)
            logger.info(
                "重算结束: total=%s updated=%s discarded=%s",
                stats["total"],
                stats["updated"],
                stats["discarded"],
            )
        elif not args.whitelist_only:
            stats = run_analysis(
                db,
                limit=max(1, args.limit),
                offline=args.offline,
                config=config,
            )
            logger.info(
                "分析结束: total=%s cleaned=%s discarded=%s failed=%s",
                stats["total"],
                stats["cleaned"],
                stats["discarded"],
                stats["failed"],
            )

        cleaned = db.list_forum_cleaned(limit=10000)
        whitelist = build_whitelist(cleaned)
        out = write_whitelist(whitelist, args.output)
        logger.info(
            "白名单已生成: %s | models=%s eligible=%s discarded_posts=%s",
            out,
            whitelist["total_models"],
            whitelist["eligible_posts"],
            whitelist["discarded_posts"],
        )
        if whitelist["items"]:
            top = whitelist["items"][0]
            logger.info(
                "价值 Top1: [%s] %s (value_score=%s, mentions=%s)",
                top["device_category"],
                top["device_model"],
                top["value_score"],
                top["mention_count"],
            )

    return 0 if stats.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
