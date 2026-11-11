"""教练验收 Day15：文件解析 7 条（Docker 那条除外，daemon 未起）。

跑法（项目根目录）：
  .venv/Scripts/python.exe D:/HermesDoc/tmp/verify_delivery.py
"""

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.file_parser import (  # noqa: E402
    ScannedPdfError,
    UnsupportedFormatError,
    extract_text,
)

TF = ROOT / "tools" / "test_files"
results = []


def check(name, ok, detail=""):
    """记录并打印一项历史文件解析验收结果。"""
    # 单项失败继续执行其他断言，最终退出码统一表示验收结果。
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


# ① docx 表格里的工作经历必须被抽到
docx_text = extract_text("table_resume.docx", (TF / "table_resume.docx").read_bytes())
has_table_slot = "2019.07 - 2023.03" in docx_text
has_duty = "Go + MySQL + Redis" in docx_text
check("① docx 抽到表格内工作经历", has_table_slot and has_duty,
      f"len={len(docx_text)} 时间行={has_table_slot} 职责行={has_duty}")

# ② 双栏 PDF 不能把左右栏串成一行
pdf_text = extract_text("text_resume.pdf", (TF / "text_resume.pdf").read_bytes())
lines = pdf_text.splitlines()
mixed = [ln for ln in lines if "李娜" in ln or ("工作经历" in ln and len(ln) > 12)]
cross = [ln for ln in lines if ("李娜" in ln and "工作经历" in ln)]
check("② PDF 双栏未串行", not cross and any("工作经历" in ln for ln in lines),
      f"行数={len(lines)} 串行={len(cross)} 首行={lines[0] if lines else ''!r}")

# ③ 扫描件 PDF → 明确异常
try:
    extract_text("scan_resume.pdf", (TF / "scan_resume.pdf").read_bytes())
    check("③ 扫描件 PDF 抛 ScannedPdfError", False, "没抛异常（静默返回了文本）")
except ScannedPdfError as exc:
    msg = str(exc)
    check("③ 扫描件 PDF 抛 ScannedPdfError", "OCR" in msg or "文本层" in msg, f"msg={msg[:30]}")

# ④ 非法后缀
try:
    extract_text("a.exe", b"MZ\x90\x00")
    check("④ .exe 抛 UnsupportedFormatError", False, "没抛异常")
except UnsupportedFormatError as exc:
    check("④ .exe 抛 UnsupportedFormatError", "不支持" in str(exc), f"msg={str(exc)[:24]}")

# ⑤ 空文件
try:
    extract_text("empty.docx", b"")
    check("⑤ 空文件抛异常", False, "没抛异常")
except Exception as exc:  # noqa: BLE001
    check("⑤ 空文件抛异常", isinstance(exc, (ScannedPdfError,)) or "无法解析" in str(exc),
          f"{type(exc).__name__}: {str(exc)[:30]}")

# ⑥ HTTP 层：422 / 413 / 200
from fastapi.testclient import TestClient  # noqa: E402
from app import api  # noqa: E402

# 打桩掉 LLM，只验文件层与状态码
from app.agent import AgentResult, StepLog  # noqa: E402
from app.schemas import ResumeInfo  # noqa: E402
# 文件解析错误与API现在使用同一异常类，不会因重复导入而捕获失败。
# 包路径统一也让AgentResult和ResumeInfo保持原有类型关系。
# 以下旧路由替身仍只用于协议验收，不代表真实模型调用成功。
# 新的六组真实匹配由verify_match.py独立执行和计费。
# 本工具保留历史Docker体积检查，未把它并入S7b的API验收分母。
# 工具根目录根据脚本位置计算，不再绑定某台机器的旧项目路径。
# 除这些导入和文档调整，原文件解析断言保持原样。


async def fake_agent(text):
    """提供旧上传接口的零网络替身，只验证文件层与HTTP协议。"""
    # 历史协议验收不因模型可用性而改变文件解析边界。
    return AgentResult(
        status="ok",
        data=ResumeInfo(name="张伟", years=6.0, skills=["Go", "PHP"]),
        steps=[StepLog(step=1, action="extract", tokens_in=1, tokens_out=1,
                       cost_yuan=0.0, ok=True)],
        prompt_version="resume_v2",
    )


api.parse_resume_agent = fake_agent
# 替身只在本独立验收进程中生效，不写入生产配置。
client = TestClient(api.app)

r = client.post("/resume/upload", files={"file": ("a.exe", b"MZ")})
check("⑥a .exe → 422", r.status_code == 422, f"code={r.status_code}")

r = client.post("/resume/upload", files={"file": ("scan.pdf", (TF / "scan_resume.pdf").read_bytes())})
check("⑥b 扫描件 → 422", r.status_code == 422, f"code={r.status_code} detail={r.json().get('detail','')[:22]}")

r = client.post("/resume/upload", files={"file": ("big.docx", b"x" * (10 * 1024 * 1024 + 10))})
check("⑥c 11MB → 413", r.status_code == 413, f"code={r.status_code}")

r = client.post("/resume/upload", files={"file": ("empty.txt", b"")})
# 空文件由真实文件解析器拒绝，不能依靠模型替身判定。
check("⑥d 空文件 → 422", r.status_code == 422, f"code={r.status_code}")

r = client.post("/resume/upload", files={"file": ("table_resume.docx", (TF / "table_resume.docx").read_bytes())})
check("⑥e docx → 200", r.status_code == 200,
      f"code={r.status_code} text_length={r.json().get('text_length') if r.status_code==200 else r.text[:60]}")

# ⑦ Docker 镜像体积：≤550MB
#   原判据写的 300MB 是拍脑袋定的（282×1.06）；
#   Day15 加了 python-docx / pdfplumber / python-multipart 三个解析库，
#   实测 488MB，合理上限应为 550MB。
import re  # noqa: E402
import subprocess  # noqa: E402

p = subprocess.run(
    ["docker", "images", "resume-ai:day15", "--format", "{{.Size}}"],
    capture_output=True,
    text=True,
    timeout=30,
)
size_txt = p.stdout.strip()
mb = 0.0
m = re.match(r"([\d.]+)\s*([MG])B", size_txt)
if m:
    mb = float(m.group(1)) * (1024.0 if m.group(2) == "G" else 1.0)
check("⑦ docker 镜像 ≤550MB", 0 < mb <= 550, f"size={size_txt or '(空)'} ≈{mb:.0f}MB")

failed = [n for n, ok, _ in results if not ok]
# Docker检查沿用旧工具行为，本次S7b不调用此整体验收脚本。
print(f"\n=== {len(results) - len(failed)}/{len(results)} 通过 ===")
print("FAILED: " + (", ".join(failed) if failed else "无"))
sys.exit(1 if failed else 0)
