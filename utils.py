"""网络请求与通用工具函数。"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, Optional
from urllib.parse import urljoin

import requests
from requests import Response, Session

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class RequestError(Exception):
    """请求失败（含重试耗尽）后抛出。"""

    def __init__(
        self,
        message: str,
        *,
        url: str,
        attempts: int,
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.attempts = attempts
        self.status_code = status_code


def random_delay(min_seconds: float = 1.0, max_seconds: float = 3.0) -> float:
    """随机休眠一段时间，返回实际等待秒数。"""
    if min_seconds < 0 or max_seconds < min_seconds:
        raise ValueError("delay 范围无效: 需满足 0 <= min_seconds <= max_seconds")
    wait = random.uniform(min_seconds, max_seconds)
    time.sleep(wait)
    return wait


def create_session(
    headers: Optional[dict[str, str]] = None,
    *,
    trust_env: bool = False,
) -> Session:
    """
    创建 Session。

    trust_env 默认 False，避免系统里无效的 HTTP(S)_PROXY 导致全部请求失败。
    若需要走系统代理，传入 trust_env=True。
    """
    session = requests.Session()
    session.trust_env = trust_env
    merged = {**DEFAULT_HEADERS, **(headers or {})}
    session.headers.update(merged)
    return session


def fetch(
    url: str,
    *,
    method: str = "GET",
    session: Optional[Session] = None,
    params: Optional[dict[str, Any]] = None,
    data: Any = None,
    json: Any = None,
    headers: Optional[dict[str, str]] = None,
    timeout: float = 15.0,
    max_retries: int = 3,
    backoff_factor: float = 1.5,
    delay_range: tuple[float, float] = (1.0, 3.0),
    retry_on_status: tuple[int, ...] = (429, 500, 502, 503, 504),
    raise_for_status: bool = True,
) -> Response:
    """
    带随机延迟与异常重试的 HTTP 请求。

    - 每次尝试前加入随机延迟，降低请求频率
    - 网络异常或指定状态码会指数退避重试
    - 重试耗尽后抛出 RequestError
    """
    own_session = session is None
    sess = session or create_session()
    last_error: Optional[BaseException] = None

    try:
        for attempt in range(1, max_retries + 1):
            wait = random_delay(*delay_range)
            logger.debug("请求前延迟 %.2fs | attempt=%s url=%s", wait, attempt, url)

            try:
                response = sess.request(
                    method=method.upper(),
                    url=url,
                    params=params,
                    data=data,
                    json=json,
                    headers=headers,
                    timeout=timeout,
                )

                if response.status_code in retry_on_status:
                    # 429：配额耗尽，短重试只会刷爆日志；读 Retry-After 后最多再试 1 次
                    if response.status_code == 429:
                        retry_after = response.headers.get("Retry-After")
                        try:
                            wait_s = float(retry_after) if retry_after else 20.0
                        except ValueError:
                            wait_s = 20.0
                        wait_s = min(max(wait_s, 8.0), 60.0)
                        logger.warning(
                            "状态码 429（限流），等待 %.0fs 后%s: %s",
                            wait_s,
                            "再试一次" if attempt < max_retries else "放弃该请求",
                            url,
                        )
                        time.sleep(wait_s)
                        if attempt >= min(max_retries, 2):
                            raise RequestError(
                                "HTTP 429 限流，本源本轮应暂停",
                                url=url,
                                attempts=attempt,
                                status_code=429,
                            )
                        continue
                    last_error = RequestError(
                        f"可重试状态码 {response.status_code}",
                        url=url,
                        attempts=attempt,
                        status_code=response.status_code,
                    )
                    logger.warning(
                        "状态码 %s，准备重试 (%s/%s): %s",
                        response.status_code,
                        attempt,
                        max_retries,
                        url,
                    )
                    _sleep_backoff(attempt, backoff_factor)
                    continue

                if raise_for_status:
                    response.raise_for_status()

                return response

            except requests.RequestException as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                logger.warning(
                    "请求异常 (%s/%s) %s: %s",
                    attempt,
                    max_retries,
                    url,
                    exc,
                )
                # 400 参数错误重试无意义；429 另路径处理
                if status in (400, 401, 403, 404):
                    raise RequestError(
                        f"不可重试状态码 {status}: {exc}",
                        url=url,
                        attempts=attempt,
                        status_code=status,
                    ) from exc
                if isinstance(
                    exc,
                    (
                        requests.exceptions.ConnectTimeout,
                        requests.exceptions.ConnectionError,
                    ),
                ):
                    raise RequestError(
                        f"无法连接主机，跳过重试: {exc}",
                        url=url,
                        attempts=attempt,
                    ) from exc
                if attempt < max_retries:
                    _sleep_backoff(attempt, backoff_factor)

        raise RequestError(
            f"请求失败，已重试 {max_retries} 次: {last_error}",
            url=url,
            attempts=max_retries,
        ) from last_error
    finally:
        if own_session:
            sess.close()


def fetch_text(url: str, **kwargs: Any) -> str:
    """快捷方法：返回响应文本。"""
    return fetch(url, **kwargs).text


def fetch_json(url: str, **kwargs: Any) -> Any:
    """快捷方法：返回解析后的 JSON。"""
    return fetch(url, **kwargs).json()


def absolute_url(base: str, href: str) -> str:
    """将相对链接转为绝对 URL。"""
    return urljoin(base, href)


def retry(
    func: Callable[..., Any],
    *,
    max_retries: int = 3,
    backoff_factor: float = 1.5,
    delay_range: tuple[float, float] = (0.5, 1.5),
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Any:
    """对任意可调用对象做随机延迟 + 异常重试。"""
    last_error: Optional[BaseException] = None
    for attempt in range(1, max_retries + 1):
        random_delay(*delay_range)
        try:
            return func()
        except exceptions as exc:
            last_error = exc
            logger.warning("调用失败 (%s/%s): %s", attempt, max_retries, exc)
            if attempt < max_retries:
                _sleep_backoff(attempt, backoff_factor)
    raise last_error  # type: ignore[misc]


def _sleep_backoff(attempt: int, backoff_factor: float) -> None:
    # 指数退避并叠加少量抖动，避免惊群
    base = backoff_factor ** (attempt - 1)
    jitter = random.uniform(0, 0.3 * base)
    time.sleep(base + jitter)
