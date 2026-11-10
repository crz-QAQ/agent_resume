"""按 S4 对岗位职责与简历原句做本地向量匹配，返回分数和逐项证据。"""

from __future__ import annotations

from dataclasses import dataclass
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
from app.rag import EMBED_MODEL_NAME
from app.schemas_match import JobInfo
from fastembed import TextEmbedding


# PRD §2.3：低于 0.6 的职责视为明显不相关，不计入平均。
COVER_THRESHOLD = 0.6
# PRD §2.3：B 项语义相近分满分 30。
MAX_SCORE = 30
# PRD §2.3 对应 S4 §一.2：丢弃少于 6 字符的标题、姓名等短片段。
MIN_SENTENCE_LEN = 6


@dataclass
class ItemSim:
    """保存一条职责的最高余弦、命中的简历原句与覆盖状态。"""

    # 职责及证据保留原文，方便后续报告逐项追溯。
    item: str
    best_sim: float
    best_sentence: str
    covered: bool


@dataclass
class SemanticScore:
    """保存 0–30 分、全部职责明细和供 S6 使用的未覆盖职责。"""

    # 未覆盖项仍出现在 per_item，不能仅返回参与平均的职责。
    score: int
    per_item: list[ItemSim]
    uncovered: list[str]


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
    """逐条匹配职责与简历句子，仅对已覆盖职责的最高余弦取平均计分。"""
    # ① 严格使用指定的中文标点和换行切句，strip 后再过滤长度。
    sentences = [part.strip() for part in re.split(r"[。！？；\n]", resume_text)
                 if len(part.strip()) >= MIN_SENTENCE_LEN]
    responsibilities = job.responsibilities
    if not responsibilities:
        # ② 防御分支：空职责无法比对，说明留给 S6，不能伪造职责明细。
        return SemanticScore(score=0, per_item=[], uncovered=["JD 无可比对职责"])
    if not sentences:
        # 没有有效原句时不加载模型，每条职责均未覆盖且证据为空。
        per_item = [ItemSim(item, 0.0, "", False) for item in responsibilities]
        return SemanticScore(score=0, per_item=per_item, uncovered=list(responsibilities))

    # ③ 句子和职责各编码一批；编码不进入下方逐职责循环。
    model = _get_model()
    resume_vectors = _encode_batch(model, sentences)
    job_vectors = _encode_batch(model, responsibilities)
    # 两侧均已 L2 归一化，职责×句子矩阵的每个点积即余弦。
    similarities = job_vectors @ resume_vectors.T
    # 浮点误差可能使同句点积略超 ±1，仅修正余弦的数值边界。
    similarities = np.clip(similarities, -1.0, 1.0)
    best_indices = np.argmax(similarities, axis=1)
    per_item = []
    for index, item in enumerate(responsibilities):
        # ④ argmax 遇到并列时固定取首句，保证证据选择顺序稳定。
        best_index = int(best_indices[index])
        best_sim = float(similarities[index, best_index])
        per_item.append(ItemSim(item, best_sim, sentences[best_index],
                                best_sim >= COVER_THRESHOLD))

    # ⑤ 只平均 covered 项；int(x + 0.5) 避免银行家舍入。
    covered_sims = [entry.best_sim for entry in per_item if entry.covered]
    score = int(MAX_SCORE * (sum(covered_sims) / len(covered_sims)) + 0.5) if covered_sims else 0
    # ⑥ 未覆盖职责按 JD 原顺序、原文返回，供 S6 生成缺口。
    uncovered = [entry.item for entry in per_item if not entry.covered]
    return SemanticScore(score=score, per_item=per_item, uncovered=uncovered)
