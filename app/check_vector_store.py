"""Day 18 N2/N3：检查检索、增量写入、重建恢复和查询延迟。"""

from __future__ import annotations

import sys

from app import rag
from app.rag import Chunk
from app.vector_store import (
    add_one_chunk,
    build_collection,
    query_chunks,
    query_latency,
)


QUESTION = "Day 9 的降级顺序是什么？"
SOURCE_PREFIX = "docs/教材"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    results = query_chunks(QUESTION)
    prefix_results = query_chunks(QUESTION, source_prefix=SOURCE_PREFIX)

    try:
        assert len(results) == 5, f"top-k 条数不是 5：{len(results)}"

        scores = [score for _chunk, score in results]
        assert (
            all(0.0 <= score <= 1.0 for score in scores)
            and all(
                previous_score > next_score
                for previous_score, next_score in zip(scores, scores[1:])
            )
        ), f"分数越界或未严格降序：{scores}"

        assert (
            len(prefix_results) == 5
            and all(
                chunk.source.startswith(SOURCE_PREFIX)
                for chunk, _score in prefix_results
            )
        ), f"来源前缀过滤失败：{[chunk.source for chunk, _ in prefix_results]}"

        added_count = add_one_chunk(
            Chunk(
                source="练习/check_vector_store.py",
                heading="N3 增量写入检查",
                text="用于验证 add_one_chunk 后 count 为 425 的临时条目。",
            )
        )
        assert added_count == 425, f"增量写入后 count 不是 425：{added_count}"

        restored_count = build_collection(rag.load_chunks()).count()
        assert restored_count == 424, f"重建后 count 未恢复到 424：{restored_count}"

        latency_ms = query_latency(QUESTION)
        assert (
            isinstance(latency_ms, float)
            and 0 < latency_ms < 500
        ), f"查询延迟不符合预期：{latency_ms!r}"
    except AssertionError as exc:
        print(f"检查失败：{exc}", file=sys.stderr)
        sys.exit(1)

    print("6/6 通过")


if __name__ == "__main__":
    main()
