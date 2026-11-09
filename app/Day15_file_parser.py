"""Day 15：从简历文件中抽取可供 Agent 使用的纯文本。

解析原则：
1. DOCX 不能只读 ``document.paragraphs``，简历经历经常放在表格中。
2. PDF 不能直接相信 PDF 阅读器给出的流顺序，双栏文本需要按页内坐标重排。
3. 扫描件没有文本层时必须抛异常，不能把空字符串继续传给 Agent。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import BadZipFile

import pdfplumber
from docx import Document
from docx.opc.exceptions import OpcError
from docx.table import Table
from lxml.etree import XMLSyntaxError
from pdfminer.pdfparser import PDFException
from pdfplumber.utils.exceptions import PdfminerException


SUPPORTED_EXTENSIONS = frozenset({".docx", ".pdf", ".txt", ".md"})
MIN_TEXT_LENGTH = 50


class FileParseError(ValueError):
    """文件内容无法解析时抛出的统一基类，接口层据此返回 422。"""


class UnsupportedFormatError(FileParseError):
    """文件后缀不在简历上传白名单中。"""

    def __init__(self, filename: str) -> None:
        self.filename = filename
        super().__init__(
            f"不支持的文件格式：{filename or '(未提供文件名)'}。"
            "仅支持 .pdf、.docx、.txt、.md。"
        )


class ScannedPdfError(FileParseError):
    """PDF 没有可用文本层，或文件可提取的文本过短。"""


def _append_story_lines(story: Any, lines: list[str]) -> None:
    """按文档内顺序追加段落和表格，供正文、单元格、页眉页脚复用。"""

    iter_inner_content = getattr(story, "iter_inner_content", None)
    if callable(iter_inner_content):
        blocks = iter_inner_content()
    else:
        # 部分 python-docx 容器没有 iter_inner_content，退回到其公开集合。
        blocks = [*getattr(story, "paragraphs", []), *getattr(story, "tables", [])]

    for block in blocks:
        if isinstance(block, Table):
            _append_table_lines(block, lines)
            continue

        paragraph_text = getattr(block, "text", "").strip()
        if paragraph_text:
            lines.append(paragraph_text)


def _append_table_lines(table: Table, lines: list[str]) -> None:
    """逐行读取表格；单元格内可能还有嵌套表格。"""

    for row in table.rows:
        values: list[str] = []
        seen_cells: list[Any] = []

        for cell in row.cells:
            # 横向合并单元格会在 row.cells 中重复返回同一 XML 节点，
            # 按底层节点身份去重，避免同一段经历被重复抽取。
            if any(cell._tc is seen_cell for seen_cell in seen_cells):
                continue
            seen_cells.append(cell._tc)

            cell_lines: list[str] = []
            _append_story_lines(cell, cell_lines)
            cell_text = " ".join(cell_lines).strip()
            if cell_text:
                values.append(cell_text)

        if values:
            lines.append(" | ".join(values))


def _extract_docx(raw: bytes) -> str:
    """提取 DOCX 正文、表格、页眉和页脚。"""

    try:
        document = Document(BytesIO(raw))
        lines: list[str] = []

        # 按正文中的真实顺序交错读取段落和表格，而不是先读完全部段落。
        _append_story_lines(document, lines)

        for section in document.sections:
            _append_story_lines(section.header, lines)
            _append_story_lines(section.footer, lines)
    except (BadZipFile, OpcError, XMLSyntaxError, KeyError, OSError, ValueError) as exc:
        raise FileParseError("Word 文件已损坏或格式无效，无法解析。") from exc

    return "\n".join(lines)


def _find_column_split(words:list[dict[str,Any]])->float|None:
    """找分栏线 x 坐标；找不到返回 None（单栏）。

        原理：双栏排版会在页面中间留下一整条【竖直空白带】—— 任何一行都没有词跨过它。
              做法：横向按 5pt 切窄条，标记每条是否被某个词的 [x0, x1] 覆盖，
              找最长的连续"没人覆盖"的区间，它的中心就是分栏线。

        ⚠️ 不能用「相邻 x0 的最大间隙」：
           每行第一个词的 x0 并不相同（60 / 84.7 / 134.5 / 233 ...），
           x0 之间的差值混进了行内词间距，测出来的分栏线是随机值 ✗
           （实测这份文件 x0 最大间隙 92.3pt，而真正的空白带宽 54pt 位置正确）
        """
    if len(words) < 6:
        return None  # 词太少不可能是多栏

    step = 5
    right_edge = max(float(w["x1"]) for w in words)
    n = int(right_edge // step) + 2
    covered = [0] * n
    for w in words:
        lo = int(float(w["x0"]) // step)
        hi = int(float(w["x1"]) // step)
        for i in range(lo, min(hi + 1, n)):
            covered[i] += 1

    # 找出所有连续空白带
    runs: list[tuple[int, int]] = []  # (长度格, 起点格)
    cur_len = 0
    cur_start = 0
    for i, c in enumerate(covered):
        if c == 0:
            if cur_len == 0:
                cur_start = i
            cur_len += 1
        else:
            if cur_len:
                runs.append((cur_len, cur_start))
            cur_len = 0
    if cur_len:
        runs.append((cur_len, cur_start))

    # ⚠️ 只保留"两侧都有词"的空白带：
    #    左右页边距也是最长的空白带（实测 60pt），但它左边没词，不是分栏线 ✗
    inner = [(ln, st) for ln, st in runs if st > 0 and st + ln < n]
    if not inner:
        return None

    best_len, best_start = max(inner)
    if best_len * step < 20:
        return None  # 空白带太窄，当单栏处理

    center = (best_start + best_len / 2) * step
    left = sum(1 for w in words if float(w["x1"]) <= center)
    right = sum(1 for w in words if float(w["x0"]) >= center)
    return center if (left >= 3 and right >= 3) else None


# def _pdf_sort_key(item: dict[str, Any]) -> tuple[int, float]:
#     """按约 10 磅一行、行内从左到右排序，修复双栏阅读顺序。"""
#
#     return round(float(item["top"]) / 10), float(item["x0"])

def _pdf_sort_key(item: dict[str,Any],split: float|None = None)->tuple[int,int,float]:
    """先分栏 → 再分行 → 行内从左到右。split 是分栏线 x 坐标（None = 单栏）。"""
    col = 1 if (split is not None and float(item["x0"]) > split) else 0
    return col, round(float(item["top"]) / 10),float(item["x0"])

def _extract_pdf(raw: bytes) -> str:
    """提取 PDF 文本层，并按坐标重建稳定的阅读顺序。"""

    try:
        with pdfplumber.open(BytesIO(raw)) as pdf:
            lines: list[str] = []

            for page in pdf.pages:
                words = page.extract_words()

                # 优先按词排序，保留英文单词边界和中文短语间的空格。
                if words:
                    split = _find_column_split(words)
                    ordered_words = sorted(words, key=lambda w:_pdf_sort_key(w,split))
                    current_line_key: tuple[int,int] | None = None
                    current_words: list[str] = []

                    for word in ordered_words:
                        col = 1 if (split is not None and float(word["x0"]) > split) else 0
                        line_key = round(float(word["top"]) / 10)
                        if current_line_key is not None and line_key != current_line_key:

                            line_text = " ".join(current_words).strip()
                            if line_text:
                                lines.append(line_text)
                            current_words = []

                        current_words.append(str(word["text"]))
                        current_line_key = line_key

                    line_text = " ".join(current_words).strip()
                    if line_text:
                        lines.append(line_text)
                    continue

                # 极少数 PDF 只能拿到字符而没有词边界时，仍按同一坐标规则重排。
                # 这个分支只在「PDF 抽不出词边界，只有单个字符」时走 —— 极少见，但走的时候同样是双栏串行 ✗
                char_split = _find_column_split(page.chars)
                ordered_chars = sorted(page.chars, key=lambda c:_pdf_sort_key(c,char_split))
                current_line_key = None
                current_chars: list[str] = []

                for char in ordered_chars:
                    col = 1 if (char_split is not None and float(char["x0"]) > char_split) else 0
                    line_key = round(float(char["top"]) / 10)
                    if current_line_key is not None and line_key != current_line_key:
                        line_text = "".join(current_chars).strip()
                        if line_text:
                            lines.append(line_text)
                        current_chars = []

                    current_chars.append(str(char["text"]))
                    current_line_key = line_key

                line_text = "".join(current_chars).strip()
                if line_text:
                    lines.append(line_text)
    except (PDFException, PdfminerException, OSError, ValueError) as exc:
        raise FileParseError("PDF 文件已损坏或格式无效，无法解析。") from exc

    return "\n".join(lines)


def _decode_text_file(raw: bytes) -> str:
    """文本文件优先 UTF-8，失败后兼容中文简历常见的 GBK 编码。"""

    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return raw.decode("gbk")
        except UnicodeDecodeError as exc:
            raise FileParseError(
                "文本文件编码无法识别，请使用 UTF-8 或 GBK 编码。"
            ) from exc


def _normalize_and_validate(raw_text: str, suffix: str) -> str:
    """去掉空行和行首尾空白，并拒绝过短或空白的提取结果。"""

    text = "\n".join(
        line.strip()
        for line in raw_text.splitlines()
        if line.strip()
    )
    if len(text) >= MIN_TEXT_LENGTH:
        return text

    if suffix == ".pdf":
        raise ScannedPdfError(
            "这份 PDF 没有文本层（很可能是扫描件/图片），需要 OCR 才能解析。"
            "请上传文本版 PDF 或直接粘贴文本。"
        )

    raise ScannedPdfError(
        "文件为空或可提取文本不足 50 字符，请上传有效简历文件。"
    )


def extract_text(filename: str, raw: bytes) -> str:
    """按后缀抽取纯文本；抽不出来时明确抛异常，绝不返回空字符串。"""

    suffix = Path(filename or "").suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFormatError(filename)

    if suffix == ".docx":
        raw_text = _extract_docx(raw)
    elif suffix == ".pdf":
        raw_text = _extract_pdf(raw)
    else:
        raw_text = _decode_text_file(raw)

    return _normalize_and_validate(raw_text, suffix)
