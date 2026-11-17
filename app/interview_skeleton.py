"""N1 纯规则骨架：复用既有证据、技能族及岗位族，不加载模型或读取文件。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha256
from itertools import combinations
import random
import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from app.guard_revision import find_original, normalize_text
from app.job_profile import GENERIC_MUST_HAVE, GENERIC_RESPONSIBILITIES, PROFILE_TEMPLATES, match_family
from app.schemas_interview import CATEGORY_BY_PART, HR_MAX, HR_MIN, PART_ORDER, TECH_CATEGORY_ORDER
from app.schemas_match import JobInfo
from app.schemas_revision import DiagnosisReport, JobProfile
from app.scoring_rules import HardScore, SKILL_ALIASES, SKILL_FAMILIES, normalize_skill, skill_family

# 这些旧模块会初始化客户端或加载向量依赖；这里只读取调用方传来的属性。
if TYPE_CHECKING:
    from app.schemas import ResumeInfo
    from app.scoring_llm import LlmScore
    from app.scoring_semantic import SemanticScore


# 说明只能放在 flag、anchor 或 warnings，不能进入出题依据。
_BASIS_NOTE = re.compile(r"兜底|可追溯|占位|TODO", re.IGNORECASE)
_MANUAL_BASIS = "无可追溯依据，需人工补充"
_PROFILE_BASIS = "依据来自岗位画像（推测），非真实 JD"
_GENERIC_PROFILE_BASIS = "岗位画像为通用兜底：建议补充真实 JD 后重新生成"
_APPROXIMATE_BASIS = "依据为近似对齐，原文表述不同"
# 实测「深入一线业务识别值得AI解决的问题」能对齐原文「深入一线业务,与业务负责人和一线员工共同工作…」（sim=0.588）。
# 0.55 在保留压缩职责与避免错配之间取折中：调高会大量丢题，调低会把不相干职责对齐。
# 阈值只决定是否标记近似，不能把上游压缩措辞误当成候选人缺少相关经历。
_ALIGNMENT_THRESHOLD = 0.55
_REQUIREMENT_CATEGORIES = frozenset({"tech_gap", "scenario_design", "scenario_risk"})
_GENERIC_PROFILE_ITEMS = frozenset(normalize_text(item) for item in [*GENERIC_MUST_HAVE, *GENERIC_RESPONSIBILITIES])
_MULTILINE_BASIS = frozenset({"scenario_design", "scenario_risk", "years_gap"})
# S5 的固定分项上限；不导入 scoring_llm，以免初始化其模型调用链。
LLM_DIMENSIONS = {"stack_depth": 12, "domain_fit": 10, "proof_strength": 8}
# 月份转为整数便于闭区间比较；至今使用固定上界，不读取当前日期。
_OPEN_END = 9999 * 12 + 11
_DATE = re.compile(
    r"(?<!\d)(?P<sy>(?:19|20)\d{2})(?:[./年-](?P<sm>0?[1-9]|1[0-2])(?!\d)月?)?"
    r"(?:\s*[-—–~～至到]+\s*(?:(?P<ey>(?:19|20)\d{2})"
    r"(?:[./年-](?P<em>0?[1-9]|1[0-2])(?!\d)月?)?|(?P<open>至今|今|现在|目前)))?"
)
# 不把逗号拆开，避免将实现手段与它对应的结果割裂成两个证据。
_SENTENCE = re.compile(r"[^。；;！？!?\n]+[。；;！？!?]?")
_PROJECT_WORD = re.compile(r"系统|平台|项目|Demo|中台|模块|链路", re.IGNORECASE)
_INTENT = re.compile(r"(?:求职意向|期望岗位|意向岗位)\s*[:：]\s*(.+)")
# 标题即使包含技术词也不能作为事实句；先剥离项目符号再判断，覆盖「·职位描述：」。
_LABEL_PREFIXES = ("内容", "技术栈", "工作经历", "项目经历", "教育经历", "个人优势",
                   "职位描述", "你要做的事情", "我们希望你")
_DUTY_VERBS = ("负责", "开发", "设计", "优化", "搭建", "实现", "排查", "重构", "接入", "上线", "维护", "支撑")
# 别名表只列异名，规范技术名还需覆盖现有项目使用的语言、数据库与框架。
# SaaS、FDE 等业务或岗位标签不属于这里的技术词，避免子标题通过最小信息量检查。
_TECH_TERMS = frozenset({
    "python", "php", "thinkphp", "java", "mysql", "redis", "gin", "gorm", "docker",
    "linux", "git", "jenkins", "langchain", "dify", "rag", "llm", "agent", "api",
    "http", "grpc", "sql", "websocket", "html5", "css", "codex", "minimax",
    *SKILL_ALIASES.values(), *(term for members in SKILL_FAMILIES.values() for term in members),
})


def _is_label(text: str) -> bool:
    """识别带排版符号的结构标签，避免把栏目名当作技术事实。"""
    # 仅为识别去掉排版前缀，返回的依据仍保留原文，不改写标题为正文。
    content = re.sub(r"^(?:[\s·•●▪#*>\-]+|\d+[.、]\s*)+", "", text).strip()
    return content.startswith(_LABEL_PREFIXES) or (len(content) < 12 and content.endswith((":", "：")))


def _has_technical_term(text: str) -> bool:
    """使用硬评分同源别名匹配已知技术词，不把任意英文词当作技术。"""
    # 完整英文词分界防止 Go 命中 Django；normalize_skill 直接复用 SKILL_ALIASES。
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9+#.]*", text)
    return any(normalize_skill(token.rstrip(".")) in _TECH_TERMS for token in tokens)


def _align_jd_item(item: str, lines: list[str]) -> tuple[str, float]:
    """按归一化编辑相似度对齐 JD 原行；低分也须有实质共同内容，拒绝无关行。"""
    target = normalize_text(item).casefold()
    if not target:
        return "", 0.0
    candidates: list[tuple[str, str]] = []
    for line in lines:
        # 单行粘贴的 JD 可能包含多个要求和职责；分号、句号之间的片段仍是原文切片。
        # 保留整行供比较，不拼接分句；这样场景职责不会占用 tech_gap 的整行技能依据。
        fragments = re.findall(r"[^；;。]+[；;。]?", line)
        if target in normalize_text(line).casefold() and normalize_text(line).casefold() != target:
            # 仅对原文已明确包含该职责的单行 JD 补充逗号分句，不用逗号碎片猜测改写串。
            fragments += re.findall(r"[^，,；;。]+[，,；;。]?", line)
        for original in [line, *(fragments if len(fragments) > 1 else [])]:
            if not _is_label(original) and not _BASIS_NOTE.search(original) and normalize_text(original):
                candidates.append((original, normalize_text(original).casefold()))
    # 原句匹配的上界已经是 1，不再逐行计算昂贵的编辑距离；相同分数仍取靠前的原文。
    exact = next((line for line, normalized in candidates if normalized == target), "")
    if exact:
        return exact, 1.0
    ranked: list[tuple[float, str]] = []
    for line, candidate in candidates:
        # 栏目、占位说明不是职责原文；同分保留 JD 中靠前的行，确保结果可重放。
        similarity = SequenceMatcher(None, target, candidate, autojunk=False).ratio()
        ranked.append((similarity, line))
    for similarity, line in sorted(ranked, key=lambda row: -row[0]):
        if similarity >= _ALIGNMENT_THRESHOLD:
            return line, similarity
        # 不把「负责/具备/要求」等套话的重合当作语义关联，更不取零分的第一行。
        filler = r"负责|参与|具备|熟悉|能够|要求|需要|完成|掌握|理解|相关|能力"
        content = re.sub(filler, "", target)
        original = re.sub(filler, "", normalize_text(line).casefold())
        blocks = SequenceMatcher(None, content, original, autojunk=False).get_matching_blocks()
        if any(block.size >= 2 and re.search(r"[\u3400-\u9fffA-Za-z]", content[block.a:block.a + block.size])
               for block in blocks):
            return line, similarity
    return "", 0.0


def pick_basis(pool: list[str], *, category: str) -> tuple[str, str]:
    """优先合格事实句，再在本池选最长技术句、非空句；空池返回空串及原因。"""
    # 池的来源与唯一性由构建器核验；这里不能接触其他池，更不能自行收集全局原句。
    nonempty: list[str] = []
    for candidate in pool:
        # 职责与要求可能跨行；其余槽位继续保留单行事实句约束。
        if category not in _MULTILINE_BASIS and any(
                char in candidate for char in "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"):
            continue
        text = candidate.strip()
        if not normalize_text(text) or _BASIS_NOTE.search(text):
            continue
        nonempty.append(candidate)
        if len(text) < 10 or _is_label(text):
            continue
        # 技术和职责信号是最小信息量约束，只有能查到原文还不足以支撑一道题。
        if not (_has_technical_term(text) or any(verb in text for verb in _DUTY_VERBS)):
            continue
        # 返回完整原候选；来源核验统一使用 find_original 的归一化口径。
        return candidate, ""
    technical = [text for text in nonempty if _has_technical_term(text)]
    if technical:
        return max(technical, key=len), f"{category} 使用池内最长技术原句"
    if nonempty:
        return nonempty[0], f"{category} 使用池内非空原句"
    return "", f"{category} 无可用依据"


class Slot(BaseModel):
    """保存考点、原句、难度及 N2 写题所需的附加证据。"""

    # 内部槽位没有题号，防止把尚未生成的问题提前当作最终题目。
    part: str
    category: str
    basis: str
    basis_source: Literal["jd", "resume", "profile"]
    difficulty: int
    focus: str
    # N1 独立契约允许项目定位失败标记，不套用 N0 Question 的 flag 限制。
    flag: str | None = None
    anchor: dict = Field(default_factory=dict)

    @field_validator("part")
    @classmethod
    def validate_part(cls, value: str) -> str:
        """拒绝未知分区，避免生成不属于四分区结构的槽位。"""
        # 字段先校验分区，后续 category 才有可靠的查表上下文。
        if value not in PART_ORDER:
            raise ValueError("未知面试分区")
        return value

    @field_validator("category")
    @classmethod
    def validate_category(cls, value: str, info: ValidationInfo) -> str:
        """要求考点属于当前分区，防止模型自创 code。"""
        # 错误分区已有独立错误，不在此重复触发查表异常。
        part = info.data.get("part")
        if part is not None and value not in CATEGORY_BY_PART[part]:
            raise ValueError("考点与分区不匹配")
        # 只检查类别合法性，不在单槽位模型中推断整份题册是否覆盖十个考点。
        return value

    @field_validator("basis")
    @classmethod
    def validate_basis(cls, value: str) -> str:
        """拒绝空白和纯标点依据，具体可追溯性由构建器检查。"""
        # 空文本不能借助字符串子串性质伪装成已定位的原句。
        if not normalize_text(value):
            raise ValueError("basis 不得为空")
        if _BASIS_NOTE.search(value):
            raise ValueError("basis 不得包含说明文字")
        return value

    @field_validator("difficulty")
    @classmethod
    def validate_difficulty(cls, value: int) -> int:
        """保持槽位星级在一至三档。"""
        # 后续调档也必须经过同一边界，不能产生四星题。
        if not 1 <= value <= 3:
            raise ValueError("difficulty 必须为 1~3")
        return value

    @field_validator("focus")
    @classmethod
    def validate_focus(cls, value: str) -> str:
        """限制考察方向为至多三十字的短说明。"""
        # focus 给 N2 指方向，不能直接塞入题干或整段示范作答。
        if len(value) > 30:
            raise ValueError("focus 不得超过 30 字")
        return value


class InterviewSkeleton(BaseModel):
    """承载按分区排序的内部骨架及确定性的审计信息。"""

    # 保留 source，推测画像不能在输出中被伪装成真实 JD。
    job_title: str
    source: Literal["user_jd", "inferred"]
    difficulty: Literal["junior", "standard", "senior"]
    # 不在这里生成随机种子或补齐题目，模型只承载构建器已经完成的结果。
    slots: list[Slot]
    project_seed: str
    warnings: list[str] = Field(default_factory=list)
    # 不记录运行耗时或系统时间，确保 model_dump 可逐字段重现。
    meta: dict = Field(default_factory=dict)

    @field_validator("slots")
    @classmethod
    def validate_unique_basis(cls, value: list[Slot]) -> list[Slot]:
        """拒绝整份骨架中复用的依据，标题降级也不能豁免。"""
        seen: dict[str, str] = {}
        for slot in value:
            key = normalize_text(slot.basis).casefold()
            if key in seen:
                raise ValueError(f"依据重复：{seen[key]} 与 {slot.category}")
            seen[key] = slot.category
        return value


@dataclass(frozen=True)
class ProjectSegment:
    """保存抽到的项目名称、原时间段、闭区间月份及引用原句。"""

    # 冻结段落对象，随机选择不能修改调用方传入的候选项目。
    name: str
    period: str
    start: int
    end: int
    basis: str


@dataclass(frozen=True)
class _Sentence:
    """携带原句所在节与原始行号，便于优先选经历而非技能清单。"""

    # 只保留输入切片，不通过拼接技术词制造新证据。
    text: str
    section: str
    line: int


def _period(text: str) -> tuple[str, int, int] | None:
    """从原文定位年月或年月范围，返回原拼写与闭区间月份。"""
    match = _DATE.search(text)
    if match is None:
        return None
    # 年月来源必须能在原文中定位，不能借助系统日期补出一个不存在的结束时间。
    # 单一年份按全年解释，单一月份按当月解释，不能臆测到下一项目开始。
    start = int(match["sy"]) * 12 + int(match["sm"] or 1) - 1
    if match["open"]:
        end = _OPEN_END
    elif match["ey"]:
        end = int(match["ey"]) * 12 + int(match["em"] or 12) - 1
    else:
        end = start if match["sm"] else int(match["sy"]) * 12 + 11
    # 倒置时间不能参与项目抽样，避免靠交换端点掩盖原文错误。
    return (match.group(), start, end) if end >= start else None


def _sentences(text: str) -> list[_Sentence]:
    """按原文行及中文句末切句，并识别经历、技能和个人优势节。"""
    headings = {
        "work": ("工作经历", "工作经验", "实习经历", "项目经历", "项目经验", "项目"),
        "skills": ("技能", "专业技能", "技能专长", "核心技能", "个人技能"),
        "advantage": ("个人优势", "个人评价", "自我评价", "其他"),
    }
    # 节名只影响证据优先级，返回的正文不增加任何节名或上下文说明。
    result: list[_Sentence] = []
    section = "other"
    for index, raw in enumerate(text.splitlines()):
        line = raw.strip()
        heading = re.sub(r"^[#\s一二三四五六七八九十\d、.（）()]+", "", line)
        name = re.split(r"[:：]", heading, maxsplit=1)[0].strip()
        found = next((key for key, names in headings.items() if name in names), None)
        # 标题必须完整匹配，不能把正文里偶然出现的「项目」二字当成换节。
        # 行内标题后的内容仍取输入切片，既不保留标题作证据，也不重写正文。
        if found:
            section = found
            line = re.split(r"[:：]", heading, maxsplit=1)[1] if re.search(r"[:：]", heading) else ""
        for match in _SENTENCE.finditer(line):
            sentence = match.group().strip()
            if normalize_text(sentence):
                result.append(_Sentence(sentence, section, index))
    return result


def _employment_headers(text: str) -> list[tuple[int, str, int, int]]:
    """定位带时间和公司或职位的经历头，保留行号与原文。"""
    # 结构化 ResumeInfo 没有经历数组，最近职位与核心经历只能回到原文定位。
    result = []
    for index, line in enumerate(text.splitlines()):
        period = _period(line)
        # 过滤教育和普通职责句，避免将大学、项目正文误当成最近一家公司。
        if period and re.search(r"公司|工程师|开发者|职位\s*[:：]", line):
            result.append((index, line.strip(), period[1], period[2]))
    return result


def _project_name(line: str) -> str:
    """从显式项目名或项目关键词所在原句中截取名称，不生成新名称。"""
    explicit = re.search(r"项目名称\s*[:：]\s*(.+)", line)
    # 显式名称优先，去掉时间及排版；所有文字仍来自原文。
    content = explicit[1] if explicit else line
    content = _DATE.sub("", content).strip(" \t·•-*#|｜:：()（）")
    content = re.sub(r"^\d+[.、]\s*", "", content)
    if _is_label(line):
        return ""
    # 简短的无谓词标题保留全名，不能在「平台」二字处截断「平台管理系统」。
    if not any(verb in content for verb in (*_DUTY_VERBS, "参与", "主导", "完成", "学习")) and not re.search(r"[:：，,；;。|｜]", content):
        return content if _PROJECT_WORD.search(content) and normalize_text(content) not in {
            "系统", "平台", "项目", "中台", "模块", "链路"
        } else ""
    # 去掉职责动词只是截取名称，basis 仍使用下面保留的完整原句。
    content = re.sub(r"^(?:独立|负责|参与|主导|协助|开发|搭建|设计|并|实现|完成)+", "", content)
    # 名称截至原文名词短语末尾，避免把冒号后的实现方式当作名称的一部分。
    # 只从名词短语起点截取，不能在整段职责中到处搜索出「到账链路」之类残片。
    match = re.match(r"[\w\u3400-\u9fff][\w\u3400-\u9fff \-]{1,40}(?:系统|平台|项目|Demo|中台|模块|链路)", content, re.I)
    return match.group().strip() if match else ""


def extract_projects(resume_text: str) -> list[ProjectSegment]:
    """抽同邻行日期或公司段继承日期的真实项目，并排除教育段与泛化名称。"""
    # 公司头限定继承范围，同一公司中的子系统可共享任职区间，不能继承到下一家公司。
    lines = resume_text.splitlines()
    normalized_resume = normalize_text(resume_text)
    result: list[ProjectSegment] = []
    seen: set[tuple[str, int, int]] = set()
    headers = {row[0]: row for row in _employment_headers(resume_text)}
    inherited: tuple[str, int, int] | None = None
    educational = False
    for index, line in enumerate(lines):
        # 教育节即使写了 Web 系统开发也不是独立项目经历，直到下一个有效分节再恢复。
        heading = line.strip().rstrip(":：")
        if heading in ("教育", "教育经历", "教育背景", "学习经历", "学历"):
            educational, inherited = True, None
            continue
        if heading in ("工作经历", "实习经历", "项目经历", "个人作品", "资格证书", "专业技能"):
            educational = False
            inherited = None
        if heading in ("业绩", "工作业绩", "个人业绩"):
            # 成果汇总中的泛化「第三方平台」不是新的项目，不能继续继承公司时间。
            inherited = None
            continue
        if index in headers and not educational:
            inherited = _period(line)
            # 公司行只提供时间，项目名来自跳过标签后的非空标题或它后面的具体子项目。
            continue
        if educational or not line.strip() or _is_label(line):
            continue
        if not _PROJECT_WORD.search(line):
            continue
        name = _project_name(line)
        neighbors = [index, index - 1, index + 1]
        period = next((value for pos in neighbors if 0 <= pos < len(lines)
                       if (value := _period(lines[pos])) is not None), None)
        period = period or inherited
        # 无标题教育段也检查三行上下文；出现技术词才保留进一步判断的机会。
        context = "\n".join(lines[max(0, index - 1):index + 2])
        education_only = bool(re.search(r"大学|学院|课程|学习|本科|学历", context)) and not _has_technical_term(context)
        if not name or period is None or education_only or name not in resume_text:
            continue
        key = (normalize_text(name).casefold(), period[1], period[2])
        # 同一项目同一时间的重复描述只作为一个候选，防止抽样权重被排版放大。
        if key in seen:
            continue
        # 项目槽位只允许引用该段首句，标题质量不足时也不能跨到后面的其他项目取句。
        basis = next((match.group().strip() for match in _SENTENCE.finditer(line)
                      if name in match.group()), line.strip())
        # 名称与时间是用于选择的附加信息，不允许组合成一条自称来自简历的新句子。
        # 与 find_original 同口径，循环中复用全文规范化结果；构建末尾仍逐槽硬校验。
        if normalize_text(basis) and normalize_text(basis) in normalized_resume:
            result.append(ProjectSegment(name, period[0], period[1], period[2], basis))
            seen.add(key)
    return result


def _project_priority(project: ProjectSegment) -> int:
    """开发类优先，未定类型居中，测试或实习类末选；不改项目原句与时间。"""
    text = project.name + " " + project.basis
    if re.search(r"测试|实习", text):
        return 2
    return 0 if re.search(r"开发|设计|实现|主导|搭建|上线", text) else 1


def pick_projects(projects: list[ProjectSegment], seed: str) -> list[ProjectSegment]:
    """用局部随机数生成器抽取名称不同、时间闭区间不相交的一对项目。"""
    # 对全部候选做无放回随机排列再找第一对合法组合，避免只试一次而漏掉可行对。
    shuffled = random.Random(seed).sample(list(projects), k=len(projects))
    # 项目自述要核实「这项目是不是你做的」；测试经历通常无法支撑开发设计与取舍的追问。
    # 只有测试项目时仍须出题，由「该项目为测试类」flag 说明其身份，不能把它冒充开发项目。
    shuffled.sort(key=_project_priority)
    # 先比较开发段数量，再比较测试段数量；同级保留局部随机排列的顺序。
    pairs = sorted(combinations(shuffled, 2), key=lambda pair: (
        sum(_project_priority(project) != 0 for project in pair),
        sum(_project_priority(project) == 2 for project in pair)))
    for left, right in pairs:
        # 项目名称去标点并折叠大小写，排版变化不能把同一项目伪装成两个。
        distinct = normalize_text(left.name).casefold() != normalize_text(right.name).casefold()
        # 按闭区间比较，共享同一个月份也视为时间重叠。
        disjoint = left.end < right.start or right.end < left.start
        if distinct and disjoint:
            return [left, right]
    # 不存在合法对时只返回一段，让调用方显式降级，不伪造第二个项目。
    return shuffled[:1]


def _role_family(title: str) -> str | None:
    """复用岗位族匹配，为 PRD 明确的 AI 业务落地专家补接 FDE 别称。"""
    # 主规则始终调用旧的岗位族函数，避免 N1 私自维护另一套完整岗位分类。
    family = match_family(title)[0]
    # 旧表没有中文「业务落地」label；仅承接本 PRD 已明确的 FDE 别称。
    if family is None and re.search(r"AI\s*业务落地|AI\s*落地", title, re.I):
        family = match_family("FDE")[0]
    return family


def is_career_switch(resume_text: str, resume: ResumeInfo, job: JobInfo,
                     job_title: str) -> tuple[bool, str]:
    """依次执行意向、最近职位、技能族三条跨行规则，返回判断与理由。"""
    target = _role_family(job_title)
    # 未知目标族没有可靠比较基准，不能把它与所有已知经历都判成不同族。
    if target is None:
        return False, "未能判定"
    intent = _INTENT.search(resume_text)
    intents = [] if intent is None else [
        family for part in re.split(r"[/／|｜、]", intent[1]) if (family := _role_family(part))
    ]
    # 多意向简历只要包含目标族，就不能因另一个意向触发第一条跨行规则。
    if intents and target not in intents:
        return True, f"求职意向岗位族 {','.join(dict.fromkeys(intents))} 与目标族 {target} 不同"
    headers = _employment_headers(resume_text)
    latest = max(headers, key=lambda row: (row[3], row[2], row[0]), default=None)
    role_line = latest[1] if latest else next(
        (line for line in resume_text.splitlines() if re.match(r"\s*职位\s*[:：]", line)), ""
    )
    # 只比较职位字段，避免公司名称中的「数据」「产品」误导岗位族。
    # 日期可能在最后一列；删除日期后应跳过空列，而非丢掉前面的真实职位。
    role = next((part.strip() for part in reversed(re.split(r"[|｜]|\s{2,}", _DATE.sub("", role_line)))
                 if part.strip()), "")
    recent_family = _role_family(role)
    if recent_family and recent_family != target:
        return True, f"最近职位岗位族 {recent_family} 与目标族 {target} 不同"
    # 第三条只比较技能族，不把同岗位族误当成技能已覆盖。
    resume_families = {skill_family(normalize_skill(skill)) or normalize_skill(skill) for skill in resume.skills}
    job_families = {skill_family(normalize_skill(skill)) or normalize_skill(skill) for skill in job.must_have}
    # 无族技术各自保持独立，空技能集不能被当作已证明「完全不重叠」。
    labels = PROFILE_TEMPLATES[target]["labels"]
    target_in_text = any(label.casefold() in resume_text.casefold() for label in labels)
    if target == "delivery":
        target_in_text = target_in_text or bool(re.search(r"AI\s*(?:业务)?落地", resume_text, re.I))
    if resume_families and job_families and resume_families.isdisjoint(job_families) and not target_in_text:
        return True, f"简历技能族与 JD 必备技能族无交集，正文未出现目标族 {target}"
    # 有职位信息但三条规则均未命中，与完全缺少可判断信息分开返回理由。
    if intents or recent_family:
        return False, f"岗位族与目标 {target} 相符，未命中跨行规则"
    return False, "未能判定"


def _skill_count(text: str, skill: str) -> int:
    """按既有别名统计完整技术词，避免 Go 因 Django 的子串而虚增次数。"""
    # 复用硬评分的别名口径，不能让骨架把 Go 与 Golang 算成两项能力。
    normalized = normalize_skill(skill)
    variants = {normalized, *(alias for alias in SKILL_ALIASES if normalize_skill(alias) == normalized)}
    # 按长别名优先匹配，Go语言不能同时再算一个 Go。
    pattern = "|".join(re.escape(alias) for alias in sorted(variants, key=lambda item: (-len(item), item)))
    return len(re.findall(rf"(?<![A-Za-z0-9_])(?:{pattern})(?![A-Za-z0-9_])", text, re.I))


def _low_dimensions(llm_score: int | LlmScore | None, warnings: list[str]) -> set[str]:
    """读取已提供的分项，严格低于满分六成时标记对应维度。"""
    if llm_score is None:
        return set()
    # 用户澄清：整数不能推断三个维度；记录原因而不把总分强摊成子分。
    if isinstance(llm_score, int):
        warnings.append("llm_score 仅提供整数，无法按子项调整难度")
        return set()
    if getattr(llm_score, "status", "ok") == "failed":
        warnings.append("LLM 分项状态 failed，未调整难度")
        return set()
    low: set[str] = set()
    # 只读现有评分产物；不会为缺失的分项发起补充评分或调用模型。
    for item in llm_score.items:
        maximum = LLM_DIMENSIONS.get(item.key)
        # 上限取固定 rubric，不相信外部传来的 max_score。
        if maximum is not None and 0 <= item.score <= maximum and item.score * 5 < maximum * 3:
            low.add(item.key)
    return low


def _take_basis(candidates: list[tuple[str, str]], *, normalized_sources: dict[str, str],
                used: set[str], reserved: dict[str, str], titles: list[tuple[str, str]],
                category: str, warnings: list[str], required_origin: str | None = None,
                resume_fallback: list[tuple[str, str]] | None = None,
                generic_context: list[tuple[str, str]] | None = None) -> tuple[str, str, bool]:
    """只消费本池原句；用尽时核验岗位标题，无 JD 的 HR 可降级到简历原句。"""
    def available(pool: list[tuple[str, str]]) -> list[str]:
        # 声明来源约束语义池；不能用简历里的同文替 JD 职责补票。
        result = []
        for text, origin in pool:
            normalized = normalize_text(text)
            key = normalized.casefold()
            if (normalized and (required_origin is None or origin == required_origin)
                    and key not in used and reserved.get(key, category) == category
                    and normalized in normalized_sources[origin]):
                result.append(text)
        return result

    basis, reason = pick_basis(available(candidates), category=category)
    title_fallback = not basis
    if title_fallback and category == "tech_gap":
        origin = "岗位画像" if candidates and candidates[0][1] == "profile" else "JD"
        raise ValueError(f"tech_gap 的{origin}技能池被过滤空（来源校验、去重或质量过滤后无可用依据）")
    if title_fallback and required_origin == "profile":
        raise ValueError(f"{category} 的岗位画像条目池被过滤空，不能借用其他来源")
    if title_fallback:
        # 标题不能来自结构化字段的自称；必须在传入原文中实查，且同样全局去重。
        basis, _ = pick_basis(available(titles), category=category)
        if not basis and resume_fallback:
            # 只有岗位名时，简历未必写出该岗位名或薪资；仍只引用未用的真实简历句。
            basis, _ = pick_basis(available(resume_fallback), category=category)
            reason = f"{category}：{_MANUAL_BASIS}，使用简历原句"
        else:
            reason = f"{category}：{_MANUAL_BASIS}，使用原文岗位标题"
        if not basis and generic_context:
            # 无相关原行时只以整份 JD 作通用题的背景，不拿另一条无关职责冒充对齐。
            basis, _ = pick_basis(available(generic_context), category=category)
            reason = f"{category}：{_MANUAL_BASIS}，使用 JD 全文作为通用场景背景"
        if not basis:
            raise ValueError(f"{category} 无可追溯且不重复的岗位标题依据")
    if reason:
        warnings.append(reason)
    # 标题降级也要参与独占，否则稀疏输入仍会退回多道题追问同一个事实。
    used.add(normalize_text(basis).casefold())
    return basis, reason, title_fallback


def build_skeleton(*, resume_text: str, resume: ResumeInfo, job: JobInfo,
                   profile: JobProfile | None, diagnosis: DiagnosisReport,
                   hard: HardScore, sem: SemanticScore, llm_score: int | LlmScore | None = None,
                   job_title: str, difficulty: str = "standard",
                   plan_id: str | None = None, seed: str | None = None,
                   jd_text: str | None = None) -> InterviewSkeleton:
    """从传入证据构建确定性骨架，不读文件、不调用模型、不修改输入对象。

    jd_text 非空时使用 JD 要求；否则只使用传入画像的要求条目。profile.source 仅作结果标注。
    """
    # 证据家族只由 JD 原文是否有内容决定，不能交给画像的来源标记。
    has_jd = bool(jd_text and jd_text.strip())
    source = profile.source if profile is not None else ("user_jd" if has_jd else "inferred")
    if source not in ("user_jd", "inferred"):
        raise ValueError("profile.source 必须为 user_jd 或 inferred")
    if not has_jd and profile is None:
        raise ValueError("未传 jd_text 时必须传 profile，tech_gap / scenario_* 需要画像条目依据")
    levels = {"junior": 1, "standard": 2, "senior": 3}
    if difficulty not in levels:
        raise ValueError("difficulty 必须为 junior、standard 或 senior")
    # 所有中间状态属于本次调用，不能缓存并污染后续用户的输入或随机结果。
    warnings: list[str] = []
    if seed is None:
        # 按任务书对 hex 字符串本身求哈希，不能先解码成字节导致种子不一致。
        seed = sha256(plan_id.encode("utf-8")).hexdigest()[:16] if plan_id is not None else "0" * 16
        if plan_id is None:
            warnings.append("未提供 seed，使用仅供测试的全零种子")
    # 真实 JD 只取 jd_text；推测画像单独保存出处，不能拼成一份虚构 JD。
    evidence_jd = jd_text if has_jd else ""
    profile_rows = [*profile.must_have, *profile.responsibilities] if not has_jd else []
    # 仅整份画像都是通用项时降级；混合画像及真实 JD 继续沿用严格取证。
    # 「只有岗位名」是 PRD §5.0 四组合之一，不能因画像为通用兜底就让这个合法入口失效。
    # 因此这里只能降级出题并加 flag；直接抛错会让没有真实 JD 的用户无法继续。
    # 低置信是证据质量提示，不是输入非法；保留提示才能让用户补 JD 后复核。
    generic_profile = bool(profile_rows) and all(normalize_text(row) in _GENERIC_PROFILE_ITEMS for row in profile_rows)
    if generic_profile:
        warnings.append("画像低置信，题目针对性弱")
    evidence_profile = "\n".join(profile_rows)
    # 不能把简历和 JD 拼成一个字符串来找原句，否则跨来源的假句也可能命中。
    normalized_sources = {"resume": normalize_text(resume_text), "jd": normalize_text(evidence_jd),
                          "profile": "" if has_jd else normalize_text(evidence_profile)}

    def basis_source(basis: str, category: str) -> str:
        """先核实证据家族，再标注实际来源，不受 profile.source 影响。"""
        in_profile = any(find_original(row, basis) for row in profile_rows)
        if category in _REQUIREMENT_CATEGORIES:
            if has_jd and find_original(evidence_jd, basis):
                return "jd"
            if not has_jd and in_profile:
                return "profile"
        if normalize_text(basis) in normalized_sources["resume"]:
            return "resume"
        if normalize_text(basis) in normalized_sources["jd"]:
            return "jd"
        return "profile" if not has_jd and in_profile else "jd"

    # 标题逐字取原文；单行 JD 的招聘标题截至第一个逗号，不能把整段要求冒充标题。
    jd_lines = [line for line in evidence_jd.splitlines() if line.strip()]
    title_line = next((line for line in jd_lines if re.search(r"岗位名称|职位名称|招聘", line)), "")
    if not title_line and jd_lines and re.search(r"工程师|开发者|专家|经理|总监|设计师", jd_lines[0]):
        title_line = jd_lines[0]
    title_line = re.split(r"[，,；;。]", title_line, maxsplit=1)[0]
    titles = [(job_title, origin) for origin in ("resume", "jd")]
    titles += [(title_line, "jd")] if title_line else []
    sentences = _sentences(resume_text)
    jd_sentences = _sentences(evidence_jd)
    # 保留跨行要求原串作为补充候选，不把折行误当作语义句末。
    jd_requirements = [match.group().strip() for match in re.finditer(
        r"[^。；;！？!?]+[。；;！？!?]?", evidence_jd) if normalize_text(match.group())]
    year_pattern = r"\d+(?:[-~～至到]\d+)?\s*年"
    year_rows = [(item.jd_evidence, "jd") for item in hard.deductions
                 if item.type in ("years_short", "years_unknown")]
    year_rows += [(row.text, "jd") for row in jd_sentences if re.search(year_pattern, row.text)]
    if not any(find_original(evidence_jd, row) for row, _ in year_rows):
        year_rows += [(row, "jd") for row in jd_requirements if re.search(year_pattern, normalize_text(row))]
    # 经历中的时间头只用于定位，不当作实现细节或技术句。
    experience = [sentence for sentence in sentences if sentence.section == "work" and not _DATE.search(sentence.text)]
    headers = _employment_headers(resume_text)
    # 经历常按时间倒排，核心经历按开始时间确定，不能直接把第一行当作最早一段。
    oldest = min(headers, key=lambda row: (row[2], row[0]), default=None)
    core = [] if oldest is None else [sentence for sentence in experience if sentence.line > oldest[0]
        and not any(oldest[0] < row[0] <= sentence.line for row in headers)]
    ordered = [*experience, *(sentence for sentence in sentences if sentence.section == "skills"),
               *(sentence for sentence in sentences if sentence.section not in ("work", "skills"))]
    # 只共享已用原句集合，不共享候选池；每个 add 调用必须给出自己的语义池。
    used: set[str] = set()
    # 单句池在生成前预留，避免前序通用槽位耗尽项目和未覆盖职责的唯一证据。
    projects = extract_projects(resume_text)
    picked = pick_projects(projects, seed)
    uncovered = next((item for item in sem.per_item if not item.covered and (
        has_jd or any(find_original(row, item.item) for row in profile.responsibilities))), None)
    duty = uncovered.item if uncovered else None
    duty_basis, duty_similarity = _align_jd_item(duty or "", jd_lines) if has_jd else (duty, 1.0)
    duty_source = "jd" if has_jd else "profile"
    if not has_jd:
        # 画像分支取条目本身，不从模板或其他文本拼接一句替代职责。
        duties = [row for row in profile.responsibilities if normalize_text(row) not in _GENERIC_PROFILE_ITEMS]
        duty = (next(iter(profile.must_have), profile_rows[0]) if generic_profile else
                next((row for row in duties if uncovered and find_original(row, uncovered.item)), next(iter(duties), None)))
        if duty is None:
            raise ValueError("scenario_design 的岗位画像没有具体职责条目，不能使用通用模板句")
        duty_basis = duty
    reserved = {normalize_text(project.basis).casefold(): category
                for project, category in zip(picked, CATEGORY_BY_PART["project"])}
    if duty_basis:
        reserved[normalize_text(duty_basis).casefold()] = "scenario_design"
    # 基础难度与低分维度只计算一次，保证同类槽位使用相同的调档依据。
    low = _low_dimensions(llm_score, warnings)
    slots: list[Slot] = []

    def add(part: str, category: str, candidates: list[tuple[str, str]], focus: str,
            *, flag: str | None = None, anchor: dict | None = None) -> Slot | None:
        """统一执行本池核验、全局去重、分维调档及槽位创建。"""
        # HR 考点不是逐条发现清单；统一入口再防一次重复，避免补足或新增分支绕过合并。
        if part == "hr" and any(slot.part == "hr" and slot.category == category for slot in slots):
            return
        # 每次复制附加证据，不把输出对象的字典引用暴露给输入对象。
        basis, reason, title_fallback = _take_basis(candidates, normalized_sources=normalized_sources, used=used,
            reserved=reserved, titles=titles, category=category, warnings=warnings,
            required_origin=("jd" if has_jd else "profile") if category in _REQUIREMENT_CATEGORIES else None,
            resume_fallback=[(row.text, "resume") for row in ordered]
                if not has_jd and part == "hr" else None,
            generic_context=[(evidence_jd.strip(), "jd")] if has_jd and part == "scenario" else None)
        # 此处与末尾都按实查反推来源，画像证据不能伪装成真实 JD。
        origin = basis_source(basis, category)
        details = dict(anchor or {})
        if reason:
            details["basis_fallback"] = reason
        if title_fallback:
            if flag:
                details["original_flag"] = flag
            flag = _MANUAL_BASIS
            if has_jd and part == "scenario":
                # 通用题保留缺失的上游条目，aligned 留空，不能把背景标题伪装成职责对齐。
                details.update(generic=True, aligned="", similarity=0.0)
        if origin == "profile":
            if flag:
                details["original_flag"] = flag
            flag = _GENERIC_PROFILE_BASIS if generic_profile and category == "scenario_design" else _PROFILE_BASIS
        dimension = {"tech": "stack_depth", "scenario": "domain_fit", "project": "proof_strength"}.get(part)
        # HR 不属于技术、场景、成果三维，不能因为技术短板被顺带提高难度。
        level = min(3, levels[difficulty] + int(dimension in low))
        slot = Slot(part=part, category=category, basis=basis, basis_source=origin,
                    difficulty=level, focus=focus, flag=flag, anchor=details)
        slots.append(slot)
        return slot

    def matching(skill: str, rows: list[_Sentence]) -> list[tuple[str, str]]:
        """按输入顺序找含同一规范技能的原句。"""
        # 归一化仅用于判断命中，basis 保留输入中的原拼写。
        return [(row.text, "resume") for row in rows if _skill_count(row.text, skill)]


    def add_resume_slot(category: str, candidates: list[tuple[str, str]], focus: str,
                        *, anchor: dict | None = None) -> None:
        """先筛本池未占用的类型句；类型句耗尽才在同池降级，统一入口负责最终独占。"""
        def sentence_rank(text: str) -> int | None:
            """返回本槽位内的类型优先级；无类型匹配返回 None，不扩大候选池。"""
            if category == "impl_detail":
                if re.search(r"独立设计并实现|分层架构|异步队列开发|全流程开发", text):
                    return 0
                return 1 if re.search(r"使用.+(?:实现|完成|开发|部署|构建)|实现|重构|接入|搭建|部署", text) else None
            if category == "growth":
                # 学时、学历、大学不是个人成长动作，不能因一个「学」字抢走业务或教育句。
                content = re.sub(r"学时|续学|学历|大学|学院|学位|学校|学生|学科", "", text)
                if re.search(r"最近|落地|应用|上手|尝试", content):
                    return 0
                return 0 if "学" in content and not re.search(r"课程|教育|本科", text) else None
            if category == "debug":
                return 0 if re.search(r"排查|故障|超时|监控|线上|报警|定位|慢|异步回调|定时任务|分库|重试|幂等|压测", text) else None
            # 最后两个槽位消费剩余技能与选型依据；不能反过来抢先占用实现、成长、排障句。
            return 0 if _has_technical_term(text) else None

        # 第一版没有独占，四个槽位曾共用「主导企业端员工与薪资链路模块…」，最终四道题都在问同一件事。
        # 必须先排除已占用句，才能让不同考点获得不同事实，而不是把同一句换个问法重复考。
        available: list[tuple[str, str]] = []
        seen: set[str] = set()
        for text, origin in candidates:
            key = normalize_text(text).casefold()
            if (key and key not in seen and key not in used and reserved.get(key, category) == category
                    and normalize_text(text) in normalized_sources[origin] and not _BASIS_NOTE.search(text)):
                available.append((text, origin))
                seen.add(key)
        typed = [(rank, pair) for pair in available if (rank := sentence_rank(pair[0])) is not None]
        # 稳定排序只调整本池优先级，不复制原句；已占用的类型句不会阻止本槽位声明降级。
        pool = [pair for _, pair in sorted(typed, key=lambda row: row[0])] if typed else available
        details = dict(anchor or {})
        if not typed:
            details["degraded"] = "no_typed_candidate"
        add("tech", category, pool, focus, anchor=details)

    # 四个必含 HR 先占位；条件项稍后按固定优先级追加，最后统一裁至八条。
    intent_line = next((line.strip() for line in resume_text.splitlines() if _INTENT.search(line)), "")
    latest = max(headers, key=lambda row: (row[3], row[2], row[0]), default=None)
    # 最长原行可能是被禁止的技术栈标签，按长度提供候选，让同一入口继续选下一条信息句。
    longest_candidates = [(row.text, "resume") for row in sorted(experience, key=lambda row: -len(row.text))]
    # 一行包含薪资、意向和年限时分别引用原字段，避免三个 HR 考点共用整行。
    salary = [(match.group(), "resume") for line in resume_text.splitlines()
              if (match := re.search(r"期望薪资\s*[:：][^|｜\r\n]+", line))]
    salary += [(row.text, "jd") for row in jd_sentences if re.search(r"薪资|薪酬|月薪|年薪", row.text)]
    # 四条必含不依赖条件判断，证据缺失也只降级依据，不能直接删除槽位。
    add("hr", "motivation", [(latest[1], "resume")] if latest else [], "离职或转岗动机")
    add("hr", "salary", salary, "薪酬预期与岗位条件")
    intent_field = re.search(r"(?:求职意向|期望岗位|意向岗位)\s*[:：][^|｜\r\n]+", intent_line)
    add("hr", "plan", [(intent_field.group(), "resume")] if intent_field else [], "职业规划与岗位选择")
    add("hr", "collaboration", longest_candidates, "团队协作与冲突处理")
    switched, switch_reason = is_career_switch(resume_text, resume, job, job_title)
    if switched:
        # 结果和理由一起传给 N2，避免后续只能看到一个无法解释的布尔值。
        add("hr", "career_switch", [(intent_line, "resume")] if intent_line else [], "跨行选择的真实动机",
            anchor={"career_switch": True, "career_switch_reason": switch_reason})
    elif switch_reason == "未能判定":
        warnings.append("未能判定是否跨行")
    # 明确区分 None 与零年要求，不能把未知年限当成不足。
    if job.required_years_min is not None and resume.years is not None and resume.years < job.required_years_min:
        add("hr", "years_gap", year_rows, "年限不足的能力补偿证据")
    timelines = [finding for finding in diagnosis.findings if finding.code in ("open_ended", "timeline_overlap")]
    if timelines:
        # 上游 overlap 的 evidence 可能包含换行，拆回原始时间行并保留全部涉及的段落。
        evidence = list(dict.fromkeys(line.strip() for finding in timelines for line in finding.evidence.splitlines()
                                      if normalize_text(line) and normalize_text(line) in normalized_sources["resume"]))
        # 整行被动机题使用时仍可引用该发现中的原时间区间，不另找无关经历补句。
        periods = [period[0] for line in evidence if (period := _period(line))]
        add("hr", "timeline", [(line, "resume") for line in [*evidence, *periods]], "时间安排与经历事实澄清",
            anchor={"evidence": evidence})
    # 先用中性 other 补足，不把补位题冒充已经触发的跨行或年限异常。
    unused = [code for code in ("other", *CATEGORY_BY_PART["hr"]) if not any(slot.category == code for slot in slots)]
    for category in dict.fromkeys(unused):
        if len(slots) >= HR_MIN:
            break
        add("hr", category, longest_candidates, "岗位适应与入职准备")
    if len(slots) > HR_MAX:
        warnings.append("HR 超过 8 条，按必含4→career_switch→years_gap→timeline截断")
        slots = slots[:HR_MAX]

    # 实现细节句最稀缺，一份简历通常只有 2~3 句，必须先给 impl_detail，不能被 growth / debug 抢走。
    # 「最近学了并落地」往往同时也是好的实现细节句；若 growth 先取，就会耗掉实现槽位的稀缺依据。
    # 因而要先分配证据、后重排展示；按展示顺序取句会再次让深度题抢走实现素材。
    # 分配顺序与展示顺序分开：先实现，再成长，再排障，最后深度与取舍。
    # 各池沿用既有简历边界，已被项目/场景预留的句子同样不能抢占。
    resume_pool = [(row.text, "resume") for row in ordered]
    vocabulary = list(dict.fromkeys(normalize_skill(value) for value in [*resume.skills, *job.must_have, *job.nice_to_have]))
    technical = [(row, sum(bool(_skill_count(row.text, term)) for term in vocabulary)) for row in experience]
    implementations = [row for row, count in sorted(technical, key=lambda pair: -pair[1]) if count]
    add_resume_slot("impl_detail", [(row.text, "resume") for row in implementations] + resume_pool, "具体实现中的关键技术细节")
    nice = job.nice_to_have[0] if job.nice_to_have else ""
    growth_rows = [*experience, *(row for row in sentences if row.section == "advantage"),
                   *(row for row in sentences if row.section not in ("work", "advantage"))]
    growth = matching(nice, growth_rows) if nice else []
    if not growth:
        growth = [(row.text, "resume") for row in sentences if row.section == "advantage"][:1]
    add_resume_slot("growth", growth + resume_pool, "新知识的验证与实际落地", anchor={"skill": nice})
    debug_rows = [row for row in (core or experience) if any(_skill_count(row.text, term) for term in vocabulary)]
    add_resume_slot("debug", [(row.text, "resume") for row in debug_rows] + resume_pool, "生产排障视角下的证据与定位")

    # 技术深度按出现次数选核心交集，平局保留 JD 的既有顺序。
    intersections = [skill for skill in job.must_have if any(normalize_skill(skill) == normalize_skill(other) for other in resume.skills)]
    skill = max(intersections, key=lambda item: _skill_count(resume_text, item), default="")
    # 五个简历技术槽位共用简历句边界，原规则只决定优先级，不因首选已用就丢掉整池。
    add_resume_slot("tech_depth", (matching(skill, ordered) if skill else []) + resume_pool,
        "核心必备技能的理解深度", anchor={"skill": skill})
    missing = next((item for item in hard.deductions if item.type == "missing_skill"), None)
    # 缺口以首条硬评分记录为准，不能为了写题再自行判定另一个「缺失技能」。
    if missing is None and not generic_profile:
        raise ValueError("tech_gap 必须有 hard.deductions 的 missing_skill，不能自行编造缺口技能")
    gap = re.sub(r"^必备技能\s*[:：]\s*", "", missing.item.strip()).strip() if missing else ""
    if not gap and not generic_profile:
        raise ValueError("tech_gap 的 missing_skill 缺少技能名")
    if normalize_text(gap) in _GENERIC_PROFILE_ITEMS and not generic_profile:
        raise ValueError("tech_gap 的 missing_skill 必须是具体技能，不能使用通用岗位句")
    # 缺口池逐行来自 JD，保留全部命中行供审计；不混入简历句或只有技能名的结构化字段。
    gap_source = "jd" if has_jd else "profile"
    gap_lines = [line for line in (evidence_jd.splitlines() if has_jd else profile.must_have)
                 if gap and _skill_count(line, gap)]
    generic_gap = generic_profile and (not gap_lines or normalize_text(gap) in _GENERIC_PROFILE_ITEMS)
    if generic_gap:
        # 通用画像无法证明具体技能缺口；保留槽位，明确按基础能力出题，不伪造技能证据。
        gap_lines = list(profile.must_have) or list(profile_rows)
        warnings.append("tech_gap：通用画像缺少具体技能依据，按岗位基础能力出题")
    elif not gap_lines:
        if has_jd:
            raise ValueError(f"tech_gap 的 JD 中确实没有该技能原句：{gap}")
        raise ValueError(f"tech_gap 的岗位画像 must_have 中确实没有该缺口技能条目：{gap}")
    gap_candidates = [(line, gap_source) for line in gap_lines]
    add("tech", TECH_CATEGORY_ORDER[1], gap_candidates, "缺口技能的迁移与补齐思路",
        flag="简历未提及，考察迁移能力",
        anchor={"skill": "" if generic_gap else gap, "jd_lines" if has_jd else "profile_items": list(gap_lines),
                "missing_skill": not generic_gap, **({"profile_fallback": True} if generic_gap else {})})
    choices = [(value, sorted(SKILL_FAMILIES[family] - {normalize_skill(value)}))
               for value in resume.skills if (family := skill_family(normalize_skill(value))) in SKILL_FAMILIES
               and normalize_skill(value) in SKILL_FAMILIES[family]]
    selected, alternatives = choices[0] if choices else ("", [])
    # 同族替代项先排序，固定候选顺序，避免不同 PYTHONHASHSEED 改变出题方向。
    add_resume_slot("tradeoff", (matching(selected, ordered) if selected else []) + resume_pool, "同族技术方案的选择与取舍",
        anchor={"skill": selected, "alternative": alternatives[0] if alternatives else None})

    # 上游职责只用于定位；引用内容必须是 JD 原行，画像分支仍逐条核验其自身来源。
    if not has_jd and duty is not None and not any(
            find_original(row, duty) for row in (profile_rows if generic_profile else profile.responsibilities)):
        raise ValueError("scenario_design 的未覆盖职责不在岗位画像原文里")
    add("scenario", "scenario_design", [(duty_basis, duty_source)] if duty_basis else [], "业务需求拆解与最小交付方案",
        flag=_APPROXIMATE_BASIS if has_jd and duty_basis and duty_similarity < _ALIGNMENT_THRESHOLD else None,
        anchor={"responsibility": duty, **({"cos": uncovered.best_sim} if uncovered and not generic_profile else {}),
                **({"item": duty or "", "aligned": duty_basis or "", "similarity": duty_similarity} if has_jd else {})})
    risk = next((item for item in hard.deductions if item.type in ("years_short", "years_unknown", "education", "education_unknown")), None)
    # 风险池有明确的两级语义边界：扣分对应的年限/学历要求，随后才是缺失技能的 JD 行。
    risk_candidates: list[tuple[str, str]] = []
    if risk is not None:
        risk_candidates.append((risk.jd_evidence, "jd"))
    # 硬评分的年限证据可能是「结构化字段」说明，需回到 JD 年限原句取证。
    if risk and risk.type in ("years_short", "years_unknown"):
        risk_candidates += year_rows
    elif risk and risk.type in ("education", "education_unknown"):
        risk_candidates += [(row.text, "jd") for row in jd_sentences if re.search(r"学历|本科|硕士|大专|博士", row.text)]
        if not any(find_original(evidence_jd, row) for row, _ in risk_candidates):
            risk_candidates += [(row, "jd") for row in jd_requirements if re.search(r"学历|本科|硕士|大专|博士", normalize_text(row))]
    if not has_jd:
        risk_candidates = [(item, "profile") for item in profile_rows
                           if generic_profile or normalize_text(item) not in _GENERIC_PROFILE_ITEMS]
    risk_alignments: dict[str, dict] = {}
    if has_jd:
        aligned_risks = []
        for item, _ in risk_candidates:
            aligned, similarity = _align_jd_item(item, jd_lines)
            if aligned:
                aligned_risks.append((aligned, "jd"))
                risk_alignments.setdefault(aligned, {"item": item, "aligned": aligned, "similarity": similarity})
        risk_candidates = aligned_risks
    # 这些仍是风险槽位允许的 JD 池内候选；不向简历池或其他职责池寻找替代品。
    primary_risk = {line for line, _ in risk_candidates}
    usable_risk = [(line, origin) for line, origin in risk_candidates
                   if normalize_text(line) and normalize_text(line) in normalized_sources[origin]
                   and normalize_text(line).casefold() not in used
                   and reserved.get(normalize_text(line).casefold(), "scenario_risk") == "scenario_risk"
                   and not _BASIS_NOTE.search(line)]
    # 主池内即使只有很短的学历要求，也先在主池降级；用尽后才进入缺口 JD 池。
    risk_candidates = usable_risk or [(line, gap_source) for line in gap_lines]
    risk_slot = add("scenario", "scenario_risk", risk_candidates, "客户现场能力缺口与风险沟通",
        anchor={"risk_type": risk.type if risk else (missing.type if missing else None),
                **({"item": risk.jd_evidence if risk else gap, "aligned": "", "similarity": 0.0} if has_jd else {})})
    if has_jd and risk_slot is not None and not risk_slot.anchor.get("generic"):
        # 缺口降级池原本就是 JD 原行，亦记录其来源，不能漏掉风险槽位的对齐审计信息。
        alignment = risk_alignments.get(risk_slot.basis)
        if alignment is None:
            aligned, similarity = _align_jd_item(risk_slot.basis, jd_lines)
            alignment = {"item": risk_slot.basis, "aligned": aligned, "similarity": similarity}
        risk_slot.anchor.update(alignment)
        if alignment["similarity"] < _ALIGNMENT_THRESHOLD:
            risk_slot.flag = _APPROXIMATE_BASIS
    if risk_slot is not None and any(find_original(line, risk_slot.basis) for line in gap_lines) and not any(
            find_original(line, risk_slot.basis) for line in primary_risk):
        # 年限或学历池耗尽后选了缺口技能，审计记录也必须反映实际选中的风险来源。
        risk_slot.anchor["risk_type"] = "missing_skill"
    if risk_slot is not None:
        # 记录最终依据的身份；JD 没有硬门槛时，不把技能要求伪装成门槛风险。
        hard_gate = (has_jd and risk is not None and risk_slot.anchor.get("risk_type") != "missing_skill"
                     and not risk_slot.anchor.get("generic") and bool(re.search(
            year_pattern + r"|学历|本科|硕士|大专|博士|学位|硬性|资格|证书", normalize_text(risk_slot.basis))))
        risk_slot.anchor["risk_priority"] = "hard_gate" if hard_gate else "missing_skill"

    # 随机只决定选哪段，不产生项目名、时间或项目成果。
    for index, category in enumerate(CATEGORY_BY_PART["project"]):
        # 两个固定 code 只表示抽中顺序，不暗示哪一段项目更重要或更量化。
        if index < len(picked):
            project = picked[index]
            add("project", category, [(project.basis, "resume")], "真实项目中的个人职责与结果",
                flag="该项目为测试类" if _project_priority(project) == 2 else None,
                anchor={"project": project.name, "period": project.period})
        else:
            flag = "未定位到第二段项目" if picked else "未定位到项目段"
            warnings.append(f"{category}：{flag}")
            # 没有这一段项目就没有该段首句，不能从另一段经历借来一句充当项目依据。
            add("project", category, [], "真实经历中的个人职责与结果", flag=flag)
    # 先占先得只改变分配次序，输出仍遵守四分区及既有技术考点顺序。
    slots.sort(key=lambda slot: (PART_ORDER.index(slot.part),
                                TECH_CATEGORY_ORDER.index(slot.category) if slot.part == "tech" else 0))
    # 最终硬校验独立于所有选句分支；即使选句器回归，也绝不能返回悬空骨架。
    for slot in slots:
        in_resume = find_original(resume_text, slot.basis)
        in_jd = find_original(evidence_jd, slot.basis)
        in_profile = any(find_original(row, slot.basis) for row in profile_rows)
        if not (in_resume or in_jd or in_profile):
            raise ValueError(f"{slot.category} 的依据不在原文里：{slot.basis[:30]}")
        if slot.category in _REQUIREMENT_CATEGORIES:
            if has_jd and not in_jd:
                raise ValueError(f"{slot.category} 的依据必须来自 JD 家族")
            if not has_jd and not in_profile:
                raise ValueError(f"{slot.category} 的依据必须来自岗位画像条目")
            if not has_jd and not generic_profile and normalize_text(slot.basis) in _GENERIC_PROFILE_ITEMS:
                raise ValueError(f"{slot.category} 的画像依据不能使用通用模板句")
        slot.basis_source = basis_source(slot.basis, slot.category)
        if slot.basis_source == "profile":
            slot.flag = _GENERIC_PROFILE_BASIS if generic_profile and slot.category == "scenario_design" else _PROFILE_BASIS
        if slot.category == "tech_gap" and not any(find_original(line, slot.basis) for line in gap_lines):
            raise ValueError(f"tech_gap 的依据必须来自含该技能的{'JD 行' if has_jd else '岗位画像要求'}")
        if slot.category == "tech_gap" and not generic_gap and not _skill_count(slot.basis, gap):
            raise ValueError(f"tech_gap 的依据必须包含扣分项的缺口技能：{gap}")
        if slot.category == "scenario_design" and not has_jd and duty is not None and not find_original(duty, slot.basis):
            raise ValueError("scenario_design 的依据必须可追溯到未覆盖职责原文")
    # 元数据只记录确定性统计；实际耗时由检查脚本测量而不污染骨架比较。
    return InterviewSkeleton(job_title=job_title, source=source, difficulty=difficulty, slots=slots,
        project_seed=seed, warnings=list(dict.fromkeys(warnings)), meta={
            "part_counts": dict(Counter(slot.part for slot in slots)),
            "basis_sources": {origin: sum(slot.basis_source == origin for slot in slots)
                              for origin in ("resume", "jd", "profile")},
            "fallback_count": sum("basis_fallback" in slot.anchor for slot in slots),
            "missing_categories": [slot.category for slot in slots if slot.flag == _MANUAL_BASIS],
            "status": "needs_review" if any(slot.flag == _MANUAL_BASIS for slot in slots) else "ok",
            "project_candidates": len(projects), "project_selected": len(picked),
            "career_switch": switched, "career_switch_reason": switch_reason,
            "low_dimensions": [key for key in LLM_DIMENSIONS if key in low],
            "jd_evidence_scope": "jd_text" if has_jd else "profile",
            "model_calls": 0, "cost_yuan": 0,
        })
