"""M4：只排版上游修改意见、诊断和硬性证据，不评分、不改写简历、不调用模型。"""

from __future__ import annotations

from collections import Counter
from io import BytesIO
import re
from typing import TYPE_CHECKING

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Frame, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app import pdf_font
from app.job_profile import profile_banner
from app.pdf_kit import (
    CONTENT_WIDTH, MARGIN, PAGE_HEIGHT, PAGE_WIDTH, RULE,
    _field, _footer, _limit_for_width, _local_time, _section, _styles, _table,
)
# 勾叉沿用评分报告已有的单字缺字补画器，避免建立第二份符号实现。
from app.report_pdf import _VerdictMark
from app.schemas_revision import DiagnosisReport, ReviewIssue, RevisionReport, ScoreDelta

if TYPE_CHECKING:
    # 类型提示不导入评分或模型模块，渲染入口只读取传入属性。
    from app.scoring_rules import HardScore


DISCLAIMER = "本报告由 AI 生成，所有建议须本人核对真实性后再使用"
# 两条人工指引来自 PRD §7.1，不由渲染阶段临时生成解释。
MISSING_GUIDANCE = "（不可代写：如确有该经历，请在技能栏补充并说明使用场景）"
UNCHANGED_GUIDANCE = "（此行需你补全：按实际在职月份/学历填写，系统不代拟）"
# 顺序仅用于摘要的展示优先级，不导入带模型客户端的 revision_llm。
CATEGORY_LABELS = {"missing": "缺失项", "weak_evidence": "表达太弱", "buried": "重点埋没",
                   "inconsistent": "前后不一致", "noise": "无关冗余"}
SEVERITY_LABELS = {"high": "高", "medium": "中", "low": "低"}


def _render_suggestion(issue: ReviewIssue) -> tuple[str, bool]:
    """返回建议栏文本及高亮标记；缺失和原样保留分别显示明确的人工动作。"""
    suggested = "".join(issue.suggested_text.split())
    # missing 即便带上游条件模板也只显示人工指引，不能变成已有经历。
    if not suggested or issue.category == "missing":
        return MISSING_GUIDANCE, False
    if suggested == "".join(issue.original_text.split()):
        # 仅空白变化也算原样保留，不能用排版差异伪装成改写。
        return UNCHANGED_GUIDANCE, False
    return issue.suggested_text, True


def _unchangeable_rows(diagnosis: DiagnosisReport, hard: HardScore | None) -> list[list[str]]:
    """按 PRD §7.2 从硬性扣分及指定诊断生成四列；没有证据就不补造一行。"""
    rows = []
    # 没有 hard 时仍保留诊断分支；两类来源互不替代。
    if hard is not None:
        for deduction in hard.deductions:
            # 不使用扣分金额推算年限差距，现状只取上游原证据。
            if deduction.type in ("years_short", "years_unknown"):
                rows.append(["年限", deduction.resume_evidence, "年限差距靠措辞改不动，需真实经历或换岗位",
                             "如实填写真实工作年限，或考虑要求适合的岗位"])
            elif deduction.type in ("education", "education_unknown"):
                rows.append(["学历", deduction.resume_evidence, "学历是硬门槛，可改的只有如实填写",
                             "核对并填写真实学历，不添加不存在的学位"])
    for finding in diagnosis.findings:
        # 这些 code 的处理是固定规则，不把其他写作问题扩大成硬性障碍。
        if finding.code in ("timeline_overlap", "timeline_invalid", "open_ended"):
            rows.append(["时间线", finding.evidence, "时间线矛盾需本人核实，改写会变成造假",
                         "核实真实起止年月与在职状态，不编造日期"])
        elif finding.code == "missing_section":
            # 联系方式、姓名缺项不冒充年限或学历硬门槛。
            item = "年限" if "年限" in finding.detail else "学历" if re.search(r"学历|教育", finding.detail) else None
            if item:
                rows.append([item, finding.detail, "缺项只能本人补，系统不代拟", f"本人核实并补充真实{item}"])
    # 相同证据只显示一次，保留硬分在前、诊断在后的原始顺序。
    return [list(row) for row in dict.fromkeys(tuple(row) for row in rows)]


def _banner(text: str, background: str, styles: dict) -> Table:
    """将来源或失败提示排成整行底色横幅，文本仍由 Paragraph 换行。"""
    # 背景只表达来源或失败状态，不参与任何意见计数。
    table = Table([[Paragraph(_field(text), styles["body"])]], colWidths=[CONTENT_WIDTH])
    table.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), styles["body"].fontName),
                              ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(background)),
                              ("LEFTPADDING", (0, 0), (-1, -1), 8),
                              ("TOPPADDING", (0, 0), (-1, -1), 7),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    return table


def _card(issue: ReviewIssue, styles: dict) -> Table:
    """建立一张意见卡片，调用方整体 KeepTogether，所有字段转义并按列宽截断。"""
    # 卡片内合并排版空白，防止原文大量换行把一张限长卡片撑到超过整页。
    # 只创建显示副本，原文的字词、数字及调用者的意见对象保持不变。
    fields = ("location", "original_text", "suggested_text", "why", "jd_evidence", "gain", "risk_note")
    issue = issue.model_copy(update={name: " ".join(getattr(issue, name).split()) for name in fields})
    width = CONTENT_WIDTH - 16
    # 外层左右各留八磅内边距，嵌套原文框使用剩余真实宽度。
    limit = _limit_for_width(width / mm)
    location_style = ParagraphStyle("location", parent=styles["mutedcell"], alignment=TA_RIGHT)
    original = Table([[Paragraph("<b>原文</b>", styles["cell"]),
                       Paragraph(_field(issue.location, limit=_limit_for_width(50)), location_style)],
                      [Paragraph(_field(issue.original_text, limit=limit), styles["body"]), ""]],
                     colWidths=[width - 50 * mm, 50 * mm])
    # 原文与位置共用灰底，位置靠右；嵌套表也显式指定同一中文字体。
    original.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), styles["body"].fontName),
                                 ("SPAN", (0, 1), (-1, 1)), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                 ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F2F4F7"))]))
    suggestion, highlighted = _render_suggestion(issue)
    # 分类与严重度只翻译已有枚举；未知值保留上游原文，不另起结论。
    title = f"{issue.id} · {CATEGORY_LABELS.get(issue.category, issue.category)} · {SEVERITY_LABELS.get(issue.severity, issue.severity)}"
    data = [[Paragraph(f"<b>{_field(title)}</b>", styles["body"])], [original],
            [Paragraph("<b>建议改为</b>", styles["cell"])],
            [Paragraph(_field(suggestion, limit=limit), styles["body"] if highlighted else styles["meta"])],
            [Paragraph("<b>为什么改：</b>" + _field(issue.why, limit=limit), styles["body"])],
            [Paragraph("JD 依据：" + _field(issue.jd_evidence, limit=limit), styles["body"])],
            [Paragraph("<b>改完的好处：</b>" + _field(issue.gain, limit=limit), styles["body"])],
            [Paragraph("风险提示：" + _field(issue.risk_note, limit=limit), styles["footer"])]]
    table = Table(data, colWidths=[CONTENT_WIDTH], hAlign="LEFT")
    # 标签、正文和风险提示保持独立段落，长文本按字体度量自然换行。
    commands = [("FONTNAME", (0, 0), (-1, -1), styles["body"].fontName),
                ("BOX", (0, 0), (-1, -1), .5, RULE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    if highlighted:
        # 人工指引不涂成可直接投递的改写，只有正常建议显示绿色高亮。
        commands.append(("BACKGROUND", (0, 3), (-1, 3), colors.HexColor("#ECFDF3")))
    table.setStyle(TableStyle(commands))
    # 每个字段最多宽列上限 120 字，整张卡片能放进空白页，外层不会退化拆行。
    return table


def _keyword_rows(diagnosis: DiagnosisReport, styles: dict) -> list[list[object]]:
    """按缺失在前、次数升序稳定排序，构造至多三十行的关键词覆盖表。"""
    coverage = sorted(diagnosis.keyword_coverage, key=lambda row: (row.count_in_resume > 0, row.count_in_resume))
    # Python 稳定排序保持同次数关键词的上游顺序。
    rows = []
    for item in coverage[:30]:
        covered = item.count_in_resume > 0
        # 补画器只负责单个符号，状态文字与出现次数均来自这一行的既有数据。
        state = Table([[_VerdictMark("✓" if covered else "✗", styles["mark"]),
                        Paragraph("已覆盖" if covered else "缺失", styles["cell"])]], colWidths=[5 * mm, 18 * mm])
        state.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), styles["cell"].fontName),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                   ("TOPPADDING", (0, 0), (-1, -1), 0),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        rows.append([item.term, item.count_in_resume, "、".join(item.sections) or "—", state])
    # 不把出现次数重算为覆盖评分，ATS 这里只展示现成频次。
    return rows


def _score_comparison(delta: ScoreDelta, styles: dict) -> list:
    """只展示上游逐项分数、精确/预估标签与真实负差值，不重新计算评分。"""
    rows = []
    entries = [("总分 / 100", delta.before_total, delta.after_total_estimate)]
    # 分项与总分分开列，LLM 的沿用值不能混称为精确重算。
    entries.extend((label, delta.before_parts[key], delta.after_parts[key]) for key, label in
                   (("hard", "硬性 / 40（精确）"), ("semantic", "语义 / 30（精确）"),
                    ("llm", f'LLM / 30<font size="7">（预估 ±{delta.llm_tolerance}）</font>')))
    for label, before, after in entries:
        # 三个数值列在单元格内附带同一行项名，保留三列表头的清晰对比。
        rows.append([Paragraph(f"{label}<br/>{before}", styles["cell"]), after,
                     f"{after - before:+d}" if after != before else "0"])
    content = [Paragraph("<b>分数对比</b>", styles["body"]),
               _table(["改前", "改后预估", "变化"], rows, [78, 52, 44], styles, empty="—")]
    if delta.after_total_estimate < delta.before_total:
        # 警告必须真的使用红色字，不能只保留在不可见的审计数据里。
        warning = ParagraphStyle("delta_warning", parent=styles["body"], textColor=colors.HexColor("#B42318"))
        content.append(Paragraph("本次改写后预估分下降，建议只采用部分建议", warning))
    # 逐项解释直接展示既有 notes，不在 PDF 层另造原因或省略 LLM 误差。
    content.extend(Paragraph(_field(note, limit=1000), styles["meta"]) for note in delta.notes)
    return content


def render_review_pdf(rep: RevisionReport, diagnosis: DiagnosisReport, *, job_title: str,
                      resume_name: str = "简历", hard: HardScore | None = None,
                      generated_at: str | None = None, delta: ScoreDelta | None = None) -> bytes:
    """将现成意见按 PRD §七排版为 PDF 字节，不修改输入，不计算分数或补造生成时间。"""
    font = pdf_font.get_cjk_font()
    # 字体不可用直接抛出，不能吞错后输出不可读的报告。
    styles = _styles(font)
    # 生成时间只用调用者或 meta 提供的值，不用当前时间替历史报告造审计信息。
    when = generated_at if generated_at is not None else rep.meta.get("generated_at")
    footer_rep = rep.model_copy(update={"meta": {**rep.meta, "generated_at": when}})
    # 浅复制只替换 meta 副本，不写回调用者的历史报告。
    footer, _ = _footer(footer_rep, styles)
    # 只替换修改报告的免责，公共审计段落原样复用；重新测量实际页脚高度。
    footer[0] = Paragraph(DISCLAIMER, styles["footer"])
    footer_height = sum(line.wrap(CONTENT_WIDTH, PAGE_HEIGHT)[1] for line in footer)
    # 正文底边采用实际页脚高度，长模型名或版本换行也不会与正文重叠。
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN,
                            topMargin=MARGIN, bottomMargin=MARGIN + footer_height + 12,
                            title="简历修改意见报告", author="resume-agent", initialFontName=font)
    story = [Paragraph("<b>简历修改意见报告</b>", styles["title"]),
             Paragraph(f"简历：{_field(resume_name)}　岗位：{_field(job_title)}<br/>"
                       f"生成时间：{_field(_local_time(when))}", styles["meta"])]
    inferred = rep.profile.source == "inferred"
    # 来源横幅只认已存储的 profile.source，用户给 JD 时不显示推测免责声明。
    story.append(_banner(profile_banner(rep.profile) if inferred else "依据：用户提供的真实 JD",
                         "#FEE4E2" if inferred else "#ECFDF3", styles))
    if rep.meta.get("rewrite_requested") and rep.meta.get("resume_ext") == "pdf":
        # M5 只传输入格式；重排说明属于意见报告，不能混入改后简历正文。
        story.append(_banner("原文件是 PDF，版式已重排；如需保留原版式请上传 Word 版", "#ECFDF3", styles))
    failed = rep.meta.get("status") == "failed"
    if failed:
        # 失败时不展示残留意见，但上游诊断、关键词和审计仍可读。
        story.append(_banner("模型不可用，本次报告不完整", "#FEE4E2", styles))
    issues = [] if failed else rep.issues
    counts = Counter(issue.severity for issue in issues)
    missing_count = sum(row.count_in_resume == 0 and row.from_must_have for row in diagnosis.keyword_coverage)
    # 摘要数字仅统计传入列表，不读取总分或估算提分字段。
    summary = [Paragraph(f"共 {len(issues)} 条建议，其中高 {counts['high']} 条；JD 必备项缺 {missing_count} 个", styles["body"]),
               Paragraph(f"高 {counts['high']} / 中 {counts['medium']} / 低 {counts['low']}；缺口 {len(diagnosis.missing_keywords)} 个", styles["body"])]
    priorities, severities = list(CATEGORY_LABELS), list(SEVERITY_LABELS)
    ranked = sorted(issues, key=lambda row: (severities.index(row.severity) if row.severity in severities else 3,
                                           priorities.index(row.category) if row.category in priorities else 5))
    # 摘要只列上游意见的前三条，不补写胜任评价、录用结论或提分预测。
    summary.extend(Paragraph(_field(f"{CATEGORY_LABELS.get(row.category, row.category)}：{row.original_text[:40]}"), styles["body"])
                   for row in ranked[:3])
    _section(story, "结论摘要", summary, styles)
    cards = [_card(issue, styles) for issue in issues]
    # 首卡交给 _section 的 KeepTogether，避免两层 KeepTogether 的虚拟高度触发空白换页。
    _section(story, "逐条意见", [cards[0], Spacer(1, 8)] if cards else [Paragraph("—", styles["body"])], styles)
    # 后续每张卡片分别成组，不把整个意见章节捆成一块。
    story.extend(KeepTogether([card, Spacer(1, 8)]) for card in cards[1:])
    gap_rows = [[row.original_text, row.why] for row in issues if row.category == "missing"]
    # skipped 是未采用修改，不等于缺技能，不能混入缺口清单。
    gap_rows.extend([term, "JD 关键词在简历未体现；仅在确有真实经历时补充"] for term in diagnosis.missing_keywords)
    gap_content = [Paragraph("以下为 JD 要求但简历未体现的项，系统不代写经历", styles["body"])]
    if gap_rows:
        # 即使只有诊断缺词、没有 missing 卡片，也要明确显示不可代写的边界。
        gap_content.append(Paragraph(MISSING_GUIDANCE, styles["meta"]))
    gap_content.append(_table(["缺什么", "为什么重要"], gap_rows, [70, 104], styles, empty="本次未列出缺口"))
    _section(story, "缺口清单", gap_content, styles)
    keyword_content = [_table(["关键词", "出现次数", "所在段落", "状态"], _keyword_rows(diagnosis, styles),
                              [45, 20, 75, 34], styles, empty="无关键词覆盖数据")]
    if len(diagnosis.keyword_coverage) > 30:
        # 截断只影响展示行数，不修改上游诊断或缺口列表。
        keyword_content.append(Paragraph("（仅显示前 30 个关键词）", styles["meta"]))
    _section(story, "4.5 ATS 关键词覆盖", keyword_content, styles)
    _section(story, "不建议改", [_table(["项", "现状", "为什么改不了", "你能做什么"],
             _unchangeable_rows(diagnosis, hard), [18, 52, 55, 49], styles,
             empty="本次没有发现改不动的问题")], styles)
    if delta is not None or rep.adaptations or (rep.meta.get("rewrite_requested") and rep.skipped):
        # 此处只展示未来上游给出的前后对照，不执行任何改写，也不生成改后简历。
        content = _score_comparison(delta, styles) if delta is not None else []
        content.append(_table(["意见", "原文", "改后"], [[item.issue_id, item.before, item.after] for item in rep.adaptations],
                              [24, 75, 75], styles, empty="—"))
        if rep.skipped:
            content.append(_table(["未采用意见", "说明"], [[row.get("issue_id", "—"), row.get("reason", "—")] for row in rep.skipped],
                                  [40, 134], styles, empty="—"))
        _section(story, "智能修改", content, styles)
    else:
        # 没有改写时仅显示占位，不制造一个空的智能修改章节。
        story.append(Paragraph("本次未生成智能修改内容。", styles["meta"]))

    def draw_footer(canvas, document) -> None:
        """每页在独立预留区域绘制完整免责和审计，文本仍使用 Paragraph/Frame。"""
        canvas.saveState()
        canvas.setStrokeColor(RULE)
        canvas.line(MARGIN, MARGIN + footer_height + 5, PAGE_WIDTH - MARGIN, MARGIN + footer_height + 5)
        frame = Frame(MARGIN, MARGIN, CONTENT_WIDTH, footer_height + .1,
                      leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        # Frame 会消费列表，必须每页复制，不能让后续页面丢失页脚。
        pending = list(footer)
        frame.addFromList(pending, canvas)
        if pending:
            # 空间不足要报错，不能让后续页脚无声缺失。
            raise ValueError("修改意见页脚超出预留区域")
        canvas.restoreState()

    doc.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    return buffer.getvalue()
