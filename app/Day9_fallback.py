"""Day 9 N3: try configured LLM providers in order with automatic fallback."""

from __future__ import annotations

import os
from dataclasses import dataclass

import time
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import openai
from dotenv import load_dotenv
from openai import OpenAI

if __package__:
    from .Day9_resilience import call_with_backoff
else:
    from Day9_resilience import call_with_backoff


PROVIDERS = [
    {
        "name": "broken-primary",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "hardcoded_key": "sk-invalid-demo-key",
    },
    {
        "name": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "key_env": "DEEPSEEK_API_KEY",
    },
    {
        "name": "zhipu-template",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "key_env": "ZHIPU_API_KEY",
    },
]

@dataclass
class UsageMeter:
    calls: int=0
    prompt_tokens: int=0
    completion_tokens:int=0
    hit_tokens:int=0

    def add(self,usage) -> None:
        self.calls += 1
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.hit_tokens += getattr(usage,"prompt_cache_hit_tokens",0) or 0

    @property
    def total_tokens(self)->int:
        return self.prompt_tokens + self.completion_tokens

    def uncache_prompt(self) -> int:
        return self.prompt_tokens - self.hit_tokens

    def cache_prompt(self) -> int:
        return self.hit_tokens

    def cost(self,*,peak:bool=False)->float:
        if peak:
            prompt_cost = 2
            completion_cost = 8
            hit_cost = 0.02
        else:
            prompt_cost = 1
            completion_cost = 4
            hit_cost = 0.02
        uncache_prompt_cost = self.uncache_prompt() / 1000000 * prompt_cost
        cache_prompt_cost = self.cache_prompt() / 1000000 * hit_cost
        completion_cost = self.completion_tokens / 1000000 * completion_cost
        return uncache_prompt_cost + cache_prompt_cost + completion_cost

meter = UsageMeter()
ClientFactory = Callable[[dict[str, Any], float], Any]


def _provider_key(provider: dict[str, Any]) -> str:
    """Return a provider key without ever logging the key itself."""
    hardcoded_key = provider.get("hardcoded_key")
    if isinstance(hardcoded_key, str) and hardcoded_key:
        return hardcoded_key

    key_env = provider.get("key_env")
    if not isinstance(key_env, str) or not key_env:
        return ""
    return os.getenv(key_env, "").strip()


def _make_provider_client(provider: dict[str, Any], timeout: float) -> OpenAI:
    """Create a single-provider client with SDK retries disabled."""
    return OpenAI(
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


def _total_tokens(response: Any) -> int | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    if isinstance(usage, dict):
        value = usage.get("total_tokens")
    else:
        value = getattr(usage, "total_tokens", None)
    return value if isinstance(value, int) else None


def _failure_reason(exc: Exception) -> str:
    """Keep the failure type and HTTP status without echoing request details."""
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


def _log_remaining_missing_keys(
    providers: list[dict[str, Any]],
    start_index: int,
    total: int,
) -> None:
    """Record later providers that would definitely be skipped, without calling them."""
    for index in range(start_index, total):
        provider = providers[index]
        if not _provider_key(provider):
            _log_result(
                index + 1,
                total,
                str(provider["name"]),
                "跳过（无 key）",
                0.0,
            )


def _run_providers(
    providers: list[dict[str, Any]],
    prompt: str,
    *,
    max_attempts_per_provider: int,
    timeout: float,
    client_factory: ClientFactory = _make_provider_client,
    backoff_base: float = 1.0,
) -> str:
    """Run the fallback chain, with injectable clients for offline self-tests."""
    if max_attempts_per_provider < 1:
        raise ValueError("max_attempts_per_provider must be at least 1")

    total = len(providers)
    failures: list[str] = []

    for index, provider in enumerate(providers):
        name = str(provider["name"])
        started_at = time.perf_counter()

        if not _provider_key(provider):
            elapsed = time.perf_counter() - started_at
            _log_result(index + 1, total, name, "跳过（无 key）", elapsed)
            failures.append(f"{name}: 跳过（无 key）")
            continue

        attempts = 0
        try:
            client = client_factory(provider, timeout)
            create = client.chat.completions.create

            def request() -> Any:
                nonlocal attempts
                attempts += 1
                return create(
                    model=str(provider["model"]),
                    messages=[{"role": "user", "content": prompt}],
                )

            response = call_with_backoff(
                request,
                max_attempts=max_attempts_per_provider,
                base=backoff_base,
            )
            if response.usage:
                meter.add(response.usage)
            text = _extract_text(response)
            elapsed = time.perf_counter() - started_at
            tokens = _total_tokens(response)
            token_text = str(tokens) if tokens is not None else "未知"
            _log_result(
                index + 1,
                total,
                name,
                f"成功  tokens {token_text}（尝试 {attempts} 次）",
                elapsed,
            )
            _log_remaining_missing_keys(providers, index + 1, total)
            return text
        except Exception as exc:
            elapsed = time.perf_counter() - started_at
            suffix = "→ 切换下一家" if index + 1 < total else "→ 已无备用厂商"
            _log_result(
                index + 1,
                total,
                name,
                f"失败 ({type(exc).__name__})（尝试 {attempts} 次）",
                elapsed,
                f" {suffix}",
            )
            failures.append(f"{name}: {_failure_reason(exc)}")

    raise RuntimeError("所有厂商均失败：" + "；".join(failures))


def call_with_fallback(
    prompt: str,
    *,
    max_attempts_per_provider: int = 2,
    timeout: float = 20.0,
) -> str:
    """Try each configured provider in order and return the first successful text."""
    load_dotenv()
    return _run_providers(
        PROVIDERS,
        prompt,
        max_attempts_per_provider=max_attempts_per_provider,
        timeout=timeout,
    )

class _RateLimitedClient:
    """A fake OpenAI-shaped client that always returns HTTP 429."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create),
        )

    def _create(self, **_: Any) -> Any:
        self.calls += 1
        request = httpx.Request("POST", f"http://{self.name}.test")
        response = httpx.Response(429, request=request)
        raise openai.RateLimitError("rate limited", response=response, body=None)


def _self_test_all_fail() -> None:
    """Verify provider fallback and exhaustion without making network requests."""
    providers = [
        {
            "name": "fake-a",
            "base_url": "http://fake-a.test",
            "model": "fake-model",
            "hardcoded_key": "fake-test-key-a",
        },
        {
            "name": "fake-b",
            "base_url": "http://fake-b.test",
            "model": "fake-model",
            "hardcoded_key": "fake-test-key-b",
        },
    ]
    fake_clients = {
        provider["name"]: _RateLimitedClient(str(provider["name"]))
        for provider in providers
    }
    max_attempts = 2

    def fake_client_factory(
        provider: dict[str, Any],
        timeout: float,
    ) -> _RateLimitedClient:
        del timeout
        return fake_clients[str(provider["name"])]

    print("\n[自测] 两家厂商持续 429，最终全失败（不联网）")
    try:
        _run_providers(
            providers,
            "offline self-test",
            max_attempts_per_provider=max_attempts,
            timeout=0.1,
            client_factory=fake_client_factory,
            backoff_base=0.01,
        )
    except RuntimeError as exc:
        print(f"RuntimeError: {exc}")
        call_counts = {
            name: client.calls for name, client in fake_clients.items()
        }
        print(f"各假客户端调用次数：{call_counts}")
        if any(count != max_attempts for count in call_counts.values()):
            raise AssertionError("每个假客户端都应重试到上限")
    else:
        raise AssertionError("全部厂商失败时必须抛出 RuntimeError")


if __name__ == "__main__":
    text = call_with_fallback("用一句话说明什么是 前沿部署工程师FDE，200字以内,要写代码吗，LLM的是写python吗")
    print(text)

    # 离线自测
    _self_test_all_fail()

    # 成本核算

    print("\n" + "=" * 62)
    print("成本核算（deepseek-flash 官方价，元 / 百万 tokens）")
    print("=" * 62)
    print(f"累计调用次数: {meter.calls}")
    print(f"tokens: 输入 {meter.prompt_tokens}（缓存命中 {meter.hit_tokens}）"
          f" / 输出 {meter.completion_tokens}")
    print(f"        合计 {meter.total_tokens}")

    idle = meter.cost()
    peak = meter.cost(peak=True)
    print(f"\n单份成本:  空闲 {idle:.6f} 元（万分之 {idle * 10000:.2f}）")
    print(f"           高峰 {peak:.6f} 元（万分之 {peak * 10000:.2f}）")

    # ---- 4. 规模化 ----
    per_day, days = 2000, 30
    monthly = per_day * days
    print(f"\n{per_day} 份/天 × {days} 天 = {monthly:,} 份/月")
    print(f"  空闲时段 → {idle * monthly:.2f} 元/月")
    print(f"  高峰时段 → {peak * monthly:.2f} 元/月")

    # ---- 5. 预算反算 ----
    budget = 300.0
    print(f"\n预算 {budget:.0f} 元/月:")
    print(f"  空闲可处理 {budget / idle / 10000:.1f} 万份")
    print(f"  高峰可处理 {budget / peak / 10000:.1f} 万份")

    # ---- 6. 人工对比 ----
    minutes_per = 3
    per_person_day = (8 * 60) // minutes_per
    print(f"\n人工对比: {minutes_per} 分钟/份 × 8 小时/天")
    print(f"  1 个人 1 天最多 {per_person_day} 份")
    print(f"  {per_day} 份/天 需要 {per_day / per_person_day:.1f} 个人")
