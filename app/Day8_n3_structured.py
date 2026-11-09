"""Day 8 N3：用 DeepSeek 抽取结构化简历信息。"""
from __future__ import annotations

import os

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError
from prompts import SYSTEM_RESUME, PROMPT_VERSION

load_dotenv()

client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
)
MODEL = os.getenv("LLM_MODEL", "deepseek-chat")


class ResumeInfo(BaseModel):
    name: str | None = None
    years: float | None = Field(default=None, ge=0, le=60)
    skills: list[str] = Field(default_factory=list)


def extract_resume(raw_text: str) -> ResumeInfo | None:
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
