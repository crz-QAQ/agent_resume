"""Day 18 评测：对比 Day 13 基线与 Chroma 的 top-1 检索结果和延迟。"""

from __future__ import annotations

import os

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
# 保留原有镜像设置；用户已经显式配置时不覆盖。

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 加入app包的父目录，命令行启动不再依赖旧的中文目录布局。

from tools.eval_retrieval import CASES  # noqa: E402
# 只复用题目常量，导入时不会运行基线工具的main。

from app import rag as R  # noqa: E402
from app import vector_store as M  # noqa: E402
# 基线与Chroma都从app包加载，复用相同片段类型和编码模型。
# 工具只比较已有检索算法，不接入匹配流水线或修改评分。
# 测试语料继续取自RAG原配置，和上传简历缓存相互独立。
# 评测进程退出后不会给HTTP服务留下替身或全局配置改动。


def _prepare_collection(chunks: list[R.Chunk]) -> M.Collection:
    """集合数量正确时直接复用；缺失或数量不符时才重建。"""
    # 复用换库模块的延迟导入与安装指引，模块加载不触发 Chroma。
    chromadb, Settings = M._require_chromadb()
    client = chromadb.PersistentClient(
        # 向量库仍使用原有模块路径，避免导入整理后新建另一套集合。
        path=M.DEFAULT_PATH,
        settings=Settings(anonymized_telemetry=False),
    )
    try:
        # 已有集合优先复用，重建条件由后续原有校验决定。
        collection = client.get_collection(
            name="fde_chunks",
            embedding_function=None,
        )
    except chromadb.errors.NotFoundError:
        # 集合缺失时保留原有重建路径，不伪造空成功。
        collection = None

    if collection is None or collection.count() != len(chunks):
        collection = M.build_collection(chunks)

    if collection.count() != len(chunks):
        raise AssertionError(
            "Chroma 集合数量与语料不一致："
            f"{collection.count()} != {len(chunks)}"
        )
    return collection


def _measure_base_latency(question: str, rounds: int = 20) -> float:
    """预热一次后测量 Day 13 向量检索的中位数毫秒。"""
    R.retrieve(question, k=5)

    # 两套实现使用同一个问题，避免问题本身影响延迟对比。
    samples: list[float] = []
    for _ in range(rounds):
        started_at = time.perf_counter()
        R.retrieve(question, k=5)
        samples.append((time.perf_counter() - started_at) * 1000)
    return float(statistics.median(samples))


def main() -> int:
    """对照原检索基线和Chroma的top-1与中位延迟。"""
    # 导入变化只统一模块身份，不修改匹配标准或评测题目。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    try:
        M._require_chromadb()
    except RuntimeError as exc:
        # 先检查依赖，缺包时只打印指引并退出，避免下载评测模型。
        print(exc, file=sys.stderr)
        return 1

    chunks = R.load_chunks()
    # 索引共享相同语料列表，保证基线和集合比较有意义。
    R._indexed_chunks = chunks
    R._vector_model, R._document_embeddings = R.build_vector_index(chunks)
    _prepare_collection(chunks)

    matched = 0
    # 基线空结果单独列为N/A，不当作Chroma的准确命中。
    na_list: list[str] = []

    for index, (question, _expected_sources, _keyword) in enumerate(
        # 固定题目顺序与原工具保持一致，便于人工逐项检查。
        CASES,
        start=1,
    ):
        base = R.retrieve(question, k=5)
        # 两路只比较实际返回的来源，不让模型生成结果进入此评测。
        chroma = M.query_chunks(question, k=5)
        chroma_source = chroma[0][0].source if chroma else "（空）"

        if not base:
            # 基线没有可比项时继续下一题，最后单独汇报数量。
            print(
                f"Q{index}  N/A(基线空)   "
                f"chromaTop1={chroma_source}"
            )
            na_list.append(f"Q{index}")
            continue

        base_source = base[0][0].source
        is_match = base_source == chroma_source
        # 该布尔值表示两套检索实现一致性，不表示候选人岗位匹配度。
        mark = "✓" if is_match else "✗"
        print(f"Q{index}  {mark}   基线={base_source}")
        print(f"         chroma={chroma_source}")
        if is_match:
            matched += 1

    latency_question = CASES[0][0]
    # 延迟观测继续使用同一份固定问题，保留原轮数配置。
    base_ms = _measure_base_latency(latency_question)
    chroma_ms = M.query_latency(latency_question)
    print(f"\n基线检索延迟: {base_ms:.2f} ms")
    print(f"Chroma 检索延迟: {chroma_ms:.2f} ms")

    result = f"一致 {matched}/{len(CASES)}"
    # 汇总只采用本轮实际计数，不读取历史评测输出。
    if na_list:
        result += f"   N/A(基线空): {', '.join(na_list)}"
    print(result)

    return 0 if matched + len(na_list) == len(CASES) else 1


if __name__ == "__main__":
    # 从项目根python -m或直接运行脚本均使用app包式导入。
    raise SystemExit(main())
