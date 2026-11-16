"""M4：只排版上游修改意见、诊断和硬性证据，不评分、不改写简历、不调用模型。"""

from __future__ import annotations

from collections import Counter
from io import BytesIO
import re
from typing import TYPE_CHECKING
from xml.sax.saxutils import quoteattr

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Flowable, Frame, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app import pdf_font
from app.job_profile import profile_banner
from app.pdf_kit import (
    CONTENT_WIDTH, MARGIN, PAGE_HEIGHT, PAGE_WIDTH, RULE,
    _field, _footer, _limit_for_width, _local_time, _section, _styles, _table,
)
# 勾叉沿用评分报告已有的单字缺字补画器，避免建立第二份符号实现。
from app.report_pdf import _VerdictMark
from app.schemas_revision import DiagnosisReport, ReviewIssue, RevisionReport, ScoreDelta, UpliftPlan

if TYPE_CHECKING:
    # 类型提示不导入评分或模型模块，渲染入口只读取传入属性。
    from app.scoring_rules import HardScore
    from app.learning_resources import LearningGap, LearningPlan


DISCLAIMER = "本报告由 AI 生成，所有建议须本人核对真实性后再使用"
# 两条人工指引来自 PRD §7.1，不由渲染阶段临时生成解释。
MISSING_GUIDANCE = "（不可代写：如确有该经历，请在技能栏补充并说明使用场景）"
UNCHANGED_GUIDANCE = "（此行需你补全：按实际在职月份/学历填写，系统不代拟）"
# 顺序仅用于摘要的展示优先级，不导入带模型客户端的 revision_llm。
CATEGORY_LABELS = {"missing": "缺失项", "weak_evidence": "表达太弱", "buried": "重点埋没",
                   "inconsistent": "前后不一致", "noise": "无关冗余"}
SEVERITY_LABELS = {"high": "高", "medium": "中", "low": "低"}
# PRD §7.6 原文逐字保存；渲染层只排版，不临时总结或改写解释。
UPLIFT_INTRO = ("本次改写只优化表达，分数基本不变 —— 因为硬性分由年限/学历/技能命中决定，"
                "语义分由「JD 职责是否被覆盖」决定，两者都不看措辞。想涨分，做下面这几件事。")
LEARNING_INTRO = "以下是你本次简历里没有体现、但 JD 明确要求的能力。改简历不能替代学能力，这里给出对应的学习材料。"
SEMANTIC_RECOMMENDATION_NOTE = "系统按语义相似推荐，请自行判断是否对得上"
ZERO_DELTA_NOTE = ("本次改动不影响分数：硬性看事实（年限/学历/技能命中），语义看覆盖，"
                   "两者都不看措辞 —— 具体怎么才能涨分，见第 9 章。")


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
                rows.append((deduction.type, ["年限", deduction.resume_evidence, "年限差距靠措辞改不动，需真实经历或换岗位",
                             "如实填写真实工作年限，或考虑要求适合的岗位"]))
            elif deduction.type in ("education", "education_unknown"):
                rows.append((deduction.type, ["学历", deduction.resume_evidence, "学历是硬门槛，可改的只有如实填写",
                             "核对并填写真实学历，不添加不存在的学位"]))
    for finding in diagnosis.findings:
        # 这些 code 的处理是固定规则，不把其他写作问题扩大成硬性障碍。
        if finding.code in ("timeline_overlap", "timeline_invalid", "open_ended"):
            rows.append((finding.code, ["时间线", finding.evidence, "时间线矛盾需本人核实，改写会变成造假",
                         "核实真实起止年月与在职状态，不编造日期"]))
        elif finding.code == "missing_section":
            # 联系方式、姓名缺项不冒充年限或学历硬门槛。
            item = "年限" if "年限" in finding.detail else "学历" if re.search(r"学历|教育", finding.detail) else None
            if item:
                rows.append((finding.code, [item, finding.detail, "缺项只能本人补，系统不代拟", f"本人核实并补充真实{item}"]))
    groups = {}
    for code, row in rows:
        # 同一 code 只合并相同清单项，missing_section 的年限与学历不能互相覆盖。
        group = groups.setdefault((code, row[0]), [])
        if row not in group:
            group.append(row)
    merged = []
    for (code, item), group in groups.items():
        # 使用新列表汇总展示，原始诊断和硬性扣分记录保持不变。
        row = list(group[0])
        if len(group) > 1:
            # 摘要只使用现成证据；优先提取原文日期范围，不推算月份或改动诊断。
            summaries = []
            for source in group:
                dates = re.findall(r"\d{4}[./年-]\d{1,2}(?:月)?\s*[-—~至]\s*(?:\d{4}[./年-]\d{1,2}(?:月)?|至今|今)", source[1])
                summaries.append("、".join(dates) if dates else " ".join(source[1].split())[:60])
            labels = {"timeline_overlap": "时间线矛盾", "timeline_invalid": "日期异常", "open_ended": "在职月份待核实"}
            label = labels.get(code, item + "信息")
            unit = "段经历" if item == "时间线" else "项"
            row[1] = f"{label}（{len(group)} {unit}）：" + "、".join(summaries)
        merged.append(row)
    # 首次出现顺序不变，单条证据保持原样，渲染层不新增评分或意见。
    return merged


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


def _impact_text(issue: ReviewIssue, plan: UpliftPlan) -> str:
    """展示上游分数影响；只有唯一策略项明确回链时才描述对应技能或职责。"""
    if issue.score_impact == 0:
        return "预计分数影响：0（仅优化表达）"
    # 分值直接读取派生字段，不在 PDF 中重新计算或认可模型自报潜力。
    matches = [item for item in plan.items if not item.blocked and item.potential == issue.score_impact
               and item.item in (f"必备技能：{issue.jd_evidence}", f"JD 职责：{issue.jd_evidence}")]
    reason = "需满足涨分策略中的真实前提"
    if len(matches) == 1:
        item = matches[0]
        reason = (f"解除必备技能 {item.item.removeprefix('必备技能：')} 的扣分" if item.part == "hard"
                  else f"补足 JD 职责覆盖：{item.item.removeprefix('JD 职责：')}")
    return f"预计分数影响：{issue.score_impact:+d}（{reason}）"


def _card(issue: ReviewIssue, styles: dict, *, uplift_plan: UpliftPlan | None = None) -> Table:
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
    if uplift_plan is not None:
        # 配套显示随第 9 章启用；两计划都为 None 时旧卡片行数和版面完全保留。
        data.append([Paragraph(_field(_impact_text(issue, uplift_plan), limit=240), styles["footer"])])
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


def _score_comparison(delta: ScoreDelta, styles: dict, *, llm_estimated: bool = False,
                      explain_zero: bool = False) -> list:
    """只展示上游逐项分数、精确/预估标签与真实负差值，不重新计算评分。"""
    rows = []
    entries = [("总分 / 100", delta.before_total, delta.after_total_estimate)]
    # 未取到原分时用明确的缺失标签，不能把降级零分写成正常预估。
    llm_label = ('LLM / 30<font size="7">：未取到（改前总分不含 LLM 项）</font>' if llm_estimated else
                 f'LLM / 30<font size="7">（预估 ±{delta.llm_tolerance}）</font>')
    # 分项与总分分开列，LLM 的沿用值不能混称为精确重算。
    entries.extend((label, delta.before_parts[key], delta.after_parts[key]) for key, label in
                   (("hard", "硬性 / 40（精确）"), ("semantic", "语义 / 30（精确）"),
                    ("llm", llm_label)))
    for label, before, after in entries:
        # 项目名称独立成列，三个数值格各放一个值，避免改前数字掉到第二行。
        rows.append([Paragraph(label, styles["cell"]), before, after,
                     f"{after - before:+d}" if after != before else "0"])
    content = [Paragraph("<b>分数对比</b>", styles["body"]),
               _table(["项目", "改前", "改后预估", "变化"], rows, [70, 32, 40, 32], styles, empty="—")]
    if delta.after_total_estimate < delta.before_total:
        # 警告必须真的使用红色字，不能只保留在不可见的审计数据里。
        warning = ParagraphStyle("delta_warning", parent=styles["body"], textColor=colors.HexColor("#B42318"))
        content.append(Paragraph("本次改写后预估分下降，建议只采用部分建议", warning))
    # 逐项解释直接展示既有 notes，不在 PDF 层另造原因或省略 LLM 误差。
    content.extend(Paragraph(_field(re.sub(r"^(硬性|语义)(?=\s*[+-]?\d)", r"\1变化", note), limit=1000),
                             styles["meta"]) for note in delta.notes)
    # 仅分数对比子块附带提示，沿用小字样式，不改变上游分数或其他报告内容。
    content.append(Paragraph("语义分对 JD 原句高度敏感：若只是把 JD 措辞搬进简历，分数会虚高，面试时无法自证",
                             styles["footer"]))
    if explain_zero and delta.delta_parts and all(value == 0 for value in delta.delta_parts.values()):
        # 只有第 9 章实际存在时才给出章节指引，且不改任何 M6 数字或解释。
        content.extend([Spacer(1, 2), Paragraph(ZERO_DELTA_NOTE, styles["footer"])])
    return content


def _learning_card(gap: LearningGap, styles: dict) -> Table:
    """将一个缺口及其现成材料排为浅灰底整块，外链可点、本地路径等宽不可点。"""
    content = [Paragraph(f"<b>{_field(gap.gap)}</b> · 严重度：{SEVERITY_LABELS.get(gap.severity, gap.severity)}", styles["body"]),
               Paragraph("为什么重要：" + _field(gap.why, limit=120), styles["cell"])]
    for material in gap.materials:
        # 标题最多六十字，完整 URL 只进入链接目标，不占据页面列宽。
        title = material.title if len(material.title) <= 60 else material.title[:59] + "…"
        label = _field(title, limit=60)
        if material.kind == "external":
            label = f'<link href={quoteattr(material.locator)} color="#245CA6"><u>{label}</u></link>'
        kind = "外部链接 · 需联网" if material.kind == "external" else "本地材料"
        # 星标只表示库内人工分数；不以相关性、重排结果或评分公式改写 quality。
        stars = "★" * material.quality + "☆" * (5 - material.quality)
        content.append(Paragraph(f"{label} · {kind} · {stars} · 核实 {_field(material.verified_at)}", styles["cell"]))
        content.append(Paragraph(_field(material.note, limit=60), styles["mutedcell"]))
        if material.kind == "local":
            # 只有确实出现本地材料时才加载等宽字体，不影响 M7 字体和启动开销。
            code = ParagraphStyle("material_path", parent=styles["cell"], fontName=pdf_font.get_monospace_font())
            content.append(Paragraph(_field(material.locator, limit=500), code))
        if gap.meta.get("matched_by") == "semantic":
            content.append(Paragraph(SEMANTIC_RECOMMENDATION_NOTE, styles["mutedcell"]))
    if not gap.materials:
        content.append(Paragraph("暂无对应材料（材料库未收录）", styles["body"]))
    # 每个段落使用独立表格行，便于真实字体度量换行；整张表由调用方保页。
    table = Table([[paragraph] for paragraph in content], colWidths=[CONTENT_WIDTH], hAlign="LEFT")
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
                              ("BOX", (0, 0), (-1, -1), .4, RULE),
                              ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                              ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    return table


def _learning_section(story: list, plan: LearningPlan, styles: dict) -> None:
    """追加固定第 8 章，缺口各自保页，并如实展示材料核实日期范围。"""
    cards = [_learning_card(gap, styles) for gap in plan.gaps]
    first = [Paragraph(LEARNING_INTRO, styles["body"])]
    if cards:
        first.extend([cards[0], Spacer(1, 8)])
    else:
        first.append(Paragraph("本次未列出需要补强的能力缺口", styles["body"]))
    _section(story, "8 能力补强建议", first, styles)
    # 不把八个缺口整体捆绑，避免大块溢页；每个缺口本身不跨页。
    story.extend(KeepTogether([card, Spacer(1, 8)]) for card in cards[1:])
    dates = sorted({material.verified_at for gap in plan.gaps for material in gap.materials})
    verified = (dates[0] if dates[0] == dates[-1] else f"{dates[0]} 至 {dates[-1]}") if dates else "未提供"
    story.append(Paragraph(f"材料评分来自本地人工标注（0-5），链接核实于 {verified}；外部链接需联网", styles["footer"]))


class _TableWithConclusion(Flowable):
    """按行拆长表，始终把剩余表格的最后一行与结论保成同一块。"""

    def __init__(self, table: Table, conclusion: list) -> None:
        """复用原表的列宽、行样式与重复表头，结论仅接收已排版的段落。"""
        super().__init__()
        self.table, self.conclusion = table, conclusion
        self.group = KeepTogether([table, *conclusion])

    def wrap(self, width: float, height: float) -> tuple[float, float]:
        """以 KeepTogether 的实际测量高度参与外层章节排版，避免嵌套虚高。"""
        measured_width, _ = self.group.wrapOn(self.canv, width, height)
        self.width, self.height = measured_width, self.group._H
        return self.width, self.height

    def split(self, width: float, height: float) -> list:
        """最多拆到倒数第二行，剩余行继续与两句结论一起度量和分页。"""
        self.table.wrapOn(self.canv, width, height)
        # 即便表格本身放得下，也必须留下一行陪结论，不能把整表先画完。
        boundary = min(height, sum(self.table._rowHeights[:-1]) + .001)
        parts = self.table.splitOn(self.canv, width, boundary)
        if len(parts) == 2:
            return [parts[0], _TableWithConclusion(parts[1], self.conclusion)]
        # 表头和末行已不能再拆，交给文档换页后整块绘制。
        return []

    def draw(self) -> None:
        """仅在整块实际高度可容纳时绘制，未成功绘制的内容不能静默丢弃。"""
        content = [self.table, *self.conclusion]
        frame = Frame(0, 0, self.width, self.height + .1,
                      leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        frame.addFromList(content, self.canv)
        if content:
            raise ValueError("涨分策略末行与结论未完整排入页面")


def _uplift_section(story: list, plan: UpliftPlan, styles: dict) -> None:
    """追加固定第 9 章，只展示上游潜力、限制、前提与合计，不产生新分数。"""
    # 依 PRD 的两句分行，保持所有文字原样，避免中文逗号孤立到下一行。
    intro_style = ParagraphStyle("uplift_intro", parent=styles["body"], fontSize=8.5, leading=13)
    content = [Paragraph(UPLIFT_INTRO.replace("，语义分", "，<br/>语义分", 1), intro_style)]
    if plan.ceiling_note:
        content.append(_banner(plan.ceiling_note, "#F2F4F7", styles))
    rows = []
    for item in plan.items:
        # blocked 行的非零理论空间也照实显示，但不改变上游的可达合计。
        values = [item.item, item.current, str(item.potential) if item.potential == 0 else f"+{item.potential}",
                  ("（改简历拿不到）" if item.blocked else "") + item.action, item.requirement]
        rows.append([Paragraph(_field(value, limit=500), styles["cell"]) for value in values])
    table = _table(["分数项", "当前", "最多可加", "该做什么", "前提（必须真实）"], rows,
                   [32, 33, 17, 46, 46], styles, empty="本次未列出可计算的涨分策略")
    # 灰底设置到实际数据行，表头仍使用公共样式；长表允许自动分页并重复表头。
    table.setStyle(TableStyle([("BACKGROUND", (0, index), (-1, index), colors.HexColor("#F2F4F7"))
                               for index, item in enumerate(plan.items, 1) if item.blocked]))
    unreachable = "、".join(plan.unreachable) or "上游未列出"
    conclusion = [Paragraph("靠改简历拿不到的分数：" + _field(unreachable, limit=5000), styles["meta"])]
    # 合计直接取 M9a 的字段，不把 blocked 空间计入，也不在 PDF 再加总取整。
    conclusion.append(Paragraph(f"合计：若上面每条前提都成立，最多可加 {plan.achievable_if_true} 分；这只是上限，不作为承诺", styles["body"]))
    # 仅末行和结论不可拆，其余行沿用表格的自动跨页与表头重复。
    content.append(_TableWithConclusion(table, conclusion))
    _section(story, "9 涨分策略", content, styles)


def render_review_pdf(rep: RevisionReport, diagnosis: DiagnosisReport, *, job_title: str,
                      resume_name: str = "简历", hard: HardScore | None = None,
                      generated_at: str | None = None, delta: ScoreDelta | None = None,
                      learning_plan: LearningPlan | None = None,
                      uplift_plan: UpliftPlan | None = None) -> bytes:
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
    cards = [_card(issue, styles, uplift_plan=uplift_plan) for issue in issues]
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
    # 合并后的现状需要展示多个日期，独立 Paragraph 避免再次按单条短字段截断。
    unchangeable = [[row[0], Paragraph(_field(row[1], limit=500), styles["cell"]), *row[2:]]
                   for row in _unchangeable_rows(diagnosis, hard)]
    _section(story, "不建议改", [_table(["项", "现状", "为什么改不了", "你能做什么"],
             unchangeable, [18, 52, 55, 49], styles,
             empty="本次没有发现改不动的问题")], styles)
    if delta is not None or rep.adaptations or (rep.meta.get("rewrite_requested") and rep.skipped):
        # 此处只展示未来上游给出的前后对照，不执行任何改写，也不生成改后简历。
        content = _score_comparison(delta, styles, llm_estimated=rep.meta.get("llm_estimated") is True,
                                    explain_zero=uplift_plan is not None) if delta is not None else []
        if rep.adaptations:
            # 无实际改写时连对照表表头也不画，分数、未采用原因仍照常展示。
            content.append(_table(["意见", "原文", "改后"], [[item.issue_id, item.before, item.after] for item in rep.adaptations],
                                  [24, 75, 75], styles, empty="—"))
        if rep.skipped:
            content.append(_table(["未采用意见", "说明"], [[row.get("issue_id", "—"), row.get("reason", "—")] for row in rep.skipped],
                                  [40, 134], styles, empty="—"))
        _section(story, "智能修改", content, styles)
    else:
        # 没有改写时仅显示占位，不制造一个空的智能修改章节。
        story.append(Paragraph("本次未生成智能修改内容。", styles["meta"]))

    # 两个固定章号独立启用；先 8 后 9，逐页页脚继续由原有回调最后绘制。
    if learning_plan is not None:
        _learning_section(story, learning_plan, styles)
    if uplift_plan is not None:
        _uplift_section(story, uplift_plan, styles)

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
