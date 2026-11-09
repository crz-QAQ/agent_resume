"""定义 PRD 第三节的五个输出模型，统一后续 JSON 和报告的数据契约。

字段描述用于结构化输出；本模块只声明数据，不进行打分、解析或渲染。
"""

from pydantic import BaseModel, Field


class MatchScore(BaseModel):
    """承载总分与三项得分，数值由后续评分模块提供。"""
    # PRD §三：匹配度总分。
    total: int = Field(description="匹配度总分，0-100")
    # PRD §三：三项得分。
    breakdown: dict[str, int] = Field(description="分项得分，键为 hard、semantic、llm")


class ResumeKeyword(BaseModel):
    """承载需求3的关键字、分类和出现次数。"""
    # PRD §三需求3：关键字文本。
    name: str = Field(description="简历关键字文本，如 Go")
    # PRD §三需求3：关键字分类。
    category: str = Field(description="类别：language/framework/middleware/db/cloud/tool/domain")
    # PRD §三需求3：关键字权重依据。
    count: int = Field(description="关键字在简历中出现的次数")


class ResumeHighlight(BaseModel):
    """承载需求4的简历主要项标签及内容。"""
    # PRD §三需求4：主要项名称。
    label: str = Field(description="简历主要项标签，如工作年限、最近项目、技术栈")
    # PRD §三需求4：主要项内容。
    value: str = Field(description="简历主要项的值，如 3 年，后端")


class Deduction(BaseModel):
    """承载需求2的扣分理由及双方原文证据。"""
    # PRD §三需求2、§2.5：扣分项。
    item: str = Field(description="扣分项名称")
    # PRD §2.5：扣分类型。
    type: str = Field(description="类型：missing_skill/years_short/education/weak_project/mismatch")
    # PRD §2.5：扣分数值。
    deduct: int = Field(description="该项扣除的分数")
    # PRD §2.5：岗位原文依据。
    jd_evidence: str = Field(description="岗位 JD 原文证据")
    # PRD §2.5：简历原文依据或未提及说明。
    resume_evidence: str = Field(description="简历原文证据，或简历未提及的说明")
    # PRD §2.5：严重程度。
    severity: str = Field(description="严重度：high/medium/low")


class MatchReport(BaseModel):
    """汇总报告字段，供后续接口和 PDF 渲染共用。"""
    # PRD §三：总分与分项得分。
    score: MatchScore = Field(description="匹配度总分和三项得分")
    # PRD §三：按总分分档的结论。
    verdict: str = Field(description="结论：高度匹配/匹配/勉强匹配/不匹配")
    # PRD §三需求3：简历关键字。
    keywords: list[ResumeKeyword] = Field(description="简历关键字及类别、出现次数")
    # PRD §三需求4：简历主要项。
    highlights: list[ResumeHighlight] = Field(description="简历主要项列表")
    # PRD §三需求2：带证据的扣分理由。
    deductions: list[Deduction] = Field(description="带 JD 和简历证据的扣分理由")
    # PRD §三需求8：岗位要求中缺失的地方。
    missing: list[str] = Field(description="相对岗位要求缺失的内容")
    # PRD §三需求8：与岗位不符的地方。
    mismatches: list[str] = Field(description="与岗位要求不符的内容")
    # PRD §三需求5：诚实的补充建议。
    suggestions: list[str] = Field(description="需要补充的内容建议，禁止编造经历")
    # PRD §三：报告元数据。
    meta: dict = Field(description="简历文件名、JD 摘要、模型、成本、耗时等元数据")
