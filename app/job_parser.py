"""把 JD 原文抽成八字段契约，并用纯代码护栏区分失败与待复核。

复用已有降级链和成本统计，避免为 S1 另建模型调用路径。
保留原始 JD 作为后续证据来源；本模块只负责抽取，不计算匹配或分数。
"""

from dataclasses import dataclass
import re

from app.fallback import call_with_fallback_async
from app.prompts import SYSTEM_JOB
from app.schemas_match import JobInfo
from app.scoring_rules import ROLE_WORDS


# S1 方向词原样移至纯规则模块共享，避免 M1 为取词表加载客户端。
# 只核对原文是否写了年数，不在代码里反推模型应当给出的上下限。
# 同时接受小数与中文年数，避免把「三年」误判成原文未提年限。
_YEAR_IN_TEXT = re.compile(r"(?:\d+(?:\.\d+)?|[零一二三四五六七八九十百两半]+)\s*(?:个\s*)?年")


class JobParseError(ValueError):
    """表示模型调用或结构解析失败，并携带已知的实际调用成本。"""

    def __init__(self, message: str, cost_yuan: float = 0.0) -> None:
        """保存失败原因与已发生的成本。

        参数：message 为原因，cost_yuan 为降级链已报告的费用。
        返回：无；异常：无。
        """
        super().__init__(message)
        # JSON 错误也已消耗 tokens，不能在 failed 分支把费用归零。
        self.cost_yuan = cost_yuan


@dataclass
class JobParseResult:
    """汇总抽取状态、数据、复核原因、成本和可追溯的原文。"""

    # 状态由代码判定，不接受模型自行声称抽取成功。
    status: str
    # failed 没有可信数据；needs_review 保留数据供人工检查。
    data: JobInfo | None
    # 原因面向调用者展示，空列表代表全部护栏通过。
    reasons: list[str]
    # 单位为元，直接沿用 UsageMeter.cost() 的口径。
    cost_yuan: float
    # 必须保留输入原文，不能用模型改写的文本替代证据。
    raw_text: str


async def parse_job(jd_text: str) -> tuple[JobInfo | None, float]:
    """通过已有降级链抽取 JD，并验证完整的八字段结构。

    参数：jd_text 为岗位描述原文。
    返回：(JobInfo, 实际成本元)；成功时不会返回空壳替代解析失败。
    异常：调用失败、空响应、JSON 或字段校验失败均抛 JobParseError。
    """
    try:
        text, meter = await call_with_fallback_async(jd_text, system=SYSTEM_JOB)
    except Exception as exc:
        # 厂商全失败时没有可用 usage，只记录已知成本，不伪造计费数据。
        raise JobParseError(f"JD模型调用失败:{type(exc).__name__}") from exc

    cost_yuan = meter.cost()
    if not isinstance(text, str) or not text.strip():
        raise JobParseError("JD解析失败:模型返回空文本或 None", cost_yuan)

    # 兼容模型偶尔违反输出约定的 Markdown 包裹，但不从说明文字里猜 JSON。
    # 只剥整段代码块；半截 JSON、额外解释仍应明确失败。
    cleaned = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL | re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1)
    try:
        # 所有字段必填、禁止额外字段；{} 或 null 都不能变成默认成功数据。
        data = JobInfo.model_validate_json(cleaned, strict=True)
    except ValueError as exc:
        raise JobParseError(f"JD解析失败:JSON或字段校验失败 ({type(exc).__name__})", cost_yuan) from exc
    return data, cost_yuan


def guard_job(jd_text: str, data: JobInfo) -> list[str]:
    """结合 JD 原文执行三条护栏，完全不调用模型。

    参数：jd_text 为原文，data 为抽取结果；原地剔除方向词与无原文依据的年限。
    返回：待复核原因列表，空列表表示通过。
    异常：无（参数需符合类型契约）。
    """
    reasons: list[str] = []
    skill_reasons: list[str] = []

    # 先过滤再查空壳，防止 must_have 仅含「后端」时被当成有技术要求。
    # 两份列表都要处理，即使原文确实含方向词，也不能将其保留为技能。
    for field_name in ("must_have", "nice_to_have"):
        retained: list[str] = []
        for skill in getattr(data, field_name):
            word = skill.strip()
            if word in ROLE_WORDS:
                reason = f"技能过滤:剔除非技术词 {word}"
                # 同一个方向词出现在两个列表时只记一次，避免重复复核事项。
                if reason not in skill_reasons:
                    skill_reasons.append(reason)
            else:
                retained.append(skill)
        setattr(data, field_name, retained)

    # 规则①来自 S1 §一.3；原文为空也不能因模型幻觉而获得 ok 状态。
    if not jd_text.strip() or (not data.must_have and not data.responsibilities):
        reasons.append("空壳:JD未抽取到要求信息")

    years_min, years_max = data.required_years_min, data.required_years_max
    years = [value for value in (years_min, years_max) if value is not None]
    inverted = years_min is not None and years_max is not None and years_min > years_max
    # 规则②的 0 与 40 来自 S1 年限护栏，显式写 0 年与未写年限是不同事实。
    out_of_range = any(value < 0 or value > 40 for value in years)
    # 原文没写年数时必须保留未知，尤其不能把模型补出的 0 当作真实要求。
    no_year_source = bool(years) and _YEAR_IN_TEXT.search(jd_text) is None
    if inverted or out_of_range or no_year_source:
        reasons.append(f"年限区间异常:min={years_min} max={years_max}")
    if no_year_source:
        # 仅清除无依据的值；对颠倒区间保留原值，便于人工核对。
        data.required_years_min = None
        data.required_years_max = None

    # 先给空壳与区间问题，再列被删除的词，保持三条规则的展示顺序。
    reasons.extend(skill_reasons)
    return reasons


async def parse_and_guard(jd_text: str) -> JobParseResult:
    """依次执行抽取、纯代码护栏并判定状态。

    参数：jd_text 为原始岗位描述。
    返回：failed / needs_review / ok 的 JobParseResult，始终保留原文和已知成本。
    异常：JobParseError 转为 failed；编程错误继续抛出，不伪装成业务状态。
    """
    try:
        data, cost_yuan = await parse_job(jd_text)
    except JobParseError as exc:
        return JobParseResult("failed", None, [str(exc)], exc.cost_yuan, jd_text)

    # 保留显式 None 分支，兼容调用方替身或未来降级链的空返回。
    if data is None:
        return JobParseResult("failed", None, ["JD解析失败:模型返回 None"], cost_yuan, jd_text)

    # 有可解析数据不等于可交付，任一护栏原因都必须进入人工复核状态。
    reasons = guard_job(jd_text, data)
    status = "needs_review" if reasons else "ok"
    return JobParseResult(status, data, reasons, cost_yuan, jd_text)
