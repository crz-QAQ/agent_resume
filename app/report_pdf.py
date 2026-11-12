"""S7a：只将现成 MatchReport 排版成 PDF，不评分、不调用模型或接口。"""

from __future__ import annotations

from collections import defaultdict
from io import BytesIO
from math import isfinite

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import Flowable, Frame, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app import pdf_font
from app.pdf_kit import (
    BLUE, CONTENT_WIDTH, DISCLAIMER, INK, MARGIN, MUTED, PAGE_HEIGHT, PAGE_WIDTH, RULE,
    TRUNCATE_BY_WIDTH, _field, _footer, _limit_for_width, _local_time, _section, _styles, _table,
)
from app.schemas_match import Deduction, JobInfo, MatchReport

# A4 四边距均为18mm；页脚占用正文框底部的独立保留空间。
# 颜色只表达状态和档位，不参与任何数值运算。
# 缺字补画也使用这些颜色，保持表格判定与总分区一致。
# 判定是四态；JD 没要求的项目不另造对比行，仅在图例解释横线。
MARKS = {"covered": "✓", "uncovered": "✗", "unknown": "?", "not_required": "—"}
UNKNOWN_TYPES = {"years_unknown", "education_unknown"}
MISMATCH_TYPES = UNKNOWN_TYPES | {"years_short", "education"}
# PRD §4.5 v1.2.1：55mm 窄列塞120字会挤成8行，按列宽分档限制展示长度。


class _VerdictMark(Paragraph):
    """用同一中文字体保留符号文本，并为缺字的勾叉提供矢量外观。"""

    def __init__(self, symbol: str, style: ParagraphStyle):
        """建立可提取的四态符号段落，不加载其他符号字体。"""
        # 微软雅黑缺少两个勾叉码位；保留 Unicode 文本保证辅助读取不丢判定。
        self.symbol = symbol
        face = pdfmetrics.getFont(style.fontName).face
        self.vector = symbol in {"✓", "✗"} and ord(symbol) not in getattr(face, "charToGlyph", {})
        if self.vector:
            # 段落设置颜色时会覆盖画布透明度，因此透明度写在段落颜色自身。
            style = ParagraphStyle("invisible_mark", parent=style,
                                   textColor=colors.Color(0, 0, 0, alpha=0))
        super().__init__(symbol, style)

    def _unicode_subset(self) -> None:
        """为当前文档的缺字分配独立Unicode子集，避免被合并成NUL。"""
        font = pdfmetrics.getFont(self.style.fontName)
        if not hasattr(font, "splitString"):
            # CID字体直接编码Unicode，不经过TTFont的缺字到零码位映射。
            return
        # splitString初始化当前PDF的状态，不修改全局字体或其他报告的子集。
        font.splitString(" ", self.canv._doc)
        state = font.state[self.canv._doc]
        missing = [ord(symbol) for symbol in ("✓", "✗") if ord(symbol) not in state.assignments]
        if not missing:
            return
        # TTFont对缺字默认返回编码0；显式子集才能保留正确的ToUnicode。
        # 该子集的轮廓仍为不可见缺字，真正的勾叉轮廓由下面的路径呈现。
        subset_index = len(state.subsets)
        state.subsets.append([0, *missing])
        for offset, code in enumerate(missing, 1):
            state.assignments[code] = subset_index * 256 + offset
        # 后续正文从新子集继续分配，不能覆盖预留给勾叉的编码位置。
        state.nextCode = subset_index * 256 + len(missing) + 1

    def draw(self) -> None:
        """让 Paragraph 排符号文本，仅缺失的单字形用路径补画。"""
        if not self.vector:
            super().draw()
            return
        # 透明的原字符仍带 ToUnicode；不显示字体的缺字方框，也不换字体。
        self._unicode_subset()
        self.canv.saveState()
        super().draw()
        self.canv.restoreState()
        # 这里只画一个符号，不使用画布排正文或计算段落的页面坐标。
        canvas = self.canv
        canvas.saveState()
        canvas.setStrokeColor(BLUE if self.symbol == "✓" else colors.HexColor("#B42318"))
        canvas.setLineWidth(1.3)
        canvas.setLineCap(1)
        center = self.width / 2
        # 这些坐标仅描述十磅大小的图标自身，不是页面正文的行位置。
        path = canvas.beginPath()
        if self.symbol == "✓":
            path.moveTo(center - 4, 6)
            path.lineTo(center - 1, 3)
            path.lineTo(center + 4, 10)
        else:
            path.moveTo(center - 3, 3)
            path.lineTo(center + 3, 9)
            path.moveTo(center - 3, 9)
            path.lineTo(center + 3, 3)
        canvas.drawPath(path)
        canvas.restoreState()


class _ScoreBar(Flowable):
    """使用既有分项分数的比例绘制进度条，不计算新的业务分值。"""

    def __init__(self, score: int, maximum: int, width: float):
        """保存原有数值与显示尺寸；上限固定来自 PRD 评分项定义。"""
        super().__init__()
        self.score, self.maximum = score, maximum
        self.width, self.height = width, 12

    def draw(self) -> None:
        """只绘制进度条图形，标签和分数均由相邻 Paragraph 排版。"""
        # 比例运算仅决定图形宽度；实际展示数字始终直接读取 rep。
        self.canv.setFillColor(colors.HexColor("#E8EDF4"))
        self.canv.roundRect(0, 2, self.width, 7, 3.5, stroke=0, fill=1)
        self.canv.setFillColor(BLUE)
        # 输入范围在入口验证；无需对分值clamp，也不生成新的得分字段。
        # 零分只保留灰色底条，避免零宽圆角矩形在部分阅读器中出现尖角。
        if self.score:
            self.canv.roundRect(0, 2, self.width * self.score / self.maximum, 7, 3.5, stroke=0, fill=1)


def _comparison_rows(rep: MatchReport, styles: dict[str, ParagraphStyle]) -> list[list[object]]:
    """严格按任务书§三从不符项、缺失技能、职责明细依次拼装对比行。"""
    rows = []
    # 任务书明确第一组取 mismatches；不能再遍历 deductions 造成重复行。
    for item in rep.mismatches:
        if item.type in MISMATCH_TYPES:
            symbol = MARKS["unknown"] if item.type in UNKNOWN_TYPES else MARKS["uncovered"]
            rows.append([item.jd_evidence, _VerdictMark(symbol, styles["mark"]), item.resume_evidence])
    for item in rep.deductions:
        if item.type == "missing_skill":
            # 保留原始文本交给_table按所在列宽统一截断，避免预建段落绕过查表。
            label = f"必备技能：{item.item}"
            rows.append([label, _VerdictMark(MARKS["uncovered"], styles["mark"]), item.resume_evidence])
    for item in rep.semantic_items:
        # 覆盖状态只认上游bool，不能按显示成三位小数后的重排值再判断。
        symbol = MARKS["covered"] if item.covered else MARKS["uncovered"]
        rows.append([item.item, _VerdictMark(symbol, styles["mark"]), item.best_sentence])
    return rows


def _evidence(item: Deduction, styles: dict[str, ParagraphStyle]) -> Paragraph:
    """将缺口两侧证据各自截断后排成一条可换行的段落。"""
    # unknown 的分类由调用方决定，正文不擅自添加任何不合格结论。
    return Paragraph(f"• {_field(item.item)} —— JD：{_field(item.jd_evidence)} ｜ "
                     f"简历：{_field(item.resume_evidence)}", styles["body"])


def render_report(rep: MatchReport, job: JobInfo, *, resume_name: str = "简历") -> bytes:
    """把 MatchReport 渲染成 PDF 字节流；所有数字取自 rep，绝不自行计算。"""
    # 字体失败直接传播；本模块不维护第二条字体降级路径。
    font = pdf_font.get_cjk_font()
    # 每次调用使用独立的story与字节流，避免失败报告残留到下一次输出。
    styles = _styles(font)
    footer, footer_height = _footer(rep, styles)
    buffer = BytesIO()
    # 整页内容外边距18mm；正文底边在页脚上方，预留实测高度与间隔。
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN,
                            topMargin=MARGIN, bottomMargin=MARGIN + footer_height + 12,
                            title="简历岗位匹配报告", author="resume-agent", initialFontName=font)
    story = []
    # 九块正文按任务书顺序进入story，第十块页脚由页面模板重复添加。
    story.append(Paragraph("<b>简历岗位匹配报告</b>", styles["title"]))
    story.append(Paragraph(f"简历：{_field(resume_name)}　岗位：{_field(job.title or '未识别')}　"
                           f"生成：{_field(_local_time(rep.meta.get('generated_at')))}", styles["meta"]))
    # 告警紧跟抬头，并将失败/复核原因逐条提供给阅读者。
    if rep.status != "ok":
        failed = rep.status == "failed"
        message = "部分环节失败，分数仅供参考" if failed else "本报告存在待确认项，请人工复核"
        background = colors.HexColor("#FEE4E2" if failed else "#FEF0C7")
        # 告警用单列表格绘背景，告警文字仍是可换行Paragraph。
        alert = Table([[Paragraph(f"<b>{message}</b>", styles["body"])]],
                      colWidths=[CONTENT_WIDTH], repeatRows=1)
        alert.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), font),
                                  ("BACKGROUND", (0, 0), (-1, -1), background),
                                  ("TOPPADDING", (0, 0), (-1, -1), 8)]))
        story.append(alert)
        for reason in rep.reasons:
            story.append(Paragraph(_field(reason), styles["meta"]))
    story.append(Paragraph(f"状态：{_field(rep.status)}", styles["meta"]))
    # 档位文字直接读取 verdict；总分仅用于选择指定的显示颜色。
    total = rep.score.total
    # 阈值仅控制绿色、蓝色、橙色、红色四种视觉样式，verdict原文不变。
    color = "#167744" if total >= 85 else "#245CA6" if total >= 70 else "#B75D08" if total >= 55 else "#B42318"
    score = Table([[Paragraph(f"<b>{_field(total)}</b> <font size='12'>/ 100</font>", styles["score"]),
                    Paragraph(f"<font color='{color}'><b>{_field(rep.verdict)}</b></font>", styles["verdict"])]],
                  colWidths=[CONTENT_WIDTH * .6, CONTENT_WIDTH * .4], repeatRows=1)
    score.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), font), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story.append(score)
    bars = []
    for key, label, maximum in (("hard", "硬性", 40), ("semantic", "语义", 30), ("llm", "LLM", 30)):
        # 缺少分项时明确失败，不能用默认零分掩盖输入契约错误。
        value = rep.score.breakdown[key]
        if not isinstance(value, int) or not 0 <= value <= maximum:
            raise ValueError(f"报告分项 {key} 必须为0至{maximum}的整数")
        bars.append([Paragraph(label, styles["body"]), _ScoreBar(value, maximum, CONTENT_WIDTH * .65 - 12),
                     Paragraph(f"{value}/{maximum}", styles["body"])])
    bar_table = Table(bars, colWidths=[CONTENT_WIDTH * .16, CONTENT_WIDTH * .65, CONTENT_WIDTH * .19], repeatRows=1)
    # 每项的标签、图形、原始n/max同处一行，读者可同时读形状与数字。
    bar_table.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), font), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story.extend([bar_table, Spacer(1, 3)])
    # 扣分只展示原 hard deductions，不把语义缺口变成额外扣分。
    # 严重度仅作中文标签翻译，不能据此改变扣分数额。
    severity = {"high": "高", "medium": "中", "low": "低"}
    # PRD §4.5 v1.2.1：扣分加负号，零扣分显示横线；不修改rep中的原始数字。
    deductions = [[item.item, f"-{item.deduct}" if item.deduct > 0 else "—",
                   item.jd_evidence, item.resume_evidence, severity.get(item.severity, item.severity)]
                  for item in rep.deductions]
    _section(story, "扣分理由", [_table(["扣分项", "扣几分", "JD 依据", "简历现状", "严重度"],
             deductions, [30, 15, 110, 95, 20], styles, empty="无扣分项")], styles)
    semantic = []
    for item in rep.semantic_items:
        # -inf 为上游定义的缺测，不打印为分数0；有限重排值固定三位小数。
        # 余弦低于0.5时仅标灰，不能据此覆盖上游covered或删掉该职责。
        rerank = f"{item.rerank_score:.3f}" if isfinite(item.rerank_score) else "未提供"
        cosine = Paragraph(f"{item.best_sim:.3f}", styles["mutedcell" if item.best_sim < .5 else "cell"])
        symbol = MARKS["covered"] if item.covered else MARKS["uncovered"]
        semantic.append([item.item, cosine, rerank, _VerdictMark(symbol, styles["mark"]), item.best_sentence])
    _section(story, "语义覆盖明细", [_table(["JD 职责", "余弦", "重排", "覆盖", "命中的简历原句"],
             semantic, [55, 15, 15, 15, 170], styles, empty="无语义覆盖明细")], styles)
    grouped = defaultdict(list)
    # 字典保留输入中的类别顺序；同类关键字的次数从rep原样抄入。
    for keyword in rep.keywords:
        grouped[keyword.category].append(f"{_field(keyword.name)}({_field(keyword.count)})")
    keywords = [Paragraph(f"<b>{_field(category)}：</b>" + " · ".join(values), styles["body"])
                for category, values in grouped.items()]
    _section(story, "简历关键字", keywords or [Paragraph("无关键字", styles["body"])], styles)
    highlights = []
    # 六类主要项固定顺序，缺失或空值不补造未知内容。
    for label in ("姓名", "工作年限", "学历", "最近公司", "技术栈", "成果亮点"):
        # 成果亮点允许多行同标签，保留原报告的每条证据而不再次合并。
        highlights.extend([[item.label, item.value] for item in rep.highlights
                           if item.label == label and item.value.strip()])
    _section(story, "简历主要项", [_table(["标签", "值"], highlights, [30, 144], styles, empty="无主要项")], styles)
    comparison = _table(["JD 要求", "判定", "简历情况"], _comparison_rows(rep, styles),
                        [75, 15, 84], styles, empty="无可比项")
    # 图例覆盖四态，但JD未提项仍不生成多余的候选人判定行。
    legend = Table([[ _VerdictMark(MARKS[key], styles["mark"]), Paragraph(label, styles["cell"])]
                    for key, label in (("covered", "达标 / 已覆盖"), ("uncovered", "不符 / 未覆盖"),
                                       ("unknown", "无法确认"), ("not_required", "JD 未提该项要求"))],
                   colWidths=[15 * mm, CONTENT_WIDTH - 15 * mm], repeatRows=1)
    legend.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), font)]))
    _section(story, "对比表", [comparison, legend], styles)
    # unknown 单独分组：候选人没有披露信息并不等于不符合岗位。
    gaps = [Paragraph("<b>缺失</b>", styles["subheading"])]
    # 每侧证据分别截断；不让JD长度吞掉简历原句或反过来吞掉JD。
    gaps.extend([_evidence(item, styles) for item in rep.missing] or [Paragraph("无缺失项", styles["body"])])
    gaps.append(Paragraph("<b>不符或待确认</b>", styles["subheading"]))
    known = [item for item in rep.mismatches if item.type not in UNKNOWN_TYPES]
    unknown = [item for item in rep.mismatches if item.type in UNKNOWN_TYPES]
    # 没有unknown时不造一个空的待确认小节，也不把它们混入不符列表。
    gaps.append(Paragraph("<b>不符</b>", styles["subheading"]))
    gaps.extend([_evidence(item, styles) for item in known] or [Paragraph("无不符项", styles["body"])])
    if unknown:
        gaps.append(Paragraph("<b>待确认（需人工补充）</b>", styles["subheading"]))
        gaps.extend(_evidence(item, styles) for item in unknown)
    _section(story, "缺失与不符", gaps, styles)
    # 语义缺失与硬分不符分别展示，两者都不能再影响总分或分项进度条。
    suggestions = []
    # 编号只是展示顺序；最多十条来自PRD，与业务评分没有关系。
    for index, suggestion in enumerate(rep.suggestions[:10], 1):
        # 标签来自明确的前缀，不把未知来源擅自标为模型建议。
        label, text = "", suggestion
        for prefix, source in (("[LLM]", "模型建议"), ("[缺口]", "岗位缺口")):
            if suggestion.startswith(prefix):
                label, text = source, suggestion[len(prefix):].lstrip()
                break
        tag = f"<font size='8' color='#667085' backColor='#EEF0F3'> {label} </font> " if label else ""
        suggestions.append(Paragraph(f"{index}. {tag}{_field(text)}", styles["body"]))
    _section(story, "建议补充", suggestions or [Paragraph("无补充建议", styles["body"])], styles)

    def draw_footer(canvas, document) -> None:
        """在固定页脚区域排两段短文字，并为所有页面保留同样的正文空间。"""
        canvas.saveState()
        # 页脚也交给 Frame 顺排 Paragraph，不逐行手排页面 y 坐标。
        canvas.setStrokeColor(RULE)
        canvas.line(MARGIN, MARGIN + footer_height + 5, PAGE_WIDTH - MARGIN, MARGIN + footer_height + 5)
        frame = Frame(MARGIN, MARGIN, CONTENT_WIDTH, footer_height + 0.1,
                      leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        pending = list(footer)
        # Frame会消费列表，因此每页用浅拷贝，避免第二页失去免责与审计。
        frame.addFromList(pending, canvas)
        if pending:
            # 页脚放不下属于排版失败，不能悄悄丢弃审计或免责文字。
            raise ValueError("页脚超出预留区域")
        canvas.restoreState()

    doc.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    # 内存字节流使接口接入留给S7b，本函数没有路径或Web层依赖。
    return buffer.getvalue()
