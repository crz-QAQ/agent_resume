"""N2 段 A 离线验收：真简历 × 贝壳 JD，四条断言且零模型。"""

from __future__ import annotations

from pathlib import Path
import sys
from time import perf_counter

# 支持从任意目录执行，验收不产生字节码文件。
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
# Windows 输出显式使用 UTF-8，保留中文及圈号。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.interview_llm import build_question_messages
from app.interview_skeleton import build_skeleton
# 只复用 N1 真实材料准备函数，不运行其 main，也不导入任何模型入口。
from tools.check_interview_skeleton import load_beike_inputs


def main() -> int:
    """执行四条断言并完整报告；材料缺失或任一断言失败时返回非零。"""
    started = perf_counter()
    passed = 0
    # 逐项记录已通过数量，任何准备或校验异常都会反映在末行总数中。
    try:
        # 沿用同一份外部真实简历与原 JD，不把人工样本冒充真实验收。
        sk = build_skeleton(**load_beike_inputs())
        messages = build_question_messages(sk)
        user_content = messages[1]["content"]
        assert len(sk.slots) == 16, f"实际槽位数：{len(sk.slots)}"
        passed += 1
        print("① 通过：真简历 × 贝壳 JD，len(sk.slots) == 16")

        # 逐字子串判断，保留 basis 的前后空白及内部换行。
        assert all(slot.basis in user_content for slot in sk.slots), "依据原文未逐字进入 user_content"
        passed += 1
        print("② 通过：16 条 slot.basis 逐字出现在 user_content 中（未 strip）")

        # 对同一骨架完整渲染两次，不缓存第一次结果充当确定性验证。
        repeated_messages = build_question_messages(sk)
        assert user_content == repeated_messages[1]["content"], "同一骨架两次渲染不一致"
        passed += 1
        print("③ 通过：同一骨架渲染两次，user_content 逐字相同")

        # 长度包括输出要求；再次确认消息结构及全部原句同时成立。
        assert len(user_content) <= 6000 and len(messages) == 2 and all(
            slot.basis in user_content for slot in sk.slots
        ), "长度、消息条数或依据完整性不合格"
        passed += 1
        print(f"④ 通过：user_content={len(user_content)} 字符 ≤6000；messages=2；16 条依据完整")
    except Exception as error:
        # 缺少真实材料不能跳过或虚报通过；退出码供自动检查识别失败。
        print(f"验收失败：{type(error).__name__}: {error}")
    duration_ms = (perf_counter() - started) * 1000
    # 计时包含原件读取、骨架构建及两次渲染，没有模型成本。
    print(f"通过 {passed}/4；耗时 {duration_ms:.2f}ms；模型调用 0；成本 0 元")
    return 0 if passed == 4 else 1


if __name__ == "__main__":
    raise SystemExit(main())
