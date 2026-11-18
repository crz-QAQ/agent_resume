"""离线回归 D-N2-06：B 全不达标不能让进程失败，A 失败必须返回非零。"""

from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.providers import UsageMeter
from tools import check_interview_llm as checker


async def main() -> int:
    destination = ROOT / "reports/d_n2_05_06/reporting_regression"
    destination.mkdir(parents=True, exist_ok=True)

    async def template_runs(sk):
        """真实执行生成入口，只有传输层返回固定空数组，不发生网络请求。"""
        runs = []
        meter = UsageMeter()
        for _ in range(3):
            plan, cost, calls = await checker._mock_plan(sk, ["[]"])
            assert calls == 1
            runs.append((plan, cost))
            meter.add(SimpleNamespace(prompt_tokens=10, completion_tokens=10, prompt_cache_hit_tokens=0))
        return runs, dict(calls=3, meter=meter, raw=["[]"] * 3, errors=[])

    output = StringIO()
    with patch.object(checker, "ROOT", destination), patch.object(checker, "_live_runs", new=template_runs), redirect_stdout(output):
        exit_code = await checker.main()
    report = output.getvalue()
    assert exit_code == 0 and "通过 A 15/15（确定性）；B 0/5（模型质量，仅供参考）" in report, report
    diagnostics = json.loads((destination / "reports/n2b/warnings.json").read_text(encoding="utf-8"))
    assert len(diagnostics["warnings"]) == 15
    assert set(diagnostics["B"]) == set(checker._B_NAMES)
    assert all(not result[0] for result in diagnostics["B"].values())
    for index in range(1, 4):
        plan = json.loads((destination / f"reports/n2b/interview_plan_{index}.json").read_text(encoding="utf-8"))
        assert any(warning.startswith("B05 ") for warning in plan["meta"]["warnings"])
    (destination / "b_zero_exit_zero.txt").write_text(report, encoding="utf-8")

    original_cases = checker._deterministic_cases

    async def one_hard_failure(sk):
        results = await original_cases(sk)
        results["A01"] = (False, "构造：一个确定性硬检查失败")
        return results

    output = StringIO()
    with patch.object(checker, "ROOT", destination), patch.object(checker, "_live_runs", new=template_runs), \
         patch.object(checker, "_deterministic_cases", new=one_hard_failure), redirect_stdout(output):
        exit_code = await checker.main()
    assert exit_code == 1 and "通过 A 14/15（确定性）；B 0/5（模型质量，仅供参考）" in output.getvalue()
    (destination / "a_failure_exit_one.txt").write_text(output.getvalue(), encoding="utf-8")
    print("通过：固定空数组产物令 B 0/5，exit=0；15 条质量 warnings 已打印并写入文件及题册。")
    print("通过：构造 A01 失败，A 14/15，exit=1；B 类五项均保留。真实网络调用 0 次。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
