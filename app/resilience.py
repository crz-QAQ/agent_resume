"""explicit timeout handling and bounded exponential backoff."""

import os
import random
import time
from typing import Any, Callable

import openai
import httpx
from dotenv import load_dotenv
from openai import OpenAI


def make_client(timeout: float = 30.0) -> OpenAI:
    """Build an OpenAI client whose retries are controlled by this module."""
    load_dotenv()
    return OpenAI(
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url=os.getenv("LLM_BASE_URL"),
        timeout=timeout,
        max_retries=0,     # 关闭SDK重试
    )

# 判断是否重试
def is_retryable(exc: Exception) -> bool:
    """Return whether an exception represents a transient server-side failure."""
    # AuthenticationError=401，PermissionDeniedError=403，BadRequestError=400
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError, openai.BadRequestError)):
        return False    # 没权限+请求格式错直接返回False
    # RateLimitError=429，APITimeoutError=超时，InternalServerError
    if isinstance(exc, (openai.RateLimitError, openai.APITimeoutError, openai.InternalServerError)):
        return True     # 限流+超时时返回True，可以重试
    if isinstance(exc, openai.APIError):
        status = getattr(exc, "status_code", None)
        return isinstance(status, int) and status >= 500
    return False


def _retry_after(exc: Exception) -> float | None:
    """Read Retry-After from an error response, when the SDK exposes it."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def call_with_backoff(fn: Callable[[], Any], max_attempts: int = 3, base: float = 1.0) -> Any:
    """Call *fn*, retrying transient failures with exponential backoff and jitter."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if not is_retryable(exc) or attempt == max_attempts:
                raise
            delay = _retry_after(exc) if isinstance(exc, openai.RateLimitError) else None
            if delay is None:
                delay = base * (2 ** (attempt - 1)) + random.uniform(0.0, 0.5)   # 0-0.5秒的随机抖动
            print(f"第 {attempt} 次失败 ({type(exc).__name__})，等待 {delay:.1f}s")
            time.sleep(delay)


class _FakeClient:
    def __init__(self, outcomes: list[Any]):
        self.outcomes = iter(outcomes)

    def create(self) -> Any:
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _fake_rate_limit() -> openai.RateLimitError:
    request = httpx.Request("POST", "http://test")
    return openai.RateLimitError("rate limited", response=httpx.Response(429, request=request), body=None)


def _run_self_tests() -> None:
    success = type("Response", (), {"choices": [type("Choice", (), {"message": type("Message", (), {"content": "成功"})()})()]})()

    print("测试 1：两次 429 后成功")
    result = call_with_backoff(_FakeClient([_fake_rate_limit(), _fake_rate_limit(), success]).create, base=0.05)
    print(f"第 3 次成功: {result.choices[0].message.content} ✓")

    print("测试 2：持续 429 最终抛出")
    try:
        call_with_backoff(_FakeClient([_fake_rate_limit()] * 3).create, base=0.05)
    except openai.RateLimitError:
        print("3 次后异常已抛出 ✓")

    print("测试 3：401 立即抛出且不重试")
    request = httpx.Request("POST", "http://test")
    auth_error = openai.AuthenticationError("bad key", response=httpx.Response(401, request=request), body=None)
    try:
        call_with_backoff(_FakeClient([auth_error]).create, base=0.05)
    except openai.AuthenticationError:
        print("401 未重试，立即抛出 ✓")

    print("测试 4：超时后成功")
    timeout_error = openai.APITimeoutError(request=httpx.Request("POST", "http://test"))
    result = call_with_backoff(_FakeClient([timeout_error, success]).create, base=0.05)
    print(f"超时重试后成功: {result.choices[0].message.content} ✓")


if __name__ == "__main__":
    print("N1：演示极短超时（真实请求）")
    try:
        make_client(timeout=0.001).chat.completions.create(
            model=os.getenv("LLM_MODEL", "deepseek-chat"),
            messages=[{"role": "user", "content": "ping"}],
        )
    except openai.APITimeoutError:
        print("超时已捕获，未卡住。")
    except openai.APIError as exc:
        print(f"请求已结束（{type(exc).__name__}），继续运行自测。")

    _run_self_tests()
