"""D4：官方客户端真实连接 stdio，验证工具、隐私、评分、PDF 与错误存活。

默认执行真实模型调用和 score.py --local 对账；--offline 只检查零模型部分。
运行：python tools/verify_mcp.py（先安装 requirements.txt 与 requirements-mcp.txt）。
"""

from __future__ import annotations

import argparse
import ast
import asyncio
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tokenize
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# 入口、输入和输出均锚定仓库；不依赖运行脚本时的工作目录。
# 官方客户端与服务使用同一解释器，确保命中同一套可选 SDK 依赖。
ROOT = Path(__file__).resolve().parents[1]
RESUME = "tools/test_files/go_backend_resume.docx"
# 固定 JD 同时交给 MCP 和 CLI，避免输入差异造成分数无法比较。
# 这是虚构验收岗位，不读取外部 JD，也不将简历复制进脚本。
GO_JD = ("Go后端工程师。要求3年以上工作经验，本科及以上学历，熟悉Go、MySQL、Redis。"
         "职责：开发订单接口；优化数据库查询；维护缓存服务。")
# 参数顺序、必填项及类型都来自 PRD §8.3，不通过服务端代码推导期望值。
# 必填数组与参数集合分别断言，防止可选字段被错误地标为必填。
TOOLS = {
    "parse_resume": (["resume_path"], ["resume_path"]),
    "score_match": (["resume_path", "jd_text"], ["resume_path", "jd_text"]),
    "make_report": (["resume_path", "jd_text", "out_path"], ["resume_path", "jd_text"]),
}


class Checks:
    """独立记录每项检查；失败继续验收，退出码取决于全部已执行检查。"""

    def __init__(self) -> None:
        """初始化通过数与执行数，离线跳过项不会被算成成功。"""
        self.passed = 0
        # 分母仅记录实际执行的检查，离线模式不能冒充正式全量验收。
        self.total = 0

    async def run(self, label: str, check, *args):
        """执行同步或异步断言，隐藏可能夹带原文的异常对象。"""
        self.total += 1
        # 失败仍继续后面的协议检查，以便确认错误不会杀死服务。
        try:
            value = check(*args)
            # 同一入口支持源码检查与真正的 MCP 异步调用。
            if asyncio.iscoroutine(value):
                value = await value
        except Exception as exc:
            print(f"FAIL {label}（{type(exc).__name__}）", flush=True)
            return None
        self.passed += 1
        # 只有断言完全通过后才增加成功数，并立即刷新终端。
        print(f"PASS {label}", flush=True)
        return value


def source_contract() -> None:
    """验证可选依赖文件、独立注释比例及所有定义的 docstring。"""
    # 精确比对两行依赖内容，防止无上界安装意外升级到下一主版本。
    expected = "# MCP server 依赖（可选安装；mcp 2.x 必须钉版本，3.x 会改 API）\nmcp>=2.0,<3\n"
    assert (ROOT / "requirements-mcp.txt").read_text(encoding="utf-8") == expected
    # MCP 不能被带进主依赖；检查有效依赖行，避免将说明注释误判为安装项。
    dependencies = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    assert not any(line.strip().lower().startswith("mcp") for line in dependencies)
    for relative in ("mcp_server.py", "tools/verify_mcp.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        # 空行计入分母，只有独立 # 注释计入分子；docstring 不冒充注释。
        comments = {token.start[0] for token in tokenize.generate_tokens(StringIO(source).readline)
                    if token.type == tokenize.COMMENT and not token.line[:token.start[1]].strip()}
        definitions = [tree] + [node for node in ast.walk(tree)
                                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
        # 模块、类、公开函数及私有辅助函数全部进入文档率分母。
        assert len(comments) / len(source.splitlines()) >= .15
        assert all(ast.get_docstring(node) for node in definitions)
        print(f"  规范 {relative}：注释 {len(comments)}/{len(source.splitlines())}；"
              f"docstring {len(definitions)}/{len(definitions)}")


async def initialize(session: ClientSession) -> None:
    """执行真实初始化并核对服务标识，拒绝连到另一个同名测试替身。"""
    # SDK 2.x 的 Python 属性采用 snake_case，wire JSON 仍由 SDK 序列化。
    result = await session.initialize()
    assert result.server_info.name == "resume-agent"
    assert result.server_info.version == "0.1.0"
    print(f"  server={result.server_info.name} version={result.server_info.version}")


async def list_tools(session: ClientSession, display: bool = True) -> None:
    """核对三个工具的完整参数集合、必填项、字符串类型及可空默认值。"""
    # 不只检查必需的三个名称，也拒绝偷偷多注册第四个工具。
    result = await session.list_tools()
    assert len(result.tools) == 3
    assert {tool.name for tool in result.tools} == set(TOOLS)
    for tool in result.tools:
        parameters, required = TOOLS[tool.name]
        properties = tool.input_schema.get("properties", {})
        assert list(properties) == parameters
        assert set(tool.input_schema.get("required", [])) == set(required)
        # 所有必填输入均为字符串；文件内容不能被误注册为 bytes 参数。
        for name in required:
            assert properties[name]["type"] == "string"
        if "out_path" in properties:
            # out_path 必须省略可用，也必须能显式传 JSON null。
            output = properties["out_path"]
            assert {item["type"] for item in output["anyOf"]} == {"string", "null"}
            assert "default" in output and output["default"] is None
        if display:
            print(f"  {tool.name}：参数={parameters}；必填={required}")


async def call(session: ClientSession, name: str, arguments: dict) -> dict:
    """通过官方会话真调工具，读取结构化 JSON 并核对兼容文本内容。"""
    # 模型首次加载较慢，单次工具请求给予完整的推理超时时间。
    result = await session.call_tool(name, arguments, read_timeout_seconds=600)
    assert not result.is_error
    text = "\n".join(item.text for item in result.content if item.type == "text")
    # SDK 2.x 同时提供结构化结果和 JSON 文本；两条路径不能互相矛盾。
    payload = result.structured_content if result.structured_content is not None else json.loads(text)
    assert isinstance(payload, dict)
    if text:
        # 检查文本通道与结构化通道一致，两个通道都不能偷偷附加正文。
        assert json.loads(text) == payload
    return payload


async def parse_check(session: ClientSession) -> None:
    """真实解析指定 DOCX，严格验证六字段白名单与正文片段零泄露。"""
    print("  调用 parse_resume（真实核心）...", flush=True)
    payload = await call(session, "parse_resume", {"resume_path": RESUME})
    assert set(payload) == {"name", "years", "skills", "status", "reasons", "raw_chars"}
    # failed 抽取不能凭字段齐全被误算成正式解析成功。
    assert payload["status"] in {"ok", "needs_review"}
    assert type(payload["raw_chars"]) is int and payload["raw_chars"] > 0
    # 检查整个 JSON，包括 reasons、技能等嵌套值，不能只检查顶层键名。
    encoded = json.dumps(payload, ensure_ascii=False)
    assert all(fragment not in encoded for fragment in ("个人简历", "教育背景", "raw_text", "raw_body"))
    print(f"  raw_chars={payload['raw_chars']}；个人简历/教育背景命中=0")


async def score_check(session: ClientSession) -> dict:
    """真实执行现有评分链路，检查整数总分及 MatchReport 原始契约。"""
    print("  调用 score_match（真实核心）...", flush=True)
    payload = await call(session, "score_match", {"resume_path": RESUME, "jd_text": GO_JD})
    assert "error" not in payload
    # 先判断业务成功，再读取得分，错误字典不能被静默跳过。
    # bool 也是 int 的子类，因此用 type(...) is int 拒绝伪造整数。
    assert type(payload["score"]["total"]) is int and 0 <= payload["score"]["total"] <= 100
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.schemas_match import MatchReport

    # 只导入数据模型做契约检查，不在客户端实现或代算得分。
    MatchReport.model_validate(payload)
    # 数据模型默认可能忽略额外键，额外核对完整字段集合。
    assert set(payload) == set(MatchReport.model_fields)
    print(f"  total={payload['score']['total']}；verdict={payload['verdict']}")
    return payload


async def report_check(session: ClientSession, output: Path) -> dict:
    """真实生成指定 reports/ 下的 PDF，拒绝旧文件与伪造路径。"""
    assert not output.exists()
    # 不把历史文件的存在当作本次 make_report 成功的证据。
    print("  调用 make_report（真实核心）...", flush=True)
    payload = await call(session, "make_report", {"resume_path": RESUME, "jd_text": GO_JD,
                                                "out_path": str(output)})
    assert set(payload) == {"report_path", "total", "verdict"}
    # 返回的绝对路径必须恰好是本次指定的新文件，不能误验历史报告。
    path = Path(payload["report_path"])
    assert path.is_absolute() and path == output
    assert path.is_file() and path.stat().st_size > 8 * 1024
    # 除大小外还核对 PDF 文件头，拒绝 JSON 错误或文本伪装的报告。
    assert path.read_bytes().startswith(b"%PDF-")
    assert type(payload["total"]) is int and 0 <= payload["total"] <= 100
    print(f"  PDF={path.relative_to(ROOT).as_posix()}；字节数={path.stat().st_size}")
    return payload


def pdf_check(payload: dict | None) -> None:
    """用 PDF 解析器读回中文与分数，不靠文件后缀断言生成成功。"""
    import pdfplumber

    assert payload is not None
    # 只读回已经成功生成的文件，不为验收另行渲染一份固定 PDF。
    with pdfplumber.open(payload["report_path"]) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        # 忽略 PDF 提取产生的视觉空格，中文字符本身必须实际存在。
        compact = "".join(text.split())
        assert all(word in compact for word in ("匹配", "扣分理由", "建议补充", payload["verdict"]))
        assert str(payload["total"]) in compact
        print(f"  中文读回成功；页数={len(pdf.pages)}")


async def cli_check(payload: dict | None) -> None:
    """启动现成 score.py --local 真跑相同输入，总分仅允许 LLM 的 ±2。"""
    assert payload is not None
    # CLI 是独立子进程，既不复用 MCP 的分数，也不替换 run_match。
    print("  执行 score.py --local --json（同简历、同 JD，真实核心）...", flush=True)
    # 参数列表直接传给子进程，JD 不经过 shell 转义或临时正文文件。
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(ROOT / "skills/resume-job-matcher/scripts/score.py"),
        "--local", "--resume", str(ROOT / RESUME), "--jd-text", GO_JD, "--json",
        cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=600)
    except BaseException:
        # 即使超时或中断，也回收真实子进程，避免它继续调用付费模型。
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    assert process.returncode == 0
    # stdout 只应是正式 MatchReport JSON，stderr 才是核心日志。
    assert b"Traceback (most recent call last)" not in stderr
    local = json.loads(stdout.decode("utf-8"))
    difference = abs(payload["score"]["total"] - local["score"]["total"])
    # 这里只计算两次结果差值，业务总分仍分别来自各自的生产流水线。
    print(f"  MCP total={payload['score']['total']}；CLI total={local['score']['total']}；差值={difference}")
    assert difference <= 2


def assert_error(payload: dict) -> None:
    """错误必须是唯一 error 字段和非空人读提示，不得是协议异常或 traceback。"""
    assert set(payload) == {"error"}
    # error 不是 SDK 的 is_error 标志；它必须存在于业务返回字典中。
    assert isinstance(payload["error"], str) and payload["error"].strip()
    assert all(value not in payload["error"] for value in ("Traceback", "个人简历", "教育背景"))


async def path_errors(session: ClientSession, path: str) -> None:
    """三个工具均真调错误路径；输入验证阶段不得调用模型。"""
    for name in TOOLS:
        # parse_resume 没有 jd_text，额外塞入该键会掩盖工具契约问题。
        arguments = {"resume_path": path}
        if name != "parse_resume":
            arguments["jd_text"] = GO_JD
        # 不替换服务实现，也不把异常当作符合约定的结构化错误。
        payload = await call(session, name, arguments)
        assert_error(payload)
        print(f"  {name} → {json.dumps(payload, ensure_ascii=False)}")


async def jd_errors(session: ClientSession) -> None:
    """两个评分工具均拒绝短 JD 与只有空白的 JD，不产生付费调用。"""
    for name in ("score_match", "make_report"):
        # 固定输入确保这些测试在没有任何模型凭据时也能运行。
        for text in ("Go", " " * 30):
            payload = await call(session, name, {"resume_path": RESUME, "jd_text": text})
            assert_error(payload)
            assert "20" in payload["error"]
    print("  短 JD / 全空白 JD：4 个结构化错误")


async def output_errors(session: ClientSession) -> None:
    """错误输出扩展名、空路径和目录在开始评分前统一拒绝。"""
    for output in ("reports/verify_mcp.txt", "", "reports"):
        # 简历与 JD 有效，仅改变输出路径，精准触发输出校验。
        payload = await call(session, "make_report", {"resume_path": RESUME, "jd_text": GO_JD,
                                                    "out_path": output})
        assert_error(payload)
    print("  输出扩展名 / 空路径 / 目录：3 个结构化错误")


async def alive_check(session: ClientSession) -> None:
    """错误调用后在同一会话 ping 并再次列工具，证明服务进程未崩溃。"""
    # 不重启客户端或 server，否则无法证明原来那个进程仍然存活。
    await session.send_ping()
    await list_tools(session, display=False)


def log_check(path: Path) -> None:
    """检查本次服务 stderr 日志，不把底层 traceback 隐藏成验收通过。"""
    # 在会话退出后读取，确保日志缓冲与子进程生命周期均已结束。
    assert "Traceback (most recent call last)" not in path.read_text(encoding="utf-8", errors="replace")


async def verify(offline: bool) -> int:
    """启动真实 server 子进程，按顺序验收并保证最后一行为通过 X/N。"""
    checks = Checks()
    print("=== D4 MCP 验收（官方 ClientSession + stdio 子进程） ===", flush=True)
    print("模式：仅零模型检查" if offline else "模式：真实核心调用 + CLI 对账；错误用例零模型")
    await checks.run("依赖与源码规范", source_contract)
    # 唯一输出文件避免历史 PDF 混入验收；日志同样只收集本次进程输出。
    token = uuid4().hex[:8]
    output = ROOT / "reports" / f"verify_mcp_{token}.pdf"
    log = ROOT / "reports" / f"verify_mcp_{token}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    # 输出只落入已忽略的 reports/，不污染 app/ 或已跟踪测试输入。
    parameters = StdioServerParameters(
        command=sys.executable, args=[str(ROOT / "mcp_server.py")], cwd=str(ROOT),
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
    )
    # 子进程继承当前环境；配置仍由既有 app 模块从 .env 加载。
    try:
        # errlog 使用真实文件句柄，模型日志不会混入客户端终端的验收输出。
        with log.open("w", encoding="utf-8") as errlog:
            async with stdio_client(parameters, errlog=errlog) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=600) as session:
                    await checks.run("initialize", initialize, session)
                    await checks.run("list_tools 名称、参数、类型和默认值", list_tools, session)
                    if not offline:
                        # 三个正式工具全部经过同一个官方客户端，不直接调用 Python 函数。
                        # PDF 与 CLI 断言只消费前面真实调用得到的结果。
                        await checks.run("parse_resume 隐私白名单", parse_check, session)
                        score = await checks.run("score_match 整数总分与报告契约", score_check, session)
                        report = await checks.run("make_report 新 PDF >8KB", report_check, session, output)
                        await checks.run("PDF 中文读回", pdf_check, report)
                        await checks.run("与 score.py --local 总分对账 ±2", cli_check, score)
                    # 固定无效输入直接穿过协议验证；不为每条护栏启动真实模型。
                    await checks.run("不存在的路径：三个工具", path_errors, session,
                                     f"tools/test_files/missing_{token}.docx")
                    await checks.run("不支持的扩展名：三个工具", path_errors, session, "requirements.txt")
                    await checks.run("JD 长度护栏：两个工具", jd_errors, session)
                    await checks.run("输出路径护栏", output_errors, session)
                    # 所有错误验证结束后再检查存活，覆盖最后一次失败请求。
                    await checks.run("错误后同一进程仍响应 ping/list_tools", alive_check, session)
    except Exception as exc:
        # 启动或会话异常单独计失败；不得伪报尚未执行的工具成功。
        checks.total += 1
        print(f"FAIL stdio 会话（{type(exc).__name__}）；诊断见 {log.relative_to(ROOT).as_posix()}")
    await checks.run("服务日志无裸 traceback", log_check, log)
    # 非零退出码供 CI 使用；这是唯一的最终汇总，后面不再追加输出。
    print(f"通过 {checks.passed}/{checks.total}", flush=True)
    return 0 if checks.passed == checks.total else 1


if __name__ == "__main__":
    # Windows 的管道输出也使用 UTF-8，保证交付输出可原样保存。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__)
    # 默认不加开关就执行全部交付判据，离线模式必须显式选择。
    parser.add_argument("--offline", action="store_true", help="只验证零模型部分，不计跳过项")
    raise SystemExit(asyncio.run(verify(parser.parse_args().offline)))
