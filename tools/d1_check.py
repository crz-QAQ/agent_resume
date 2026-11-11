"""D1 本地链路自检：PyCharm 右键 Run 即可，全过 exit=0。

用途：D1 任务书里「本地部署」那一半的 5 条判据，一次跑完并打印证据。
前置：本地服务已起（tools/start_server.bat），端口 8000 有响应。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

# 让脚本不依赖 PyCharm 的工作目录：项目根 = tools/ 的上一层
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8000"
# 被测样例用 Go 后端简历，和 S7b 验收用的是同一份，结果才有可比性
SAMPLE = ROOT / "tools" / "test_files" / "go_backend_resume.docx"
# JD 少于 20 字会被接口按 422 拒绝，这里给一段真实岗位描述
JD = (
    "后端开发工程师，负责业务系统服务端开发。"
    "要求熟悉 Go 或 Python，熟悉 MySQL、Redis、消息队列，"
    "有接口设计和线上问题排查经验，3 年以上开发经验。"
)

rows: list[tuple[bool, str, str]] = []


def record(ok: bool, name: str, detail: str) -> None:
    """把一条判据的结果存下来，最后统一打印并决定退出码。"""
    rows.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name:<22} {detail}")


def check_imports() -> None:
    """D1 第三节：4 个遗留模块的扁平 import 必须全部能导入。"""
    import importlib

    for mod in (
        "app.api",
        "app.check_agent",
        "app.check_vector_store",
        "app.langchain_agent",
        "app.langchain_extract",
    ):
        try:
            importlib.import_module(mod)
        except Exception as exc:  # noqa: BLE001 - 自检脚本要把任何异常都报出来
            record(False, f"import {mod}", f"{type(exc).__name__}: {exc}")
        else:
            record(True, f"import {mod}", "ok")


def check_docs(client: httpx.Client) -> None:
    """D1 验收第 3 条的本地版：/docs 必须 200，否则说明服务没起或路由没挂。"""
    resp = client.get("/docs")
    record(resp.status_code == 200, "GET /docs", f"status={resp.status_code}")


def check_score(client: httpx.Client) -> str:
    """真打一次 /match/score，返回 report_id 供下一步下载 PDF。"""
    with SAMPLE.open("rb") as fh:
        files = {
            "file": (
                SAMPLE.name,
                fh,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        }
        started = time.time()
        resp = client.post("/match/score", files=files, data={"jd_text": JD}, timeout=180)
    cost = time.time() - started
    if resp.status_code != 200:
        record(False, "POST /match/score", f"status={resp.status_code} body={resp.text[:120]}")
        return ""
    body = resp.json()
    report = body.get("report") or {}
    total = (report.get("score") or {}).get("total")
    record(
        True,
        "POST /match/score",
        f"total={total} verdict={report.get('verdict')} status={report.get('status')} {cost:.1f}s",
    )
    return body.get("report_id", "")


def check_report(client: httpx.Client, report_id: str) -> None:
    """D1 验收第 5 条的本地版：报告要能下载，且是真 PDF（首字节 %PDF）。"""
    if not report_id:
        record(False, "GET /match/report", "上一步没拿到 report_id")
        return
    resp = client.get(f"/match/report/{report_id}", timeout=60)
    head = resp.content[:4]
    ok = resp.status_code == 200 and head == b"%PDF" and len(resp.content) > 20000
    record(
        ok,
        "GET /match/report",
        f"status={resp.status_code} head={head!r} bytes={len(resp.content)}",
    )


def main() -> int:
    print(f"目标服务 {BASE}  样例 {SAMPLE.name}\n")
    check_imports()
    try:
        with httpx.Client(base_url=BASE, timeout=30) as client:
            check_docs(client)
            check_report(client, check_score(client))
    except httpx.ConnectError:
        record(False, "连接服务", f"{BASE} 无响应 → 先双击 tools/start_server.bat")
    except httpx.HTTPError as exc:
        record(False, "HTTP 请求", f"{type(exc).__name__}: {exc}")

    failed = [r for r in rows if not r[0]]
    print(f"\n合计 {len(rows)} 条，失败 {len(failed)} 条")
    if failed:
        print("未通过：" + "、".join(r[1] for r in failed))
        return 1
    print("本地链路全通 → 可以进 Docker 那一半")
    return 0


if __name__ == "__main__":
    sys.exit(main())
