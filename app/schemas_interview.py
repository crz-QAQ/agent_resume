"""N0 面试契约：字段校验只依赖 Pydantic，不调用模型或读取外部资料。"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator


# 任务书 §2.1：展示顺序与技术面配额分别维护，HR 不计入技术面。
PART_ORDER = ("hr", "tech", "scenario", "project")
# PRD §1.5 按真实面试占比分配 6+2+2，分别覆盖技术深挖、业务判断与项目真实性。
# 配额由代码固定，避免模型偏向通用技术题而漏掉场景和项目自述。
QUOTA = {"tech": 6, "scenario": 2, "project": 2}
# HR 的 5~8 条上下限来自 M1.5 验收口径：既覆盖核心话题，也限制重复追问。
HR_MIN, HR_MAX = 5, 8
# id 必须由服务端生成，模型不得输出，防止模型自行改编号打乱分区排序。
# 答案按 id 关联题目，前缀稳定才能避免跨分区引用错误。
ID_PREFIX = {"hr": "Q-HR-", "tech": "Q-T-", "scenario": "Q-S-", "project": "Q-P-"}

# 考点 code 是接口契约；HR 可重复选类，技术面的十个考点需完整覆盖。
CATEGORY_BY_PART = {
    # tech_depth 考核心必备技能的理解深度，避免仅凭技能名判断熟练程度。
    # tech_gap 考 JD 缺口技能的迁移思路，不能把简历未提及的能力当作既有经验。
    # impl_detail 考真实实现过程，用细节区分亲手做过与只听过方案。
    # tradeoff 考选型依据，避免只背结论而说不清为什么放弃其他方案。
    # debug 考生产故障定位，检验能否从现象收集证据并缩小问题范围。
    # growth 考新知识的落地程度，区分学习经历与经过验证的实际应用。
    "tech": ("tech_depth", "tech_gap", "impl_detail", "tradeoff", "debug", "growth"),
    # scenario_design 考业务需求拆解与方案设计，防止技术知识脱离实际交付。
    # scenario_risk 考能力或条件不足时的风险沟通，避免承诺无法兑现的交付。
    "scenario": ("scenario_design", "scenario_risk"),
    # project_random_1 考系统抽中的首段真实项目，避免总让候选人挑最熟的一段。
    # project_random_2 考另一段项目的职责与结果，防止只靠单段背熟的项目自述。
    "project": ("project_random_1", "project_random_2"),
    # career_switch 考检测到跨行后的转行动机，避免对同岗位族候选人无效追问。
    # motivation 考离职或转岗诉求，判断岗位是否能回应候选人的真实期待。
    # salary 考薪酬预期，尽早暴露与岗位条件不一致的假设。
    # plan 考职业方向与能力建设，了解候选人的选择是否有持续投入的依据。
    # collaboration 考协作冲突处理，防止只描述个人成果而忽略团队中的实际行为。
    "hr": ("career_switch", "motivation", "salary", "plan", "collaboration",
           # years_gap 考年限不足的补偿证据，避免用工作时长直接替代能力判断。
           # timeline 考异常时间段的事实说明，不由系统替候选人补写经历。
           # other 为岗位相关的其他 HR 话题留出空间，避免把自由发挥压成固定题库。
           "years_gap", "timeline", "other"),
}
TECH_CATEGORY_ORDER = ("tech_depth", "tech_gap", "impl_detail", "tradeoff", "debug", "growth")

# 所有字数按 Python 字符数计算，边界均包含端点。
# 来源 PRD §3.3：题干需足以说明问题，同时避免背景过长掩盖要问的内容。
QUESTION_LEN = (20, 120)
# 来源 PRD §3.3：要点用于逐条自查和评分，长度限制防止把整段范文塞成一条。
POINT_MAX = 30
POINTS_N = (3, 6)
# 来源 PRD §3.3：示范作答需展示表达结构，又不能膨胀成难以核实的长篇范文。
REF_LEN = (120, 300)
# 来源 PRD §3.3：至少能继续追问，并控制追问链长度以免单题无限扩张。
FOLLOWUPS_N = (1, 3)
DIFFICULTY_RANGE = (1, 3)
# 任务书 §2.5 / PRD §7.2：每维上限，单题合计十分。
# 5+2+2+1=10：要点命中是唯一客观可复算项，故分配一半分数。
# 其余分数留给结构、深度和一致性，避免只按背全要点评价作答。
SCORE_WEIGHTS = {"points": 5, "structure": 2, "depth": 2, "consistency": 1}


def _validate_plan_id(value: str) -> str:
    """校验完整的三十二位小写十六进制令牌，不接受末尾换行。"""
    # fullmatch 避免正则 $ 在末尾换行之前也能匹配的例外。
    if re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise ValueError("plan_id 必须是 32 位小写十六进制字符")
    return value


class Question(BaseModel):
    """保存单题内容，并检查字数、分区、考点与迁移能力标记。"""

    # id 由服务端提供；保留契约的字段顺序，在 part 校验时核对前缀。
    id: str  # 全计划内唯一；跨题唯一性由 check_id_prefix 检查。
    part: Literal["hr", "tech", "scenario", "project"]  # 字段声明先后供关联校验读取。
    category: str  # 只接受对应分区定义的 code，不允许模型自创。
    difficulty: int  # 单题星级，与题册的 junior/standard/senior 档位分开。
    question: str  # 题干与答案分开，疑问语气独立于长度约束。
    assesses: str  # 简短描述被考察的能力，不承载整段题干。
    # N0 仅查非空；原句可追溯性由后续带原文的护栏检查。
    basis: str
    basis_source: Literal["jd", "resume"]  # 指明依据原句应在哪一份输入中核实。
    answer_points: list[str]  # 后续评分逐条匹配的客观要点。
    reference_answer: str  # 示范作答必须保留，不能仅提供要点列表。
    bonus: list[str]  # 加分内容不并入 answer_points 的数量限制。
    pitfalls: list[str]  # 翻车点用于练习提示，不直接推导成绩。
    follow_ups: list[str]  # 这里只检查数量，具体追问深度由后续阶段确定。
    # 默认值也经过校验，tech_gap 漏填 flag 时同样失败。
    flag: str | None = Field(default=None, validate_default=True)
    source: Literal["model", "injected"]  # 显式区分生成内容与模板兜底内容。

    @field_validator("part")
    @classmethod
    def validate_part(cls, value: str, info: ValidationInfo) -> str:
        """利用已校验的 id 核对当前分区前缀。"""
        # 防止编号指向另一分区，造成展示排序和后续答案关联发生错位。
        question_id = info.data.get("id")
        # id 自身类型不合法时，交给该字段原有的类型错误处理。
        if question_id is not None and not question_id.startswith(ID_PREFIX[value]):
            raise ValueError(f"{value} 的 id 必须以 {ID_PREFIX[value]} 开头")
        return value

    @field_validator("category")
    @classmethod
    def validate_category(cls, value: str, info: ValidationInfo) -> str:
        """限制考点为当前分区定义的 code。"""
        # 防止模型自创考点或把场景题塞进技术问答，破坏固定考点覆盖。
        part = info.data.get("part")
        if part is not None and value not in CATEGORY_BY_PART[part]:
            raise ValueError(f"{part} 不允许考点 {value}")
        return value

    @field_validator("difficulty")
    @classmethod
    def validate_difficulty(cls, value: int) -> int:
        """把单题难度限制在一至三星。"""
        # 防止模型输出任意星级或百分比，导致不同题目的难度口径无法比较。
        if not DIFFICULTY_RANGE[0] <= value <= DIFFICULTY_RANGE[1]:
            raise ValueError("difficulty 必须为 1~3")
        return value

    @field_validator("question")
    @classmethod
    def validate_question(cls, value: str) -> str:
        """同时校验题干长度与任务书规定的疑问语气。"""
        # 防止过短提示、冗长背景或普通陈述句被当成可直接练习的面试题。
        if not QUESTION_LEN[0] <= len(value) <= QUESTION_LEN[1]:
            raise ValueError("question 必须为 20~120 字")
        # 英文问号不代替任务书明确指定的中文问号。
        if not (value.endswith("？") or any(
            word in value for word in ("怎么", "如何", "为什么", "请讲", "谈谈")
        )):
            raise ValueError("question 必须是疑问句")
        return value

    @field_validator("assesses")
    @classmethod
    def validate_assesses(cls, value: str) -> str:
        """限制考察点短句的长度。"""
        # 防止把题干或解释整段复制到考察点，挤占题卡中的能力提示位置。
        if len(value) > 20:
            raise ValueError("assesses 不得超过 20 字")
        return value

    @field_validator("basis")
    @classmethod
    def validate_basis(cls, value: str) -> str:
        """拒绝空依据和仅含空白的依据，保留原句原样。"""
        # 防止没有任何证据的题进入后续护栏，并用空白伪装成已提供依据。
        if not value.strip():
            raise ValueError("basis 不得为空")
        return value

    @field_validator("answer_points")
    @classmethod
    def validate_answer_points(cls, value: list[str]) -> list[str]:
        """要求三至六条短要点，每条至多三十字且没有换行。"""
        # 防止整段范文伪装成短要点，导致逐条命中计算失去明确的评分单位。
        if not POINTS_N[0] <= len(value) <= POINTS_N[1]:
            raise ValueError("answer_points 必须有 3~6 条")
        for point in value:
            if len(point) > POINT_MAX:
                raise ValueError("answer_points 每条不得超过 30 字")
            # 同时拒绝 CRLF、Unicode 行分隔符及 Python 支持的其他换行。
            if any(char in point for char in "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"):
                raise ValueError("answer_points 不许含换行")
        return value

    @field_validator("reference_answer")
    @classmethod
    def validate_reference_answer(cls, value: str) -> str:
        """限制示范长度，并禁止承诺性质的指定用语。"""
        # 防止示范过于简略或冗长，并防止模型把参考表达包装成保证通过的答案。
        if not REF_LEN[0] <= len(value) <= REF_LEN[1]:
            raise ValueError("reference_answer 必须为 120~300 字")
        if any(word in value for word in ("标准答案", "必过")):
            raise ValueError("reference_answer 禁止含标准答案或必过")
        return value

    @field_validator("bonus", "pitfalls", "follow_ups")
    @classmethod
    def validate_short_lists(cls, value: list[str], info: ValidationInfo) -> list[str]:
        """校验加分点、翻车点和追问各有一至三条。"""
        # 防止题卡缺少练习提示，或因无限追加提示与追问而失去单题范围。
        # 三个列表的数量区间相同，追问数量常量保留为唯一数值来源。
        if not FOLLOWUPS_N[0] <= len(value) <= FOLLOWUPS_N[1]:
            raise ValueError(f"{info.field_name} 必须有 1~3 条")
        return value

    @field_validator("flag")
    @classmethod
    def validate_flag(cls, value: str | None, info: ValidationInfo) -> str | None:
        """强制缺口题带固定说明，其余类别必须为 None。"""
        # 防止模型把简历未提及的技能当作已会，也防止其他题滥用迁移能力标记。
        category = info.data.get("category")
        # 本次用户指令优先于 PRD 中项目题降级标记的描述。
        if category == "tech_gap":
            if value != "简历未提及，考察迁移能力":
                raise ValueError("tech_gap 的 flag 必须为简历未提及，考察迁移能力")
        elif value is not None:
            raise ValueError("非 tech_gap 的 flag 必须为 None")
        return value


class InterviewMeta(BaseModel):
    """记录题册的生成状态、版本、成本和随机抽取种子。"""

    # N0 不调用模型，验收数据的 model 为 None、成本恒为零。
    status: Literal["ok", "needs_review", "failed"]  # needs_review 与 failed 独立保留。
    model: str | None  # 必填但可空；None 表示未调用模型。
    prompt_version: str  # 保留生成时的提示词版本，便于后续审计。
    cost_yuan: float = 0.0  # 单位为人民币元，N0 缺省零成本。
    duration_ms: int = 0  # 总耗时的单位为毫秒。
    injected_count: int = 0  # 记录兜底注入数量，不在契约层重新推导。
    # 后续项目抽样使用 sha256(plan_id)[:16]；这里仅承载种子。
    project_seed: str | None = None
    warnings: list[str] = Field(default_factory=list)  # 每份题册单独持有警告列表。


class NextAction(BaseModel):
    """为 API、MCP 和一次性调用方提供下一步状态提示。"""

    # 使用剩余题目 id，支持乱序与重复作答，无需题号游标。
    type: Literal["answer", "answer_remaining", "can_grade", "done", "restart"]
    suggest: list[str] = Field(default_factory=list)  # 可同时建议多题，支持跳题。
    hint: str = ""  # 供调用方展示的解释文字，可省略。


class RejectedItem(BaseModel):
    """记录被丢弃题目的分区和原因。"""

    # 保留 str 契约，供后续护栏记录失败输入的分区原值。
    part: str
    reason: str


class InterviewPlan(BaseModel):
    """聚合四个分区及审计信息，结构配额交给纯函数校验器。"""

    plan_id: str  # 唯一能力令牌；此处校验格式，不生成或登记会话。
    job_title: str  # 岗位名必须有实际内容，不能只有空白。
    source: Literal["user_jd", "inferred"]  # 区分用户 JD 与推测岗位画像。
    # inferred 的确认状态按任务书约定，由服务端另行校验。
    profile_confirmed: bool
    difficulty: Literal["junior", "standard", "senior"]
    parts: dict[str, list[Question]]  # 保留四个固定分区，题量由纯函数检查。
    coverage: dict[str, int]  # 技术面十个考点的摘要，不包含 HR。
    rejected: list[RejectedItem]  # 被丢弃题目的原因仍需向上层保留。
    meta: InterviewMeta  # 审计信息有独立契约，避免任意字段拼写。
    # N0 不生成 PDF；字段为后续阶段保留可空字符串契约。
    pdf_url: str | None = None
    next_action: NextAction | None = None  # N0 允许尚未绑定答题流程。

    @field_validator("plan_id")
    @classmethod
    def validate_plan_id(cls, value: str) -> str:
        """校验题册的能力令牌格式。"""
        # 防止长度不足、含大写或夹带换行的令牌进入题册会话的标识字段。
        return _validate_plan_id(value)

    @field_validator("job_title")
    @classmethod
    def validate_job_title(cls, value: str) -> str:
        """拒绝空岗位名称，保留用户输入的实际文本。"""
        # 防止题册没有明确目标岗位，后续仍被作为已经对齐岗位的产物使用。
        if not value.strip():
            raise ValueError("job_title 不得为空")
        return value

    @field_validator("parts")
    @classmethod
    def validate_parts(cls, value: dict[str, list[Question]]) -> dict[str, list[Question]]:
        """要求四个固定键齐全，并逐题核对容器分区。"""
        # 防止分区缺失、多出未知分区或合法单题被装错容器，误导展示与配额检查。
        if set(value) != set(PART_ORDER):
            raise ValueError("parts 必须且只能包含 hr、tech、scenario、project 四个键")
        for part, questions in value.items():
            for question in questions:
                if question.part != part:
                    raise ValueError(f"{question.id} 的 part 与所在 parts 键 {part} 不一致")
        return value

    @field_validator("coverage")
    @classmethod
    def validate_coverage(cls, value: dict[str, int]) -> dict[str, int]:
        """要求覆盖摘要准确声明十个非 HR 考点各一次。"""
        # 防止摘要漏报必考 code、重复计数，或把 HR 考点混入技术面的十项约定。
        expected = {code: 1 for part in QUOTA for code in CATEGORY_BY_PART[part]}
        # 实际题目是否满足此摘要由 check_coverage 独立计算，不能相信自报值。
        if value != expected:
            raise ValueError("coverage 必须包含十个技术面考点且各为 1，HR 不计")
        return value


class AnswerItem(BaseModel):
    """承载一题的原始作答，空串表示未作答。"""

    # id 是否属于特定题册由持有该题册的服务端核对。
    id: str  # 与 Question.id 原样关联，不能在这里重编号。
    answer: str  # 保留空串与空白，未作答判定交给服务端。

    @field_validator("answer")
    @classmethod
    def validate_answer(cls, value: str) -> str:
        """保留作答原文，拒绝超过两千字的单题答案。"""
        # 防止超长单题作答挤占处理资源，且不能通过静默截断丢失用户原话。
        if len(value) > 2000:
            raise ValueError("answer 不得超过 2000 字")
        return value


class AnswerSheet(BaseModel):
    """保存增量答案及剩余题目，计数由服务端计算。"""

    plan_id: str
    answers: list[AnswerItem]  # 增量提交可只包含本次更新的题目。
    # 接口允许部分作答；不把缺答题伪装成零分答案。
    answered: int = 0
    missing_ids: list[str] = Field(default_factory=list)  # 明确记录尚未完成的题目编号。
    next_action: NextAction | None = None

    @field_validator("plan_id")
    @classmethod
    def validate_plan_id(cls, value: str) -> str:
        """让答案表使用与题册完全相同的令牌格式。"""
        # 防止答案表接受题册不认可的令牌格式，使增量作答无法关联同一会话。
        return _validate_plan_id(value)


class ConsistencyIssue(BaseModel):
    """记录作答相对简历与 JD 新增的技术、数字或项目。"""

    # 新信息需要提示核实，不在契约层推断其真实性。
    kind: Literal["new_tech", "new_number", "new_project"]
    value: str  # 保存需要核实的具体词语、数字或项目名。
    hint: str = ""  # 提醒面试追问风险，不将新信息自动判为虚假。


def _empty_consistency() -> dict:
    """为每条评分创建独立的一致性默认值。"""
    # 每次重新构造嵌套列表，避免不同评分之间共享可变状态。
    return {"ok": True, "issues": []}


class GradeItem(BaseModel):
    """记录单题分数、要点命中、深度和一致性提示。"""

    id: str
    part: str
    score: int  # 单题分数由后续评分公式产生，契约只查范围。
    level: Literal["危险", "需练", "可投", "强"]  # 使用 PRD 统一档位用词。
    hit_points: list[str] = Field(default_factory=list)  # 实际命中的评分要点。
    miss_points: list[str] = Field(default_factory=list)  # 没有命中的要点需显式列出。
    # 对外保持 dict 契约，issues 的各项仍按 ConsistencyIssue 校验。
    consistency: dict = Field(default_factory=_empty_consistency)
    depth: int = 0  # 0 结论、1 做法、2 取舍或踩坑。
    next_step: str = ""  # 针对本题的下一步练习建议。
    answered: bool = True  # 未答题的表示不依赖 score 是否为零。

    @field_validator("score")
    @classmethod
    def validate_score(cls, value: int) -> int:
        """限制单题总分在零至十分。"""
        # 防止越界分数进入累计结果，破坏十分制的解释与总分上限。
        if not 0 <= value <= 10:
            raise ValueError("score 必须为 0~10")
        return value

    @field_validator("depth")
    @classmethod
    def validate_depth(cls, value: int) -> int:
        """限制深度为结论、做法、取舍三个层级。"""
        # 防止深度维度越权占用其他维度的分值，使四维评分公式失去可复算性。
        if not 0 <= value <= 2:
            raise ValueError("depth 必须为 0~2")
        return value

    @field_validator("consistency")
    @classmethod
    def validate_consistency(cls, value: dict) -> dict:
        """检查一致性字典的布尔状态与问题列表，输出可序列化字典。"""
        # 防止任意字典冒充一致性检查结果，导致冲突提示无法按约定读取和展示。
        if not isinstance(value.get("ok"), bool) or not isinstance(value.get("issues"), list):
            raise ValueError("consistency 必须包含布尔 ok 和 issues 列表")
        # 校验嵌套 code，防止任意字典绕过 ConsistencyIssue 的枚举约束。
        issues = [ConsistencyIssue.model_validate(issue).model_dump() for issue in value["issues"]]
        return {**value, "issues": issues}


class GradeTotal(BaseModel):
    """保存技术面总分、HR 命中率和已答题统计。"""

    tech: int  # 技术面十分乘十题，总分上限为一百。
    hr_hit_rate: float  # HR 单独报告命中率，不并入技术面总分。
    band: Literal["危险", "需练", "可投", "强"]  # 档位计算留给评分引擎。
    # 计数和档位的计算属于评分阶段，不在 N0 重新实现公式。
    answered: int
    total_questions: int  # 与已答数量同时返回，体现部分作答的分母。

    @field_validator("tech")
    @classmethod
    def validate_tech(cls, value: int) -> int:
        """限制技术面十分乘十题的总分范围。"""
        # 防止负分或超满分汇总流入报告，造成档位与百分制总分含义不一致。
        if not 0 <= value <= 100:
            raise ValueError("tech 必须为 0~100")
        return value

    @field_validator("hr_hit_rate")
    @classmethod
    def validate_hr_hit_rate(cls, value: float) -> float:
        """限制 HR 命中率为零至一，拒绝非有限数。"""
        # 防止百分数、负数或非有限值冒充比例，误导 HR 要点命中率的展示。
        if not 0 <= value <= 1:
            raise ValueError("hr_hit_rate 必须为 0~1")
        return value


class GradeReport(BaseModel):
    """聚合评分条目、总分、建议与后续动作。"""

    plan_id: str
    items: list[GradeItem]  # 保留逐题明细，供用户定位失分。
    total: GradeTotal  # 总分结构与单题结构分开约束。
    advice: list[str] = Field(default_factory=list)  # 整份报告的练习建议。
    # meta 的模型调用与估算字段由后续评分器写入。
    meta: dict = Field(default_factory=dict)
    pdf_url: str | None = None
    next_action: NextAction | None = None

    @field_validator("plan_id")
    @classmethod
    def validate_plan_id(cls, value: str) -> str:
        """让评分报告引用有效格式的题册令牌。"""
        # 防止评分报告引用格式无效的题册令牌，导致报告与答题会话无法回链。
        return _validate_plan_id(value)
