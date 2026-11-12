"""M1 简历缺陷诊断：仅检查输入原文，不生成意见，也不调用模型。

证据统一由 M0 护栏检索；岗位技能口径复用评分模块，不另建技术或方向词表。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from time import perf_counter
from typing import TYPE_CHECKING

from app.guard_revision import TECH_TOKEN, find_original
from app.schemas_match import JobInfo
from app.schemas_revision import DiagnosisFinding, DiagnosisReport, KwCoverage
from app.scoring_rules import (
    EDU_LEVEL, ROLE_WORDS, SKILL_ALIASES, SKILL_FAMILIES, group_required_skills, normalize_skill,
)

# 现有 schemas 模块在导入时读环境并创建客户端；运行时只读取输入对象属性。
if TYPE_CHECKING:
    from app.schemas import ResumeInfo


# 顺序逐项对应 PRD §6.3；严重度只由规则决定，不接受模型或用户自由指定。
RULES = (("no_quantify", "medium"), ("weak_verb", "low"),
         ("timeline_overlap", "high"), ("timeline_invalid", "high"),
         ("open_ended", "medium"), ("relative_time", "medium"),
         ("missing_section", "high"), ("skill_flat", "medium"))
# 标题只决定节归属，单独的标题不占段号；同一节多次出现时段号连续。
_HEADINGS = {
    "技能": ("技能", "专业技能", "技术技能", "技术栈", "职业技能", "技能清单"),
    "工作经历": ("工作经历", "工作经验", "职业经历", "任职经历", "实习经历"),
    "教育": ("教育", "教育背景", "教育经历", "学习经历", "学历"),
    "项目": ("项目", "项目经历", "项目经验", "项目实践"),
    "其他": ("其他", "其他信息", "个人信息", "基本信息", "个人简历", "自我评价"),
}
# 月份是可选项；年份前后有数字边界，避免把手机号片段当作履历年份。
_DATE_RANGE = re.compile(
    r"(?<!\d)(?P<sy>\d{4})(?:\s*[./年-]\s*(?P<sm>0?[1-9]|1[0-2])(?!\d)\s*月?)?年?"
    r"(?:\s*[-–—~～到至]\s*|\s*(?=至今|现在|目前))"
    r"(?P<end>(?P<ey>\d{4})(?:\s*[./年-]\s*(?P<em>0?[1-9]|1[0-2])(?!\d)\s*月?)?年?"
    r"|至今|现在|目前)(?!\d)"
)


@dataclass(frozen=True)
class _Paragraph:
    """内部定位辅助结构：保存节、段号和未经改写的原文位置。"""

    # text 保留原始行内容，查证不使用重建文本。
    section: str
    index: int
    text: str
    start: int
    end: int

    @property
    def location(self) -> str:
        """生成可读地点；无参数，返回节名与段号，异常无。"""
        return f"{self.section} · 第 {self.index} 段"


@dataclass(frozen=True)
class _Interval:
    """内部时间区间：月份闭区间以及其所属原文段落。"""

    # 年份未写月份时覆盖全年，不擅自推测具体入离职日期。
    start: int
    end: int
    text: str
    paragraph: _Paragraph


def _paragraphs(text: str) -> list[_Paragraph]:
    """识别简历分节；参数为原文，返回非空内容段列表，异常无。"""
    paragraphs: list[_Paragraph] = []
    section, offset = "其他", 0
    counters = dict.fromkeys(_HEADINGS, 0)
    # 逐行定位能保留 PDF/Word 抽取后的原文连续性，也便于处理项目符号。
    # 空行仅推进原文偏移，不制造没有证据的段落。
    for raw in text.splitlines(keepends=True):
        line = raw.strip()
        content = line
        # 只去标题前的排版标记，不把「负责技能培训」之类正文误识别为标题。
        heading = re.sub(r"^[#\s一二三四五六七八九十\d、.（）()]+", "", line).strip()
        parts = re.split(r"[:：]", heading, maxsplit=1)
        heading_name = "".join(parts[0].split())
        # 标题词必须完整匹配；正文里的「公司项目经历丰富」不触发切节。
        matched = next((name for name, aliases in _HEADINGS.items() if heading_name in aliases), None)
        if matched is not None:
            section = matched
            content = parts[1].strip() if len(parts) == 2 else ""
        if content:
            counters[section] += 1
            # inline 标题后的正文仍来自原行，位置偏移也按该原文计算。
            # 标题与正文分开计数，但 evidence 始终截取原文中的真实内容。
            start = offset + raw.find(content)
            paragraphs.append(_Paragraph(section, counters[section], content, start, start + len(content)))
        offset += len(raw)
    return paragraphs


def _finding(code: str, paragraph: _Paragraph, evidence: str, detail: str) -> DiagnosisFinding:
    """构造固定规则发现；参数为 code、段落、原文与说明，返回模型，未知 code 抛 KeyError。"""
    # 从唯一规则表取得严重度，避免同一 code 在不同入口出现不一致级别。
    return DiagnosisFinding(code=code, severity=dict(RULES)[code], location=paragraph.location,
                            evidence=evidence, detail=detail)


def _text_findings(paragraphs: list[_Paragraph]) -> list[DiagnosisFinding]:
    """检查职责与时间措辞；参数为原文段落，返回候选发现，异常无。"""
    findings: list[DiagnosisFinding] = []
    for paragraph in paragraphs:
        # 绝对日期、邮箱或技能清单都不是职责句；无标题时只接收明确职责动词。
        duty = paragraph.section in ("工作经历", "项目") or re.match(r"^[·•\s-]*(?:负责|参与|协助|配合)", paragraph.text)
        if duty and not _DATE_RANGE.search(paragraph.text):
            # 逗号不拆句，防止把「负责优化，从 2 秒降至 120 毫秒」拆成无量化半句。
            for part in re.findall(r"[^。；;！？!?]+[。；;！？!?]?", paragraph.text):
                sentence = re.sub(r"^[\s·•●▪*\-]+", "", part).strip()
                if not sentence:
                    continue
                # 严格使用任务书的数字或量级词判据，不靠语义猜测量化成果。
                if not re.search(r"\d|[万千百亿倍%％]", sentence):
                    findings.append(_finding("no_quantify", paragraph, sentence[:120], "职责未提供数字或量级，成果缺少可核对的依据"))
                # 实现/达成沿用 M1 任务书补充的结果词，与其余五词同样处理。
                if re.match(r"^(?:负责|参与|协助|配合)", sentence) and not re.search(r"提升|降低|上线|交付|支撑|实现|达成", sentence):
                    findings.append(_finding("weak_verb", paragraph, sentence[:120], "以弱职责动词开头，且未说明结果或交付"))
        # 至今与相对时间可出现在任意节，不只检查工作经历。
        if re.search(r"至今|现在|目前", paragraph.text):
            findings.append(_finding("open_ended", paragraph, paragraph.text[:120], "存在开放式时间，工作年限需人工确认"))
        if re.search(r"去年|今年|最近|近期|应届", paragraph.text):
            findings.append(_finding("relative_time", paragraph, paragraph.text[:120], "使用相对时间，建议核实并补充明确年月"))
    return findings


def _timeline_findings(text: str, paragraphs: list[_Paragraph], current_year: int) -> list[DiagnosisFinding]:
    """核对时间区间；参数为原文、段落与当前年，返回倒序/未来/重叠发现，异常无。"""
    findings: list[DiagnosisFinding] = []
    intervals: list[_Interval] = []
    # 先排除无效区间再比较重叠，避免把倒序日期当成另一项时间冲突。
    for paragraph in paragraphs:
        for match in _DATE_RANGE.finditer(paragraph.text):
            start_year = int(match["sy"])
            end_year = int(match["ey"]) if match["ey"] else current_year
            start = start_year * 12 + int(match["sm"] or 1)
            end = end_year * 12 + int(match["em"] or 12)
            # 倒序或未来结束年份都使用同一个规定 code，不增加新类别。
            if start > end or end_year > current_year:
                reason = "起止时间倒序" if start > end else f"结束年份 {end_year} 晚于当前年 {current_year}"
                findings.append(_finding("timeline_invalid", paragraph, match.group().strip(), reason))
                continue
            # 教育与项目可与任职并行；只比较工作经历，未分节文本保留兼容。
            if paragraph.section in ("工作经历", "其他"):
                intervals.append(_Interval(start, end, match.group().strip(), paragraph))
    # 按原文顺序枚举配对，不使用无序集合决定输出先后。
    for index, left in enumerate(intervals):
        for right in intervals[index + 1:]:
            if left.paragraph == right.paragraph or max(left.start, right.start) > min(left.end, right.end):
                continue
            # 两处证据之间保留全部原文，不能用省略号拼接出不可检索的引用。
            evidence = text[left.paragraph.start:right.paragraph.end]
            detail = f"{left.paragraph.location}（{left.text}）与{right.paragraph.location}（{right.text}）时间重叠"
            findings.append(_finding("timeline_overlap", left.paragraph, evidence, detail))
    return findings


def _completeness(text: str, resume: ResumeInfo, paragraphs: list[_Paragraph]) -> dict[str, bool]:
    """检查五项信息；参数为原文、结构化输入与段落，返回固定五键布尔值，异常无。"""
    # 优先接受已有结构字段，正则仅作漏抽兜底，不反向修改 ResumeInfo。
    name = bool((resume.name or "").strip())
    if not name:
        name = bool(re.search(r"姓名\s*[:：]\s*[\u3400-\u9fff]{2,4}", text))
        # 无标签姓名常位于首行；段落已经排除纯标题，避免将「工作经历」当人名。
        if not name and paragraphs:
            name = bool(re.match(r"^[\u3400-\u9fff]{2,4}(?=\s*(?:[|｜，,]|$))", paragraphs[0].text))
    number = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百]+)\s*年"
    # 0 年是明确已填写；不能使用 bool(resume.years) 错判应届候选人。
    years = resume.years is not None or bool(re.search(
        rf"工作年限\s*[:：]?\s*{number}|{number}\s*(?:工作|从业|开发)?\s*(?:经验|经历)|(?:工作|工龄|从业)\s*{number}", text))
    education = any(word in text for word in EDU_LEVEL)
    # ResumeInfo 只有姓名、年限与技能，学历需从原文识别，不能读不存在的字段。
    # 技能节有正文即算提供了该项；只有方向词的问题由 skill_flat 单独指出。
    skills = bool(resume.skills) or any(paragraph.section == "技能" for paragraph in paragraphs)
    skills = skills or bool(re.search(r"(?:技术栈|熟悉|熟练|掌握|使用|擅长|会)\s*[:：]?\s*[A-Za-z][A-Za-z0-9+#.-]+", text))
    phone = re.search(r"(?<!\d)(?:\+?86[\s-]?)?1[3-9]\d{9}(?!\d)", text)
    email = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    # 手机或邮箱任一有效即有联系方式；没有邮箱且没有手机才算缺失。
    return dict(name=name, years=years, education=education, skills=skills, contact=bool(phone or email))


def _candidate_findings(text: str, resume: ResumeInfo, paragraphs: list[_Paragraph],
                        completeness: dict[str, bool], current_year: int) -> list[DiagnosisFinding]:
    """汇总八条规则；参数为原文、简历、分段、完整性和当前年，返回候选发现，异常无。"""
    findings = _text_findings(paragraphs) + _timeline_findings(text, paragraphs, current_year)
    # 缺失信息本身无法引用，引用首个实际段落作为检查对象，缺项名称写进 detail。
    # 空简历没有任何可用证据，仍提交空候选，由统一护栏丢弃并计数。
    anchor = paragraphs[0] if paragraphs else _Paragraph("其他", 1, text.strip()[:120], 0, len(text))
    labels = dict(name="姓名", years="工作年限", education="学历", skills="技能", contact="联系方式（手机或邮箱）")
    for key, present in completeness.items():
        if not present:
            # 每个缺项保留一条说明，使调用方能够逐项显示完整性缺口。
            findings.append(_finding("missing_section", anchor, anchor.text[:120], f"简历缺少{labels[key]}信息"))
    # 仅使用共享方向词集合；长词先替换，避免「开发工程师」被拆得剩下无意义部分。
    roles = re.compile("|".join(re.escape(word) for word in sorted(ROLE_WORDS, key=lambda value: (-len(value), value))))
    skill_paragraphs = [paragraph for paragraph in paragraphs if paragraph.section == "技能"]
    # 删除说明标签与熟练度词后，技能节须全部由方向词和分隔符构成才报 skill_flat。
    # 同一技能节另一行已有 Go 时，不把单独写「后端」的那一行误报为空泛技能。
    skill_text = "\n".join(paragraph.text for paragraph in skill_paragraphs)
    remaining = roles.sub("", skill_text)
    remaining = re.sub(r"熟悉|熟练|掌握|擅长|方向|技能|技术栈|[\s、，,；;:：/|·。]+", "", remaining)
    if not remaining:
        for paragraph in skill_paragraphs:
            if not roles.search(paragraph.text):
                continue
            findings.append(_finding("skill_flat", paragraph, paragraph.text[:120], "技能段只有岗位方向词，缺少具体技术名称"))
    return findings


def _keyword_sources(job: JobInfo) -> list[tuple[str, bool, str | None]]:
    """建立关键词来源；参数为岗位模型，返回去重前的词、必备标记和族，异常无。"""
    # 同一套公开分组函数负责必备技能族归属，族表和别名表均由评分模块提供。
    families = {normalize_skill(name): family
                for family, names in group_required_skills(job.must_have) for name in names}
    sources = [(term.strip(), True, families[normalize_skill(term)]) for term in job.must_have if term.strip()]
    # 职责只抽英文候选词；普通中文职责不自动变成必须覆盖的关键词。
    sources.extend((term.rstrip(".-"), False, None)
                   for responsibility in job.responsibilities for term in TECH_TOKEN.findall(responsibility))
    return sources


def _keyword_coverage(text: str, paragraphs: list[_Paragraph], job: JobInfo) -> tuple[list[KwCoverage], int]:
    """统计 ATS；参数为原文、分段和岗位，返回覆盖列表及超限词数，异常无。"""
    sources: dict[str, tuple[str, bool, str | None]] = {}
    # 关键词按归一后的词去重；不同的同族具体词仍各自保留 JD 原名。
    for term, required, family in _keyword_sources(job):
        normalized = normalize_skill(term)
        if normalized:
            # 必备词先进入，职责中的同名词不会把 from_must_have 降成 False。
            sources.setdefault(normalized, (term, required, family))
    # 每个保留字符保存原文位置，跨行技术名也能定位回原来的节。
    positions: list[int] = []
    characters: list[str] = []
    for index, char in enumerate(text):
        if not char.isspace():
            lowered = char.lower()
            characters.append(lowered)
            # Unicode 小写偶尔展开成多个字符，每个输出字符都回指同一原文位置。
            positions.extend([index] * len(lowered))
    compact = "".join(characters)
    # M1 的统计遵循 PRD 子串口径，区别于 M0 检查改写新增英文词的完整词口径。
    coverage: list[KwCoverage] = []
    for normalized, (term, required, family) in list(sources.items())[:30]:
        # 必备项可命中族内任一具体成员；泛称自身不证明有具体产品经验。
        members = SKILL_FAMILIES[family] if required and family else {normalized}
        patterns = set(members)
        if not family:
            # 别名仍来自评分模块，避免 JS/Postgres 等原文写法与技能分互相矛盾。
            patterns.update(alias for alias in SKILL_ALIASES if normalize_skill(alias) == normalized)
        # 长词优先且非重叠计数，ChatGPT / GPT 或 ChromaDB / Chroma 不重复累计。
        expression = re.compile("|".join(re.escape(member) for member in sorted(patterns, key=lambda value: (-len(value), value))))
        matches = list(expression.finditer(compact))
        sections: list[str] = []
        for paragraph in paragraphs:
            if any(positions[match.start()] < paragraph.end and positions[match.end() - 1] >= paragraph.start for match in matches):
                if paragraph.section not in sections:
                    sections.append(paragraph.section)
        # 文本标题也属于原文；命中只在标题时归「其他」，不产生空位置覆盖。
        if matches and not sections:
            sections.append("其他")
        # count 必须来自原文实际匹配次数，结构化 skills 不会凭空增加次数。
        coverage.append(KwCoverage(term=term, count_in_resume=len(matches), sections=sections, from_must_have=required))
    return coverage, max(0, len(sources) - 30)


def diagnose(resume_text: str, resume: ResumeInfo, job: JobInfo) -> DiagnosisReport:
    """纯规则诊断简历，不调模型、不联网、不读环境或文件。

    参数：resume_text 为完整简历原文，resume 为已有 ResumeInfo，job 为已有 JobInfo。
    返回：内部 DiagnosisReport，含最多二十条可检索发现、ATS 与完整性。
    异常：无（输入须符合已有类型契约）。
    """
    started = perf_counter()
    paragraphs = _paragraphs(resume_text)
    completeness = _completeness(resume_text, resume, paragraphs)
    # 本地时钟只用于当前年规则和耗时元数据；不推算或覆盖简历已抽取的年限。
    candidates = _candidate_findings(resume_text, resume, paragraphs, completeness, date.today().year)
    findings: list[DiagnosisFinding] = []
    dropped = 0
    for finding in candidates:
        # 复用 M0 的唯一证据入口，不另写 substring/空白/标点校验规则。
        if find_original(resume_text, finding.evidence):
            findings.append(finding)
        else:
            dropped += 1
    order = {code: index for index, (code, _) in enumerate(RULES)}
    severity = {"high": 0, "medium": 1, "low": 2}
    # Python 稳定排序在同级同规则时保留原文顺序，输出不依赖 set 遍历。
    findings.sort(key=lambda finding: (severity[finding.severity], order[finding.code]))
    truncated_findings = max(0, len(findings) - 20)
    findings = findings[:20]
    coverage, truncated_keywords = _keyword_coverage(resume_text, paragraphs, job)
    return DiagnosisReport(
        findings=findings, keyword_coverage=coverage,
        missing_keywords=[item.term for item in coverage if item.count_in_resume == 0],
        completeness=completeness,
        # dropped 专指证据不可检索，与正常上限截断分开记录。
        meta=dict(resume_chars=len(resume_text), finding_count=len(findings),
                  elapsed_ms=round((perf_counter() - started) * 1000, 3), cost_yuan=0,
                  dropped=dropped, truncated=truncated_findings + truncated_keywords,
                  truncated_findings=truncated_findings, truncated_keywords=truncated_keywords),
    )
