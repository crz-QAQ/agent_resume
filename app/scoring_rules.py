"""按 S3 固定规则计算硬性条件分数，保留每笔扣分及其证据。"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING

from app.schemas_match import Deduction, JobInfo

# 现有简历模型所在模块有客户端初始化副作用，运行时只需要读取输入属性。
if TYPE_CHECKING:
    from app.schemas import ResumeInfo


# v1 简化表，够用即可；同义学历共用等级，多处学历取最高值。
EDU_LEVEL = {"博士": 5, "硕士": 4, "研究生": 4, "本科": 3, "学士": 3,
             "大专": 2, "专科": 2, "高中": 1, "中专": 1}
# 只使用 S3 指定别名，不额外推断技能之间的包含关系。
SKILL_ALIASES = {"golang": "go", "go语言": "go", "postgres": "postgresql",
                 "js": "javascript", "ts": "typescript", "k8s": "kubernetes"}

# PRD v1.3 §2.2 / S3.5 任务书 §二.1：固定同族成员，禁止让模型判定同族。
# 族表只含已评审的四族，Go/Python 等无族技能不允许合并。
SKILL_FAMILIES: dict[str, set[str]] = {
    "llm_providers": {"deepseek", "qwen", "openai", "gpt", "chatgpt", "claude",
                      "gemini", "通义", "文心", "豆包", "kimi", "glm", "智谱",
                      "llama", "mistral"},
    "message_queue": {"kafka", "rabbitmq", "rocketmq", "pulsar"},
    "vector_db": {"chroma", "chromadb", "milvus", "faiss", "qdrant", "weaviate", "pgvector"},
    "cloud": {"aws", "阿里云", "腾讯云", "华为云", "azure", "gcp"},
}

# PRD v1.3 §2.2：上位概念反查同一族，承接 S1.5 的抽取结果。
# 比较前使用 _normalize_skill，让 LLM 产品 API 的大小写和空格不影响归属。
FAMILY_ALIASES: dict[str, set[str]] = {
    "llm_providers": {"llm", "llm api", "llm 产品", "llm 产品 api", "llm 厂商",
                      "大模型", "大模型产品", "大模型 api", "大模型厂商"},
    "message_queue": {"消息队列", "mq"},
    "vector_db": {"向量数据库", "向量库"},
    "cloud": {"云平台", "云厂商", "云服务"},
}

# PRD v1.3 §2.2：族代表名固定，禁止从无序 set 随机选成员充当族名。
FAMILY_LABELS = {"llm_providers": "LLM 产品", "message_queue": "消息队列",
                 "vector_db": "向量数据库", "cloud": "云平台"}


@dataclass
class HardScore:
    """承载 0–40 分、三项明细以及复用 S0 数据模型的扣分记录。"""

    # 固定权重为年限 15、学历 10、必备技能 15。
    score: int
    # 明细键固定为 years / education / skills，供后续汇总读取。
    breakdown: dict[str, int]
    # 保留逐项原因和证据，禁止只返回无法复核的数字。
    deductions: list[Deduction]


def _normalize_skill(skill: str) -> str:
    """小写、去首尾及内部空白后，应用指定的技能别名表。"""
    # split 同时处理空格、换行与制表符，避免格式差异造成未命中。
    normalized = "".join(skill.lower().split())
    return SKILL_ALIASES.get(normalized, normalized)


def _skill_family(normalized: str) -> str | None:
    """先按具体成员、再按上位概念别名查族；未命中保持无族。"""
    for family, members in SKILL_FAMILIES.items():
        if normalized in members:
            return family
    # 上位概念也去内部空白，避免提示词修正后反而让 DeepSeek 无法命中。
    for family, aliases in FAMILY_ALIASES.items():
        if normalized in {_normalize_skill(alias) for alias in aliases}:
            return family
    return None


def _group_required_skills(skills: list[str]) -> list[tuple[str | None, list[str]]]:
    """仅合并同族必备项，按输入顺序保留各组原始名称作为扣分依据。"""
    groups: list[tuple[str | None, list[str]]] = []
    family_names: dict[str, list[str]] = {}
    for skill in skills:
        family = _skill_family(_normalize_skill(skill))
        if family is None:
            # 无族条目逐项计入分母，沿用原口径而不是扩大同义词或族表。
            groups.append((None, [skill]))
        elif family not in family_names:
            # 两处共享本地名称列表，后续同族项只追加说明，不新增分母。
            family_names[family] = [skill]
            groups.append((family, family_names[family]))
        elif skill not in family_names[family]:
            family_names[family].append(skill)
    # 字典和列表保留首次出现顺序；输出不依赖集合的哈希顺序。
    return groups


def _education_level(text: str) -> int | None:
    """返回文本中出现的最高学历等级，未提及表内学历时返回 None。"""
    # default=None 保留未知状态，不把没学历证据解释为已知最低等级。
    return max((level for word, level in EDU_LEVEL.items() if word in text), default=None)


def _years_evidence(text: str) -> str:
    """提取带工作年限表述的原文行，找不到时明确标记未提及。"""
    # 中文数字仅用来定位证据，不在此重新估算或覆盖结构化年限。
    number = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百]+)\s*年"
    pattern = rf"{number}\s*(?:工作|从业)?\s*(?:经验|经历)|(?:工作|工龄|从业)\s*{number}"
    for line in text.splitlines():
        if re.search(pattern, line):
            return line.strip()
    return "简历未提及工作年限"


def score_hard(resume: ResumeInfo, job: JobInfo, resume_text: str) -> HardScore:
    """硬性条件打分：年限 15 + 学历 10 + 必备技能 15，总分 40。

    resume 和 job 为已有结构化输入；resume_text 必须传入，供学历判断、
    技能漏抽兜底及简历证据截取。返回分数与扣分守恒的 HardScore。
    """
    deductions: list[Deduction] = []
    # 显式判断 None，保证下限 0 优先于上限，且未知年限不会变成 0 年。
    jd_req = job.required_years_min
    if jd_req is None:
        jd_req = job.required_years_max
    years_score = 15
    if jd_req is not None:
        # 判断顺序与 S3 §二 A 一致，尤其不能把 None 与 0 混在同一分支。
        if resume.years is None:
            years_score, years_type = 0, "years_unknown"
        elif resume.years >= jd_req:
            years_score, years_type = 15, "years_short"
        elif jd_req - resume.years < 2:
            years_score, years_type = 8, "years_short"
        else:
            # 恰好差两年也扣满 15 分，比较运算不能写成小于等于。
            years_score, years_type = 0, "years_short"
        if years_score < 15:
            # JobInfo 没有 JD 原文；明确标识字段来源，不能伪称重建文本是原文。
            bounds = f"下限={job.required_years_min}，上限={job.required_years_max}"
            deductions.append(Deduction(
                item="工作年限", type=years_type, deduct=15 - years_score,
                jd_evidence=f"JD 年限要求（结构化字段）：{bounds}",
                # 未知保持独立类型；不能凭原文猜测数值后偷偷替换输入。
                resume_evidence=("简历未提及工作年限" if resume.years is None
                                 else _years_evidence(resume_text)),
                severity="high",
            ))

    # JD 的明确豁免优先；无法匹配学历等级时也不凭空设置学历门槛。
    jd_education = job.education or ""
    jd_level = 0 if any(word in jd_education for word in ("不限", "无要求")) else (
        _education_level(jd_education) or 0
    )
    resume_level = _education_level(resume_text)
    education_score = 10
    if jd_level and (resume_level is None or resume_level < jd_level):
        # 不足和未知都得零分，但保留不同类型，供报告区分事实与信息缺失。
        education_score = 0
        # 学历从原文匹配，不依赖结构化简历是否抽取到了学历字段。
        evidence = next((line.strip() for line in resume_text.splitlines()
                         if _education_level(line) == resume_level), "简历未提及") if (
            resume_level is not None
        ) else "简历未提及"
        deductions.append(Deduction(
            item="学历", type="education_unknown" if resume_level is None else "education",
            deduct=10, jd_evidence=jd_education, resume_evidence=evidence, severity="high",
        ))

    # 仅归一化技能名；原文严格按任务书只转小写，保留子串匹配口径。
    resume_skills = {_normalize_skill(skill) for skill in resume.skills}
    lower_text = resume_text.lower()
    required = _group_required_skills(job.must_have)
    missing: list[str] = []
    for family, names in required:
        # 族内任一具体成员即可命中；泛称本身不能凭空证明用过某个产品。
        members = SKILL_FAMILIES[family] if family else {_normalize_skill(names[0])}
        if any(member in resume_skills or member in lower_text for member in members):
            continue
        # 举例仅使用输入里的原始条目，不补造 JD 未提及的厂商名称。
        # 只有上位概念的输入也保留该原项，方便回查抽取字段。
        label = f"{FAMILY_LABELS[family]}（举例：{'、'.join(names)}）" if family else names[0]
        missing.append(label)
    skills_score = 15
    if required:
        # PRD v1.3 §2.2：分母只在同族内部去重，其余条目继续独立计分。
        count = len(required)
        # int(x + 0.5) 执行四舍五入，避免默认的银行家舍入使 .5 向偶数取整。
        skills_score = int(15 * (count - len(missing)) / count + 0.5)
        per_missing = int(15 / count + 0.5)
        remaining = 15 - skills_score
        for index, skill in enumerate(missing):
            # 前面的条目按 15/N 四舍五入；最后一条吸收全部余数，不能丢分。
            deduct = remaining if index == len(missing) - 1 else per_missing
            deductions.append(Deduction(
                item=f"必备技能：{skill}", type="missing_skill", deduct=deduct,
                jd_evidence=skill, resume_evidence="简历未提及", severity="high",
            ))
            remaining -= deduct

    # 技能子项单独校验，避免其他子项的扣分偶然抵消技能金额错误。
    assert sum(d.deduct for d in deductions if d.type == "missing_skill") == 15 - skills_score
    # 每次调用都执行守恒断言，避免只在验收样本里碰巧对上总分。
    breakdown = {"years": years_score, "education": education_score, "skills": skills_score}
    score = sum(breakdown.values())
    assert sum(d.deduct for d in deductions) == 40 - score
    return HardScore(score=score, breakdown=breakdown, deductions=deductions)
