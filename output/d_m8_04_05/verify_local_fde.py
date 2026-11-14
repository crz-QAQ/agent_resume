"""使用真实本地重排器验证 FDE 长句，不请求生成式模型或下载任何权重。"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from app.learning_resources import build_learning_plan
from app.schemas_revision import DiagnosisReport


def main():
    """本地模型初始化与推理均禁网；同时生成含新材料和合并时间线的 QA 报告。"""
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"}))
        for name in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo"):
            stack.enter_context(patch(name, side_effect=AssertionError("本地验收禁止联网")))
        from app.rag import _get_reranker
        _get_reranker()
        gaps = ["RAG", "效果评测", "需求梳理与拆解", "Prompt 工程", "Agent", "知识库", "交付", "负责知识库搭建与效果评测"]
        diagnosis = DiagnosisReport(findings=[], keyword_coverage=[], missing_keywords=gaps, completeness={}, meta={})
        plan = build_learning_plan(diagnosis=diagnosis, hard=None, sem=None, job_title="AI 业务落地专家", semantic_fallback=True)
        result = {"gaps": [{"gap": row.gap, "materials": [item.id for item in row.materials]} for row in plan.gaps],
                  "unmatched": plan.unmatched, "meta": plan.meta, "network_calls": 0, "generative_model_calls": 0}
        assert plan.meta["semantic_attempts"] > 0 and plan.meta["semantic_fallback"] == "used"
        assert not plan.meta["semantic_errors"] and plan.meta["cost_yuan"] == 0
        assert len([row for row in plan.gaps if row.materials]) >= 3
        output = Path(__file__).resolve().parent
        (output / "local_fde_check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        from tools.check_revision_pdf import _inputs, _zero_delta, _uplift
        from app.schemas_revision import DiagnosisFinding
        from app.revision_pdf import render_review_pdf
        report, report_diagnosis, hard = _inputs()
        dates = ["2024.04-2024.08", "2021.07-2024.08", "2020.01-2021.06", "2019.01-2020.02"]
        report_diagnosis.findings = [DiagnosisFinding(code="timeline_overlap", severity="medium", location=f"第{i}段",
            evidence=value + " 某公司经历", detail="上游时间矛盾") for i, value in enumerate(dates, 1)]
        delta = _zero_delta().model_copy(update={"notes": ["硬性 0：词句改写不影响年限/学历", "语义 0：覆盖条数未变"]})
        data = render_review_pdf(report, report_diagnosis, job_title="AI 业务落地专家", resume_name="合成排版验收简历",
            hard=hard, delta=delta, learning_plan=plan, uplift_plan=_uplift())
        target = ROOT / "output/pdf/d_m8_04_05/FDE_材料与报告文案验收.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
