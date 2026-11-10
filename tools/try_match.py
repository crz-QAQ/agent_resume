"""临时试用脚本：简历文件 + JD 文本 → 抽取 + 硬性打分（S1+S2+S3 串起来）。

用途：在看 S4/S5 之前，先自己玩一下已经做完的部分。
注意：这不是 PRD 的交付物，是本地试用脚本（S6/S7 会有正式接口和 PDF 报告）。

跑法：
  1) PyCharm 里右键 → Run 'try_match'
  2) 或终端：.venv\\Scripts\\python.exe tools\\try_match.py
  3) 想换简历：改 RESUME_FILE；想换岗位：改 JD_TEXT（或用命令行第 1 个参数传 JD 文件路径）
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 兼容两种 import 风格：项目根（app.*）+ app/ 目录（扁平 import）
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from app.resume_parser import parse_resume_file  # noqa: E402  简历：文件 → 结构化
from app.job_parser import parse_and_guard       # noqa: E402  JD：文本 → 结构化 + 护栏
from app.scoring_rules import score_hard         # noqa: E402  硬性条件打分（纯规则）

# ============ 在这里改你要试的输入 ============
RESUME_FILE = ROOT / "tools" / "test_files" / "clean_resume.docx"
# 也可以换成："table_resume.docx" / "text_resume.pdf" / "scan_resume.pdf"（扫描件会被拒）

JD_TEXT = """招聘后端开发工程师（FDE 方向）
3-5 年经验，本科及以上学历。
熟悉 Go 与 MySQL，了解 Redis 优先；有 RAG 或 LLM 落地经验加分。
负责客户现场 AI 交付、订单系统接口开发与性能优化。
"""
# ================================================


def _show_resume(r) -> None:
    print("【简历抽取】", r.file_name)
    print(f"  status   = {r.status}")
    print(f"  name     = {r.data.name if r.data else None}")
    print(f"  years    = {r.data.years if r.data else None}")
    print(f"  skills   = {r.data.skills if r.data else None}")
    print(f"  reasons  = {r.reasons}")
    print(f"  原文长度 = {len(r.raw_text)} 字   成本 = {r.cost_yuan:.8f} 元")


def _show_job(j) -> None:
    d = j.data
    print()
    print("【JD 抽取】")
    print(f"  status       = {j.status}   reasons={j.reasons}")
    if d:
        print(f"  title        = {d.title}")
        print(f"  年限要求     = {d.required_years_min} ~ {d.required_years_max}")
        print(f"  学历要求     = {d.education}")
        print(f"  must_have    = {d.must_have}")
        print(f"  nice_to_have = {d.nice_to_have}")
        print(f"  responsibilities = {d.responsibilities}")
    print(f"  成本 = {j.cost_yuan:.8f} 元")


def _show_score(h) -> None:
    print()
    print("【硬性条件打分】总分 40 分口径")
    print(f"  得分 = {h.score} / 40")
    for k, v in h.breakdown.items():
        print(f"    - {k:10s} {v}")
    if h.deductions:
        print("  扣分明细：")
        for d in h.deductions:
            print(f"    [{d.severity:6s}] -{d.deduct:<2} {d.item}  ({d.type})")
            print(f"              JD 依据：{d.jd_evidence}")
            print(f"              简历现状：{d.resume_evidence}")
    else:
        print("  无扣分")

    total = sum(d.deduct for d in h.deductions)
    print(f"  —— 校验：扣分合计 {total} + 得分 {h.score} = {total + h.score}（应为 40）")


async def main() -> int:
    print("=" * 60)
    print("简历 × 岗位 硬性条件打分试用（S1+S2+S3，未含语义分与 LLM 复核）")
    print("=" * 60)

    if not RESUME_FILE.exists():
        print(f"找不到简历文件：{RESUME_FILE}")
        return 1

    resume = await parse_resume_file(RESUME_FILE.name, RESUME_FILE.read_bytes())
    _show_resume(resume)
    if resume.data is None:
        print("简历没抽出结构化数据，后面打不了分（status=", resume.status, "）")
        return 1

    job = await parse_and_guard(JD_TEXT)
    _show_job(job)
    if job.data is None:
        print("JD 没抽出结构化数据，后面打不了分（status=", job.status, "）")
        return 1

    hard = score_hard(resume.data, job.data, resume.raw_text)
    _show_score(hard)

    print()
    print(f"【成本合计】{(resume.cost_yuan + job.cost_yuan):.8f} 元（硬性打分本身不花钱）")
    print("说明：完整功能还会加 语义相近 30 分（S4）+ LLM 复核 30 分（S5）+ PDF 报告（S7）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
