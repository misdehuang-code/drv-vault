"""
本地 Ollama AI 清洗 / SEO Agent（默认 qwen2.5 @ localhost:11434）。

Link Curation 主路径由 link_curator / commercial_pipeline 调用 seo_with_ai。
本模块仍保留对单目录 metadata 的 CLI 工具（遗留，可选）。

用法：
    python ai_cleaner.py --probe
    python ai_cleaner.py --device-dir <optional_legacy_dir>
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

from catalog_rules import (
    CATEGORY_PROMPT_LIST,
    is_complete_asset,
    normalize_category,
    normalize_model,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
DEFAULT_OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
DEFAULT_OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
# 管线默认缩短超时，避免「暂停」卡在单次 chat 上；CLI 仍可读 OLLAMA_TIMEOUT
DEFAULT_TIMEOUT = float(
    os.environ.get("PIPELINE_OLLAMA_TIMEOUT")
    or os.environ.get("OLLAMA_TIMEOUT")
    or "25"
)
CLI_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "120"))
PROBE_CACHE_SECONDS = 30.0
# 本机已拉取模型时，按「能力」优先选用（越大越靠前）
_MODEL_PREFERENCE = (
    "qwen2.5:32b",
    "qwen2.5:14b",
    "qwen2.5-coder:14b",
    "qwen2.5:7b",
    "qwen2.5-coder:7b",
    "llama3.1:8b",
    "llama3.2:3b",
    "qwen2.5:3b",
    "mistral",
    "gemma2",
)
JUNK_DIR_NAME = "_junk"
METADATA_NAME = "metadata.json"
META_LEGACY_NAME = "meta.json"

TEXT_EXTS = {".txt", ".md", ".nfo", ".inf", ".ini", ".cfg", ".html", ".htm", ".log", ".xml"}
MAX_CONTEXT_CHARS = 7000
MAX_FILE_LIST = 100

_CATEGORY_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"bios|uefi|ami\s*bios|award", re.I), "BIOS"),
    (re.compile(r"打印机|printer|LBP|LaserJet|Canon|Epson", re.I), "品牌打印机"),
    (re.compile(r"采集卡|DAQ|运动控制|工控卡|PCI[eE]?\s*卡", re.I), "老旧采集卡/工控卡"),
    (re.compile(r"工控|Advantech|研华|ADLINK|凌华", re.I), "工控设备"),
    (re.compile(r"RTL|Realtek|网卡|Ethernet|NIC|LAN", re.I), "工控/板载网卡"),
    (re.compile(r"Wi-?Fi|无线|WLAN|Atheros|Broadcom\s*Wireless", re.I), "无线网卡"),
    (re.compile(r"GTX|RTX|Radeon|GeForce|显卡|Graphics|GPU", re.I), "显卡"),
    (re.compile(r"芯片组|Chipset|主板|motherboard", re.I), "主板芯片组"),
    (re.compile(r"声卡|Audio|HD\s*Audio", re.I), "声卡"),
    (re.compile(r"RS232|串口|COM\s*\d|USB.?Serial", re.I), "串口/工控通信"),
    (re.compile(r"固件|firmware|\.rom|\.bin", re.I), "固件"),
]

_OS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bwindows\s*xp\b|\bxp\b", re.I), "Windows XP"),
    (re.compile(r"\bwindows\s*7\b|\bwin\s*7\b", re.I), "Windows 7"),
    (re.compile(r"\bwindows\s*8\.?1?\b|\bwin\s*8", re.I), "Windows 8/8.1"),
    (re.compile(r"\bwindows\s*10\b|\bwin\s*10\b", re.I), "Windows 10"),
    (re.compile(r"\bwindows\s*11\b|\bwin\s*11\b", re.I), "Windows 11"),
    (re.compile(r"\bwindows\s*server\b", re.I), "Windows Server"),
    (re.compile(r"\blinux\b", re.I), "Linux"),
]

_JUNK_NAME_RE = re.compile(
    r"(advert|adware|toolbar|offer|coupon|bundleware|sponsor|promo|"
    r"installer_ad|setup_ad|open\.candy|clickhere|tracker|"
    r"thumbs\.db|desktop\.ini|\.ds_store|__macosx|\.url$|\.lnk$)",
    re.I,
)

_ERROR_CODE_RE = re.compile(
    r"\b(?:Code|Error)\s*[:#]?\s*(?:10|43|31|28|19|39)\b|"
    r"\b0x[0-9A-Fa-f]{8}\b|"
    r"\bIRQL_NOT_LESS_OR_EQUAL\b|"
    r"\bVIDEO_TDR_FAILURE\b",
    re.I,
)

CLASSIFY_SYSTEM = f"""You are an industrial/legacy hardware driver asset classifier.
Read the provided archive text / README / file list and output ONLY valid JSON (no markdown).

Required fields:
- hardware_category: MUST be one of: {CATEGORY_PROMPT_LIST}
  Forbidden: 其他外设, 其他, 未分类, unknown
- exact_model: precise searchable model (RTL8168, GTX 1060, PCI\\VEN_xxxx&DEV_xxxx). 
  Forbidden: 未知型号, unknown, AMD64, ARM64, SHA-256
- os_support: array of OS names
- confidence: 0~1
- notes: short Chinese note

If you cannot determine a valid category AND exact model, return empty strings for those fields.
"""


SEO_SYSTEM = """You write high-converting English SEO metadata for a global geek/driver archive site.
Output ONLY valid JSON (no markdown).

Required fields:
- seo_title: catchy English title under 70 chars, include model + OS or error fix when possible
- pain_points: array of 1-3 English strings describing pain (e.g. fixes Code 43 / Code 10 / missing driver on XP)
- tags: array of 4-10 lowercase English keyword tags
"""

JUNK_SYSTEM = """You audit extracted driver package file names for adware/PUA/junk.
Output ONLY valid JSON (no markdown).

Required fields:
- junk_files: array of relative paths that look like ads, toolbars, bundleware, fake installers, trackers, or useless junk
- keep_files: array of relative paths that look like real drivers (inf/sys/cat/dll) or useful docs
- reason: short English explanation

Be conservative: only flag obvious junk. Prefer keeping .inf .sys .cat .dll driver files.
"""


@dataclass
class AssetAIResult:
    hardware_category: str = "其他外设"
    exact_model: str = "未知型号"
    os_support: list[str] = field(default_factory=lambda: ["通用"])
    seo_title: str = ""
    pain_points: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    junk_files: list[str] = field(default_factory=list)
    confidence: float = 0.0
    ai_used: bool = False
    ai_backend: str = "regex_fallback"
    notes: str = ""
    source_url: str = ""
    updated_at: str = ""

    def to_metadata(self) -> dict[str, Any]:
        return {
            "hardware_category": self.hardware_category,
            "exact_model": self.exact_model,
            "device_name": self.exact_model,
            "os_support": self.os_support,
            "os_version": " / ".join(self.os_support) if self.os_support else "通用",
            "seo_title": self.seo_title,
            "pain_points": self.pain_points,
            "tags": self.tags,
            "junk_files": self.junk_files,
            "confidence": self.confidence,
            "ai_used": self.ai_used,
            "ai_backend": self.ai_backend,
            "notes": self.notes,
            "source_url": self.source_url,
            "updated_at": self.updated_at
            or datetime.now(timezone.utc).isoformat(),
        }


class OllamaClient:
    """轻量 Ollama 客户端；不可达时不抛致命错误。"""

    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_URL,
        model: str = DEFAULT_OLLAMA_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._lock = threading.Lock()
        self._available: Optional[bool] = None
        self._checked_at = 0.0
        self._resolved_model: Optional[str] = None

    def _list_models(self, session: requests.Session) -> list[str]:
        resp = session.get(f"{self.base_url}/api/tags", timeout=3.0)
        resp.raise_for_status()
        models = resp.json().get("models") or []
        names: list[str] = []
        for m in models:
            name = str(m.get("name") or m.get("model") or "").strip()
            if name:
                names.append(name)
        return names

    def resolve_model(self, session: Optional[requests.Session] = None) -> str:
        """解析本机最佳可用标签：尊重 OLLAMA_MODEL，否则按能力偏好挑最大可用。"""
        if self._resolved_model:
            return self._resolved_model
        own = session is None
        sess = session or requests.Session()
        if own:
            sess.trust_env = False
        try:
            names = self._list_models(sess)
        except Exception:
            self._resolved_model = self.model
            return self.model
        finally:
            if own:
                sess.close()

        if not names:
            self._resolved_model = self.model
            return self.model

        wanted = self.model.strip()
        lower_map = {n.lower(): n for n in names}

        def _pick(label: str) -> Optional[str]:
            if label.lower() in lower_map:
                return lower_map[label.lower()]
            prefix = label.split(":")[0].lower()
            # 同族里挑参数量最大的（:32b > :14b > :7b）
            family = [
                n
                for n in names
                if n.lower() == prefix or n.lower().startswith(prefix + ":")
            ]
            if not family:
                return None

            def _size_key(n: str) -> tuple:
                m = re.search(r":(\d+)\s*b\b", n.lower())
                return (int(m.group(1)) if m else 0, len(n))

            return sorted(family, key=_size_key, reverse=True)[0]

        # 1) 用户指定（可被映射到本机更大同族）
        if wanted:
            hit = _pick(wanted)
            if hit:
                self._resolved_model = hit
                if hit != wanted:
                    logger.info("Ollama 模型自动映射 %s -> %s", wanted, hit)
                return hit

        # 2) 本机已装模型里按偏好表选「最好」
        for pref in _MODEL_PREFERENCE:
            hit = _pick(pref)
            if hit:
                self._resolved_model = hit
                logger.info("Ollama 选用本机最佳模型 %s (请求的是 %s)", hit, wanted)
                return hit

        # 3) 任意 qwen / llama
        for name in names:
            low = name.lower()
            if "qwen" in low or "llama" in low or "mistral" in low:
                self._resolved_model = name
                logger.info("Ollama 模型回退选用 %s (请求的是 %s)", name, wanted)
                return name

        self._resolved_model = names[0]
        return names[0]

    def probe(self, force: bool = False) -> bool:
        now = time.monotonic()
        with self._lock:
            if (
                not force
                and self._available is not None
                and (now - self._checked_at) < PROBE_CACHE_SECONDS
            ):
                return self._available
        ok = False
        try:
            session = requests.Session()
            session.trust_env = False
            names = self._list_models(session)
            ok = bool(names)
            if ok:
                self.resolve_model(session)
            session.close()
        except Exception as exc:
            logger.info("Ollama 不可达 (%s): %s", self.base_url, exc)
            ok = False
        with self._lock:
            self._available = ok
            self._checked_at = time.monotonic()
        return ok

    def chat_json(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
    ) -> Optional[dict[str, Any]]:
        if not self.probe():
            return None
        session = requests.Session()
        session.trust_env = False
        try:
            model = self.resolve_model(session)
            payload = {
                "model": model,
                "stream": False,
                "format": "json",
                "options": {"temperature": temperature},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user[:MAX_CONTEXT_CHARS]},
                ],
            }
            resp = session.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                logger.warning(
                    "Ollama /api/chat HTTP %s: %s",
                    resp.status_code,
                    resp.text[:300],
                )
                with self._lock:
                    # 模型错误不永久拉黑服务；其它错误短暂标不可用
                    if resp.status_code >= 500:
                        self._available = False
                        self._checked_at = time.monotonic()
                return None
            content = (resp.json().get("message") or {}).get("content") or ""
            return _parse_json_object(content)
        except Exception as exc:
            logger.warning("Ollama 调用失败，将降级正则: %s", exc)
            return None
        finally:
            session.close()


_default_client: Optional[OllamaClient] = None
_client_lock = threading.Lock()


def get_ollama_client(
    *,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
) -> OllamaClient:
    global _default_client
    with _client_lock:
        if _default_client is None or base_url or model or timeout:
            _default_client = OllamaClient(
                base_url=base_url or DEFAULT_OLLAMA_URL,
                model=model or DEFAULT_OLLAMA_MODEL,
                timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
            )
        return _default_client


def _parse_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("AI 返回非 JSON 对象")
    return data


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,;|/]+", value)
        return [p.strip() for p in parts if p.strip()]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            s = str(item).strip()
            if s:
                out.append(s)
        return out
    return [str(value).strip()] if str(value).strip() else []


def collect_device_context(device_dir: Path) -> tuple[str, list[str]]:
    """收集 README/说明文本 + 相对文件列表。"""
    texts: list[str] = []
    files: list[str] = []
    if not device_dir.exists():
        return "", []

    for path in sorted(device_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(device_dir)).replace("\\", "/")
        if rel.startswith(f"{JUNK_DIR_NAME}/"):
            continue
        if path.name in {METADATA_NAME, META_LEGACY_NAME}:
            continue
        files.append(rel)
        if path.suffix.lower() in TEXT_EXTS and path.stat().st_size < 200_000:
            try:
                raw = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            snippet = raw.strip()
            if snippet:
                texts.append(f"----- FILE: {rel} -----\n{snippet[:2500]}")

    files = files[:MAX_FILE_LIST]
    blob = "\n\n".join(texts)[:MAX_CONTEXT_CHARS]
    return blob, files


def regex_fallback(
    *,
    text: str,
    file_list: list[str],
    hint_category: str = "",
    hint_model: str = "",
    hint_os: str = "",
    hint_pains: Optional[list[str]] = None,
) -> AssetAIResult:
    blob = " ".join(
        [
            text,
            " ".join(file_list),
            hint_category,
            hint_model,
            hint_os,
            " ".join(hint_pains or []),
        ]
    )
    category = normalize_category(hint_category, context=blob) or normalize_category(
        "", context=blob
    )
    model = normalize_model(hint_model, context=blob) or normalize_model("", context=blob)
    if not category:
        category = "工控设备"  # 最后保底：仍禁止「其他外设」；仅当完全无信号时用工控设备+从文件名挖型号
    if not model:
        for name in file_list[:30]:
            stem = Path(name).stem
            cand = normalize_model(stem, context=blob)
            if cand:
                model = cand
                break
    if not model:
        model = sanitize_fallback_model(hint_model or Path(file_list[0]).stem if file_list else "DriverPack")

    os_support = _as_str_list(hint_os) if hint_os and hint_os != "通用" else []
    for pat, label in _OS_PATTERNS:
        if pat.search(blob) and label not in os_support:
            os_support.append(label)
    if not os_support:
        os_support = ["通用"]

    codes = sorted({m.group(0) for m in _ERROR_CODE_RE.finditer(blob)})
    pains = list(hint_pains or [])
    if codes and not pains:
        pains = [f"Fixes classic Windows device error {c}" for c in codes[:2]]
    if not pains:
        pains = [f"Legacy {category} driver package for {model}"]

    junk = [f for f in file_list if _JUNK_NAME_RE.search(f)]
    tags = [
        "driver",
        "legacy",
        "windows",
        (category or "hardware").lower().replace("/", "-"),
        (model or "driver").lower().replace(" ", "-"),
    ]
    for c in codes[:3]:
        tags.append(re.sub(r"\s+", "-", c.lower()))

    seo_title = f"{model} Driver Download — {os_support[0]} Legacy Fix"
    if codes:
        seo_title = f"{model} Driver Fix {codes[0]} for {os_support[0]}"

    return AssetAIResult(
        hardware_category=category or "工控设备",
        exact_model=model or "DriverPack",
        os_support=os_support,
        seo_title=seo_title[:70],
        pain_points=pains[:3],
        tags=tags[:10],
        junk_files=junk,
        confidence=0.35 if is_complete_asset(category, model) else 0.2,
        ai_used=False,
        ai_backend="regex_fallback",
        notes="Ollama unavailable or timed out; used regex fallback",
    )


def sanitize_fallback_model(name: str) -> str:
    name = re.sub(r"[^\w\-\.]+", " ", name or "").strip()
    name = re.sub(r"\s+", " ", name)
    bad = {"unknown", "未知型号", "未知设备", "other", "其他外设"}
    if not name or name.lower() in bad:
        return "DriverPack"
    return name[:80]


def classify_with_ai(
    client: OllamaClient,
    *,
    text: str,
    file_list: list[str],
    hint_category: str = "",
    hint_model: str = "",
    hint_os: str = "",
) -> Optional[dict[str, Any]]:
    user = (
        f"Hints:\n"
        f"- category: {hint_category or 'unknown'}\n"
        f"- model: {hint_model or 'unknown'}\n"
        f"- os hint: {hint_os or 'unknown'}\n\n"
        f"File list ({len(file_list)}):\n"
        + "\n".join(f"- {f}" for f in file_list[:MAX_FILE_LIST])
        + "\n\nText excerpts:\n"
        + (text or "(empty)")
    )
    return client.chat_json(CLASSIFY_SYSTEM, user)


def seo_with_ai(
    client: OllamaClient,
    *,
    hardware_category: str,
    exact_model: str,
    os_support: list[str],
    text: str,
    hint_pains: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    user = (
        f"Category: {hardware_category}\n"
        f"Model: {exact_model}\n"
        f"OS: {', '.join(os_support)}\n"
        f"Known pains: {'; '.join(hint_pains or []) or 'n/a'}\n"
        f"Context:\n{(text or '')[:3000]}"
    )
    return client.chat_json(SEO_SYSTEM, user, temperature=0.4)


def junk_with_ai(
    client: OllamaClient,
    *,
    file_list: list[str],
) -> Optional[dict[str, Any]]:
    if not file_list:
        return {"junk_files": [], "keep_files": [], "reason": "empty"}
    user = "Files:\n" + "\n".join(f"- {f}" for f in file_list[:MAX_FILE_LIST])
    return client.chat_json(JUNK_SYSTEM, user)


def move_junk_files(device_dir: Path, junk_rels: list[str]) -> list[str]:
    """把标记的垃圾文件移入 _junk/，返回实际移动路径。"""
    moved: list[str] = []
    junk_root = device_dir / JUNK_DIR_NAME
    for rel in junk_rels:
        rel_norm = rel.replace("\\", "/").lstrip("/")
        if not rel_norm or rel_norm.startswith(f"{JUNK_DIR_NAME}/"):
            continue
        src = device_dir / rel_norm
        if not src.is_file():
            continue
        # 不移动驱动核心文件，除非名字本身明显是广告
        if src.suffix.lower() in {".inf", ".sys", ".cat"} and not _JUNK_NAME_RE.search(
            src.name
        ):
            continue
        dest = junk_root / rel_norm
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            if dest.exists():
                dest.unlink()
            shutil.move(str(src), str(dest))
            moved.append(rel_norm)
        except Exception as exc:
            logger.warning("移动垃圾文件失败 %s: %s", rel_norm, exc)
    return moved


def enrich_device_asset(
    device_dir: Path,
    *,
    hint_category: str = "",
    hint_model: str = "",
    hint_os: str = "",
    hint_pains: Optional[list[str]] = None,
    source_url: str = "",
    client: Optional[OllamaClient] = None,
    use_ai: bool = True,
) -> AssetAIResult:
    """
    对单个设备目录做 AI 清洗（失败自动正则兜底），写入 metadata.json。
    """
    device_dir = Path(device_dir)
    text, file_list = collect_device_context(device_dir)
    client = client or get_ollama_client()

    result = regex_fallback(
        text=text,
        file_list=file_list,
        hint_category=hint_category,
        hint_model=hint_model,
        hint_os=hint_os,
        hint_pains=hint_pains,
    )
    result.source_url = source_url

    if use_ai and client.probe():
        classified = classify_with_ai(
            client,
            text=text,
            file_list=file_list,
            hint_category=hint_category or result.hardware_category,
            hint_model=hint_model or result.exact_model,
            hint_os=hint_os or " / ".join(result.os_support),
        )
        if classified:
            blob = f"{text} {classified}"
            cat = normalize_category(
                str(classified.get("hardware_category") or ""),
                context=blob,
            ) or normalize_category(hint_category, context=blob)
            model = normalize_model(
                str(classified.get("exact_model") or ""),
                context=blob,
            ) or normalize_model(hint_model, context=blob)
            if cat:
                result.hardware_category = cat
            if model:
                result.exact_model = model
            os_list = _as_str_list(classified.get("os_support"))
            if os_list:
                result.os_support = os_list
            result.confidence = float(classified.get("confidence") or 0.7)
            result.notes = str(classified.get("notes") or "")
            result.ai_used = True
            result.ai_backend = f"ollama:{client.resolve_model()}"

        seo = seo_with_ai(
            client,
            hardware_category=result.hardware_category,
            exact_model=result.exact_model,
            os_support=result.os_support,
            text=text,
            hint_pains=hint_pains or result.pain_points,
        )
        if seo:
            result.seo_title = str(seo.get("seo_title") or result.seo_title).strip()[
                :90
            ]
            pains = _as_str_list(seo.get("pain_points"))
            if pains:
                result.pain_points = pains[:5]
            tags = _as_str_list(seo.get("tags"))
            if tags:
                result.tags = [t.lower() for t in tags][:12]
            result.ai_used = True
            result.ai_backend = f"ollama:{client.resolve_model()}"

        junk_resp = junk_with_ai(client, file_list=file_list)
        if junk_resp:
            ai_junk = _as_str_list(junk_resp.get("junk_files"))
            # 合并正则命中
            merged = sorted(set(result.junk_files) | set(ai_junk))
            result.junk_files = merged
            result.ai_used = True
            result.ai_backend = f"ollama:{client.resolve_model()}"

    moved = move_junk_files(device_dir, result.junk_files)
    result.junk_files = moved
    result.updated_at = datetime.now(timezone.utc).isoformat()

    meta = result.to_metadata()
    # 兼容旧字段
    meta["value_score"] = None
    write_metadata(device_dir, meta, also_legacy=True)
    return result


def write_metadata(
    device_dir: Path,
    meta: dict[str, Any],
    *,
    also_legacy: bool = True,
) -> Path:
    device_dir.mkdir(parents=True, exist_ok=True)
    path = device_dir / METADATA_NAME
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    if also_legacy:
        legacy = {
            "hardware_category": meta.get("hardware_category"),
            "device_name": meta.get("exact_model") or meta.get("device_name"),
            "os_version": meta.get("os_version"),
            "os_support": meta.get("os_support"),
            "source_url": meta.get("source_url"),
            "pain_points": meta.get("pain_points"),
            "seo_title": meta.get("seo_title"),
            "tags": meta.get("tags"),
            "ai_used": meta.get("ai_used"),
            "ai_backend": meta.get("ai_backend"),
            "updated_at": meta.get("updated_at"),
        }
        (device_dir / META_LEGACY_NAME).write_text(
            json.dumps(legacy, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return path


def load_device_metadata(device_dir: Path) -> dict[str, Any]:
    for name in (METADATA_NAME, META_LEGACY_NAME):
        path = device_dir / name
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
    return {}


def enrich_vault(
    vault_root: Path,
    *,
    use_ai: bool = True,
    client: Optional[OllamaClient] = None,
    limit: int = 0,
) -> dict[str, int]:
    """
    [遗留] 扫描本地设备目录做 AI 富化。
    Link Curation 主路径请用 link_curator.enrich_index_seo；勿再建 data/vault。
    """
    if not vault_root.exists():
        logger.info("enrich_vault: %s 不存在，跳过（纯血版无需本地 vault）", vault_root)
        return {"devices": 0, "ai": 0, "fallback": 0, "junk_moved": 0, "skipped": 1}
    client = client or get_ollama_client()
    stats = {"devices": 0, "ai": 0, "fallback": 0, "junk_moved": 0}
    count = 0
    for category_dir in sorted(vault_root.iterdir()):
        if not category_dir.is_dir() or category_dir.name.startswith((".", "_")):
            continue
        for device_dir in sorted(category_dir.iterdir()):
            if not device_dir.is_dir():
                continue
            if limit and count >= limit:
                return stats
            existing = load_device_metadata(device_dir)
            result = enrich_device_asset(
                device_dir,
                hint_category=str(existing.get("hardware_category") or category_dir.name),
                hint_model=str(existing.get("exact_model") or existing.get("device_name") or device_dir.name),
                hint_os=str(existing.get("os_version") or ""),
                hint_pains=list(existing.get("pain_points") or []),
                source_url=str(existing.get("source_url") or ""),
                client=client,
                use_ai=use_ai,
            )
            stats["devices"] += 1
            count += 1
            if result.ai_used:
                stats["ai"] += 1
            else:
                stats["fallback"] += 1
            stats["junk_moved"] += len(result.junk_files)
            print(
                f"[AI清洗] {category_dir.name}/{device_dir.name} -> "
                f"{result.exact_model} | backend={result.ai_backend} | "
                f"junk={len(result.junk_files)}",
                flush=True,
            )
    return stats


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="本地 Ollama 资产清洗 Agent")
    p.add_argument("--probe", action="store_true", help="仅探测 Ollama 是否可用")
    p.add_argument("--device-dir", type=Path, help="清洗单个设备目录")
    p.add_argument("--vault", type=Path, default=ROOT / "data" / "vault", help="批量清洗 vault")
    p.add_argument("--enrich-vault", action="store_true", help="扫描 vault 批量富化")
    p.add_argument("--limit", type=int, default=0, help="批量最多处理 N 个设备，0=全部")
    p.add_argument("--no-ai", action="store_true", help="强制正则兜底")
    p.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    p.add_argument("--ollama-model", default=DEFAULT_OLLAMA_MODEL)
    p.add_argument("--timeout", type=float, default=CLI_TIMEOUT)
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    client = get_ollama_client(
        base_url=args.ollama_url,
        model=args.ollama_model,
        timeout=args.timeout,
    )
    if args.probe:
        ok = client.probe(force=True)
        print(f"Ollama {args.ollama_url} model={args.ollama_model} -> {'OK' if ok else 'UNAVAILABLE'}")
        return 0 if ok else 1

    if args.device_dir:
        result = enrich_device_asset(
            args.device_dir,
            client=client,
            use_ai=not args.no_ai,
        )
        print(json.dumps(result.to_metadata(), ensure_ascii=False, indent=2))
        return 0

    if args.enrich_vault:
        stats = enrich_vault(
            args.vault,
            use_ai=not args.no_ai,
            client=client,
            limit=max(0, args.limit),
        )
        print(f"完成: {stats}")
        return 0

    build_parser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
