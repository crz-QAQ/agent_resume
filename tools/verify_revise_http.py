"""M7/M8c/M9c HTTP 验收：默认真实模型，--offline 仅替换模型响应，原始材料不落盘。"""

from __future__ import annotations

import argparse
import ast
import asyncio
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import tokenize
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit
from uuid import uuid4

import pdfplumber
from docx import Document
from docx.oxml import OxmlElement
from fastapi.testclient import TestClient

# 与生产入口使用相同包身份；脚本不依赖启动工作目录。
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from app import api, fallback, report_store, revise_pipeline as pipeline, score_delta
from app.file_parser import extract_text
from app.job_parser import JobParseResult
from app.match_pipeline import ModelUnavailableError
from app.prompts import SYSTEM_JOB, SYSTEM_MATCH_RUBRIC, SYSTEM_RESUME, SYSTEM_REVISION
from app.providers import UsageMeter
from app.resume_parser import ResumeParseResult
from app.schemas import ResumeInfo
from app.schemas_match import JobInfo
from app.scoring_llm import LlmScore
from app.learning_resources import ALLOWED_DOMAINS, load_materials


FILENAME = "go_backend_resume.docx"
TITLE = "Go 后端开发工程师"
PROFILE_TITLE = "FDE 交付工程师"
RESPONSIBILITIES = ["参与需求评审，设计并交付可维护的业务接口", "排查线上故障，完善日志与发布流程", "编写设计说明、测试与协作文档"]
JD = (TITLE + "。要求3年以上经验、本科学历，熟悉Go、MySQL、Redis。岗位职责："
      + "；".join(RESPONSIBILITIES) + "。")
# 为新增计划的真实正例明确加入简历未体现的 FastAPI，不改变原 M7 固定 JD。
PLAN_JD = JD.replace("熟悉Go、MySQL、Redis", "熟悉Go、MySQL、Redis，必须掌握FastAPI")
# 两份交付来自真实 HTTP 下载，不从生成函数再次构造另一份文件。
OUTPUT_REVIEW = ROOT / "output/pdf/m8c_m9c/M9c_修改意见.pdf"
OUTPUT_RESUME = ROOT / "output/docx/m8c_m9c/go_backend_resume_修改版.docx"


def _expect(condition: object, message: str) -> None:
    """显式断言，优化模式也必须报告真实失败。"""
    # 不使用 assert 语句，python -O 不得跳过任何验收。
    if not condition:
        raise AssertionError(message)


def _pdf_text(data: bytes) -> str:
    """实际打开下载字节提取文字，不以生产流程内部字符串替代读回。"""
    # 逐页读取最终文件，标题出现在内存 story 中不等于成功写入 PDF。
    with pdfplumber.open(BytesIO(data)) as document:
        return "\n".join(page.extract_text() or "" for page in document.pages)


def _fixture_job() -> JobInfo:
    """仅供离线模型和协议故障注入，字段逐项来自固定 JD。"""
    # 不添加原 JD 没有的公司、年限上限或优先技能。
    return JobInfo(title=TITLE, company=None, required_years_min=3, required_years_max=None,
        education="本科", must_have=["Go", "MySQL", "Redis"], nice_to_have=[], responsibilities=list(RESPONSIBILITIES))


async def _offline_provider(providers, prompt, *, system=None, **kwargs):
    """离线只替换供应商响应，S1/M0/M3/M5/M6 与 HTTP 均执行真实代码。"""
    # 按生产 system prompt 分支，不依据测试用例名称返回特制结果。
    if system == SYSTEM_RESUME:
        # 离线字段只用于协议验收，不冒充一次真实抽取的模型结果。
        payload = ResumeInfo(name="离线候选人", years=3, skills=["Go", "MySQL", "Redis"]).model_dump()
    # JD 分支仍会由 S1 解析及其三条护栏实际审核。
    elif system == SYSTEM_JOB:
        payload = _fixture_job().model_dump()
        # 受控模式仍只输出当前固定 JD 实际出现的具体技能，不造词或伪造真实验收。
        if "FastAPI" in prompt:
            payload["must_have"].append("FastAPI")
    elif system == SYSTEM_REVISION:
        # 只读取生产 M3 生成的材料区，测试不另造诊断或定位。
        data = json.JSONDecoder().raw_decode(prompt.split("下面是材料数据：\n", 1)[1])[0]
        sources = data["岗位"]["原文片段"]
        issues = []
        # 固定策略只调整原句动词；分类、严重度与定位仍必须通过 M3 七道护栏。
        for target in data["待写条目"]:
            before = target["original_text"]
            after = before.replace("负责", "完成", 1)
            if after == before:
                after = before
            # 缺失能力保持空建议，离线模式也不能把没有的经历改成已有。
            if target["category"] == "missing":
                after = ""
            issues.append({key: target[key] for key in ("category", "severity", "location", "original_text")}
                          | {"suggested_text": after, "jd_evidence": sources[-1],
                             "why": f"岗位要求「{sources[-1]}」；原句「{before}」需核实并说明已有工作范围"
                             + ("；岗位画像（推测）" if data["jd_source"] == "inferred" else ""),
                             "gain": "原有职责表达 → 清楚说明已有工作范围", "risk_note": "只能保留真实经历和真实数据"})
        # 候选未经挑选，重复、超过上限或不合法项仍交给生产护栏拒绝。
        payload = {"issues": issues}
    elif system == SYSTEM_MATCH_RUBRIC:
        # 受控非零分可检出遗漏 LLM 的回归；离线结果仍不代表真实评分。
        payload = {"items": [{"key": key, "score": score, "jd_evidence": RESPONSIBILITIES[0],
                    "resume_evidence": "负责订单系统接口开发", "reason": "离线协议测试不代表真实评分"}
                    for key, score in (("stack_depth", 10), ("domain_fit", 8), ("proof_strength", 7))], "suggestions": []}
    else:
        raise AssertionError("离线模式遇到未声明的模型任务")
    # 没有网络请求就没有已发生账单，不给受控响应伪造 token 消耗。
    return json.dumps(payload, ensure_ascii=False), UsageMeter()


class Acceptance:
    """保存本次验收的内存状态、下载字节和匿名化检查结果。"""

    def __init__(self, client: TestClient, cache: Path) -> None:
        """只读取已有 fixture，所有请求日志和返回结果留在内存。"""
        # 每轮验收使用全新上下文，不复用上一轮的成功结果。
        self.client, self.cache = client, cache
        self.raw = (ROOT / "tools/test_files" / FILENAME).read_bytes()
        self.text = extract_text(FILENAME, self.raw)
        # 测试日志只存在内存，不创建包含模型输入的文本日志。
        self.logs = StringIO()
        self.responses: list[dict] = []
        self.passed, self.total = 0, 0
        self.review, self.profile, self.inferred, self.applied = {}, {}, {}, {}
        # A2 可建立受控改写正例；一致性始终比较 A1/G2 的两次原始 HTTP 结果。
        self.baseline_review, self.match = {}, {}
        self.plan_review = {}
        self.review_bytes, self.resume_bytes = b"", b""

    def run(self, label: str, action) -> None:
        """逐条计数；失败继续，最终退出码严格反映所有已执行项目。"""
        # 已执行项目才进入分母，任何异常都不能算作通过。
        self.total += 1
        try:
            detail = action()
        except Exception as exc:
            # 不打印异常中的输入对象，避免失败日志变成简历的另一个泄漏出口。
            message = str(exc) if isinstance(exc, AssertionError) else type(exc).__name__
            print(f"{label}：失败；{message}", flush=True)
        else:
            self.passed += 1
            print(f"{label}：通过" + (f"；{detail}" if detail else ""), flush=True)

    def post_review(self, *, data=None, raw=None, filename=FILENAME):
        """提交真实 multipart 请求，捕获服务日志而不写入日志文件。"""
        # TestClient 经真实 multipart 解析器，不能直接调用业务函数代替 HTTP。
        with redirect_stdout(self.logs), redirect_stderr(self.logs):
            response = self.client.post("/revise/review", files={"file": (filename, self.raw if raw is None else raw)},
                                        data=data if data is not None else {"job_title": TITLE, "jd_text": JD})
        # 所有修改响应都保留用于统一隐私断言，包括错误和画像路径。
        self.responses.append(response.json())
        return response

    def post_apply(self, selected_ids=None):
        """提交已有报告编号，不把前端文字当作新的改写指令。"""
        # selected_ids=None 与 [] 均原样发出，防止验收器自行改变选择语义。
        with redirect_stdout(self.logs), redirect_stderr(self.logs):
            response = self.client.post("/revise/apply", json={"review_id": self.review["review_id"], "selected_ids": selected_ids})
        self.responses.append(response.json())
        return response

    def a1(self) -> str:
        """A1：真实 JD 链路生成意见、来源、分数与合法报告标识。"""
        # 默认模式在此执行真实 JD 抽取、简历抽取和 M3 意见生成。
        with patch.object(pipeline.scoring_llm, "score_llm", AsyncMock(wraps=pipeline.scoring_llm.score_llm)) as llm:
            response = self.post_review()
        # 正常路径也必须恰好一次，防止重复请求被总账的小额费用掩盖。
        _expect(llm.await_count == 1, "改前LLM未恰好调用一次")
        _expect(response.status_code == 200, f"预期 200，实际 HTTP {response.status_code}")
        self.review = response.json()
        self.baseline_review = self.review
        # ID 的完整匹配既排除路径，也排除合法 UUID 后附加扩展名。
        _expect(re.fullmatch(r"[0-9a-f]{32}", self.review["review_id"]), "review_id 不是32位小写hex")
        _expect(self.review["report"]["issues"] and self.review["profile"]["source"] == "user_jd", "意见或真实 JD 来源缺失")
        _expect(self.review["delta"] and self.review["report"]["score_before"] is not None, "M6 分数未回填")
        # 改前完整计分，改后沿用原分并保留 M6 的 ±2；降级另由 G5 验收。
        delta = self.review["delta"]
        _expect(self.review["report"]["meta"]["llm_estimated"] is False, "正常改前 LLM 评分发生降级")
        _expect(delta["before_parts"]["llm"] == delta["after_parts"]["llm"] and
                delta["estimated_fields"] == ["llm"] and delta["llm_tolerance"] == 2, "LLM 预估口径错误")
        _expect(self.review["report"]["score_before"] == sum(delta["before_parts"].values()), "改前总分未合计三项")
        meta = self.review["report"]["meta"]
        _expect(meta["llm_cost_yuan"] <= .003, "单次新增LLM成本超过0.003元")
        # 报告账单必须包括新增评分，不能只在验收器的总账中计费。
        _expect(abs(meta["cost_yuan"] - sum(meta[key] for key in
                ("revision_cost_yuan", "resume_cost_yuan", "job_cost_yuan", "llm_cost_yuan"))) < 1e-12, "报告成本漏计LLM")
        counts = {reason: sum(row["reason"] == reason for row in self.review["report"]["skipped"])
                  for reason in dict.fromkeys(row["reason"] for row in self.review["report"]["skipped"])}
        print("  可自动采用 " + str(len(self.review["report"]["meta"]["auto_applicable_ids"])) +
              " 条；未采用原因 " + json.dumps(counts, ensure_ascii=False), flush=True)
        return (f"意见 {len(self.review['report']['issues'])} 条；成本 {self.review['stats']['cost_yuan']:.8f} 元；"
                f"新增改前LLM {meta['llm_cost_yuan']:.8f} 元")

    def a2(self) -> str:
        """A2：实际应用后两个独立 URL 都能下载，格式跟随 Word 输入。"""
        if not self.review["report"]["meta"]["auto_applicable_ids"]:
            # 真实模型可能只给人工项；报告仍可下载，不能伪造自动修改。
            skipped = self.post_apply()
            _expect(skipped.status_code == 200 and not skipped.json()["applied"] and skipped.json()["resume_url"] is None,
                    "全部人工项未返回报告或被伪记为 applied")
            _expect(self.client.get(skipped.json()["review_pdf_url"]).status_code == 200, "全部人工项报告下载失败")
            original_generate = pipeline.revision_llm.generate_issues

            async def controlled_revision(prompt, *, system=None, **kwargs):
                """仅为自动修改正例固定 M3 模型响应，仍执行生产七道护栏。"""
                text, meter = await _offline_provider([], prompt, system=system)
                payload = json.loads(text)
                # 正例只提交一条已有表达弱候选，避免同原句 missing 优先去重挤掉改写。
                payload["issues"] = [row for row in payload["issues"] if row["category"] == "weak_evidence"
                    and row["suggested_text"] != row["original_text"]][:1]
                return json.dumps(payload, ensure_ascii=False), meter

            # 正例创建全新的 review ID；A1 的真实报告及未采用记录原样保留。
            with self._cached_models(), patch.object(pipeline.revision_llm, "generate_issues", original_generate), \
                 patch.object(pipeline.revision_llm, "call_with_fallback_async", controlled_revision):
                positive = self.post_review()
            _expect(positive.status_code == 200 and positive.json()["report"]["meta"]["auto_applicable_ids"],
                    "受控M3正例未通过实际护栏")
            self.review = positive.json()
            print("  真实报告全部人工项→200/报告可下载；自动修改正例使用独立受控M3响应，七道护栏及文件/HTTP均实跑", flush=True)
        # 申请应用后再走两个 GET 下载，不能只验证 URL 字符串存在。
        # 禁止改后再次评分；检查调用次数，避免哨兵异常被降级处理吞掉。
        with patch.object(pipeline.scoring_llm, "score_llm", AsyncMock(side_effect=AssertionError("改后禁止重跑LLM"))) as llm:
            response = self.post_apply()
        llm.assert_not_awaited()
        _expect(response.status_code == 200, f"预期 200，实际 HTTP {response.status_code}")
        self.applied = response.json()
        _expect(self.applied["delta"]["before_parts"]["llm"] == self.applied["delta"]["after_parts"]["llm"] ==
                self.review["delta"]["before_parts"]["llm"], "应用后丢失改前LLM原分")
        review = self.client.get(self.applied["review_pdf_url"])
        resume = self.client.get(self.applied["resume_url"])
        _expect(review.status_code == resume.status_code == 200, "双文件下载失败")
        _expect(self.applied["resume_ext"] == "docx" and resume.content.startswith(b"PK"), "Word 输入未生成 Word")
        # 文件头、扩展名、MIME 和中文下载头必须同时匹配。
        _expect("wordprocessingml" in resume.headers["content-type"] and "application/pdf" in review.headers["content-type"], "下载 MIME 错误")
        _expect("filename*=UTF-8''" in review.headers["content-disposition"], "中文下载头未编码")
        self.review_bytes, self.resume_bytes = review.content, resume.content
        # 固定交付文件只保存实际下载的最终产物，绝不保存上传原件或提取文本。
        OUTPUT_REVIEW.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_RESUME.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_REVIEW.write_bytes(self.review_bytes)
        OUTPUT_RESUME.write_bytes(self.resume_bytes)
        return f"意见 PDF {len(self.review_bytes)} 字节；修改版 DOCX {len(self.resume_bytes)} 字节"

    def a3(self) -> None:
        """A3：逐段检查结构与未采用原句，不能只检查扩展名。"""
        # 两份文档分别重开，段落结构不能用生成前的对象自证。
        old, new = Document(BytesIO(self.raw)), Document(BytesIO(self.resume_bytes))
        _expect(len(old.paragraphs) == len(new.paragraphs), "Word 段落数改变")
        # 提取器会合并空白；这里只用独立归一键识别已选段落，未选内容仍逐字比较。
        changed = {re.sub(r"[\s　·|/]+", "", row["before"]) for row in self.applied["applied"]}
        for left, right in zip(old.paragraphs, new.paragraphs):
            if re.sub(r"[\s　·|/]+", "", left.text) not in changed:
                _expect(left.text == right.text, "未采用段落被顺手改写")
        # 磁盘原件与原 raw 都不能被最终产物覆盖。
        _expect(self.raw == (ROOT / "tools/test_files" / FILENAME).read_bytes(), "原始 fixture 被覆盖")

    def a4(self) -> None:
        """A4：下载 PDF 必须包含真实意见、分数对比和虚高提示。"""
        # 关键提示必须读回自下载产物，不能只 grep 渲染器源码。
        text = _pdf_text(self.review_bytes)
        _expect(all(word in text for word in ("简历修改意见报告", "分数对比", "虚高")), "PDF 关键文字无法读回")

    def b1(self) -> None:
        """B1：没有 JD 和画像确认时，在调用模型前拒绝。"""
        # 只安装失败哨兵；若画像门槛正确，哨兵根本不会被调用。
        with patch.object(pipeline.resume_parser, "parse_resume_file", AsyncMock(side_effect=AssertionError("未确认却解析简历"))):
            response = self.post_review(data={"job_title": PROFILE_TITLE})
        _expect(response.status_code == 422 and "请先确认岗位画像" in response.json()["detail"], "缺少画像确认门槛")

    def b2(self) -> None:
        """B2：纯模板画像接口没有模型调用，返回草案与指纹。"""
        # 画像接口是纯模板；模型替身只负责发现任何意外调用。
        with patch.object(fallback, "_run_providers_async", AsyncMock(side_effect=AssertionError("画像接口调用模型"))):
            response = self.client.get("/revise/profile", params={"job_title": PROFILE_TITLE})
        _expect(response.status_code == 200, "画像接口失败")
        # 保存本次返回的同一份草案指纹，后续确认不能自造新 hash。
        self.profile = response.json()
        _expect(self.profile["profile_hash"] and not self.profile["profile"]["confirmed"], "草案或指纹缺失")

    def b3(self) -> str:
        """B3：确认后的推测画像保持来源标注，编辑内容也传到 M3。"""
        # 即使不编辑也显式提交两列，覆盖五个表单字段同时存在的上传路径。
        response = self.post_review(data={"job_title": PROFILE_TITLE, "profile_hash": self.profile["profile_hash"],
            "profile_edits": json.dumps({"must_have": self.profile["profile"]["must_have"],
                                         "responsibilities": self.profile["profile"]["responsibilities"]}, ensure_ascii=False)})
        _expect(response.status_code == 200, f"画像确认后预期200，实际 HTTP {response.status_code}")
        self.inferred = response.json()
        _expect(self.inferred["profile"]["source"] == "inferred" and self.inferred["profile"]["confirmed"], "推测来源或确认状态丢失")
        # 若模型写了推测声明，不允许真实 JD 的清理函数误作用到这个分支。
        _expect("推测" in self.inferred["profile"]["note"], "确认把画像变成真实 JD")
        # 画像分支也只增加一次改前复核，单次预算与真实 JD 相同。
        llm_cost = self.inferred["report"]["meta"]["llm_cost_yuan"]
        _expect(llm_cost <= .003, "画像单次新增LLM成本超过0.003元")
        return f"source=inferred；成本 {self.inferred['stats']['cost_yuan']:.8f} 元；新增改前LLM {llm_cost:.8f} 元"

    def b4(self) -> None:
        """B4：初始意见 PDF 抬头声明画像推测与非真实 JD。"""
        # 检查初始意见下载，避免只有应用后的报告才带来源声明。
        response = self.client.get(f"/revise/file/{self.inferred['review_id']}/review")
        _expect(response.status_code == 200, "画像意见 PDF 无法下载")
        text = _pdf_text(response.content)
        _expect("岗位画像（推测）" in text and "非真实 JD" in text, "PDF 来源横幅缺失")

    def c1(self) -> None:
        """C1：已确认画像下扫描件返回 422，不触发 JD 模型。"""
        # 使用真实无文本层 fixture，不用模型假装扫描件解析失败。
        response = self.post_review(filename="scan_resume.pdf", raw=(ROOT / "tools/test_files/scan_resume.pdf").read_bytes(),
            data={"job_title": PROFILE_TITLE, "profile_hash": self.profile["profile_hash"]})
        _expect(response.status_code == 422, "扫描件没有返回422")

    def c2(self) -> None:
        """C2：短 JD 不能悄悄走画像路径。"""
        # 岗位名合法也不能让一个已提供的短 JD 被忽略。
        _expect(self.post_review(data={"job_title": TITLE, "jd_text": "太短"}).status_code == 422, "短JD没有返回422")

    def c3(self) -> None:
        """C3：11MB 上传在切换临时磁盘前返回 413。"""
        # 全局 rollover 哨兵保证这份超大材料没有先进入临时磁盘。
        _expect(self.post_review(raw=b"x" * (11 * 1024 * 1024)).status_code == 413, "超限文件没有返回413")

    def c4(self) -> None:
        """C4：伪造报告 ID 不会读取任意磁盘文件。"""
        # 形状合法但不存在的 ID，同样只能得到统一的404。
        response = self.client.post("/revise/apply", json={"review_id": "0" * 32})
        _expect(response.status_code == 404, "伪造 review_id 没有返回404")

    def c5(self) -> None:
        """C5：普通和编码目录穿越均返回 404。"""
        # 同时覆盖客户端规范化和服务器 URL 解码两种路径。
        for path in ("/revise/file/../../etc/passwd/review", "/revise/file/%2e%2e%2fetc%2fpasswd/review"):
            _expect(self.client.get(path).status_code == 404, "目录穿越没有返回404")

    def c6(self) -> None:
        """C6：岗位名与 JD 同时缺失必须拒绝。"""
        # 上传文件存在，拒绝原因只能是四组合中的两个岗位字段都为空。
        _expect(self.post_review(data={}).status_code == 422, "缺少岗位输入没有返回422")

    def c7(self) -> None:
        """C7：未知 kind 和合法 ID 的错误 kind 都不能下载。"""
        # 用确实存在的 review ID，防止不存在文件掩盖 kind 校验缺失。
        identifier = self.review["review_id"]
        _expect(self.client.get(f"/revise/file/{identifier}/bad").status_code == 404, "非法kind没有返回404")
        _expect(self.client.get(f"/revise/file/{identifier}/resume").status_code == 404, "合法ID能够越类型下载")

    def _cached_models(self, review: dict | None = None) -> ExitStack:
        """复用已实测结构隔离协议分支，明确零新增模型费用，不伪装新的真实生成。"""
        # 使用 A1 已获得的可信结构，依赖模型内容的正常验收不会进入这里。
        original = api._review_sessions[(self.review if review is None else review)["review_id"]][1]
        stack = ExitStack()
        report = original.report.model_copy(deep=True)
        # 无新请求就无新增费用，不能把 A1 的历史费用重复加到账单。
        report.meta["cost_yuan"] = 0
        # 这些替身只用于四组合和故障码，A/B/G 的正常链路从不使用。
        stack.enter_context(patch.object(pipeline.job_parser, "parse_and_guard", AsyncMock(
            return_value=JobParseResult("ok", original.job.model_copy(deep=True), [], 0, JD))))
        stack.enter_context(patch.object(pipeline.resume_parser, "parse_resume_file", AsyncMock(return_value=
            ResumeParseResult("ok", original._resume.model_copy(deep=True), [], original.resume_text, 0, FILENAME))))
        stack.enter_context(patch.object(pipeline.revision_llm, "generate_issues", AsyncMock(return_value=report)))
        # 协议故障不应悄悄增加远端评分；复用 A1 的原分，明确本次零费用。
        cached_llm = LlmScore(original._before_llm, [], [], [],
            "failed" if original.report.meta.get("llm_estimated") else "ok", [], 0)
        stack.enter_context(patch.object(pipeline.scoring_llm, "score_llm", AsyncMock(return_value=cached_llm)))
        return stack

    def c8(self) -> None:
        """C8：只有 JD 没有岗位名仍可用，标题以 JD 为准。"""
        # 这一组只检查 HTTP 四组合，抽取结构已由 A1 实测。
        with self._cached_models():
            response = self.post_review(data={"jd_text": JD})
        _expect(response.status_code == 200 and response.json()["mode"] == "user_jd", "只有JD的四组合失败")
        _expect(response.json()["profile"]["title"] == self.review["profile"]["title"], "JD标题未沿用")

    def c9(self) -> None:
        """C9：模型异常和 M3 failed 结果均映射 503，不输出异常细节。"""
        # 使用带敏感标记的受控异常，确认响应不会反射异常字符串。
        with patch.object(pipeline.resume_parser, "parse_resume_file", AsyncMock(side_effect=RuntimeError("PRIVATE_ERROR"))):
            response = self.post_review(data={"job_title": PROFILE_TITLE, "profile_hash": self.profile["profile_hash"]})
        _expect(response.status_code == 503 and "PRIVATE_ERROR" not in response.text, "模型异常未安全映射503")
        # 第二种失败是 M3 返回 failed，而不是抛异常，两条路径都须覆盖。
        with self._cached_models():
            failed = api._review_sessions[self.review["review_id"]][1].report.model_copy(deep=True)
            failed.meta["status"] = "failed"
            with patch.object(pipeline.revision_llm, "generate_issues", AsyncMock(return_value=failed)):
                response = self.post_review()
        _expect(response.status_code == 503, "M3 failed未映射503")

    def c10(self) -> None:
        """C10：非模型的渲染故障返回中文 500，不暴露栈或敏感异常。"""
        # 故障发生在意见 PDF 渲染，不能误翻译为模型不可用503。
        original = api._review_sessions[self.review["review_id"]][1]
        with patch.object(pipeline, "run_review", AsyncMock(return_value=original)), \
             patch.object(pipeline, "review_pdf", AsyncMock(side_effect=RuntimeError("PRIVATE_ERROR"))):
            response = self.post_review()
        _expect(response.status_code == 500 and "PRIVATE_ERROR" not in response.text, "渲染错误状态或隐私不符")

    def c11(self) -> None:
        """C11：坏 JSON、画像编辑与 multipart 参数错误均为 422，绝不使用400。"""
        # 覆盖请求体解码、表单 JSON 字段、必需文件与 multipart 边界四类错误。
        responses = [self.client.post("/revise/apply", content="{", headers={"Content-Type": "application/json"}),
                     self.post_review(data={"job_title": TITLE, "profile_edits": "{"}),
                     self.client.post("/revise/review", data={"job_title": TITLE}),
                     self.client.post("/revise/review", content=b"bad", headers={"Content-Type": "multipart/form-data"})]
        _expect(all(response.status_code == 422 for response in responses), "协议错误出现400或非422")

    def d1(self) -> str:
        """D1：隔离的 reports 缓存只含最终 PDF/DOCX，没有任何原文或侧车。"""
        # 只检查本次生产 API 管理的隔离缓存，不删除仓库中的历史人工样张。
        files = [path for path in self.cache.iterdir() if path.is_file()]
        _expect(files and all(path.suffix in {".pdf", ".docx"} for path in files), "缓存出现非PDF/DOCX文件")
        # 所有落盘文件还需满足旧 UUID 或新 UUID__kind 命名，不能藏一个原件副本。
        _expect(all(re.fullmatch(r"[0-9a-f]{32}(?:__(?:review|resume))?\.(?:pdf|docx)", path.name) for path in files), "缓存路径使用了展示名")
        return f"最终文件 {len(files)} 份；非 PDF/DOCX 文件 0"

    def d2(self) -> None:
        """D2：修改响应不含全文、姓名、教育标题或完整解析对象。"""
        # 合并成功和失败响应检查，不能只挑一个正常响应证明隐私。
        serialized = json.dumps(self.responses, ensure_ascii=False)
        original = api._review_sessions[self.review["review_id"]][1]
        _expect(self.text not in serialized and "教育背景" not in serialized, "响应包含原文或教育标题")
        # 候选人姓名只从原抽取结构读取，不打印姓名或把它写入验收日志。
        if original._resume.name:
            _expect(original._resume.name not in serialized, "响应包含候选人姓名")
        _expect(not any('"' + key + '"' in serialized for key in ("resume_text", "raw_text", "resume_info")), "响应包含完整材料字段")
        # 额外覆盖内部 Outcome 的序列化排除，防止未来 response_model 调整泄漏全文。
        _expect("resume_text" not in original.model_dump(), "内部原文意外可序列化")

    def d3(self) -> None:
        """D3：正常及故障请求的服务日志均不含全文或裸 traceback。"""
        # 前面包括故障注入，故障时的日志也不能出现未经处理的栈。
        logs = self.logs.getvalue()
        _expect("Traceback (most recent call last)" not in logs and self.text not in logs, "服务日志泄漏原文或栈")

    def e1(self) -> None:
        """E1：只选一条时恰好应用一条，其余 Word 段落逐字不变。"""
        # 选择 M5 预演确认可用的真实意见，而非在验收器里伪造改写。
        identifier = self.review["report"]["meta"]["auto_applicable_ids"][0]
        response = self.post_apply([identifier])
        _expect(response.status_code == 200, "单条勾选应用失败")
        payload = response.json()
        _expect(len(payload["applied"]) == 1 and payload["applied"][0]["issue_id"] == identifier, "单选变成多选")
        changed = self.client.get(payload["resume_url"]).content
        # 只用归一化确定哪段被选中，其它段落始终作逐字比较。
        before = re.sub(r"[\s　·|/]+", "", payload["applied"][0]["before"])
        for left, right in zip(Document(BytesIO(self.raw)).paragraphs, Document(BytesIO(changed)).paragraphs):
            if re.sub(r"[\s　·|/]+", "", left.text) != before:
                _expect(left.text == right.text, "单选修改了未选段落")

    def e2(self) -> None:
        """E2：显式空选择返回 422，不回退默认全选。"""
        # 空列表必须原样到达服务端，不能被请求封装改为 None。
        _expect(self.post_apply([]).status_code == 422, "空选择没有返回422")

    def f(self) -> None:
        """F：统计只有数量和统一三十分钟 TTL。"""
        # 统计读取走生产 TTL 清理，不从验收器自己的结果清单伪造数量。
        response = self.client.get("/revise/stats")
        _expect(response.status_code == 200 and response.json()["cached_reviews"] >= 2 and
                response.json()["ttl_minutes"] == 30, "修改统计不符合契约")

    def g1(self) -> str:
        """G1：原健康、文档、评分统计和 UI 路由全部仍为200。"""
        # 每条路由分别发请求，不能只用 OpenAPI 中有路径就宣称可用。
        _expect(all(self.client.get(path).status_code == 200 for path in ("/", "/docs", "/match/stats", "/ui")), "旧GET路由回归")
        _expect(self.client.get("/").json()["revise"] == "/ui", "健康检查未声明修改入口")
        return "/、/docs、/match/stats、/ui 均200"

    def g2(self) -> str:
        """G2：实际评分接口仍完成一次原始链路及 PDF 下载。"""
        # 默认模式会真实运行原评分的抽取、语义与 LLM，不替换原入口。
        with redirect_stdout(self.logs), redirect_stderr(self.logs):
            response = self.client.post("/match/score", files={"file": (FILENAME, self.raw)}, data={"jd_text": JD})
        _expect(response.status_code == 200, f"评分回归预期200，实际 HTTP {response.status_code}")
        payload = response.json()
        self.match = payload
        # 旧 save_report/load_report 也在此经过实际下载回归。
        _expect(self.client.get("/match/report/" + payload["report_id"]).status_code == 200, "旧评分PDF下载失败")
        return f"POST /match/score 200；成本 {payload['report']['meta']['cost_yuan']:.8f} 元"

    def g4(self) -> str:
        """G4：同一文件/JD 的两次真实 HTTP 链路必须得到完全一致的改前总分。"""
        # 复用 A1 和 G2 的原始响应，不覆盖分数、不缓存供应商响应以制造一致。
        score = self.match["report"]["score"]
        review = self.baseline_review
        # 必须整数差零；不能放宽为允许波动的近似相等。
        before = review["report"]["score_before"]
        difference = score["total"] - before
        _expect(difference == 0, f"评分total={score['total']}；改前={before}；差值={difference}，必须差0")
        _expect(all(score["breakdown"][key] == review["delta"]["before_parts"][key]
                    for key in ("hard", "semantic", "llm")), "总分一致但三项分数不一致")
        return f"/match/score total={score['total']}；/revise/review score_before={before}；差值=0"

    def g5(self) -> str:
        """G5：复核抛异常或返回 failed 均明确降级为200，PDF 和应用链路保留说明。"""
        # 两种供应商失败形式均实跑 HTTP 编排，其他模型复用已验证结构以免重复收费。
        for failure in (RuntimeError("PRIVATE_LLM_ERROR"), LlmScore(0, [], [], [], "failed", ["PRIVATE_LLM_ERROR"], 0)):
            # 异常直接从评分入口抛出，不能用外层503替代所需的局部降级。
            mock = AsyncMock(side_effect=failure) if isinstance(failure, Exception) else AsyncMock(return_value=failure)
            with self._cached_models(), patch.object(pipeline.scoring_llm, "score_llm", mock):
                response = self.post_review()
                _expect(response.status_code == 200, f"LLM降级预期200，实际HTTP {response.status_code}")
                payload = response.json()
                delta, meta = payload["delta"], payload["report"]["meta"]
                # 对外仍然可用，但缺失评分必须进入待复核状态。
                _expect(meta["llm_estimated"] is True and meta["status"] == "needs_review", "降级伪装成正常")
                _expect(delta["before_parts"]["llm"] == delta["after_parts"]["llm"] == 0 and
                        payload["report"]["score_before"] == delta["before_parts"]["hard"] + delta["before_parts"]["semantic"],
                        "LLM失败后未以0计分")
                _expect(any("改前总分不含 LLM 项" in note for note in delta["notes"]), "降级缺少分数提示")
                _expect("PRIVATE_LLM_ERROR" not in response.text, "响应泄漏复核异常详情")
                # 读取真实下载，确认降级报告不会仍把零分标成正常 ±2。
                pdf = self.client.get(f"/revise/file/{payload['review_id']}/review")
                text = "".join(_pdf_text(pdf.content).split())
                _expect(pdf.status_code == 200 and "未取到（改前总分不含LLM项）" in text and "预估±2" not in text,
                        "PDF未披露降级或仍展示正常预估标签")
                if meta["auto_applicable_ids"]:
                    # 修改应用仍沿用降级原分，禁止为改后再发一次评分请求。
                    applied = self.client.post("/revise/apply", json={"review_id": payload["review_id"]})
                    _expect(applied.status_code == 200 and applied.json()["delta"]["after_parts"]["llm"] == 0,
                            "应用后丢失LLM降级基准")
                _expect(mock.await_count == 1, "改前复核未恰好一次，或改后重跑了LLM")
        return "异常/failed均HTTP200；llm_estimated=true；LLM=0；PDF明确提示；改后LLM不重跑"

    def g3(self) -> str:
        """G3：静态页面无硬编码外链或持久存储，旧评分脚本与表单保留，JS 语法有效。"""
        # 检查实际交付的单文件页面，不依赖模板或框架构建结果。
        html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        _expect("https://" not in html and "localStorage" not in html and "document.cookie" not in html, "页面含外网或持久存储")
        # 直接与提交前版本逐字比较，保障第一个页签的实现未被顺手改造。
        old = subprocess.check_output(["git", "show", "HEAD:app/static/index.html"], cwd=ROOT).decode("utf-8")
        scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
        _expect(scripts[0] == re.findall(r"<script>(.*?)</script>", old, re.S)[0], "评分脚本被改动")
        old_panel = old.split('<section id="score-panel"', 1)[1].split('<section id="revise-panel"', 1)[0]
        _expect(old_panel in html, "评分表单或结果区域被改动")
        # node 从 stdin 检查语法，不往报告缓存写 JS 或临时 HTML。
        syntax = subprocess.run(["node", "--check"], input="\n".join(scripts), text=True, encoding="utf-8", capture_output=True)
        _expect(syntax.returncode == 0, "页面 JavaScript 语法无效")
        # 必须实际有确认流程、三态人工文案、分数敏感提示与双下载入口。
        _expect(all(value in html for value in ("确认画像并分析", "不可代写", "此行需你补全", "虚高", "下载修改后简历", "/revise/apply")), "修改页面缺少必要交互")
        # 直接执行生产渲染函数，两种状态切换后都检查用户实际可见的文字。
        render = re.search(r"      function renderDelta\(.*?(?=      /\*\* 展示本次意见)", scripts[1], re.S).group()
        harness = r'''
class Element {
  constructor(text = "") { this.text = text; this.children = []; }
  append(...values) { this.children.push(...values); }
  replaceChildren() { this.children = []; }
  get textContent() { return this.text + this.children.map(value => value.textContent).join("\n"); }
}
const target = new Element();
const document = {createElement: () => new Element()};
const node = () => target;
const element = (tag, value) => new Element(String(value ?? ""));
const cell = (row, title, value) => row.append(element("td", value));
const delta = {before_total: 80, after_total_estimate: 80, llm_tolerance: 2,
  before_parts: {hard: 40, semantic: 15, llm: 25}, after_parts: {hard: 40, semantic: 15, llm: 25},
  delta_parts: {hard: 0, semantic: 0, llm: 0}, notes: []};
for (const missing of [false, true, false]) {
  renderDelta(delta, {llm_estimated: missing});
  const text = target.textContent;
  if (missing ? !text.includes("未取到（改前总分不含 LLM 项）") || !text.includes("请重新评分") || text.includes("预估 ±2")
              : !text.includes("（预估 ±2）") || text.includes("未取到")) throw new Error("LLM状态文案错误");
}
'''
        rendered = subprocess.run(["node"], input=render + harness, text=True, encoding="utf-8", capture_output=True)
        _expect(rendered.returncode == 0, "LLM正常/降级页面文案未正确渲染")
        _expect("renderDelta(payload.delta, payload.report.meta)" in html and
                "renderDelta(payload.delta, state.review.report.meta)" in html, "分析/应用页面未传递降级标记")
        _expect(all(value in html for value in ("怎么才能涨分", "能力补强建议", 'link.target = "_blank"', 'link.rel = "noopener"')),
                "新计划区块或外链保护缺失")
        # 执行实际下载区函数，交替有/无简历检查旧链接撤销与恢复，不能只查源码文案。
        downloads = re.search(r"      function renderApplyDownloads\(.*?(?=      /\*\* 只提交编号；)", scripts[1], re.S).group()
        harness = r'''
const nodes = new Map();
const node = id => { if (!nodes.has(id)) nodes.set(id, {hidden: true,
  removeAttribute(key) { delete this[key]; }, setAttribute(key, value) { this[key] = value; }}); return nodes.get(id); };
const readable = value => String(value ?? "");
const state = {review: {report: {issues: Array.from({length: 6}, (_, i) => ({id: "R-" + i, category: i < 5 ? "missing" : "inconsistent"}))}}};
const base = "/revise/file/" + "a".repeat(32);
const skipped = state.review.report.issues.map((issue, i) => ({issue_id: issue.id,
  reason: i < 5 ? "需本人补充经历，系统不代写" : "需本人按真实月份/学历补全"}));
// 四次切换覆盖首次显示、旧链接清理与重新启用。
for (const [index, present] of [true, false, true, false].entries()) {
  // 跨段原因优先，不能因意见分类为 inconsistent 就吞进补全信息数量。
  if (!present) skipped[5].reason = index === 1 ? "需本人按真实月份/学历补全" : "跨段落，需手动改（原句在文档里被拆成了多段）";
  renderApplyDownloads({review_pdf_url: base + "/review", resume_url: present ? base + "/resume" : null,
    resume_ext: present ? "docx" : null, skipped: present ? [] : skipped});
  const resume = node("revise-resume-download"), note = node("revise-no-resume");
  if (resume["aria-disabled"] !== String(!present) || resume.tabIndex !== (present ? 0 : -1) ||
      (present ? resume.href !== base + "/resume" : "href" in resume) || note.hidden !== present ||
      node("revise-final-review").href !== base + "/review" || node("revise-downloads").hidden) throw new Error("下载状态错误");
  if (!present && (!note.textContent.includes("5 条属于你缺的能力") ||
      !note.textContent.includes((index === 1 ? 1 : 0) + " 条需你补全真实信息") ||
      !note.textContent.includes((index === 3 ? 1 : 0) + " 条跨段落需手动改"))) throw new Error("三类数量错误");
}
// 三类均非零时也必须同时显示，且不能把跨段条目重复计算。
state.review.report.issues.push({id: "R-6", category: "inconsistent"});
renderApplyDownloads({review_pdf_url: base + "/review", resume_url: null, resume_ext: null,
  skipped: [...skipped, {issue_id: "R-6", reason: "需本人按真实月份/学历补全"}]});
const combined = node("revise-no-resume").textContent;
if (!["5 条属于你缺的能力", "1 条需你补全真实信息", "1 条跨段落需手动改"].every(text => combined.includes(text))) throw new Error("三类未同时显示");
'''
        # null 不得被当成畸形 URL；有 URL 时仍走同源校验。
        checked = subprocess.run(["node"], input=downloads + harness, text=True, encoding="utf-8", capture_output=True)
        _expect(checked.returncode == 0, "零改写下载区状态/统计错误")
        _expect('pointer-events: none' in html and '#revise-resume-download[aria-disabled="true"]' in html, "禁用下载未置灰/拦截点击")
        return "评分原脚本/表单未改；JS语法通过；计划/LLM文案通过；下载状态切换正确；缺能力/补信息/跨段手改5/1/0、5/0/1、5/1/1均通过"

    def i1(self) -> str:
        """I1：真实 HTTP 计划正例，两份计划存在且至少一条意见具有正分数影响。"""
        response = self.post_review(data={"job_title": TITLE, "jd_text": PLAN_JD})  # 正常路径完整执行模型和 HTTP。
        _expect(response.status_code == 200, f"新增计划预期200，实际HTTP {response.status_code}")
        self.plan_review = response.json()
        learning, uplift = self.plan_review.get("learning_plan"), self.plan_review.get("uplift_plan")  # 均需真实返回。
        _expect(isinstance(learning, dict) and isinstance(uplift, dict), "响应缺少学习计划或涨分策略")
        _expect(any(issue["score_impact"] > 0 for issue in self.plan_review["report"]["issues"]), "所有分数影响仍为 0")
        meta = self.plan_review["report"]["meta"]  # 不依赖测试器自己补充耗时或状态。
        _expect(meta["learning_ms"] >= 0 and meta["uplift_ms"] >= 0 and meta["learning_unavailable"] is False,
                "计划耗时或可用状态未显式记录")
        # 编排层显式打开兜底，HTTP meta 与内部计划必须同源，不能只在底层支持开关。
        _expect(meta["semantic_fallback"] != "disabled" and
                all(meta[key] == learning["meta"][key] for key in ("semantic_fallback", "semantic_attempts", "semantic_errors")),
                "HTTP未启用语义兜底或审计元数据未回填")
        # 全部已由确定性层命中时无需语义；不能强迫为增加计数而调用模型。
        if meta["semantic_attempts"] == 0:
            _expect(all(row["meta"]["matched_by"] in {"family", "keyword"} for row in learning["gaps"]),
                    "未匹配项没有进入已启用的语义层")
        # 此处只核对元数据来源，不重新计算任何学习缺口或匹配分数。
        # 每个返回的外链既要在白名单内，也要精确存在于本轮加载的材料库。
        materials, _ = load_materials()  # 用实际库精确核对 URL，不只看协议或主机后缀。
        expected = {row.locator for row in materials if row.kind == "external"}
        links = [material["locator"] for gap in learning["gaps"] for material in gap["materials"] if material["kind"] == "external"]
        _expect(links and all(url in expected and urlsplit(url).hostname in ALLOWED_DOMAINS for url in links), "外链不来自材料库白名单")
        _expect(meta["has_external_links"] is True, "外链标记丢失")  # 与实际已推荐材料一致。
        # 仅保存实际 HTTP 的脱敏响应，供浏览器重放截图；不保存上传字节或全文。
        out = ROOT / "output/m8c_m9c"
        out.mkdir(parents=True, exist_ok=True)
        (out / "review_payload.json").write_text(json.dumps(self.plan_review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (out / "ui_request.json").write_text(json.dumps({"job_title": TITLE, "jd_text": PLAN_JD, "filename": FILENAME},
                                                        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return f"learning/uplift 均非空；正影响意见存在；材料链接 {len(links)} 条均来自白名单；成本 {meta['cost_yuan']:.8f} 元"

    def i2(self) -> str:
        """I2：意见 FastAPI 的分数影响与对应策略及原扣分逐项一致。"""
        _expect(bool(self.plan_review), "计划场景尚未完成")
        issues = self.plan_review["report"]["issues"]  # 使用 I1 的同一份响应，不另造意见。
        issue = next((row for row in issues if row["category"] == "missing" and row["jd_evidence"] == "FastAPI"), None)
        item = next((row for row in self.plan_review["uplift_plan"]["items"]
                     if row["part"] == "hard" and row["item"] == "必备技能：FastAPI" and not row["blocked"]), None)
        _expect(issue is not None and item is not None, "没有 FastAPI 意见与对应硬性策略")
        outcome = api._review_sessions[self.plan_review["review_id"]][1]
        deduction = next(row for row in outcome._hard.deductions if row.type == "missing_skill" and row.jd_evidence == "FastAPI")
        _expect(issue["score_impact"] == item["potential"] == deduction.deduct > 0, "score_impact/potential/真实扣分不一致")
        return f"FastAPI：score_impact = potential = deduct = {deduction.deduct}；未重算或估造分数"

    def i3(self) -> str:
        """I3：真实缺失及损坏材料库均局部降级，HTTP 200 且明确标记不可用。"""
        _expect(bool(self.plan_review), "计划场景尚未完成")
        # 临时文件只包含坏 JSON，无任何简历内容；删除范围留在工作区 tmp 中。
        with TemporaryDirectory(prefix="m9c_materials_", dir=ROOT / "tmp") as directory:
            path = Path(directory).resolve()
            _expect(path.is_relative_to(ROOT.resolve()), "临时测试目录越界")
            broken = path / "broken.json"  # 故意不存在的邻接路径用于另一种失败模式。
            broken.write_text("{", encoding="utf-8")
            for candidate in (path / "missing.json", broken):
                with self._cached_models(self.plan_review), patch.object(pipeline, "load_materials", side_effect=lambda: load_materials(candidate)):
                    response = self.post_review(data={"job_title": TITLE, "jd_text": PLAN_JD})
                _expect(response.status_code == 200, f"材料库故障预期200，实际HTTP {response.status_code}")
                payload = response.json()  # HTTP 应继续成功，不以外层异常兜底成 500。
                meta = payload["report"]["meta"]
                _expect(payload["learning_plan"] is None and payload["uplift_plan"] is not None
                        and meta["learning_unavailable"] is True and meta["has_external_links"] is False,
                        "降级静默、沿用旧材料或连带丢失涨分策略")
                _expect(str(candidate) not in response.text, "降级泄漏材料库本机路径")  # 只公开固定的失败说明。
        # 故障夹具增加明确没有关键词的缺口，防止正常条目全被新关键词层解决。
        original_learning = pipeline.build_learning_plan

        def learning_with_unknown(**kwargs):
            """仅构造未匹配输入，实际分层匹配及异常降级仍执行生产函数。"""
            source = kwargs["diagnosis"]
            kwargs["diagnosis"] = source.model_copy(update={"missing_keywords": [*source.missing_keywords,
                "推动方案从演示走向实际使用并解决使用中的问题"]})
            return original_learning(**kwargs)

        # 本地向量失败属于局部降级，真实 HTTP 仍200且明确列出未匹配与错误类型。
        with self._cached_models(self.plan_review), patch("app.learning_resources._local_semantic_match",
                side_effect=RuntimeError("PRIVATE_REORDER_FAILURE")), patch.object(pipeline, "build_learning_plan", learning_with_unknown):
            response = self.post_review(data={"job_title": TITLE, "jd_text": PLAN_JD})
        _expect(response.status_code == 200, "本地重排异常导致HTTP失败")
        payload = response.json()
        _expect(payload["learning_plan"]["unmatched"] and payload["report"]["meta"]["semantic_errors"] and
                payload["report"]["meta"]["semantic_fallback"] == "unavailable" and "PRIVATE_REORDER_FAILURE" not in response.text,
                "重排失败未记录未匹配/错误或泄漏异常详情")
        # 含明确错误类型即可追踪降级，不把内部异常正文发给页面。
        return "缺失/坏JSON均HTTP200且材料库明确降级；本地重排异常HTTP200、unmatched/semantic_errors保留"

    def j1(self) -> str:
        """J1：六条真实缺项经过完整 apply，零改写也保存可下载的完整报告。"""
        source = api._review_sessions[self.plan_review["review_id"]]
        outcome = source[1].model_copy(deep=True)
        template = outcome.report.issues[0]
        # 明确构造六条缺项作为边界输入，实际改写、计分、PDF、存储和 HTTP 均不替换。
        outcome.report.issues = [template.model_copy(update={"id": f"R-{index:03}", "category": "missing",
            "suggested_text": ""}) for index in range(1, 7)]
        outcome.report.meta["auto_applicable_ids"] = []
        # 新会话复用已生成的计划，仅改造所需边界输入，不增加模型费用。
        identifier = uuid4().hex
        api._review_sessions[identifier] = (source[0], outcome, source[2], source[3])
        resumes_before = set(self.cache.glob("*__resume.*"))
        # 记录文件集合以证明零改写没有旁路存入简历产物。
        for selection in (None, [row.id for row in outcome.report.issues]):
            response = self.client.post("/revise/apply", json={"review_id": identifier, "selected_ids": selection})
            self.responses.append(response.json())
            _expect(response.status_code == 200, "全部 skipped 未返回200")
            payload = response.json()
            # skipped 必须逐条带编号和原因，六条 missing 不得计入 applied。
            _expect(payload["applied"] == [] and len(payload["skipped"]) == 6 and
                    all(row.get("issue_id") and row.get("reason") for row in payload["skipped"]), "缺项被伪改写或跳过原因丢失")
            _expect(payload["resume_url"] is None and payload["resume_ext"] is None and isinstance(payload["delta"], dict), "零改写下载字段或 delta 错误")
            pdf = self.client.get(payload["review_pdf_url"])
            _expect(pdf.status_code == 200, "零改写报告不可下载")
            text = "".join(_pdf_text(pdf.content).split())
            # 标题使用渲染器的实际章节名称，比较最终 PDF 提取文字。
            _expect(all(title in text for title in ("8能力补强建议", "9涨分策略")), "零改写报告遗漏第8/9章")
        _expect(resumes_before == set(self.cache.glob("*__resume.*")), "零改写仍保存了冒充修改版的原件")
        # 零意见也应产生报告，不因为 skipped 为空退回旧的422。
        outcome.report.issues = []
        empty = self.client.post("/revise/apply", json={"review_id": identifier})
        _expect(empty.status_code == 200 and empty.json()["applied"] == [] and empty.json()["skipped"] == [] and
                self.client.get(empty.json()["review_pdf_url"]).status_code == 200, "零意见无法下载报告")
        # 整份 Word 因修订痕迹拒绝时，也必须为实际候选逐条返回 issue_id/reason。
        source = api._review_sessions[self.review["review_id"]]
        document = Document(BytesIO(source[2]))
        document.paragraphs[0]._p.append(OxmlElement("w:ins"))
        buffer = BytesIO()
        document.save(buffer)
        api._review_sessions[identifier] = (source[0], source[1].model_copy(deep=True), buffer.getvalue(), source[3])
        tracked = self.client.post("/revise/apply", json={"review_id": identifier})
        _expect(tracked.status_code == 200 and not tracked.json()["applied"] and tracked.json()["skipped"] and
                all(row.get("issue_id") and row.get("reason") for row in tracked.json()["skipped"]), "整份拒绝缺少逐条原因")
        return "构造6条missing：默认/显式全选均200；applied=[]、skipped=6逐条带原因；报告GET200含第8/9章；resume_url/ext=null；零意见也200"

    def j2(self) -> str:
        """J2：原句含换行但 Word 分成多段，HTTP 应整体改写并保留其余 XML。"""
        document = Document()
        # 无关段落补足实际解析器的最短文本阈值，同样必须逐字不变。
        for text in ("保留开头，本用例核对多段原句及下载产物，并逐字保留此段所有内容。", "整理内部操作手册", "维护内部交接清单", "保留结尾"):
            document.add_paragraph(text)
        document.add_table(rows=1, cols=1).cell(0, 0).text = "保留表格"
        document.add_section()
        # 第二个 section 同样参与守恒检查，不能只验证单页默认文档。
        buffer = BytesIO()
        document.save(buffer)
        raw = buffer.getvalue()
        source = api._review_sessions[self.review["review_id"]]
        outcome = source[1].model_copy(deep=True)
        before = "整理内部操作手册\r\n维护内部交接清单"
        after = "完成内部操作手册整理\n完成内部交接清单维护"
        # 仅固定待应用意见；原文护栏、JD 闸门和跨段替换仍用生产实现。
        outcome.resume_text = extract_text("multiline.docx", raw)
        # 原句中的 CRLF 与解析器正文中的 LF 可由既有护栏规范化核对。
        outcome.report.issues = [outcome.report.issues[0].model_copy(update={"id": "R-MULTI", "category": "weak_evidence",
            "original_text": before, "suggested_text": after})]
        identifier = uuid4().hex
        api._review_sessions[identifier] = (source[0], outcome, raw, "multiline.docx")
        # 请求只提交服务端意见编号，不能把 before/after 从客户端传入生产端点。
        response = self.client.post("/revise/apply", json={"review_id": identifier, "selected_ids": ["R-MULTI"]})
        self.responses.append(response.json())
        _expect(response.status_code == 200 and len(response.json()["applied"]) == 1, "跨段 HTTP 未整体改写成功")
        payload = response.json()
        # 文件下载必须执行路由和存储读回，不能只检查返回 URL 的形状。
        download = self.client.get(payload["resume_url"])
        _expect(download.status_code == self.client.get(payload["review_pdf_url"]).status_code == 200, "跨段双下载失败")
        old, new = Document(BytesIO(raw)), Document(BytesIO(download.content))
        # 计数从最终 DOCX 全 XML 独立统计，包含表格中的段落。
        _expect(all(len(old.element.xpath(f".//w:{tag}")) == len(new.element.xpath(f".//w:{tag}"))
                    for tag in ("p", "tbl", "sectPr")), "跨段落结构数量变化")
        _expect([paragraph.text for paragraph in new.paragraphs[1:3]] == after.split("\n"), "跨段对应行替换失败")
        for index, (left, right) in enumerate(zip(old.paragraphs, new.paragraphs)):
            if index not in (1, 2):
                _expect(left.text == right.text and left._p.xml == right._p.xml, "其余段落未逐字保留")
        _expect(old.tables[0]._tbl.xml == new.tables[0]._tbl.xml, "未选表格变化")
        return "CRLF原句→连续2段分别替换；applied=1；双下载200；段落/表格/section守恒，其余段落逐字/XML相同"

    def j3(self) -> str:
        """J3：旧自动改写双下载不回归，空选择/全无效仍422，混合编号保留有效项。"""
        selected = self.review["report"]["meta"]["auto_applicable_ids"][0]
        # 混选合法与未知编号时仅应用现有意见，全未知编号另行严格拒绝。
        for selection in (None, [selected, "DOES-NOT-EXIST"]):
            response = self.post_apply(selection)
            _expect(response.status_code == 200 and len(response.json()["applied"]) >= 1, "可改旧场景回归")
            payload = response.json()
            _expect(payload["resume_ext"] == "docx" and self.client.get(payload["resume_url"]).status_code ==
                    self.client.get(payload["review_pdf_url"]).status_code == 200, "旧场景双下载失败")
        for selection, detail in (([], "没有选择可自动修改的条目"), (["DOES-NOT-EXIST"], "勾选条目无效，请重新选择")):
            # 精确中文消息属于接口契约，避免状态码正确却误导用户。
            invalid = self.post_apply(selection)
            _expect(invalid.status_code == 422 and invalid.json()["detail"] == detail, "错误勾选被放宽或错误文案变化")
        return "默认/有效+无效混选：applied≥1、双下载200；显式[]及全无效ID仍422且文案不变"

    def h1(self) -> None:
        """H1：新旧存储隔离、扩展名、ID、展示名与 TTL 均实测。"""
        # 每组使用新标识，不能通过覆盖已有文件掩盖独占写入要求。
        identifier = uuid4().hex
        path = report_store.save_artifact(identifier, "review", self.review_bytes, filename="../../仅展示.pdf")
        _expect(path.parent == self.cache and path.name == identifier + "__review.pdf", "展示名参与了路径")
        _expect(report_store.load_artifact(identifier) == (self.review_bytes, "../../仅展示.pdf"), "产物读回不一致")
        # 不同旧/新 API 的统计不相互计数，旧评分报告仍可独立保存和读取。
        old_id = uuid4().hex
        report_store.save_report(old_id, self.review_bytes, filename="旧报告.pdf")
        _expect(report_store.load_report(old_id) == self.review_bytes, "旧存储行为改变")
        # 大写和尾随换行同样不符合完整的小写 hex 契约。
        for bad in ("../outside", "A" * 32, "a" * 32 + "\n"):
            _expect(report_store.load_artifact(bad) is None, "非法ID进入了存储")
        # filename 即使作为展示名，也不能选择白名单以外的产物类型。
        try:
            report_store.save_artifact(uuid4().hex, "resume", b"bad", filename="bad.exe")
        except ValueError:
            pass
        else:
            raise AssertionError("非法扩展名被接收")
        # 使用 mtime 驱动生产清理，不修改 TTL 常量来制造通过。
        expired = time.time() - 31 * 60
        os.utime(path, (expired, expired))
        _expect(report_store.load_artifact(identifier) is None and not path.exists(), "产物TTL未生效")
        # 过期会话必须失去原始字节引用，单有过期 PDF 清理还不够。
        session = api._review_sessions[self.review["review_id"]]
        expired_id = uuid4().hex
        api._review_sessions[expired_id] = (0, *session[1:])
        _expect(self.client.post("/revise/apply", json={"review_id": expired_id}).status_code == 404, "会话TTL未生效")
        _expect(expired_id not in api._review_sessions, "过期原件仍被会话引用")

    def h2(self) -> str:
        """H2：注释率、全部 docstring 与旧存储函数源码逐项核对。"""
        # 注释比例使用 tokenizer，docstring 和字符串里的井号均不冒充注释。
        summaries = []
        for relative in ("app/report_store.py", "app/revise_pipeline.py", "app/api.py", "tools/verify_revise_http.py"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            tree = ast.parse(source)
            comments = {token.start[0] for token in tokenize.generate_tokens(StringIO(source).readline) if token.type == tokenize.COMMENT}
            ratio = len(comments) / len(source.splitlines())
            # 包含模块、类、私有函数和嵌套回调，不只统计 public 函数。
            nodes = [node for node in ast.walk(tree) if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
            _expect(ratio >= .15 and all(ast.get_docstring(node) for node in nodes), f"{relative} 注释率或docstring不达标")
            summaries.append(f"{Path(relative).name} {ratio:.1%}")
        old = subprocess.check_output(["git", "show", "HEAD:app/report_store.py"], cwd=ROOT).decode("utf-8")
        new = (ROOT / "app/report_store.py").read_text(encoding="utf-8")
        # 三个旧函数要求逐字未改，语义相同但重写源码也不能算满足任务书。
        for name in ("save_report", "load_report", "stats"):
            original = next(node for node in ast.parse(old).body if isinstance(node, ast.FunctionDef) and node.name == name)
            current = next(node for node in ast.parse(new).body if isinstance(node, ast.FunctionDef) and node.name == name)
            _expect(ast.get_source_segment(old, original) == ast.get_source_segment(new, current), "旧存储函数被改动")
        return "；".join(summaries) + "；docstring100%；旧存储三函数逐字未改"


class _LogTee:
    """同步写控制台和验收日志，进程中断时保留已完成用例与费用记录。"""

    def __init__(self, console, logfile):
        """仅保留当前输出句柄，不接触上传原件或模型内容。"""
        self.console, self.logfile = console, logfile

    def write(self, value: str) -> int:
        """每次输出立即落盘，避免终端重启丢失验收证据。"""
        # 日志仅含脚本原有公开摘要；隐私检查仍捕获内部响应与应用日志。
        self.console.write(value)
        self.logfile.write(value)
        self.flush()
        return len(value)

    def flush(self) -> None:
        """同时刷新两个目标，完整保留执行到当前步骤的输出。"""
        self.console.flush()
        self.logfile.flush()


def main(*, offline: bool = False, direct_models: bool = False) -> int:
    """先跑真实正常链路，再检查受控异常；隔离缓存且按真实 UsageMeter 合计全部费用。"""
    # 所有状态按本次运行初始化，账单不借用以前脚本的统计。
    started, costs, semantic_calls = time.perf_counter(), [], []
    original_provider = fallback._run_providers_async
    original_semantic = pipeline.score_semantic

    async def metered(*args, **kwargs):
        """计量每次成功响应，包含生成后被护栏拒绝的模型费用。"""
        # 默认调用原供应商函数，原始 prompt、响应和计量均不修改。
        result = await (_offline_provider(*args, **kwargs) if offline else original_provider(*args, **kwargs))
        costs.append(result[1].cost())
        return result

    def checked_semantic(*args, **kwargs):
        """断言语义模型没有在事件循环中运行，然后调用同一个真实评分器。"""
        # 工作线程没有运行中的 asyncio loop；在端点线程推理将立即失败。
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise AssertionError("语义推理阻塞HTTP事件循环")
        # 计数证明检查真正经过，不允许没有语义调用也宣称线程池正确。
        semantic_calls.append(1)
        return original_semantic(*args, **kwargs)

    label = "离线受控模型" if offline else "真实模型"
    print(f"M7/M8c/M9c TestClient 验收（{label}；隔离 reports 缓存，不改历史样张）", flush=True)
    # 暂存目录只接收生产存储 API 写出的最终产物，不复制上传原件。
    with TemporaryDirectory(prefix="m7_reports_") as directory, ExitStack() as stack:
        cache = Path(directory)
        if direct_models:
            # 只在显式指定时为本次验收直连已配置模型主机，退出后恢复代理环境。
            hosts = ",".join(filter(None, (urlsplit(provider["base_url"]).hostname for provider in fallback.PROVIDERS)))
            no_proxy = ",".join(filter(None, (os.environ.get("NO_PROXY", ""), "127.0.0.1,localhost", hosts)))
            stack.enter_context(patch.dict(os.environ, {"NO_PROXY": no_proxy, "no_proxy": no_proxy}))
        stack.enter_context(patch.object(report_store, "REPORT_DIR", cache))
        stack.enter_context(patch.dict(report_store._filenames, {}, clear=True))
        stack.enter_context(patch.dict(report_store._artifact_filenames, {}, clear=True))
        stack.enter_context(patch.dict(api._review_sessions, {}, clear=True))
        stack.enter_context(patch.object(fallback, "_run_providers_async", metered))
        # 初始语义证据与 M6 内部重算都检查，不能只覆盖第一次调用。
        stack.enter_context(patch.object(pipeline, "score_semantic", checked_semantic))
        stack.enter_context(patch.object(score_delta, "score_semantic", checked_semantic))
        # 所有上传都禁止 rollover；若解析器试图把简历写盘，当组立即失败。
        stack.enter_context(patch("tempfile.SpooledTemporaryFile.rollover", side_effect=AssertionError("上传原件禁止落盘")))
        client = stack.enter_context(TestClient(api.app, raise_server_exceptions=False))
        acceptance = Acceptance(client, cache)
        cases = (("A1 真实JD意见", acceptance.a1), ("A2 双文件下载", acceptance.a2),
                 ("A3 Word结构/未选段落", acceptance.a3), ("A4 PDF文字", acceptance.a4),
                 ("B1 未确认拒绝", acceptance.b1), ("B2 零模型画像", acceptance.b2),
                 ("B3 确认画像分析", acceptance.b3), ("B4 画像PDF横幅", acceptance.b4),
                 ("C1 扫描件422", acceptance.c1), ("C2 短JD422", acceptance.c2),
                 ("C3 11MB返回413", acceptance.c3), ("C4 伪造ID404", acceptance.c4),
                 ("C5 目录穿越404", acceptance.c5), ("C6 输入均空422", acceptance.c6),
                 ("C7 非法kind404", acceptance.c7), ("C8 只有JD", acceptance.c8),
                 ("C9 模型503", acceptance.c9), ("C10 内部故障500", acceptance.c10),
                 ("C11 协议错误无400", acceptance.c11), ("E1 单条勾选", acceptance.e1),
                 ("E2 空选择422", acceptance.e2), ("F 修改统计", acceptance.f),
                 ("G1 旧GET接口", acceptance.g1), ("G2 旧评分真实回归", acceptance.g2),
                 ("G4 同输入分数一致", acceptance.g4), ("G5 LLM失败降级", acceptance.g5),
                 ("I1 学习/涨分计划响应", acceptance.i1), ("I2 分数影响一致性", acceptance.i2),
                 ("I3 材料库失败降级", acceptance.i3),
                 ("J1 全skipped报告可下载", acceptance.j1), ("J2 跨段落原句", acceptance.j2),
                 ("J3 自动改写/勾选不回归", acceptance.j3),
                 ("G3 页面与JS", acceptance.g3), ("D1 仅最终文件落盘", acceptance.d1),
                 ("D2 响应隐私", acceptance.d2), ("D3 日志隐私", acceptance.d3),
                 ("H1 存储/会话TTL", acceptance.h1), ("H2 注释与兼容", acceptance.h2))
        # 每个标号独立计分，某一正常链路失败不会隐藏后续错误码检查。
        for name, action in cases:
            acceptance.run(name, action)
        # 计量检查本身也进入分母，避免预算超限时仍返回成功退出码。
        acceptance.run("H3 语义线程池", lambda: _expect(len(semantic_calls) >= 2, "未真实执行语义线程池检查"))
        acceptance.run("H4 总成本上限", lambda: _expect(sum(costs) <= .05, "总成本超过0.05元"))
    # 字节数来自下载内容，最终汇总同时披露预算和真实/受控模式。
    print(f"下载实际字节数：意见PDF={len(acceptance.review_bytes)}；修改版DOCX={len(acceptance.resume_bytes)}", flush=True)
    print(f"总成本 {sum(costs):.8f} 元；{'受控' if offline else '真实'}模型响应 {len(costs)} 次；耗时 {time.perf_counter()-started:.3f}s", flush=True)
    print(f"通过 {acceptance.passed}/{acceptance.total}", flush=True)
    return 0 if acceptance.passed == acceptance.total else 1


if __name__ == "__main__":
    # Windows/CI 使用 UTF-8，完整日志可以直接写入实施记录。
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="只替换供应商响应，不计为真实模型验收")
    # 代理失效时可显式直连；不改变 API 或全局供应商配置。
    parser.add_argument("--direct-models", action="store_true", help="仅本次验收绕过模型主机代理")
    # 持久日志独立于临时 reports，重启后也可核实已发请求的验收进度。
    parser.add_argument("--log", type=Path, help="同时保存完整 UTF-8 验收输出")
    arguments = parser.parse_args()
    if arguments.log:
        # 仅写指定验收摘要文件，不把上传原文或供应商响应写入文件。
        arguments.log.parent.mkdir(parents=True, exist_ok=True)
        with arguments.log.open("w", encoding="utf-8") as logfile, redirect_stdout(_LogTee(sys.stdout, logfile)):
            status = main(offline=arguments.offline, direct_models=arguments.direct_models)
    else:
        status = main(offline=arguments.offline, direct_models=arguments.direct_models)
    raise SystemExit(status)
