"""M6：同步重算硬性与本地语义分，沿用原 LLM 分并明确预估误差。"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

# 替换入口同时承担首次定位、规范化和冲突去重，M6 不维护这些规则。
from app.resume_rewrite import _replace_pdf_text
from app.schemas_match import JobInfo, MatchReport
from app.schemas_revision import AdaptedText, ScoreDelta
from app.scoring_rules import HardScore, score_hard
# 语义评分只使用既有本地编码器和重排器；本模块没有远端评分入口。
from app.scoring_semantic import ItemSim, SemanticScore, score_semantic

if TYPE_CHECKING:
    # 旧 ResumeInfo 模块会初始化远端客户端，运行时只读既有对象的属性。
    from app.schemas import ResumeInfo


def rewrite_text(resume_text: str, adaptations: list[AdaptedText]) -> tuple[str, int]:
    """复用原文坐标替换及规范化匹配，每条只改首次出现，返回新文本与成功条数。

    未命中、重复冲突、空建议和原样建议均沿用 M5 的跳过规则；不改输入。
    """
    # 直接调用既有实现，避免另写一份替换、冲突判断或规范化规则。
    rewritten, applied, _ = _replace_pdf_text(resume_text, adaptations, normalize=True)
    return rewritten, len(applied)


def _hard_note(before: HardScore, after: HardScore) -> str:
    """根据逐项扣分的消失、新增或金额变化解释硬性分变化。"""
    difference = after.score - before.score
    if difference == 0:
        # 未变化时不暗示改写能补足真实年限或学历。
        return "硬性 0：词句改写不影响年限/学历"
    # 金额与原始证据都从评分结果读取，不根据技术名臆造扣分。
    old = {(row.type, row.item): row for row in before.deductions}
    new = {(row.type, row.item): row for row in after.deductions}
    reasons = []
    for key, row in old.items():
        # 按原扣分顺序输出，使相同输入的 notes 逐字稳定。
        if key not in new:
            reasons.append(f"解除「{row.item}」扣分")
        elif row.deduct != new[key].deduct:
            reasons.append(f"「{row.item}」扣分 {row.deduct} → {new[key].deduct}")
    for key, row in new.items():
        # 负向变化同样列出，不能只描述解除扣分的收益。
        if key not in old:
            reasons.append(f"新增「{row.item}」扣分")
    return f"硬性 {difference:+d}：" + "；".join(reasons or ["按现有硬性规则精确重算"])


def _semantic_note(before: SemanticScore, after: SemanticScore) -> str:
    """同时解释覆盖数量与相似度变化，保留丢失和新增职责原文。"""
    # covered 是评分器的重排判定，不能根据余弦或整数分自行推断覆盖。
    old = [row.item for row in before.per_item if row.covered]
    new = [row.item for row in after.per_item if row.covered]
    # 直接相减保留负数，文案和数据使用同一个差值。
    difference = after.score - before.score
    label = f"{difference:+d}" if difference else "0"
    counts = f"{len(old)} → {len(new)}"
    # 职责集合只用于查找，实际文案始终沿用 JD 原顺序。
    added = [item for item in new if item not in old]
    lost = [item for item in old if item not in new]
    if len(old) == len(new):
        note = f"语义 {label}：覆盖条数未变（{counts}）"
        if not added and not lost and difference == 0:
            note += "，改写只影响措辞"
        elif difference:
            # 同样条数也可能因覆盖句余弦变化而增减，不能谎称分数必然不变。
            note += "，覆盖句余弦相似度变化影响得分"
    else:
        note = f"语义 {label}：覆盖条数 {counts}"
    if added:
        # 新增和丢失可以同时发生，不能只输出其中较有利的一面。
        note += f"（新增覆盖：{'、'.join(added)}）"
    if lost:
        note += "，" + "；".join(f"改写后「{item}」不再被判定为覆盖" for item in lost)
    if difference == 0 and len(old) != len(new):
        # 语义分按全部职责平均后取整，覆盖条数变化不保证整数分改变。
        note += "；语义分按覆盖句余弦之和/职责数取整，覆盖变化未改变整数分"
    return note


def delta_from_scores(*, before_hard: HardScore, before_sem: SemanticScore,
                      before_llm: int, after_hard: HardScore,
                      after_sem: SemanticScore) -> ScoreDelta:
    """纯计算汇总三项、计算真实差值及解释；越界直接报错，不修剪分数。"""
    # 三项都是整数；拒绝缺失值，不能把没有原 LLM 分误当成零分。
    if type(before_llm) is not int:
        raise ValueError("必须提供改前 LLM 整数分")
    before = {"hard": before_hard.score, "semantic": before_sem.score, "llm": before_llm}
    # LLM 行始终沿用原值，estimated_fields 显式披露这项预估。
    after = {"hard": after_hard.score, "semantic": after_sem.score, "llm": before_llm}
    total = sum(after.values())
    if not 0 <= total <= 100:
        # 暴露评分异常；负向差值是正常结果，不适用这条边界检查。
        raise ValueError(f"after_total_estimate 必须在 0..100：{total}")
    # 三类解释固定顺序，重复运行不会引入时间或随机元数据。
    notes = [_hard_note(before_hard, after_hard), _semantic_note(before_sem, after_sem),
             f"LLM 30 分未重算，沿用改前 {before_llm} 分（预估误差 ±2，以重新评分为准）"]
    # 返回全新模型与容器，调用方可安全持有原评分对象。
    return ScoreDelta(before_total=sum(before.values()), after_total_estimate=total,
                      before_parts=before, after_parts=after,
                      delta_parts={key: after[key] - before[key] for key in before},
                      estimated_fields=["llm"],
                      covered_before=sum(row.covered for row in before_sem.per_item),
                      covered_after=sum(row.covered for row in after_sem.per_item), notes=notes)


def estimate_delta(*, resume: ResumeInfo, job: JobInfo, resume_text: str,
                   adaptations: list[AdaptedText], before_report: MatchReport | None = None,
                   before_llm: int | None = None) -> ScoreDelta:
    """改后文本 → 既有硬性/本地语义评分器 → 逐项差值，不重算 LLM。

    改前优先采用正式报告的总分、分项和覆盖证据；否则现算硬性/语义。
    无报告时必须显式传 before_llm。调用方负责将本地推理放入线程池。
    """
    # 在任何本地推理前校验原分，缺 LLM 时尽早失败。
    if before_report is not None:
        parts = before_report.score.breakdown
        before_llm = parts.get("llm", before_llm)
    if type(before_llm) is not int:
        raise ValueError("必须传 before_llm 或 before_report.score.breakdown['llm']")
    if before_report is None:
        # 没有正式报告时才现算改前，两种入口共用相同评分规则。
        before_hard = score_hard(resume, job, resume_text)
        before_sem = score_semantic(resume_text, job)
    else:
        # 报告中的正式分优先，不能偷偷用新算分覆盖历史口径。
        before_hard = HardScore(parts["hard"], dict(parts.get("hard_detail", {})),
                                list(before_report.deductions))
        if before_report.semantic_items:
            # 创建 dataclass 副本，不把历史报告内的明细交给可变评分对象。
            entries = [ItemSim(**row.model_dump()) for row in before_report.semantic_items]
            before_sem = SemanticScore(parts["semantic"], entries,
                                       [row.item for row in entries if not row.covered])
        else:
            # 兼容没有逐项证据的旧报告：仅重算覆盖证据，分数仍取报告。
            before_sem = replace(score_semantic(resume_text, job), score=parts["semantic"])
    # 先完成文本替换再评分，不能把 adaptations 自带的标签当成评分证据。
    rewritten, count = rewrite_text(resume_text, adaptations)
    if rewritten == resume_text:
        # 没有实际改动就沿用同一基准，空列表和全部跳过均不能凭空产生分差。
        after_hard, after_sem = before_hard, before_sem
    else:
        # ResumeInfo 年限/技能沿用输入；原文兜底和学历证据读取改后文本。
        after_hard = score_hard(resume, job, rewritten)
        after_sem = score_semantic(rewritten, job)
    # 所有汇总、边界与模板都经同一个纯计算入口。
    result = delta_from_scores(before_hard=before_hard, before_sem=before_sem,
                               before_llm=before_llm, after_hard=after_hard, after_sem=after_sem)
    if before_report is not None:
        result.before_total = before_report.score.total
        result.notes.append("改前分为评分报告口径")
    if not adaptations:
        # 空改动明确告知用户，不能只返回一组看不出原因的零差值。
        result.notes.append("本次未改动任何句子，分数不变")
    elif count < len(adaptations):
        # 返回值的条数指成功数，未匹配/冲突条数在 notes 中明确披露。
        result.notes.append(f"成功替换 {count} 条，未替换 {len(adaptations) - count} 条")
        if rewritten == resume_text:
            result.notes.append("本次未改动任何句子，分数不变")
    return result
