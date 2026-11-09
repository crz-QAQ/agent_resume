"""统一文件简历的解析入口，保留原文、Agent 状态及实际费用。

只封装现有文件解析器与简历 Agent，让后续模块不必重复处理输入边界。
不改变底层解析规则，也不进行匹配、打分或额外的模型调用。
"""

from dataclasses import dataclass
from pathlib import Path
import sys

# agent.py 仍通过顶层名称导入 schemas/fallback/prompts；兼容既有导入方式，
# 避免为支持 from app.resume_parser 导入而改动用户禁止修改的 Agent。
_APP_DIR = str(Path(__file__).resolve().parent)
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

# 类型沿用 Agent 实际使用的模型，避免包导入与顶层导入产生不同的类对象。
from app.agent import ResumeInfo, parse_resume_agent
from app.file_parser import (
    FileParseError,
    ScannedPdfError,
    UnsupportedFormatError,
    extract_text,
)


# S2 §一.1：与现有 /resume/upload 的 10MB 上限一致，在解析前拦截超限文件。
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@dataclass
class ResumeParseResult:
    """承载统一的简历抽取结果；文件解析失败时抛异常，不创建该结果。"""

    # 沿用 Agent 的 ok / needs_review / failed，不在封装层重新判断状态。
    status: str
    # 保留 Agent 原始数据，failed 时允许为 None。
    data: ResumeInfo | None
    # 护栏原因原样传递，避免丢失相对时间等人工复核依据。
    reasons: list[str]
    # S6 的 evidence 从这里截取，必须原样保留 extract_text 返回的文本。
    raw_text: str
    # Agent 的 total_cost_yuan 已包含 repair 费用，不能只取首次抽取成本。
    cost_yuan: float
    # 文件名保留调用方输入，供下游标识对应的简历文件。
    file_name: str


async def parse_resume_file(filename: str, raw: bytes) -> ResumeParseResult:
    """把文件字节交给已有解析器与 Agent，并保留可追溯的原文。

    参数：filename 为原始文件名，raw 为文件字节。
    返回：原样映射 Agent 状态、数据、原因与总成本的 ResumeParseResult。
    异常：超限或空白文本抛 FileParseError；FileParseError 及其子类、
        Agent 的依赖异常均直接向上抛出，不返回伪造的空结果。
    """
    if len(raw) > MAX_UPLOAD_BYTES:
        raise FileParseError("文件超过 10MB 上限")

    # 不捕获解析异常，确保扫描件、损坏文件及不支持格式保留各自异常类型。
    raw_text = extract_text(filename, raw)
    # strip 仅用于判空，不能覆盖 raw_text，否则 S6 的证据片段可能失真。
    if not raw_text.strip():
        raise FileParseError("文件没有可解析的文本")

    # 只调用现有 Agent；它已有的抽取、repair 与护栏行为全部沿用。
    result = await parse_resume_agent(raw_text)
    return ResumeParseResult(
        status=result.status,
        data=result.data,
        reasons=result.reasons,
        raw_text=raw_text,
        cost_yuan=result.total_cost_yuan,
        file_name=filename,
    )
