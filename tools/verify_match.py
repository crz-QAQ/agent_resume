"""S7b：TestClient验收七个旧接口、六组真实匹配、PDF、错误码和缓存TTL。"""

from __future__ import annotations

import argparse
import ast
import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
from io import BytesIO, StringIO
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
import time
import tokenize
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import unquote
from uuid import uuid4

import pdfplumber
from fastapi.testclient import TestClient

# 脚本从任意工作目录运行，全部业务模块按项目包名加载。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import api, fallback, match_pipeline, report_store
from app.agent import AgentResult, StepLog
from app.job_parser import JobParseResult
from app.match_pipeline import MatchOutcome
from app.providers import UsageMeter
from app.report_pdf import render_report
from app.resume_parser import ResumeParseResult
from app.schemas import ResumeInfo
from app.schemas_match import JobInfo, MatchReport, MatchScore
from app.scoring_llm import LlmScore
from app.scoring_rules import HardScore
from app.scoring_semantic import SemanticScore

# 现成S2三份简历包含普通Word、表格Word、双栏PDF，不生成或落盘新简历。
FILES = ("clean_resume.docx", "table_resume.docx", "text_resume.pdf")
JOBS = (
    ("Go岗位", "Go后端工程师。要求3年以上工作经验，本科及以上学历，熟悉Go、MySQL、Redis。"
     "职责：开发订单接口；优化数据库查询；维护缓存服务。"),
    ("Python岗位", "Python后端工程师。要求3年以上工作经验，本科及以上学历，熟悉Python、FastAPI、MySQL。"
     "职责：开发业务接口；优化数据库查询；维护接口文档。"),
)


class Checks:
    """逐项记录真实执行结果，任一失败都让进程返回非零。"""

    def __init__(self) -> None:
        """初始化独立计数和已成功生成的报告清单。"""
        self.passed, self.total = 0, 0
        # 清单仅保留报告计量与ID，避免测试输出包含简历全文。
        self.reports: list[dict] = []

    def run(self, label: str, check) -> None:
        """继续执行后续检查，不把失败检查伪计为通过。"""
        self.total += 1
        # 每个已执行检查才进入分母，离线模式不会把跳过的六组记作通过。
        try:
            check()
        except Exception as exc:
            # 不打印异常对象或请求体，避免失败诊断意外包含简历原文。
            print(f"FAIL {label}：{type(exc).__name__}", flush=True)
        else:
            self.passed += 1
            print(f"PASS {label}", flush=True)


def _controlled() -> MatchOutcome:
    """仅用于协议故障和TTL检查的内存报告，不参与真实六组分数。"""
    job = JobInfo(title="受控岗位", company=None, required_years_min=None, required_years_max=None,
                  education=None, must_have=["Go"], nice_to_have=[], responsibilities=["开发订单接口"])
    # 受控数值只用于异常和缓存测试，实际六组的分数全部由生产流水线返回。
    report = MatchReport(score=MatchScore(total=60, breakdown={"hard": 40, "semantic": 20, "llm": 0}),
                         verdict="勉强", keywords=[], highlights=[], deductions=[], missing=[], mismatches=[],
                         suggestions=[], meta={"model": "controlled", "prompt_version": "test", "cost_yuan": 0.0,
                         "elapsed_ms": 0, "generated_at": "2026-11-10T08:00:00+00:00",
                         "report_filename": "受控报告.pdf"})
    return MatchOutcome(report, job, "受控简历.pdf")


def _old_routes(client: TestClient) -> None:
    """旧七接口全部检查200；三个模型入口使用受控响应避免新增账单。"""
    result = AgentResult(status="ok", data=ResumeInfo(name="回归", years=3, skills=["Go"]),
                         steps=[StepLog(step=1, action="extract", tokens_in=0, tokens_out=0,
                                        cost_yuan=0.0, ok=True)], prompt_version="test")
    # 这里只验证旧HTTP契约；后面的六组匹配不使用这些替身。
    with patch.object(api, "parse_resume_agent", AsyncMock(return_value=result)), \
         patch.object(api, "call_with_fallback_async", AsyncMock(return_value=(result.data.model_dump_json(), UsageMeter()))):
        responses = [client.get("/"), client.post("/resume", json={"text": "Go"}),
                     client.post("/resume/llm", json={"text": "Go"}),
                     client.post("/resume/agent", json={"text": "Go"}),
                     client.post("/resume/upload", files={"file": ("table_resume.docx", (ROOT / "tools/test_files/table_resume.docx").read_bytes())}),
                     client.get("/resume"), client.get("/resume/1")]
    # 包含文件解析成功的旧上传路由，以及依赖旧入库结果的详情路由。
    assert all(response.status_code == 200 for response in responses)
    print("  GET /、POST /resume、POST /resume/llm、POST /resume/agent、POST /resume/upload、GET /resume、GET /resume/1：均200")


def _live_case(client: TestClient, checks: Checks, filename: str, label: str, jd: str) -> None:
    """真实执行一次三阶段模型调用并下载PDF，打印总分、档位、页数和成本。"""
    raw = (ROOT / "tools/test_files" / filename).read_bytes()
    # 不缓存模型响应、不挑选重试结果，每个组合只发送一次真实评分请求。
    response = client.post("/match/score", files={"file": (filename, raw)}, data={"jd_text": jd})
    assert response.status_code == 200, f"HTTP {response.status_code}"
    payload = response.json()
    identifier, report = payload["report_id"], payload["report"]
    # ID必须来自服务端UUID，不能采用文件名、路径或模型输出作为报告标识。
    assert re.fullmatch(r"[0-9a-f]{32}", identifier)
    score = report["score"]
    # 这是报告契约守恒检查，验收器不重新执行或覆盖任何评分算法。
    assert score["total"] == sum(score["breakdown"][key] for key in ("hard", "semantic", "llm"))
    assert "semantic_items" in report and report["semantic_items"] and report["status"] in {"ok", "needs_review"}
    download = client.get(f"/match/report/{identifier}")
    # 真正下载最终落盘PDF，而不是检查流水线内部的内存对象。
    assert download.status_code == 200 and download.headers["content-type"].startswith("application/pdf")
    assert len(download.content) > 8 * 1024
    # 体积下限能发现空响应或服务端错误页被误当PDF下载的情况。
    # RFC5987头部必须能恢复中文名，且符合PRD的文件主干、岗位和时间结构。
    header = download.headers["content-disposition"]
    assert "filename*=UTF-8''" in header
    name = unquote(header.split("filename*=UTF-8''", 1)[1])
    assert name.startswith(f"简历评分_{Path(filename).stem}_") and re.search(r"_\d{8}-\d{4}\.pdf$", name)
    with pdfplumber.open(BytesIO(download.content)) as document:
        # 读回在内存中进行，不另写文本提取文件或简历副本。
        text = "\n".join(page.extract_text() or "" for page in document.pages)
        pages = len(document.pages)
    # 除总分和免责外，还验证职责、扣分标题与审计prompt均能读回。
    assert all(value in text for value in (str(score["total"]), "本报告由 AI 生成", "扣分理由", "语义覆盖明细", "prompt"))
    assert any(item["item"] in text for item in report["semantic_items"])
    # 至少一条职责必须出现在PDF中，不能让只有标题的空语义表通过。
    record = dict(file=filename, job=label, total=score["total"], verdict=report["verdict"],
                  cost=report["meta"]["cost_yuan"], report_id=identifier, pages=pages, bytes=len(download.content))
    checks.reports.append(record)
    # 仅打印分数、状态、字节与费用，完整原文不进入验收日志。
    print(f"  {filename} × {label}：total={record['total']}；verdict={record['verdict']}；"
          f"status={report['status']}；PDF={pages}页/{len(download.content)}字节；成本={record['cost']:.8f}元")


def _post(client: TestClient, *, name: str = "resume.txt", raw: bytes = b"test", jd: str = JOBS[0][1]):
    """统一发送错误码验收请求，上传材料仅使用内存字节。"""
    return client.post("/match/score", files={"file": (name, raw)}, data={"jd_text": jd})
    # 这个便捷入口不替换任何服务端函数，错误码仍经过真实HTTP解析链。


def _status(response, expected: int) -> None:
    """只比较状态码，失败时不输出服务响应中的业务材料。"""
    assert response.status_code == expected, f"HTTP {response.status_code}, expected {expected}"


def _failure_guards(client: TestClient) -> None:
    """验证三处模型故障503、PDF失败500、坏格式422以及大上传不写临时磁盘。"""
    outcome = _controlled()
    # 受控结构只为走到指定失败阶段，不会写成成功报告或计入六份交付。
    resume = ResumeParseResult("ok", ResumeInfo(name="受控", years=3, skills=["Go"]), [], "受控简历内容。", 0.0, "resume.txt")
    job = JobParseResult("ok", outcome.job, [], 0.0, JOBS[0][1])
    # 受控故障分支绝不能意外调用真实模型，否则立即让本组失败。
    with patch.object(fallback, "_run_providers_async", AsyncMock(side_effect=AssertionError("故障验收禁止模型调用"))):
        with patch.object(match_pipeline.resume_parser, "parse_resume_file", AsyncMock(side_effect=RuntimeError("所有厂商均失败"))):
            # 第一阶段抛出的依赖错误必须停止并返回503。
            _status(_post(client), 503)
        with patch.object(match_pipeline.resume_parser, "parse_resume_file", AsyncMock(return_value=resume)):
            with patch.object(match_pipeline.job_parser, "parse_and_guard", AsyncMock(return_value=
                              JobParseResult("failed", None, ["JD模型调用失败:RuntimeError"], 0.0, JOBS[0][1]))):
                _status(_post(client), 503)
            # S1/S5实际会吞异常并返回failed，因此同时覆盖这种返回值路径。
            with patch.object(match_pipeline.job_parser, "parse_and_guard", AsyncMock(return_value=job)), \
                 patch.object(match_pipeline.scoring_semantic, "score_semantic", return_value=SemanticScore(0, [], [])), \
                 patch.object(match_pipeline.scoring_llm, "score_llm", AsyncMock(return_value=
                              LlmScore(0, [], [], [], "failed", ["LLM调用失败:RuntimeError"], 0.0))):
                _status(_post(client), 503)
        # PDF渲染RuntimeError是500，不能被“所有RuntimeError都503”的粗分类掩盖。
        with patch.object(match_pipeline, "run_match", AsyncMock(return_value=outcome)), \
             patch.object(api, "render_report", side_effect=RuntimeError("受控字体失败")), \
             patch("tempfile.SpooledTemporaryFile.rollover", side_effect=AssertionError("禁止上传落盘")):
            _status(_post(client, raw=b"x" * (2 * 1024 * 1024)), 500)
        _status(_post(client, name="bad.exe"), 422)
        # 缺少multipart boundary时也使用422，不能回到框架默认400。
        _status(client.post("/match/score", content=b"bad", headers={"content-type": "multipart/form-data"}), 422)
    print("  简历/JD/复核模型故障均503；PDF失败500；不支持文件/坏multipart均422；2MB上传未触发磁盘缓冲")


def _pipeline_guards() -> None:
    """记录六阶段调用顺序，并验证失败停止、抽取成本、解析复核状态传递。"""
    controlled = _controlled()
    # 解析阶段的模拟费用与LLM费用不同，便于发现只统计S5的遗漏。
    resume = ResumeParseResult("needs_review", ResumeInfo(name="受控", years=3, skills=["Go"]),
                               ["待人工确认"], "受控简历内容。", .001, "resume.txt")
    job = JobParseResult("needs_review", controlled.job, ["待人工确认"], .002, JOBS[0][1])
    values = [resume, job, HardScore(40, {"years": 15, "education": 10, "skills": 15}, []),
              SemanticScore(0, [], []), LlmScore(0, [], [], [], "ok", [], .003)]
    parent = Mock()
    # 父Mock汇集跨模块调用顺序，能识别跳步、并行提前调用和失败后继续。
    targets = [(match_pipeline.resume_parser, "parse_resume_file"), (match_pipeline.job_parser, "parse_and_guard"),
               (match_pipeline.scoring_rules, "score_hard"), (match_pipeline.scoring_semantic, "score_semantic"),
               (match_pipeline.scoring_llm, "score_llm")]
    # 替身只覆盖单独的离线护栏，六组真实匹配在上下文之外执行。
    with ExitStack() as stack:
        for index, ((module, name), value) in enumerate(zip(targets, values)):
            # 保留原函数的同步/异步形态，TestClient与工作线程使用方式不变。
            mock = AsyncMock(return_value=value) if index in {0, 1, 4} else Mock(return_value=value)
            parent.attach_mock(mock, name)
            stack.enter_context(patch.object(module, name, mock))
        build = stack.enter_context(patch.object(match_pipeline.match_report, "build_report", wraps=match_pipeline.match_report.build_report))
        parent.attach_mock(build, "build_report")
        result = asyncio.run(match_pipeline.run_match("resume.txt", b"memory", JOBS[0][1]))
        # 精确六步顺序也检查build_report只能在模型复核成功后发生。
        assert [entry[0] for entry in parent.mock_calls] == [name for _, name in targets] + ["build_report"]
        assert abs(result.report.meta["cost_yuan"] - .006) < 1e-10
        assert result.report.status == "needs_review" and len(result.report.reasons) == 2
        # 来自简历和岗位的两条护栏都应保留，不能在汇总后恢复为ok。
        # 第二步失败时后面四步调用次数均不能增长。
        parent.reset_mock()
        targets_job = getattr(parent, "parse_and_guard")
        targets_job.return_value = JobParseResult("failed", None, ["JD解析失败:JSON"], 0.0, "bad")
        try:
            asyncio.run(match_pipeline.run_match("resume.txt", b"memory", JOBS[0][1]))
        except match_pipeline.JobParseError:
            pass
        else:
            raise AssertionError("JD解析失败未停止")
        assert [entry[0] for entry in parent.mock_calls] == ["parse_resume_file", "parse_and_guard"]


def _store_guards() -> None:
    """隔离检查TTL、ID拒绝、惰性清理及进程重启后下载名恢复。"""
    outcome = _controlled()
    # 渲染的是受控报告，不是上传简历；这段额外PDF仅在内存里存在。
    pdf = render_report(outcome.report, outcome.job, resume_name=outcome.resume_name)
    # 测试缓存目录最终删除，真实缓存不被修改，不保存任何原始简历。
    with TemporaryDirectory(dir=ROOT / "docs/实施记录/简历打分", prefix="store-test-") as temporary, \
         patch.object(report_store, "REPORT_DIR", Path(temporary)), patch.dict(report_store._filenames, {}, clear=True):
        identifier = uuid4().hex
        # 独立随机ID避免与实际报告碰撞，目录patch结束后恢复真实缓存位置。
        with patch.object(report_store, "purge_expired", wraps=report_store.purge_expired) as purge:
            path = report_store.save_report(identifier, pdf)
            assert report_store.load_report(identifier) == pdf and purge.call_count >= 2
        # 保存和读取分别触发惰性清理，不能只在服务启动时清一次。
        assert report_store.stats() == {"cached_reports": 1, "ttl_minutes": 30}
        name = report_store.download_filename(identifier, pdf)
        # 测试没有预存下载名，等同于重启进程后从PDF恢复中文下载名。
        assert name.startswith("简历评分_受控简历_受控岗位_")
        # 非法ID在路径拼接和文件读取之前被拒绝。
        for invalid in ("../../etc/passwd", "A" * 32, "a" * 31, "0" * 33):
            assert report_store.load_report(invalid) is None
            try:
                report_store.save_report(invalid, pdf)
            except ValueError:
                pass
            else:
                raise AssertionError("非法ID被写入")
        old = time.time() - 31 * 60
        # 修改测试PDF的mtime模拟过期，不实际等待三十分钟。
        os.utime(path, (old, old))
        assert report_store.load_report(identifier) is None and not path.exists()
        assert report_store.stats() == {"cached_reports": 0, "ttl_minutes": 30}


def _documentation() -> None:
    """检查本次新协议/编排/存储代码的注释、docstring及工具包导入。"""
    for relative in ("app/api.py", "app/match_upload.py", "app/match_pipeline.py", "app/report_store.py", "tools/verify_match.py"):
        source = (ROOT / relative).read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        comments = {token.start[0] for token in tokenize.generate_tokens(StringIO(source).readline)
                    if token.type == tokenize.COMMENT and not token.line[:token.start[1]].strip()}
        nodes = [tree] + [node for node in ast.walk(tree) if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
        # 独立注释行计算比例；空行仍计入分母，docstring不冒充注释。
        assert len(comments) / len(source.splitlines()) >= .15, relative
        assert all(ast.get_docstring(node) for node in nodes), relative
        print(f"  规范：{relative}；注释={len(comments)}/{len(source.splitlines())}；docstring={len(nodes)}/{len(nodes)}")
    local = {path.stem for path in (ROOT / "app").glob("*.py")}
    # 用源码AST检查工具，避免导入历史验收脚本时触发模型或其他副作用。
    # 所有工具源码做AST检查，避免只修新验收脚本，遗留工具仍依赖扁平导入。
    for path in (ROOT / "tools").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                assert node.module.split(".")[0] not in local, str(path)
            elif isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] not in local for alias in node.names), str(path)


def main(*, offline: bool = False) -> int:
    """运行实际检查、累计成功模型usage，最后一行输出通过数。"""
    checks = Checks()
    costs: list[float] = []
    # 实际账单通过降级链返回的UsageMeter累计，包括成功请求之后的校验失败。
    original = fallback._run_providers_async

    async def metered(*args, **kwargs):
        """观察原降级链返回的真实usage，不替换请求、评分或模型响应。"""
        result = await original(*args, **kwargs)
        # 原请求与真实响应完全原样传递，只观察用量，不伪造零成本。
        costs.append(result[1].cost())
        return result

    print("=== S7b TestClient验收 ===" + ("（仅离线护栏，未执行六组真实匹配）" if offline else ""), flush=True)
    with TestClient(api.app, raise_server_exceptions=False) as client, \
         patch.object(fallback, "_run_providers_async", metered), \
         patch("tempfile.SpooledTemporaryFile.rollover", side_effect=AssertionError("匹配上传禁止临时落盘")):
        # 先运行零成本护栏，及时发现协议问题，真实模型按六个组合各执行一次。
        checks.run("老七接口均200", lambda: _old_routes(client))
        checks.run("模型503/渲染500/输入422及内存上传", lambda: _failure_guards(client))
        checks.run("六阶段顺序、失败停止、完整成本与复核状态", _pipeline_guards)
        checks.run("存储ID、惰性清理、TTL及下载名恢复", _store_guards)
        checks.run("注释≥15%、docstring100%及工具包式导入", _documentation)
        # TestClient内直接驱动ASGI，不起端口，也不依赖后台服务是否已经运行。
        if not offline:
            for filename in FILES:
                # 组合顺序固定，输出可直接与实施记录的六行结果表对照。
                for label, jd in JOBS:
                    print(f"开始真实匹配：{filename} × {label}", flush=True)
                    checks.run(f"真实匹配及PDF下载：{filename} × {label}",
                               lambda filename=filename, label=label, jd=jd: _live_case(client, checks, filename, label, jd))
        # 六个固定错误请求均不应产生模型调用或新报告。
        errors = [("扫描件422", lambda: _post(client, name="scan_resume.pdf", raw=(ROOT / "tools/test_files/scan_resume.pdf").read_bytes()), 422),
                  ("空JD422", lambda: _post(client, jd=""), 422), ("短JD422", lambda: _post(client, jd="太短"), 422),
                  ("11MB超限413", lambda: _post(client, raw=b"x" * (11 * 1024 * 1024)), 413),
                  ("伪造ID404", lambda: client.get("/match/report/" + "0" * 32), 404),
                  ("目录穿越404", lambda: client.get("/match/report/%2E%2E%2F%2E%2E%2Fetc%2Fpasswd"), 404)]
        for label, request, expected in errors:
            # 错误码六连打各自独立计数，避免一个通过掩盖另一个失败。
            checks.run(label, lambda request=request, expected=expected: _status(request(), expected))
        response = client.get("/match/stats")
        # 统计必须在六次成功保存及错误请求之后读取，检查当前真实缓存。
        checks.run("缓存统计200且ttl_minutes=30", lambda: _stats(response, checks))
    # 报告费用包含抽取与复核，观察降级链usage得到的账单也应与之相符。
    total_cost = sum(costs)
    # 离线替身都被排除在usage观察之外，因此不会冒充真实厂商账单。
    checks.run("实际成本≤0.02元且报告累计一致", lambda: _cost(total_cost, checks))
    # 每份成功报告已经单独打印结果，末尾保留账单和通过数便于归档。
    print(f"总成本 {total_cost:.8f} 元；成功模型响应 {len(costs)} 次", flush=True)
    # 最后一个输出行固定为通过X/N，方便人工和自动化脚本识别。
    print(f"通过 {checks.passed}/{checks.total}", flush=True)
    return 0 if checks.passed == checks.total else 1
    # 费用或规范检查失败同样会使退出码为1，不仅检查HTTP成功数量。


def _stats(response, checks: Checks) -> None:
    """验证TTL契约、当前成功报告数量及缓存目录只含PDF。"""
    assert response.status_code == 200
    data = response.json()
    # 已有其他有效报告时数量可更大，但不能少于本轮已交付的报告数。
    assert data["ttl_minutes"] == 30 and data["cached_reports"] >= len(checks.reports)
    assert all(path.is_file() and path.suffix == ".pdf" for path in report_store.REPORT_DIR.iterdir())
    # 目录断言包含子目录检查，旧预览文件必须移出报告缓存位置。
    print(f"  /match/stats：{data}")


def _cost(total: float, checks: Checks) -> None:
    """比较真实usage与成功报告累计；失败场景仍保留已经实际发生的usage。"""
    assert total <= .02
    # 新接口报告meta包含抽取、JD解析和复核三部分，必须与真实usage一致。
    assert abs(total - sum(record["cost"] for record in checks.reports)) < 1e-9
    # 零成本协议替身不会出现在成功报告清单或真实费用合计中。


if __name__ == "__main__":
    # Windows终端输出UTF-8，较慢的真实调用每一步立即刷新进度。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="只运行协议与TTL护栏，不调用模型")
    raise SystemExit(main(offline=parser.parse_args().offline))
