"""真实简历与贝壳 JD 走完整 HTTP 链路，只保存不含简历正文的学习计划与聚合取证。"""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch
from urllib.parse import urlsplit

from docx import Document
from fastapi.testclient import TestClient
import pdfplumber

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from app import api, fallback, report_store


def main():
    """保留原始供应商响应与计费，不替换评分、诊断、意见、匹配或模型分数。"""
    output = Path(__file__).resolve().parent
    history_path = output / "real_review_runs.json"
    previous = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else []
    summary_path = output / "real_review_summary.json"
    if not previous and summary_path.exists():
        previous.append(json.loads(summary_path.read_text(encoding="utf-8")))
    resume, jd = Path(sys.argv[1]), Path(sys.argv[2])
    raw = resume.read_bytes()
    jd_text = "\n".join(row.text for row in Document(jd).paragraphs if row.text.strip())
    provider, costs, logs = fallback._run_providers_async, [], StringIO()
    started = perf_counter()

    async def metered(*args, **kwargs):
        """只包装真实费用计量，不改 prompt 或模型内容。"""
        result = await provider(*args, **kwargs)
        costs.append(result[1].cost())
        return result

    with TemporaryDirectory(prefix="m8_06_real_") as directory, ExitStack() as stack:
        hosts = ",".join(filter(None, (urlsplit(row["base_url"]).hostname for row in fallback.PROVIDERS)))
        no_proxy = ",".join(filter(None, (os.environ.get("NO_PROXY", ""), "127.0.0.1,localhost", hosts)))
        stack.enter_context(patch.dict(os.environ, {"NO_PROXY": no_proxy, "no_proxy": no_proxy}))
        stack.enter_context(patch.object(report_store, "REPORT_DIR", Path(directory)))
        stack.enter_context(patch.dict(api._review_sessions, {}, clear=True))
        stack.enter_context(patch.object(fallback, "_run_providers_async", metered))
        stack.enter_context(patch("tempfile.SpooledTemporaryFile.rollover", side_effect=AssertionError("原件不落盘")))
        client = stack.enter_context(TestClient(api.app, raise_server_exceptions=False))
        with redirect_stdout(logs), redirect_stderr(logs):
            response = client.post("/revise/review", files={"file": (resume.name, raw)},
                data={"job_title": "AI 业务落地专家", "jd_text": jd_text})
        if response.status_code != 200:
            raise RuntimeError(f"real review HTTP {response.status_code}")
        payload = response.json()
        plan = payload["learning_plan"]
        assert plan is not None
        assert all(row["meta"]["matched_by"] in {"family", "keyword", "semantic", "none"} for row in plan["gaps"])
        assert plan["meta"]["matched_by"] == {row["gap"]: row["meta"]["matched_by"] for row in plan["gaps"]}
        pdf = client.get(f"/revise/file/{payload['review_id']}/review")
        assert pdf.status_code == 200
        with pdfplumber.open(BytesIO(pdf.content)) as document:
            text = "".join((page.extract_text() or "") for page in document.pages)
        placeholders = text.count("暂无对应材料")
        # 学习计划只含 JD 能力缺口与库内材料，不含姓名、履历与原件字节。
        (output / "real_learning_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary = {"http_status": response.status_code, "input_resume_bytes": len(raw), "jd_chars": len(jd_text),
            "gap_count": len(plan["gaps"]), "matched_gaps": sum(bool(row["materials"]) for row in plan["gaps"]),
            "unmatched": plan["unmatched"], "semantic_attempts": plan["meta"]["semantic_attempts"],
            "semantic_fallback": plan["meta"]["semantic_fallback"], "semantic_errors": plan["meta"]["semantic_errors"],
            "learning_cost_yuan": plan["meta"]["cost_yuan"], "full_review_cost_yuan": round(sum(costs), 8),
            "real_model_responses": len(costs), "elapsed_seconds": round(perf_counter() - started, 3),
            "review_pdf_download": pdf.status_code, "unmatched_material_placeholders": placeholders,
            "gaps": [{"gap": row["gap"], "matched_by": row["meta"]["matched_by"],
                      "materials": [item["id"] for item in row["materials"]]} for row in plan["gaps"]]}
        summary["cumulative_review_cost_yuan"] = round(sum(row["full_review_cost_yuan"] for row in previous) + sum(costs), 8)
        history_path.write_text(json.dumps([*previous, summary], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        assert summary["matched_gaps"] >= 3 and summary["semantic_attempts"] > 0
        assert len(summary["unmatched"]) <= 2 and placeholders <= 3
        assert not summary["semantic_errors"] and summary["learning_cost_yuan"] == 0
        assert resume.read_bytes() == raw


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
