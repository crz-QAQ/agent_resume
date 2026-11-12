"""M5 A～I 离线验收：真实文件、结构字体逐项比较、产物读回及源码红线检查。"""

from __future__ import annotations

import argparse
import ast
from contextlib import ExitStack
from hashlib import sha256
from io import BytesIO, StringIO
from pathlib import Path
import re
import sys
from time import perf_counter
import tokenize
from unittest.mock import patch
from zipfile import ZipFile

import pdfplumber
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.shared import Pt, RGBColor

# 从任何工作目录执行都能找到仓库，且验收不写源码目录的字节码。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.dont_write_bytecode = True
from app import resume_rewrite as rewrite
from app.file_parser import extract_text
from app.schemas_revision import AdaptedText, DiagnosisReport, JobProfile, ReviewIssue, RevisionReport


# 交付只允许写到显式指定的新目录；源文件始终只读。
FIXTURES = PROJECT_ROOT / "tools/test_files"
ARTIFACTS: dict[str, bytes] = {}
COMPARISON: list[str] = []
MEASUREMENTS: list[float] = []
STAMP = "2026-11-12T13:00:00+08:00"
# 固定时间用于核对历史审计，不使用验收运行时刻覆盖原报告。
# ARTIFACTS 仅保存实际生成的字节，比较表从重读后的文档提取。


def _expect(condition: object, message: str) -> None:
    """独立断言条件，失败抛 AssertionError，优化模式下仍执行。"""
    if not condition:
        # 非零退出码由 main 汇总，各组同时保留具体失败原因。
        raise AssertionError(message)


def _adapt(before: str, after: str, issue_id: str = "R-001") -> AdaptedText:
    """创建固定意见的改写记录，不生成或猜测任何候选经历。"""
    # 审计初始为空，编排函数必须自行核对，不能直接相信测试预填字段。
    return AdaptedText(issue_id=issue_id, location="工作经历 · 第 2 段", before=before,
                       after=after, added_terms=[], kept_numbers=[])


def _raw(document) -> bytes:
    """把合成文档保存到内存，永远不接收原文件路径。"""
    # 合成边界文档也只写独立内存，避免测试留下无关原件。
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _text(data: bytes) -> str:
    """实际用 pdfplumber 重开 PDF，返回全部页面的可读文本。"""
    with pdfplumber.open(BytesIO(data)) as document:
        # 必须解码真实 PDF，不能以构建 story 时的字符串代替读回检查。
        return "\n".join(page.extract_text() or "" for page in document.pages)


def _counts(document) -> tuple:
    """独立统计整个正文 XML 的结构，包含所有层级表格段落。"""
    # 验收独立统计，不调用被测模块自己的结构函数来证明它正确。
    return tuple(len(document.element.xpath(f".//w:{tag}")) for tag in ("p", "tbl", "sectPr"))


def _font_runs(paragraph) -> list:
    """逐 run 采样字体、字号、粗斜体、颜色与下划线，保留 None 继承语义。"""
    # 混合 run 的列表长度和每个位置都参与比较，不能只比第一项。
    return [(run.font.name, run.font.size, run.bold, run.italic,
             run.font.color.type, str(run.font.color.rgb), run.underline) for run in paragraph.runs]


def _page_setup(document) -> list:
    """采样所有 section 的页边距、页面尺寸、方向及页眉页脚距离。"""
    # 遍历全部 section，防止只保留首页设置而损坏后续页面方向。
    return [tuple(getattr(section, field) for field in (
        "left_margin", "right_margin", "top_margin", "bottom_margin", "page_width",
        "page_height", "orientation", "header_distance", "footer_distance")) for section in document.sections]


def _real_adaptation() -> tuple[bytes, AdaptedText, int]:
    """从真实 Word 中定位一条既有职责，只调整动词而不新增事实。"""
    raw = (FIXTURES / "go_backend_resume.docx").read_bytes()
    document = Document(BytesIO(raw))
    # 不依赖固定索引选择职责；找到后仍输出它的真实段落序号。
    index = next(index for index, paragraph in enumerate(document.paragraphs) if "负责订单系统接口开发" in paragraph.text)
    before = document.paragraphs[index].text
    # 只替换动作词，职责中的技术、公司、时间与数字都来自原件。
    return raw, _adapt(before, before.replace("负责订单系统接口开发", "完成订单系统接口开发", 1)), index


def _inputs(items: list[AdaptedText]) -> tuple[RevisionReport, DiagnosisReport]:
    """只构造现成意见、岗位与诊断输入，保留一个非零历史费用用于审计不变检查。"""
    # 后续用例只通过明确的字段更新构造反例，保持每组输入可追溯。
    issues = [ReviewIssue(id=item.issue_id, category="weak_evidence", severity="medium",
              location=item.location, original_text=item.before, suggested_text=item.after,
              why="岗位要求接口交付，原文仅列职责", gain="职责描述变成实际交付描述",
              jd_evidence="负责接口开发", risk_note="仅保留真实经历") for item in items]
    # 历史意见费用与本次改写费用分开，不能把模型历史成本伪造为零。
    report = RevisionReport(issues=issues, rejected_issues=[], profile=JobProfile(
        title="Go 后端工程师", source="user_jd", confirmed=True, must_have=["Go", "MySQL", "Redis"],
        responsibilities=["负责接口开发"], note="用户真实 JD"), score_before=None,
        score_after_estimate=None, adaptations=[], skipped=[], meta={"model": "M3历史模型",
        "prompt_version": "revision_v1.1", "cost_yuan": 0.001, "elapsed_ms": 19, "generated_at": STAMP})
    diagnosis = DiagnosisReport(findings=[], keyword_coverage=[], missing_keywords=[],
        completeness={"name": True, "years": True, "education": True, "skills": True, "contact": True}, meta={"cost_yuan": 0})
    # 诊断不调用解析、评分或生成入口，这里只需最小合法数据契约。
    return report, diagnosis


def _check_a1() -> str:
    """A1：真实 Word 修改一条，其余文字、全部格式及 ZIP 非正文部件不变。"""
    raw, item, target = _real_adaptation()
    original = Document(BytesIO(raw))
    result = rewrite.rewrite_docx(raw, [item])
    # 所有比较以保存后重开的文档为准，不能只查修改中的内存树。
    modified = Document(BytesIO(result.data))
    _expect(result.applied == [rewrite._applied(item)] and not result.skipped, "真实职责未完成改写")
    _expect(_counts(original) == _counts(modified), "段落/表格/section 数变化")
    expected = [paragraph.text for paragraph in original.paragraphs]
    # 期望列表只允许目标位置改变一次，其他位置直接来自原件。
    expected[target] = item.after
    # 整个列表比较能证明所有未勾选条目逐字不变，而不是只抽查几段。
    _expect(expected == [paragraph.text for paragraph in modified.paragraphs], "未选段落文字变化")
    _expect(_page_setup(original) == _page_setup(modified), "页边距、尺寸或方向变化")
    COMPARISON[:] = ["| 段落 | 原文 / 改后 | style 原→改 | run 字体 / pt / bold 原→改 | 格式 |",
                     "|---|---|---|---|---|"]
    for index, (before, after) in enumerate(zip(original.paragraphs, modified.paragraphs), 1):
        # 全部十九段检查，比任务书抽查三段更严格；目标段落也不能变字体。
        _expect(before.style.name == after.style.name and _font_runs(before) == _font_runs(after), f"第{index}段字体或样式变化")
        _expect((before._p.pPr.xml if before._p.pPr is not None else None)
                == (after._p.pPr.xml if after._p.pPr is not None else None), f"第{index}段落格式变化")
        if index - 1 != target:
            # XML 等值同时覆盖编号、缩进、行距、对齐和隐藏 run 属性。
            _expect(before._p.xml == after._p.xml, f"未选第{index}段 XML 变化")
        description = before.text.replace("|", "\\|")
        if index - 1 == target:
            # 表格明确列出唯一改动，不让字体相等掩盖改错段落的问题。
            description += "<br/>→ " + after.text.replace("|", "\\|")
        font = "; ".join(f"{run.font.name or '继承'}/{run.font.size.pt if run.font.size else '继承'}/{run.bold if run.bold is not None else '继承'}"
                         for run in before.runs)
        COMPARISON.append(f"| {index} | {description} | {before.style.name} → {after.style.name} | {font} → 相同 | 相同 |")
    # 比较顺序也固定，便于人工从 Word 段落定位到对应表格行。
    with ZipFile(BytesIO(raw)) as first, ZipFile(BytesIO(result.data)) as second:
        # 样式、主题、图片、关系与原有页眉页脚都逐部件比对，不能混入审计页。
        _expect(first.namelist() == second.namelist(), "ZIP 部件增删")
        for part in first.namelist():
            if part != "word/document.xml":
                _expect(first.read(part) == second.read(part), f"非正文部件变化：{part}")
    ARTIFACTS["go_backend_resume_修改版.docx"] = result.data
    COMPARISON.extend(["", f"结构：{_counts(original)} → {_counts(modified)}（段落 / 表格 / section）。",
                       "页边距、页面方向、所有非正文 ZIP 部件逐项相同；None 表示继续继承原样式。"])
    return f"{len(original.paragraphs)} 段逐项相同；仅第 {target + 1} 段文字改写；结构 {_counts(original)}；页边距/方向/非正文部件不变"


def _check_a2() -> str:
    """A2：输入 bytes 和磁盘源文件的长度、哈希在改写前后完全一致。"""
    raw, item, _ = _real_adaptation()
    # 先记完整原始指纹，函数返回值不参与原件不变的判断。
    snapshot = len(raw), sha256(raw).hexdigest()
    rewrite.rewrite_docx(raw, [item])
    disk = (FIXTURES / "go_backend_resume.docx").read_bytes()
    # 重新读磁盘，既检查 bytes 不变，也排除暗中回写源路径。
    _expect(snapshot == (len(raw), sha256(raw).hexdigest()) == (len(disk), sha256(disk).hexdigest()), "原文件或 raw 被改动")
    return f"原 raw/磁盘文件均未改；{snapshot[0]} 字节；sha256={snapshot[1]}"


def _check_b() -> str:
    """B：三 run、规范化、同段冲突、精确边界及复杂 run 的安全退让。"""
    document = Document()
    paragraph = document.add_paragraph()
    # 三种粗体状态都检查，None 与 False 不可互换。
    for text, bold in (("负责", True), ("订单接口", False), ("开发", None)):
        # 显式混合字体和所有要求保留的 run 属性，避免样张全 None 无法发现误改。
        run = paragraph.add_run(text)
        run.font.name, run.font.size, run.bold = "宋体", Pt(11), bold
        run.italic, run.underline = not bold, True
        run.font.color.rgb = RGBColor(20, 40, 60)
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    baseline = _font_runs(paragraph)
    # 文本变化与格式比较分别断言，不能只验证最终 paragraph.text。
    original = _raw(document)
    result = rewrite.rewrite_docx(original, [_adapt(paragraph.text, "完成订单接口开发"),
        _adapt(paragraph.text, "第二次修改", "R-002")])
    rewritten = Document(BytesIO(result.data)).paragraphs[0]
    _expect([run.text for run in rewritten.runs] == ["完成订单接口开发", "", ""], "跨 run 分配不正确")
    _expect(_font_runs(rewritten) == baseline and rewritten.alignment == paragraph.alignment, "跨 run 字体或段落格式变化")
    _expect(result.skipped[0]["reason"] == rewrite.ALREADY_CHANGED, "同段第二条未跳过")
    # 新文本不能成为下条建议的原文依据，且短句不得作为整段的子串命中。
    chained = rewrite.rewrite_docx(original, [_adapt(paragraph.text, "完成订单接口开发"),
        _adapt("完成订单接口开发", "第三次修改", "R-003")])
    _expect(len(chained.applied) == 1 and chained.skipped[0]["reason"] == rewrite.NOT_FOUND, "建议串改")
    _expect(not rewrite.rewrite_docx(original, [_adapt("订单接口", "替换")]).applied, "用了子串匹配")
    normalized = Document()
    # 同时覆盖全角空格、普通空格、软换行和任务书指定的三个分隔符。
    normalized.add_paragraph(" \u3000负责·订单|接口/开发\n")
    result = rewrite.rewrite_docx(_raw(normalized), [_adapt("负责订单接口开发", "完成接口交付")])
    _expect(Document(BytesIO(result.data)).paragraphs[0].text == "完成接口交付", "空白/分隔符规范化失败")
    # 只读页眉：正文不存在时记录未找到，不能去编辑页眉段落。
    normalized.sections[0].header.paragraphs[0].add_run("只在页眉")
    result = rewrite.rewrite_docx(_raw(normalized), [_adapt("只在页眉", "错误")])
    _expect(result.skipped[0]["reason"] == rewrite.NOT_FOUND, "改动页眉")
    return "跨 3 run 清空后两项；字体/粗斜体/颜色/下划线保留；normalize、同段冲突、禁止子串/串改/页眉均通过"


def _check_c() -> str:
    """C：合并单元格及三层嵌套可改，第四层不扫，表格属性与结构不变。"""
    document = Document()
    table = document.add_table(rows=1, cols=2)
    # 合并后两个单元格包装指向同一个 XML，需防止重复访问或重复改写。
    merged = table.cell(0, 0).merge(table.cell(0, 1))
    merged.paragraphs[0].add_run("表格职责")
    cell = merged
    items = [_adapt("表格职责", "表格交付")]
    for depth in range(2, 5):
        # 额外造第四层作为上限反例，不只证明三层以内能改。
        cell = cell.add_table(rows=1, cols=1).cell(0, 0)
        cell.paragraphs[0].add_run(f"嵌套第{depth}层")
        items.append(_adapt(f"嵌套第{depth}层", f"交付第{depth}层", f"R-{depth:03d}"))
    original = _raw(document)
    result = rewrite.rewrite_docx(original, items)
    # 结构基线包含第四层，即便不改也要保证它在输出里完整存在。
    changed = Document(BytesIO(result.data))
    _expect(len(result.applied) == 3 and len(result.skipped) == 1, "嵌套深度不是 3")
    _expect(_counts(document) == _counts(changed), "表格结构变化")
    _expect(changed.tables[0].cell(0, 0).paragraphs[0].text == "表格交付", "表格内职责没改")
    for first, second in zip(document.element.xpath(".//w:tbl"), changed.element.xpath(".//w:tbl")):
        # 表格文本可以变化，表格属性、网格与单元格属性必须逐项一致。
        _expect(first.tblPr.xml == second.tblPr.xml and first.tblGrid.xml == second.tblGrid.xml, "表格属性改变")
    _expect([cell.xml for cell in document.element.xpath(".//w:tcPr")]
            == [cell.xml for cell in changed.element.xpath(".//w:tcPr")], "单元格或合并属性变化")
    return "合并单元格去重；第 1/2/3 层成功、第 4 层跳过；表格网格/属性/段落数不变"


def _check_d() -> str:
    """D：空建议、未变化、找不到原句三态都有准确理由；护栏再次拒绝新事实。"""
    document = Document()
    document.add_paragraph("负责订单接口开发")
    raw = _raw(document)
    # 三种不能自动改的情况保持不同意见编号，便于逐条追溯原因。
    items = [_adapt("负责订单接口开发", "", "R-001"),
             _adapt("负责订单接口开发", "负责 订单接口开发", "R-002"),
             _adapt("正文不存在的原句", "完成接口交付", "R-003")]
    result = rewrite.rewrite_docx(raw, items)
    expected = [rewrite.MISSING, rewrite.UNCHANGED, rewrite.NOT_FOUND]
    # 一条也没改时还要求输出字节等于 raw，排除多余打包或审计注入。
    _expect([row["reason"] for row in result.skipped] == expected and result.data == raw, "三态原因或零修改字节不符")
    report, diagnosis = _inputs(items)
    report.issues[0].category = "missing"
    # 显式选中也不能让 missing 类生成经历；报告应显示全部跳过的原因。
    outcome = rewrite.apply_revisions(rep=report, diagnosis=diagnosis, raw=raw,
        filename="three.docx", job_title="后端工程师", resume_text="负责订单接口开发", selected_ids=[item.issue_id for item in items])
    _expect([row["reason"] for row in outcome.skipped] == expected and not outcome.applied, "编排三态不符")
    review = _text(outcome.review_pdf)
    # 报告内容同样实读，不能仅检查传给渲染器的 skipped 数据结构。
    _expect(all(reason in review.replace("\n", "") for reason in expected), "全部跳过时报告丢失原因")
    bad_report, diagnosis = _inputs([_adapt("负责订单接口开发", "使用 Kubernetes 完成接口交付 999 次")])
    # 即使调用者直接提交未通过 M3 的报告，最终写入仍需再过事实护栏。
    bad = rewrite.apply_revisions(rep=bad_report, diagnosis=diagnosis, raw=raw,
        filename="guard.docx", job_title="后端工程师", resume_text="负责订单接口开发")
    _expect(not bad.applied and "无原文依据" in bad.skipped[0]["reason"], "未再次核对护栏")
    return "missing / before==after / 未找到：理由逐字正确；全跳过原因进入报告；新增事实被护栏拒绝"


def _check_e() -> str:
    """E：分别注入插入与删除修订，整份拒绝且字节逐字不变。"""
    for tag in ("ins", "del"):
        # 两种修订各自创建文件，不能只测一个插入节点代表全部修订。
        document = Document()
        paragraph = document.add_paragraph("负责接口开发")
        paragraph._p.append(OxmlElement(f"w:{tag}"))
        raw = _raw(document)
        # 目标正文确实存在；拒绝原因只能是修订痕迹，而非原文查不到。
        result = rewrite.rewrite_docx(raw, [_adapt("负责接口开发", "完成接口交付")])
        _expect(result.data == raw and not result.applied and result.skipped == [{"reason": rewrite.TRACKED}], f"{tag} 未整份拒绝")
    return "w:ins / w:del 均整份拒绝；data==raw、applied=[]、理由含修订"


def _check_f() -> str:
    """F：读取真实 PDF 后重排，读回中文、审计及长句，检查 A4 和字体尺寸。"""
    # 输入复用现有双栏解析器，确保姓名与右栏经历不会被拼到同一行。
    source = extract_text("text_resume.pdf", (FIXTURES / "text_resume.pdf").read_bytes())
    # 替换只调整既有技能次序，不引入简历原文以外的名词。
    before = "技能：Python、FastAPI、MySQL、Redis"
    _expect(before in source, "真实 PDF 测试原句不存在")
    after = "技能：FastAPI、Python、MySQL、Redis"
    meta = {"model": "M3历史模型", "prompt_version": "revision_v1.1", "cost_yuan": 0,
            "elapsed_ms": 12, "generated_at": STAMP}
    snapshot = dict(meta)
    # 性能只计生成操作；独立 PDF 解码时间不算到业务调用中。
    started = perf_counter()
    data = rewrite.rewrite_pdf_from_text(source, [_adapt(before, after)],
        job_title="后端工程师", resume_name="text_resume", meta=meta)
    MEASUREMENTS.append(perf_counter() - started)
    extracted = _text(data)
    # 去提取器引入的排版空白后逐字核对，不要求自动换行与原 PDF 相同。
    compact = "".join(extracted.split())
    _expect("".join(after.split()) in compact and before not in extracted and "李娜" in extracted, "PDF 中文或替换读回失败")
    _expect(all(label in extracted for label in ("模型", "prompt", "成本", "耗时", "生成时间")), "审计页脚不全")
    _expect("版式已重排" not in extracted and meta == snapshot, "说明污染正文或 meta 变化")
    with pdfplumber.open(BytesIO(data)) as pdf:
        # 使用实际页面点数验证 A4，避免把默认页面误认为任务书版式。
        _expect(abs(pdf.pages[0].width - 595.276) < 0.1 and abs(pdf.pages[0].height - 841.89) < 0.1, "不是 A4")
        _expect(any(abs(char["size"] - 16) < 0.1 for char in pdf.pages[0].chars), "姓名不是 16pt")
        _expect(not any("Helvetica" in char["fontname"] for page in pdf.pages for char in page.chars), "字体绕过统一入口")
    # 独立合成补足职责缩进、章节标题、长段自动换行及每页审计边界。
    long_text = "张三\n工作经历\n• 负责接口开发\n" + ("完整中文长句保留不能截断" * 8 + "\n") * 45
    extra = rewrite.rewrite_pdf_from_text(long_text, [], job_title="后端", resume_name="长简历", meta=meta)
    with pdfplumber.open(BytesIO(extra)) as pdf:
        # 每页审计需检查整个长文档，第一页成功不足以证明后续页正常。
        _expect(len(pdf.pages) > 1, "未覆盖分页")
        _expect(all("生成时间" in (page.extract_text() or "") for page in pdf.pages), "多页审计丢失")
        first = pdf.pages[0]
        bullet = next(char for char in first.chars if char["text"] == "•")
        # Platypus Frame 默认含 6pt 内边距，段落左缩进以此为基准。
        _expect(abs(bullet["x0"] - (18 + 4) * 72 / 25.4 - 6) < 0.5, "职责缩进不是 4mm")
        _expect(any(abs(char["size"] - 12) < 0.1 for char in first.chars), "章节不是 12pt")
        # 排除 7pt 审计后拼接跨页正文，自动换行不应造成错误的截断报告。
        read_back = "".join(page.filter(lambda obj: obj.get("object_type") != "char" or obj["size"] > 7.5).extract_text() or "" for page in pdf.pages)
        _expect("".join(read_back.split()) == "".join(long_text.split()), "长句被截断")
    ARTIFACTS["text_resume_修改版.pdf"] = data
    # 多页样张只用于内部视觉核验，不作为真实简历交付。
    ARTIFACTS["多页排版_QA.pdf"] = extra
    return "真实 text_resume.pdf 中文替换读回；A4/16pt 姓名/12pt 章节/4mm 职责/长句分页/逐页审计均通过"


def _check_g() -> str:
    """G：两类输入一次两份、勾选语义、报告深拷贝、护栏审计与重排声明。"""
    raw, item, _ = _real_adaptation()
    report, diagnosis = _inputs([item])
    missing = report.issues[0].model_copy(update={"id": "R-002", "category": "missing", "suggested_text": ""})
    same = report.issues[0].model_copy(update={"id": "R-003", "suggested_text": item.before})
    report.issues.extend([missing, same])
    # 序列化快照覆盖所有嵌套对象，能发现浅拷贝造成的原地回填。
    snapshot = report.model_dump_json(), diagnosis.model_dump_json(), sha256(raw).hexdigest()
    captured = []
    renderer = rewrite.render_review_pdf

    def capture(rep, diag, **kwargs):
        """拦截报告副本并继续调用真实渲染器，不以假 PDF 冒充一次两份。"""
        # 保留副本引用，以便核对护栏审计与输入对象身份。
        captured.append((rep, diag))
        return renderer(rep, diag, **kwargs)

    with patch.object(rewrite, "render_review_pdf", side_effect=capture):
        # 渲染仍走真实 M4 函数，保证最终两份字节都可以打开。
        started = perf_counter()
        outcome = rewrite.apply_revisions(rep=report, diagnosis=diagnosis, raw=raw,
            filename="go_backend_resume.docx", job_title=report.profile.title, resume_text="\n".join(p.text for p in Document(BytesIO(raw)).paragraphs))
        MEASUREMENTS.append(perf_counter() - started)
    _expect(outcome.review_pdf.startswith(b"%PDF") and outcome.resume_bytes.startswith(b"PK")
            and outcome.resume_ext == "docx" and len(outcome.applied) == 1 and len(outcome.skipped) == 2, "Word 一次两份或默认选择错误")
    _expect(snapshot == (report.model_dump_json(), diagnosis.model_dump_json(), sha256(raw).hexdigest()), "输入对象被修改")
    copied, diag_copy = captured[0]
    # 除了身份不同还检查嵌套 profile，排除仅复制顶层容器。
    _expect(copied is not report and diag_copy is not diagnosis and copied.profile is not report.profile, "未深拷贝")
    _expect(copied.adaptations[0].added_terms == [] and copied.adaptations[0].kept_numbers == [], "空护栏审计未填")
    _expect(copied.meta["cost_yuan"] == 0.001 and copied.meta["rewrite_cost_yuan"] == 0, "历史成本或改写成本错误")
    # 空列表表示一个也不采纳，不得误用 selected_ids or 默认列表。
    none = rewrite.apply_revisions(rep=report, diagnosis=diagnosis, raw=raw, filename="none.docx",
        job_title="后端", resume_text=item.before, selected_ids=[])
    _expect(none.resume_bytes == raw and not none.applied and not none.skipped, "空选择未保留源文件")
    pdf_raw = (FIXTURES / "text_resume.pdf").read_bytes()
    # 大写扩展名也是合法上传输入，输出扩展名应统一成小写。
    # 与真实上传后的 resume_text 一致；读回验证仍直接使用 pdfplumber。
    source = extract_text("text_resume.pdf", pdf_raw)
    pdf_item = _adapt("技能：Python、FastAPI、MySQL、Redis", "技能：FastAPI、Python、MySQL、Redis")
    pdf_report, diagnosis = _inputs([pdf_item])
    pdf_outcome = rewrite.apply_revisions(rep=pdf_report, diagnosis=diagnosis, raw=pdf_raw,
        filename="text_resume.PDF", job_title="后端", resume_text=source, selected_ids=["R-001"])
    _expect(pdf_outcome.resume_ext == "pdf" and pdf_outcome.review_pdf.startswith(b"%PDF")
            and pdf_outcome.resume_bytes.startswith(b"%PDF") and len(pdf_outcome.applied) == 1, "PDF 一次两份失败")
    # 重排声明只归意见报告，修改版简历正文不能出现相同说明。
    _expect("原文件是PDF，版式已重排" in "".join(_text(pdf_outcome.review_pdf).split()), "报告缺少重排声明")
    _expect("版式已重排" not in _text(pdf_outcome.resume_bytes), "重排声明污染简历")
    # 有数字的真实职责用既有数字改写，回填必须包含归一后保留的数字。
    numbers, diagnosis = _inputs([_adapt("支持 20 家客户", "交付 20 家客户")])
    number_doc = Document()
    number_doc.add_paragraph("支持 20 家客户")
    captured.clear()
    # 带数字用例证明审计不是无条件填写空列表。
    with patch.object(rewrite, "render_review_pdf", side_effect=capture):
        rewrite.apply_revisions(rep=numbers, diagnosis=diagnosis, raw=_raw(number_doc), filename="numbers.docx",
            job_title="后端", resume_text="支持 20 家客户")
    _expect(captured[0][0].adaptations[0].kept_numbers == ["20"], "保留数字没有来自护栏")
    ARTIFACTS["go_backend_resume_意见报告.pdf"] = outcome.review_pdf
    ARTIFACTS["text_resume_意见报告.pdf"] = pdf_outcome.review_pdf
    return "docx→docx+报告、PDF→PDF+报告；默认全选/显式单选/空选择正确；copy 回填、数字审计、历史元数据不变"


def _check_h() -> str:
    """H：源码红线、注释/docstring、零模型导入及真实操作成本与性能。"""
    ratios = []
    for relative in ("app/resume_rewrite.py", "tools/check_resume_rewrite.py"):
        # 注释分母包含空行，字符串中的井号不计入注释数量。
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        comments = {token.start[0] for token in tokenize.generate_tokens(StringIO(source).readline) if token.type == tokenize.COMMENT}
        ratio = len(comments) / len(source.splitlines())
        nodes = [node for node in ast.walk(tree) if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
        # 私有函数、局部函数与类也都必须提供 docstring。
        _expect(ratio >= 0.15 and all(ast.get_docstring(node) for node in nodes), f"{relative} 注释 {ratio:.1%} 或 docstring 不足")
        ratios.append(f"{Path(relative).name} 注释 {ratio:.1%}")
        if relative.startswith("app/"):
            # 既检查源码词，也检查语法节点，避免别名或未执行分支绕过红线。
            _expect(not re.search(r"openai|\bawait\b|dotenv|\.env|requests|httpx|urllib", source, re.I), "业务模块出现模型/网络/环境依赖")
            _expect(not any(isinstance(node, (ast.Await, ast.AsyncFunctionDef)) for node in ast.walk(tree)), "出现异步调用")
            forbidden = [node for node in ast.walk(tree) if isinstance(node, ast.Attribute)
                         and isinstance(node.ctx, ast.Store) and node.attr == "text"
                         and isinstance(node.value, ast.Name) and node.value.id in {"paragraph", "p"}]
            _expect(not forbidden, "用了 paragraph.text 整段赋值")
    # 导入检测与运行时 socket 封锁共同检查，不能只靠文本 grep 声称零网络。
    _expect(not any(name in sys.modules for name in ("openai", "app.agent", "app.providers", "dotenv")), "离线验收导入了模型或环境模块")
    raw, item, _ = _real_adaptation()
    # 真实 Word 操作独立计时，不将整套验收的多次读回累加到单次阈值。
    started = perf_counter()
    rewrite.rewrite_docx(raw, [item])
    MEASUREMENTS.append(perf_counter() - started)
    _expect(max(MEASUREMENTS) < 1.5, f"单次操作超时 {max(MEASUREMENTS):.3f}s")
    return "无模型/网络/环境依赖；改写成本=0；" + "；".join(ratios) + f"；docstring 100%；最慢操作 {max(MEASUREMENTS):.3f}s<1.5s"


def _check_i() -> str:
    """I：直接编译源码中的剥离函数，验证 D-M3-06 而不导入其模型客户端。"""
    source = (PROJECT_ROOT / "app/revision_llm.py").read_text(encoding="utf-8")
    node = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "_strip_inferred_note")
    # 函数体来自当前源码，不在验收中复制一份实现；仅提供其唯一依赖 re。
    namespace = {"re": re}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "app/revision_llm.py", "exec"), namespace)
    strip = namespace["_strip_inferred_note"]
    # 反例既覆盖用户指定的非推测，也覆盖句子中没有括号的裸词。
    for text in ("时间重叠，需核实（非推测）", "此处推测原因待核实", "不应推测任职月份"):
        _expect(strip(text) == text, f"误剥离事实文字：{text}")
    for phrase in ("岗位画像（推测）", "（推测）", "岗位画像", "岗位画像(推测)"):
        # 正向用例保证修复误删时没有让完整声明无法被剥离。
        _expect(strip("JD 要求 Go；" + phrase) == "JD 要求 Go", f"完整短语未剥离：{phrase}")
    return "时间重叠，需核实（非推测）逐字保留；裸词推测保留；三种完整声明正常剥离"


def main() -> int:
    """执行 A1/A2/B～I，逐组继续并汇总退出码，可选保存明确命名的交付样张。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="只在指定目录保存修改版与逐段比对表，不覆盖源文件")
    args = parser.parse_args()
    # 总耗时包括所有 A～I 与样张落盘，单次性能另在 H 中明确打印。
    started = perf_counter()
    groups = (("A1 保版式逐项比对", _check_a1), ("A2 原件不变", _check_a2),
              ("B 跨 run 与匹配", _check_b), ("C 表格递归", _check_c),
              ("D skipped 三态", _check_d), ("E 修订痕迹拒绝", _check_e),
              ("F PDF 重排读回", _check_f), ("G 一次两份", _check_g),
              ("H 零模型与规范", _check_h), ("I D-M3-06", _check_i))
    passed = 0
    # 未使用的合成场景也不能联网；任何实际网络动作立即使所在组失败。
    with ExitStack() as stack:
        for target in ("socket.create_connection", "socket.socket.connect", "socket.getaddrinfo"):
            stack.enter_context(patch(target, side_effect=AssertionError("M5 验收禁止网络")))
        for label, check in groups:
            try:
                # 整组全部断言通过才记分；失败不会中断后面的独立分组。
                detail = check()
            except Exception as exc:
                print(f"{label}：失败；{type(exc).__name__}: {exc}")
            else:
                passed += 1
                print(f"{label}：通过；{detail}")
    if args.output_dir is not None and passed == len(groups):
        destination = args.output_dir.resolve()
        # 验收样张不允许落到源 fixture 目录，避免任何路径或名称造成原件覆盖。
        _expect(not destination.is_relative_to(FIXTURES.resolve()), "输出目录不能位于原件目录内")
        destination.mkdir(parents=True, exist_ok=True)
        for filename, data in ARTIFACTS.items():
            # 所有样张都有新文件名，原 fixture 文件没有写入入口。
            (destination / filename).write_bytes(data)
        (destination / "M5_段落字体比对.md").write_text("\n".join(COMPARISON) + "\n", encoding="utf-8")
        print(f"样张与段落/字体比对表：{destination}")
    elapsed = perf_counter() - started
    # 耗时阈值在 H 按真实单次操作核对；整套验收还包括多次文档与 PDF 重读。
    print(f"通过 {passed}/{len(groups)}；耗时 {elapsed:.3f} 秒")
    return 0 if passed == len(groups) else 1


if __name__ == "__main__":
    # 命令行与 CI 可依赖退出码，而不是解析含有「通过」字样的失败行。
    raise SystemExit(main())
