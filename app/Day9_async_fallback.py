"""Day 9 N5: asynchronous, sequential provider fallback with usage accounting."""

from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

import httpx
import openai
from dotenv import load_dotenv
from openai import AsyncOpenAI

if __package__:
    from .Day9_fallback import PROVIDERS, UsageMeter, _provider_key
    from .Day9_resilience import _retry_after, is_retryable
else:
    from Day9_fallback import PROVIDERS, UsageMeter, _provider_key
    from Day9_resilience import _retry_after, is_retryable


AsyncClientFactory = Callable[[dict[str, Any], float], Any]


async def call_with_backoff_async(
    fn: Callable[[], Awaitable[Any]],
    max_attempts: int = 3,
    base: float = 1.0,
) -> Any:
    """Await an async call with bounded exponential backoff and jitter."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except Exception as exc:
            if not is_retryable(exc) or attempt == max_attempts:
                raise

            delay = (
                _retry_after(exc)
                if isinstance(exc, openai.RateLimitError)
                else None
            )
            if delay is None:
                delay = base * (2 ** (attempt - 1)) + random.uniform(0.0, 0.5)

            print(
                f"第 {attempt} 次失败 ({type(exc).__name__})，"
                f"异步等待 {delay:.1f}s"
            )
            await asyncio.sleep(delay)


def _make_async_provider_client(
    provider: dict[str, Any],
    timeout: float,
) -> AsyncOpenAI:
    """Create one async provider client with SDK retries disabled."""
    return AsyncOpenAI(
        api_key=_provider_key(provider),
        base_url=str(provider["base_url"]),
        timeout=timeout,
        max_retries=0,
    )


def _extract_text(response: Any) -> str:
    content = response.choices[0].message.content
    if not isinstance(content, str) or not content.strip():
        raise ValueError("厂商返回了空文本")
    return content


def _usage_with_defaults(usage: Any) -> Any:
    """Let UsageMeter accept either SDK usage objects or dictionary usage."""
    if not isinstance(usage, dict):
        return usage
    return SimpleNamespace(
        prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
        completion_tokens=int(usage.get("completion_tokens", 0) or 0),
        prompt_cache_hit_tokens=int(
            usage.get("prompt_cache_hit_tokens", 0) or 0
        ),
    )


def _total_tokens(usage: Any) -> int | None:
    if isinstance(usage, dict):
        value = usage.get("total_tokens")
    else:
        value = getattr(usage, "total_tokens", None)
    return value if isinstance(value, int) else None


def _failure_reason(exc: Exception) -> str:
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    return f"{name} (HTTP {status})" if status is not None else name


def _log_result(
    index: int,
    total: int,
    provider_name: str,
    result: str,
    elapsed: float,
    suffix: str = "",
) -> None:
    dots = "." * max(3, 18 - len(provider_name))
    print(
        f"[{index}/{total}] {provider_name} {dots} "
        f"{result} 耗时 {elapsed:.2f}s{suffix}"
    )


def _build_messages(system:str | None,prompt:str)->list[dict[str,str]]:
    # system 管规矩，user管内容 ----两者不能混在一句里
    messages : list[dict[str,str]] = []
    if system:
        messages.append({"role":"system","content":system})
    messages.append({"role":"user","content":prompt})
    return messages


async def _run_providers_async(
    providers: list[dict[str, Any]],
    prompt: str,
    *,
    system:str | None = None,    # 让规则能进到role:system,而不是混在user里
    max_attempts_per_provider: int,
    timeout: float,
    client_factory: AsyncClientFactory = _make_async_provider_client,
    backoff_base: float = 1.0,
) -> tuple[str, UsageMeter]:
    """Try providers one at a time; injectable clients support offline tests."""
    if max_attempts_per_provider < 1:
        raise ValueError("max_attempts_per_provider must be at least 1")

    loop = asyncio.get_running_loop()
    total = len(providers)
    failures: list[str] = []
    meter = UsageMeter()

    for index, provider in enumerate(providers):
        name = str(provider["name"])
        started_at = loop.time()

        if not _provider_key(provider):
            _log_result(
                index + 1,
                total,
                name,
                "跳过（无 key）",
                loop.time() - started_at,
            )
            failures.append(f"{name}: 跳过（无 key）")
            continue

        attempts = 0
        try:
            client = client_factory(provider, timeout)
            create = client.chat.completions.create

            async def request() -> Any:
                nonlocal attempts
                attempts += 1
                return await create(
                    model=str(provider["model"]),
                    messages=_build_messages(system,prompt),
                    temperature=0,
                    response_format={"type":"json_object"}
                )

            response = await call_with_backoff_async(
                request,
                max_attempts=max_attempts_per_provider,
                base=backoff_base,
            )
            text = _extract_text(response)
            usage = getattr(response, "usage", None)
            if usage is not None:
                meter.add(_usage_with_defaults(usage))

            token_value = _total_tokens(usage) if usage is not None else None
            token_text = str(token_value) if token_value is not None else "未知"
            _log_result(
                index + 1,
                total,
                name,
                f"成功 tokens {token_text}（尝试 {attempts} 次）",
                loop.time() - started_at,
            )
            return text, meter
        except Exception as exc:
            suffix = "→ 切换下一家" if index + 1 < total else "→ 已无备用厂商"
            _log_result(
                index + 1,
                total,
                name,
                f"失败 ({type(exc).__name__})（尝试 {attempts} 次）",
                loop.time() - started_at,
                f" {suffix}",
            )
            failures.append(f"{name}: {_failure_reason(exc)}")

    raise RuntimeError("所有厂商均失败：" + "；".join(failures))


async def call_with_fallback_async(
    prompt: str,
    *,
    system: str | None = None,
    max_attempts_per_provider: int = 2,
    timeout: float = 20.0,
) -> tuple[str, UsageMeter]:
    """Return the first provider's text and its usage, or raise on exhaustion."""
    load_dotenv()
    return await _run_providers_async(
        PROVIDERS,
        prompt,
        system = system,
        max_attempts_per_provider=max_attempts_per_provider,
        timeout=timeout,
    )


class _AsyncRateLimitedClient:
    """An AsyncOpenAI-shaped fake whose completion call always returns 429."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create),
        )

    async def _create(self, **_: Any) -> Any:
        self.calls += 1
        request = httpx.Request("POST", f"http://{self.name}.test")
        response = httpx.Response(429, request=request)
        raise openai.RateLimitError(
            "rate limited",
            response=response,
            body=None,
        )


async def _self_test_all_fail_async() -> None:
    """Verify retry exhaustion and fallback failure without network access."""
    providers = [
        {
            "name": "fake-async-a",
            "base_url": "http://fake-async-a.test",
            "model": "fake-model",
            "hardcoded_key": "fake-test-key-a",
        },
        {
            "name": "fake-async-b",
            "base_url": "http://fake-async-b.test",
            "model": "fake-model",
            "hardcoded_key": "fake-test-key-b",
        },
    ]
    clients = {
        provider["name"]: _AsyncRateLimitedClient(str(provider["name"]))
        for provider in providers
    }
    max_attempts = 2

    def fake_client_factory(
        provider: dict[str, Any],
        timeout: float,
    ) -> _AsyncRateLimitedClient:
        del timeout
        return clients[str(provider["name"])]

    print("\n[异步自测] 两家厂商持续 429，最终全失败（不联网）")
    try:
        await _run_providers_async(
            providers,
            "offline async self-test",
            max_attempts_per_provider=max_attempts,
            timeout=0.1,
            client_factory=fake_client_factory,
            backoff_base=0.01,
        )
    except RuntimeError as exc:
        message = str(exc)
        print(f"RuntimeError: {message}")
        call_counts = {
            name: client.calls for name, client in clients.items()
        }
        print(f"各假客户端调用次数：{call_counts}")
        assert all(count == max_attempts for count in call_counts.values())
        assert "fake-async-a" in message
        assert "fake-async-b" in message
        assert "RateLimitError" in message
    else:
        raise AssertionError("全部厂商失败时必须抛出 RuntimeError")


if __name__ == "__main__":
    asyncio.run(_self_test_all_fail_async())
