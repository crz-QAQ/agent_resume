"""生成 Day 15 的三个测试文件（放在 工具/test_files/）。

三个文件对应三种真实情况：
  ① table_resume.docx —— 工作经历写在【表格】里（只遍历 paragraphs 的实现会挂）
  ② text_resume.pdf   —— 文本层 PDF，双栏排版（不按坐标排序会串行）
  ③ scan_resume.pdf   —— 纯图片 PDF，无文本层（必须被 422 拒绝，不能返回空）

跑法：.venv/Scripts/python.exe 工具/make_test_files.py
"""

from pathlib import Path

OUT = Path(__file__).resolve().parent / "test_files"
OUT.mkdir(exist_ok=True)


def make_docx() -> Path:
    from docx import Document

    doc = Document()
    doc.add_heading("个人简历", 0)
    doc.add_paragraph("张伟  |  男  |  1995 年  |  现居深圳")
    doc.add_paragraph("求职意向：后端开发工程师")

    # ★ 关键：工作经历放在表格里
    table = doc.add_table(rows=4, cols=2)
    try:
        table.style = "Table Grid"
    except Exception:
        pass
    rows = [
        ("时间", "公司与职位"),
        ("2019.07 - 2023.03", "深圳某某科技有限公司 · 后端开发工程师"),
        ("主要职责", "负责订单系统和支付模块，技术栈 Go + MySQL + Redis"),
        ("2023.04 - 至今", "广州某某软件有限公司 · 高级后端开发工程师"),
    ]
    for i, (left, right) in enumerate(rows):
        table.cell(i, 0).text = left
        table.cell(i, 1).text = right

    doc.add_paragraph("技能：Go、PHP、MySQL、Redis、RabbitMQ、Docker")
    doc.add_paragraph("工作年限：6 年后端开发经验")
    path = OUT / "table_resume.docx"
    doc.save(path)
    return path


def make_text_pdf() -> Path:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    path = OUT / "text_resume.pdf"
    c = canvas.Canvas(str(path))
    c.setFont("STSong-Light", 12)

    # 左栏
    c.drawString(60, 780, "李娜")
    c.drawString(60, 750, "2018 年入行，7 年后端开发经验")
    c.drawString(60, 730, "技能：Python、FastAPI、MySQL、Redis")

    # 右栏（x=320 —— 不按 x 坐标排序的实现会把左右栏串成一行）
    c.drawString(320, 780, "工作经历")
    c.drawString(320, 750, "2018-2021 某电商公司 后端开发")
    c.drawString(320, 730, "2021-至今 某 SaaS 公司 高级后端")

    c.drawString(60, 690, "教育：某大学 计算机科学与技术 2014-2018")
    c.save()
    return path


def make_scan_pdf() -> Path:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (900, 1200), "white")
    d = ImageDraw.Draw(img)
    d.text((60, 60), "WANG FANG", fill="black")
    d.text((60, 110), "2017 - NOW : 8 years backend development", fill="black")
    d.text((60, 160), "SKILLS: Go, PHP, MySQL, Redis, RabbitMQ", fill="black")
    d.text((60, 210), "EXPERIENCE: e-commerce order system, payment module", fill="black")
    d.rectangle([50, 40, 850, 260], outline="gray")
    path = OUT / "scan_resume.pdf"
    img.save(path, "PDF", resolution=150)
    return path


def make_clean_docx() -> Path:
    """不含任何相对时间词（去年/今年/至今/目前/现在），用来验证 status=ok。

    前三个测试文件都带「至今」，必然触发「相对时间」护栏 → needs_review。
    要验证 ok 分支，必须有一份"时间全写死"的简历。
    """
    from docx import Document

    doc = Document()
    doc.add_heading("个人简历", 0)
    doc.add_paragraph("陈明  |  男  |  现居深圳")
    doc.add_paragraph("求职意向：后端开发工程师")

    table = doc.add_table(rows=2, cols=2)
    try:
        table.style = "Table Grid"
    except Exception:
        pass
    table.cell(0, 0).text = "2015.07 - 2021.12"
    table.cell(0, 1).text = "杭州某某网络科技有限公司 · Java 开发工程师"
    table.cell(1, 0).text = "2021.12 - 2025.06"
    table.cell(1, 1).text = "上海某某信息科技有限公司 · 后端开发工程师"

    doc.add_paragraph("技能：Java、Spring、MySQL、Redis、Docker")
    doc.add_paragraph("工作年限：10 年后端开发经验")
    path = OUT / "clean_resume.docx"
    doc.save(path)
    return path


def main() -> None:
    p1 = make_docx()
    p2 = make_text_pdf()
    p3 = make_scan_pdf()
    p4 = make_clean_docx()
    for p in (p1, p2, p3, p4):
        print(f"生成: {p.relative_to(OUT.parent)}  {p.stat().st_size} bytes")

    # 自检：确认 ① 表格有内容、② 有文本层、③ 没有文本层
    import pdfplumber

    with pdfplumber.open(str(p2)) as pdf:
        t2 = pdf.pages[0].extract_text() or ""
    with pdfplumber.open(str(p3)) as pdf:
        t3 = pdf.pages[0].extract_text() or ""

    print(f"\n自检:")
    print(f"  text_resume.pdf 文本层字符数: {len(t2)}  （应 > 100 ✓）")
    print(f"  scan_resume.pdf 文本层字符数: {len(t3)}  （应 < 10，证明是扫描件 ✓）")

    from docx import Document

    doc = Document(str(p1))
    table_text = "".join(cell.text for t in doc.tables for row in t.rows for cell in row.cells)
    para_text = "".join(p.text for p in doc.paragraphs)
    print(f"  table_resume.docx:")
    print(f"    paragraphs 抽到: {len(para_text)} 字（不含工作经历表）")
    print(f"    tables    抽到: {len(table_text)} 字（工作经历在这里 ✓）")
    print(f"    → 只遍历 paragraphs 会漏掉 {len(table_text)} 字 ✗")


if __name__ == "__main__":
    main()
