"""只读真实输入，经 HTTP 验收，持久化脱敏 apply 响应与聚合结果。"""

from collections import Counter
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch
from urllib.parse import urlsplit

import pdfplumber
from docx import Document
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from app import api, fallback, report_store


def main():
    """不改写模型响应，不复制原件；打印内容只含状态、费用和脱敏响应。"""
    output = Path(__file__).resolve().parent
    resume = Path(sys.argv[1])
    jd = Path(sys.argv[2])
    raw = resume.read_bytes()
    jd_text = "\n".join(paragraph.text for paragraph in Document(jd).paragraphs if paragraph.text.strip())
    costs, logs = [], StringIO()
    provider = fallback._run_providers_async

    async def metered(*args, **kwargs):
        """按真实供应商 UsageMeter 计量，包括护栏拒绝的响应。"""
        result = await provider(*args, **kwargs)
        costs.append(result[1].cost())
        return result

    started = perf_counter()
    with TemporaryDirectory(prefix="m9c_real_") as directory, ExitStack() as stack:
        hosts = ",".join(filter(None, (urlsplit(item["base_url"]).hostname for item in fallback.PROVIDERS)))
        no_proxy = ",".join(filter(None, (os.environ.get("NO_PROXY", ""), "127.0.0.1,localhost", hosts)))
        stack.enter_context(patch.dict(os.environ, {"NO_PROXY": no_proxy, "no_proxy": no_proxy}))
        stack.enter_context(patch.object(report_store, "REPORT_DIR", Path(directory)))
        stack.enter_context(patch.dict(api._review_sessions, {}, clear=True))
        stack.enter_context(patch.object(fallback, "_run_providers_async", metered))
        stack.enter_context(patch("tempfile.SpooledTemporaryFile.rollover", side_effect=AssertionError("原件不落盘")))
        client = stack.enter_context(TestClient(api.app, raise_server_exceptions=False))
        with redirect_stdout(logs), redirect_stderr(logs):
            review = client.post("/revise/review", files={"file": (resume.name, raw)},
                data={"job_title": "AI 业务落地专家", "jd_text": jd_text})
        if review.status_code != 200:
            raise RuntimeError(f"review HTTP {review.status_code}")
        payload = review.json()
        with redirect_stdout(logs), redirect_stderr(logs):
            response = client.post("/revise/apply", json={"review_id": payload["review_id"]})
        if response.status_code != 200:
            raise RuntimeError(f"apply HTTP {response.status_code}")
        applied = response.json()
        pdf = client.get(applied["review_pdf_url"])
        if pdf.status_code != 200:
            raise RuntimeError(f"report download HTTP {pdf.status_code}")
        with pdfplumber.open(BytesIO(pdf.content)) as document:
            text = "".join("".join(page.extract_text() or "" for page in document.pages).split())
            pages = len(document.pages)
        if not all(title in text for title in ("8能力补强建议", "9涨分策略")):
            raise AssertionError("真实报告缺少第8/9章")
        if any(not row.get("issue_id") or not row.get("reason") for row in applied["skipped"]):
            raise AssertionError("skipped 缺少编号或原因")
        if not applied["applied"] and (applied["resume_url"] is not None or applied["resume_ext"] is not None):
            raise AssertionError("零改写仍提供简历下载")
        # 完整保留响应字段，仅遮盖可能包含身份、公司和联系方式的定位及原句。
        safe = json.loads(json.dumps(applied))
        for row in [*safe["applied"], *safe["skipped"]]:
            for key in ("before", "after", "location"):
                if key in row:
                    row[key] = "[已脱敏]"
        (output / "real_apply_redacted.json").write_text(json.dumps(safe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary = {"review_status": review.status_code, "apply_status": response.status_code,
            "resume_bytes": len(raw), "jd_chars": len(jd_text), "issues": len(payload["report"]["issues"]),
            "categories": dict(Counter(row["category"] for row in payload["report"]["issues"])),
            "applied": len(applied["applied"]), "skipped": len(applied["skipped"]),
            "skip_reasons": dict(Counter(row["reason"] for row in applied["skipped"])),
            "review_pdf_download": pdf.status_code, "review_pdf_bytes": len(pdf.content), "pdf_pages": pages,
            "chapter_8_9_present": True, "resume_url_is_null": applied["resume_url"] is None,
            "cost_yuan": round(sum(costs), 8), "model_responses": len(costs), "elapsed_seconds": round(perf_counter() - started, 3)}
        # PDF 含真实经历，只在内存验证，不作为脱敏交付落盘。
        (output / "real_case_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(json.dumps(safe, ensure_ascii=False, indent=2))
    if raw != resume.read_bytes():
        raise AssertionError("原简历字节变化")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
