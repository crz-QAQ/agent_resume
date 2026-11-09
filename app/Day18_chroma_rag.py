"""Day 18：把 Day 13 的 chunk 与向量写入本地 Chroma 集合。"""

from __future__ import annotations

import statistics
import sys
import time
from collections.abc import Iterable
from pathlib import Path

import chromadb
import numpy as np
from chromadb.api.models.Collection import Collection
from chromadb.config import Settings

import Day13_rag
from Day13_rag import Chunk


COLLECTION_METADATA = {"hnsw:space": "cosine"}
# 绝对不能写相对路径 "练习/.chroma"：PyCharm 和命令行的工作目录不同，
# 相对路径会按各自工作目录解析，最终生成两个不同的向量库。
DEFAULT_PATH = Path(__file__).resolve().parent / ".chroma"


def _normalize_rows(embeddings: np.ndarray) -> np.ndarray:
    """按行执行 L2 归一化，使余弦相似度可直接由点积得到。"""
    matrix = np.asarray(embeddings, dtype=np.float32)
    if matrix.ndim != 2:
        raise ValueError(f"embeddings 必须是二维矩阵，实际形状为 {matrix.shape}")

    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.maximum(norms, 1e-12)


def _collection_matches(
    collection: Collection,
    ids: list[str],
    metadatas: list[dict[str, str]],
    documents: list[str],
) -> bool:
    """判断集合是否已完整保存本次输入，避免无变化时重复写入。"""
    existing = collection.get(
        ids=ids,
        include=["metadatas", "documents"],
    )
    existing_ids = existing.get("ids", [])
    if len(existing_ids) != len(ids) or set(existing_ids) != set(ids):
        return False

    existing_metadatas = dict(
        zip(existing_ids, existing.get("metadatas") or [])
    )
    existing_documents = dict(
        zip(existing_ids, existing.get("documents") or [])
    )
    return all(
        existing_metadatas.get(chunk_id) == metadata
        and existing_documents.get(chunk_id) == document
        for chunk_id, metadata, document in zip(ids, metadatas, documents)
    )


def build_collection(
    chunks: Iterable[Chunk],
    path: str | Path | None = None,
    name: str = "fde_chunks",
) -> Collection:
    """把全部 chunk 灌进本地持久化集合，重复调用沿用稳定 ID 覆盖。"""
    chunk_list = list(chunks)
    target = Path(path) if path is not None else DEFAULT_PATH
    client = chromadb.PersistentClient(
        path=target,
        settings=Settings(anonymized_telemetry=False),
    )
    collection = client.get_or_create_collection(
        name=name,
        metadata=COLLECTION_METADATA,
        embedding_function=None,
    )

    # 确保复用已有集合时仍采用余弦空间，避免默认 L2 距离混入。
    if collection.metadata != COLLECTION_METADATA:
        collection.modify(metadata=COLLECTION_METADATA)

    if not chunk_list:
        return collection

    ids = [f"c{idx:04d}" for idx in range(len(chunk_list))]
    metadatas = [
        {"source": chunk.source, "heading": chunk.heading}
        for chunk in chunk_list
    ]
    documents = [chunk.text for chunk in chunk_list]

    # 完全相同的数据已落库时跳过写入，避免连续写入与 Chroma 后台
    # compaction 产生竞态；但仍要检查残留 ID，不能在这里直接返回。
    if not _collection_matches(collection, ids, metadatas, documents):
        _model, embeddings = Day13_rag.build_vector_index(chunk_list)
        normalized_embeddings = _normalize_rows(embeddings)
        collection.upsert(
            ids=ids,
            embeddings=normalized_embeddings,
            metadatas=metadatas,
            documents=documents,
        )

    # upsert 只覆盖同 ID、不删除额外 ID；add_one_chunk 之后重建时 c0424
    # 会一直残留，因此必须显式删除不属于当前 chunks 的稳定 ID。
    existing = set(collection.get(include=[])["ids"])
    stale = existing - set(ids)
    if stale:
        collection.delete(ids=sorted(stale))
    return collection


def query_chunks(
    question: str,
    k: int = 5,
    source_prefix: str | None = None,
) -> list[tuple[Chunk, float]]:
    """返回 (Chunk, 余弦相似度) top-k。"""
    if k <= 0:
        return []

    if Day13_rag._vector_model is None:
        chunks = Day13_rag.load_chunks()
        Day13_rag._vector_model, _ = Day13_rag.build_vector_index(chunks)
    vector_model = Day13_rag._vector_model

    question_embedding = np.asarray(
        next(iter(vector_model.embed([question]))),
        dtype=np.float32,
    )
    question_norm = float(np.linalg.norm(question_embedding))
    if question_norm == 0:
        return []
    question_embedding = question_embedding / question_norm

    client = chromadb.PersistentClient(
        path=DEFAULT_PATH,
        settings=Settings(anonymized_telemetry=False),
    )
    collection = client.get_collection(
        name="fde_chunks",
        embedding_function=None,
    )

    where = None
    if source_prefix is not None:
        stored = collection.get(include=["metadatas"])
        sources = {
            metadata["source"]
            for metadata in stored.get("metadatas") or []
            if metadata is not None
            and metadata["source"].startswith(source_prefix)
        }
        if sources:
            where = {"source": {"$in": sorted(sources)}}

    result = collection.query(
        query_embeddings=[question_embedding],
        n_results=k,
        include=["metadatas", "documents", "distances"],
        where=where,
    )
    metadatas = result["metadatas"][0] or []
    documents = result["documents"][0] or []
    distances = result["distances"][0] or []
    return [
        (
            Chunk(
                source=metadata["source"],
                heading=metadata["heading"],
                text=document,
            ),
            1.0 - float(distance),
        )
        for metadata, document, distance in zip(
            metadatas,
            documents,
            distances,
        )
    ]


def add_one_chunk(chunk: Chunk) -> int:
    """增量追加 1 条，返回追加后的 count。"""
    client = chromadb.PersistentClient(
        path=DEFAULT_PATH,
        settings=Settings(anonymized_telemetry=False),
    )
    collection = client.get_collection(
        name="fde_chunks",
        embedding_function=None,
    )
    chunk_id = f"c{collection.count():04d}"

    if Day13_rag._vector_model is None:
        chunks = Day13_rag.load_chunks()
        Day13_rag._vector_model, _ = Day13_rag.build_vector_index(chunks)
    vector_model = Day13_rag._vector_model

    document = f"{chunk.heading}\n{chunk.text}"
    embedding = np.asarray(
        next(iter(vector_model.embed([document]))),
        dtype=np.float32,
    )
    normalized_embedding = _normalize_rows(
        np.asarray([embedding], dtype=np.float32)
    )[0]
    collection.upsert(
        ids=[chunk_id],
        embeddings=[normalized_embedding],
        metadatas=[{"source": chunk.source, "heading": chunk.heading}],
        documents=[document],
    )
    return collection.count()


def query_latency(question: str, rounds: int = 20) -> float:
    """同一条 query 跑 rounds 次，返回中位数毫秒。"""
    if rounds <= 0:
        return 0.0

    # 首次调用会加载 BGE 模型；空跑结果丢弃，避免冷启动污染延迟。
    query_chunks(question)

    samples: list[float] = []
    for _ in range(rounds):
        started_at = time.perf_counter()
        query_chunks(question)
        samples.append((time.perf_counter() - started_at) * 1000)
    return float(statistics.median(samples))


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    chunks = Day13_rag.load_chunks()
    chunk_count = len(chunks)
    print(f"load_chunks() = {chunk_count}")

    first_collection = build_collection(chunks)
    first_count = first_collection.count()
    print(f"第一次 build 后 count = {first_count}")

    second_collection = build_collection(chunks)
    second_count = second_collection.count()
    print(f"第二次 build 后 count = {second_count}")

    if first_count != chunk_count or second_count != chunk_count:
        raise AssertionError(
            "集合数量与运行时 load_chunks() 数量不一致："
            f"{first_count}, {second_count}, {chunk_count}"
        )
    if second_collection.metadata != COLLECTION_METADATA:
        raise AssertionError(
            f"集合 metadata 不符合预期：{second_collection.metadata}"
        )


if __name__ == "__main__":
    main()
