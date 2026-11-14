"""从已验收 M3 意见和 M9a 真实输入离线重放，生成含固定第 8、9 章的 PDF。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
from time import perf_counter
from types import SimpleNamespace

# 该脚本只复用历史产物；在导入业务模块之前禁止模型模块和网络。
ROOT = Path(__file__).resolve().parents[2]  # 从产物脚本位置回到仓库，不依赖启动目录。
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
sys.dont_write_bytecode = True


class DenyModels:
    """禁止为了出 PDF 而导入模型客户端或本地推理模块。"""

    def find_spec(self, fullname, path=None, target=None):
        """拦截模型依赖，其余模块沿用正常导入机制。"""
        if fullname.split(".")[0] in {"openai", "fastembed", "onnxruntime", "dotenv"} or fullname in {
            "app.rag", "app.scoring_semantic", "app.fallback", "app.revision_llm"
        }:
            raise AssertionError(f"离线 PDF 禁止模型导入：{fullname}")  # 不允许客户端初始化副作用。


def deny_network(*args, **kwargs):
    """任何 DNS 或 socket 连接都视为离线重放失败。"""
    raise AssertionError("离线 PDF 禁止网络")


sys.meta_path.insert(0, DenyModels())
socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
socket.getaddrinfo = deny_network

import pdfplumber

from app.guard_revision import find_original
from app.learning_resources import build_learning_plan
from app.revision_pdf import render_review_pdf
from app.schemas_match import Deduction, JobInfo
from app.schemas_revision import DiagnosisReport, RevisionReport, ScoreDelta
from app.scoring_rules import HardScore
from app.uplift_plan import apply_score_impact, build_uplift_plan

# 路径都在既有本地验收资料中，不重新解析简历或运行语义评分模型。
started = perf_counter()
source_path = ROOT / "output/m9a/M9a_UpliftPlan_输入依据.json"
review_path = ROOT / "docs/实施记录/简历修改/M3_真实意见.json"
source = json.loads(source_path.read_text(encoding="utf-8"))  # 真实评分的历史输入快照。
report = RevisionReport.model_validate(json.loads(review_path.read_text(encoding="utf-8"))[1])  # 同一 JD 的既有意见。
job = JobInfo.model_validate(source["job"])
diagnosis = DiagnosisReport.model_validate(source["diagnosis"])  # 不为示例重新生成缺口词表。
hard = HardScore(**{**source["hard"], "deductions": [Deduction(**row) for row in source["hard"]["deductions"]]})
semantic = SimpleNamespace(**{**source["semantic"], "per_item": [SimpleNamespace(**row) for row in source["semantic"]["per_item"]]})

# 必须是同一份 JD，不能把另一岗位的历史意见配上当前分数。
assert report.profile.must_have == job.must_have
assert report.profile.responsibilities == job.responsibilities
with pdfplumber.open(ROOT / source["resume_file"]) as document:
    resume_text = "\n".join(page.extract_text() or "" for page in document.pages)
omitted = [issue.id for issue in report.issues if not find_original(resume_text, issue.original_text)]  # 留下排除审计。
report.issues = [issue for issue in report.issues if find_original(resume_text, issue.original_text)]
assert report.issues, "真实源 PDF 没有可锚定的历史意见"

# 四条意见的原句已在真实文件中复核；无法锚定的历史样例明确审计而非冒充本次有效意见。
uplift = build_uplift_plan(hard=hard, sem=semantic, llm_score=source["llm_score"],
                           job=job, diagnosis=diagnosis, llm_estimated=source["llm_estimated"])
learning = build_learning_plan(diagnosis=diagnosis, hard=hard, sem=semantic, job_title=job.title)  # 语义兜底默认关闭。
report.issues = apply_score_impact(report.issues, uplift)  # 复用已验收回填，不在 PDF 中计算影响。

# 没有采用任何新改写，前后使用同一份上游分数；LLM 缺测继续明确标注。
parts = dict(hard=hard.score, semantic=semantic.score, llm=source["llm_score"] or 0)  # 缺 LLM 同时保留缺测标记。
total = sum(parts.values())  # 仅汇总快照中现成的分项，未调用任何评分器。
covered = sum(row.covered for row in semantic.per_item)
delta = ScoreDelta(before_total=total, after_total_estimate=total,
                   before_parts=parts, after_parts=dict(parts), delta_parts=dict(hard=0, semantic=0, llm=0),
                   covered_before=covered, covered_after=covered, estimated_fields=["llm"],
                   notes=["本次仅复用既有意见与评分，未采用新的改写；LLM 复核分未取到，改前总分不含 LLM 项"])
report.adaptations = []
report.score_before = report.score_after_estimate = total
historical_meta = dict(report.meta)  # 历史模型费用与耗时另存，不冒充本次新调用。
stamp = datetime.now(timezone.utc).isoformat()  # 本次排版真实时间，由调用方显式传入。
report.meta = {**report.meta, "model": "offline-cached-M3", "cost_yuan": 0,
               "elapsed_ms": round((perf_counter() - started) * 1000, 3),
               "elapsed_scope": "读取历史输入与纯规则计划，不含 PDF 排版",
               "llm_estimated": source["llm_estimated"], "generated_at": stamp}

# 保存传给渲染器的同一份对象，数字和链接均可从真实输入及材料库回查。
out = ROOT / "output/pdf/m8b_m9b/简历修改意见_真实输入_能力补强与涨分策略.pdf"
out.parent.mkdir(parents=True, exist_ok=True)
render_started = perf_counter()
data = render_review_pdf(report, diagnosis, job_title=job.title, resume_name=source["resume_file"], hard=hard,
                         generated_at=stamp, delta=delta, learning_plan=learning, uplift_plan=uplift)
render_ms = (perf_counter() - render_started) * 1000  # 实测排版耗时，不拿固定值代替。
out.write_bytes(data)  # 交付和后续读回使用同一份 bytes。
evidence = dict(source_resume=source["resume_file"], source_scores=str(source_path.relative_to(ROOT)),
                source_reviews=str(review_path.relative_to(ROOT)), source_review_index=1,
                omitted_unanchored_issue_ids=omitted, historical_review_meta=historical_meta,
                report=report.model_dump(), diagnosis=diagnosis.model_dump(), hard=source["hard"],
                semantic=source["semantic"], delta=delta.model_dump(), learning_plan=learning.model_dump(),
                uplift_plan=uplift.model_dump(), model_calls=0, network_calls=0, cost_yuan=0)
(ROOT / "output/m8b_m9b/real_pdf_inputs.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
with pdfplumber.open(out) as document:
    pages = len(document.pages)  # 从实际 PDF 结构计页，不按输入长度估算。
    text = "".join("".join((page.extract_text() or "").split()) for page in document.pages)
assert text.index("8能力补强建议") < text.index("9涨分策略")
assert f"最多可加{uplift.achievable_if_true}分；这只是上限，不作为承诺" in text
print(f"真实 PDF：{out}\n{pages} 页 / {len(data)} 字节 / {render_ms:.2f}ms；意见原句 {len(report.issues)} 条已锚定")
print(f"学习缺口 {len(learning.gaps)} 条；未匹配 {len(learning.unmatched)} 条；可达上限 {uplift.achievable_if_true} 分")
print(f"历史意见未锚定而省略的编号：{omitted}；模型调用 0；网络调用 0；成本 0 元")
