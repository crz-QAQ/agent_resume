"""Day 8 N3：用 DeepSeek 抽取结构化简历信息。"""
from __future__ import annotations

import os

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError
from app.prompts import SYSTEM_RESUME, PROMPT_VERSION

load_dotenv()

client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
)
MODEL = os.getenv("LLM_MODEL", "deepseek-chat")


class ResumeInfo(BaseModel):
    """保留姓名、年限及技能三个既有抽取字段。"""
    # 姓名没有可信依据时允许为空。
    # 年限上限与下限沿用原模型约束。
    # 技能用独立列表实例，避免请求之间共享可变默认值。
    # 此类型由app包统一导入，避免Pydantic类型身份重复。
    name: str | None = None
    years: float | None = Field(default=None, ge=0, le=60)
    skills: list[str] = Field(default_factory=list)


def extract_resume(raw_text: str) -> ResumeInfo | None:
    """按原提示词抽取简历，结构校验失败时执行既有修复。"""
    # system约束与用户原文保持两条独立消息。
    # 模型温度和token上限沿用原实现，导入整理不改调用参数。
    # 返回字段必须通过ResumeInfo结构校验。
    # 首次错误只生成修复反馈，不能伪造成功结果。
    # 两次解析仍失败时返回None供调用方处理。
    # 错误字段用于模型修复，不写入持久化简历缓存。
    # 本函数属于旧同步抽取示例，新匹配走独立异步流水线。
    # 这里只调整模块归属，不增加新的模型请求。
    # 费用及fallback策略继续由相应调用入口维护。
    # 包式导入让命令行和服务使用同一提示词版本。
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_RESUME},
        {"role": "user", "content": raw_text},
    ]

    for attempt in range(2):
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0,
            max_tokens=500,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or ""
        try:
            return ResumeInfo.model_validate_json(raw)
        except (ValidationError, ValueError) as e:
            if attempt == 1:
                return None
            error_text = "; ".join(
                f"{item.get('loc', '?')}: {item.get('msg', '校验失败')}"
                for item in e.errors()
            )
            messages.append(
                {"role": "user", "content": f"上次输出校验失败：{error_text}。请重试，只输出合法 JSON。"}
            )
    return None


if __name__ == "__main__":
    test_inputs = [
        "张三，3 年后端经验，使用 Go 和 PHP",
        "李四，会 Python 和 Redis",
        "王五，五年经验，使用 MySQL",
    ]
    for text in test_inputs:
        print(f"输入：{text} -> 解析结果：{extract_resume(text)}")
    r1 = extract_resume("张三，3 年后端经验，会 Go 和 PHP")
    assert r1 is not None and r1.years == 3.0, f"①失败: {r1}"

    r2 = extract_resume("李四，会 Python 和 Redis")
    assert r2 is not None and r2.years is None, f"②失败: 期望 None，实际 {r2.years}"

    r3 = extract_resume("王五，五年经验，会 MySQL")
    assert r3 is not None and r3.years == 5.0, f"③失败: {r3}"

    print("3/3 通过 ✓")
