"""N0 结构校验纯函数：只读题册，不调用模型、网络或存储。"""

from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
from itertools import combinations
import re
import unicodedata

from app.schemas_interview import (
    CATEGORY_BY_PART, HR_MAX, HR_MIN, ID_PREFIX, PART_ORDER, QUOTA,
    InterviewPlan, Question,
)


def validate_plan(plan: InterviewPlan) -> list[str]:
    """返回题册的结构错误；空列表表示配额、覆盖、编号和去重均通过。"""
    # 拦截单题分别合法、合在一起却缺题或重复的题册，以及构造后被改坏的分区。
    # 这些错误依赖整份题册才能发现，必须在交付题册前统一检查，不能只信字段校验。
    errors: list[str] = []
    # 模型构造时已有字段校验；此处也检测构造后被修改的容器结构。
    if set(plan.parts) != set(PART_ORDER):
        errors.append("parts 必须且只能包含 hr、tech、scenario、project 四个键")
    questions = [question for group in plan.parts.values() for question in group]  # 去重范围覆盖 HR。
    for part, group in plan.parts.items():
        for question in group:
            if question.part != part:
                errors.append(f"{question.id} 的 part 与所在 parts 键 {part} 不一致")

    # 覆盖从实际题目重新计算，不能因为 coverage 自报为 1 就认定通过。
    expected = {code: 1 for part in QUOTA for code in CATEGORY_BY_PART[part]}
    if plan.coverage != expected:
        errors.append("coverage 必须包含十个技术面考点且各为 1，HR 不计")
    errors.extend(check_quota(plan.parts))  # 缺题与超额均返回错误，不静默补题。
    errors.extend(check_coverage(plan.parts))  # 即使配额错误也继续收集缺失考点。
    errors.extend(check_id_prefix(questions))  # 编号唯一性作用于整份题册。
    errors.extend(check_duplicate(questions))  # 不同分区也可能提出重复问题。
    return errors


def check_quota(parts: dict) -> list[str]:
    """检查 tech 为六题、scenario 与 project 各两题、HR 五至八题。"""
    # 拦截生成不足、护栏丢弃后缺额，以及模板补题或重试合并造成的超额。
    # 单题模型不知道分区最终数量，因此必须在组合后的列表上核对固定配比。
    errors: list[str] = []
    for part, required in QUOTA.items():
        # 缺失分区视为零题，既不抛 KeyError 也不跳过错误。
        count = len(parts.get(part, []))
        if count != required:
            errors.append(f"{part} 需要 {required} 条，实际 {count} 条")
    hr_count = len(parts.get("hr", []))  # HR 使用区间，与固定技术配额分开检查。
    if not HR_MIN <= hr_count <= HR_MAX:
        errors.append(f"hr 需要 {HR_MIN}~{HR_MAX} 条，实际 {hr_count} 条")
    return errors


def check_coverage(parts: dict) -> list[str]:
    """逐分区检查十个技术面考点各恰好一次，HR 不计。"""
    # 拦截题数凑够却集中在同一考点的题册，也拦截跨分区使用考点 code。
    # coverage 摘要可能只是自报值，必须从实际题目重算，才能发现被遗漏的必考项。
    errors: list[str] = []
    for part in QUOTA:
        counts = Counter(question.category for question in parts.get(part, []))  # 不采信摘要字段。
        # 按契约顺序报告缺失和重复，保持同一输入输出稳定。
        for category in CATEGORY_BY_PART[part]:
            count = counts[category]
            if count == 0:
                errors.append(f"考点缺失：{part}/{category} 需要 1 条，实际 0 条")
            elif count > 1:
                errors.append(f"考点重复：{part}/{category} 需要 1 条，实际 {count} 条")
        for category in counts:
            if category not in CATEGORY_BY_PART[part]:
                errors.append(f"考点非法：{part} 不允许 {category}")
    return errors


def check_id_prefix(questions: list[Question]) -> list[str]:
    """核对 id 与单题分区前缀，并检查整份题册的 id 唯一性。"""
    # 拦截分区编号错配及重试合并、模板注入后重复使用同一 id 的题目。
    # 编号唯一性只有遍历整份题册才能确认，否则后续按 id 覆盖答案时会写错题。
    errors: list[str] = []
    seen: set[str] = set()  # 只保存局部集合，不给 Question 回写任何状态。
    for question in questions:
        prefix = ID_PREFIX.get(question.part)
        if prefix is None or not question.id.startswith(prefix):
            errors.append(f"id 前缀错误：{question.id} 与分区 {question.part} 不匹配")
        # 即便前缀错，也继续报告重复，便于调用方一次修复全部问题。
        if question.id in seen:
            errors.append(f"id 重复：{question.id}")
        seen.add(question.id)  # 后续每个重复编号都能定位到具体输入。
    return errors


def check_duplicate(questions: list[Question], threshold: float = 0.8) -> list[str]:
    """归一化后逐对比较，相似度大于等于阈值时返回重复错误。"""
    # 拦截换了编号、考点或礼貌前缀，却仍在重复问同一件事的题目。
    # 必须跨分区比较最终题单，防止相同问题分别出现在技术问答和项目自述中。
    # 0.8 来自 N0 §三 / PRD 红线 7，衡量的是面试题之间的重复。
    # resume_rewrite.JD_SIMILARITY_LIMIT 的 0.7 针对改写文本抄 JD，属于另一套场景。
    # 两个阈值需分别调整，故意不复用常量，避免修改一处影响另一道闸门。
    if not 0 <= threshold <= 1:
        raise ValueError("threshold 必须为 0~1")
    normalized = [(question.id, normalize_question(question.question)) for question in questions]  # 每题只归一化一次。
    errors: list[str] = []
    for (left_id, left), (right_id, right) in combinations(normalized, 2):
        # 题干最多 120 字；禁用热门字符启发式，按完整序列计算相似度。
        ratio = SequenceMatcher(None, left, right, autojunk=False).ratio()
        if ratio >= threshold:  # 边界值也属于重复，不能改成严格大于。
            errors.append(f"题目重复：{left_id} / {right_id}，相似度 {ratio:.3f}")
    return errors


def normalize_question(text: str) -> str:
    """去空白与标点，反复去除请、谈谈、说说、介绍一下前缀，再转小写。"""
    # 消除排版、大小写及礼貌开场差异，防止同一问题换个表面形式就逃过去重。
    # 先统一比较文本，才能让相似度反映实质题干差异，而非标点或称呼的差异。
    # Unicode P 类覆盖中英文标点；空白一并去掉，避免排版差异绕过去重。
    normalized = "".join(
        char for char in text.strip().lower()
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )
    # 重复匹配处理「请介绍一下」「请谈谈」等组合前缀。
    return re.sub(r"^(?:(?:介绍一下|谈谈|说说|请))+", "", normalized)
