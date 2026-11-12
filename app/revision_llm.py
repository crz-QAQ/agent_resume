"""M3：以已有诊断和评分事实生成意见，按七道护栏逐条审核并保留拒绝记录。

不重算分数，不生成文件；模型只为代码列出的诊断条目写建议。
"""

from __future__ import annotations

from collections import Counter
import json
import re
from time import perf_counter
from typing import TYPE_CHECKING

from pydantic import SerializeAsAny, ValidationError

from app.fallback import call_with_fallback_async
from app.guard_revision import NUMBER_PLACEHOLDER as _PLACEHOLDER, find_original, normalize_text, validate_issue
from app.job_profile import require_confirmed
from app.prompts import SYSTEM_REVISION
from app.resume_diagnose import _paragraphs
from app.schemas_match import JobInfo
from app.schemas_revision import DiagnosisReport, JobProfile, ReviewIssue, RevisionReport
from app.scoring_rules import SKILL_FAMILIES, group_required_skills, normalize_skill

# 类型标注不初始化抽取客户端或加载本地语义模型。
if TYPE_CHECKING:
    from app.schemas import ResumeInfo
    from app.scoring_rules import HardScore
    from app.scoring_semantic import SemanticScore


ISSUE_CATEGORIES = ("missing", "weak_evidence", "buried", "noise", "inconsistent")
SEVERITIES = ("high", "medium", "low")
CATEGORY_PRIORITY = {"missing": 0, "weak_evidence": 1, "buried": 2, "inconsistent": 3, "noise": 4}
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
MAX_ISSUES = 12
MAX_HIGH = 5
MAX_PER_LOCATION = 2
MAX_RESUME_CHARS = 4000
VAGUE_GAIN = ("更专业", "更有竞争力", "更强", "更好", "更完善", "更加完善",
              "提升整体", "加分项", "有优势", "脱颖而出")

# 版本与预算只属于 M3，不能改动旧抽取及评分提示词。
PROMPT_VERSION = "revision_v1.1"
COST_BUDGET_YUAN = 0.005
# 分类来自 M1 的固定诊断 code；模型不得把时间问题改成技术缺口。
_FINDING_CATEGORY = {"no_quantify": "weak_evidence", "weak_verb": "weak_evidence",
                     "timeline_overlap": "inconsistent", "timeline_invalid": "inconsistent",
                     "open_ended": "inconsistent", "relative_time": "inconsistent",
                     "missing_section": "missing", "skill_flat": "weak_evidence"}
_ISSUE_FIELDS = tuple(name for name in ReviewIssue.model_fields if name != "id")
# D-M3-02：仅这两项可缺省；不能顺手为证据或核心内容编默认值。
_OPTIONAL_DEFAULTS = {"location": "—", "risk_note": "涉及数字、规模、成果的建议，必须是你真实的数据"}
_CODE = re.compile(r"```|</?[A-Za-z][^>]*>|javascript:|^\s*(?:def |class |import |from \S+ import )", re.I | re.M)


class _InjectedIssue(ReviewIssue):
    """代码生成的条件式缺口提示；来源不能由模型输入伪造。"""

    # 使用局部子类，保持 M0 的既有 ReviewIssue 字段契约不变。
    source: str = "injected"


class _RevisionReport(RevisionReport):
    """保留注入意见来源的报告，字段与既有 RevisionReport 完全兼容。"""

    # 基类默认会裁掉子类新增字段；SerializeAsAny 让 JSON 也保留 source。
    issues: list[SerializeAsAny[ReviewIssue]]


def _job_sources(job: JobInfo | None, profile: JobProfile | None) -> list[str]:
    """取得独立来源片段；真实 JD 优先，不把 JSON 键名或另一份画像加入白名单。"""
    if job is None:
        # 画像标题和说明不是岗位要求，不能被拿来为新增技术背书。
        return [*profile.must_have, *profile.responsibilities] if profile else []
    parts = []
    # JobInfo 无 JD 全文槽位，只能使用八字段中保留的原文片段。
    for value in job.model_dump().values():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            # 列表元素分别保留边界，证据检索不能跨两条职责拼句。
            parts.extend(value)
    return parts


def build_insight_block(job: JobInfo | None, profile: JobProfile | None,
                        hard: HardScore | None, sem: SemanticScore | None) -> str:
    """把评分事实整理成已知事实：未覆盖职责、missing_skill 和应保留的已覆盖职责。"""
    sources = _job_sources(job, profile)
    # 即使无上游结果也明确说明缺测，不能把「未提供」说成「全部覆盖」。
    lines = ["已知事实（上游评分已算好，不重算、不推测）："]
    if sem is not None:
        for item in sem.per_item:
            # 不能把另一岗位的评分条目注入当前 JD；covered 使用上游布尔值。
            if item.item not in sources:
                continue
            mark = "✓ 已覆盖，保留、不要求改" if item.covered else "✗ 未覆盖，优先指出缺口"
            # 相似度仅用于解释，不能重判 covered 或决定建议总条数。
            lines.append(f"{mark}：{item.item}（cos={item.best_sim:.3f}, rerank={item.rerank_score:.3f}）")
    if hard is not None:
        for item in hard.deductions:
            # 年限/学历等扣分不能冒充 missing_skill，也不让模型重算扣分。
            if item.type == "missing_skill" and any(find_original(source, item.jd_evidence) for source in sources):
                lines.append(f"✗ 必备技能：{item.jd_evidence}（-{item.deduct} 分，missing_skill；{item.item}）")
    if len(lines) == 1:
        lines.append("未提供可用评分证据；只围绕 M1 诊断，不臆测已评分或未覆盖。")
    return "\n".join(lines)


def _build_targets(text: str, diagnosis: DiagnosisReport, sources: list[str],
                   hard: HardScore | None, sem: SemanticScore | None) -> list[dict]:
    """从 M1 和评分证据建立待写条目；分类、严重度、原句和位置都由代码提供。"""
    targets = []
    for finding in diagnosis.findings:
        # M1 未识别的 code 不能由语言模型自创类别兜底。
        category = _FINDING_CATEGORY.get(finding.code)
        if category and finding.evidence in text and finding.severity in SEVERITIES:
            targets.append(dict(category=category, severity=finding.severity, location=finding.location,
                                original_text=finding.evidence, detail=finding.detail))
    paragraphs = _paragraphs(text)
    # 缺口没有「不存在的原句」；使用真实技能/职责段作补充位置，绝不杜撰经历。
    anchors = [p for p in paragraphs if p.section in ("技能", "工作经历", "项目")] or paragraphs
    gaps = [(term, "high") for term in diagnosis.missing_keywords
            if any(find_original(source, term) for source in sources)]
    if hard is not None:
        # 与 ATS 缺词交叉的硬扣分稍后合并，避免重复请求同一个缺口。
        gaps.extend((d.jd_evidence, d.severity) for d in hard.deductions if d.type == "missing_skill"
                    and any(find_original(source, d.jd_evidence) for source in sources))
    if sem is not None:
        # 语义已有覆盖时禁止把它反转成缺失经历。
        gaps.extend((item.item, "medium") for item in sem.per_item
                    if not item.covered and item.item in sources)
    seen_gaps = set()
    for evidence, severity in gaps:
        # 无原文锚点就没有可交付意见；不能拿 JD 本身充当简历原句。
        if evidence in seen_gaps or not anchors:
            continue
        # 循环选择已有段落只是定位补充处，不声称该段已经覆盖相应职责。
        anchor = anchors[len(seen_gaps) % len(anchors)]
        seen_gaps.add(evidence)
        targets.append(dict(category="missing", severity=severity if severity in SEVERITIES else "medium",
                            location=anchor.location, original_text=anchor.text,
                            detail=f"缺口：{evidence}；只建议核实后补充，不写成已有经历", jd_evidence=evidence))
    # 同一个原句只下发一个最高优先级任务，减少模型重复写作与实际计费。
    targets.sort(key=lambda row: (SEVERITY_RANK[row["severity"]], CATEGORY_PRIORITY[row["category"]]))
    unique = {}
    for target in targets:
        # 稳定排序后第一次出现即最高优先级，不依赖 set 的无序迭代。
        unique.setdefault(target["original_text"], target)
    return list(unique.values())


def _reject(rejected: list[dict], raw: object, reason: str) -> None:
    """追加简短审计记录，保留原类别和最多四十字原句，不把拒绝项修复回填。"""
    row = raw if isinstance(raw, dict) else {}
    # 非对象候选同样留下记录，候选数必须等于接收数与拒绝数之和。
    rejected.append({"category": row.get("category", ""),
                     "original_text": str(row.get("original_text", ""))[:40], "reason": reason})


def _strip_inferred_note(why: str) -> str:
    """纯函数：仅移除画像/推测声明及其包裹符，整理分号、空白，保留其余文字。"""
    note = r"(?:岗位\s*画像(?:\s*[（(]\s*推测\s*[）)])?|推测)"
    # 先匹配整段括号或引号声明，避免删除词语后留下空括号。
    pattern = rf"(?:[（(]\s*{note}\s*[）)]|「\s*{note}\s*」|『\s*{note}\s*』|{note})"
    cleaned, removed = re.subn(pattern, "", why)
    if not removed:
        # 无声明的理由逐字返回，不能顺带润色或改变原有事实。
        return why
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"\s*([;；])\s*", r"\1", cleaned)
    # 中间声明删掉后保留一个原分号；尾部声明删掉后不残留分号。
    cleaned = re.sub(r"([;；])[;；]+", r"\1", cleaned)
    return cleaned.strip(" ;；")


def _clean_user_jd_whys(issues: list[ReviewIssue], rejected: list[dict]) -> list[ReviewIssue]:
    """清理真实 JD 意见的来源声明；空理由追加拒绝审计，返回重新编号的副本。"""
    cleaned = []
    for issue in issues:
        why = _strip_inferred_note(issue.why)
        if not why.strip():
            # 不伪造理由，也不将已拒意见修复回填；每条只追加一次审计。
            _reject(rejected, issue.model_dump(), "理由仅剩推测声明")
            continue
        cleaned.append(issue.model_copy(update={"why": why, "id": f"R-{len(cleaned) + 1:03d}"}))
    return cleaned


def _guard_candidates(candidates: list, text: str, sources: list[str],
                      targets: list[dict]) -> tuple[list[ReviewIssue], list[dict]]:
    """依序执行七道护栏；所有丢弃项均留下首个失败原因，最后由代码重新编号。"""
    accepted, rejected, location_keys = [], [], {}
    # 每条只记录最先触发的失败，不对同一条计入多个 rejected。
    for raw in candidates:
        # ① 先检查枚举和结构，不让 Pydantic 转换未知类型或模型字段。
        if not isinstance(raw, dict):
            _reject(rejected, raw, "意见不是 JSON 对象")
            continue
        if raw.get("category") not in ISSUE_CATEGORIES:
            _reject(rejected, raw, f"未知类别: {raw.get('category')}")
            continue
        if raw.get("severity") not in SEVERITIES:
            _reject(rejected, raw, f"未知严重度: {raw.get('severity')}")
            continue
        # ② 必须先过 M0 检索；另外要求逐字子串，不能借忽略标点拼接原文。
        original = raw.get("original_text")
        if not isinstance(original, str) or not find_original(text, original) or original not in text:
            _reject(rejected, raw, "原句不在简历中（必须逐字复制）")
            continue
        try:
            # 必填六项和岗位证据仍严格取值，只对可选字段提供默认值。
            values = {key: raw[key] if key in raw else _OPTIONAL_DEFAULTS[key] for key in _ISSUE_FIELDS}
            if any(not isinstance(value, str) for value in values.values()):
                raise ValueError("意见字段必须为字符串")
            # id/total/verdict/source 等模型自报字段不会进入输出。
            issue = ReviewIssue(id="", **values)
        except KeyError as exc:
            # 明确缺哪个必填字段，避免把核心内容缺失和可选项省略混为一谈。
            _reject(rejected, raw, f"意见字段不合法: 缺少必填字段 {exc.args[0]}")
            continue
        except (ValidationError, ValueError) as exc:
            # 单条缺字段不能让整份响应失败，也不能静默消失。
            _reject(rejected, raw, f"意见字段不合法: {type(exc).__name__}")
            continue
        # ③ 每条执行 M0；XX 连同量级整体豁免，不扩充事实来源。
        checked = issue.model_copy(update={"suggested_text": _PLACEHOLDER.sub("", issue.suggested_text)})
        valid, reason = validate_issue(checked, text, "\n".join(sources))
        if not valid:
            # 保留 M0 的具体违规内容，并补齐任务书要求的「新名词」检索词。
            _reject(rejected, raw, "新名词/" + reason if "技术词" in reason else reason)
            continue
        # 同一道事实护栏还禁止代码/HTML，以及把没有的技能写成已有经历。
        if _CODE.search(issue.suggested_text):
            _reject(rejected, raw, "建议包含代码/HTML")
            continue
        if issue.category == "missing" and issue.suggested_text.strip():
            # 模型缺口仍不得写成经历；有条件的代码模板走独立核验入口。
            _reject(rejected, raw, "缺失项只能诚实提示补充，不得写成已有经历")
            continue
        if not issue.why.strip() or not issue.risk_note.strip() or (not issue.suggested_text.strip() and issue.category != "missing"):
            # missing 不写建议正文；其他类别不能只有解释没有改写。
            _reject(rejected, raw, "理由、诚实提醒或改写正文为空")
            continue
        if re.search(r"\d|XX|规模|成果|结果|提升|降低|支撑|交付", issue.suggested_text) and "真实" not in issue.risk_note:
            if re.search(r"保证.*(?:成功|录用)|必(?:然成功|定录用)", issue.risk_note):
                _reject(rejected, raw, "risk_note 含结果保证，不能替代真实数据提醒")
                continue
            # 可选提醒的措辞不完整时由代码补上诚实兜底，不因缺某个字误拒时间意见。
            issue = issue.model_copy(update={"risk_note": _OPTIONAL_DEFAULTS["risk_note"] + "；" + issue.risk_note})
        # 只接收代码已有任务，模型不能用合法枚举包装新发现或伪造位置。
        target = next((target for target in targets if all(getattr(issue, key) == target[key] for key in
                       ("category", "severity", "original_text")) and
                       (issue.location == "—" or issue.location == target["location"])), None)
        if target is None:
            _reject(rejected, raw, "不属于诊断/评分待写条目，或类别、严重度、位置被改动")
            continue
        # ④ 分别回查每段来源，不能跨 JD 条目拼接证据；真实 JD 排除旁路画像。
        if not any(find_original(source, issue.jd_evidence) for source in sources):
            # 不接受模型自己在解释里声称「来自 JD」作为查证结果。
            _reject(rejected, raw, "JD 依据查不到")
            continue
        # ⑤ 要有具体前后状态；只有箭头或含任一空话都不能通过。
        contrast = (re.search(r"\S.*→.*\S", issue.gain)
                    or re.search(r"从.+到.+", issue.gain) or re.search(r".+变成.+", issue.gain))
        if any(phrase in issue.gain for phrase in VAGUE_GAIN) or not contrast:
            _reject(rejected, raw, "gain 空话或缺少改前到改后的具体对比")
            continue
        # 缺省位置仍按诊断的真实段落计限额，不能把所有「—」误当成同一段。
        location_keys[id(issue)] = target["location"]
        accepted.append(issue)
    # 只有前五道全部通过的候选才参与去重；拒绝项永远不会流回 accepted。
    # ⑥ 同原句按最高严重度保留，平级保留模型先给的那条。
    best = {}
    for issue in accepted:
        key = normalize_text(issue.original_text)
        previous = best.get(key)
        # 严格小于意味着平级不替换，从而保留模型最初的顺序。
        if previous is None or SEVERITY_RANK[issue.severity] < SEVERITY_RANK[previous.severity]:
            best[key] = issue
    deduped, locations = [], Counter()
    for issue in accepted:
        if best[normalize_text(issue.original_text)] is not issue:
            _reject(rejected, issue.model_dump(), "同原句重复，仅保留最高严重度（平级保留先给者）")
        elif locations[location_keys[id(issue)]] >= MAX_PER_LOCATION:
            _reject(rejected, issue.model_dump(), f"同 location 超过 {MAX_PER_LOCATION} 条")
        else:
            # 位置计数只加实际保留项，重复项不能占用同段限额。
            locations[location_keys[id(issue)]] += 1
            deduped.append(issue)
    # ⑦ 稳定排序保留模型原顺序作为最终决胜；high 和总条数上限均由代码强制。
    deduped.sort(key=lambda issue: (SEVERITY_RANK[issue.severity], CATEGORY_PRIORITY[issue.category]))
    issues, high_count = [], 0
    for issue in deduped:
        if issue.severity == "high" and high_count >= MAX_HIGH:
            _reject(rejected, issue.model_dump(), f"high 超过 {MAX_HIGH} 条")
        elif len(issues) >= MAX_ISSUES:
            _reject(rejected, issue.model_dump(), f"总条数超过 {MAX_ISSUES} 条")
        else:
            # 编号在最终输出时生成，去重或截断后不留下跳号。
            high_count += issue.severity == "high"
            issues.append(issue.model_copy(update={"id": f"R-{len(issues) + 1:03d}"}))
    return issues, rejected


def _mentions(text: str, skill: str) -> bool:
    """按完整技能词检查提及，忽略大小写/空白，避免把 Django 中的 go 算作 Go。"""
    term = "".join(skill.split()).casefold()
    # 中文上下文允许紧邻技术名，英文技术词不能只是另一个单词的子串。
    # 保留原文词间空白，避免把「Go MySQL」拼成单词后漏掉已存在的 Go。
    pattern = r"(?<![a-z0-9_])" + r"\s*".join(re.escape(char) for char in term) + r"(?![a-z0-9_])"
    return bool(term) and re.search(pattern, text.casefold()) is not None


def _missing_skills(text: str, resume: ResumeInfo, job: JobInfo | None,
                    profile: JobProfile, hard: HardScore | None) -> list[tuple[str, str]]:
    """复核 missing_skill：必须属于当前 JD 必备项，且原文和结构化技能均无覆盖。"""
    if hard is None:
        return []
    requirements = job.must_have if job else profile.must_have
    sources = _job_sources(job, profile)
    resume_skills = {normalize_skill(skill) for skill in resume.skills}
    gaps = []
    for deduction in hard.deductions:
        if deduction.type != "missing_skill":
            continue
        for family, names in group_required_skills(requirements):
            # 只从当前 JD 的真实条目取技能名，不能信任任意传入扣分文本。
            named = [name for name in names if _mentions(deduction.jd_evidence, name)]
            if not named:
                continue
            members = SKILL_FAMILIES[family] if family else {normalize_skill(names[0])}
            # 沿用评分的别名/技能族；过期扣分不能让已有能力再次被称为缺口。
            if any(member in resume_skills or _mentions(text, member) for member in members):
                continue
            skill = named[0]
            evidence = next((source for source in sources if _mentions(source, skill)), None)
            # 找不到岗位原文片段时不制造证据；同一技能重复扣分只处理一次。
            if evidence and not any(_mentions(existing, skill) for existing, _ in gaps):
                gaps.append((skill, evidence))
    return gaps


def _inject_missing(issues: list[ReviewIssue], text: str, gaps: list[tuple[str, str]],
                    sources: list[str]) -> tuple[list[ReviewIssue], list[dict]]:
    """为尚未提及的真实缺技能追加条件模板；不改 rejected、不回收被拒模型意见。"""
    result, skipped = list(issues), []
    paragraphs = _paragraphs(text)
    locations = Counter()
    for issue in result:
        # 可选位置为「—」时仍查回真实段落，保证同段上限不会被注入绕过。
        location = next((p.location for p in paragraphs if issue.original_text in p.text
                         or p.text in issue.original_text), issue.location) if issue.location == "—" else issue.location
        locations[location] += 1
    for skill, evidence in gaps:
        # 已有三种证据字段任一个提到该技能就不重复追加；why 不替代证据。
        if any(_mentions(value, skill) for issue in result for value in
               (issue.original_text, issue.suggested_text, issue.jd_evidence)):
            continue
        if len(result) >= MAX_ISSUES:
            # 不为兜底挤掉已接收候选；上限无法容纳时明确保留未覆盖指标。
            skipped.append({"skill": skill, "reason": "缺口兜底达到总条数上限"})
            continue
        duties = [p for p in paragraphs if p.section in ("工作经历", "项目")
                  and not re.match(r"^\d{4}", p.text)
                  and re.search(r"负责|参与|协助|配合|设计|开发|优化|交付|实现|部署", p.text)]
        # 优先选与岗位依据共有词组最多的职责句，再退回真实技能段和其他原文。
        normalized = normalize_text(evidence)
        pairs = {normalized[i:i + 2] for i in range(len(normalized) - 1)}
        duties.sort(key=lambda p: sum(pair in normalize_text(p.text) for pair in pairs), reverse=True)
        anchors = duties + [p for p in paragraphs if p.section == "技能"] + paragraphs
        anchor = next((p for p in anchors if locations[p.location] < MAX_PER_LOCATION), None)
        if anchor is None:
            # ResumeInfo 拼接不能证明原句存在；宁可明确跳过也不生成悬空证据。
            skipped.append({"skill": skill, "reason": "无可检索且未满额的简历原文位置"})
            continue
        high_count = sum(issue.severity == "high" for issue in result)
        injected = _InjectedIssue(id="", category="missing", severity="high" if high_count < MAX_HIGH else "medium",
                    location=anchor.location, original_text=anchor.text,
                    suggested_text=f"在技能栏补充 {skill}，并在相关经历中说明具体使用场景与产出",
                    why=f"JD 明确要求 {skill}，简历未体现；这是硬性必备项，缺失会直接扣分",
                    gain=f"未体现 {skill} → 核实真实经历后补充技能及可验证的使用场景",
                    jd_evidence=evidence, risk_note="如无真实使用经历请勿添加")
        # 模板也过 M0，禁止版本号等 JD 数字成为简历新数字，禁止新专名。
        valid, reason = validate_issue(injected, text, "\n".join(sources))
        if not valid or not any(find_original(source, evidence) for source in sources):
            skipped.append({"skill": skill, "reason": reason or "JD 依据查不到"})
            continue
        locations[anchor.location] += 1
        result.append(injected)
    # 注入不是恢复 rejected；重新稳定排序和编号，不把模型 ID 带回输出。
    result.sort(key=lambda issue: (SEVERITY_RANK[issue.severity], CATEGORY_PRIORITY[issue.category]))
    return [issue.model_copy(update={"id": f"R-{index:03d}"}) for index, issue in enumerate(result, 1)], skipped


async def generate_issues(resume_text: str, resume: ResumeInfo, job: JobInfo | None,
                          profile: JobProfile | None, diagnosis: DiagnosisReport, *,
                          hard: HardScore | None = None, sem: SemanticScore | None = None) -> RevisionReport:
    """生成并审核修改意见；非法 JSON 重试一次，模型失败返回 failed，未确认画像仍由 M2 拒绝。"""
    started = perf_counter()
    if job is not None:
        # 真实 JD 优先，丢弃无关画像；不改调用方传入的任何对象。
        profile = JobProfile(title=job.title or "未命名岗位", source="user_jd", confirmed=True,
                             must_have=list(job.must_have), responsibilities=list(job.responsibilities), note="基于用户提供的真实 JD")
    elif profile is None:
        raise ValueError("请提供岗位 JD 或已确认的岗位画像")
    else:
        require_confirmed(profile)
        profile = profile.model_copy(deep=True)
    text = resume_text[:MAX_RESUME_CHARS]
    # 后续检索也使用同一截断视窗，模型未见的尾文不能为意见背书。
    sources = _job_sources(job, profile)
    targets = _build_targets(text, diagnosis, sources, hard, sem)
    # 原文只放一次；诊断条目是定位所需的短证据，不重复序列化整份 ResumeInfo。
    prompt = ("按代码已确认的待写条目逐条写意见，每个不同原句写一条，最多8条。"
              "每条必须给出 category/severity/original_text/suggested_text/why/gain 及可回查的 jd_evidence。"
              "时间问题也不能省略这些字段；没有可确定的改写时可逐字保留原句，在 why/risk_note 中提醒核实。"
              "下面是材料数据：\n"
              + json.dumps({"jd_source": profile.source, "岗位": {"title": profile.title, "原文片段": sources},
                            "简历原文": text, "待写条目": targets}, ensure_ascii=False, separators=(",", ":"))
              + "\n" + build_insight_block(job, profile, hard, sem))
    meta = dict(model="fallback（厂商未透传）", prompt_version=PROMPT_VERSION, cost_yuan=0.0,
                jd_source=profile.source, candidate_count=0, accepted_count=0, rejected_count=0,
                resume_chars=len(text), resume_truncated=len(resume_text) > len(text), json_retries=0)
    candidates, status, reason = [], "failed", "模型不可用，请稍后重试"
    # 重试只针对不可解析的响应，不为凑足有效条数而反复调用模型。
    for attempt in range(2):
        try:
            raw, meter = await call_with_fallback_async(prompt, system=SYSTEM_REVISION)
        except Exception:
            # 不将 provider 异常（可能含请求材料）泄漏给调用方；保留前次已计费用。
            reason = "模型不可用，请稍后重试"
            break
        # 每次拿到响应先计费，哪怕随后 JSON 解析失败也不能免费化。
        meta["cost_yuan"] += meter.cost()
        # 现有 fallback 不携带实际 model；有真实元数据才使用，不猜测成功厂商。
        meta["model"] = getattr(meter, "model", meta["model"])
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get("issues"), list):
                # 根结构错误与语法错误同样只重试一次。
                raise ValueError("必须是包含 issues 数组的 JSON 对象")
            candidates, status, reason = payload["issues"], "ok", ""
            break
        except (ValueError, TypeError):
            reason = "模型返回非法 JSON，请稍后重试"
            if attempt == 0:
                meta["json_retries"] = 1
                prompt += '\n上次输出非法。请重试，只输出 {"issues":[...]} 的合法 JSON 对象。'
    issues, rejected = _guard_candidates(candidates, text, sources, targets)
    if profile.source == "user_jd":
        # D-M3-04：七道护栏保持原样，清理在审核之后执行，画像分支不调用。
        issues = _clean_user_jd_whys(issues, rejected)
    model_accepted_count = len(issues)
    # D-M3-03：用完整简历复核缺技能，避免截断尾文已存在的技能被误判缺失。
    gaps = _missing_skills(resume_text, resume, job, profile, hard)
    injection_skipped = []
    if status == "ok":
        # 模型服务全挂仍维持 failed 契约；合法空数组可以由确定性缺口提示兜底。
        issues, injection_skipped = _inject_missing(issues, text, gaps, sources)
        if profile.source == "user_jd":
            # 同样覆盖代码注入的意见，确保最终所有 why 都符合真实 JD 来源。
            issues = _clean_user_jd_whys(issues, rejected)
    if status == "ok" and not issues:
        # 没有合格意见不能伪装成成功，也不能把 rejected 补进 issues。
        status, reason = "needs_review", "模型未产出可用的修改建议"
    # 预算是观测指标，不能用清空真实费用或把已验证意见丢弃来伪装达标。
    meta.update(status=status, reason=reason, candidate_count=len(candidates), accepted_count=len(issues),
                model_accepted_count=model_accepted_count, injected_count=len(issues) - model_accepted_count,
                injection_skipped=injection_skipped,
                rejected_count=len(rejected), elapsed_ms=round((perf_counter() - started) * 1000, 3),
                cost_over_budget=meta["cost_yuan"] > COST_BUDGET_YUAN)
    return _RevisionReport(issues=issues, rejected_issues=rejected, profile=profile, score_before=None,
                          score_after_estimate=None, adaptations=[], skipped=[], meta=meta)
