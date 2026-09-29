"""AsyncIO + aiohttp 高并发 HEAD 验活。

设计目标：
- 并发 20~50 协程，单请求 timeout≈3s
- 超时 / 404 瞬时失败，不拖垮整批
- 返回结构与 link_curator.verify_direct_link 对齐，便于无缝接入
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36 DrvVaultAsyncVerify/1.0"
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
)

DEFAULT_CONCURRENCY = 40
DEFAULT_TIMEOUT = 3.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _guess_filename(url: str, headers: dict[str, str]) -> str:
    cd = headers.get("Content-Disposition") or headers.get("content-disposition") or ""
    import re

    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.I)
    if m:
        return unquote(m.group(1).strip())
    name = Path(unquote(urlparse(url).path)).name
    return name or "package.bin"


def _looks_like_html(sample: bytes, content_type: str) -> bool:
    ct = (content_type or "").lower()
    if "text/html" in ct:
        return True
    head = sample.lstrip()[:400].lower()
    return any(
        head.startswith(m) or m in head
        for m in (b"<!doctype html", b"<html", b"<head", b"<title")
    )


def _candidate_urls(url: str) -> list[str]:
    low = (url or "").lower()
    out = [url]
    if "github.com/" in low and "/releases/download/" in low:
        for prefix in ("https://ghfast.top/", "https://ghproxy.net/"):
            out.append(prefix + url)
    return out


async def _probe_one(
    session: Any,
    url: str,
    *,
    timeout: float,
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
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
        from link_curator import is_direct_file_url

        if not is_direct_file_url(url):
            result["error"] = "not_direct_file"
            return result
    except Exception:
        pass

    import aiohttp

    to = aiohttp.ClientTimeout(total=float(timeout), connect=min(2.0, float(timeout)))
    headers = {"User-Agent": UA, "Accept": "*/*"}
    candidates = _candidate_urls(url)
    sample = b""

    async with sem:
        last_err = ""
        for cand in candidates:
            try:
                async with session.head(
                    cand, headers=headers, timeout=to, allow_redirects=True
                ) as resp:
                    code = int(resp.status)
                    hdrs = {k: v for k, v in resp.headers.items()}
                    result["status_code"] = code
                    result["content_type"] = (
                        hdrs.get("Content-Type") or hdrs.get("content-type") or ""
                    )
                    result["etag"] = hdrs.get("ETag") or hdrs.get("etag") or ""
                    result["file_name"] = _guess_filename(url, hdrs)
                    cl = hdrs.get("Content-Length") or hdrs.get("content-length")
                    if cl and str(cl).isdigit():
                        result["content_length"] = int(cl)

                    if code in (401, 403, 404, 410, 451):
                        result["error"] = f"http_{code}"
                        return result

                    # HEAD 被拒 → 极短 Range 探针
                    if code in (405, 501) or code >= 500:
                        result["method"] = "RANGE_PROBE"
                        h2 = dict(headers)
                        h2["Range"] = "bytes=0-2047"
                        async with session.get(
                            cand, headers=h2, timeout=to, allow_redirects=True
                        ) as r2:
                            code = int(r2.status)
                            hdrs = {k: v for k, v in r2.headers.items()}
                            result["status_code"] = code
                            result["content_type"] = (
                                hdrs.get("Content-Type")
                                or hdrs.get("content-type")
                                or ""
                            )
                            result["file_name"] = _guess_filename(url, hdrs)
                            cl = hdrs.get("Content-Length") or hdrs.get(
                                "content-length"
                            )
                            if cl and str(cl).isdigit():
                                result["content_length"] = int(cl)
                            cr = hdrs.get("Content-Range") or ""
                            import re

                            m = re.search(r"/(\d+)\s*$", cr)
                            if m and not result["content_length"]:
                                result["content_length"] = int(m.group(1))
                            try:
                                sample = await r2.content.read(2048)
                            except Exception:
                                sample = b""
                        if code in (401, 403, 404, 410, 451):
                            result["error"] = f"http_{code}"
                            return result
                        if code not in (200, 206):
                            result["error"] = f"http_{code}"
                            return result
                    elif code not in (200, 206):
                        result["error"] = f"http_{code}"
                        return result

                    if sample and _looks_like_html(sample, result["content_type"]):
                        result["error"] = "html_payload"
                        return result
                    if "text/html" in (result["content_type"] or "").lower():
                        # 无 sample 时再探一次极短 Range
                        if not sample:
                            h2 = dict(headers)
                            h2["Range"] = "bytes=0-1023"
                            try:
                                async with session.get(
                                    cand, headers=h2, timeout=to, allow_redirects=True
                                ) as r3:
                                    sample = await r3.content.read(1024)
                                    if _looks_like_html(
                                        sample, r3.headers.get("Content-Type", "")
                                    ):
                                        result["error"] = "html_payload"
                                        return result
                            except Exception:
                                result["error"] = "html_payload"
                                return result

                    fname = (result["file_name"] or "").lower()
                    path = unquote(urlparse(url).path).lower()
                    if not (
                        any(path.endswith(ext) for ext in DIRECT_EXTS)
                        or any(fname.endswith(ext) for ext in DIRECT_EXTS)
                        or "/releases/download/" in url.lower()
                        or "archive.org/download/" in url.lower()
                        or "downloadmirror.intel.com" in url.lower()
                        or "download.ni.com" in url.lower()
                    ):
                        result["error"] = "final_url_not_file"
                        return result

                    result["ok"] = True
                    result["error"] = ""
                    result["final_url"] = url  # 店面挂原始 URL
                    result["probe_url"] = cand
                    return result
            except asyncio.TimeoutError:
                last_err = "network:TimeoutError"
                continue
            except aiohttp.ClientError as exc:
                last_err = f"network:{exc.__class__.__name__}"
                continue
            except Exception as exc:
                last_err = f"error:{exc.__class__.__name__}"
                continue

        result["error"] = last_err or "network:TimeoutError"
        return result


async def _verify_many(
    urls: list[str],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float = DEFAULT_TIMEOUT,
    should_stop: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[dict[str, Any]], None]] = None,
) -> list[dict[str, Any]]:
    import aiohttp

    stop = should_stop or (lambda: False)
    conc = max(5, min(int(concurrency), 50))
    sem = asyncio.Semaphore(conc)
    connector = aiohttp.TCPConnector(
        limit=conc,
        limit_per_host=max(8, conc // 2),
        ttl_dns_cache=60,
        enable_cleanup_closed=True,
    )
    results: list[Optional[dict[str, Any]]] = [None] * len(urls)
    done = 0
    ok_n = 0
    fail_n = 0
    t0 = time.monotonic()

    async with aiohttp.ClientSession(
        connector=connector,
        headers={"User-Agent": UA},
        trust_env=False,
    ) as session:

        async def _one(idx: int, u: str) -> None:
            nonlocal done, ok_n, fail_n
            if stop():
                results[idx] = {
                    "ok": False,
                    "url": u,
                    "error": "stopped",
                    "verified_at": _utc_now(),
                }
                return
            vr = await _probe_one(session, u, timeout=timeout, sem=sem)
            results[idx] = vr
            done += 1
            if vr.get("ok"):
                ok_n += 1
            else:
                fail_n += 1
            if on_progress and (done % 10 == 0 or done == len(urls)):
                elapsed = max(0.001, time.monotonic() - t0)
                on_progress(
                    {
                        "done": done,
                        "total": len(urls),
                        "ok": ok_n,
                        "failed": fail_n,
                        "concurrency": conc,
                        "throughput": round(done / elapsed, 2),
                        "elapsed": round(elapsed, 2),
                    }
                )

        tasks = [asyncio.create_task(_one(i, u)) for i, u in enumerate(urls)]
        if tasks:
            await asyncio.gather(*tasks)

    elapsed = max(0.001, time.monotonic() - t0)
    out = [r or {"ok": False, "url": urls[i], "error": "missing"} for i, r in enumerate(results)]
    if on_progress:
        on_progress(
            {
                "done": len(out),
                "total": len(out),
                "ok": ok_n,
                "failed": fail_n,
                "concurrency": conc,
                "throughput": round(len(out) / elapsed, 2),
                "elapsed": round(elapsed, 2),
                "final": True,
            }
        )
    return out


def verify_urls_async(
    urls: list[str],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float = DEFAULT_TIMEOUT,
    should_stop: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[dict[str, Any]], None]] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """同步入口：跑完整批异步验活，返回 (results, metrics)。"""
    t0 = time.monotonic()
    metrics: dict[str, Any] = {
        "concurrency": max(5, min(int(concurrency), 50)),
        "timeout": float(timeout),
        "total": len(urls),
        "throughput": 0.0,
        "elapsed": 0.0,
        "ok": 0,
        "failed": 0,
    }
    if not urls:
        return [], metrics

    def _prog(p: dict[str, Any]) -> None:
        metrics.update(
            {
                "throughput": p.get("throughput", 0),
                "elapsed": p.get("elapsed", 0),
                "ok": p.get("ok", 0),
                "failed": p.get("failed", 0),
                "concurrency": p.get("concurrency", metrics["concurrency"]),
            }
        )
        if on_progress:
            on_progress(p)
        if p.get("final") or p.get("done", 0) % 20 == 0:
            print(
                f"当前并发协程：{p.get('concurrency')} | "
                f"验活吞吐量：{p.get('throughput')} 个/秒 | "
                f"本轮有效入库：{p.get('ok')} "
                f"(进度 {p.get('done')}/{p.get('total')})",
                flush=True,
            )

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 已在事件循环中：开新 loop 在线程跑
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                results = pool.submit(
                    lambda: asyncio.run(
                        _verify_many(
                            urls,
                            concurrency=concurrency,
                            timeout=timeout,
                            should_stop=should_stop,
                            on_progress=_prog,
                        )
                    )
                ).result()
        else:
            results = loop.run_until_complete(
                _verify_many(
                    urls,
                    concurrency=concurrency,
                    timeout=timeout,
                    should_stop=should_stop,
                    on_progress=_prog,
                )
            )
    except RuntimeError:
        results = asyncio.run(
            _verify_many(
                urls,
                concurrency=concurrency,
                timeout=timeout,
                should_stop=should_stop,
                on_progress=_prog,
            )
        )

    elapsed = max(0.001, time.monotonic() - t0)
    ok = sum(1 for r in results if r.get("ok"))
    metrics.update(
        {
            "ok": ok,
            "failed": len(results) - ok,
            "elapsed": round(elapsed, 2),
            "throughput": round(len(results) / elapsed, 2),
        }
    )
    return results, metrics
