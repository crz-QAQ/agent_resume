"""简历岗位匹配的 CLI：只调用既有链路或 HTTP 接口，不实现评分。"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stdout
from pathlib import Path
import sys
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    # 类型提示不在运行时加载依赖，--help 仍可独立使用。
    import httpx

    from app.schemas_match import MatchReport


# 目录层级是交付契约；从任意工作目录运行时均能定位项目模块。
PROJECT_ROOT = Path(__file__).resolve().parents[3]
# CLI 仅接受 PDF/DOCX，不扩大底层解析器支持的文件范围。
RESUME_SUFFIXES = {".pdf", ".docx"}


class CliError(Exception):
    """携带可展示的中文说明及约定的整数退出码。"""

    def __init__(self, message: str, exit_code: int) -> None:
        """保存错误说明和退出码，不暴露底层异常或配置值。"""
        super().__init__(message)
        # 只允许入口把此说明写入 stderr，业务函数不自行打印结果。
        self.exit_code = exit_code


def build_parser() -> argparse.ArgumentParser:
    """按任务书声明参数及两个必填的互斥组。"""
    parser = argparse.ArgumentParser(description="对照岗位 JD 生成简历匹配报告。")
    # local/api 必须明确选择，不能静默切换调用方式。
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--local", action="store_true", help="调用本地匹配链路")
    mode.add_argument("--api", action="store_true", help="调用已启动的匹配服务")
    parser.add_argument("--resume", type=Path, required=True, help="简历 PDF 或 DOCX")
    # JD 文件与直接文本同样二选一，缺失参数由 argparse 以 2 退出。
    jd = parser.add_mutually_exclusive_group(required=True)
    jd.add_argument("--jd", type=Path, help="UTF-8 编码的 JD 文本文件")
    jd.add_argument("--jd-text", help="完整岗位 JD 文本")
    # 导出与输出格式相互独立，--json 也可以同时保存 PDF。
    parser.add_argument("--out", type=Path, help="PDF 报告保存路径")
    parser.add_argument("--json", action="store_true", help="仅输出 MatchReport JSON")
    # 地址仅传给 API 模式，不影响本地链路或其配置加载方式。
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="匹配服务地址")
    return parser


def read_inputs(args: argparse.Namespace) -> tuple[Path, bytes, str]:
    """检查简历文件和 JD，在任何模型或网络调用之前拒绝输入错误。"""
    resume_path = args.resume.expanduser()
    if resume_path.suffix.lower() not in RESUME_SUFFIXES:
        raise CliError("输入错误：简历仅支持 .pdf 或 .docx 文件。", 2)
    # 文件不存在、目录冒充文件和读取权限问题都属于输入不可读。
    try:
        raw_bytes = resume_path.read_bytes()
    except OSError as exc:
        raise CliError("输入错误：简历文件不存在或无法读取，请检查路径和权限。", 2) from exc
    # 空文件在本地拒绝，避免向服务发送必然无法解析的材料。
    if not raw_bytes:
        raise CliError("输入错误：简历文件为空，请提供有效简历。", 2)
    # UTF-8 BOM 属于文件编码标记，不应被当作 JD 的正文字符。
    try:
        jd_text = args.jd.expanduser().read_text(encoding="utf-8-sig") if args.jd else args.jd_text
    except (OSError, UnicodeError) as exc:
        raise CliError("输入错误：JD 文件不存在、不可读或不是 UTF-8 编码。", 2) from exc
    # 只用去首尾空白后的长度验证；传给链路的正文保持原样。
    if len(jd_text.strip()) < 20:
        raise CliError("输入错误：岗位 JD 至少需要 20 字，请补充完整描述。", 2)
    return resume_path, raw_bytes, jd_text


def save_pdf(path: Path, content: bytes) -> None:
    """保存既有渲染器或下载接口给出的 PDF，写入失败统一归为内部错误。"""
    # 写入错误不能与读取简历失败混淆，也不能先打印成功报告。
    try:
        path.write_bytes(content)
    except OSError as exc:
        raise CliError("PDF 保存失败：请检查输出目录、写入权限和剩余空间。", 4) from exc


def run_local(resume_path: Path, raw_bytes: bytes, jd_text: str, out: Path | None) -> MatchReport:
    """用 asyncio.run 调用唯一匹配入口，并按需调用现成 PDF 渲染器。"""
    # 延迟导入，参数错误和 API 模式不需要加载本地模型依赖。
    from app.file_parser import FileParseError, ScannedPdfError
    from app.job_parser import JobParseError
    from app.match_pipeline import ModelUnavailableError, run_match
    from app.report_pdf import render_report

    # 文件解析与岗位解析沿用原异常类型，不复制任何抽取或评分逻辑。
    try:
        outcome = asyncio.run(run_match(resume_path.name, raw_bytes, jd_text))
    except ScannedPdfError as exc:
        raise CliError("输入错误：简历没有可解析的文本，请提供含文本层的 PDF 或有效 DOCX。", 2) from exc
    except (FileParseError, JobParseError) as exc:
        raise CliError("输入错误：简历或岗位 JD 无法解析，请检查文件内容与岗位描述。", 2) from exc
    except ModelUnavailableError as exc:
        # 任务书的退出码 3 用于 API 连接失败或 503，本地链路故障为 4。
        raise CliError("本地匹配失败：模型不可用，请检查 .env、网络和本地模型依赖。", 4) from exc
    if out is not None:
        # 单独包住渲染阶段，确保渲染异常不会被误判成输入错误。
        try:
            pdf = render_report(outcome.report, outcome.job, resume_name=resume_path.name)
        except Exception as exc:
            raise CliError("PDF 生成失败：请检查报告渲染依赖与字体配置。", 4) from exc
        save_pdf(out, pdf)
    return outcome.report


def check_response(response: httpx.Response, *, download: bool = False) -> None:
    """把上传和下载接口的 HTTP 状态映射为固定退出码及中文说明。"""
    # 两个接口都可能返回 503，下载阶段也必须保留服务不可用语义。
    status = response.status_code
    if status == 503:
        raise CliError("服务暂时不可用（HTTP 503），请稍后重试。", 3)
    if 200 <= status < 300:
        return
    # 下载的 404 是报告失效，不能当作简历不存在的输入错误。
    if not download and status in {400, 413, 415, 422}:
        if status == 413:
            raise CliError("输入错误：简历超过服务的 10MB 上传限制。", 2)
        # 服务端会统一隐藏解析细节，因此保留对无文本层文件的条件提示。
        raise CliError("输入错误：简历或 JD 无法解析；若没有可解析的文本，请改用文本版简历。", 2)
    action = "PDF 下载" if download else "匹配请求"
    raise CliError(f"{action}失败（HTTP {status}），请检查服务或重新生成报告。", 4)


def run_api(args: argparse.Namespace, resume_path: Path, raw_bytes: bytes,
            jd_text: str, out: Path | None) -> MatchReport:
    """先上传简历和 JD，成功后仅在指定 out 时下载报告 PDF。"""
    import httpx

    # API 模式只复用报告数据结构，不导入本地推理链路。
    from app.schemas_match import MatchReport

    # 去掉尾斜杠，保持用户提供的地址前缀；不拼入本机文件路径。
    base_url = args.base_url.rstrip("/")
    content_type = "application/pdf" if resume_path.suffix.lower() == ".pdf" else (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    try:
        # 首次模型加载可能较慢，连接与整个响应分别设置超时。
        with httpx.Client(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
            response = client.post(
                f"{base_url}/match/score",
                # 表单字段名严格对应现有 FastAPI 端点。
                files={"file": (resume_path.name, raw_bytes, content_type)},
                data={"jd_text": jd_text},
            )
            check_response(response)
            payload = response.json()
            # 复用报告契约校验接口响应，stdout 不包含 report_id 等包装字段。
            report = MatchReport.model_validate(payload["report"])
            if out is not None:
                # ID 仅作为一个 URL 路径段，不允许改变下载端点的层级。
                report_id = quote(payload["report_id"], safe="")
                response = client.get(f"{base_url}/match/report/{report_id}")
                check_response(response, download=True)
                save_pdf(out, response.content)
            # 无 --out 时到此结束，不额外发起下载请求。
            return report
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise CliError("服务未启动：先运行 tools/start_server.bat", 3) from exc
    except (httpx.InvalidURL, httpx.UnsupportedProtocol) as exc:
        raise CliError("输入错误：--base-url 必须是有效的 HTTP 或 HTTPS 服务地址。", 2) from exc
    except httpx.RequestError as exc:
        # 已建立连接后的超时或响应传输错误属于其余内部错误。
        raise CliError("服务请求失败：响应超时或传输中断，请检查服务后重试。", 4) from exc


def format_summary(report: MatchReport, out: Path | None) -> str:
    """按任务书规定的字段顺序显示上游报告，建议只取前三条且保留前缀。"""
    breakdown = report.score.breakdown
    # 所有得分、档位与状态均读取报告，不在 CLI 里重新计算。
    lines = [
        f"匹配结果：{report.score.total}/100  档位：{report.verdict}  状态：{report.status}",
        f"分项：硬性 {breakdown['hard']}/40 · 语义 {breakdown['semantic']}/30 · LLM复核 {breakdown['llm']}/30",
        f"扣分：{len(report.deductions)} 条" if report.deductions else "扣分：无扣分项",
        f"缺口：{len(report.missing)} 条" if report.missing else "缺口：无缺口",
        "建议：前 3 条",
    ]
    # 只加缩进，不改写 [LLM]/[缺口] 前缀和建议原文。
    lines.extend(f"  {suggestion}" for suggestion in report.suggestions[:3])
    if out is not None:
        lines.append(f"PDF：{out}")
    # 成本已经由编排层合计；直接展示报告中的元值。
    lines.append(f"成本：¥{report.meta['cost_yuan']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """执行 CLI 并返回约定整数，所有运行错误仅向 stderr 写人读说明。"""
    # Windows 管道也使用 UTF-8，确保中文 JSON 与人读版符号均可输出。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        resume_path, raw_bytes, jd_text = read_inputs(args)
        out = args.out.expanduser().resolve() if args.out is not None else None
        # 必须先插入项目根再导入 app；API 模式也复用轻量报告模型。
        sys.path.insert(0, str(PROJECT_ROOT))
        # 原链路及字体选择会 print 日志；统一转到 stderr，保持 stdout 可解析。
        with redirect_stdout(sys.stderr):
            if args.local:
                report = run_local(resume_path, raw_bytes, jd_text, out)
            else:
                report = run_api(args, resume_path, raw_bytes, jd_text, out)
        # PDF 已成功保存才输出最终结果，失败时不会留下貌似成功的 JSON。
        # JSON 由现成模型序列化，避免手工拼接遗漏报告字段。
        output = report.model_dump_json(indent=2) if args.json else format_summary(report, out)
        print(output)
        return 0
    except CliError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        # 用户中断也不泄露 asyncio 的 traceback。
        print("操作已中断，未完成匹配报告。", file=sys.stderr)
        return 4
    except Exception:
        # 不回显未知异常，避免泄露模型配置、密钥或简历正文。
        print("内部错误：报告生成失败，请检查依赖、配置及输出路径。", file=sys.stderr)
        return 4


if __name__ == "__main__":
    # 仅以整数退出，禁止把错误字符串交给 sys.exit。
    sys.exit(main())
