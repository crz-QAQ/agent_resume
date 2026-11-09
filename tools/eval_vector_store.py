"""Day 18 评测：对比 Day 13 基线与 Chroma 的 top-1 检索结果和延迟。"""

import os

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "练习"))

import chromadb  # noqa: E402
from chromadb.errors import NotFoundError  # noqa: E402
from eval_retrieval import CASES  # noqa: E402

import rag as R  # noqa: E402
import vector_store as M  # noqa: E402


def _prepare_collection(chunks: list[R.Chunk]) -> M.Collection:
    """集合数量正确时直接复用；缺失或数量不符时才重建。"""
    client = chromadb.PersistentClient(
        path=M.DEFAULT_PATH,
        settings=M.Settings(anonymized_telemetry=False),
    )
    try:
        collection = client.get_collection(
            name="fde_chunks",
            embedding_function=None,
        )
    except NotFoundError:
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

    samples: list[float] = []
    for _ in range(rounds):
        started_at = time.perf_counter()
        R.retrieve(question, k=5)
        samples.append((time.perf_counter() - started_at) * 1000)
    return float(statistics.median(samples))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    chunks = R.load_chunks()
    R._indexed_chunks = chunks
    R._vector_model, R._document_embeddings = R.build_vector_index(chunks)
    _prepare_collection(chunks)

    matched = 0
    na_list: list[str] = []

    for index, (question, _expected_sources, _keyword) in enumerate(
        CASES,
        start=1,
    ):
        base = R.retrieve(question, k=5)
        chroma = M.query_chunks(question, k=5)
        chroma_source = chroma[0][0].source if chroma else "（空）"

        if not base:
            print(
                f"Q{index}  N/A(基线空)   "
                f"chromaTop1={chroma_source}"
            )
            na_list.append(f"Q{index}")
            continue

        base_source = base[0][0].source
        is_match = base_source == chroma_source
        mark = "✓" if is_match else "✗"
        print(f"Q{index}  {mark}   基线={base_source}")
        print(f"         chroma={chroma_source}")
        if is_match:
            matched += 1

    latency_question = CASES[0][0]
    base_ms = _measure_base_latency(latency_question)
    chroma_ms = M.query_latency(latency_question)
    print(f"\n基线检索延迟: {base_ms:.2f} ms")
    print(f"Chroma 检索延迟: {chroma_ms:.2f} ms")

    result = f"一致 {matched}/{len(CASES)}"
    if na_list:
        result += f"   N/A(基线空): {', '.join(na_list)}"
    print(result)

    return 0 if matched + len(na_list) == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
