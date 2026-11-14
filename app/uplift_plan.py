"""M9a：复用已有评分事实，纯代码计算附真实前提的涨分策略。

不重算评分，不加载语义模型，不调用 LLM；输出只表示公式潜力，不承诺实际涨分。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.schemas_match import JobInfo
from app.schemas_revision import DiagnosisReport, ReviewIssue, UpliftItem, UpliftPlan
from app.scoring_rules import group_required_skills, normalize_skill

# 评分类型仅供静态检查，避免导入 scoring_semantic 时引入模型依赖。
if TYPE_CHECKING:
    from app.scoring_rules import HardScore
    from app.scoring_semantic import SemanticScore


# PRD v2.0 §6.2.5：实测被覆盖句余弦 0.75~0.83，取中位。
TYPICAL_COS = 0.8
# 展示上限同样约束合计的范围，后续报告可逐条核对。
MAX_ITEMS = 10
# 已知不足和未能确认均不允许靠改写计入可达分数。
_BLOCKED_HARD = {"years_short", "years_unknown", "education", "education_unknown"}
# 下列解释与动作逐字摘自 §3.4；后续报告应原样使用 notes，不让模型另写承诺。
EXPLANATION_NOTES = (
    "用户实测：同一份简历，走完智能修改后总分不变。这不是缺陷，是三层评分的结构决定的：",
    "① 硬性：真的具备 JD 的必备技能 → 在技能栏与相关经历中写出真实使用场景（每命中一条 +15/N）",
    "② 语义：把 JD 要求、但简历里完全没有的职责，用你真实做过的事补成一条（覆盖 0 → 1，+30/n×余弦）",
    "③ LLM：补项目细节（规模/角色/结果）→ 本版不重跑，只能说「下次评分会体现」",
    "靠改简历永远拿不到的分数：年限不足、学历不达标、以及「没做过却要写的经历」。",
)


def build_uplift_plan(*, hard: HardScore, sem: SemanticScore, llm_score: int | None,
                      job: JobInfo, diagnosis: DiagnosisReport,
                      llm_estimated: bool = False) -> UpliftPlan:
    """按 §6.2.5 从已有评分构造策略，返回新对象，不修改输入或执行模型。

    llm_estimated 为真时不使用 llm_score；否则其必须是 0～30 的整数。
    分母来自当前 JD；缺技能却无技能分组等矛盾输入抛 ValueError。
    """
    items: list[UpliftItem] = []
    # 每次复制解释列表，调用方修改结果不会污染后续计算。
    notes = list(EXPLANATION_NOTES)
    # 没做过的经历始终属于限制，即使当前没有任何技能或职责缺口。
    unreachable = ["没做过的经历：不能通过改稿写成已有经历；如确有相关经历请补充，否则不建议写"]
    # 分组仅保留 S3.5 的校验与审计口径，潜力直接复用已有扣分，不再平均分配。
    group_count = len(group_required_skills(job.must_have))
    for deduction in hard.deductions:
        if deduction.type == "missing_skill":
            if not group_count:
                raise ValueError("missing_skill 缺少 JD 必备技能分组，无法计算潜力")
            skill = deduction.jd_evidence
            # 与 S3 扣分口径一致，最后一条吸收余数；potential = deduction.deduct。
            potential = deduction.deduct
            items.append(UpliftItem(
                part="hard", item=f"必备技能：{skill}", current="未命中（技能项总权重 15 分）",
                potential=potential,
                action=f"在技能栏补 {skill}，并在相关经历里写出你真实的使用场景与产出",
                requirement=f"你真的用过 {skill}；写过但做不到，面试必露", blocked=False,
            ))
        elif deduction.type in _BLOCKED_HARD:
            # 年限/学历不足或未知均按契约阻断；potential=0，不把扣分当成可赚分数。
            items.append(UpliftItem(
                part="hard", item=deduction.item,
                current=f"{deduction.resume_evidence}（当前扣 {deduction.deduct} 分）", potential=0,
                action="改简历拿不到这分：年限靠时间积累，学历靠学历；建议换匹配的岗位",
                requirement="年限或学历必须真实满足 JD 要求，不能靠措辞改变", blocked=True,
            ))
            # 先收集全部限制，即使零分项被十条展示上限截去，也保留诚实说明。
            unreachable.append(f"{deduction.item}：{deduction.resume_evidence}；改简历拿不到这分")

    duty_count = len(job.responsibilities)
    # 不使用 uncovered 列表或相似度阈值重新判定，以 per_item.covered 为唯一事实。
    for entry in sem.per_item:
        if entry.covered:
            continue
        if not duty_count:
            raise ValueError("未覆盖职责缺少 JD 职责总数，无法计算潜力")
        # 语义公式：potential = round(30 / 职责总数 × TYPICAL_COS)，只补 covered=False。
        potential = round(30 / duty_count * TYPICAL_COS)
        items.append(UpliftItem(
            part="semantic", item=f"JD 职责：{entry.item}",
            current=f"未覆盖（cos={entry.best_sim:.2f}）", potential=potential,
            action=f"用你真实做过的一条经历回应「{entry.item}」：写清场景、你负责的部分、结果数字",
            requirement="你真的做过这件事；这一条是新写，不是改写旧句", blocked=False,
        ))
    # 空职责不能因 all([]) 为真而被描述为已覆盖；未提供评分明细也不冒充全覆盖。
    ceiling_note = ""
    if duty_count and len(sem.per_item) == duty_count and all(entry.covered for entry in sem.per_item):
        ceiling_note = f"语义分已接近上限（{sem.score}/30），再改措辞只会 ±1"
    elif not sem.per_item:
        notes.append("JD 无可比对职责或未提供语义明细，语义潜力未计算。")

    if llm_estimated:
        # 缺测/不可用的 LLM 分不能把占位的零分转换成三十分潜力。
        notes.append("LLM 不可用（llm_estimated=True），未列 LLM 潜力项；本次不重算。")
    else:
        # 非缺测情况下拒绝越界、布尔和浮点输入，避免生成负潜力或偷做类型转换。
        if type(llm_score) is not int or not 0 <= llm_score <= 30:
            raise ValueError("llm_score 必须是 0～30 的整数；缺测请传 llm_estimated=True")
        # LLM 公式：potential = 30 - llm_score；本版不重跑，始终 blocked=True。
        items.append(UpliftItem(
            part="llm", item="LLM 项目细节", current=f"{llm_score}/30", potential=30 - llm_score,
            action="补规模/团队角色/前后对比数字；下一轮评分会体现，本次不重算",
            requirement="规模、团队角色、前后对比数字必须真实；需下一轮重新评分", blocked=True,
        ))
        unreachable.append("LLM 项：本版不重跑，本次改稿无法取得这一分项的潜力分")

    # 诊断中的措辞、量化、ATS 缺词不是额外分项，不能脱离 hard/sem 叠加潜力。
    notes.append(f"规则诊断 {len(diagnosis.findings)} 条、ATS 缺词 {len(diagnosis.missing_keywords)} 项，"
                 "仅作补充指引，不另计涨分；覆盖不变时，改措辞的余弦波动为 ±1。")
    notes.append("以上为最多潜力，必须满足每条真实前提；逐项取整，实际变化以重新评分为准。")
    # 稳定排序保留同潜力项的上游顺序；先限十条再合计，避免展示与合计无法核对。
    items.sort(key=lambda entry: -entry.potential)
    # 记录截断前数量，不能用十条限制掩盖剩余缺口。
    candidate_count = len(items)
    items = items[:MAX_ITEMS]
    # 汇总公式：achievable_if_true = Σ potential（仅最终 items 中的非 blocked 项）。
    achievable = sum(entry.potential for entry in items if not entry.blocked)
    # 元数据仅描述确定输入，不写时间或耗时，保持相同输入逐字段相同。
    return UpliftPlan(
        items=items, achievable_if_true=achievable, unreachable=unreachable,
        ceiling_note=ceiling_note, notes=notes,
        meta={"cost_yuan": 0.0, "model_calls": 0, "typical_cos": TYPICAL_COS,
              "skill_group_count": group_count, "responsibility_count": duty_count,
              "llm_estimated": llm_estimated, "candidate_count": candidate_count,
              "truncated_count": candidate_count - len(items)},
    )


def _matches_issue(issue: ReviewIssue, item: UpliftItem) -> bool:
    """用岗位证据匹配原项或技能组明确列出的别名，不做模糊词或子串猜测。"""
    prefix = "必备技能：" if item.part == "hard" else "JD 职责："
    label = item.item.removeprefix(prefix)
    # 完整证据优先；去空白与技能别名沿用既有评分入口，避免 Go 命中 Django。
    evidence = normalize_skill(issue.jd_evidence)
    names = [label]
    if item.part == "hard" and "（举例：" in label and label.endswith("）"):
        # S3.5 的族扣分证据含组内原名；代码注入的单个原名可回链这条组项。
        names.extend(label.split("（举例：", 1)[1][:-1].split("、"))
    return bool(evidence) and any(evidence == normalize_skill(name) for name in names)


def apply_score_impact(issues: list[ReviewIssue], plan: UpliftPlan) -> list[ReviewIssue]:
    """返回派生分数已回填的新意见列表，保留输入与子类来源字段不变。

    只有 missing 与唯一非 blocked 策略项相配时赋该项 potential；其余全部归零。
    """
    result = []
    # 顺序与调用方意见一致，不按分数重新编号，也不丢弃零收益意见。
    for issue in issues:
        # 只依据 jd_evidence 回链，不从 why/gain/改写文字中猜技能或认可模型自报值。
        matches = [item for item in plan.items if not item.blocked and item.part in ("hard", "semantic")
                   and _matches_issue(issue, item)] if issue.category == "missing" else []
        # 模糊多配不能叠加潜力或随意选一项；同项多条意见也只各标原项值。
        impact = matches[0].potential if len(matches) == 1 else 0
        # Pydantic 子类通过 model_copy 保留 source，不把派生字段写回原对象。
        result.append(issue.model_copy(update={"score_impact": impact}, deep=True))
    return result
