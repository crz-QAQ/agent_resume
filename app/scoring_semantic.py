"""按 PRD §2.3 v1.1 用余弦召回、重排精判返回语义分和逐项证据。"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
import re
import sys

import numpy as np

# rag.py 沿用同级模块的绝对导入；兼容其路径，不改动既有 RAG 逻辑。
_APP_DIR = str(Path(__file__).resolve().parent)
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

# 先导入 rag，让其在 FastEmbed 导入前设置项目约定的模型镜像地址。
from app.rag import EMBED_MODEL_NAME, _get_reranker
from app.schemas_match import JobInfo
from fastembed import TextEmbedding


# PRD §2.3 v1.1：每条职责只重排余弦 top-3 候选，限制精判成本。
RERANK_TOP_K = 3
# PRD §2.3 v1.1 实测表：真相关 +5.53，无关 -0.86/-1.10/-1.70，0 为天然分界。
RERANK_THRESHOLD = 0.0
# PRD §2.3 v1.1：仅重排不可用时回退旧余弦阈值，并明确标记待复核。
LEGACY_COS_THRESHOLD = 0.6
# 固定降级文案，供调用方和报告审计，不能仅打印日志后返回 ok。
RERANK_FALLBACK_REASON = "重排模型不可用，已回退余弦阈值0.6，结果偏宽松"
# PRD §2.3：B 项语义相近分满分 30。
MAX_SCORE = 30
# PRD §2.3 对应 S4 §一.2：丢弃少于 6 字符的标题、姓名等短片段。
MIN_SENTENCE_LEN = 6


@dataclass
class ItemSim:
    """保留最高余弦，并记录重排最高分、对应原句与覆盖状态。"""

    # 职责及证据保留原文，方便后续报告逐项追溯。
    item: str
    best_sim: float
    best_sentence: str
    covered: bool
    # logit 只判覆盖，不参与分数幅度；无有效重排结果时用负无穷表示缺测。
    rerank_score: float


@dataclass
class SemanticScore:
    """保存 0–30 分、全部职责明细和供 S6 使用的未覆盖职责。"""

    # 未覆盖项仍出现在 per_item，不能仅返回参与平均的职责。
    score: int
    per_item: list[ItemSim]
    uncovered: list[str]
    # 默认值兼容 S5/S6 的三位置参数构造；降级原因可直接向报告传播。
    status: str = "ok"
    reasons: list[str] = field(default_factory=list)


@lru_cache(maxsize=1)
def _get_model() -> TextEmbedding:
    """惰性加载并复用同一 CPU 编码器，避免每次打分重新加载权重。"""
    # 固定单线程 CPU 推理，减少执行后端与线程调度造成的数值差异。
    # 只缓存模型，不缓存打分结果，重复验收仍会真实执行两批编码。
    return TextEmbedding(
        model_name=EMBED_MODEL_NAME, threads=1, providers=["CPUExecutionProvider"]
    )


def _encode_batch(model: TextEmbedding, texts: list[str]) -> np.ndarray:
    """一次编码整批文本，按行执行与 RAG 相同的 float32 L2 归一化。"""
    # FastEmbed 返回生成器，消费后每一行与输入文本的下标一致。
    embeddings = np.asarray(list(model.embed(texts, batch_size=32)), dtype=np.float32)
    # 保留 N×1 范数以广播；零向量用 1e-12 保护，避免出现 NaN。
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    return embeddings / np.maximum(norms, 1e-12)


def score_semantic(resume_text: str, job: JobInfo) -> SemanticScore:
    """余弦 top-3 后重排判覆盖，未覆盖计零，重排失败显式降级待复核。"""
    # ① 严格使用指定的中文标点和换行切句，strip 后再过滤长度。
    sentences = [part.strip() for part in re.split(r"[。！？；\n]", resume_text)
                 if len(part.strip()) >= MIN_SENTENCE_LEN]
    responsibilities = job.responsibilities
    if not responsibilities:
        # ② 防御分支：空职责无法比对，说明留给 S6，不能伪造职责明细。
        return SemanticScore(score=0, per_item=[], uncovered=["JD 无可比对职责"])
    if not sentences:
        # 没有有效原句时不加载模型，每条职责均未覆盖且证据为空。
        # 缺少证据不能用 0 logit，否则在 >=0 的规则下会被误判覆盖。
        per_item = [ItemSim(item, 0.0, "", False, float("-inf")) for item in responsibilities]
        return SemanticScore(score=0, per_item=per_item, uncovered=list(responsibilities))

    # ③ 句子和职责各编码一批；编码不进入下方逐职责循环。
    model = _get_model()
    resume_vectors = _encode_batch(model, sentences)
    job_vectors = _encode_batch(model, responsibilities)
    # 两侧均已 L2 归一化，职责×句子矩阵的每个点积即余弦。
    similarities = job_vectors @ resume_vectors.T
    # 浮点误差可能使同句点积略超 ±1，仅修正余弦的数值边界。
    similarities = np.clip(similarities, -1.0, 1.0)
    per_item = []
    status, reasons = "ok", []
    try:
        # ④ rag 的模块级 _reranker 只加载一次；在职责循环外取复用实例。
        rr = _get_reranker()
        for item, row in zip(responsibilities, similarities):
            # 稳定排序使余弦并列时保留原句顺序；不足三句就全部作为候选。
            candidates = np.argsort(-row, kind="stable")[:RERANK_TOP_K]
            docs = [sentences[int(index)] for index in candidates]
            rerank_scores = np.asarray(list(rr.rerank(item, docs)), dtype=float)
            # 生成器可能在消费时失败；缺项、NaN、无穷也不能冒充有效精判。
            if rerank_scores.shape != (len(docs),) or not np.isfinite(rerank_scores).all():
                raise ValueError("重排结果数量或数值无效")
            best = int(np.argmax(rerank_scores))
            rerank_score = float(rerank_scores[best])
            # 余弦最大值与重排证据分别保留，不能将 logit 当相似度乘 30。
            per_item.append(ItemSim(item, float(row[candidates[0]]), docs[best],
                                    rerank_score >= RERANK_THRESHOLD, rerank_score))
    except Exception:
        # 加载或推理失败都整批回退，防止同一结果混用两套覆盖规则。
        status, reasons = "needs_review", [RERANK_FALLBACK_REASON]
        per_item = []
        for item, row in zip(responsibilities, similarities):
            best = int(np.argmax(row))
            best_sim = float(row[best])
            # 缺测 logit 不伪造成 0；降级证据恢复为余弦最高句。
            per_item.append(ItemSim(item, best_sim, sentences[best],
                                    best_sim >= LEGACY_COS_THRESHOLD, float("-inf")))

    # ⑤ S6 覆盖率修正：只累加 covered 相似度，分母保留全部职责。
    covered_sims = [entry.best_sim for entry in per_item if entry.covered]
    score = int(MAX_SCORE * sum(covered_sims) / len(job.responsibilities) + 0.5)
    # ⑥ 未覆盖职责按 JD 原顺序、原文返回，供 S6 生成缺口。
    uncovered = [entry.item for entry in per_item if not entry.covered]
    return SemanticScore(score=score, per_item=per_item, uncovered=uncovered,
                         status=status, reasons=reasons)
