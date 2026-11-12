"""M4 A～I 合成验收：生成、读回和检查 PDF，全程禁止网络、模型与环境文件。"""

from __future__ import annotations

import ast
from contextlib import ExitStack
from dataclasses import dataclass
from io import BytesIO, StringIO
from pathlib import Path
import re
import sys
from time import perf_counter
import tokenize
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
from app.schemas_revision import AdaptedText, DiagnosisFinding, DiagnosisReport, JobProfile, KwCoverage, ReviewIssue, RevisionReport
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


def _generate(name: str, report: RevisionReport, diagnosis: DiagnosisReport, hard=None) -> Artifact:
    """计时生成并用 pdfplumber 实读字节，检查输入未被改动及合成渲染低于 300ms。"""
    # 比较整个序列化快照，能发现元数据或关键词被渲染器原地修改。
    before = report.model_dump_json(), diagnosis.model_dump_json(), repr(hard)
    started = perf_counter()
    data = revision.render_review_pdf(report, diagnosis, job_title=report.profile.title,
                                      resume_name="合成简历" + name, hard=hard)
    # 性能门槛只计渲染，不把独立 PDF 文本解析耗时混进来。
    elapsed = (perf_counter() - started) * 1000
    _expect(before == (report.model_dump_json(), diagnosis.model_dump_json(), repr(hard)), f"{name} 修改输入")
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
    (temporary / "M4_G_分页.pdf").write_bytes(item.data)
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


def main() -> int:
    """运行九组并保存三个稳定样张，输出每例真实页数/字节及汇总耗时。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    started = perf_counter()
    ARTIFACTS.clear()
    passed = 0
    groups = (("A 正常", _check_a), ("B 画像", _check_b), ("C 失败", _check_c),
              ("D 三态", _check_d), ("E ATS 排序", _check_e), ("F 不建议改", _check_f),
              ("G 每页页脚/分页", _check_g), ("H 字体", _check_h), ("I 规范", _check_i))
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
    output = PROJECT_ROOT / "output/pdf/m4"
    output.mkdir(parents=True, exist_ok=True)
    for item in ARTIFACTS:
        if item.name in ("A", "B", "C"):
            # 写入验收实际读回过的同一份 bytes，不重新生成另一版交付物。
            (output / f"M4_合成{item.name}.pdf").write_bytes(item.data)
    # 汇总耗时含所有独立读回；每个合成 PDF 的 300ms 门槛已单独检查。
    elapsed = (perf_counter() - started) * 1000
    sizes = "；".join(f"{item.name}={item.pages}页/{len(item.data)}字节" for item in ARTIFACTS if item.name in ("A", "B", "C"))
    print(f"通过 {passed}/{len(groups)}；耗时 {elapsed:.2f}ms；{sizes}；模型调用 0；成本 0 元")
    return 0 if passed == len(groups) else 1


if __name__ == "__main__":
    raise SystemExit(main())
