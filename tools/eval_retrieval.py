"""Day 13 评测脚本：8 题 × 3 种检索方式，产出可直接贴简历的对比表。

跑法：cd 练习 && HF_ENDPOINT=https://hf-mirror.com ../.venv/Scripts/python.exe ../工具/eval_retrieval.py
"""

import os

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import rag as R  # noqa: E402
# RAG通过包路径载入，工具换cwd也不会加载另一个同名模块。
# 业务模块不会从脚本目录寻找密钥或语料文件。

# (问题, 正确答案可能出现的文件前缀列表, 期望关键词)
CASES = [
    ("Day 9 的降级顺序是什么？", ["教材/Day9"], "429"),
    ("空壳成功的定义是什么？", ["任务/Day11", "教材/Day1-10"], "空壳"),
    ("预算护栏是什么？", ["教材/Day9"], "预算"),
    ("五层检查是哪五层？", ["教材/Day1-10"], "五层"),
    ("系统 prompt 有多少 token？", ["教材/Day1-10"], "304"),
    ("相对时间为什么不能进 repair？", ["失败样本", "任务/Day11"], "相对时间"),
    ("三个静默失败是哪三个？", ["教材/怎么看运行结果", "教材/Day1-10"], "空壳"),
    ("Dockerfile 里 WORKDIR 为什么要设成 /app/练习？", ["任务/Day12"], "WORKDIR"),
]


def main() -> int:
    """运行既有三类检索基线，输出各题命中情况与汇总。"""
    # 使用项目包中的同一RAG模块，避免工具与API各持有一套索引状态。
    # 以下评测保持原有题集和判断方式，不参与S7b简历评分。
    chunks = R.load_chunks()
    print(f"语料: {len(chunks)} 个 chunk，{len({c.source for c in chunks})} 个文件")

    t = time.time()
    # 构建顺序保持文档列表与分词、向量矩阵的下标对应。
    R._indexed_chunks = chunks
    R._bm25, R._tokenized_chunks = R.build_index(chunks)
    R._vector_model, R._document_embeddings = R.build_vector_index(chunks)
    print(f"索引构建: {time.time()-t:.1f}s\n")

    modes = [
        # 各模式沿用既有函数名和召回参数，包式导入不改变评测口径。
        ("BM25 字面", "retrieve_bm25"),
        ("向量 bge-zh", "retrieve"),
        ("混合 RRF", "retrieve_hybrid"),
    ]

    table = {}
    # 每种模式独立记录命中数，避免把多路召回结果混成一个数字。
    for label, fn_name in modes:
        fn = getattr(R, fn_name, None)
        # 没有实现的模式明确跳过，不能被统计成成功。
        if fn is None:
            print(f"[跳过] {label}")
            continue
        hits = 0
        # 逐题保留判定和第一条来源，方便与历史基线对照。
        detail = []
        for q, expect_srcs, _kw in CASES:
            # 单题失败不阻止后续题目提供诊断信息。
            try:
                res = fn(q, k=5)
            except Exception as exc:  # noqa: BLE001
                res = []
                print(f"  [{label}] {q} 出错: {exc}")
            ok = any(any(e in c.source for e in expect_srcs) for c, _s in res)
            # 来源必须来自实际检索结果，不能采用生成模型自报引用。
            hits += 1 if ok else 0
            top = res[0][0].source if res else "（空）"
            detail.append((ok, top))
        table[label] = (hits, detail)
        # 保存本轮结果，汇总表只读取这些实际执行记录。
        print(f"{label:<12} {hits}/{len(CASES)}")

    print("\n" + "=" * 78)
    header = f"{'问题':<36}" + "".join(f"{lbl:<14}" for lbl, _ in modes)
    # 题目和每路结果按固定顺序并列展示，保持旧输出可比较。
    print(header)
    print("-" * 78)
    for i, (q, _, _) in enumerate(CASES):
        # 序号与detail下标保持一致，避免错列成另一题的命中。
        row = f"{q[:34]:<36}"
        for label, _ in modes:
            if label in table:
                ok = table[label][1][i][0]
                row += f"{'✓' if ok else '✗':<14}"
        print(row)
    print("=" * 78)
    for label, (hits, _) in table.items():
        # 百分比仅用于评测展示，不写回检索或匹配评分。
        print(f"{label:<12} 命中 {hits}/{len(CASES)}  ({hits/len(CASES)*100:.0f}%)")
    return 0


if __name__ == "__main__":
    # 直接运行仍只执行本工具，不因导入CASES触发模型加载。
    raise SystemExit(main())
