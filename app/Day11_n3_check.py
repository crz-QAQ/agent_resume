"""Day 11 N3 验收：状态判定到底有没有真的拦住。

在 PyCharm 里用法（最简单）：
    打开本文件 → 右键 → Run 'Day11_n3_check'

为什么用右键 Run 最省事：
    PyCharm 会自动把「本文件所在目录」加入 sys.path ✓
    → 同目录的 Day11_agent / prompts / Day8_n3_structured 都能直接 import ✓
    → 不用管工作目录、不用配 PYTHONPATH ✓

会真实调用模型 4 次，成本约 0.0025 元。
"""

import asyncio

from Day11_agent import parse_resume_agent

# (说明, 输入, 期望 status)
CASES = [
    ("① 正常简历", "张三，3 年后端经验，会 Go 和 PHP", "ok"),
    ("② 学生（未毕业）", "我还没毕业，会一点 Python", "needs_review"),
    ("③ 相对时间（去年）", "赵六，2016 年入行，去年离职，做后端", "needs_review"),
    ("④ 完全无意义文本", "今天天气不错，多云转晴", "needs_review"),
]


async def main() -> int:
    passed = 0
    for name, text, expect in CASES:
        result = await parse_resume_agent(text)
        ok = result.status == expect
        passed += int(ok)
        mark = "✓" if ok else "✗"
        print(f"{mark} {name}")
        print(f"    status={result.status!r}  期望={expect!r}")
        print(f"    steps={len(result.steps)}  cost={result.total_cost_yuan:.6f} 元")
        if result.reasons:
            print(f"    reasons={result.reasons}")
        print()

    total = len(CASES)
    print(f"{passed}/{total} 通过")
    if passed != total:
        print("→ 没有全过，说明 ④ 收口段还没真正影响 status（现在很可能还是硬编码 'ok'）")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
