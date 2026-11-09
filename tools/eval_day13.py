"""Day 13 评测脚本：8 题 × 3 种检索方式，产出可直接贴简历的对比表。

跑法：cd 练习 && HF_ENDPOINT=https://hf-mirror.com ../.venv/Scripts/python.exe ../工具/eval_day13.py
"""

import os

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "练习"))

import Day13_rag as R  # noqa: E402

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
    chunks = R.load_chunks()
    print(f"语料: {len(chunks)} 个 chunk，{len({c.source for c in chunks})} 个文件")

    t = time.time()
    R._indexed_chunks = chunks
    R._bm25, R._tokenized_chunks = R.build_index(chunks)
    R._vector_model, R._document_embeddings = R.build_vector_index(chunks)
    print(f"索引构建: {time.time()-t:.1f}s\n")

    modes = [
        ("BM25 字面", "retrieve_bm25"),
        ("向量 bge-zh", "retrieve"),
        ("混合 RRF", "retrieve_hybrid"),
    ]

    table = {}
    for label, fn_name in modes:
        fn = getattr(R, fn_name, None)
        if fn is None:
            print(f"[跳过] {label}")
            continue
        hits = 0
        detail = []
        for q, expect_srcs, _kw in CASES:
            try:
                res = fn(q, k=5)
            except Exception as exc:  # noqa: BLE001
                res = []
                print(f"  [{label}] {q} 出错: {exc}")
            ok = any(any(e in c.source for e in expect_srcs) for c, _s in res)
            hits += 1 if ok else 0
            top = res[0][0].source if res else "（空）"
            detail.append((ok, top))
        table[label] = (hits, detail)
        print(f"{label:<12} {hits}/{len(CASES)}")

    print("\n" + "=" * 78)
    header = f"{'问题':<36}" + "".join(f"{lbl:<14}" for lbl, _ in modes)
    print(header)
    print("-" * 78)
    for i, (q, _, _) in enumerate(CASES):
        row = f"{q[:34]:<36}"
        for label, _ in modes:
            if label in table:
                ok = table[label][1][i][0]
                row += f"{'✓' if ok else '✗':<14}"
        print(row)
    print("=" * 78)
    for label, (hits, _) in table.items():
        print(f"{label:<12} 命中 {hits}/{len(CASES)}  ({hits/len(CASES)*100:.0f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
