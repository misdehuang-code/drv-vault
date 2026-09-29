"""GitHub token loading + rate-limit aware helpers for the dig pipeline."""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
TOKEN_FILE_CANDIDATES = (
    ROOT / "data" / "github_token.txt",
    ROOT / ".github_token",
    ROOT / ".env",
)

# process-local: when unauthenticated API is exhausted, skip further API until reset
_api_block_until = 0.0
_token_logged = False
# github.com HTML 连续超时 → 熔断，避免矿工卡死整轮 ACQUIRE
_html_block_until = 0.0
_html_fail_streak = 0


def load_dotenv_file(path: Path = ROOT / ".env") -> int:
    """Load KEY=VALUE lines into os.environ (do not override existing)."""
    if not path.exists():
        return 0
    n = 0
    try:
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip("'").strip('"')
            if not key:
                continue
            if key not in os.environ or not str(os.environ.get(key) or "").strip():
                os.environ[key] = val
                n += 1
    except Exception as exc:
        logger.debug("load_dotenv %s: %s", path, exc)
    return n


def get_github_token() -> str:
    """Resolve token: env → data/github_token.txt → .env file."""
    global _token_logged
    load_dotenv_file(ROOT / ".env")
    tok = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if tok:
        if not _token_logged:
            logger.info("GitHub token: from environment")
            _token_logged = True
        return tok
    for path in (ROOT / "data" / "github_token.txt", ROOT / ".github_token"):
        if not path.exists():
            continue
        try:
            for raw in path.read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.lower().startswith("github_token="):
                    line = line.split("=", 1)[1].strip().strip("'").strip('"')
                if line:
                    os.environ["GITHUB_TOKEN"] = line
                    if not _token_logged:
                        logger.info("GitHub token: from %s", path.name)
                        _token_logged = True
                    return line
        except Exception as exc:
            logger.debug("read token file %s: %s", path, exc)
    return ""


def mark_api_blocked(seconds: float = 3600.0) -> None:
    global _api_block_until
    _api_block_until = max(_api_block_until, time.time() + max(60.0, seconds))


def api_allowed() -> bool:
    return time.time() >= _api_block_until


def note_rate_limit_headers(resp) -> None:
    """If GitHub returns 403/429, block API until reset."""
    try:
        code = int(getattr(resp, "status_code", 0) or 0)
    except Exception:
        return
    if code not in {403, 429}:
        # also proactively block when remaining hits 0
        try:
            rem = resp.headers.get("X-RateLimit-Remaining")
            if rem is not None and int(rem) <= 0:
                reset = resp.headers.get("X-RateLimit-Reset")
                if reset:
                    wait = max(60.0, float(reset) - time.time() + 5.0)
                    mark_api_blocked(wait)
                else:
                    mark_api_blocked(3600.0)
        except Exception:
            pass
        return
    try:
        reset = resp.headers.get("X-RateLimit-Reset")
        if reset:
            wait = max(60.0, float(reset) - time.time() + 5.0)
            mark_api_blocked(wait)
            return
    except Exception:
        pass
    mark_api_blocked(3600.0)


def html_allowed() -> bool:
    return time.time() >= _html_block_until


def reset_html_circuit() -> None:
    """ACQUIRE 开轮时软重置熔断，给镜像/重试一次机会。"""
    global _html_block_until, _html_fail_streak
    _html_block_until = 0.0
    _html_fail_streak = 0


def mark_html_ok() -> None:
    global _html_fail_streak
    _html_fail_streak = 0


def mark_html_fail() -> None:
    """连续 HTML 超时则熔断 2 分钟，别拖死 ACQUIRE。"""
    global _html_block_until, _html_fail_streak
    _html_fail_streak += 1
    if _html_fail_streak >= 2:
        _html_block_until = max(_html_block_until, time.time() + 120.0)
        logger.info("github.com HTML 熔断 120s（连续超时）")


def html_release_assets(
    session,
    full_name: str,
    *,
    max_assets: int = 12,
    allow_mirrors: bool = True,
) -> list[str]:
    """Scrape GitHub releases via expanded_assets；github.com 不通时走镜像。"""
    del session
    if "/" not in full_name:
        return []
    owner, repo = full_name.split("/", 1)
    import requests as _rq

    s = _rq.Session()
    s.trust_env = False
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    bases = [f"https://github.com/{owner}/{repo}"]
    if allow_mirrors:
        bases.extend(
            [
                f"https://ghfast.top/https://github.com/{owner}/{repo}",
                f"https://ghproxy.net/https://github.com/{owner}/{repo}",
            ]
        )

    out: list[str] = []
    seen: set[str] = set()
    try:
        for base in bases:
            if len(out) >= max_assets:
                break
            # 熔断仅针对 github.com 本体；镜像仍可试
            if "github.com" in base and "ghfast" not in base and "ghproxy" not in base:
                if not html_allowed():
                    continue
            releases_url = f"{base}/releases"
            try:
                resp = s.get(releases_url, timeout=7, allow_redirects=True)
                if resp.status_code != 200:
                    if "github.com/" in releases_url and "ghfast" not in releases_url:
                        mark_html_fail()
                    continue
                if "github.com/" in releases_url and "ghfast" not in releases_url:
                    mark_html_ok()
                html = resp.text[:900_000]
            except Exception as exc:
                if "github.com/" in releases_url and "ghfast" not in releases_url:
                    mark_html_fail()
                logger.debug("html releases %s: %s", releases_url, exc)
                continue

            tags = re.findall(
                rf"/{re.escape(owner)}/{re.escape(repo)}/releases/tag/([^\"'/#?\s]+)",
                html,
                flags=re.I,
            )
            if not tags:
                # 镜像页可能只有 download 直链
                for href in re.findall(
                    r'href=["\']([^"\']*/releases/download/[^"\']+)["\']',
                    html,
                    flags=re.I,
                ):
                    url = href.strip()
                    if url.startswith("/"):
                        url = "https://github.com" + url
                    # 规范化掉镜像前缀，保留 github 真链供验活时再套镜像
                    if "github.com/" in url:
                        idx = url.find("github.com/")
                        url = "https://" + url[idx:]
                    low = url.lower()
                    if low.endswith((".sig", ".asc")):
                        continue
                    if url not in seen:
                        seen.add(url)
                        out.append(url)
                    if len(out) >= max_assets:
                        return out
                continue

            for tag in tags[:2]:
                # expanded_assets 优先走 github；失败再镜像
                candidates = [
                    f"https://github.com/{owner}/{repo}/releases/expanded_assets/{tag}",
                ]
                if allow_mirrors:
                    candidates.extend(
                        [
                            f"https://ghfast.top/https://github.com/{owner}/{repo}/releases/expanded_assets/{tag}",
                            f"https://ghproxy.net/https://github.com/{owner}/{repo}/releases/expanded_assets/{tag}",
                        ]
                    )
                body = ""
                for exp in candidates:
                    try:
                        r2 = s.get(exp, timeout=7, allow_redirects=True)
                        if r2.status_code == 200 and len(r2.text) > 200:
                            body = r2.text
                            break
                    except Exception:
                        continue
                if not body:
                    continue
                for href in re.findall(
                    r'href=["\']([^"\']*/releases/download/[^"\']+)["\']',
                    body,
                    flags=re.I,
                ):
                    url = href.strip()
                    if url.startswith("/"):
                        url = "https://github.com" + url
                    if "github.com/" in url:
                        idx = url.find("github.com/")
                        url = "https://" + url[idx:]
                    low = url.lower()
                    if low.endswith((".sig", ".asc")):
                        continue
                    if url not in seen:
                        seen.add(url)
                        out.append(url)
                    if len(out) >= max_assets:
                        return out
        return out
    finally:
        s.close()


def token_status() -> dict:
    tok = get_github_token()
    return {
        "has_token": bool(tok),
        "api_allowed": api_allowed(),
        "blocked_for_sec": max(0, int(_api_block_until - time.time())),
        "html_allowed": html_allowed(),
        "html_blocked_for_sec": max(0, int(_html_block_until - time.time())),
    }
