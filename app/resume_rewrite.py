"""M5：只采纳现成建议，以内存字节生成保版式 Word 或单栏 PDF 及意见报告。"""

from __future__ import annotations

from io import BytesIO
from pathlib import PurePosixPath
import re
from time import perf_counter
from xml.sax.saxutils import escape
from zipfile import ZipFile

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from lxml import etree
from pydantic import BaseModel
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from app import guard_revision as guard, pdf_font
from app.pdf_kit import _local_time
from app.revision_pdf import render_review_pdf
from app.schemas_revision import AdaptedText, DiagnosisReport, RevisionReport


# 原因文字是报告与调用方共同使用的契约，不能在各分支自行润色。
MISSING = "需本人补充经历，系统不代写"
UNCHANGED = "需本人按真实月份/学历补全"
NOT_FOUND = "未在正文找到（可能在文本框/页眉/艺术字）"
ALREADY_CHANGED = "同一段落已有改动"
TRACKED = "文档含未接受的修订痕迹，请先在 Word 里接受/拒绝后再试"
# 只去任务书指定的分隔符；句号、大小写等差异不能被宽松匹配放过。
_NORMALIZE = re.compile(r"[\s\u3000·|/]+")


class RewriteOutcome(BaseModel):
    """返回独立文档字节及实际采纳、未采用记录，不持有源文档对象。"""

    # 序列化与落盘留给调用方；此处永远没有原文件路径。
    data: bytes
    # 仅表示实际写入，不能直接复制用户勾选列表。
    applied: list[dict]
    # 顺序与处理顺序一致，供调用者逐条显示失败原因。
    skipped: list[dict]


class ApplyOutcome(BaseModel):
    """承载同一次操作的意见 PDF 与格式跟随输入的修改版简历。"""

    # 两份都构建成功才返回，避免调用方误把一份文件当作完成。
    review_pdf: bytes
    # 修改版使用独立字节，原始 raw 仍由上传方保存。
    resume_bytes: bytes
    # 扩展名只使用两种受支持格式，调用方据此命名修改版。
    resume_ext: str
    applied: list[dict]
    skipped: list[dict]


def _normalize(text: str) -> str:
    """仅消除空白、全角空格与三种分隔符，返回整段比较键。"""
    # 不更改大小写，不删除其他标点，也不对 Unicode 字形做兼容归一。
    return _NORMALIZE.sub("", text)


def _manual_reason(item: AdaptedText) -> str | None:
    """识别不能代写的空建议和去空白后未变化的建议。"""
    # 此处只去空白，不能把有意义的标点调整误判为时间补全。
    if not item.after.strip():
        return MISSING
    if "".join(item.before.split()) == "".join(item.after.split()):
        return UNCHANGED
    return None


def _skip(item: AdaptedText, reason: str) -> dict:
    """为未采用建议创建新记录，保留意见编号和未改原句。"""
    # 每次创建新字典，后续报告回填不会改动 AdaptedText。
    return {"issue_id": item.issue_id, "before": item.before, "reason": reason}


def _applied(item: AdaptedText) -> dict:
    """为确实完成的修改创建最小前后对照记录。"""
    # 护栏审计由报告内完整的 AdaptedText 保存，此处只返回定位与对照。
    return {key: getattr(item, key) for key in ("issue_id", "location", "before", "after")}


def _table_paragraphs(table: Table, depth: int, seen: set) -> list:
    """按行、单元格顺序收集表格段落，合并单元格去重，最多访问三层表格。"""
    if depth > 3:
        # 第四层及以下的结构仍由最终全文 XML 计数保护，但不自动改动。
        return []
    paragraphs = []
    for row in table.rows:
        for cell in row.cells:
            # 合并单元格会被 rows/cells 多次包装，按底层 XML 身份去重。
            if cell._tc in seen:
                continue
            seen.add(cell._tc)
            # 单元格的直接段落先收集，嵌套表格随后按原有顺序递归。
            paragraphs.extend(cell.paragraphs)
            for nested in cell.tables:
                paragraphs.extend(_table_paragraphs(nested, depth + 1, seen))
    return paragraphs


def _structure(document) -> tuple[int, int, int]:
    """统计全文 XML 中的段落、表格与 section，包含超过可改深度的结构。"""
    # 不用 document.paragraphs 计数，避免漏掉单元格、嵌套表格和文本框。
    return tuple(len(document.element.xpath(f".//w:{tag}")) for tag in ("p", "tbl", "sectPr"))


def _replace_runs(paragraph, before: str, after: str) -> bool:
    """在保留所有 run 属性的前提下替换整段；不支持的复杂内容安全跳过。"""
    runs = paragraph.runs
    # 超链接、域或内容控件可能不属于直接 runs；不能只改其中一部分。
    if not runs or "".join(run.text for run in runs) != paragraph.text:
        return False
    # 空 run 可能携带图片或锚点，不能为了清空文本而破坏这些对象。
    textual = [run for run in runs if run.text]
    # run.text 会清空 run 子节点；含图片、域等非文本节点时不能冒险修改。
    text_tags = {qn(tag) for tag in ("w:rPr", "w:t", "w:tab", "w:br", "w:cr")}
    if any(child.tag not in text_tags for run in textual for child in run._r):
        return False
    # 单 run 精确命中时直接采用该 run；跨 run 时以第一个有正文的 run 为准。
    target = next((run for run in textual if run.text == before), None)
    if target is None:
        # 纯分隔符的 run 不作为字体来源，优先继承真正正文的属性。
        target = next((run for run in textual if _normalize(run.text)), None)
    if target is None:
        return False
    for run in textual:
        # 只写文字，不触及字体、字号、粗斜体、颜色、下划线与段落属性。
        run.text = after if run is target else ""
    return True


def rewrite_docx(raw: bytes, adaptations: list[AdaptedText]) -> RewriteOutcome:
    """以内存副本完成整段的 run 级替换；结构变化抛 ValueError，修订文档整份拒绝。"""
    # 在 python-docx 解析之前检查修订节点，也识别使用其他命名空间前缀的文件。
    with ZipFile(BytesIO(raw)) as archive:
        # 只读取主文档 XML，不打开任何外部关系或链接。
        root = etree.fromstring(archive.read("word/document.xml"))
    if any(node.tag in {qn("w:ins"), qn("w:del")} for node in root.iter()):
        return RewriteOutcome(data=raw, applied=[], skipped=[{"reason": TRACKED}])
    document = Document(BytesIO(raw))
    # 统计基线在任何 run 赋值之前取得，包含不可编辑的深层结构。
    counts = _structure(document)
    # 顺序固定为正文直接段落，再依次访问所有表格，不扫描页眉页脚。
    paragraphs = list(document.paragraphs)
    seen: set = set()
    for table in document.tables:
        paragraphs.extend(_table_paragraphs(table, 1, seen))
    # 保留原段落匹配键：第一次替换后，第二条仍能识别同段冲突。
    candidates = [(paragraph, _normalize(paragraph.text)) for paragraph in paragraphs]
    changed: set = set()
    applied, skipped = [], []
    for item in adaptations:
        # 人工补全原因优先，避免空原句被当作任意段落匹配。
        reason = _manual_reason(item)
        key = _normalize(item.before)
        if reason:
            skipped.append(_skip(item, reason))
            continue
        # 严格整段等值，不使用位置描述猜测，也不回退到子串或模糊搜索。
        matches = [paragraph for paragraph, original in candidates if key and original == key]
        if not matches:
            skipped.append(_skip(item, NOT_FOUND))
            continue
        # 重复原文只定位到顺序中的第一段，不能悄悄改到另一个相同段落。
        paragraph = matches[0]
        if paragraph._p in changed:
            skipped.append(_skip(item, ALREADY_CHANGED))
        elif not _replace_runs(paragraph, item.before, item.after):
            skipped.append(_skip(item, "段落含不支持的复杂内容，需本人在 Word 中修改"))
        else:
            # 只有 run 实际替换成功才登记；跳过不占用该段落的修改名额。
            changed.add(paragraph._p)
            applied.append(_applied(item))
    # 没有采纳时直接返回原字节，连 ZIP 元数据也不必重新打包。
    buffer = BytesIO()
    if applied:
        # 输出流独立于输入流，函数没有覆盖任何文件的能力。
        document.save(buffer)
    data = buffer.getvalue() if applied else raw
    try:
        # 同时核对内存树与保存后重读的结构，不能只检查修改前的对象。
        assert counts == _structure(document) == _structure(Document(BytesIO(data)))
    except AssertionError as exc:
        raise ValueError("改写改变了段落、表格或 section 数量") from exc
    return RewriteOutcome(data=data, applied=applied, skipped=skipped)


def _replace_pdf_text(text: str, adaptations: list[AdaptedText]) -> tuple[str, list, list]:
    """以原文本坐标逐字替换，每条只定位一次，避免后续建议命中新写入的文字。"""
    replacements, applied, skipped = [], [], []
    for item in adaptations:
        # PDF 路径坚持逐字查找，不使用 Word 的去分隔符规范化匹配。
        reason = _manual_reason(item)
        start = text.find(item.before) if item.before else -1
        end = start + len(item.before)
        if reason:
            skipped.append(_skip(item, reason))
        elif start < 0:
            skipped.append(_skip(item, NOT_FOUND))
        elif any(start < right and end > left for left, right, _ in replacements):
            skipped.append(_skip(item, ALREADY_CHANGED))
        else:
            # 坐标始终来自传入正文；不对规范化文本或新生成的 after 做匹配。
            replacements.append((start, end, item.after))
            applied.append(_applied(item))
    for start, end, after in sorted(replacements, reverse=True):
        # 从右向左写回，左侧原文坐标不会被前面替换的长度变化移动。
        text = text[:start] + after + text[end:]
    return text, applied, skipped


class _BoldParagraph(Paragraph):
    """以同一中文字体描边加粗，兼容统一字体族没有独立粗体字形的情况。"""

    def draw(self) -> None:
        """仅在本段落绘制范围启用填充加描边，不影响后续正文与页脚。"""
        self.canv.saveState()
        # 使用公开 TextObject 接口设置字形描边，字体仍由统一入口提供。
        state = self.canv.beginText()
        state.setTextRenderMode(2)
        self.canv.drawText(state)
        self.canv.setLineWidth(0.25)
        super().draw()
        # 恢复文本渲染状态，普通 10.5pt 正文不会继承标题描边。
        self.canv.restoreState()


def rewrite_pdf_from_text(text: str, adaptations: list[AdaptedText], *, job_title: str,
                          resume_name: str, meta: dict | None = None) -> bytes:
    """先逐字替换再生成 A4 单栏 PDF；只输出简历正文及审计小字，不添加修改说明。"""
    text, _, _ = _replace_pdf_text(text, adaptations)
    # 统一入口自行负责字体缓存与回退；这里不注册第二套中文字体。
    font = pdf_font.get_cjk_font()
    # 每一个样式显式继承中文字体，正文完整保留，不套用报告字段截断器。
    body = ParagraphStyle("resume-body", fontName=font, fontSize=10.5, leading=15, wordWrap="CJK")
    name = ParagraphStyle("resume-name", parent=body, fontSize=16, leading=22,
                          alignment=TA_CENTER, spaceAfter=8)
    heading = ParagraphStyle("resume-heading", parent=body, fontSize=12, leading=15,
                             spaceBefore=6, keepWithNext=True)
    bullet = ParagraphStyle("resume-bullet", parent=body, leftIndent=4 * mm)
    foot = ParagraphStyle("resume-audit", parent=body, fontSize=7, leading=10)
    # 缺失历史元数据时如实标明；不能用当前时间或假成本覆盖上游审计。
    audit = dict(meta or {})
    # 这些是已生成意见的审计信息，本模块只使用其中值，不触发生成过程。
    footer_text = (f"模型 {audit.get('model', '未提供')} · prompt {audit.get('prompt_version', '未提供')} · "
                   f"成本 ¥{audit.get('cost_yuan', 0)} · 耗时 {audit.get('elapsed_ms', '未提供')}ms · "
                   f"生成时间 {_local_time(audit.get('generated_at'))}")
    footer = Paragraph(escape(footer_text), foot)
    width, height = A4
    margin = 18 * mm
    # 先测量审计文字高度，长模型名或版本自动换行也不会覆盖正文。
    _, footer_height = footer.wrap(width - 2 * margin, height)
    buffer = BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=margin, rightMargin=margin,
                                 topMargin=margin, bottomMargin=margin + footer_height + 12,
                                 initialFontName=font, title=f"{resume_name} - {job_title}")
    story = []
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            # 原文本空行只转为空白间距，不补写新的简历内容。
            story.append(Spacer(1, 15))
            continue
        # 项目符号职责优先于关键字标题；正文中的技能词不会改变职责样式。
        is_heading = (not line.startswith("• ") and len(line) <= 40
                      and any(word in line for word in ("经历", "教育", "技能", "项目", "证书", "自我评价")))
        style = name if index == 0 else bullet if line.startswith("• ") else heading if is_heading else body
        kind = _BoldParagraph if style in (name, heading) else Paragraph
        # XML 转义防止技术文本中的 <、>、& 被解释为排版标签。
        story.append(kind(escape(line), style))
    if not story:
        # 空正文也要生成带真实审计的文件，不把空 story 当作零页成功。
        story.append(Spacer(1, 15))

    def draw_footer(canvas, doc) -> None:
        """在每一页预留区域绘制同一份小字审计，绝不混入简历正文。"""
        canvas.saveState()
        footer.drawOn(canvas, margin, margin)
        canvas.restoreState()

    document.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    return buffer.getvalue()


def apply_revisions(*, rep: RevisionReport, diagnosis: DiagnosisReport, raw: bytes,
                    filename: str, job_title: str, resume_text: str, hard=None,
                    selected_ids: list[str] | None = None) -> ApplyOutcome:
    """以勾选建议一次构建两份文件；所有回填只进入深拷贝，不修改传入报告。"""
    started = perf_counter()
    # Windows 或 POSIX 路径均只取文件名；拒绝不支持格式而不是猜测内容。
    resume_name = PurePosixPath(filename.replace("\\", "/")).name
    extension = PurePosixPath(resume_name).suffix.lower().lstrip(".")
    if extension not in {"docx", "pdf"}:
        raise ValueError("智能修改仅支持 docx 或 pdf")
    report = rep.model_copy(deep=True)
    diagnosis_copy = diagnosis.model_copy(deep=True)
    # None 使用自动选择，空列表代表用户明确一个也不选，两者不可混淆。
    selected = set(selected_ids) if selected_ids is not None else None
    # 白名单只使用已经随报告存储的岗位要求，不读取环境或外部岗位信息。
    source = "\n".join([report.profile.title, *report.profile.must_have, *report.profile.responsibilities])
    adaptations, skipped = [], []
    for issue in report.issues:
        if selected is not None and issue.id not in selected:
            # 未勾选项不参与查找与替换，也不误记成技术性跳过。
            continue
        item = AdaptedText(issue_id=issue.id, location=issue.location, before=issue.original_text,
                           after=issue.suggested_text, added_terms=[], kept_numbers=[])
        # 默认仅采纳有实质改写的条目；不能代写的项仍记录人工补全原因。
        reason = MISSING if issue.category == "missing" else _manual_reason(item)
        if reason:
            skipped.append(_skip(item, reason))
            continue
        valid, reason = guard.validate_adaptation(item, resume_text, source)
        if not valid:
            # 定位失败的用户文案统一，名词和数字违规则保留护栏给出的具体原因。
            skipped.append(_skip(item, NOT_FOUND if not guard.find_original(resume_text, item.before) else reason))
            continue
        # 重新从正文核对，不能相信外部填入的 added_terms / kept_numbers。
        item.added_terms = guard.new_tech_terms(item.after, resume_text, "")
        item.added_terms.extend(guard.new_proper_nouns(item.after, resume_text, ""))
        # 复用护栏的同数归一规则，XX 占位符不冒充真实数字。
        item.kept_numbers = list(dict.fromkeys(guard._number_tokens(item.after)))
        adaptations.append(item)
    if extension == "docx":
        rewritten = rewrite_docx(raw, adaptations)
    else:
        # 替换记录与 PDF 正文共享同一纯函数，查不到的建议不会假记为已改。
        _, applied, pdf_skipped = _replace_pdf_text(resume_text, adaptations)
        rewritten = RewriteOutcome(data=b"", applied=applied, skipped=pdf_skipped)
        rewritten.data = rewrite_pdf_from_text(resume_text, adaptations, job_title=job_title,
                                               resume_name=resume_name, meta=report.meta)
    # 只把真正写入的条目回填报告，找不到原文或同段冲突不能伪装成已修改。
    applied_ids = {row["issue_id"] for row in rewritten.applied}
    report.adaptations = [item for item in adaptations if item.issue_id in applied_ids]
    report.skipped = [*skipped, *rewritten.skipped]
    # 新增独立操作审计，不篡改 M3 历史模型费用或历史耗时。
    report.meta["rewrite_cost_yuan"] = 0
    report.meta["rewrite_elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
    # M4 负责展示重排提示与全部跳过时的原因，简历文件内不加入这些说明。
    report.meta["rewrite_requested"] = True
    report.meta["resume_ext"] = extension
    review_pdf = render_review_pdf(report, diagnosis_copy, job_title=job_title,
                                   resume_name=resume_name, hard=hard)
    return ApplyOutcome(review_pdf=review_pdf, resume_bytes=rewritten.data, resume_ext=extension,
                        applied=rewritten.applied, skipped=report.skipped)
