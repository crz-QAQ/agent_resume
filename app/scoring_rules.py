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
    missing = [skill for skill in job.must_have
               if _normalize_skill(skill) not in resume_skills
               and _normalize_skill(skill) not in lower_text]
    skills_score = 15
    if job.must_have:
        # 分母保持 JD 条目数；只对简历技能建集合，避免改变岗位权重。
        count = len(job.must_have)
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

    # 每次调用都执行守恒断言，避免只在验收样本里碰巧对上总分。
    breakdown = {"years": years_score, "education": education_score, "skills": skills_score}
    score = sum(breakdown.values())
    assert sum(d.deduct for d in deductions) == 40 - score
    return HardScore(score=score, breakdown=breakdown, deductions=deductions)
