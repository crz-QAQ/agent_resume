"""M4 A～I 与 K1 合成验收：生成、读回和检查 PDF，全程禁止网络、模型与环境文件。"""

from __future__ import annotations

import ast
import hashlib
import json
from contextlib import ExitStack
from dataclasses import dataclass
from io import BytesIO, StringIO
from pathlib import Path
import re
import sys
from time import perf_counter
import tokenize
from tempfile import NamedTemporaryFile
from unittest.mock import patch

import pdfplumber
from reportlab.pdfbase import pdfmetrics

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 测试可从任意目录启动，字体和样张路径均以工作区为基准。
sys.path.insert(0, str(PROJECT_ROOT))
# 用户指定 -B；直接导入执行时也避免生成源码目录的字节码。
sys.dont_write_bytecode = True

from app import pdf_font, pdf_kit, report_pdf, revision_pdf as revision
from app.schemas_match import Deduction
from app.schemas_revision import AdaptedText, DiagnosisFinding, DiagnosisReport, JobProfile, KwCoverage, ReviewIssue, RevisionReport, ScoreDelta
from app.schemas_revision import UpliftItem, UpliftPlan
from app.learning_resources import LearningGap, LearningPlan, Material, load_materials
from app.scoring_rules import HardScore


@dataclass
class Artifact:
    """保存真实生成的字节、文本、页数及独立渲染耗时，不以内容长度推测页数。"""

    # 字节与页数分别存储，检查时不能用文件名中的数字冒充实际页数。
    name: str
    data: bytes
    pages: int
    text: str
    elapsed_ms: float


# A/B/C 作为稳定样张；其余边界样例只用于验证，不改动真实个人简历。
ARTIFACTS: list[Artifact] = []
STAMP = "2026-11-12T13:00:00+08:00"
# 固定时间让重复验收可比较，不把当前时间偷偷补成历史审计值。


def _expect(condition: object, message: str) -> None:
    """显式断言条件，失败抛含具体问题的 AssertionError，不依赖解释器优化选项。"""
    if not condition:
        raise AssertionError(message)


def _issue(**updates) -> ReviewIssue:
    """生成固定合成意见；更新字段仅用于相应排版反例，不进行模型推理。"""
    # 基线只描述可核实的职责；三态反例逐字段覆盖，避免各样例互相污染。
    row = dict(id="R-001", category="weak_evidence", severity="medium", location="工作经历 · 第 2 段",
               original_text="负责订单系统的接口开发", suggested_text="使用 Go 完成订单接口开发，说明实际交付范围",
               why="JD 要求订单接口开发，原句只列职责，没有可核对的交付范围",
               gain="只列职责 → 展示可核实的交付范围", jd_evidence="负责 Go 订单接口开发与性能优化",
               risk_note="只填真实内容，未发生的经历不要添加")
    return ReviewIssue(**{**row, **updates})


def _inputs() -> tuple[RevisionReport, DiagnosisReport, HardScore]:
    """自造两条意见、两项缺口、五个关键词与年限扣分，完全不调用评分入口。"""
    # 时间类保留开放日期，渲染器不能凭当前日期补造结束月份。
    same = "2021-至今 某 SaaS 公司 高级后端"
    profile = JobProfile(title="Go 后端工程师", source="user_jd", confirmed=True,
                         must_have=["Go", "Redis", "Kubernetes"], responsibilities=["负责 Go 订单接口开发与性能优化"], note="用户真实 JD")
    # 两条意见中不放 missing 卡片，A 必须从真实缺词清单给出人工补充指引。
    report = RevisionReport(issues=[_issue(), _issue(id="R-002", category="inconsistent", severity="high",
                location="工作经历 · 第 3 段", original_text=same, suggested_text=same,
                why="JD 需要核对后端经历，至今的时间边界需本人确认", gain="开放时间 → 本人核实起止年月")],
                rejected_issues=[], profile=profile, score_before=73, score_after_estimate=None,
                adaptations=[], skipped=[{"issue_id": "SKIP", "reason": "未采用标记不能混成缺技能"}],
                meta={"status": "ok", "model": "synthetic-offline", "prompt_version": "M4_fixture",
                      "cost_yuan": 0, "elapsed_ms": 19, "generated_at": STAMP})
    # 故意打乱频次与覆盖顺序，E 必须验证排序而不是复现输入顺序。
    coverage = [KwCoverage(term=term, count_in_resume=count, sections=sections, from_must_have=must)
                for term, count, sections, must in (("Go", 3, ["技能", "工作经历"], True),
                ("Redis", 0, [], True), ("Python", 2, ["技能"], False),
                ("Kubernetes", 0, [], True), ("MySQL", 1, ["技能"], False))]
    diagnosis = DiagnosisReport(findings=[DiagnosisFinding(code="open_ended", severity="medium",
                   location="工作经历 · 第 3 段", evidence=same, detail="开放时间需要本人确认")],
                   keyword_coverage=coverage, missing_keywords=["Redis", "Kubernetes"],
                   completeness={"name": True, "years": True, "education": True, "skills": True, "contact": True},
                   meta={"cost_yuan": 0})
    # 直接实例化现成分数契约；不调用 score_hard，也不让 PDF 重算分值。
    hard = HardScore(score=33, breakdown={"years": 8, "education": 10, "skills": 15}, deductions=[
        Deduction(item="工作年限", type="years_short", deduct=7, jd_evidence="至少 3 年后端经验",
                  resume_evidence="2 年后端经验", severity="high")])
    return report, diagnosis, hard


def _generate(name: str, report: RevisionReport, diagnosis: DiagnosisReport, hard=None,
              *, delta: ScoreDelta | None = None, learning_plan: LearningPlan | None = None,
              uplift_plan: UpliftPlan | None = None) -> Artifact:
    """计时生成并用 pdfplumber 实读字节，检查输入未被改动及合成渲染低于 300ms。"""
    # 比较整个序列化快照，能发现元数据或关键词被渲染器原地修改。
    before = report.model_dump_json(), diagnosis.model_dump_json(), repr(hard), repr(delta), repr(learning_plan), repr(uplift_plan)  # 新计划也纳入输入快照。
    started = perf_counter()
    data = revision.render_review_pdf(report, diagnosis, job_title=report.profile.title,
                                      resume_name="合成简历" + name, hard=hard, delta=delta,
                                      learning_plan=learning_plan, uplift_plan=uplift_plan)
    # 性能门槛只计渲染，不把独立 PDF 文本解析耗时混进来。
    elapsed = (perf_counter() - started) * 1000
    _expect(before == (report.model_dump_json(), diagnosis.model_dump_json(), repr(hard), repr(delta),
                      repr(learning_plan), repr(uplift_plan)), f"{name} 修改输入")
    # 重新打开真实字节流，章节与页数不能来自构建 story 的内存结构。
    with pdfplumber.open(BytesIO(data)) as document:
        item = Artifact(name, data, len(document.pages), "\n".join(page.extract_text() or "" for page in document.pages), elapsed)
    # 失败组的已生成样张也保留，便于定位真实字体或分页问题。
    ARTIFACTS.append(item)
    _expect(elapsed < 300, f"{name} 合成渲染 {elapsed:.2f}ms，要求<300ms")
    return item


def _compact(text: str) -> str:
    """去提取文本中的空白，用于跨 PDF 自动换行比较原有中文内容。"""
    # 只用于 PDF 提取排版差异，不作为业务原句检索或数值校验。
    return "".join(text.split())


def _check_a() -> str:
    """检查正常样张的大小、中文、章节顺序、两条意见和人工指引。"""
    report, diagnosis, hard = _inputs()
    item = _generate("A", report, diagnosis, hard)
    # 大小阈值排除只生成空页或未嵌入字体的伪成功结果。
    _expect(item.pages >= 1 and len(item.data) > 20 * 1024, "A PDF 过小或无页面")
    for label in ("简历修改意见报告", "建议改为", "为什么改", "不建议改", "ATS", "不可代写", "此行需你补全"):
        _expect(label in item.text, f"A 读不回 {label}")
    # 检查每个标题的实际位置，不能只证明所有标题曾经出现。
    titles = ("简历修改意见报告", "结论摘要", "逐条意见", "缺口清单", "4.5 ATS 关键词覆盖", "不建议改")
    positions = [item.text.index(title) for title in titles]
    _expect(positions == sorted(positions), "PRD 章节顺序变化")
    # 真实 JD 与推测画像的来源标签互斥，避免延续 D-M3-04 的来源混淆。
    _expect("依据：用户提供的真实 JD" in item.text and "岗位画像（推测）" not in item.text, "A 来源错误")
    _expect("JD必备项缺2个" in _compact(item.text), "摘要没有按上游关键词统计缺口")
    # 未采用项和评分字段都有诱饵，检查它们未进入不相干的正文区域。
    _expect("未采用标记不能混成缺技能" not in item.text and "73 分" not in item.text, "混入无关结论或未采用项")
    return f"{item.pages} 页 / {len(item.data)} 字节 / {item.elapsed_ms:.2f}ms；章节、两条卡片、两项缺口可读"


def _check_b() -> str:
    """检查推测画像红色横幅，声明使用唯一 profile_banner 文案。"""
    report, diagnosis, hard = _inputs()
    # 只切换画像来源，其余数据保持同一份合成契约。
    report.profile = report.profile.model_copy(update={"source": "inferred", "note": "岗位画像推测"})
    item = _generate("B", report, diagnosis, hard)
    _expect("岗位画像（推测）" in item.text and "非真实 JD" in item.text, "B 没有画像来源声明")
    _expect("依据：用户提供的真实 JD" not in item.text, "B 错印真实 JD 来源")
    # 文字存在还不够，另检查红底图形确实绘进 PDF。
    _red_banner(item)
    return f"{item.pages} 页 / {len(item.data)} 字节 / {item.elapsed_ms:.2f}ms；画像红横幅正确"


def _red_banner(item: Artifact) -> None:
    """核对 PDF 真正绘制红底矩形，不能只检查字符串就声称有红色告警。"""
    # pdfplumber 读出的 RGB 为浮点数，允许显示编码带来的微小精度差。
    with pdfplumber.open(BytesIO(item.data)) as document:
        colors = [shape.get("non_stroking_color") for page in document.pages for shape in page.rects]
    _expect(any(isinstance(value, (tuple, list)) and len(value) == 3 and
                all(abs(a - b) < .01 for a, b in zip(value, (254 / 255, 228 / 255, 226 / 255))) for value in colors), "没有红底横幅")


def _check_c() -> str:
    """检查失败报告：保留来源、诊断与审计，意见区明确为横线。"""
    report, diagnosis, hard = _inputs()
    # 故意留一份旧意见，确保 failed 不会把旧结果伪装成这次有效意见。
    report.meta["status"] = "failed"
    item = _generate("C", report, diagnosis, hard)
    _expect("模型不可用，本次报告不完整" in item.text, "C 缺失败声明")
    # 限定意见章节，不能借其他表格里的横线冒充失败空态。
    opinions = item.text.split("逐条意见", 1)[1].split("缺口清单", 1)[0]
    _expect("—" in opinions and "建议改为" not in opinions, "C 输出了旧意见")
    _red_banner(item)
    return f"{item.pages} 页 / {len(item.data)} 字节 / {item.elapsed_ms:.2f}ms；失败红横幅、意见区—"


def _check_d() -> str:
    """分别验证空建议、仅空白差异的原样建议、正常建议及 missing 防虚构。"""
    report, diagnosis, hard = _inputs()
    rows = [_issue(id="D-empty", category="missing", suggested_text=" \n\t "),
            _issue(id="D-same", original_text="2021-至今 后端", suggested_text="2021 - 至今\n后端"),
            _issue(id="D-normal", suggested_text="正常建议标记：说明原有接口交付范围")]
    # 第二条只有空白差异；若只比较原始字符串会错误地显示高亮。
    expected = [(revision.MISSING_GUIDANCE, False), (revision.UNCHANGED_GUIDANCE, False), (rows[2].suggested_text, True)]
    for row, want in zip(rows, expected):
        _expect(revision._render_suggestion(row) == want, f"三态错误 {row.id}")
    # 异常上游不能把缺失技能写成已有经历，即使其正文非空。
    forged = _issue(category="missing", suggested_text="已经精通从未使用的技术")
    _expect(revision._render_suggestion(forged) == (revision.MISSING_GUIDANCE, False), "missing 渲染为已有经历")
    # helper 的返回值和最终 PDF 均检查，防止调用方忽略了三态结果。
    report.issues = rows
    item = _generate("D", report, diagnosis, hard)
    text = _compact(item.text)
    # 人工指引会自动换行，比较时忽略提取器加入的换行空白。
    _expect(all(_compact(want[0]) in text for want in expected), "PDF 没有三态实际文本")
    # 三条建议的标签都应存在，不能以删除整块来掩盖空建议栏。
    _expect(item.text.count("建议改为") == 3, "建议栏丢失")
    return f"{item.pages} 页 / {len(item.data)} 字节；三态逐项读回，高亮标记=False/False/True"


def _check_e() -> str:
    """检查 ATS 缺失优先、次数升序、段落拼接和三十行截断。"""
    report, diagnosis, _ = _inputs()
    item = next(row for row in ARTIFACTS if row.name == "A")
    # 关键词也会在缺口清单重复出现，必须只截取 ATS 表来判断顺序。
    ats = item.text.split("4.5 ATS 关键词覆盖", 1)[1].split("不建议改", 1)[0]
    _expect([ats.index(word) for word in ("Redis", "Kubernetes", "MySQL", "Python", "Go")] ==
            sorted(ats.index(word) for word in ("Redis", "Kubernetes", "MySQL", "Python", "Go")), "ATS 排序错误")
    _expect("技能、工作经历" in _compact(ats) and "—" in ats, "段落合并或空位置错误")
    # 超限用例清空其他文字，确保唯一词标记只能来自关键词表。
    report.issues, diagnosis.findings, diagnosis.missing_keywords = [], [], []
    diagnosis.keyword_coverage = [KwCoverage(term=f"ATS项{index:03d}终", count_in_resume=index % 4,
                     sections=[] if index % 4 == 0 else ["技能"], from_must_have=True) for index in range(32)]
    many = _generate("E", report, diagnosis)
    # 同频次保留输入顺序，并对最终真实文本逐项比较三十个完整标记。
    expected = [f"ATS项{index:03d}终" for index in sorted(range(32), key=lambda i: (i % 4 > 0, i % 4))[:30]]
    found = re.findall(r"ATS项\d{3}终", _compact(many.text))
    # 用完整结束标记避免 001 被 0010 之类的词包含而产生假通过。
    _expect(found == expected and "仅显示前30个关键词" in _compact(many.text), "ATS 30 行上限或稳定排序错误")
    return f"{many.pages} 页 / {len(many.data)} 字节；缺失在前、次数升序、段落/—、30行上限通过"


def _check_f() -> str:
    """检查 §7.2 全部指定类型、仅诊断回退、无问题空态和可选智能修改展示。"""
    report, diagnosis, hard = _inputs()
    # 已知不足与未知披露都需要诚实提醒，不能只覆盖 years_short 一种。
    hard.deductions = [Deduction(item="合成硬项", type=kind, deduct=1, jd_evidence="上游要求",
                      resume_evidence="上游现状：" + kind, severity="high")
                      for kind in ("years_short", "years_unknown", "education", "education_unknown")]
    diagnosis.findings = [DiagnosisFinding(code=code, severity="medium", location="工作经历",
                    evidence="上游时间证据：" + code, detail="上游时间问题")
                    for code in ("timeline_overlap", "timeline_invalid", "open_ended")]
    # 联系方式作为负例，避免把所有 missing_section 都说成改不动的硬门槛。
    diagnosis.findings.extend(DiagnosisFinding(code="missing_section", severity="high", location="其他",
                    evidence="上游原文", detail=detail) for detail in ("缺少年限信息", "缺少学历信息", "缺少联系方式"))
    rows = revision._unchangeable_rows(diagnosis, hard)
    _expect(len(rows) == 9 and all(len(row) == 4 for row in rows), "不建议改规则漏项或误收联系方式")
    # 缺 hard 只能显示诊断证据，不能凭职位名称猜年限或学历差距。
    _expect(len(revision._unchangeable_rows(diagnosis, None)) == 5, "hard=None 时伪造硬性条目")
    item = _generate("F", report, diagnosis, hard)
    area = item.text.split("不建议改", 1)[1]
    # 标题之外必须实际出现四列表头和上游现状，不能只返回内部规则行。
    _expect(all(word in area for word in ("年限", "学历", "时间线", "为什么改不了", "你能做什么")), "四列内容未读回")
    # 同一诊断 code 的四段经历只显示一行；原始四条诊断不应被渲染器合并修改。
    multiple = diagnosis.model_copy(deep=True)
    dates = ["2024.04-2024.08", "2021.07-2024.08", "2020.01-2021.06", "2019.01-2020.02"]
    multiple.findings = [DiagnosisFinding(code="timeline_overlap", severity="medium", location=f"第{index}段",
        evidence=date + " 某公司经历", detail="上游时间矛盾") for index, date in enumerate(dates, 1)]
    merged_rows = revision._unchangeable_rows(multiple, None)
    _expect(len(merged_rows) == 1 and "时间线矛盾（4 段经历）" in merged_rows[0][1]
            and all(date in merged_rows[0][1] for date in dates), "同一 code 没有合并并保留日期摘要")
    merged = _generate("F-merged", report, multiple)
    _expect("时间线矛盾（4段经历）" in _compact(merged.text) and
            all(date in _compact(merged.text) for date in dates), "合并摘要没有完整写入PDF")
    # 单独生成真正的空态，不让正常样张中的规则行替空态通过测试。
    diagnosis.findings, diagnosis.keyword_coverage, diagnosis.missing_keywords, report.issues = [], [], [], []
    empty = _generate("F-empty", report, diagnosis)
    _expect("本次没有发现改不动的问题" in empty.text, "无问题空态缺失")
    # 只提供已有前后文本，渲染器不得发起智能改写或另产一份简历。
    report.adaptations = [AdaptedText(issue_id="R-001", location="工作经历", before="上游改前", after="上游改后",
                                     added_terms=[], kept_numbers=[])]
    adapted = _generate("F-adapted", report, diagnosis)
    _expect(all(word in adapted.text for word in ("智能修改", "上游改前", "上游改后", "未采用标记不能混成缺技能")), "未按上游展示智能修改")
    return f"{item.pages} 页 / {len(item.data)} 字节；years/education/三类时间/两类缺项、无问题空态与上游对比通过"


def _check_g() -> str:
    """制造多页长卡片，检查每页页脚、卡片不拆页、A4 边界及转义截断。"""
    report, diagnosis, hard = _inputs()
    report.issues = []
    tags = ["LONG-" + char for char in "ABCDEF"]
    for tag in tags:
        # 每个字段放同一独特标记，必须在同一页同时找到，不能只查卡片标题。
        report.issues.append(_issue(id=tag, original_text=f"原句{tag}：" + "真实职责与接口证据" * 30,
            suggested_text=f"建议{tag}：" + "仅改写原有事实" * 30, why=f"理由{tag}：" + "岗位要求与原文问题" * 30,
            jd_evidence=f"依据{tag}：" + "原有岗位职责" * 30, gain=f"收益{tag}：" + "从职责到证据" * 30,
            risk_note=f"风险{tag}：" + "仅填真实信息" * 30))
    # 真实上传证据可能含大量换行；卡片不能只在无换行的长句上通过整页约束。
    report.issues[-1].why = "理由LONG-F：\n" + "职责证据\n" * 150
    item = _generate("G", report, diagnosis, hard)
    # 至少三页使 onLaterPages 路径与多次页脚消费真正被执行。
    _expect(item.pages >= 3, "G 未实际覆盖多页")
    with pdfplumber.open(BytesIO(item.data)) as document:
        page_texts = [_compact(page.extract_text() or "") for page in document.pages]
        for number, (page, text) in enumerate(zip(document.pages, page_texts), 1):
            # 每页逐项核对，不能用整份 PDF 文本包含一次页脚来替代。
            _expect("AI生成" in text and "prompt" in text and "M4_fixture" in text, f"第 {number} 页缺页脚")
            _expect("19ms" in text and "synthetic-offline" in text, "审计值被改写")
            _expect(abs(page.width - 595.276) < 1 and abs(page.height - 841.89) < 1, "不是 A4")
            # 对实际字符边框作页面边界检查，长无空格中文也必须自动换行。
            _expect(all(0 <= char["x0"] <= char["x1"] <= page.width + 1 and char["bottom"] <= page.height for char in page.chars), "文字超出页面")
        for tag in tags:
            # 摘要会重复前三条原句；用建议/风险字段定位卡片，排除摘要中的标记。
            locations = [i for i, text in enumerate(page_texts) if "建议" + tag in text or "风险" + tag in text]
            _expect(len(locations) == 1, f"卡片 {tag} 跨页")
            _expect(all(prefix + tag in page_texts[locations[0]] for prefix in ("原句", "建议", "理由", "依据", "收益", "风险")), f"卡片 {tag} 字段丢失")
    # 保存压力样张供逐页视觉检查，稳定交付样张仍为 A/B/C 三份。
    temporary = PROJECT_ROOT / "tmp/pdfs"
    temporary.mkdir(parents=True, exist_ok=True)
    (temporary / "M8b_M9b_G_分页.pdf").write_bytes(item.data)
    return f"{item.pages} 页 / {len(item.data)} 字节 / {item.elapsed_ms:.2f}ms；每页免责/审计齐全，六张长卡片均不跨页"


def _check_h() -> str:
    """检查字体真实名称、统一入口调用与字体失败直抛，不允许静默换字体。"""
    font = pdf_font.get_cjk_font()
    # 注册别名和 PDF 嵌入的字体内部名称不同，必须取得真实字体面名称。
    name = pdfmetrics.getFont(font).face.name
    name = name.decode() if isinstance(name, bytes) else name
    for item in ARTIFACTS:
        with pdfplumber.open(BytesIO(item.data)) as document:
            # 连审计英文与状态符号也一起检查，防止仅中文正文使用正确字体。
            fonts = {char["fontname"] for page in document.pages for char in page.chars}
        _expect(all(name in value or font in value for value in fonts), f"字体不来自统一入口：{fonts}")
    report, diagnosis, hard = _inputs()
    # 包装真实入口，而非返回伪字体名，保持 PDF 真正可读。
    with patch.object(pdf_font, "get_cjk_font", wraps=pdf_font.get_cjk_font) as lookup:
        _generate("H-font", report, diagnosis, hard)
    _expect(lookup.call_count == 1, "未通过统一字体入口")
    # 字体不可用必须中止，不能悄悄生成方块 PDF。
    with patch.object(pdf_font, "get_cjk_font", side_effect=RuntimeError("受控字体不可用")):
        try:
            revision.render_review_pdf(report, diagnosis, job_title="合成岗位")
        except RuntimeError as exc:
            _expect(str(exc) == "受控字体不可用", "字体异常原因被覆盖")
        else:
            raise AssertionError("字体异常被静默吞掉")
    for path in ("app/revision_pdf.py", "app/pdf_kit.py"):
        # 同时限制新渲染器与公共件，防止把禁用字体藏到新模块里。
        _expect("Hel" + "vetica" not in (PROJECT_ROOT / path).read_text(encoding="utf-8"), "源码引入禁用默认字体")
    return f"统一字体 {font} / {name}；全部 PDF 字体、入口及异常传播通过"


def _check_i() -> str:
    """检查注释/docstring、七件唯一来源及渲染模块无模型、网络和环境依赖。"""
    summaries = []
    names = {"_limit_for_width", "_field", "_local_time", "_styles", "_table", "_section", "_footer"}
    # 身份比较证明真正共用一个实现，不能只复制出相同源码蒙混过关。
    for name in names:
        _expect(getattr(report_pdf, name) is getattr(pdf_kit, name), f"评分报告没有复用 {name}")
    for relative in ("app/pdf_kit.py", "app/revision_pdf.py", "tools/check_revision_pdf.py"):
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        # 用 tokenizer 计真实注释，颜色字符串的井号和 docstring 均不算注释。
        comments = {token.start[0] for token in tokenize.generate_tokens(StringIO(source).readline) if token.type == tokenize.COMMENT}
        ratio = len(comments) / len(source.splitlines())
        # 私有函数、回调和类都纳入 docstring 覆盖，空行也计入注释率分母。
        nodes = [node for node in ast.walk(tree) if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
        _expect(ratio >= .15, f"{relative} 注释仅 {ratio:.1%}")
        _expect(all(ast.get_docstring(node) for node in nodes), f"{relative} docstring 不完整")
        if relative == "app/revision_pdf.py":
            _expect(not names.intersection(node.name for node in tree.body if isinstance(node, ast.FunctionDef)), "复制共享排版函数")
            _expect("drawString" not in source, "使用画布手排长文字")
        summaries.append(f"{Path(relative).name} {ratio:.1%}")
    # 评分报告必须删掉旧定义，同时保留导入以兼容原自检的引用路径。
    _expect(not names.intersection(node.name for node in ast.parse((PROJECT_ROOT / "app/report_pdf.py").read_text(encoding="utf-8")).body if isinstance(node, ast.FunctionDef)), "评分报告残留共享函数定义")
    # 导入图没有模型客户端或 dotenv，避免只拦网络却仍读取了用户配置。
    forbidden = ("app.schemas", "app.fallback", "app.providers", "app.revision_llm", "app.scoring_semantic", "dotenv", "openai", "fastembed")
    _expect(not any(name in sys.modules for name in forbidden), "PDF 路径导入模型或环境模块")
    return "；".join(summaries) + "；docstring 100%；七件共用、零模型/网络/环境依赖"


def _check_k1() -> str:
    """K1：传 delta 才在分数对比下方印小字提示，无 delta 的智能修改不印。"""
    report, diagnosis, hard = _inputs()
    delta = ScoreDelta(before_total=50, after_total_estimate=55,
        before_parts={"hard": 33, "semantic": 7, "llm": 10},
        after_parts={"hard": 33, "semantic": 12, "llm": 10},
        delta_parts={"hard": 0, "semantic": 5, "llm": 0}, estimated_fields=["llm"],
        covered_before=1, covered_after=2, notes=["语义覆盖增加；LLM 沿用原分，预估波动 ±2"])
    # 保留改写对照，使无 delta 的反例仍包含完整智能修改章节。
    report.adaptations = [AdaptedText(issue_id="R-001", location="工作经历", before="上游改前", after="上游改后",
                                     added_terms=[], kept_numbers=[])]
    with_delta = _generate("K1-delta", report, diagnosis, hard, delta=delta)
    without_delta = _generate("K1-no-delta", report, diagnosis, hard)
    warning = "语义分对 JD 原句高度敏感：若只是把 JD 措辞搬进简历，分数会虚高，面试时无法自证"
    text = _compact(with_delta.text)
    _expect(_compact(warning) in text and "虚高" in text, "传 delta 没有完整虚高提示")
    # 顺序限定在分数对比与改写对照之间，不能把文案印到无关页脚。
    _expect(text.index("分数对比") < text.index(_compact(warning)) < text.index("上游改前"), "虚高提示不在分数对比下方")
    _expect("智能修改" in without_delta.text and "虚高" not in without_delta.text, "无 delta 仍印虚高提示")
    with pdfplumber.open(BytesIO(with_delta.data)) as document:
        # 仅定位完整词「虚高」，避免把意见严重度「高」的正文字号混入提示。
        sizes = [char["size"] for page in document.pages for match in page.search("虚高") for char in match["chars"]]
    _expect(sizes and all(abs(size - 8) < .1 for size in sizes), "虚高提示不是 8pt 小字")
    return "传 delta：分数对比下方完整提示含虚高、8pt；不传 delta：智能修改无虚高提示"


def _uplift() -> UpliftPlan:
    """提供固定上游策略，包含可达、不可达及零潜力行，不让 PDF 计算分数。"""
    # 每项潜力与总数都是显式测试输入，便于发现渲染器偷偷重算或混入 blocked。
    rows = [dict(part="hard", item="必备技能：Go", current="未命中", potential=7,  # 可回链意见的硬性分。
                 action="补充真实 Go 使用场景", requirement="确有 Go 使用经历", blocked=False),
            dict(part="semantic", item="JD 职责：负责订单系统接口开发", current="未覆盖（cos=0.56）", potential=12,  # 小数必须连续可读。
                 action="说明真实职责与成果", requirement="确实承担过该职责", blocked=False),
            dict(part="hard", item="工作年限", current="年限不足", potential=0,  # blocked 的零值必须真实印 0。
                 action="等待真实年限积累或换岗位", requirement="实际年限达到要求", blocked=True),
            dict(part="llm", item="LLM 复核", current="22 / 30", potential=8,  # 非零 blocked 也不能进入合计。
                 action="后续重新评分核实", requirement="补充真实项目证据", blocked=True)]
    return UpliftPlan(items=[UpliftItem(**row) for row in rows], achievable_if_true=19,  # 独立固定期望为 7+12。
                      unreachable=["年限不足", "LLM 复核项本次改写无法改变"], ceiling_note="", notes=[],
                      meta={"model_calls": 0, "cost_yuan": 0})


def _learning() -> LearningPlan:
    """取真实材料库中的 Go 官方文档构造排版输入，不联网检查链接。"""
    materials, _ = load_materials()  # 链接、标题、分数和日期都来自实际材料库。
    material = next(row for row in materials if row.id == "go-official-doc")
    return LearningPlan(gaps=[LearningGap(gap="Go", why="JD 必备技能：Go", severity="high", materials=[material])],
                        unmatched=[], meta={"has_external": True})


def _zero_delta() -> ScoreDelta:
    """明确提供三个变化均为零的上游分数，不调用 M6 重算。"""
    # 测试只消费分数契约，未导入或调用 M6 的计算模块。
    return ScoreDelta(before_total=50, after_total_estimate=50,
                      before_parts={"hard": 33, "semantic": 7, "llm": 10},
                      after_parts={"hard": 33, "semantic": 7, "llm": 10},
                      delta_parts={"hard": 0, "semantic": 0, "llm": 0}, estimated_fields=["llm"],
                      covered_before=1, covered_after=1, notes=[])


def _pdf_signature(data: bytes) -> str:
    """对真实文本、字体、逐字坐标、底色、线条和链接做版面签名，忽略生成时间。"""
    with pdfplumber.open(BytesIO(data)) as pdf:
        # 字体或任何字形位置变化都会影响签名，不只比较章节文字是否存在。
        pages = [{"width": page.width, "height": page.height,
                  "chars": [(char["text"], char["fontname"], char["size"], char["x0"], char["x1"],
                             char["top"], char["bottom"], char.get("non_stroking_color")) for char in page.chars],
                  "rects": [(rect["x0"], rect["x1"], rect["top"], rect["bottom"], rect.get("non_stroking_color")) for rect in page.rects],
                  "lines": [(line["x0"], line["x1"], line["top"], line["bottom"]) for line in page.lines],
                  "links": page.hyperlinks} for page in pdf.pages]
    return hashlib.sha256(json.dumps(pages, sort_keys=True, ensure_ascii=True).encode()).hexdigest()  # 排除 PDF 随机 ID。


def _gray_contains(page, match: dict) -> bool:
    """核对指定文字落在实际绘制的灰底矩形中，不只读取内存样式。"""
    for rect in page.rects:
        color = rect.get("non_stroking_color")  # 使用实际填充色，不能拿描边色冒充灰底。
        if (isinstance(color, (tuple, list)) and len(color) == 3
                and all(abs(a - b) < .01 for a, b in zip(color, (242 / 255, 244 / 255, 247 / 255)))
                and rect["x0"] <= match["x0"] and rect["x1"] >= match["x1"]
                and rect["top"] <= match["top"] and rect["bottom"] >= match["bottom"]):
            return True
    return False


def _check_j1() -> str:
    """J1：两章顺序、原样解释、blocked 灰底、原值合计与每页页脚均读回。"""
    report, diagnosis, hard = _inputs()
    item = _generate("J1-both", report, diagnosis, hard, learning_plan=_learning(), uplift_plan=_uplift())  # 同时传两章。
    text = _compact(item.text)
    _expect(text.count("8能力补强建议") == text.count("9涨分策略") == 1, "章节缺失或重复章号")  # 禁止两个第 8 章。
    _expect(text.index("8能力补强建议") < text.index("9涨分策略"), "8/9 章节顺序错误")
    # 独立读取 PRD 原文；嵌套中文引号不能导致只截到半句解释。
    prd = (PROJECT_ROOT / "prd/PRD_简历修改意见_v1.md").read_text(encoding="utf-8")
    match = re.search(r"「(本次改写只优化表达，.*?想涨分，做下面这几件事。)」", prd, re.S)
    _expect(match is not None, "找不到 PRD §7.6 原文")
    exact = "".join(line.strip() for line in match.group(1).splitlines())  # 只去 Markdown 排版换行。
    _expect(revision.UPLIFT_INTRO == exact and _compact(exact) in text, "第 9 章解释没有原样印出")
    _expect("合计：若上面每条前提都成立，最多可加19分；这只是上限，不作为承诺" in text, "合计或承诺边界错误")  # 不接受误加 blocked。
    _expect("工作年限年限不足0" in text, "blocked 零潜力没有印 0")
    _expect("靠改简历拿不到的分数：年限不足、LLM复核项本次改写无法改变" in text, "不可达说明漏项")
    with pdfplumber.open(BytesIO(item.data)) as document:
        blocked = []  # 两条 blocked 行都要落在实际灰底区域内。
        _expect(any(page.search(r"0\.56") for page in document.pages), "当前值的小数被拆到两行")
        for page in document.pages:
            _expect("本报告由AI生成" in _compact(page.extract_text() or ""), "新章节页缺页脚")
            _expect(all(0 <= char["x0"] <= char["x1"] <= page.width + 1 and char["bottom"] <= page.height
                        for char in page.chars), "新章节文字超出页面")
            blocked.extend(_gray_contains(page, found) for found in page.search("（改简历拿不到）"))
        _expect(len(blocked) == 2 and all(blocked), "blocked 行没有实际灰底或动作前缀")
    # 长表必须真实跨页，最后一条策略与两句结论不能分到不同页。
    plan = _uplift()
    plan.items = [plan.items[index % len(plan.items)].model_copy(update={"item": f"TAIL-ROW-{index:02}"})
                  for index in range(32)]
    paginated = _generate("J1-tail-together", report, diagnosis, hard, delta=_zero_delta(), uplift_plan=plan)
    with pdfplumber.open(BytesIO(paginated.data)) as document:
        # 只比较最终 PDF 中的位置，不以 KeepTogether 的存在替代实际分页验收。
        texts = [_compact(page.extract_text() or "") for page in document.pages]
        positions = [[index for index, text in enumerate(texts) if marker in text]
                     for marker in ("TAIL-ROW-31", "靠改简历拿不到的分数：", "合计：若上面每条前提都成立")]
        _expect(paginated.pages >= 3 and positions[0] == positions[1] == positions[2] and len(positions[0]) == 1,
                "表格末行与结论分离或结论孤页")
        # 前面的行仍能自动分页，且表头在最后一页正常重复。
        _expect("分数项当前最多可加该做什么前提" in texts[positions[0][0]], "跨页末表缺少重复表头")
    return f"{item.pages} 页 / {len(item.data)} 字节 / {item.elapsed_ms:.2f}ms；8→9→页脚、解释原文、灰底和合计 19 正确"


def _check_j2() -> str:
    """J2：None 路径保留旧正文，仅两份分数对比基线采用本轮授权的四列修复。"""
    report, diagnosis, hard = _inputs()
    report.issues[0].score_impact = 7  # None 路径即使收到非零派生值，也保持 M7 原卡片。
    item = _generate("M7-zero-positive", report, diagnosis, hard, delta=_zero_delta(),
                     learning_plan=None, uplift_plan=None)
    _expect("涨分策略" not in item.text and "能力补强建议" not in item.text
            and "本次改动不影响分数" not in item.text and "预计分数影响" not in item.text, "None 仍改变 M7 内容")
    baseline = json.loads((PROJECT_ROOT / "output/m8b_m9b/m7_baseline.json").read_text(encoding="utf-8"))  # 来自修改前。
    seen = set()
    # D-M9b-01 明确要求修改分数对比表，其他十一份版面签名继续严格相等。
    four_column = {"K1-delta", "M7-zero-positive"}
    for artifact in ARTIFACTS:
        if artifact.name in baseline:
            # 修改前已在独立进程生成签名，不用当前渲染器给自己生成期望。
            same = _pdf_signature(artifact.data) == baseline[artifact.name]["signature"]
            _expect(not same if artifact.name in four_column else same, f"M7 授权变更边界错误：{artifact.name}")
            seen.add(artifact.name)
    _expect(seen == set(baseline), "原 M7 版面基线没有全部回归")  # 禁止只比对一个正常样张。
    return f"两计划为 None；{len(seen) - len(four_column)} 份 M7 版面逐项一致，2 份仅授权修复分数四列表；两章均未出现"


def _check_k2() -> str:
    """D-M9b-01：独立核对四列表头、列宽和单值数格，禁止用 br 拼接名称与数字。"""
    delta = _zero_delta()
    styles = pdf_kit._styles(pdf_font.get_cjk_font())
    table = revision._score_comparison(delta, styles)[1]
    # 检查真实参与 PDF 排版的单元格对象，不能只 grep 一个正确表头字符串。
    _expect([cell.getPlainText() for cell in table._cellvalues[0]] == ["项目", "改前", "改后预估", "变化"], "分数表不是四列表头")
    _expect(all(abs(actual / pdf_kit.CONTENT_WIDTH * 174 - expected) < .01
                for actual, expected in zip(table._colWidths, [70, 32, 40, 32])), "四列表宽不符")
    expected = [["50", "50", "0"], ["33", "33", "0"], ["7", "7", "0"], ["10", "10", "0"]]
    _expect([[cell.getPlainText() for cell in row[1:]] for row in table._cellvalues[1:]] == expected, "项名与数值仍混在同格")
    _expect(all("<br" not in cell.text.lower() for row in table._cellvalues for cell in row), "分数表单元格含 br 换行")
    report, diagnosis, hard = _inputs()
    item = _generate("K2-four-columns", report, diagnosis, hard, delta=delta)
    _expect("项目改前改后预估变化" in _compact(item.text), "实际 PDF 未读回四列表头")
    return f"{item.pages} 页 / {len(item.data)} 字节；项目/改前/改后预估/变化，宽度 70/32/40/32，数值单格且无 br"


def _check_j3() -> str:
    """J3：零变化解释只随第 9 章启用，非零变化不印该解释。"""
    report, diagnosis, hard = _inputs()
    zero = _generate("J3-zero", report, diagnosis, hard, delta=_zero_delta(), uplift_plan=_uplift())
    changed = _zero_delta().model_copy(update={"after_total_estimate": 55,  # 负例只改变语义项，不能误印全零说明。
        "after_parts": {"hard": 33, "semantic": 12, "llm": 10}, "delta_parts": {"hard": 0, "semantic": 5, "llm": 0}})
    nonzero = _generate("J3-nonzero", report, diagnosis, hard, delta=changed, uplift_plan=_uplift())
    text = _compact(zero.text)  # 允许 PDF 自动换行，不容许解释缺字。
    _expect(_compact(revision.ZERO_DELTA_NOTE) in text and "见第9章" in text, "零变化缺完整解释或章节指引")
    _expect(text.index("分数对比") < text.index("本次改动不影响分数") < text.index("9涨分策略"), "动态解释位置错误")
    _expect("本次改动不影响分数" not in nonzero.text, "非零变化误印零变化解释")
    return f"{zero.pages} 页 / {len(zero.data)} 字节；零变化指向第 9 章，非零变化无此解释，M6 输入未改"


def _check_k3() -> str:
    """D-M9b-02：两句完整提示分别占一行，相邻有序，真实字形边界不重叠。"""
    report, diagnosis, hard = _inputs()
    delta = _zero_delta().model_copy(update={"notes": ["硬性 0：词句改写不影响年限/学历", "语义 0：覆盖条数未变"]})
    item = _generate("K3-distinct-warnings", report, diagnosis, hard, delta=delta, uplift_plan=_uplift())
    # 仅显示层补「变化」，原始 ScoreDelta 在 _generate 的输入快照中保持逐字不变。
    _expect("硬性变化0：" in _compact(item.text) and "语义变化0：" in _compact(item.text), "PDF仍把变化0显示成得分0")
    expected = ["语义分对 JD 原句高度敏感：若只是把 JD 措辞搬进简历，分数会虚高，面试时无法自证",
                revision.ZERO_DELTA_NOTE]
    with pdfplumber.open(BytesIO(item.data)) as document:
        lines = [(page_index, line_index, line) for page_index, page in enumerate(document.pages)
                 for line_index, line in enumerate(page.extract_text_lines())]
        # 逐整行比较，若提示混成一行、被遮盖或删除，均不能命中两条完整期望。
        found = [[row for row in lines if _compact(row[2]["text"]) == _compact(text)] for text in expected]
        _expect(all(len(rows) == 1 for rows in found), "两句提示没有各占独立完整的一行")
        first, second = found[0][0], found[1][0]
        _expect(first[0] == second[0] and second[1] == first[1] + 1, "提示未同页顺序相邻")
        _expect(first[2]["bottom"] < second[2]["top"], "两句提示的字形边界重叠")
        # 原 8pt/11pt 行距保持，第二句基线下移 11+2pt，证明 2pt 间隔实际参与排版。
        _expect(abs(second[2]["top"] - first[2]["top"] - 13) < .1, "两提示之间没有真实2pt间隔")
    return f"{item.pages} 页 / {len(item.data)} 字节；两句全文各占一行、同页相邻有序、字形不重叠、间隔2pt"


def _check_k4() -> str:
    """空 adaptations 不渲染原文/改后小表表头，有真实改写时正常显示。"""
    report, diagnosis, hard = _inputs()
    for name, delta in (("K4-empty-delta", _zero_delta()), ("K4-empty-skipped", None)):
        # 同时覆盖有分数对比、只有未采用原因的两种智能修改入口。
        report.meta["rewrite_requested"] = True
        item = _generate(name, report, diagnosis, hard, delta=delta, uplift_plan=_uplift())
        area = item.text.split("智能修改", 1)[1].split("9 涨分策略", 1)[0]
        _expect(not re.search(r"(?:^|\s)(?:原文|改后)(?=\s|$)", area), "空改写仍印原文/改后小表表头")
        _expect("未采用意见" in area and "未采用标记不能混成缺技能" in area, "隐藏空表误删未采用原因")
        # 四列分数表仍然存在，不能把整个智能修改章节一起隐藏。
        if delta is not None:
            _expect("项目改前改后预估变化" in _compact(area), "空改写误删分数四列表")
    report.adaptations = [AdaptedText(issue_id="R-001", location="工作经历", before="真实改前", after="真实改后",
                                    added_terms=[], kept_numbers=[])]
    present = _generate("K4-with-adaptation", report, diagnosis, hard)
    _expect("意见原文改后" in _compact(present.text) and "真实改前" in present.text and "真实改后" in present.text,
            "有改写时对照表缺失")
    return "空改写两入口：不印原文/改后小表，保留四列分数表及未采用原因；非空时对照表照常显示"


def _check_j4() -> str:
    """J4：非空 ceiling_note 原样显示为表格上方灰底提示，空值不显示。"""
    report, diagnosis, hard = _inputs()
    note = "语义分已接近上限：JD 职责已全部覆盖"  # 提示本身是上游输入，PDF 不推测上限。
    plan = _uplift().model_copy(update={"items": [], "achievable_if_true": 0, "ceiling_note": note})
    item = _generate("J4-ceiling", report, diagnosis, hard, uplift_plan=plan)
    _expect(_compact(note) in _compact(item.text), "ceiling_note 缺失")
    _expect(_compact(item.text).index(_compact(note)) < _compact(item.text).index("分数项"), "上限提示未放在表格上方")
    with pdfplumber.open(BytesIO(item.data)) as document:
        found = [(page, match) for page in document.pages for match in page.search("语义分已接近上限")]  # 对实际字形查底色。
        _expect(found and all(_gray_contains(page, match) for page, match in found), "上限提示没有灰底")
    return f"{item.pages} 页 / {len(item.data)} 字节；上限提示灰底，空表格与合计 0 正确"


def _check_j5() -> str:
    """J5：意见卡片的零影响带明确说明，正影响取上游 7 分及对应 Go 技能。"""
    report, diagnosis, hard = _inputs()
    report.issues = [_issue(), _issue(id="R-002", category="missing", jd_evidence="Go", score_impact=7)]  # 两种影响分别覆盖。
    item = _generate("J5-impact", report, diagnosis, hard, uplift_plan=_uplift())
    text = _compact(item.text)
    _expect("预计分数影响：0（仅优化表达）" in text, "零分影响缺解释")  # 不接受只印一个数字 0。
    _expect("预计分数影响：+7（解除必备技能Go的扣分）" in text, "正分影响或技能依据错误")
    return f"{item.pages} 页 / {len(item.data)} 字节；卡片 0（仅优化表达）与 +7（解除 Go 扣分）均读回"


def _check_l1() -> str:
    """L1：八个有材料缺口分别保页，星标和人工核实说明可读，新章不复制简历原文。"""
    report, diagnosis, hard = _inputs()
    material = _learning().gaps[0].materials[0]  # 只改变用于定位的文案，不新造外部地址。
    tags = [f"BLOCK-{letter}" for letter in "ABCDEFGH"]  # 正好八个缺口，覆盖整章最大规模。
    plan = LearningPlan(gaps=[LearningGap(gap=f"Go {tag}", why=f"JD 要求 Go {tag}", severity="high",
        materials=[material.model_copy(update={"note": f"学习用途 {tag}"})]) for tag in tags], unmatched=[], meta={})
    item = _generate("L1-eight", report, diagnosis, hard, learning_plan=plan)
    with pdfplumber.open(BytesIO(item.data)) as document:
        texts = [_compact(page.extract_text() or "") for page in document.pages]  # 标记同时出现在缺口和材料末尾。
        for tag in tags:
            _expect(sum(tag in text for text in texts) == 1, f"学习缺口 {tag} 跨页")
    learning = _compact(item.text).split("8能力补强建议", 1)[1]  # 隐私断言只针对新学习章节。
    _expect("★★★★★" in learning and "材料评分来自本地人工标注（0-5）" in learning
            and "链接核实于2026-11-13；外部链接需联网" in learning, "星标或核实说明缺失")
    _expect(_compact(report.issues[0].original_text) not in learning, "学习章节复制了简历原文")
    return f"{item.pages} 页 / {len(item.data)} 字节；8 个缺口均不跨页，星标、核实日期及隐私边界正确"


def _check_l2() -> str:
    """L2：无材料缺口明确显示空态；空计划不编造核实日期。"""
    report, diagnosis, hard = _inputs()
    plan = LearningPlan(gaps=[LearningGap(gap="数控机床维修", why="JD 要求数控维修", severity="high", materials=[])],
                        unmatched=["数控机床维修"], meta={})
    item = _generate("L2-unmatched", report, diagnosis, hard, learning_plan=plan)
    _expect("暂无对应材料（材料库未收录）" in _compact(item.text), "无材料空态缺失")
    _expect("链接核实于未提供" in _compact(item.text), "无材料却编造核实日期")  # 空日期不能补当前日期。
    return f"{item.pages} 页 / {len(item.data)} 字节；无材料空态原样显示，不凑材料或核实日期"


def _check_l3() -> str:
    """L3：存在的本地教材以等宽字体显示完整路径及行号，PDF 没有链接注解。"""
    report, diagnosis, hard = _inputs()
    # 文件只在本用例存活，避免用不存在的文件冒充本地学习材料。
    with NamedTemporaryFile(prefix="m8b-check-", suffix=".md", dir=PROJECT_ROOT / "docs/教材") as temporary:
        locator = Path(temporary.name).relative_to(PROJECT_ROOT).as_posix() + "#L40"
        values = _learning().gaps[0].materials[0].model_dump()
        material = Material(**{**values, "kind": "local", "locator": locator, "title": "本地 Go 教材"})  # 经过真实路径校验。
        plan = LearningPlan(gaps=[LearningGap(gap="Go", why="JD 要求 Go", severity="high", materials=[material])], unmatched=[], meta={})
        item = _generate("L3-local", report, diagnosis, hard, learning_plan=plan)
    with pdfplumber.open(BytesIO(item.data)) as document:
        _expect(not any(page.hyperlinks for page in document.pages), "本地路径变成可点击链接")  # 从 PDF 注解层检查。
        codes = [char for page in document.pages for found in page.search("docs/") for char in found["chars"]]
        _expect(codes and all("SimSun" in char["fontname"] for char in codes), "本地路径不是中文等宽字体")
    _expect(_compact(locator) in _compact(item.text) and "本地材料" in item.text, "本地完整路径或类型缺失")
    font = pdf_font.get_monospace_font()  # 字体名检查之外，再验证实际字宽。
    _expect(pdfmetrics.stringWidth("iiii", font, 8) == pdfmetrics.stringWidth("WWWW", font, 8), "等宽度量不成立")
    return f"{item.pages} 页 / {len(item.data)} 字节；真实本地文件等宽显示，#L40 保留，链接注解 0"


def _check_l4() -> str:
    """L4：外链有真实 URI 注解和联网标记，长标题截为六十字，URL 不占正文。"""
    report, diagnosis, hard = _inputs()
    plan = _learning()
    material = plan.gaps[0].materials[0].model_copy(update={"title": "Go 官方文档 " + "标题长度测试" * 11})  # 专门覆盖长标题边界。
    plan.gaps[0].materials = [material]
    item = _generate("L4-external", report, diagnosis, hard, learning_plan=plan)
    with pdfplumber.open(BytesIO(item.data)) as document:
        links = [link for page in document.pages for link in page.hyperlinks]  # 读取真正的 URI 注解。
        _expect(len(links) == 1 and links[0]["uri"] == material.locator, "外链目标注解不正确")
    _expect("外部链接·需联网" in _compact(item.text) and material.locator not in item.text, "联网标记缺失或 URL 撑正文")
    _expect(_compact(material.title[:59] + "…") in _compact(item.text) and _compact(material.title) not in _compact(item.text), "长标题未限制为 60 字")
    return f"{item.pages} 页 / {len(item.data)} 字节；外链 URI 可点、需联网标记正确、标题 ≤60 字"


def main() -> int:
    """运行原十组、M9b 五组和 M8b 四组，保存样张并输出真实页数/字节。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    started = perf_counter()
    ARTIFACTS.clear()
    passed = 0
    groups = (("A 正常", _check_a), ("B 画像", _check_b), ("C 失败", _check_c),
              ("D 三态", _check_d), ("E ATS 排序", _check_e), ("F 不建议改", _check_f),
              ("G 每页页脚/分页", _check_g), ("H 字体", _check_h), ("I 规范", _check_i),
              ("K1 分数虚高提示", _check_k1), ("K2 D-M9b-01 四列表", _check_k2),
              ("K3 D-M9b-02 提示不重叠", _check_k3), ("K4 空改写隐藏小表", _check_k4),
              ("J1 涨分策略", _check_j1), ("J2 M7 None 回归", _check_j2),
              ("J3 零变化解释", _check_j3), ("J4 语义上限", _check_j4), ("J5 意见分数影响", _check_j5),
              ("L1 学习材料", _check_l1), ("L2 无材料空态", _check_l2),
              ("L3 本地不可点", _check_l3), ("L4 外链可点", _check_l4))
    with ExitStack() as stack:
        # socket 双重封锁；发生任何请求都直接失败，不能假装成离线成功。
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo"):
            stack.enter_context(patch(target, side_effect=AssertionError("M4 禁止联网")))
        # 每组失败继续后续检查，最终退出码由所有分组的真实结果决定。
        for label, action in groups:
            try:
                detail = action()
            except Exception as exc:
                print(f"{label}：失败；{type(exc).__name__}: {exc}")
            else:
                passed += 1
                print(f"{label}：通过；{detail}")
    # 稳定输出目录只写本任务样张，不删除或覆盖其他报告。
    output = PROJECT_ROOT / "output/pdf/m8b_m9b/checks"  # 新目录保留既有 M4/M6 产物。
    output.mkdir(parents=True, exist_ok=True)
    for item in ARTIFACTS:
        if item.name in ("A", "B", "C") or item.name.startswith(("J", "K3", "K4", "L")):
            # 写入验收实际读回过的同一份 bytes，不重新生成另一版交付物。
            (output / f"{item.name}.pdf").write_bytes(item.data)
    # 汇总耗时含所有独立读回；每个合成 PDF 的 300ms 门槛已单独检查。
    elapsed = (perf_counter() - started) * 1000
    sizes = "；".join(f"{item.name}={item.pages}页/{len(item.data)}字节" for item in ARTIFACTS if item.name in ("A", "B", "C"))
    print(f"通过 {passed}/{len(groups)}；耗时 {elapsed:.2f}ms；{sizes}；模型调用 0；成本 0 元")
    return 0 if passed == len(groups) else 1


if __name__ == "__main__":
    raise SystemExit(main())
