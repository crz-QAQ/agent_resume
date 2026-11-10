"""按 S5 复核三项子分，先校验证据与建议，再由代码合计 0–30 分。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import TYPE_CHECKING
import unicodedata

from app.fallback import call_with_fallback_async
from app.prompts import SYSTEM_MATCH_RUBRIC
from app.schemas_match import JobInfo

# 类型导入不初始化 schemas 的客户端，也不加载语义模型及其权重。
if TYPE_CHECKING:
    from app.schemas import ResumeInfo
    from app.scoring_rules import HardScore
    from app.scoring_semantic import SemanticScore


# 名称、上限由代码维护，绝不信任模型返回的 label、max_score 或总分。
RUBRIC = (("stack_depth", "技术栈深度", 12),
          ("domain_fit", "业务场景相似度", 10),
          ("proof_strength", "成果可信度", 8))
# 截断限制输入 tokens 与费用并固定上下文；五次短语料实测单次 0.00106280–0.00166600 元。
# 保留 3000 字上限；上述为 UsageMeter 实测，非 3000 字最坏成本压测，详见 S5.md。
MAX_RESUME_CHARS = 3000
# 技术词规则严格采用 S5 §三⑥；大小写不影响是否在来源里出现。
_TECH_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]{1,20}")


@dataclass
class LlmItem:
    """保存固定 rubric 子分，以及可回查的双方引文和评分理由。"""

    # 这些字段均为校验后的数据，固定名称与上限不取自模型。
    key: str
    label: str
    score: int
    max_score: int
    jd_evidence: str
    resume_evidence: str
    reason: str


@dataclass
class LlmScore:
    """保存代码合计、过滤后的建议、异常原因以及已知调用成本。"""

    # failed 始终为零分和空 items，防止无证据子分流入后续汇总。
    score: int
    items: list[LlmItem]
    suggestions: list[str]
    dropped_suggestions: list[str]
    status: str
    reasons: list[str]
    cost_yuan: float


def filter_suggestions(suggestions: list[str], resume_text: str,
                       jd_text: str) -> tuple[list[str], list[str]]:
    """纯函数：丢弃含有双方来源均未出现的英文技术词的整条建议。"""
    kept: list[str] = []
    dropped: list[str] = []
    # 两份来源分别查询，避免拼接后跨来源形成一个并不存在的技术名。
    resume_lower, jd_lower = resume_text.casefold(), jd_text.casefold()
    for suggestion in suggestions:
        # 句末英文句号/连字符不是技术名的一部分，C++、C#、.NET 仍保留。
        words = [word.rstrip(".-").casefold() for word in _TECH_WORD.findall(suggestion)]
        unknown = any(word not in resume_lower and word not in jd_lower for word in words)
        # 任一词无来源就丢整条，不能只删技术名后留下虚构经验描述。
        (dropped if unknown else kept).append(suggestion)
    # 中文建议无法可靠用词法判定事实真伪，不做此校验；事实约束还依赖提示词。
    # JD 已有而简历没有的技术会通过此白名单，提示词仍禁止写成已有经历。
    return kept, dropped


def _normalize_evidence(text: str) -> str:
    """仅去空白和 Unicode 标点，保留原有字词用于子串查证。"""
    # 不折叠大小写或重写数字，防止把内容改写误认为逐字引文。
    return "".join(char for char in text
                   if not char.isspace() and not unicodedata.category(char).startswith("P"))


def _job_text(job: JobInfo) -> str:
    """取八字段的值作为可用 JD 文本，不把 JSON 字段名加入技术白名单。"""
    parts: list[str] = []
    # 当前签名没有 JD 原文，只能核验传入字段里的原始片段，不能重造原句。
    for value in job.model_dump().values():
        if isinstance(value, list):
            parts.extend(value)
        elif value is not None:
            parts.append(str(value))
    return "\n".join(parts)


def _failed(reason: str, cost_yuan: float) -> LlmScore:
    """失败时不保留任何可计分项，但保留已经发生的已知费用。"""
    # 不因响应无法解析而把 meter 已报告的真实成本清零。
    return LlmScore(0, [], [], [], "failed", [reason], cost_yuan)


def _validate_items(payload: dict) -> tuple[list[LlmItem], list[str]]:
    """先验证完整三项、整数、双证据和理由，越界子分 clamp 并留痕。"""
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or len(raw_items) != len(RUBRIC):
        raise ValueError("items 必须包含完整且唯一的三项")
    by_key: dict[str, dict] = {}
    for entry in raw_items:
        # 重复或未知 key 不能覆盖另一项，也不能参与合计。
        if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
            raise ValueError("item 必须是带字符串 key 的对象")
        key = entry["key"]
        if key not in {row[0] for row in RUBRIC} or key in by_key:
            raise ValueError(f"未知或重复 key:{key}")
        by_key[key] = entry
    items, reasons = [], []
    # 固定遍历顺序，模型改变 items 排列也不影响调用者和波动比较。
    for key, label, maximum in RUBRIC:
        entry = by_key[key]
        score = entry.get("score")
        # bool 虽是 int 子类，但不代表合法分数；字符串、浮点、NaN 同样拒绝。
        if type(score) is not int:
            raise ValueError(f"分数必须为整数:{key}")
        clamped = max(0, min(score, maximum))
        # 修正必须留原因，由统一状态判断触发人工复核。
        if clamped != score:
            reasons.append(f"分数越界已clamp:{key}:{score}->{clamped}")
        evidence = []
        for field in ("jd_evidence", "resume_evidence"):
            value = entry.get(field)
            # 长度按去空白检查；纯标点也不能伪装成四字证据。
            if (not isinstance(value, str) or len("".join(value.split())) < 4
                    or not _normalize_evidence(value)):
                raise ValueError(f"证据不足4字:{key}.{field}")
            evidence.append(value)
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"评分理由为空:{key}")
        items.append(LlmItem(key, label, clamped, maximum, *evidence, reason))
    # suggestions 结构也要验证，禁止把对象或裸字符串当作建议数组透传。
    suggestions = payload.get("suggestions")
    if not isinstance(suggestions, list) or any(not isinstance(s, str) for s in suggestions):
        raise ValueError("suggestions 必须为字符串数组")
    return items, reasons


async def score_llm(resume: ResumeInfo, job: JobInfo, resume_text: str,
                    hard: HardScore, semantic: SemanticScore) -> LlmScore:
    """按 §三①～⑦调用、解析、校验证据、过滤建议并合计；失败零分。"""
    # ① 只传业务材料；抽取与已有评分是上下文，不能作为双证据的来源。
    context = {"resume_summary": {"years": resume.years, "skills": resume.skills},
               "hard": {"score": hard.score, "breakdown": hard.breakdown},
               "semantic": {"score": semantic.score, "uncovered": semantic.uncovered}}
    user_prompt = ("请按system中的rubric复核；摘要不作证据，不得修改已有评分。\n"
                   f"简历原文（至多{MAX_RESUME_CHARS}字）：\n{resume_text[:MAX_RESUME_CHARS]}\n"
                   f"JD八字段（只引用字段值原句）：\n{job.model_dump_json()}\n"
                   f"上下文摘要：{json.dumps(context, ensure_ascii=False)}\n"
                   "rubric上限：stack_depth=12，domain_fit=10，proof_strength=8。")
    # ② 降级链内部固定 temperature=0，此处不重复传递、不另建客户端。
    try:
        text, meter = await call_with_fallback_async(user_prompt, system=SYSTEM_MATCH_RUBRIC)
    except Exception as exc:
        # 全链失败不提供 usage，只报告已知费用 0，不推测或伪造账单。
        return _failed(f"LLM调用失败:{type(exc).__name__}", 0.0)
    cost_yuan = meter.cost()
    # 先取费用再解析，保证 JSON 失败和字段失败也准确保留已知消耗。
    # ③ 仅兼容整段围栏；解释文字或残缺 JSON 不靠截取花括号来猜。
    try:
        cleaned = text.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL | re.IGNORECASE)
        payload = json.loads(fenced.group(1) if fenced else cleaned)
    except (AttributeError, TypeError, ValueError) as exc:
        return _failed(f"JSON解析失败:{exc}", cost_yuan)
    # ④ 任一必要字段非法就整体失败，尤其不允许证据为空仍留下正分。
    try:
        if not isinstance(payload, dict):
            raise ValueError("顶层必须为JSON对象")
        items, reasons = _validate_items(payload)
    except ValueError as exc:
        return _failed(f"子项校验失败:{exc}", cost_yuan)
    # ⑤ 只放宽空白、标点；找不到引文保留数据供复核，不直接判 failed。
    jd_text = _job_text(job)
    sources = {"jd_evidence": _normalize_evidence(jd_text),
               "resume_evidence": _normalize_evidence(resume_text)}
    # 验证来源严格按字段对应，JD 证据不能拿简历里的同一句来兜底。
    for item in items:
        for field, source in sources.items():
            if _normalize_evidence(getattr(item, field)) not in source:
                reasons.append(f"证据未在原文中找到:{item.key}.{field}")
    # ⑥ 无论模型是否宣称建议安全，交付前都必须经过纯函数过滤。
    suggestions, dropped = filter_suggestions(payload["suggestions"], resume_text, jd_text)
    # ⑦ 忽略模型自报的 total、score、档位等额外字段；分数唯一来源是三项之和。
    score = sum(item.score for item in items)
    assert 0 <= score <= 30
    return LlmScore(score, items, suggestions, dropped,
                    "needs_review" if reasons else "ok", reasons, cost_yuan)
