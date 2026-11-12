"""共享 PDF 排版件；从评分报告原样抽取，不进行业务判断或模型调用。"""

from __future__ import annotations

from datetime import datetime
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Flowable, KeepTogether, Paragraph, Table, TableStyle

from app.schemas_match import MatchReport

# 页面、颜色与评分页脚文案保持原值，评分报告从这里导入同一份常量。
# 修改意见报告可替换免责段落，审计段落仍复用下面的 _footer。
MARGIN = 18 * mm
PAGE_WIDTH, PAGE_HEIGHT = A4
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN
DISCLAIMER = "本报告由 AI 生成，分数仅作初筛参考；关键结论请人工复核。"
INK = colors.HexColor("#223248")
MUTED = colors.HexColor("#667085")
RULE = colors.HexColor("#D9DFE7")
BLUE = colors.HexColor("#245CA6")
TRUNCATE_BY_WIDTH = {"narrow": 40, "medium": 80, "wide": 120}


def _limit_for_width(width_mm: float) -> int:
    """按PRD §4.5 v1.2.1完整区间将声明列宽（mm）映射为截断字数。"""
    # 区间连续覆盖小数宽度；用原始列宽分档，页面等比缩放只影响绘制尺寸。
    if width_mm <= 55:
        return TRUNCATE_BY_WIDTH["narrow"]
    if width_mm <= 120:
        return TRUNCATE_BY_WIDTH["medium"]
    return TRUNCATE_BY_WIDTH["wide"]


def _field(value: object, *, limit: int = TRUNCATE_BY_WIDTH["wide"]) -> str:
    """按字段所在列的上限截断加省略号，再转义并保留可见换行。"""
    # 先截原文再转义，避免把 &amp; 等实体截断成非法 Paragraph 标记。
    text = str(value) if value is not None else ""
    # PDF段落不保留NUL等控制字符，但允许原文正常换行和制表。
    text = "".join(char for char in text if char in "\n\t" or ord(char) >= 32)
    # 字段边界逐一处理，不能把拼接后的整段只截一次而丢失另一侧证据。
    if len(text) > limit:
        text = text[:limit] + "…"
    return escape(text).replace("\n", "<br/>")


def _local_time(value: object) -> str:
    """将报告给定的带时区时间转成本地时间，缺失或无时区时如实展示。"""
    # 不能用当前时间补造审计时间；历史报告必须仍显示原生成时间。
    if not value:
        return "未提供"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    # 无时区输入不猜 UTC，避免渲染器无依据地移动生成时间。
    if parsed.tzinfo is None:
        return f"{value}（未标时区）"
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def _styles(font: str) -> dict[str, ParagraphStyle]:
    """显式指定全部段落字体，避免继承第三方默认字体。"""
    # CJK 自动换行也能处理没有空格的中文证据；粗体仍走统一字体族。
    base = ParagraphStyle("body", fontName=font, fontSize=9, leading=14,
                          textColor=INK, wordWrap="CJK", spaceAfter=5)
    styles = {"body": base}
    # 每个派生样式均从显式设置了中文字体的 base 继承。
    settings = {
        "title": dict(fontSize=18, leading=25, spaceAfter=6),
        "meta": dict(fontSize=9, leading=14, textColor=MUTED, spaceAfter=8),
        "heading": dict(fontSize=12, leading=18, spaceBefore=12, spaceAfter=6, keepWithNext=True),
        "subheading": dict(fontSize=9, leading=14, spaceBefore=6, keepWithNext=True),
        "cell": dict(fontSize=8, leading=12, spaceAfter=0),
        "headcell": dict(fontSize=8, leading=12, spaceAfter=0),
        "mutedcell": dict(fontSize=8, leading=12, spaceAfter=0, textColor=MUTED),
        "mark": dict(fontSize=11, leading=14, alignment=TA_CENTER, spaceAfter=0),
        "score": dict(fontSize=40, leading=48, spaceAfter=0),
        "verdict": dict(fontSize=16, leading=23, spaceAfter=0),
        "footer": dict(fontSize=8, leading=11, textColor=MUTED, spaceAfter=0),
    }
    for name, values in settings.items():
        # 不使用样式表的内置Normal，确保没有隐式的西文字体来源。
        styles[name] = ParagraphStyle(name, parent=base, **values)
    return styles


def _table(headers: list[str], rows: list[list[object]], widths: list[float],
           styles: dict[str, ParagraphStyle], *, empty: str) -> Table:
    """按固定列宽比例建立可自动分页、重复表头的证据表格。"""
    # 任务书两张五列表宽合计270mm，按原比例缩到A4可用的174mm。
    columns = [CONTENT_WIDTH * width / sum(widths) for width in widths]
    # 所有表格从同一列宽数组推导上限，新增列无需另写可能漏改的limits列表。
    limits = [_limit_for_width(width) for width in widths]
    # 每列内容都独立Paragraph化；Table本身只负责宽度、分页和表头重复。
    data = [[Paragraph(f"<b>{_field(header)}</b>", styles["headcell"]) for header in headers]]
    # 已是 Paragraph/Flowable 的内容保留样式，普通外部字段统一截断转义。
    # 分档仅限制展示长度，截断后的内容仍由Paragraph自动换行，不设置固定行高。
    for row in rows:
        data.append([cell if isinstance(cell, Flowable) else Paragraph(
            _field(cell, limit=limits[index]), styles["cell"])
            for index, cell in enumerate(row)])
    if not rows:
        # 空状态保留表头，在下一行合并全部列显示明确说明。
        data.append([Paragraph(empty, styles["cell"])] + [""] * (len(headers) - 1))
    table = Table(data, colWidths=columns, repeatRows=1, hAlign="LEFT")
    # 表头使用灰底；横线代替密集竖线，避免长证据换行后视觉拥挤。
    commands = [("FONTNAME", (0, 0), (-1, -1), styles["cell"].fontName),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF0F3")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LINEBELOW", (0, 0), (-1, -1), 0.35, RULE)]
    if not rows:
        commands.append(("SPAN", (0, 1), (-1, 1)))
    if headers[0] == "扣分项":
        # 扣分数字列保留原比例，只缩内边距使“扣几分”完整显示。
        commands.extend([("LEFTPADDING", (1, 0), (1, -1), 1),
                         ("RIGHTPADDING", (1, 0), (1, -1), 1)])
    if headers[0] == "JD 职责":
        # 窄数字列减少内边距并缩为7pt，保证负重排分仍能整行显示。
        for row in data[1:]:
            for column in (1, 2):
                if isinstance(row[column], Paragraph):
                    # 仅改变显示字号，保留原文字与低余弦灰色标记。
                    row[column] = Paragraph(row[column].text, ParagraphStyle(
                        "numeric", parent=row[column].style, fontSize=7))
        commands.extend([("LEFTPADDING", (1, 0), (3, -1), 1),
                         ("RIGHTPADDING", (1, 0), (3, -1), 1)])
    table.setStyle(TableStyle(commands))
    # 不指定固定行高，字段截断后剩余内容仍可完整自动换行。
    return table


def _section(story: list, title: str, content: list, styles: dict[str, ParagraphStyle]) -> None:
    """让标题至少与表头和首行保持相邻，同时保留长表格自动跨页能力。"""
    heading = Paragraph(f"<b>{title}</b>", styles["heading"])
    # KeepTogether 的 split 会释放长表格，keepWithNext 继续约束标题与首片。
    # 不设置固定章节高度或人工换页坐标，表格行高由实际证据段落决定。
    story.append(KeepTogether([heading, *content]))


def _footer(rep: MatchReport, styles: dict[str, ParagraphStyle]) -> tuple[list[Paragraph], float]:
    """预排免责及审计段落并测量空间，长元数据换行仍不会覆盖正文。"""
    # 成本保留报告的原数值精度，不按示例写死0.0011或擅自改为零。
    meta = rep.meta
    audit = (f"模型 {_field(meta.get('model', '未提供'))} · "
             f"prompt {_field(meta.get('prompt_version', '未提供'))} · "
             f"成本 ¥{_field(meta.get('cost_yuan', '未提供'))} · "
             f"耗时 {_field(meta.get('elapsed_ms', '未提供'))}ms · "
             f"生成于 {_field(_local_time(meta.get('generated_at')))}")
    lines = [Paragraph(DISCLAIMER, styles["footer"]), Paragraph(audit, styles["footer"])]
    # 审计字段也有120字上限；较长的模型名与版本仍可能自然换成多行。
    height = sum(line.wrap(CONTENT_WIDTH, PAGE_HEIGHT)[1] for line in lines)
    return lines, height
