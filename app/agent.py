"""Day 11 N1：实现带 repair 护栏的简历解析 Agent。"""

import asyncio
import sys
from typing import Literal
from unittest.mock import patch

from pydantic import BaseModel, Field, ValidationError

from app.schemas import ResumeInfo
from app.fallback import call_with_fallback_async
from app.prompts import PROMPT_VERSION, SYSTEM_RESUME
# 包导入保证旧HTTP入口与S7b流水线共享同一ResumeInfo类型。
# 降级链及prompt本身保持既有实现，命名空间调整不改变抽取策略。


class StepLog(BaseModel):
    """记录一次抽取或修复的调用计量及结果。"""
    # 步骤号用于串联执行历史，不接收模型自报的调用费用。
    # action仅区分抽取与修复，调用成败由代码填写。
    # token计量来自当前调用返回的UsageMeter。
    # note记录阶段原因，不保存原始简历正文。
    step: int                    # 第几步，从 1 开始
    action: Literal["extract", "repair"]
    tokens_in: int
    tokens_out: int
    cost_yuan: float
    ok: bool
    note: str = ""


class AgentResult(BaseModel):
    """汇总简历Agent状态、可信字段、护栏原因与总成本。"""
    # 状态描述解析和护栏结果，HTTP状态码由上层决定。
    # 原始简历不属于结果字段，不会随结构化字段一起入库。
    # 步骤清单保留每次实际执行，便于追溯修复费用。
    # prompt版本用于区分解析约束的历史版本。
    status: Literal["ok", "needs_review", "failed"]
    data: ResumeInfo | None = None
    reasons: list[str] = Field(default_factory=list)
    steps: list[StepLog] = Field(default_factory=list)
    total_cost_yuan: float = 0.0
    prompt_version: str


repair_reason_keywords = ("空壳", "疑似把招聘要求当成工作经历")
# 只有这两类原因触发原有repair，相对时间等复核原因不会扩大调用范围。


def check_resume(raw_text: str, data: ResumeInfo) -> list[str]:
    """结合原文执行既有四条简历护栏，不请求模型。"""
    # 护栏以原文为依据，不允许模型自行声明已经通过。
    # 结构字段为空不等同于一次成功的解析。
    # 相对时间需要人工核对，不能在这里猜具体年份。
    # 招聘要求与候选人经历可能混淆，保持独立原因。
    # 学生身份只产生复核提示，不直接改写年限字段。
    # 所有原因由调用者决定是否触发修复，函数自身不修复。
    reasons: list[str] = []

    # 规则 1：空壳。
    if data.name is None and data.years is None and not data.skills:
        reasons.append("空壳:未抽取到任何字段")

    # 规则 2：只要原文出现相对时间词，就交人工确认。
    relative_words = ("去年", "今年", "目前", "至今", "现在")
    has_relative_time = any(word in raw_text for word in relative_words)
    if has_relative_time:
        reasons.append("相对时间:年限需人工确认")

    # 规则 3：招聘要求。原文是岗位描述，不是本人的工作经历。
    # ⚠️ 这里曾经写成 (has_requirement and is_student and data.years is not None)，
    #    多了一个 is_student 条件 —— 导致「纯招聘广告」被放行（实测返回 ok）。
    #    是后来用 Dify 复刻同一需求时才发现的：换一种实现复刻，本身就是一次 code review。
    requirement_words = ("岗位要求", "招", "任职要求")
    student_words = ("应届", "未毕业", "我是学生", "没毕业", "还没毕业", "在校")
    has_requirement = any(word in raw_text for word in requirement_words)
    is_student = any(word in raw_text for word in student_words)
    if has_requirement:
        reasons.append("疑似把招聘要求当成工作经历")

    # 规则 4：学生身份的工作年限需人工确认。
    if is_student:
        reasons.append("学生身份:工作年限需人工确认")

    return reasons


async def parse_resume_agent(
    raw_text: str,
    max_steps: int = 3,
    budget_yuan: float = 0.02,
) -> AgentResult:
    """依次抽取、校验及按预算修复，返回原有Agent契约。"""
    # 首次调用与修复共享同一降级链，避免两套厂商选择顺序。
    # 预算检查发生在修复调用之前，已经花掉的费用不会归零。
    # 只有指定护栏原因能够触发修复，复核不等于一定重调模型。
    # JSON不可解析时没有可信字段，返回明确的失败状态。
    # 修复失败仍保留已发生步骤，不伪装成一次成功抽取。
    # 每个实际调用单独记费，最终总成本由步骤计量合并。
    # 模型依赖不可用与输入字段不可信是两类不同问题。
    # 包式导入只统一类型来源，不改变本函数的护栏条件。
    steps: list[StepLog] = []

    # ① extract：固定调用一次模型，并解析结构化结果。
    try:
        text, meter = await call_with_fallback_async(
            raw_text,
            system=SYSTEM_RESUME,
        )
    except RuntimeError:
        # 首次抽取时厂商全挂属于依赖故障，交由接口层映射为 HTTP 503。
        raise

    cost_yuan = meter.cost()
    try:
        data = ResumeInfo.model_validate_json(text)
    except (ValidationError, ValueError) as exc:
        steps.append(
            StepLog(
                step=1,
                action="extract",
                tokens_in=meter.prompt_tokens,
                tokens_out=meter.completion_tokens,
                cost_yuan=cost_yuan,
                ok=False,
                note=f"extract JSON彻底不可解析:{type(exc).__name__}",
            ),
        )
        return AgentResult(
            status="failed",
            reasons=[
                f"extract失败:JSON彻底不可解析 ({type(exc).__name__})"
            ],
            steps=steps,
            total_cost_yuan=sum(item.cost_yuan for item in steps),
            prompt_version=PROMPT_VERSION,
        )

    steps.append(
        StepLog(
            step=1,
            action="extract",
            tokens_in=meter.prompt_tokens,
            tokens_out=meter.completion_tokens,
            cost_yuan=cost_yuan,
            ok=True,
            note="extract 调用并解析成功",
        ),
    )

    # ② 纯代码校验。
    reasons = check_resume(raw_text, data)
    status = "needs_review" if reasons else "ok"

    # ③ repair：仅在调用前同时满足触发原因、步数和预算约束时执行一次。
    should_repair = any(
        keyword in reason
        for reason in reasons
        for keyword in repair_reason_keywords
    )
    total_cost_yuan = sum(item.cost_yuan for item in steps)

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
            + "\n请修正这些问题，并只输出符合要求的 JSON 对象。"
        )
        repair_step = len(steps) + 1

        try:
            repair_text, repair_meter = await call_with_fallback_async(
                repair_prompt,
                system=SYSTEM_RESUME,
            )
        except RuntimeError as exc:
            steps.append(
                StepLog(
                    step=repair_step,
                    action="repair",
                    tokens_in=0,
                    tokens_out=0,
                    cost_yuan=0.0,
                    ok=False,
                    note=f"repair厂商全挂:{type(exc).__name__}",
                ),
            )
            reasons.append("repair失败:厂商全挂")
        else:
            repair_cost_yuan = repair_meter.cost()
            try:
                repaired_data = ResumeInfo.model_validate_json(repair_text)
            except (ValidationError, ValueError) as exc:
                steps.append(
                    StepLog(
                        step=repair_step,
                        action="repair",
                        tokens_in=repair_meter.prompt_tokens,
                        tokens_out=repair_meter.completion_tokens,
                        cost_yuan=repair_cost_yuan,
                        ok=False,
                        note=f"repair JSON彻底不可解析:{type(exc).__name__}",
                    ),
                )
                reasons.append(
                    f"repair失败:JSON彻底不可解析 ({type(exc).__name__})"
                )
            else:
                steps.append(
                    StepLog(
                        step=repair_step,
                        action="repair",
                        tokens_in=repair_meter.prompt_tokens,
                        tokens_out=repair_meter.completion_tokens,
                        cost_yuan=repair_cost_yuan,
                        ok=True,
                        note="repair 调用并解析成功",
                    ),
                )
                data = repaired_data
                remaining_reasons = check_resume(raw_text, data)
                if remaining_reasons:
                    reasons.extend(
                        f"repair后仍需复核:{reason}"
                        for reason in remaining_reasons
                    )
                else:
                    reasons = []
                    status = "ok"

    # ④ 收口：所有实际调用均已记入 steps，再汇总成本。
    # 汇总仍使用每步实际计量，不把预算上限当成已经发生的费用。
    # 这也是S7b计算全链路费用时采用的简历抽取成本来源。
    total_cost_yuan = sum(item.cost_yuan for item in steps)
    return AgentResult(
        status=status,
        data=data,
        reasons=reasons,
        steps=steps,
        total_cost_yuan=total_cost_yuan,
        prompt_version=PROMPT_VERSION,
    )


if __name__ == "__main__":
    # 自检替身只在直接执行时创建，包导入不会执行这些模型模拟场景。
    # 因此TestClient加载API时不会产生额外调用或测试输出。
    class _FakeMeter:
        """为本模块自检提供固定的零网络计量替身。"""
        # 该类只在直接运行模块时定义，生产请求不会使用。
        # 受控费用用于验证预算分支，不表示真实账单。
        prompt_tokens = 10
        completion_tokens = 5

        def cost(self) -> float:
            """返回自检使用的固定模拟费用。"""
            # 模拟值只用于验证控制流，实际费用仍从真实UsageMeter取得。
            return 0.001

    empty_shell_json = '{"name": null, "years": null, "skills": []}'
    case4_calls: list[str] = []

    async def always_empty_shell(
        prompt: str,
        *,
        system: str | None = None,
    ) -> tuple[str, _FakeMeter]:
        """自检替身：持续返回空壳，验证失败与修复收口。"""
        # 替身不访问网络，因此可以独立验证护栏。
        # 空壳字段保持固定，避免随机模型输出影响自检。
        # 调用历史用于核实是否发生多余修复。
        case4_calls.append(prompt)
        return empty_shell_json, _FakeMeter()

    with patch.object(
        sys.modules[__name__],
        "call_with_fallback_async",
        always_empty_shell,
    ):
        case4 = asyncio.run(parse_resume_agent("李四，会 Python"))
    assert case4.status == "needs_review", f"④失败: status={case4.status}"
    assert len(case4_calls) == 2, "④失败: repair 应只尝试一次"

    case5_calls: list[str] = []

    async def repair_triggering_fake(
        prompt: str,
        *,
        system: str | None = None,
    ) -> tuple[str, _FakeMeter]:
        """自检替身：按调用阶段提供触发修复的受控结果。"""
        # 受控场景只验证步数和预算，不代替生产模型评分。
        # 修复调用与首次调用使用同一签名，防止协议差异被隐藏。
        # 测试输出仍经过Agent原有校验路径。
        case5_calls.append(prompt)
        return empty_shell_json, _FakeMeter()

    with patch.object(
        sys.modules[__name__],
        "call_with_fallback_async",
        repair_triggering_fake,
    ):
        case5 = asyncio.run(
            parse_resume_agent(
                "我还没毕业，会一点 Python",
                budget_yuan=0.0,
            )
        )
    assert case5.status == "needs_review", f"⑤失败: status={case5.status}"
    assert "预算" in "".join(case5.reasons), f"⑤失败: {case5.reasons}"
    assert len(case5.steps) == 1, "⑤失败: 预算不足时不应调用 repair"
    assert len(case5_calls) == 1, "⑤失败: budget_yuan=0.0 仍调用了 repair"

    print("用例④⑤通过")
