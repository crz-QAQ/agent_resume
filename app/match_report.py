"""按 S6 汇总既有评分、原文证据与建议，不产生任何新增扣分。"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from time import perf_counter
from typing import TYPE_CHECKING

from app.prompts import PROMPT_VERSION
from app.schemas_match import Deduction, JobInfo, MatchReport, MatchScore
from app.schemas_match import ResumeHighlight, ResumeKeyword, SemanticItem

# 仅依赖结构化输入；导入报告模块不初始化客户端、不加载向量权重。
if TYPE_CHECKING:
    from app.schemas import ResumeInfo
    from app.scoring_llm import LlmScore
    from app.scoring_rules import HardScore
    from app.scoring_semantic import SemanticScore


# 档位阈值来自 PRD §2.6，由 S6 §一明确固定；模型无权设置档位。
VERDICT_HIGH = 85
VERDICT_MATCH = 70
VERDICT_WEAK = 55
# S6 §三.3 的封闭类别表，不从模型或岗位方向扩展分类。
_CATEGORY_SKILLS = {
    "语言": "Go Python Java PHP C++ C# JavaScript TypeScript Rust".split(),
    "框架": "Spring Gin Django Flask FastAPI LangChain Vue React".split(),
    "中间件": "Kafka RabbitMQ Redis Nginx".split(),
    "数据库": "MySQL PostgreSQL MongoDB Elasticsearch SQLite".split(),
    "云": "AWS 阿里云 腾讯云 Docker Kubernetes".split(),
    "工具": "Git Linux CI/CD Jenkins".split(),
}
# 技能名保持简历抽取值，映射和次数匹配均忽略大小写。
_CATEGORIES = {skill.casefold(): category for category, skills in _CATEGORY_SKILLS.items()
               for skill in skills}
# 四类不符项只搬运硬分；missing_skill 单独进入零扣分缺口。
_MISMATCH_TYPES = {"years_short", "years_unknown", "education", "education_unknown"}


def _verdict(total: int) -> str:
    """只根据代码阈值将百分制总分映射成四个档位。"""
    # 从高档往低档判断，边界分数归入含等号的一侧。
    if total >= VERDICT_HIGH:
        return "高度匹配"
    if total >= VERDICT_MATCH:
        return "匹配"
    if total >= VERDICT_WEAK:
        return "勉强"
    return "不匹配"


def _keywords(skills: list[str], resume_text: str) -> list[ResumeKeyword]:
    """逐技能统计原文非重叠出现次数，按固定表分类并保留首次顺序。"""
    # 未在正文出现的抽取技能仍如实记为零，不补造出现次数。
    result, seen = [], set()
    folded_text = resume_text.casefold()
    for skill in skills:
        name = skill.strip()
        key = name.casefold()
        # 空技能无法计数；同一技能不同大小写不重复展示。
        if not name or key in seen:
            continue
        seen.add(key)
        result.append(ResumeKeyword(name=name, category=_CATEGORIES.get(key, "其他"),
                                    count=folded_text.count(key)))
    return result


def _recent_company(resume_text: str) -> str | None:
    """优先读明确的最近公司标签，否则按任职起始日期取可核实公司。"""
    # 所有返回值均来自原文片段，不通过公司词典或模型补全名称。
    candidates = []
    undated = []
    for line in resume_text.splitlines():
        # 明确的当前公司标签比日期排序更可靠。
        explicit = re.search(r"(?:最近|当前|现任)公司\s*[:：]\s*([^|｜；;]+)", line)
        if explicit:
            return explicit.group(1).strip() or None
        labeled = re.search(r"(?:公司|工作单位)\s*[:：]\s*([^|｜；;]+)", line)
        # 日期履历行需要明确的公司后缀，普通技术栈不当作雇主。
        company = re.search(r"[\w\u4e00-\u9fff（）()·]+?(?:有限公司|公司|集团|工作室)", line)
        # 普通叙述里提到客户公司不足以证明任职，只接受标签或带日期的履历行。
        date = re.search(r"((?:19|20)\d{2})(?:[./年-](\d{1,2}))?", line)
        value = labeled.group(1).strip() if labeled else company.group(0) if company else None
        if value and date:
            candidates.append(((int(date.group(1)), int(date.group(2) or 0)), value))
        elif labeled and value:
            undated.append(value)
    # 没有日期且有多个不同公司时无法确定最近一份，不猜排序。
    if candidates:
        return max(candidates, key=lambda entry: entry[0])[1]
    return undated[0] if len(set(undated)) == 1 else None


def _highlights(resume: ResumeInfo, resume_text: str) -> list[ResumeHighlight]:
    """仅从简历原文提取可核实的主要项；缺失字段直接省略。"""
    # 不把 None、未知或推测内容渲染成候选人的事实。
    result = []
    if resume.name and resume.name in resume_text:
        result.append(ResumeHighlight(label="姓名", value=resume.name))
    # 年限保留实际措辞；不从工作起止日期自行估算新的年限。
    number = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百]+)\s*年"
    years = re.search(rf"{number}\s*(?:工作|从业)?\s*(?:经验|经历)|(?:工作|工龄|从业)\s*{number}", resume_text)
    if years:
        result.append(ResumeHighlight(label="工作年限", value=years.group(0)))
    # 同一简历多个学历时取最高的原文字词，不从结构化年限推测学历。
    education = next((word for word in ("博士", "硕士", "研究生", "本科", "学士", "大专", "专科", "高中", "中专")
                      if word in resume_text), None)
    if education:
        result.append(ResumeHighlight(label="学历", value=education))
    company = _recent_company(resume_text)
    if company:
        result.append(ResumeHighlight(label="最近公司", value=company))
    # 最多取前五项，且每项确实在原文出现，避免把抽取幻觉写成亮点。
    stack = [skill for skill in resume.skills[:5] if skill.strip()
             and skill.casefold() in resume_text.casefold()]
    if stack:
        result.append(ResumeHighlight(label="技术栈", value=" / ".join(stack)))
    achievements = []
    # 中文数字量化句同样有效，例如“从两秒降至一百毫秒”。
    for part in re.split(r"[。！？；\n]", resume_text):
        sentence = part.strip()
        numeric = re.search(r"\d|[零〇一二两三四五六七八九十百千万亿]+\s*(?:毫秒|秒|倍|个|项|人|元|%)", sentence)
        # 成果句还需包含动作，避免把电话、日期、年龄当成量化成果。
        if numeric and re.search(r"完成|优化|提升|降低|缩短|增长|减少|降至|提高|节省|上线|交付|处理|支撑|达到|实现", sentence):
            if sentence not in achievements:
                achievements.append(sentence)
    for sentence in achievements[:2]:
        # 两条成果分开展示，便于逐句回查原文。
        result.append(ResumeHighlight(label="成果亮点", value=sentence))
    return result


def _missing(job: JobInfo, jd_text: str, hard: HardScore,
             sem: SemanticScore) -> tuple[list[Deduction], list[str]]:
    """合并语义未覆盖和硬分未命中技能；只交付能落到 JD 原文的缺口。"""
    candidates, reasons = [], []
    # 候选集合只有两个指定来源，不读取任何 LLM 项的 reason。
    for item in sem.uncovered:
        # 空职责的防御说明不是 JD 要求，不能伪装成缺失职责。
        if item not in job.responsibilities:
            reasons.append(f"语义未覆盖项不是JD职责：{item}")
            continue
        candidates.append((item, "medium"))
    for deduction in hard.deductions:
        if deduction.type == "missing_skill":
            # S3 的 jd_evidence 即技能名；不把“必备技能：”标签写进缺口正文。
            candidates.append((deduction.jd_evidence, deduction.severity))
    result, seen = [], set()
    # 去重保留职责在先、技能在后的稳定顺序。
    for item, severity in candidates:
        key = " ".join(item.split()).casefold()
        if not key or key in seen:
            continue
        evidence = re.search(re.escape(item), jd_text, re.IGNORECASE)
        # 不重新编写证据：缺失原文时留复核原因，分数和扣分都不受影响。
        if not evidence:
            reasons.append(f"缺口缺少JD原文依据：{item}")
            continue
        seen.add(key)
        # 缺口只是报告信息，deduct 始终为零。
        result.append(Deduction(item=item, type="missing", deduct=0,
                                jd_evidence=evidence.group(0),
                                resume_evidence="简历未体现", severity=severity))
    return result, reasons


def _aggregate_status(hard: HardScore, sem: SemanticScore, llm: LlmScore,
                      report_reasons: list[str]) -> tuple[str, list[str]]:
    """聚合三项状态并标明原因来源；failed 优先于 needs_review。"""
    statuses, reasons = [], []
    # 每个来源分别加前缀，避免同一句原因无法确定来自哪个阶段。
    for name, result in (("hard", hard), ("semantic", sem), ("llm", llm)):
        # S3/S4 目前无状态字段，正常返回即 ok；不为此改动其算法或结构。
        status = getattr(result, "status", "ok")
        statuses.append(status)
        stage_reasons = getattr(result, "reasons", [])
        reasons.extend(f"{name}：{reason}" for reason in stage_reasons)
        if status in {"failed", "needs_review"} and not stage_reasons:
            reasons.append(f"{name}：{status}")
    reasons.extend(report_reasons)
    # 状态不影响总分，不因 failed 再扣分或归零其他阶段。
    if "failed" in statuses:
        return "failed", list(dict.fromkeys(reasons))
    if "needs_review" in statuses or report_reasons:
        return "needs_review", list(dict.fromkeys(reasons))
    return "ok", list(dict.fromkeys(reasons))


def build_report(*, resume: ResumeInfo, job: JobInfo, resume_text: str, jd_text: str,
                 hard: HardScore, sem: SemanticScore, llm: LlmScore,
                 meta: dict | None = None) -> MatchReport:
    """按 S6 十节构造报告；meta 可提供实际模型和全链路 elapsed_ms。"""
    started = perf_counter()
    # ①② 只加既有三项分数，模型的 total/verdict 等额外字段一律不读取。
    total = hard.score + sem.score + llm.score
    if not 0 <= total <= 100:
        raise ValueError(f"三项相加的总分必须在0–100：{total}")
    score = MatchScore(total=total, breakdown={
        "hard": hard.score, "semantic": sem.score, "llm": llm.score,
        "hard_detail": hard.breakdown, "semantic_detail": sem.score,
        "llm_detail": {item.key: item.score for item in llm.items},
    })
    # ③④ 关键字与亮点只读简历字段及原文，不额外调用模型。
    keywords = _keywords(resume.skills, resume_text)
    highlights = _highlights(resume, resume_text)
    # ⑤⑥⑦ deductions 原样传递；缺口为零扣分，不符项保留原对象。
    missing, report_reasons = _missing(job, jd_text, hard, sem)
    mismatches = [item for item in hard.deductions if item.type in _MISMATCH_TYPES]
    # ⑧ 缺口模板只引用 m.item，不采用模型 reason 或 dropped_suggestions。
    suggestions = [f"[LLM] {suggestion}" for suggestion in llm.suggestions]
    suggestions += [f"[缺口] 补充「{item.item}」相关经历或把已有经验改成可对应的说法"
                    for item in missing]
    suggestions = list(dict.fromkeys(suggestions))[:10]
    # ⑨ 保留调用者扩展元数据，但分数来源、费用、版本与时间由代码写回。
    metadata = dict(meta or {})
    # 硬分与本地语义分当前无费用属性，按已知零成本与 LLM 费用相加。
    metadata.update({
        # 上游未报告实际模型时明确未知，避免把默认厂商冒充实际降级结果。
        "model": metadata.get("model") or getattr(llm, "model", None) or "unknown",
        "prompt_version": PROMPT_VERSION,
        "cost_yuan": sum(getattr(result, "cost_yuan", 0.0) for result in (hard, sem, llm)),
        "elapsed_ms": metadata.get("elapsed_ms", round((perf_counter() - started) * 1000)),
        "resume_chars": len(resume_text), "jd_chars": len(jd_text),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    })
    # ⑩ 即使阶段 failed，仍照常交付三项之和及带来源的失败原因。
    status, reasons = _aggregate_status(hard, sem, llm, report_reasons)
    # S7a / PRD §三：只复制 S4.5 的明细，不重算余弦、重排或覆盖状态。
    semantic_items = [SemanticItem(item=item.item, best_sim=item.best_sim,
                                   rerank_score=item.rerank_score,
                                   best_sentence=item.best_sentence, covered=item.covered)
                      for item in sem.per_item]
    return MatchReport(score=score, verdict=_verdict(total), keywords=keywords,
                       highlights=highlights, deductions=hard.deductions, missing=missing,
                       mismatches=mismatches, suggestions=suggestions, meta=metadata,
                       status=status, reasons=reasons, semantic_items=semantic_items)
