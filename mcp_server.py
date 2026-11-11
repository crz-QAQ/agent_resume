# mcp 2.x API（MCPServer）；升级 3.x 前必须先读 migration guide：
# https://py.sdk.modelcontextprotocol.io/v2/migration/
"""通过本机 stdio 暴露简历解析、既有匹配流水线和 PDF 报告三个工具。"""

from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
from pathlib import Path
import sys
from uuid import uuid4

from mcp.server.mcpserver import MCPServer

# 路径锚定入口文件，客户端从其他目录启动也能导入同一套 app 模块。
# 插入根目录后才允许延迟导入业务包，避免出现两份同名模块。
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 与本项目 score.py 的文件契约保持一致，不在适配层增加解析格式。
# 大小在本地先行检查，无效文件不消耗抽取模型的费用。
RESUME_SUFFIXES = {".pdf", ".docx"}
MAX_RESUME_BYTES = 10 * 1024 * 1024
server = MCPServer(name="resume-agent", version="0.1.0")
# stdout 重定向影响整个进程；串行进入核心，防止并发恢复错位污染协议。
# 锁只约束核心调用，不改变 MCP 对工具参数的自动 schema 生成。
_CORE_LOCK = asyncio.Lock()


class InputError(ValueError):
    """只携带本模块编写的安全中文提示，不包含原文或底层异常详情。"""


def _read_resume(resume_path: str) -> tuple[Path, bytes]:
    """在加载模型之前校验路径、格式、大小及文件可读性。"""
    # 空路径不得被 Path 转成当前目录；NUL 不能传给文件系统。
    if not resume_path.strip() or "\x00" in resume_path:
        raise InputError("简历路径不能为空或包含无效字符，请提供文件路径。")
    # 相对路径以项目根为基准，与客户端自己的工作目录无关。
    path = Path(resume_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if path.suffix.lower() not in RESUME_SUFFIXES:
        raise InputError("简历仅支持 .pdf 或 .docx 文件，请转换格式后重试。")
    # 目录也不属于有效简历，不能交给底层解析器。
    if not path.is_file():
        raise InputError("简历文件不存在或不是文件，请检查 resume_path。")
    # 有界读取避免超大文件先占用内存；核心层仍保留自己的 10MB 护栏。
    with path.open("rb") as stream:
        raw = stream.read(MAX_RESUME_BYTES + 1)
    if len(raw) > MAX_RESUME_BYTES:
        raise InputError("简历超过 10MB 上限，请压缩文件后重试。")
    if not raw:
        raise InputError("简历文件为空，请提供有内容的 PDF 或 DOCX。")
    return path, raw


def _validate_jd(jd_text: str) -> None:
    """按 CLI 的口径校验去首尾空白后的 JD，向核心传递原始文本。"""
    # 首尾空白不能凑够 20 字，但校验成功后不得擅自改写 JD 原文。
    if len(jd_text.strip()) < 20:
        raise InputError("岗位 JD 至少需要 20 字，请补充完整的岗位描述。")


def _report_path(out_path: str | None, resume: Path) -> Path:
    """选择 PDF 目标，拒绝空路径、错误扩展名及覆盖输入简历。"""
    if out_path is None:
        # 随机文件名保证省略参数时不会覆盖之前生成的报告。
        # 默认目标只在项目 reports/ 中，返回时始终提供绝对路径。
        return PROJECT_ROOT / "reports" / f"match_{uuid4().hex}.pdf"
    if not out_path.strip() or "\x00" in out_path:
        raise InputError("报告路径不能为空或包含无效字符，请指定 .pdf 文件。")
    path = Path(out_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path = path.resolve()
    # 显式 out_path 按调用者指定保存，但输入文件必须保留。
    if path.suffix.lower() != ".pdf" or path.is_dir():
        raise InputError("报告输出路径必须指向 .pdf 文件。")
    if path == resume.resolve():
        raise InputError("报告路径不能与输入简历相同，请另选保存位置。")
    return path


def _error(exc: Exception) -> dict:
    """将所有工具内部异常转换为人读错误，隐藏堆栈、配置与简历正文。"""
    if isinstance(exc, InputError):
        # 此异常的消息全部是本模块固定文案，可以安全交给客户端。
        return {"error": str(exc)}
    # 文件路径、厂商响应和模型校验错误可能夹带隐私，不能直接 str(exc)。
    if isinstance(exc, OSError):
        return {"error": "文件读写失败，请检查路径、访问权限和剩余空间。"}
    if isinstance(exc, ValueError):
        return {"error": "简历或 JD 无法解析，请检查内容；扫描件需先转换为文本版。"}
    # 未知异常同样收口，不让 SDK 将业务错误包装成带堆栈的工具失败。
    return {"error": "处理失败，请检查模型依赖、.env 配置及网络后重试。"}


@server.tool()
async def parse_resume(resume_path: str) -> dict:
    """解析本机 PDF/DOCX，仅返回姓名、年限、技能、状态、原因和原文字数。"""
    try:
        path, raw = _read_resume(resume_path)
        async with _CORE_LOCK:
            # 延迟导入让错误输入无需模型配置；依赖导入失败也转换为 error。
            # 既有核心的 print 仅进入 stderr，stdout 保留给 MCP JSON-RPC。
            with redirect_stdout(sys.stderr):
                from app import resume_parser

                result = await resume_parser.parse_resume_file(path.name, raw)
        # 显式白名单，禁止 dataclass/model 整体序列化把 raw_text 带出进程。
        # 字数只来自原文长度；既不截取正文，也不把模型调用日志塞进返回值。
        return {
            "name": result.data.name if result.data else None,
            "years": result.data.years if result.data else None,
            "skills": result.data.skills if result.data else [],
            "status": result.status,
            "reasons": result.reasons,
            "raw_chars": len(result.raw_text),
        }
    except Exception as exc:
        return _error(exc)


@server.tool()
async def score_match(resume_path: str, jd_text: str) -> dict:
    """调用唯一匹配流水线，返回未经改分的 MatchReport JSON。"""
    try:
        _validate_jd(jd_text)
        path, raw = _read_resume(resume_path)
        async with _CORE_LOCK:
            # 打分、档位和证据全部由 app 生成；MCP 不计算或覆盖任何分项。
            with redirect_stdout(sys.stderr):
                from app import match_pipeline

                outcome = await match_pipeline.run_match(path.name, raw, jd_text)
        # MatchOutcome 不含简历正文，报告保留 PRD 约定的结构与逐项证据。
        # model_dump 仅序列化正式报告，不能把整个 outcome 或解析结果返回。
        return outcome.report.model_dump(mode="json")
    except Exception as exc:
        return _error(exc)


@server.tool()
async def make_report(resume_path: str, jd_text: str, out_path: str | None = None) -> dict:
    """用同一流水线评分并保存 PDF，返回绝对路径、总分和档位。"""
    try:
        _validate_jd(jd_text)
        path, raw = _read_resume(resume_path)
        output = _report_path(out_path, path)
        # 输出路径先校验，扩展名错误时不发生付费评分。
        async with _CORE_LOCK:
            # 渲染器只排版既有报告，不重新抽取或添加另一套打分逻辑。
            with redirect_stdout(sys.stderr):
                from app import match_pipeline, report_pdf

                outcome = await match_pipeline.run_match(path.name, raw, jd_text)
                pdf = report_pdf.render_report(outcome.report, outcome.job, resume_name=path.name)
        # 匹配和渲染成功后才创建目录、写入 PDF，返回值不包含报告正文。
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(pdf)
        # 仅暴露下载位置和两个摘要值，不把 PDF 字节灌入模型上下文。
        return {"report_path": str(output), "total": outcome.report.score.total,
                "verdict": outcome.report.verdict}
    except Exception as exc:
        return _error(exc)


if __name__ == "__main__":
    # SDK 使用 UTF-8 协议；底层日志也统一编码，避免 Windows 中文输出失败。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    server.run(transport="stdio")
