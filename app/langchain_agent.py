"""Day 17B / BN1：LangChain 多厂商 fallback + repair 护栏。

本文件复用 Day 9 的厂商列表和计费口径，也复用 Day 11 的校验、结果模型与
repair 触发词。LangChain 只负责结构化输出和 runnable fallback；业务状态、
步数、预算仍然由显式代码控制。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from dotenv import load_dotenv
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda
from langchain_openai import ChatOpenAI

from schemas import ResumeInfo
from providers import PROVIDERS, UsageMeter, _provider_key
# repair 触发词来自 agent 模块级常量，改关键词只改那一处。
from agent import (
    AgentResult,
    StepLog,
    check_resume,
    repair_reason_keywords,
)
from prompts import PROMPT_VERSION, SYSTEM_RESUME

# Windows 默认 GBK 控制台无法编码“✓”等验收标记。脚本显式使用 UTF-8，
# 避免业务测试通过后只因终端编码而异常退出。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# 默认为假模型，首次自测不会联网或产生费用。真实对照显式设置
# FAKE_MODEL=false 后运行。
FAKE_MODEL = os.getenv("FAKE_MODEL", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

TOTAL_COST_LIMIT_YUAN = 0.01

# SYSTEM_RESUME 含 JSON 示例，而 ChatPromptTemplate 使用花括号做变量
# 替换；只在这里转义模板标记，不复制或改写原 prompt。
SYSTEM_RESUME_TEMPLATE = SYSTEM_RESUME.replace("{", "{{").replace("}", "}}")
prompt = ChatPromptTemplate.from_messages(
    [
        ("system", SYSTEM_RESUME_TEMPLATE),
        ("user", "{raw}"),
    ]
)


def _usage_metadata_to_meter(
    usage_metadata: dict[str, dict[str, Any]],
) -> UsageMeter:
    """按 Day 9 的 UsageMeter 口径汇总一次 LangChain 模型调用的 usage。"""
    meter = UsageMeter()
    for usage in usage_metadata.values():
        input_details = usage.get("input_token_details") or {}
        meter.add(
            SimpleNamespace(
                prompt_tokens=int(usage.get("input_tokens", 0) or 0),
                completion_tokens=int(usage.get("output_tokens", 0) or 0),
                prompt_cache_hit_tokens=int(
                    input_details.get("cache_read", 0) or 0
                ),
            )
        )
    return meter


class _UsageLedger:
    """汇总一次 Agent 解析中所有实际厂商调用的 token 与费用。"""

    def __init__(self) -> None:
        self.meter = UsageMeter()

    def record(self, handler: UsageMetadataCallbackHandler) -> None:
        attempt_meter = _usage_metadata_to_meter(handler.usage_metadata)
        self.meter.calls += attempt_meter.calls
        self.meter.prompt_tokens += attempt_meter.prompt_tokens
        self.meter.completion_tokens += attempt_meter.completion_tokens
        self.meter.hit_tokens += attempt_meter.hit_tokens

    def snapshot(self) -> tuple[int, int, int, float]:
        return (
            self.meter.calls,
            self.meter.prompt_tokens,
            self.meter.completion_tokens,
            self.meter.cost(),
        )


def _log_provider(
    index: int,
    total: int,
    name: str,
    outcome: str,
    *,
    elapsed: float,
    suffix: str = "",
) -> None:
    dots = "." * max(3, 18 - len(name))
    print(
        f"[{index}/{total}] {name} {dots} {outcome} "
        f"耗时 {elapsed:.2f}s{suffix}",
        flush=True,
    )


def _raise_on_none(value: ResumeInfo | None) -> ResumeInfo:
    if value is None:
        raise ValueError("未触发 tool_call")
    return value


def _fake_resume(raw_text: str) -> ResumeInfo:
    """离线模式使用的小型确定性模型，仅用于验证链和状态收口。"""
    if "今天天气" in raw_text:
        return ResumeInfo(name=None, years=None, skills=[])
    if "还没毕业" in raw_text:
        return ResumeInfo(name=None, years=None, skills=["Python"])
    if "赵六" in raw_text:
        return ResumeInfo(name="赵六", years=8.0, skills=["后端"])
    if "岗位要求" in raw_text:
        return ResumeInfo(name=None, years=3.0, skills=["Go", "MySQL"])
    if "李雷" in raw_text:
        return ResumeInfo(name="李雷", years=5.0, skills=["Python", "FastAPI"])
    return ResumeInfo(name="张三", years=3.0, skills=["Go", "PHP"])


def _make_fake_inner_chain() -> RunnableLambda:
    """返回和结构化模型同样“可能返回 None”的假链。"""

    def invoke(payload: dict[str, Any]) -> ResumeInfo:
        return _fake_resume(str(payload["raw"]))

    return RunnableLambda(invoke) | RunnableLambda(_raise_on_none)


def _make_provider_runnable(
    provider: dict[str, Any],
    index: int,
    total: int,
    ledger: _UsageLedger,
) -> RunnableLambda:
    """为单个厂商构造独立 runnable，并把该厂商 usage 单独记账。

    模型和 callback 都必须按厂商隔离。若多个 runnable 共用一个 client 或
    一个外层 callback，被 fallback 跳过的厂商就无法准确看到用量，日志也
    容易全部指向同一个 client。
    """
    name = str(provider["name"])
    key = _provider_key(provider)

    if not key:
        inner = RunnableLambda(
            lambda _: (_ for _ in ()).throw(ValueError("缺少 API key"))
        )
    elif FAKE_MODEL:
        inner = _make_fake_inner_chain()
    else:
        llm = ChatOpenAI(
            model=str(provider["model"]),
            base_url=str(provider["base_url"]),
            api_key=key,
            temperature=0,
            timeout=60,
            max_retries=0,
        )
        structured_llm = llm.with_structured_output(
            ResumeInfo,
            method="function_calling",
        )

        # 关键机理解释：with_structured_output 在模型没有触发 tool_call 时
        # 可能安静地返回 None，而 RunnableWithFallbacks 只处理异常。它看起来
        # “已经串好 fallback”，实际却会把 None 当成功结果直接返回，后面的
        # 厂商一次也不会调用。把 None 提升为 ValueError 后，fallback 才真正
        # 能看到失败并切到下一家。
        inner = prompt | structured_llm | RunnableLambda(_raise_on_none)

    def invoke_provider(payload: dict[str, Any]) -> ResumeInfo:
        # 每次 invoke 都新建 handler，避免把上一条 chain 的用量重复带入。
        handler = UsageMetadataCallbackHandler()
        started_at = time.perf_counter()
        try:
            result = inner.invoke(
                payload,
                config={"callbacks": [handler]},
            )
        except Exception as exc:
            ledger.record(handler)
            suffix = (
                "→ 切换下一家"
                if index < total
                else "→ 已无备用厂商"
            )
            _log_provider(
                index,
                total,
                name,
                f"失败 ({type(exc).__name__})",
                elapsed=time.perf_counter() - started_at,
                suffix=suffix,
            )
            raise

        ledger.record(handler)
        _log_provider(
            index,
            total,
            name,
            "成功",
            elapsed=time.perf_counter() - started_at,
        )
        return result

    return RunnableLambda(invoke_provider)


def _build_fallback_chain(ledger: _UsageLedger):
    """严格按 PROVIDERS 顺序构造 primary + fallback runnable 链。"""
    if not PROVIDERS:
        raise RuntimeError("PROVIDERS 不能为空")

    total = len(PROVIDERS)
    runnables = [
        _make_provider_runnable(provider, index, total, ledger)
        for index, provider in enumerate(PROVIDERS, start=1)
    ]
    primary = runnables[0]
    backups = runnables[1:]
    if not backups:
        return primary
    return primary.with_fallbacks(
        backups,
        exceptions_to_handle=(Exception,),
    )


def _collect_reasons(raw_text: str, data: ResumeInfo) -> list[str]:
    """调用共享校验规则，并补齐任务书要求的“纯招聘要求”输入。"""
    reasons = check_resume(raw_text, data)

    # Day 11 现有 check_resume 只有在“招聘要求 + 学生身份”同时出现时才
    # 报错；17B 用例⑤本身只是一段岗位要求。为了保持用例语义且不重写
    # check_resume，这里只补充这种明确输入，并复用同一个 repair 触发词。
    requirement_words = ("岗位要求", "招聘要求", "任职要求", "任职资格")
    if (
        any(word in raw_text for word in requirement_words)
        and "疑似把招聘要求当成工作经历" not in reasons
    ):
        reasons.append("疑似把招聘要求当成工作经历")
    return reasons


def _step_cost(
    before: tuple[int, int, int, float],
    after: tuple[int, int, int, float],
) -> tuple[int, int, float]:
    return (
        after[1] - before[1],
        after[2] - before[2],
        after[3] - before[3],
    )


def parse_resume_lc(
    raw_text: str,
    budget_yuan: float = 0.02,
) -> AgentResult:
    """执行一次 LangChain extract，并按共享护栏决定是否 repair。"""
    ledger = _UsageLedger()
    fallback_chain = _build_fallback_chain(ledger)
    steps: list[StepLog] = []

    extract_before = ledger.snapshot()
    try:
        data = fallback_chain.invoke({"raw": raw_text})
    except Exception as exc:
        extract_after = ledger.snapshot()
        tokens_in, tokens_out, cost_yuan = _step_cost(
            extract_before,
            extract_after,
        )
        steps.append(
            StepLog(
                step=1,
                action="extract",
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_yuan=cost_yuan,
                ok=False,
                note=f"extract失败:所有厂商均失败 ({type(exc).__name__})",
            )
        )
        return AgentResult(
            status="failed",
            reasons=[f"extract失败:所有厂商均失败 ({type(exc).__name__})"],
            steps=steps,
            total_cost_yuan=cost_yuan,
            prompt_version=PROMPT_VERSION,
        )

    extract_after = ledger.snapshot()
    tokens_in, tokens_out, cost_yuan = _step_cost(
        extract_before,
        extract_after,
    )
    steps.append(
        StepLog(
            step=1,
            action="extract",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_yuan=cost_yuan,
            ok=True,
            note="extract 调用并解析成功",
        )
    )

    reasons = _collect_reasons(raw_text, data)
    status = "needs_review" if reasons else "ok"
    should_repair = any(
        keyword in reason
        for reason in reasons
        for keyword in repair_reason_keywords
    )
    total_cost_yuan = ledger.meter.cost()

    # 预算和步数必须在 repair 的 invoke 之前判断。先调用再检查，钱已经
    # 花掉，护栏只会在账单上留下“已知超支”，不能阻止超支。
    if should_repair and len(steps) >= 2:
        reasons.append("repair未执行:步数已用尽")
    elif should_repair and total_cost_yuan >= budget_yuan:
        reasons.append("repair未执行:预算不足")
    elif should_repair:
        repair_prompt = (
            f"简历原文：\n{raw_text}\n\n"
            f"上一轮抽取结果：\n{data.model_dump_json()}\n\n"
            "代码校验发现以下问题：\n"
            + "\n".join(f"- {reason}" for reason in reasons)
            + "\n请修正这些问题。"
        )
        repair_step = len(steps) + 1
        repair_before = ledger.snapshot()

        try:
            repaired_data = fallback_chain.invoke({"raw": repair_prompt})
        except Exception as exc:
            repair_after = ledger.snapshot()
            repair_in, repair_out, repair_cost = _step_cost(
                repair_before,
                repair_after,
            )
            steps.append(
                StepLog(
                    step=repair_step,
                    action="repair",
                    tokens_in=repair_in,
                    tokens_out=repair_out,
                    cost_yuan=repair_cost,
                    ok=False,
                    note=f"repair厂商全挂 ({type(exc).__name__})",
                )
            )
            reasons.append("repair失败:厂商全挂")
        else:
            repair_after = ledger.snapshot()
            repair_in, repair_out, repair_cost = _step_cost(
                repair_before,
                repair_after,
            )
            steps.append(
                StepLog(
                    step=repair_step,
                    action="repair",
                    tokens_in=repair_in,
                    tokens_out=repair_out,
                    cost_yuan=repair_cost,
                    ok=True,
                    note="repair 调用并解析成功",
                )
            )
            data = repaired_data
            remaining_reasons = _collect_reasons(raw_text, data)
            if remaining_reasons:
                reasons.extend(
                    f"repair后仍需复核:{reason}"
                    for reason in remaining_reasons
                )
            else:
                reasons = []
                status = "ok"

    total_cost_yuan = sum(step.cost_yuan for step in steps)
    return AgentResult(
        status=status,
        data=data,
        reasons=reasons,
        steps=steps,
        total_cost_yuan=total_cost_yuan,
        prompt_version=PROMPT_VERSION,
    )


CASES = [
    ("①", "张三，3 年后端经验，会 Go 和 PHP", "ok"),
    ("②", "我还没毕业，会一点 Python", "needs_review"),
    ("③", "赵六，2016 年入行，去年离职，做后端", "needs_review"),
    ("④", "今天天气不错，多云转晴", "needs_review"),
    ("⑤", "岗位要求：3 年以上后端经验，熟悉 Go、MySQL", "needs_review"),
    ("⑥", "李雷，5 年 Python 开发，会 FastAPI", "ok"),
]


def _check_total_cost(total_cost_yuan: float) -> None:
    if total_cost_yuan > TOTAL_COST_LIMIT_YUAN:
        raise RuntimeError(
            f"总花费 {total_cost_yuan:.6f} 元超过 "
            f"{TOTAL_COST_LIMIT_YUAN:.2f} 元上限，已立即停止"
        )


def main() -> int:
    print("LangChain fallback + repair 演示")
    model_kind = "假模型" if FAKE_MODEL else "真模型"
    print(f"当前模型：{model_kind}（FAKE_MODEL={FAKE_MODEL}）")
    print(f"厂商链：{[str(item['name']) for item in PROVIDERS]}")

    matched = 0
    total_cost_yuan = 0.0

    for case_no, raw_text, hand_written_status in CASES:
        try:
            result = parse_resume_lc(raw_text)
        except RuntimeError:
            raise

        total_cost_yuan += result.total_cost_yuan
        _check_total_cost(total_cost_yuan)

        is_match = result.status == hand_written_status
        matched += int(is_match)
        match_text = "一致✓" if is_match else "不一致✗"
        print(
            f"{case_no}  手写={hand_written_status}  "
            f"LC={result.status}  {match_text}  "
            f"steps={len(result.steps)}  "
            f"cost={result.total_cost_yuan:.6f}  "
            f"reasons={result.reasons}"
        )

    print(f"{matched}/{len(CASES)} 一致  总花费={total_cost_yuan:.6f} 元")

    guard_result = parse_resume_lc(
        "今天天气不错",
        budget_yuan=0.0,
    )
    total_cost_yuan += guard_result.total_cost_yuan
    _check_total_cost(total_cost_yuan)

    assert len(guard_result.steps) == 1, (
        f"预算实测失败：steps={len(guard_result.steps)}，期望 1"
    )
    assert any("预算不足" in reason for reason in guard_result.reasons), (
        f"预算实测失败：reasons={guard_result.reasons}"
    )
    print(
        "预算实测  steps="
        f"{len(guard_result.steps)}  "
        f"cost={guard_result.total_cost_yuan:.6f}  "
        f"reasons={guard_result.reasons}"
    )
    print(f"全部用例后总花费={total_cost_yuan:.6f} 元（上限 0.01 元）")

    return 0 if matched == len(CASES) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
