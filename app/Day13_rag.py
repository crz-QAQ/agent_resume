"""Day 13 RAG：载入资料、检索、生成回答并校验引用。

本阶段负责载入、切分、BM25 / 向量检索和 RAG 回答生成。
"""

from __future__ import annotations

import asyncio
import json

# FastEmbed 会在导入阶段读取 Hugging Face endpoint，因此环境变量必须先设置。
# setdefault 不会覆盖用户已经显式配置的 HF_ENDPOINT。
import os

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import warnings

import numpy as np
from fastembed import TextEmbedding

# 当前安装的 fastembed 版本从子模块导出 TextCrossEncoder；
# 若后续版本改到包顶层导出，也能继续兼容。
try:
    from fastembed import TextCrossEncoder
except ImportError:
    from fastembed.rerank.cross_encoder import TextCrossEncoder

import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from pydantic import BaseModel
from rank_bm25 import BM25Okapi

from Day9_async_fallback import call_with_fallback_async


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIRECTORIES = ("教材", "任务", "练习")

# 分块采用“正文最多 420 字符 + 相邻块最多 80 字符重叠”的约束，
# 从而保证拼上重叠后的单个 chunk 仍不超过 500 字符。
MAX_CHUNK_SIZE = 500
OVERLAP_SIZE = 80

# BM25 分数不限定在 [0, 1]，0.5 是当前语料上的经验阈值。
MIN_RELEVANT_SCORE = 0.5
# 归一化向量的点积等于余弦相似度；0.35 仅作为弱结果兜底。
MIN_VECTOR_SCORE = 0.35
# RRF 的排名平滑常量；k 越大，低排名候选之间的分数差越平缓。
RRF_K = 60

HEADING_RE = re.compile(
    r"^ {0,3}(#{1,3})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$"
)
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
PARAGRAPH_RE = re.compile(r"\n[ \t]*\n+")
TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")
CHINESE_RE = re.compile(r"[\u4e00-\u9fff]+")
CITATION_RE = re.compile(r"\[(\d+)\]")

RAG_SYSTEM_PROMPT = """你是资料问答助手，只能依据【资料片段】回答。
规则：
1. 每个结论后标出处，格式 [1] [2]，编号对应片段编号
2. 资料里没有答案 → 只回答"资料里没有相关内容"，不许推测、不许补充常识
3. 不许引入片段之外的信息
4. 只输出 JSON 对象，字段为 answer（字符串）和 refused（布尔值）"""

# retrieve() 和 retrieve_bm25() 的公开签名不含语料参数，因此索引状态保存在模块级。
# _indexed_chunks 按索引位置与 BM25 分数、_document_embeddings 的行一一对应。
_bm25: BM25Okapi | None = None
_indexed_chunks: list[Chunk] = []
_tokenized_chunks: list[list[str]] = []
_vector_model: TextEmbedding | None = None
_document_embeddings: np.ndarray | None = None
# 交叉编码器模型约 1.1GB，加载一次约需 18 秒；模块级缓存避免每次检索都重复加载。
_reranker: TextCrossEncoder | None = None


@dataclass(frozen=True, slots=True)
class Chunk:
    source: str
    heading: str
    text: str


@dataclass(frozen=True, slots=True)
class _Section:
    heading: str
    text: str


class RagAnswer(BaseModel):
    """经过本地校验后的 RAG 回答；citations 不直接信任模型输出。"""

    answer: str
    # 形如 "教材/Day1-10复习总结.md#7 个坑"，只由真实 Chunk 反查生成。
    citations: list[str]
    # 本次请求内采用的资料片段编号，从 1 开始，与 prompt 中的 [n] 对应。
    used_chunks: list[int]
    # 检索为空或模型明确判定资料无法回答时为 True，此时不保留引用。
    refused: bool


# 排除包含验收问题和答案的 Day13 任务书，避免测试题直接命中任务书文本。
# 排除「写了评测题 / 写了评测结论」的文档，避免 RAG 检索到自己的考题和答案（自引用污染）
# 新增复盘、评测类文档时，必须同步加进这里，否则命中率会虚高
_EXCLUDED_FILES = {
    "Day13_RAG_Codex任务书.md",
    "Day13_评测结果.md",
    "Day11-13复盘总结.md",
    # ⚠️ 这份对照表里直接写了 8 道评测题和它们的答案，
    #    一旦进语料，8 题命中率会虚高到接近 8/8（自引用污染）。
    #    以后新增「评测/复盘/对照」类文档，必须同步加到这里。
    "Day16_检索对照.md",
    # 同类的「对照/评测」文档一律不进语料，否则语料规模反复变化，
    # 历史命中率数字就失去可比性。
    "Day17_LangChain对照.md",
    # 修复/复盘类文档同样不进语料：Day 18 的 8 题双跑必须建立在固定语料上，
    # 中途加文档会让 chunk 数从 424 变掉，前后两次数值就不可比了。
    "Day_18_fix.md",
    # Day 20 的三版本对照里直接写了 6 条测试用例和期望 status，
    # 进语料会让检索把这些"用例本身"当成答案命中（自引用污染）。
    "Day20_dify复刻.md",
    # 复盘类文档同理：里面写满了评测数字和用例结论，进语料就是自问自答。
    "Day14-20复盘总结.md",
    # 演示脚本里写满了各档命中数（5/8、6/8、7/8）和延迟，进语料会让评测虚高。
    "Day21_演示脚本.md",
}


def scan_markdown_files(project_root: Path = PROJECT_ROOT) -> list[Path]:
    """递归查找教材/和任务/下的 Markdown 文件，返回稳定排序的路径。"""
    files: list[Path] = []
    for directory_name in CORPUS_DIRECTORIES:
        directory = project_root / directory_name
        if directory.is_dir():
            files.extend(path for path in directory.rglob("*.md") if path.is_file())

    return sorted(
        files,
        key=lambda path: path.relative_to(project_root).as_posix(),
    )


def _extract_sections(markdown: str) -> list[_Section]:
    """按 #、##、### 标题切节，并忽略代码块中的井号行。"""
    sections: list[_Section] = []
    heading = "(无标题)"
    body: list[str] = []
    fence_marker: str | None = None

    def add_section() -> None:
        text = "\n".join(body).strip()
        if text:
            sections.append(_Section(heading=heading, text=text))

    for line in markdown.splitlines():
        fence_match = FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence_marker is None:
                fence_marker = marker
            elif marker[0] == fence_marker[0] and len(marker) >= len(fence_marker):
                fence_marker = None
            body.append(line)
            continue

        heading_match = HEADING_RE.match(line) if fence_marker is None else None
        if heading_match:
            add_section()
            heading = heading_match.group(2).strip()
            body = []
            continue

        body.append(line)

    add_section()
    return sections


def _split_paragraph(paragraph: str, size: int) -> Iterable[str]:
    """超长段落没有自然分段时，按字符兜底切分。"""
    for start in range(0, len(paragraph), size):
        yield paragraph[start : start + size]


def _pack_paragraphs(text: str) -> list[str]:
    """按段落打包，为后续注入 80 字符重叠预留空间。"""
    content_size = MAX_CHUNK_SIZE - OVERLAP_SIZE
    paragraphs = [
        paragraph.strip()
        for paragraph in PARAGRAPH_RE.split(text)
        if paragraph.strip()
    ]

    pieces: list[str] = []
    current = ""
    for paragraph in paragraphs:
        for part in _split_paragraph(paragraph, content_size):
            candidate = f"{current}\n\n{part}" if current else part
            if len(candidate) <= content_size:
                current = candidate
                continue

            if current:
                pieces.append(current)
            current = part

    if current:
        pieces.append(current)
    return pieces


def _split_long_section(text: str) -> list[str]:
    """把长节按段落切开，并让相邻块复用上一块末尾 80 字符。"""
    pieces = _pack_paragraphs(text)
    chunks: list[str] = []
    for piece in pieces:
        if not chunks:
            chunks.append(piece)
            continue

        overlap = chunks[-1][-OVERLAP_SIZE:]
        chunks.append(f"{overlap}{piece}")

    return chunks


def split_markdown(markdown: str, source: str) -> list[Chunk]:
    """把一个 Markdown 文件切成带来源和标题的 Chunk。"""
    chunks: list[Chunk] = []
    for section in _extract_sections(markdown):
        texts = (
            [section.text]
            if len(section.text) <= MAX_CHUNK_SIZE
            else _split_long_section(section.text)
        )
        chunks.extend(
            Chunk(source=source, heading=section.heading, text=text)
            for text in texts
            if text
        )
    return chunks


def load_chunks(
    files: Iterable[Path] | None = None,
    project_root: Path = PROJECT_ROOT,
) -> list[Chunk]:
    """读取 Markdown 语料并返回全部 Chunk；文件只读不改写。"""
    markdown_files = (
        [p for p in files if p.name not in _EXCLUDED_FILES]
        if files is not None
        else [p for p in scan_markdown_files(project_root) if p.name not in _EXCLUDED_FILES]
    )
    chunks: list[Chunk] = []
    for path in markdown_files:
        source = path.relative_to(project_root).as_posix()
        markdown = path.read_text(encoding="utf-8-sig")
        chunks.extend(split_markdown(markdown, source))
    return chunks


def tokenize(text: str) -> list[str]:
    """把连续中文切成字符 bigram，英文和数字 token 原样保留。

    中文只在本段连续汉字内生成相邻二元组，例如“预算护栏”会生成
    “预算”“算护”“护栏”；单字中文无法组成 bigram，因此保留单字。
    英文字母保留原始大小写，数字和下划线同样不经过归一化。
    """
    tokens: list[str] = []
    for match in TOKEN_RE.finditer(text):
        token = match.group()
        if CHINESE_RE.fullmatch(token):
            if len(token) == 1:
                tokens.append(token)
            else:
                # 对长度为 n 的连续中文生成 n - 1 个重叠二元组。
                tokens.extend(token[index : index + 2] for index in range(len(token) - 1))
        else:
            # TOKEN_RE 已限定为字母、数字和下划线，可直接保留原文。
            tokens.append(token)
    return tokens


def build_index(
    chunks: list[Chunk],
) -> tuple[BM25Okapi, list[list[str]]]:
    """为全部 chunk 分词并建立 BM25 索引。

    返回的 token 列表与 chunks 下标对齐，便于用 BM25 的分数下标
    反查对应的 Chunk。该函数只构建索引，不修改模块级检索状态。
    """
    # 标题必须一起进索引：很多答案是「节标题」本身（如 ### 预算护栏），
    # 只索引正文会让这类问题永远检索不到。
    chunk_tokens = [tokenize(f"{chunk.heading}\n{chunk.text}") for chunk in chunks]
    return BM25Okapi(chunk_tokens), chunk_tokens


def build_vector_index(
    chunks: list[Chunk],
) -> tuple[TextEmbedding, np.ndarray]:
    """用 bge-small-zh 编码全部 chunk，并返回 L2 归一化后的向量矩阵。

    返回矩阵形状为 (chunk 数, 向量维度)，每一行对应相同下标的 Chunk。
    归一化后，文档矩阵与归一化问题向量的点积就是余弦相似度。
    """
    model = TextEmbedding(model_name="BAAI/bge-small-zh-v1.5")
    # FastEmbed 返回生成器；先完整消费再转换为二维 float32 矩阵。
    embeddings = np.asarray(
        list(model.embed([f"{c.heading}\n{c.text}" for c in chunks], batch_size=32)),
        dtype=np.float32,
    )
    # axis=1 表示分别计算每一行的 L2 范数，keepdims 保留 N×1 形状用于广播。
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    # 极小范数用 1e-12 代替，防止零向量导致除零；正常句向量范数远大于该值。
    normalized_embeddings = embeddings / np.maximum(norms, 1e-12)
    return model, normalized_embeddings


def retrieve_bm25(question: str, k: int = 3) -> list[tuple[Chunk, float]]:
    """返回 BM25 top-k 结果；最高分不足阈值时返回空列表。

    阈值只检查排序后的最高分。一旦最高分达到阈值，返回完整的 top-k，
    较低名次不会再次被过滤。
    """
    if k <= 0 or _bm25 is None:
        return []

    question_tokens = tokenize(question)
    if not question_tokens:
        return []

    scores = _bm25.get_scores(question_tokens)
    # scores 的下标与 _indexed_chunks 下标一致，按分数降序取前 k 项。
    ranked = sorted(
        enumerate(scores),
        key=lambda item: item[1],
        reverse=True,
    )[:k]
    results = [
        (_indexed_chunks[index], float(score))
        for index, score in ranked
    ]

    # 无结果或最高 BM25 分低于经验阈值时，交由上层执行拒答。
    if not results or results[0][1] < MIN_RELEVANT_SCORE:
        return []
    return results


def retrieve(question: str, k: int = 3) -> list[tuple[Chunk, float]]:
    """返回向量 top-k 结果；最高余弦相似度不足阈值时返回空列表。

    文档矩阵在 build_vector_index() 中已经逐行归一化；这里只归一化问题向量。
    因此矩阵乘法 doc_embs @ question_embedding 的每个结果就是一条余弦相似度，
    数值越大表示语义越接近。
    """
    if (
        k <= 0
        or _vector_model is None
        or _document_embeddings is None
        or len(_document_embeddings) == 0
    ):
        return []

    # embed([question]) 返回生成器且只有一项；next() 取出该问题向量。
    question_embedding = np.asarray(
        next(iter(_vector_model.embed([question]))),
        dtype=np.float32,
    )
    question_norm = float(np.linalg.norm(question_embedding))
    # 零问题向量无法定义余弦相似度，直接按无结果处理。
    if question_norm == 0:
        return []
    question_embedding = question_embedding / question_norm

    # 两侧都已 L2 归一化，点积等价于余弦相似度，形状为 (chunk 数,)。
    scores = _document_embeddings @ question_embedding
    # 对负分数排序相当于按原始分数降序；argsort 返回的是文档行下标。
    ranked = np.argsort(-scores)[:k]
    results = [
        (_indexed_chunks[int(index)], float(scores[index]))
        for index in ranked
    ]

    # 与 BM25 版一致，只根据最高分决定是否拒答，通过后返回完整 top-k。
    if not results or results[0][1] < MIN_VECTOR_SCORE:
        return []
    return results


def retrieve_hybrid(
    question: str,
    k: int = 5,
) -> list[tuple[Chunk, float]]:
    """融合 BM25 与向量检索结果，返回按 RRF 分数降序排列的 top-k。

    两路检索各取前 10 个候选。同一个 Chunk 在最终结果中只保留一次；
    若它由两路返回，只采用两路贡献中更高的 1 / (RRF_K + rank)。
    """
    if k <= 0:
        return []

    bm25_results = retrieve_bm25(question, k=10)
    vec_results = retrieve(question, k=10)

    # Chunk 是 frozen dataclass，可直接作为字典键完成跨检索器去重。
    rrf_scores: dict[Chunk, float] = {}
    for results in (bm25_results, vec_results):
        for rank, (chunk, _original_score) in enumerate(results, start=1):
            contribution = 1.0 / (RRF_K + rank)
            # 同一 Chunk 再次出现时不再累加，只保留排名更高的贡献。
            rrf_scores[chunk] = max(
                rrf_scores.get(chunk, 0.0),
                contribution,
            )

    # RRF 分数只由名次决定，与 BM25、余弦相似度的原始量纲无关。
    ranked = sorted(
        rrf_scores.items(),
        key=lambda item: item[1],
        reverse=True,
    )[:k]
    return [(chunk, float(score)) for chunk, score in ranked]


def _get_reranker() -> TextCrossEncoder:
    """懒加载并复用全局交叉编码器，避免每次检索重复加载大模型。"""
    global _reranker

    if _reranker is None:
        # 首次调用 retrieve_rerank() 时才加载；后续调用直接复用同一个实例。
        _reranker = TextCrossEncoder("BAAI/bge-reranker-base")
    return _reranker


def retrieve_rerank(
    question: str,
    k: int = 5,
    recall: int = 20,
) -> list[tuple[Chunk, float]]:
    """先做双路召回和 RRF 融合，再用交叉编码器精排并返回 top-k。

    RRF 只根据两个检索器给出的名次排序，并不知道问题与文档具体写了什么。
    某条文档可能因若干关键词重合或向量距离较近而名次靠前，却没有真正
    回答当前问题。交叉编码器把问题和每条候选文档作为一对同时输入模型，
    通过注意力直接判断二者的语义匹配程度，因此能纠正 RRF 仅凭名次组合
    造成的排序错误。函数最终返回交叉编码器相关性分数，而不是 RRF 分数。
    """
    if k <= 0:
        return []

    # 两路都严格使用 recall，保证候选池规模由调用方控制，不能写死为 10。
    bm25_results = retrieve_bm25(question, k=recall)
    vec_results = retrieve(question, k=recall)

    # 与 retrieve_hybrid() 保持同一套 RRF 思路：同一 Chunk 只保留两路中
    # 更高的 1 / (RRF_K + rank)，不同检索器的原始分数量纲不会混入融合分数。
    rrf_scores: dict[Chunk, float] = {}
    for results in (bm25_results, vec_results):
        for rank, (chunk, _original_score) in enumerate(results, start=1):
            contribution = 1.0 / (RRF_K + rank)
            rrf_scores[chunk] = max(
                rrf_scores.get(chunk, 0.0),
                contribution,
            )

    # 先按 RRF 分数保留候选顺序；RRF 同分时后续稳定排序会沿用该顺序。
    candidates = [
        chunk
        for chunk, _rrf_score in sorted(
            rrf_scores.items(),
            key=lambda item: item[1],
            reverse=True,
        )
    ]
    assert len(candidates) > 10, "recall 参数没生效，检查是不是还在收 k=10"

    # 交叉编码器接收“问题 + 文档”成对输入，并逐个输出直接相关性分数。
    # 文档文本与向量索引用相同的“标题 + 正文”，避免精排时丢失标题信息。
    documents = [f"{chunk.heading}\n{chunk.text}" for chunk in candidates]
    rerank_scores = list(_get_reranker().rerank(question, documents))

    # rerank() 的输出下标与 documents 一一对应；按交叉编码器分数降序取 top-k。
    ranked = sorted(
        zip(candidates, rerank_scores),
        key=lambda item: item[1],
        reverse=True,
    )[:k]
    return [(chunk, float(score)) for chunk, score in ranked]


def retrieve_weighted(
    question: str,
    k: int = 5,
    w_vec: float = 2.0,
    w_bm: float = 1.0,
) -> list[tuple[Chunk, float]]:
    """按两路权重融合 BM25 与向量检索结果，返回加权 RRF top-k。

    本函数与 retrieve_hybrid() 的唯一区别是贡献值改为
    weight / (RRF_K + rank)：BM25 使用 w_bm，向量检索使用 w_vec。

    当前语料上向量检索默认采用 2.0 的更高权重。像 Q8
    “Dockerfile 里 WORKDIR 为什么要设成 /app/练习？”这样的查询，
    同时包含英文术语和中文问题；相关答案段落可能主要用中文解释，
    与问题中的英文术语字面重合不足，BM25 只依赖分词后的字面匹配，
    容易漏掉或压低这类结果。向量检索比较的是整体语义，能更好地关联
    “英文术语”与“中文解释”，所以在本语料上应给予更高权重。
    """
    if k <= 0:
        return []

    bm25_results = retrieve_bm25(question, k=10)
    vec_results = retrieve(question, k=10)

    # Chunk 是 frozen dataclass，可直接作为字典键完成跨检索器去重。
    rrf_scores: dict[Chunk, float] = {}
    # 分别传入两路自己的权重，使加权排名仍不受原始分数量纲影响。
    for results, weight in (
        (bm25_results, w_bm),
        (vec_results, w_vec),
    ):
        for rank, (chunk, _original_score) in enumerate(results, start=1):
            contribution = weight / (RRF_K + rank)
            # 与 retrieve_hybrid() 一致，同一 Chunk 只保留更高的贡献。
            rrf_scores[chunk] = max(
                rrf_scores.get(chunk, 0.0),
                contribution,
            )

    ranked = sorted(
        rrf_scores.items(),
        key=lambda item: item[1],
        reverse=True,
    )[:k]
    return [(chunk, float(score)) for chunk, score in ranked]


def _build_rag_user_prompt(
    question: str,
    chunks: list[tuple[Chunk, float]],
) -> str:
    """按检索顺序给片段编号，构造只包含资料和问题的 user 消息。"""
    sections = ["资料片段："]
    for number, (chunk, _score) in enumerate(chunks, start=1):
        # 编号仅在本次请求内有效，number - 1 可直接反查对应的真实 Chunk。
        sections.append(
            f"[{number}] 来源：{chunk.source} · {chunk.heading}\n{chunk.text}"
        )
    sections.append(f"问题：{question}")
    return "\n\n".join(sections)


def _parse_model_output(raw_text: str) -> tuple[str, bool]:
    """提取模型正文和 refused 标志；同时兼容 JSON 与纯文本返回。"""
    text = raw_text.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text, False

    if isinstance(payload, str):
        return payload.strip(), False
    if not isinstance(payload, dict):
        return text, False

    answer = payload.get("answer")
    answer_text = answer.strip() if isinstance(answer, str) else text
    model_refused = payload.get("refused") is True
    return answer_text, model_refused


def _validated_rag_answer(
    answer_text: str,
    model_refused: bool,
    chunks: list[tuple[Chunk, float]],
) -> RagAnswer:
    """从正文引用编号反查真实来源，并忽略模型返回的伪造来源。"""
    referenced_numbers = [
        int(number) for number in CITATION_RE.findall(answer_text)
    ]
    valid_numbers = set(range(1, len(chunks) + 1))
    invalid_numbers = sorted(set(referenced_numbers) - valid_numbers)
    if invalid_numbers:
        # 一轮回答只记录一次 warning，并同时列出所有越界编号。
        warnings.warn(
            f"模型引用了不存在的资料片段编号：{invalid_numbers}；已忽略这些编号",
            UserWarning,
            stacklevel=2,
        )

    # 按正文首次出现顺序去重，只保留当前请求中真实存在的片段编号。
    used_chunks = list(
        dict.fromkeys(
            number for number in referenced_numbers if number in valid_numbers
        )
    )

    normalized_answer = answer_text.strip().strip("。.!！ ")
    refused = (
        model_refused
        or normalized_answer == "资料里没有相关内容"
    )
    if refused:
        return RagAnswer(
            answer=answer_text,
            citations=[],
            used_chunks=[],
            refused=True,
        )

    # citations 完全由本地 Chunk 生成，不采用模型 JSON 里的 citations 字段。
    citations = list(
        dict.fromkeys(
            f"{chunks[number - 1][0].source}#{chunks[number - 1][0].heading}"
            for number in used_chunks
        )
    )
    return RagAnswer(
        answer=answer_text,
        citations=citations,
        used_chunks=used_chunks,
        refused=False,
    )


async def ask(question: str, k: int = 5) -> RagAnswer:
    """执行一次完整 RAG：检索、生成回答、解析并校验引用。"""
    chunks = retrieve_hybrid(question, k=5)
    if not chunks:
        # 没有达到阈值的片段时直接拒答，绝不进入模型调用以节省成本。
        return RagAnswer(
            answer="资料里没有相关内容",
            citations=[],
            used_chunks=[],
            refused=True,
        )

    user_prompt = _build_rag_user_prompt(question, chunks)
    # 问答规则必须作为 system 消息传入，不能混入包含资料的 user prompt。
    raw_text, _meter = await call_with_fallback_async(
        user_prompt,
        system=RAG_SYSTEM_PROMPT,
    )
    answer_text, model_refused = _parse_model_output(raw_text)
    return _validated_rag_answer(answer_text, model_refused, chunks)


async def _run_rag_questions(questions: Iterable[str]) -> None:
    """顺序执行问题，避免并发请求导致日志和厂商回退过程交错。"""
    for question in questions:
        # k 必须跟评测脚本保持一致（eval_day13.py 用 k=5），
        # 否则「评测命中」和「实际能答」是两套标准，评测结果不代表线上效果
        answer = await ask(question, k=5)
        # 不再截断：截断会让人误判「答案被切断 = 回答失败」
        preview = answer.answer.replace("\n", " ")
        print(f"\n问题: {question}")
        print(
            f"(refused={answer.refused}, "
            f"citations={answer.citations}, answer={preview})"
        )


async def _run_acceptance_summary(
    chunks: list[Chunk],
    cases: list[tuple[str, list[str], str]],
) -> tuple[int, int, bool]:
    """按 8 道评测题现场统计检索命中、引用准确率和拒答结果。"""
    hit_count = 0
    citation_count = 0
    real_citations = {
        f"{chunk.source}#{chunk.heading}"
        for chunk in chunks
    }

    for question, expected_sources, _keyword in cases:
        results = retrieve_hybrid(question, k=5)
        if any(
            any(expected in chunk.source for expected in expected_sources)
            for chunk, _score in results
        ):
            hit_count += 1

        answer = await ask(question)
        if (
            answer.citations
            and all(citation in real_citations for citation in answer.citations)
        ):
            citation_count += 1

    refusal = await ask("公司年会在几号？")
    return hit_count, citation_count, refusal.refused


def main() -> None:
    # 两个检索函数依赖这些模块级索引；build_* 只负责返回新索引。
    global _bm25, _indexed_chunks, _tokenized_chunks
    global _vector_model, _document_embeddings

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    files = scan_markdown_files()
    chunks = load_chunks(files)

    sources = {c.source for c in chunks}
    excluded = len(files) - len(sources)
    print(f"扫描到 {len(files)} 个 Markdown 文件，排除 {excluded} 个评测文档后 {len(sources)} 个进语料")
    # load_chunks() 会排除 _EXCLUDED_FILES，因此该数量可能少于扫描文件所切出的总数。
    print(f"共切成 {len(chunks)} 个 chunk")

    for index, chunk in enumerate(chunks[:3], start=1):
        preview = chunk.text[:80].replace("\n", "\\n")
        print(f"\n[{index}] source: {chunk.source}")
        print(f"    heading: {chunk.heading}")
        print(f"    text(前80字): {preview}")

    # 只统计索引构建本身；Markdown 扫描、读取和切分发生在计时之前。
    started_at = time.perf_counter()
    _bm25, _tokenized_chunks = build_index(chunks)
    _indexed_chunks = chunks
    elapsed = time.perf_counter() - started_at
    print(f"\n建索引耗时: {elapsed:.4f} 秒")

    # 此耗时包含模型加载（必要时下载）和全部 chunk 编码，不包含问题编码。
    started_at = time.perf_counter()
    _vector_model, _document_embeddings = build_vector_index(chunks)
    vector_elapsed = time.perf_counter() - started_at
    print(f"向量索引构建耗时: {vector_elapsed:.4f} 秒")

    questions = (
        "空壳成功的定义是什么？",
        "Day 9 的降级顺序是什么？",
        "预算护栏为什么要放在调用之前？",
        "公司年会在几号？",
    )
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from 工具.eval_day13 import CASES

    # ⚠️ 整条流程只能有一个事件循环。
    # 原因：Day9 的 AsyncOpenAI 是「每次调用新建一个客户端」，
    # 如果前后调用两次 asyncio.run()，第 1 个循环里创建的客户端
    # 会在循环关闭之后才被回收，回收时调用 aclose() 就会抛
    # RuntimeError: Event loop is closed（Task exception was never retrieved）。
    async def _run_all():
        await _run_rag_questions(questions)
        return await _run_acceptance_summary(chunks, CASES)

    hit_count, citation_count, refused = asyncio.run(_run_all())
    print(
        f"chunks={len(chunks)} 命中率={hit_count}/{len(CASES)} "
        f"引用准确率={citation_count}/{len(CASES)} "
        f"拒答={'OK' if refused else 'FAIL'}"
    )


if __name__ == "__main__":
    main()
