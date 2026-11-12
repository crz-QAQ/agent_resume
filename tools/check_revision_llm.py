"""验收 M3：A/B/C 原有检查及 D1～D4 的岗位来源声明隔离。

默认执行完整验收；--offline 只跑 B、C2/C3、D1～D3，不将跳过组记为通过。
"""

from __future__ import annotations

import argparse
import ast
import asyncio
from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
import sys
import tokenize
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# 在项目外执行也能定位源码和固定测试文件，不依赖终端当前目录。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.dont_write_bytecode = True

from app import revision_llm as revision
from app.file_parser import extract_text
from app.guard_revision import find_original, validate_issue
from app.job_profile import confirm_profile, infer_profile, profile_hash, ProfileNotConfirmed
from app.providers import UsageMeter
from app.resume_diagnose import diagnose
from app.schemas_match import JobInfo
from app.schemas_revision import DiagnosisReport
from app.scoring_rules import score_hard

# 不创建另一套 ResumeInfo 契约；结构化字段由固定 fixture 原文提供，不额外调抽取模型。
from app.schemas import ResumeInfo


def _expect(condition: object, message: str) -> None:
    """验证条件；失败显式抛 AssertionError，避免 python -O 静默跳过验收。"""
    if not condition:
        raise AssertionError(message)


def _job() -> JobInfo:
    """返回 A1/A2 共用的固定后端 JD，所有证据均为字段中的原文片段。"""
    return JobInfo(title="后端开发工程师", company=None, required_years_min=3,
                   required_years_max=None, education="本科", must_have=["Go", "MySQL", "Redis"],
                   nice_to_have=[], responsibilities=["负责订单系统接口开发与性能优化",
                   "负责数据库查询优化与缓存设计", "负责服务部署与问题排查"])


def _fixture() -> tuple:
    """构造零网络护栏基线；意见对应真实 M1 no_quantify 诊断，不伪造诊断出口。"""
    text = "姓名：张三\n3 年经验\n邮箱：dev@example.test\n教育：本科\n技能：Go、MySQL、Redis\n工作经历\n负责订单系统接口开发"
    resume, job = ResumeInfo(name="张三", years=3, skills=["Go", "MySQL", "Redis"]), _job()
    # 诊断仍由 M1 产生；因此能发现新增绑定规则误杀正常意见的回归。
    diagnosis = diagnose(text, resume, job)
    # 固定可用例隔离每道护栏，单项反例不会因另一处不合法而提前被挡。
    issue = dict(category="weak_evidence", severity="medium", location="工作经历 · 第 1 段",
                 original_text="负责订单系统接口开发", suggested_text="负责订单系统接口开发，说明接口交付范围",
                 why="JD 要求负责订单系统接口开发与性能优化；原句只列职责，缺少交付范围",
                 gain="只列职责 → 说明接口交付范围", risk_note="只保留真实交付内容，不虚构成果",
                 jd_evidence=job.responsibilities[0])
    return text, resume, job, None, diagnosis, issue


async def _fake(payload: object, *, text: str | None = None, job: JobInfo | None = None,
                profile=None, meter: UsageMeter | None = None, hard=None) -> tuple:
    """在真正调用入口注入假模型，返回报告和 mock；只替换模型，不跳过审核代码。"""
    base_text, resume, base_job, _, diagnosis, _ = _fixture()
    # 不把函数作为默认参数绑定，monkeypatch 必须击中运行时真正查找的入口。
    text = text if text is not None else base_text
    job = job if job is not None else base_job
    # 接受字符串原样注入非法 JSON，其他输入按标准 JSON 序列化。
    raw = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    fake = AsyncMock(return_value=(raw, meter or UsageMeter()))
    with patch.object(revision, "call_with_fallback_async", fake):
        # patch 作用域只覆盖本次调用，后续 A 组必定恢复真实模型。
        report = await revision.generate_issues(text, resume, job, profile, diagnosis, hard=hard)
    return report, fake


def _audit(report, text: str, job: JobInfo | None, hard=None) -> None:
    """独立检查意见原句、M0、岗位证据、排序、上限与计数守恒。"""
    _expect(report.meta["status"] == "ok" and report.issues, f"真实意见不可用：{report.meta}")
    sources = revision._job_sources(job, report.profile)
    for issue in report.issues:
        # 独立调用 M0；按明确契约只豁免独立 XX，不给新事实加白名单。
        checked = issue.model_copy(update={"suggested_text": revision._PLACEHOLDER.sub("", issue.suggested_text)})
        _expect(issue.original_text in text and find_original(text, issue.original_text), "原句悬空")
        _expect(validate_issue(checked, text, "\n".join(sources))[0], f"意见未过 M0：{issue}")
        _expect(any(find_original(source, issue.jd_evidence) for source in sources), "JD 依据查不到")
        _expect(issue.category in revision.ISSUE_CATEGORIES and issue.severity in revision.SEVERITIES, "模型自创枚举")
        # 审核 gain 不以「模型承诺改进」作为通过条件。
        _expect(not any(word in issue.gain for word in revision.VAGUE_GAIN), "空话 gain")
        if report.profile.source == "user_jd":
            # 所有真实 JD 验收都检查来源污染，D4 仍另外独立真实调用一组。
            _expect(not any(phrase in issue.why for phrase in ("岗位画像", "推测")), "真实 JD 理由混入画像/推测声明")
    keys = [(revision.SEVERITY_RANK[i.severity], revision.CATEGORY_PRIORITY[i.category]) for i in report.issues]
    # 业务输出不得受模型自报条数上限或总分影响。
    _expect(keys == sorted(keys), "严重度/category 排序错误")
    _expect(sum(i.severity == "high" for i in report.issues) <= 5 and len(report.issues) <= 12, "代码条数上限失效")
    _expect(all(sum(i.location == location for i in report.issues) <= 2 for location in {i.location for i in report.issues}), "同位置超过两条")
    # 注入并非模型候选；分别统计后仍须满足总量守恒。
    _expect(report.meta["candidate_count"] + report.meta["injected_count"] == len(report.issues) + len(report.rejected_issues), "丢弃或注入未审计")
    _expect(report.meta["injected_count"] == sum(getattr(i, "source", "") == "injected" for i in report.issues), "注入来源计数不实")
    if hard is not None:
        # 对照实际硬分独立检查覆盖关系，不能只断言注入 helper 被调用过。
        for deduction in hard.deductions:
            if deduction.type == "missing_skill":
                _expect(any(revision._mentions(value, deduction.jd_evidence) for issue in report.issues
                            for value in (issue.original_text, issue.suggested_text, issue.jd_evidence)),
                        f"扣分技能在意见中未提及：{deduction.jd_evidence}")
    # 分数与改写均留给后续分块，None 不等于候选人得零分。
    _expect(report.score_before is None and report.score_after_estimate is None, "模型污染分数")
    _expect(not report.adaptations and not report.skipped, "M3 越界改写")
    _expect(0 <= report.meta["cost_yuan"] <= 0.005, f"单次成本超预算：{report.meta['cost_yuan']:.8f}")


async def _check_b(index: int) -> str:
    """运行指定 B1～B7 反例，返回准确的拦截原因；B7 同时覆盖 JSON 重试与失败计费。"""
    text, resume, job, profile, diagnosis, baseline = _fixture()
    issue = deepcopy(baseline)
    # 深拷贝让每个反例只改变自己声称测试的那个字段。
    if index <= 5:
        fields = {1: ("suggested_text", "使用 Kubernetes 交付接口", "新名词"),
                  2: ("suggested_text", "负责订单系统接口开发，日均 500 万单", "数字"),
                  3: ("category", "style", "未知类别"),
                  4: ("gain", "更专业", "空话"),
                  5: ("original_text", "主导不存在的项目", "原句不在简历中")}
        key, value, expected = fields[index]
        # 下列断言必须同时验证拒绝原因、空 issues 和降级状态。
        issue[key] = value
        report, fake = await _fake({"issues": [issue]})
        _expect(not report.issues and report.meta["status"] == "needs_review", "非法意见进入 issues")
        _expect(len(report.rejected_issues) == 1 and expected in report.rejected_issues[0]["reason"], f"未被指定护栏拦下：{report}")
        _expect(fake.await_count == 1 and report.meta["cost_yuan"] == 0, "反例多调模型或产生费用")
        # 打印真实 reason，用户可核对各条在哪一道被挡。
        return report.rejected_issues[0]["reason"]
    if index == 6:
        # 同时污染顶层与条目，避免只忽略其中一层便误算通过。
        payload = {"issues": [issue], "total": 999, "verdict": "直接录用", "MAX_ISSUES": 999}
        payload["issues"][0].update(id="MODEL-ID", total=888, verdict="必录用")
        report, _ = await _fake(payload)
        _audit(report, text, job)
        _expect(report.issues[0].id == "R-001", "编号由模型决定")
        _expect("total" not in report.model_dump() and "verdict" not in report.model_dump(), "保留模型总分/档位")
        _expect("total" not in report.issues[0].model_dump() and report.score_before is None, "子项污染评分")
        return "忽略顶层/条目 total、verdict、上限、id；代码编号 R-001，分数 null"
    # B7：provider 全挂时不能把异常抛到接口层，也不能误报 needs_review。
    broken = AsyncMock(side_effect=RuntimeError("假模型全挂"))
    with patch.object(revision, "call_with_fallback_async", broken):
        # 异常必须在业务模块内部被转换，不在测试里替调用方吞异常。
        report = await revision.generate_issues(text, resume, job, profile, diagnosis)
    _expect(report.meta["status"] == "failed" and not report.issues, "异常未降级")
    _expect(report.meta["reason"] == "模型不可用，请稍后重试" and broken.await_count == 1, "异常文案或调用次数错误")
    meter = UsageMeter(prompt_tokens=100, completion_tokens=10)
    # 这里的 meter 是模拟计量，绝不混入脚本打印的真实总成本。
    # 首次非法 JSON 后成功，两个实际返回的 meter 必须累计，不能丢掉失败费用。
    retry = AsyncMock(side_effect=[("{broken", meter), (json.dumps({"issues": [issue]}), meter)])
    with patch.object(revision, "call_with_fallback_async", retry):
        repaired = await revision.generate_issues(text, resume, job, profile, diagnosis)
    _expect(repaired.meta["status"] == "ok" and repaired.meta["json_retries"] == 1, "非法 JSON 没有重试一次")
    _expect(retry.await_count == 2 and repaired.meta["cost_yuan"] == 2 * meter.cost(), "重试费用未累计")
    # 两轮都非法、错误根结构、以及重试阶段 provider 抛错，都不能崩溃。
    for responses in ([ ("[]", meter), ("{}", meter) ], [("非法", meter), RuntimeError("第二次全挂")]):
        retry = AsyncMock(side_effect=responses)
        with patch.object(revision, "call_with_fallback_async", retry):
            failed = await revision.generate_issues(text, resume, job, profile, diagnosis)
        expected_cost = meter.cost() * (2 if isinstance(responses[-1], tuple) else 1)
        _expect(failed.meta["status"] == "failed" and retry.await_count == 2, "失败重试超过一次或未降级")
        _expect(failed.meta["cost_yuan"] == expected_cost, "失败费用丢失")
    empty, _ = await _fake({"issues": []})
    # 空数组是合法 JSON，不属于格式错误，不应该触发额外模型请求。
    _expect(empty.meta["status"] == "needs_review", "零有效意见状态错误")
    return "全挂 failed；非法 JSON 仅重试 1 次；空结果 needs_review；重试/失败累计 cost"


async def _check_c2() -> str:
    """用受控模型及七道真实护栏检查证据、去重、稳定排序、截断和来源优先级。"""
    text, resume, job, profile, diagnosis, baseline = _fixture()
    # 前五道反例中保留其他字段合法，便于定位实际失败的护栏。
    for key, value, expected in (
            ("severity", "urgent", "未知严重度"), ("jd_evidence", "不存在的岗位要求", "JD 依据查不到"),
            ("gain", "接口交付更清晰", "空话"), ("gain", "→", "空话"),
            ("original_text", "", "原句不在简历中"), ("suggested_text", '<script>alert("x")</script>', "新名词"),
            ("suggested_text", "本科，毕业于虚构大学", "专名"),
            ("risk_note", "保证成功", "真实"), ("location", "工作经历 · 第 999 段", "待写条目")):
        # 每次只改一个字段，确认完整 generate_issues 出口执行相应护栏。
        issue = dict(baseline, **{key: value})
        report, _ = await _fake({"issues": [issue]})
        _expect(not report.issues and expected in report.rejected_issues[0]["reason"], f"{key} 护栏失效：{report}")
    # HTML 中只用来源已有的技术名，避免新名词先挡住而没有测到代码/HTML 禁令。
    report, _ = await _fake({"issues": [dict(baseline, suggested_text="<Go>接口</Go>")]})
    _expect("代码/HTML" in report.rejected_issues[0]["reason"], "HTML 禁令未执行")
    # 任务书允许 XX；不许因此放行 XXScript 或凭空使用量级与数字。
    report, _ = await _fake({"issues": [dict(baseline, suggested_text="负责订单系统接口开发，交付结果：XX")]})
    _expect(len(report.issues) == 1, "任务书许可的 XX 占位符被误杀")
    # 构造足够多的已诊断条目，输入为无新数字/名词的纯职责句。
    rows = []
    # 二十三条足以同时触发 high 上限与总量上限，不靠模型偶然多输出来验证。
    for index in range(23):
        row = dict(baseline, original_text=f"负责{'接口' * (index + 1)}开发", location=f"经历 {index}",
                   category=revision.ISSUE_CATEGORIES[index % 5], severity=("high" if index < 8 else "medium"),
                   suggested_text="" if index % 5 == 0 else "负责接口交付",
                   risk_note="只能使用真实数据；如确有相关经历请补充，否则不建议写")
        rows.append(row)
    # 同原句低/高严重度相隔出现，必须保留最高；同位置第三条必须独立记录拒绝原因。
    rows.insert(0, dict(rows[1], severity="low"))
    rows += [dict(baseline, original_text="负责缓存开发", location="共同位置"),
             dict(baseline, original_text="负责日志开发", location="共同位置"),
             dict(baseline, original_text="负责配置开发", location="共同位置")]
    all_text = "\n".join(row["original_text"] for row in rows)
    # 每条原句都在这份受控原文中，排序用例不会被第二道提前拦下。
    # 这些人工诊断目标只用于隔离第六/七道，不替换护栏或对外返回结果。
    kept, rejected = revision._guard_candidates(rows, all_text, job.responsibilities, rows)
    reasons = [row["reason"] for row in rejected]
    # 检查最终守恒，也检查每一类截断都有可读审计原因。
    _expect(len(kept) == 12 and sum(i.severity == "high" for i in kept) == 5, "5/12 上限失效")
    _expect(all(any(part in reason for reason in reasons) for part in ("同原句重复", "同 location", "high 超过", "总条数超过")), "丢弃记录不完整")
    _expect(len(kept) + len(rejected) == len(rows), "截断或去重未计入 rejected")
    keys = [(revision.SEVERITY_RANK[i.severity], revision.CATEGORY_PRIORITY[i.category]) for i in kept]
    _expect(keys == sorted(keys), "严重度/category 排序不稳定")
    _expect(all(i.severity != "low" for i in kept), "保留了低级重复项")
    # 对比具体原句序列，而非只比较严重度集合。
    # 平级、同类别的多个不同原句必须保留输入顺序。
    ties = [dict(baseline, original_text=row["original_text"], location=f"平级 {i}") for i, row in enumerate(rows[9:13])]
    stable, _ = revision._guard_candidates(ties, all_text, job.responsibilities, ties)
    _expect([i.original_text for i in stable] == [i["original_text"] for i in ties], "平级原顺序丢失")
    # 画像和真实 JD 同时存在时，陌生画像技术不能成为建议的合法来源。
    draft = infer_profile("FDE 交付工程师")
    with patch.object(revision, "call_with_fallback_async", AsyncMock()) as fake:
        try:
            await revision.generate_issues(text, resume, None, draft, diagnosis)
        except ProfileNotConfirmed:
            pass
        else:
            raise AssertionError("未确认画像进入模型调用")
        _expect(fake.await_count == 0, "确认前花费模型成本")
    draft.must_have.append("Kubernetes")
    # 画像故意未确认；存在真实 JD 时应完全不受这份草案干扰。
    report, _ = await _fake({"issues": [dict(baseline, suggested_text="使用 Kubernetes 交付接口")]}, profile=draft)
    _expect(report.profile.source == "user_jd" and "新名词" in report.rejected_issues[0]["reason"], "JD 未优先于画像")
    # 提示词截断与评分证据注入检查使用同一个实际调用入口。
    long_text = text + "\n" + "长" * 5000 + "不应进入提示词的尾标"
    report, fake = await _fake({"issues": [baseline]}, text=long_text)
    _expect(report.meta["resume_chars"] == 4000 and report.meta["resume_truncated"], "4000 字截断失效")
    _expect("不应进入提示词的尾标" not in fake.call_args.args[0], "提示词泄漏截断尾文")
    # 实际 hard 与指定语义结果混合，验证不只给模型写空标题。
    missing_job = job.model_copy(update={"must_have": ["Go", "MySQL", "Redis", "Python"]})
    hard = score_hard(resume, missing_job, text)
    sem = SimpleNamespace(per_item=[SimpleNamespace(item=job.responsibilities[i], covered=bool(i), best_sim=0.2 + i / 2, rerank_score=-0.5 + i) for i in range(2)])
    insight = revision.build_insight_block(missing_job, None, hard, sem)
    # covered=False 与 missing_skill 两条通道都必须包含实际证据。
    _expect(all(value in insight for value in ("已知事实", "未覆盖", "已覆盖", "cos=", "rerank=", "missing_skill", "Python")), "遗漏 PRD §8.0 评分证据")
    fake = AsyncMock(return_value=('{"issues": []}', UsageMeter()))
    with patch.object(revision, "call_with_fallback_async", fake):
        # 再看真正收到的 prompt，防止 helper 写对了却没有被主函数调用。
        await revision.generate_issues(text, resume, missing_job, None, diagnosis, hard=hard, sem=sem)
    _expect(insight in fake.call_args.args[0], "已知事实没有进入真实 prompt")
    expensive = UsageMeter(prompt_tokens=6000)
    # 超预算只记指标，不能清空有效建议或篡改已发生费用。
    report, _ = await _fake({"issues": [baseline]}, meter=expensive)
    _expect(report.meta["status"] == "ok" and report.meta["cost_over_budget"], "超预算没有只记 metric")
    # 非对象/缺字段/错误类型同样要计入 rejected，禁止候选无声消失。
    report, _ = await _fake({"issues": [None, {}, dict(baseline, why=None)]})
    _expect(report.meta["candidate_count"] == report.meta["rejected_count"] == 3, "畸形条目无审计")
    await _check_defect_regressions()
    return "七道护栏全覆盖；去重/同位置2条/稳定排序/high≤5/总≤12；JD优先、画像确认、4000字、评分事实、成本metric；D-M3-01/02/03 回归通过"


async def _check_defect_regressions() -> None:
    """验证三项缺陷修复及负向边界；不联网，不修改真实模型报告来凑验收。"""
    text, resume, job, _, diagnosis, baseline = _fixture()
    minimal = {key: value for key, value in baseline.items() if key not in ("location", "risk_note")}
    report, _ = await _fake({"issues": [minimal]})
    # D-M3-02：六项核心内容加可回查证据足以保留意见，可选项原样填默认值。
    _expect(len(report.issues) == 1 and not report.rejected_issues, "缺可选字段被误拒")
    _expect(report.issues[0].location == "—" and report.issues[0].risk_note == "涉及数字、规模、成果的建议，必须是你真实的数据", "可选字段默认值错误")
    # 有核实提醒但未逐字写「真实」时，同样由代码补足，不能靠模型固定措辞才通过。
    supplemented, _ = await _fake({"issues": [dict(baseline, risk_note="请核实原有数据后填写")]})
    _expect(len(supplemented.issues) == 1 and "必须是你真实的数据" in supplemented.issues[0].risk_note, "诚实兜底未补足可选提醒")
    # 三个不同诊断位置都缺 location 时，不能因为三个「—」被误限成两条。
    targets = [dict(baseline, original_text=f"负责{word}开发", location=f"段落 {index}")
               for index, word in enumerate(("缓存", "日志", "配置"))]
    rows = [{key: value for key, value in target.items() if key not in ("location", "risk_note")} for target in targets]
    kept, rejected = revision._guard_candidates(rows, "\n".join(t["original_text"] for t in targets), job.responsibilities, targets)
    _expect(len(kept) == 3 and not rejected, "缺省 location 造成同位置误拒")
    for field in ("category", "severity", "original_text", "suggested_text", "why", "gain", "jd_evidence"):
        incomplete = {key: value for key, value in minimal.items() if key != field}
        rejected, _ = await _fake({"issues": [incomplete]})
        _expect(not rejected.issues and len(rejected.rejected_issues) == 1, f"缺必填字段 {field} 被放行")
    # 显式错误类型不属于「可缺省」，不能被默认值掩盖。
    for field in ("location", "risk_note"):
        rejected, _ = await _fake({"issues": [dict(baseline, **{field: None})]})
        _expect(not rejected.issues, f"可选字段 {field} 的错误类型被掩盖")
    # D-M3-01：整体数量占位符必须通过完整 M3 出口，而不只通过数字 helper。
    for placeholder in ("XX 万级", "XX万", "XX%"):
        kept, _ = await _fake({"issues": [dict(minimal, suggested_text=f"负责订单系统接口开发，支撑 {placeholder} 请求")]})
        _expect(len(kept.issues) == 1, f"M3 仍误杀占位符 {placeholder}")
    # 构造确实缺 Python 的硬分，而不是只在 prompt 中声称有扣分。
    missing_job = job.model_copy(update={"must_have": [*job.must_have, "Python"]})
    hard = score_hard(resume, missing_job, text)
    report, _ = await _fake({"issues": [baseline]}, job=missing_job, hard=hard)
    _audit(report, text, missing_job, hard)
    _expect(len(report.issues) == 2 and report.meta["injected_count"] == 1 and not report.rejected_issues, "漏写技能没有代码兜底，或增加了拒绝项")
    injected = next(issue for issue in report.issues if getattr(issue, "source", "") == "injected")
    _expect(injected.suggested_text == "在技能栏补充 Python，并在相关经历中说明具体使用场景与产出", "注入模板不符")
    _expect(injected.risk_note == "如无真实使用经历请勿添加", "注入建议缺少条件提醒")
    # 来源既要能在对象读到，也必须保留在最终序列化 JSON 中。
    serialized = json.loads(report.model_dump_json())
    _expect(sum(issue.get("source") == "injected" for issue in serialized["issues"]) == 1, "JSON 丢失注入 source")
    spoofed, _ = await _fake({"issues": [dict(baseline, source="injected")]})
    _expect("source" not in spoofed.issues[0].model_dump(), "允许模型伪造注入来源")
    # 模型已有合法技能依据时不重复注入；重复扣分也只能追加一次。
    mentioned, _ = await _fake({"issues": [dict(baseline, jd_evidence="Python")]}, job=missing_job, hard=hard)
    _expect(mentioned.meta["injected_count"] == 0, "已提及技能被重复注入")
    duplicated, _ = await _fake({"issues": [baseline]}, job=missing_job, hard=SimpleNamespace(deductions=hard.deductions * 2))
    _expect(duplicated.meta["injected_count"] == 1, "重复扣分产生重复意见")
    # 只要完整原文已体现技能，即便扣分过期、技能在截断尾文，也不能注入。
    for existing_text in (text + "\n技能：Python", text + "\n技能：Python Redis", text + "\n" + "长" * 5000 + "\n技能：Python"):
        stale, _ = await _fake({"issues": [baseline]}, job=missing_job, hard=hard, text=existing_text)
        _expect(stale.meta["injected_count"] == 0, "过期扣分或截断造成虚假缺口")
    unrelated, _ = await _fake({"issues": [baseline]}, hard=hard)
    _expect(unrelated.meta["injected_count"] == 0, "JD 没有要求仍注入技能")
    # 合法空结果可以纯代码兜底；模型不可用的 failed 契约仍保留。
    empty, _ = await _fake({"issues": []}, job=missing_job, hard=hard)
    _expect(empty.meta["status"] == "ok" and empty.meta["injected_count"] == 1 and empty.meta["candidate_count"] == 0, "空结果缺口兜底失败")
    broken = AsyncMock(side_effect=RuntimeError("模型不可用"))
    with patch.object(revision, "call_with_fallback_async", broken):
        failed = await revision.generate_issues(text, resume, missing_job, None, diagnosis, hard=hard)
    _expect(failed.meta["status"] == "failed" and not failed.issues, "注入掩盖模型全挂")
    # missing 候选省略风险字段也不能因旧的固定短语检查再被误杀。
    targets = revision._build_targets(text, diagnose(text, resume, missing_job), revision._job_sources(missing_job, None), hard, None)
    target = next(item for item in targets if item["category"] == "missing")
    candidate = dict(minimal, category=target["category"], severity=target["severity"], original_text=target["original_text"], suggested_text="", jd_evidence="Python")
    kept, rejected = revision._guard_candidates([candidate], text, revision._job_sources(missing_job, None), targets)
    _expect(len(kept) == 1 and not rejected and kept[0].location == "—", "missing 缺可选字段被间接误拒")
    # 不得用新版本数字绕过 M0，也不得把 Go 算作 Django 的子串覆盖。
    _expect(not revision._mentions("Django", "Go"), "技能子串伪覆盖")
    _expect(revision._mentions("Go MySQL", "Go"), "空白分隔的真实技能被漏掉")
    _expect(not revision._missing_skills(text, resume.model_copy(update={"skills": ["Python"]}), missing_job,
                                        infer_profile("后端开发工程师"), hard), "结构化技能已有仍注入")
    numbered_job = missing_job.model_copy(update={"must_have": ["Python9"]})
    numbered_hard = score_hard(resume, numbered_job, text)
    numbered, _ = await _fake({"issues": []}, job=numbered_job, hard=numbered_hard)
    _expect(not numbered.issues and numbered.meta["injection_skipped"] and not numbered.rejected_issues, "注入编造新数字或污染 rejected")
    # 技能族与别名沿用硬分：已用 Qwen 时，不因过期 DeepSeek 扣分而追加。
    family_job = job.model_copy(update={"must_have": ["DeepSeek"]})
    family_hard = score_hard(resume, family_job, text)
    family, _ = await _fake({"issues": []}, job=family_job, hard=family_hard, text=text + "\n技能：Qwen")
    _expect(not family.issues and family.meta["injected_count"] == 0, "技能族已覆盖仍注入")


async def _check_d(index: int) -> str:
    """零成本验证 D1/D2 的真实 JD 清理，以及 D3 画像理由完整保留。"""
    text, resume, job, _, diagnosis, baseline = _fixture()
    if index == 1:
        # 故意让模型违反新 prompt，证明代码不依赖模型自觉遵守来源限制。
        why = "JD 要求 Go…；岗位画像（推测）"
        report, fake = await _fake({"issues": [dict(baseline, why=why)]})
        _expect(len(report.issues) == 1 and not report.rejected_issues, "D1 有效理由被丢弃")
        _expect(report.issues[0].why == "JD 要求 Go…", "D1 原有理由未完整保留")
        # 除 why 与代码编号，全部意见字段都要与原输入保持一致。
        _expect(report.issues[0].model_dump(exclude={"id", "why"}) == {k: v for k, v in baseline.items() if k != "why"}, "D1 修改了其他意见字段")
        for before, after in (
                ("JD 要求 Go；岗位画像（推测）；原句缺证据", "JD 要求 Go；原句缺证据"),
                ("岗位画像（推测）；JD 要求 Go", "JD 要求 Go"),
                # D-M3-06：裸词属于原文事实说明，不再作为完整声明剥离。
                ("JD 要求 Go；推测", "JD 要求 Go；推测"),
                ("时间重叠，需核实（非推测）", "时间重叠，需核实（非推测）"),
                ("JD 要求 Go；（推测）", "JD 要求 Go"),
                ("JD 要求 Go ; ；岗位画像（推测） ； ; 原句缺证据", "JD 要求 Go;原句缺证据"),
                ("JD 要求 Go（岗位画像（推测））", "JD 要求 Go"),
                ("JD 要求 Go；「岗位画像（推测）」", "JD 要求 Go"),
                ("JD 要求 Go；岗位画像 ( 推测 )", "JD 要求 Go"),
                ("JD 要求  Go； 原句缺证据", "JD 要求  Go； 原句缺证据")):
            # 期望独立写明，既检查禁词消失，也检查事实、单个分号和原文空白保留。
            cleaned = revision._strip_inferred_note(before)
            _expect(cleaned == after, f"D1 纯函数改错其他内容：{before!r} → {cleaned!r}")
            # 清理幂等，二次处理注入报告时也不能逐次损失原理由。
            _expect(revision._strip_inferred_note(cleaned) == cleaned, "D1 清理不幂等")
        _expect(fake.await_count == 1 and report.meta["cost_yuan"] == 0, "D1 不是零成本单次假模型")
        return f"user_jd；why={report.issues[0].why!r}；其余字段保留；中间/尾部/分号/空白/幂等通过；成本=0"
    if index == 2:
        # 三种完全没有实质理由的声明都必须拒绝，不能输出一个空 why。
        for why in ("岗位画像（推测）", "； （推测） ； 岗位画像（推测） ；", "（推测）"):
            report, fake = await _fake({"issues": [dict(baseline, why=why)]})
            _expect(not report.issues and report.meta["status"] == "needs_review", "D2 保留空 why 意见")
            _expect(len(report.rejected_issues) == 1 and report.rejected_issues[0]["reason"] == "理由仅剩推测声明", "D2 缺少指定拒绝原因")
            _expect(report.meta["candidate_count"] == report.meta["rejected_count"] == 1, "D2 拒绝未审计")
            _expect(fake.await_count == 1 and report.meta["cost_yuan"] == 0, "D2 产生真实费用")
        # 一条声明意见被拒后，其他有效意见的内容和连续编号仍应正确。
        second = dict(baseline, original_text="负责缓存开发", location="另一段", why="JD 要求 Go…；岗位画像（推测）")
        targets = [baseline, second]
        # 只替换任务清单以提供第二个合法定位，七道护栏仍执行真实实现。
        fake = AsyncMock(return_value=(json.dumps({"issues": [dict(baseline, why="岗位画像（推测）"), second]}, ensure_ascii=False), UsageMeter()))
        with patch.object(revision, "call_with_fallback_async", fake), patch.object(revision, "_build_targets", return_value=targets):
            mixed = await revision.generate_issues(text + "\n负责缓存开发", resume, job, None, diagnosis)
        _expect(len(mixed.issues) == 1 and mixed.issues[0].id == "R-001" and mixed.issues[0].why == "JD 要求 Go…", "D2 清理后编号或理由错误")
        _expect(mixed.meta["candidate_count"] == mixed.meta["accepted_count"] + mixed.meta["rejected_count"] == 2, "D2 计数不守恒")
        return "user_jd；声明被剥离后为空 → rejected；reason=理由仅剩推测声明；混合候选/计数/编号通过；成本=0"
    draft = infer_profile("后端开发工程师")
    # 明确传 job=None，避免真实 JD 优先级让 D3 意外落到 user_jd 分支。
    profile = confirm_profile(draft, profile_hash(draft), edits={"must_have": job.must_have, "responsibilities": job.responsibilities})
    why = "JD 要求 Go…；岗位画像（推测）"
    fake = AsyncMock(return_value=(json.dumps({"issues": [dict(baseline, why=why)]}, ensure_ascii=False), UsageMeter()))
    # 除了比较输出，还让清理函数一旦被调用就抛错，确认 inferred 完全绕过剥离。
    with patch.object(revision, "call_with_fallback_async", fake), patch.object(revision, "_strip_inferred_note", side_effect=AssertionError("inferred 不得剥离")) as stripped:
        report = await revision.generate_issues(text, resume, None, profile, diagnosis)
    _expect(report.profile.source == "inferred" and len(report.issues) == 1, "D3 未走画像分支或意见被误拒")
    # 逐字符比较整个理由，防止为了过测只保留一小段声明。
    _expect(report.issues[0].why == why and stripped.call_count == 0, "D3 剥离或改写了画像理由")
    _expect(fake.await_count == 1 and report.meta["cost_yuan"] == 0, "D3 产生真实费用")
    return f"inferred；why={report.issues[0].why!r} 原样保留；剥离函数调用 0 次；成本=0"


def _check_c3() -> str:
    """检查新模块的注释/docstring，并用 git 基线证明旧 prompts 前缀逐字节保留。"""
    stats = []
    for relative in ("app/revision_llm.py", "tools/check_revision_llm.py"):
        # 注释用 tokenizer 统计，JSON 字符串里的井号不算注释。
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        comments = {token.start[0] for token in tokenize.generate_tokens(io.StringIO(source).readline) if token.type == tokenize.COMMENT}
        ratio = len(comments) / len(source.splitlines())
        # 分母包含空行，按 M0/M1 既有验收方式计算。
        nodes = [node for node in ast.walk(ast.parse(source)) if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
        # 模块与所有函数（含异步）都需要 docstring，不能只检查公开入口。
        _expect(ratio >= 0.15, f"{relative} 注释仅 {ratio:.1%}，要求≥15%")
        _expect(all(ast.get_docstring(node) for node in nodes), f"{relative} 缺少 docstring")
        stats.append(f"{Path(relative).name} 注释 {ratio:.1%}、docstring 100%")
    baseline = subprocess.run(["git", "show", "HEAD:app/prompts.py"], cwd=PROJECT_ROOT, capture_output=True, check=True).stdout
    current = (PROJECT_ROOT / "app/prompts.py").read_bytes()
    # git 只读取得基线，不写 index，不依赖提交新的实现才能验收。
    # 兼容工作树自动 CRLF；归一后必须整段旧文件仍是前缀，约束比只比常量更严。
    _expect(current.replace(b"\r\n", b"\n").startswith(baseline.replace(b"\r\n", b"\n")), "prompts.py 不是只追加")
    return "；".join(stats) + "；旧 prompts 整段保留，仅追加 SYSTEM_REVISION"


def _live_inputs(filename: str, *, profile=None) -> tuple:
    """读取指定真实 fixture 并运行既有 hard/semantic/diagnose，不预填评分结果。"""
    # 离线反例不需要语义模型；仅在真实组按项目已有逻辑加载缓存权重。
    from app.scoring_semantic import score_semantic

    path = PROJECT_ROOT / "tools/test_files" / filename
    text = extract_text(path.name, path.read_bytes())
    # 原始 PDF/Word 由现有解析器读取，验收不生成或修改任何 PDF。
    good = filename == "go_backend_resume.docx"
    resume = ResumeInfo(name="王浩" if good else "李娜", years=8 if good else 7,
                        skills=["Go", "Python", "PHP", "MySQL", "Redis", "Kafka", "Docker", "Gin", "LangChain", "Dify", "Git", "Linux"]
                        if good else ["Python", "FastAPI", "MySQL", "Redis"])
    # 画像模式复用已确认原文条目构造评分输入，不调用额外的抽取模型。
    job = _job() if profile is None else _job().model_copy(update={"title": profile.title,
             "must_have": list(profile.must_have), "responsibilities": list(profile.responsibilities),
             "required_years_min": None, "education": None})
    diagnosis, hard, sem = diagnose(text, resume, job), score_hard(resume, job, text), score_semantic(text, job)
    # 上游分数实际计算，不用旧报告中的历史值代替当前覆盖判定。
    return text, resume, job if profile is None else None, profile, diagnosis, hard, sem


async def _live(inputs: tuple):
    """对一组已计算的上游输入调用真实 fallback，并返回未经改动的 RevisionReport。"""
    text, resume, job, profile, diagnosis, hard, sem = inputs
    return await revision.generate_issues(text, resume, job, profile, diagnosis, hard=hard, sem=sem)


def _summary(report) -> str:
    """打印真实意见计数、类别、拒绝原因与费用摘要，不隐藏被拒绝项。"""
    # 拒绝原因原样输出，不能只给最终有效条数让人工无法排查。
    return (f"status={report.meta['status']}；候选={report.meta['candidate_count']}、有效={len(report.issues)}、注入={report.meta['injected_count']}、拒绝={len(report.rejected_issues)}；"
            f"category={sorted({i.category for i in report.issues})}；成本={report.meta['cost_yuan']:.8f} 元；"
            f"rejected={json.dumps(report.rejected_issues, ensure_ascii=False)}")


async def _run(offline: bool, output_dir: Path | None) -> int:
    """执行全部分组；每组失败继续，保存原始真实报告并打印总成本和通过数。"""
    passed, executed, total_cost = 0, 0, 0.0
    live_reports, inputs, counts = [], None, []
    for index in range(1, 8):
        # 每组独立计数，异常之后继续，最终退出码反映任何未通过项。
        executed += 1
        try:
            detail = await _check_b(index)
        except Exception as exc:
            print(f"B{index}：失败；{type(exc).__name__}: {exc}", flush=True)
        else:
            passed += 1
            print(f"B{index}：通过；{detail}", flush=True)
    # 离线异常不会阻断真实组，真实组失败也不能让 B/C 被标成未执行。
    if not offline:
        for index, filename in enumerate(("go_backend_resume.docx", "text_resume.pdf", "text_resume.pdf"), 1):
            # A3 换成画像输入；保留同一薄简历以观察岗位上下文变化。
            executed += 1
            report = None
            try:
                profile = None
                if index == 3:
                    draft = infer_profile("FDE 交付工程师")
                    profile = confirm_profile(draft, profile_hash(draft))
                current = _live_inputs(filename, profile=profile)
                # C1 复用 A1 的上游结果，但三次模型响应分别真实调用、分别计费。
                if index == 1:
                    inputs = current
                report = await _live(current)
                # 先记实际费用再做断言，失败的真实调用也必须计入总成本。
                live_reports.append(report)
                total_cost += report.meta["cost_yuan"]
                print(f"A{index} 实测：{_summary(report)}", flush=True)
                _audit(report, current[0], current[2], current[5])
                if index == 1:
                    _expect(len(report.issues) >= 2, "好简历不足两条有效意见")
                    _expect(not any(any(term in row["reason"] for term in ("新名词", "专名", "数字")) for row in report.rejected_issues), "好简历出现被拒绝的幻觉项")
                elif index == 2:
                    # 比较两份真实输出，而非将任一份改写成预期条数。
                    _expect(live_reports and len(report.issues) >= len(live_reports[0].issues), "薄简历条数少于好简历")
                    _expect(any(i.category == "missing" for i in report.issues), "薄简历没有 missing")
                else:
                    _expect(report.profile.source == "inferred" and report.profile.confirmed, "画像来源或确认状态丢失")
                    _expect(any(any(find_original(term, i.jd_evidence) for term in profile.must_have) for i in report.issues), "画像意见没有回链 must_have")
                    _expect(sum(r.meta["cost_yuan"] for r in live_reports[:3]) <= 0.015, "A组总成本超过0.015元")
            except Exception as exc:
                # 将网络或模型失败清晰打印，绝不改用假模型冒充 A 组通过。
                print(f"A{index}：失败；{type(exc).__name__}: {exc}", flush=True)
            else:
                passed += 1
                print(f"A{index}：通过", flush=True)
        executed += 1
        try:
            _expect(inputs is not None and live_reports, "A1 无有效调用，无法验证三次确定性")
            first = live_reports[0]
            results = [first]
            for round_index in (2, 3):
                # 逐轮真实请求；不缓存意见，也不取三次响应的交集来伪装确定性。
                report = await _live(inputs)
                total_cost += report.meta["cost_yuan"]
                live_reports.append(report)
                results.append(report)
                print(f"C1 第{round_index}次实测：{_summary(report)}", flush=True)
            for report in results:
                _audit(report, inputs[0], inputs[2], inputs[5])
            counts = [len(r.issues) for r in results]
            # 最大值减最小值能发现三轮向两个方向分别漂移。
            categories = [{i.category for i in r.issues} for r in results]
            _expect(max(counts) - min(counts) <= 1 and categories[0] == categories[1] == categories[2], f"三次波动超限：{counts} / {categories}")
        except Exception as exc:
            print(f"C1：失败；{type(exc).__name__}: {exc}", flush=True)
        else:
            passed += 1
            print(f"C1：通过；三次有效条数={counts}；category集合一致", flush=True)
    else:
        # 明示跳过，离线 12/12 不能冒充包含真实调用的完整 17/17。
        print("A1/A2/A3/C1：跳过（--offline；不计为通过）", flush=True)
    for label, action in (("C2", _check_c2), ("C3", _check_c3)):
        executed += 1
        try:
            detail = await action() if label == "C2" else action()
        except Exception as exc:
            print(f"{label}：失败；{type(exc).__name__}: {exc}", flush=True)
        else:
            passed += 1
            print(f"{label}：通过；{detail}", flush=True)
    for index in range(1, 4):
        # D1～D3 无论真实组是否失败都执行，各自计入总分母。
        executed += 1
        try:
            detail = await _check_d(index)
        except Exception as exc:
            print(f"D{index}：失败；{type(exc).__name__}: {exc}", flush=True)
        else:
            passed += 1
            print(f"D{index}：通过；{detail}", flush=True)
    if offline:
        # D4 明确跳过且不计通过，离线输出不能冒充真实来源验收。
        print("D4：跳过（--offline；不计为通过）", flush=True)
    else:
        executed += 1
        try:
            # D4 独立真实调用；只复用上游诊断/评分，不复用 A1 的模型响应。
            current = inputs if inputs is not None else _live_inputs("go_backend_resume.docx")
            report = await _live(current)
            # 先记录真实费用再断言，即使 D4 失败也不把调用成本清零。
            total_cost += report.meta["cost_yuan"]
            live_reports.append(report)
            print(f"D4 实测：{_summary(report)}", flush=True)
            _expect(report.profile.source == "user_jd", "D4 未使用真实 JD 模式")
            _audit(report, current[0], current[2], current[5])
            occurrences = {phrase: sum(issue.why.count(phrase) for issue in report.issues) for phrase in ("岗位画像", "推测")}
            # 分别打印两个短语的出现次数，而非只判断合成声明是否消失。
            _expect(all(count == 0 for count in occurrences.values()), f"D4 真实 JD 理由仍被污染：{occurrences}")
        except Exception as exc:
            print(f"D4：失败；{type(exc).__name__}: {exc}", flush=True)
        else:
            passed += 1
            print(f"D4：通过；独立真模型 user_jd；全部 {len(report.issues)} 条 why；岗位画像=0 次、推测=0 次", flush=True)
    if output_dir is not None and live_reports:
        # 输出目录由调用者显式给定；生成 JSON 是 M3 交付物，不触及 PDF 渲染。
        output_dir.mkdir(parents=True, exist_ok=True)
        # 原始真实报告逐份保存；不为制造 rejected 示例而往真实报告添加假条目。
        (output_dir / "M3_真实意见.json").write_text(json.dumps([r.model_dump() for r in live_reports], ensure_ascii=False, indent=2), encoding="utf-8")
        # 若真模型无 rejected，则另存明确标注的假模型 B1 示例，避免混淆证据来源。
        baseline = _fixture()[-1]
        rejected_example, _ = await _fake({"issues": [dict(baseline, suggested_text="使用 Kubernetes 交付接口")]})
        example = {"source": "真实模型原始 RevisionReport", "report": next((r for r in live_reports if r.rejected_issues), live_reports[0]).model_dump(),
                   "rejected_example_source": "B1 假模型注入，仅演示拒绝审计，不计入真实报告", "rejected_example": rejected_example.rejected_issues}
        (output_dir / "M3_真实意见示例.json").write_text(json.dumps(example, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"真实 JSON：{output_dir / 'M3_真实意见示例.json'}", flush=True)
    # 总成本包括 A/C1/D4 全部六次真实调用；假 meter 不算真实开销。
    scope = "离线零模型" if offline else "含全部 A/C1/D4 真实调用"
    print(f"总成本 {total_cost:.8f} 元（UsageMeter；{scope}）", flush=True)
    print(f"通过 {passed}/{executed}", flush=True)
    return 0 if passed == executed else 1


def main() -> int:
    """解析离线/输出目录选项并执行异步验收，返回 0/1 退出码。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="仅执行零模型 B/C2/C3/D1～D3，跳过组不记通过")
    parser.add_argument("--output-dir", type=Path, help="保存全部真实报告与含 rejected 的明确来源示例")
    args = parser.parse_args()
    # 网络调用只在默认完整模式发生；不从失败真实调用自动切换离线模式。
    return asyncio.run(_run(args.offline, args.output_dir))


if __name__ == "__main__":
    # 非零退出码反映全部真实分组与规范验收，避免只打印错误却宣称成功。
    raise SystemExit(main())
