"""只重跑上次真简历×贝壳 JD 的既存 gaps；本次不重新生成分数或意见。"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True


def main():
    """使用同一个真实本地模型重算材料与审计，明确标注输入来自历史真实 review。"""
    from app.learning_resources import build_learning_plan
    from app.schemas_revision import DiagnosisReport
    from app.scoring_semantic import _get_model

    source = ROOT / "output/d_m8_06/real_learning_plan.json"
    original = json.loads(source.read_text(encoding="utf-8"))
    assert all(row["severity"] in {"high", "medium"} for row in original["gaps"])
    diagnosis = DiagnosisReport(findings=[], keyword_coverage=[], completeness={}, meta={},
        missing_keywords=[row["gap"] for row in original["gaps"] if row["severity"] == "high"])
    sem = SimpleNamespace(per_item=[SimpleNamespace(item=row["gap"], covered=False)
        for row in original["gaps"] if row["severity"] == "medium"])
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"}))
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo"):
            stack.enter_context(patch(target, side_effect=AssertionError("缺口重放禁止联网")))
        _get_model()
        plan = build_learning_plan(diagnosis=diagnosis, hard=None, sem=sem,
                                   job_title="AI 业务落地专家", semantic_fallback=True)
    assert [row.gap for row in plan.gaps] == [row["gap"] for row in original["gaps"]]
    for before, after in zip(original["gaps"], plan.gaps):
        if before["meta"]["matched_by"] in {"family", "keyword"}:
            assert after.meta["matched_by"] == before["meta"]["matched_by"]
            assert [row.id for row in after.materials] == [row["id"] for row in before["materials"]]
    ai = next(row for row in plan.gaps if row.gap == "AI编程工具")
    assert ai.meta["matched_by"] == "none" and not ai.materials and ai.gap in plan.unmatched
    assert ai.meta["rejected_semantic"][0]["id"] == "fde-prompt-engineering"
    output = Path(__file__).resolve().parent
    (output / "real_gaps_replay_plan.json").write_text(plan.model_dump_json(indent=2) + "\n", encoding="utf-8")
    summary = dict(mode="replay_saved_real_review_gaps", source=str(source.relative_to(ROOT)),
        source_review_date="2026-11-14", source_resume_bytes=276107, source_jd_chars=1081,
        gap_count=len(plan.gaps), matched_gaps=sum(bool(row.materials) for row in plan.gaps),
        unmatched=plan.unmatched, rejected_semantic=plan.meta["rejected_semantic"],
        semantic_attempts=plan.meta["semantic_attempts"], cost_yuan=0,
        gaps=[dict(gap=row.gap, matched_by=row.meta["matched_by"],
                   materials=[item.id for item in row.materials],
                   rejected_semantic=row.meta.get("rejected_semantic", [])) for row in plan.gaps])
    (output / "real_gaps_replay_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
