"""Day 17 N2：用 LangChain 重写 Day 11 的简历 extract 段。

本文件只替换 Day 11 中的 extract 环节，不把 repair、护栏和成本统计
强行塞进 LangChain。这样可以直接观察框架替我们做了什么，以及哪些
业务控制仍然需要手写。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda
from langchain_openai import ChatOpenAI

from Day11_agent import check_resume
from Day8_n3_structured import ResumeInfo
from Day9_fallback import UsageMeter
from prompts import SYSTEM_RESUME


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# 默认先用假模型验证链结构，避免首次调试就消耗网络请求和模型费用。
# 设置 FAKE_MODEL=false 时切换到 ChatOpenAI，进行 N3 真实模型对照。
FAKE_MODEL = os.getenv("FAKE_MODEL", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# SYSTEM_RESUME 必须从 prompts.py 导入，不能在这里复制一份 prompt。
# 但它包含 {"name": ...} 这样的 JSON 示例；ChatPromptTemplate 使用
# f-string 语法，会把花括号误认为模板变量。因此只在构造模板时转义，
# 不修改 prompts.py。格式化后的 system 内容仍与 SYSTEM_RESUME 完全一致。
SYSTEM_RESUME_TEMPLATE = SYSTEM_RESUME.replace("{", "{{").replace("}", "}}")

prompt = ChatPromptTemplate.from_messages(
    [
        # system 负责约束模型只能输出 ResumeInfo 对应的三个字段。
        ("system", SYSTEM_RESUME_TEMPLATE),
        # raw 是 invoke 时传入的简历原文，不与 prompt 字符串硬编码在一起。
        ("user", "{raw}"),
    ]
)


class _FakeStructuredModel(RunnableLambda):
    """给假模型补一个结构化输出接口，但内部仍是 RunnableLambda。

    ChatOpenAI.with_structured_output() 会把 Pydantic 模型转换成工具定义，
    并在模型发起 tool_call 后解析参数。假模型不需要真的走这一套协议，
    with_structured_output() 直接返回自身，因此 lambda 可以直接产生
    ResumeInfo，专门用于免费验证 prompt、链连接和返回类型。
    """

    def with_structured_output(
        self,
        schema: type[ResumeInfo],
        **_: object,
    ) -> _FakeStructuredModel:
        return self


# 假模型直接返回 ResumeInfo；N3 切成真模型时，这一段仍然保留。
fake_model = _FakeStructuredModel(
    lambda _: ResumeInfo(name="张三", years=3.0, skills=["Go"])
)

# 真模型配置全部来自项目根目录 .env 中的三个变量。temperature=0 用于
# 减少抽取结果的随机性；timeout 和 max_retries 则避免单次请求无限等待。
llm = (
    fake_model
    if FAKE_MODEL
    else ChatOpenAI(
        model=os.getenv("LLM_MODEL", "deepseek-chat"),
        base_url=os.getenv("LLM_BASE_URL"),
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        temperature=0,
        timeout=60,
        max_retries=1,
    )
)

# 与手写 json.loads + ResumeInfo.model_validate_json 相比，这里多了：
#   1. ResumeInfo 自动转成模型的 function/tool schema，模型知道字段要求；
#   2. LangChain 负责取出 tool_call 参数，不再手工截取 JSON 代码块；
#   3. 返回值已经过 Pydantic 模型校验，正常时直接得到 ResumeInfo。
# 这里也少了：
#   1. 对原始响应的完全控制，默认看不到 tool_call 的完整中间结构；
#   2. 自己实现的解析重试、多厂商 fallback、token 和费用统计；
#   3. 对“模型没触发 tool_call”的自动兜底，此时仍可能返回 None。
chain = prompt | llm.with_structured_output(
    ResumeInfo,
    method="function_calling",
)


def extract_resume(raw_text: str) -> ResumeInfo | None:
    """调用 LCEL 链；None 表示模型没有返回可解析的 tool_call。"""
    payload = {"raw": raw_text}
    print(f"invoke payload keys = {list(payload.keys())}")
    print(f"invoke 传入原文 = {raw_text!r}")
    return chain.invoke(payload)


def verify_schema_binding() -> None:
    """只验证真实 ChatOpenAI 的 schema 绑定，不调用模型、不发送网络请求。

    ChatOpenAI 的构造函数仅创建客户端和配置。with_structured_output() 会先把
    ResumeInfo 转成 function/tool 定义并绑定到模型，同样不会触发网络调用；
    只有后续 invoke() 才会真正请求模型。这里直接检查绑定参数，因此可以在
    默认的假模型模式下同时验证“真假两条路径”。
    """
    real_llm = ChatOpenAI(
        model=os.getenv("LLM_MODEL"),
        base_url=os.getenv("LLM_BASE_URL"),
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        temperature=0,
        timeout=60,
        max_retries=1,
    )
    bound = real_llm.with_structured_output(
        ResumeInfo,
        method="function_calling",
    )

    # 不同 LangChain 版本可能把绑定参数放在 bound.kwargs，也可能在
    # RunnableSequence 的第一个绑定步骤中。两种布局都不影响本次验证。
    bound_kwargs = (
        bound.kwargs
        if hasattr(bound, "kwargs")
        else bound.first.kwargs
    )
    tools = bound_kwargs["tools"]
    tool = tools[0]
    function_schema = tool.get("function", tool)
    properties = function_schema["parameters"]["properties"]
    field_names = list(properties.keys())

    assert {"name", "years", "skills"}.issubset(field_names), (
        f"schema 绑定缺少字段，实际字段为 {field_names}"
    )
    print(f"真实 ChatOpenAI 绑定字段 = {field_names}")


def _cost_from_usage_metadata(
    usage_metadata: dict[str, dict],
) -> float:
    """把 LangChain usage metadata 转成项目现有 UsageMeter 的费用口径。"""
    meter = UsageMeter()
    for usage in usage_metadata.values():
        input_token_details = usage.get("input_token_details") or {}
        meter.add(
            SimpleNamespace(
                prompt_tokens=int(usage.get("input_tokens", 0) or 0),
                completion_tokens=int(usage.get("output_tokens", 0) or 0),
                prompt_cache_hit_tokens=int(
                    input_token_details.get("cache_read", 0) or 0
                ),
            )
        )
    return meter.cost()


def run_real_model_comparison() -> int:
    """用真模型跑 4 条 LC 对照，不重新调用手写 Agent。"""
    if FAKE_MODEL:
        raise RuntimeError("N3 对照必须设置 FAKE_MODEL=false")

    # 用例文字和手写期望状态原样取自 Day11_n3_check.py。这里只把期望值
    # 当常量使用，绝不调用 parse_resume_agent，以免为了对照重复花钱。
    cases = [
        ("①", "张三，3 年后端经验，会 Go 和 PHP", "ok"),
        ("②", "我还没毕业，会一点 Python", "needs_review"),
        ("③", "赵六，2016 年入行，去年离职，做后端", "needs_review"),
        ("④", "今天天气不错，多云转晴", "needs_review"),
    ]

    matched = 0
    total_cost = 0.0
    for case_no, text, handwritten_status in cases:
        usage_callback = UsageMetadataCallbackHandler()
        data = chain.invoke(
            {"raw": text},
            config={"callbacks": [usage_callback]},
        )

        if data is None:
            lc_status = "failed"
            reason = "extract 未触发 tool_call"
        else:
            # 这里复用 Day11 的 check_resume，而不是在 LC 文件里重写一套。
            # “是否需要人工复核”是业务护栏，不是 LangChain 的解析职责；
            # 手写版和 LC 版应共享同一事实来源，否则两份规则一旦修改不同步，
            # 对照结果就会失去意义，而且面试时也无法证明两种实现行为一致。
            reasons = check_resume(text, data)
            lc_status = "needs_review" if reasons else "ok"
            reason = "；".join(reasons)

        cost_yuan = _cost_from_usage_metadata(usage_callback.usage_metadata)
        total_cost += cost_yuan

        # 预算是硬护栏：一旦超过 0.005 元就立即终止，不继续后面的用例。
        if total_cost > 0.005:
            print(
                f"错误：总花费 {total_cost:.4f} 元超过上限 0.005 元，"
                "已立即停止。",
                file=sys.stderr,
            )
            sys.exit(1)

        is_match = lc_status == handwritten_status
        matched += int(is_match)
        match_text = "一致 ✓" if is_match else "不一致 ✗"
        print(
            f"{case_no} 手写={handwritten_status:<12} "
            f"LC={lc_status:<12} {match_text}   cost={cost_yuan:.4f}"
        )
        if reason and not is_match:
            print(f"   LC reasons: {reason}")

    print(f"{matched}/{len(cases)} 一致  总花费={total_cost:.4f} 元")
    return 0 if matched == len(cases) else 1


if __name__ == "__main__":
    sample = "张三，3 年后端经验，会 Go 和 PHP"
    raw_text = sys.argv[1] if len(sys.argv) > 1 else sample

    model_kind = "假模型" if FAKE_MODEL else "真模型"
    print(f"当前模型：{model_kind}（FAKE_MODEL={FAKE_MODEL}）")
    verify_schema_binding()

    if not FAKE_MODEL:
        raise SystemExit(run_real_model_comparison())

    print(f"输入：{raw_text}")
    data = extract_resume(raw_text)

    # with_structured_output 在模型没有发起匹配的 tool_call 时可能返回
    # None。空值不能继续经过属性访问或冒充成功，必须明确失败并让脚本
    # 以非零状态退出，方便手工运行和自动化流程同时发现问题。
    if data is None:
        print(
            "错误：模型没有触发 tool_call，未得到结构化数据。",
            file=sys.stderr,
        )
        sys.exit(1)

    assert isinstance(data, ResumeInfo), "链没有返回 ResumeInfo"
    print(f"输出：{data.model_dump_json()}")
    print(f"类型：{type(data).__name__}")

    # 假模型返回固定值，额外断言可以防止链连接错或开关失效。
    if FAKE_MODEL:
        assert data == ResumeInfo(name="张三", years=3.0, skills=["Go"])
    print("chain OK")
