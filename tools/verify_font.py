"""验证 S0 中文字体能完成 PDF 写入与文本读回，及时发现缺字问题。

字体注册成功 ≠ 真正嵌入；必须用 pdfplumber 读回来验证，
否则要等完整报告生成后才发现中文是方块。
从项目根目录直接运行；测试 PDF 写入系统临时目录，失败时退出码为 1。
"""

from pathlib import Path
import sys
import tempfile

# 脚本位于 tools/，app 是兄弟目录；把项目根加入 sys.path 才能直接导入 app。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pdfplumber
from reportlab.pdfgen import canvas

from app.pdf_font import get_cjk_font


def main() -> int:
    """生成固定中文样例并核对读回文本。

    参数：无；使用 S0 任务书指定的两行文本。
    返回：成功为 0；字体、文件读写或文本一致性验证失败为 1。
    异常：普通异常均捕获并输出 FAIL，不向调用方继续抛出。
    """
    try:
        # 通过统一入口取字体，防止验证脚本形成另一套注册逻辑。
        font = get_cjk_font()
        # 临时目录避免生成的 PDF 混入项目源码或被提交。
        output = Path(tempfile.gettempdir()) / "font_test.pdf"
        # 固定样例及 3 年、78 分来自 S0 验收要求，不涉及真实匹配计算。
        # 故意混合中英文，验证 ASCII 与 CJK 同页不乱码、不互相影响。
        lines = ("简历匹配分析报告", "Go 3 年 / 后端 / 匹配度 78 分")
        document = canvas.Canvas(str(output))
        # Canvas 必须显式设中文字体；18 磅仅为测试页的可读字号。
        document.setFont(font, 18)
        # x=72pt 为左边距；y 从距页底 770pt 起，每行下移 36pt，避免文本重叠。
        for index, line in enumerate(lines):
            document.drawString(72, 770 - index * 36, line)
        document.save()
        written = "\n".join(lines)
        # 从实际保存的文件读回，才能发现 PDF 写入阶段的字体问题。
        # 文件生成或大小非零无法证明中文正确，必须用 extract_text 核对实际文本。
        with pdfplumber.open(output) as pdf:
            # S0 要求生成一页测试 PDF，页数异常也视为验收失败。
            assert len(pdf.pages) == 1, "测试 PDF 应只有一页"
            extracted = pdf.pages[0].extract_text() or ""
        print(f"字体名: {font}")
        print(f"PDF: {output}")
        print(f"写入: {written}")
        print(f"读回: {extracted}")
        # 任务书允许空白差异，但中文、英文和数字必须完整一致。
        # CJK 抽取可能插入多余空格，比较时去掉空白，避免直接比较造成误报。
        assert "".join(extracted.split()) == "".join(written.split()), "中文往返文本不一致"
        print("PASS (exit 0)")
        return 0
    except Exception as exc:
        # 保留错误类型与原因，同时让命令行验收可通过退出码识别失败。
        print(f"FAIL: {type(exc).__name__}: {exc}")
        # 必须 return 1 而不是 pass；CI / 调用脚本依靠 exit code 判断失败。
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
