"""离线运行 M1 的十二组检查，包含证据、同源技能口径、确定性与规范检查。"""

from __future__ import annotations

import ast
from datetime import date
import io
from pathlib import Path
import sys
from time import perf_counter
import tokenize
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

# 导入业务模块之前开始计时；不导入任何解析器或客户端模块。
STARTED = perf_counter()
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.dont_write_bytecode = True

from app.guard_revision import find_original
import app.resume_diagnose as module
from app.resume_diagnose import diagnose
from app.schemas_match import JobInfo
from app.schemas_revision import DiagnosisFinding, DiagnosisReport, KwCoverage
import app.scoring_rules as scoring

# 复用既有字段契约，运行时禁止加载会读 .env 的 ResumeInfo 模块。
if TYPE_CHECKING:
    from app.schemas import ResumeInfo

# 独立期望逐字来自 §6.3，不能直接把待测规则表当作正确答案。
CODES = ("no_quantify", "weak_verb", "timeline_overlap", "timeline_invalid",
         "open_ended", "relative_time", "missing_section", "skill_flat")
REPORTS: list[DiagnosisReport] = []


def _equal(label: str, expected: object, actual: object) -> None:
    """比较双方；参数为条目、期望和实际，返回无，不等抛 AssertionError。"""
    # 保留 repr 可区分 False、None、空串以及带空白的原文证据。
    assert actual == expected, f"哪条={label}；期望={expected!r}；实际={actual!r}"


def _resume(name: str | None = "张三", years: float | None = 3, skills: tuple[str, ...] = ("Go",)) -> ResumeInfo:
    """构造离线输入；参数对应原模型三字段，返回同字段对象，异常无。"""
    # 每次复制列表，不让样例之间共享输入状态；不创建重复的业务模型。
    return cast("ResumeInfo", SimpleNamespace(name=name, years=years, skills=list(skills)))


def _job(skills: tuple[str, ...] = (), duties: tuple[str, ...] = ()) -> JobInfo:
    """构造岗位；参数为必备词与职责，返回既有模型，非法参数抛 ValidationError。"""
    # 未设置年限或学历门槛，不让额外岗位条件干扰缺陷诊断反例。
    return JobInfo(title="后端开发", company=None, required_years_min=None, required_years_max=None,
                   education=None, must_have=list(skills), nice_to_have=[], responsibilities=list(duties))


def _text(body: str, skills: str = "Go", contact: str = "邮箱：zhangsan@example.com") -> str:
    """拼装简历；参数为职责、技能与联系方式，返回测试原文，异常无。"""
    # 其他完整性项始终给齐，使每条核心反例只改变一个检查因素。
    return f"姓名：张三\n工作年限：3 年\n学历：本科\n{contact}\n技能\n{skills}\n工作经历\n{body}"


def _run(label: str, text: str, resume: ResumeInfo | None = None, job: JobInfo | None = None) -> DiagnosisReport:
    """执行真实诊断；参数为条目和输入，返回报告，通用不变量失败抛 AssertionError。"""
    resume = resume if resume is not None else _resume()
    job = job if job is not None else _job()
    before = (resume.name, resume.years, list(resume.skills), job.model_dump())
    report = diagnose(text, resume, job)
    # 每次调用都核对证据与输入不变，不能只在专门的证据样例里检查。
    _equal(f"{label}/证据", [], [item.evidence for item in report.findings if not find_original(text, item.evidence)])
    _equal(f"{label}/code", [], [item.code for item in report.findings if item.code not in CODES])
    _equal(f"{label}/输入不变", before, (resume.name, resume.years, list(resume.skills), job.model_dump()))
    _equal(f"{label}/零成本", 0, report.meta["cost_yuan"])
    _equal(f"{label}/原文字数", len(text), report.meta["resume_chars"])
    _equal(f"{label}/发现数量", len(report.findings), report.meta["finding_count"])
    _equal(f"{label}/单次耗时", True, 0 <= report.meta["elapsed_ms"] < 1000)
    # 保存真实输出供最后核对八个 code，不能用手工构造的模型冒充规则命中。
    REPORTS.append(report)
    return report


def _findings(report: DiagnosisReport, code: str) -> list[DiagnosisFinding]:
    """筛选发现；参数为报告和目标 code，返回对应列表，异常无。"""
    # 一条职责可以同时有多个缺陷，检查目标 code 时保留其他规则的正常输出。
    return [item for item in report.findings if item.code == code]


def _check_quantified() -> str:
    """检查①；无参数，返回量化检查摘要，失败抛 AssertionError。"""
    report = _run("已量化", _text("负责接口优化，从 2 秒降至 120 毫秒"))
    _equal("已量化不报 no_quantify", [], _findings(report, "no_quantify"))
    # 量化数字必须来自同一个职责句，不能借简历其他位置的日期来通过。
    # 每个量级符号单独覆盖，避免只针对阿拉伯数字实现规则。
    for scale in ("万", "千", "百", "亿", "倍", "%", "％"):
        report = _run(f"量级 {scale}", _text(f"负责接口优化，支撑{scale}级请求"))
        _equal(f"量级 {scale} 不误报", [], _findings(report, "no_quantify"))
    return "2 秒→120 毫秒与七种量级符号均不误报"


def _check_no_quantify() -> str:
    """检查②；无参数，返回无量化与地点摘要，失败抛 AssertionError。"""
    text = _text("2020.01-2023.01 某公司\n负责后端开发")
    findings = _findings(_run("未量化", text), "no_quantify")
    # 时间行上的数字不能为下一行职责充当量化事实。
    _equal("无量化职责", [("medium", "工作经历 · 第 2 段", "负责后端开发")],
           [(item.severity, item.location, item.evidence) for item in findings])
    long_sentence = "负责" + "接口功能开发" * 30
    # 长句保留首 120 字，验证截断不会拼出不存在的证据。
    finding = _findings(_run("长职责", _text(long_sentence)), "no_quantify")[0]
    _equal("证据截断 120 字", long_sentence[:120], finding.evidence)
    # 不检查技能与教育中的普通说明，保持职责句边界。
    report = _run("分节边界", "技能\nGo\n教育\n本科\n工作经历\n负责接口上线 2 次")
    _equal("其他节不报职责缺量化", [], _findings(report, "no_quantify"))
    return "no_quantify=medium；工作经历 · 第 2 段；证据截断仍可检索"


def _check_weak_verb() -> str:
    """检查③；无参数，返回弱动词摘要，失败抛 AssertionError。"""
    # 四种弱动词分别命中；项目符号属于排版，不改变句首判定。
    for verb in ("负责", "参与", "协助", "配合"):
        findings = _findings(_run(verb, _text(f"· {verb}接口开发")), "weak_verb")
        _equal(f"{verb}/弱动词", ["low"], [item.severity for item in findings])
    # 显式结果词要取消弱动词发现，不能对所有「负责」一概报错。
    for result in ("提升", "降低", "上线", "交付", "支撑", "实现", "达成"):
        report = _run(result, _text(f"参与接口开发并{result}目标"))
        _equal(f"{result}/有结果", [], _findings(report, "weak_verb"))
    return "四种弱动词=low；七种结果词避免误报"


def _check_overlap() -> str:
    """检查④；无参数，返回重叠区间摘要，失败抛 AssertionError。"""
    body = "2020.01-2022.06 甲公司\n负责接口交付 2 次\n2021.07-2023.04 乙公司"
    finding = _findings(_run("时间重叠", _text(body)), "timeline_overlap")[0]
    # 重叠说明需同时指出两段，不接受只有笼统「时间有问题」的提示。
    _equal("重叠 high", "high", finding.severity)
    _equal("说明含两段", True, all(part in finding.detail for part in ("第 1 段", "第 3 段", "2020.01-2022.06", "2021.07-2023.04")))
    # 不重叠区间以及教育与任职并行不能产生工作重叠。
    report = _run("无重叠", _text("2019-2020 甲公司\n2021-2022 乙公司"))
    _equal("区间分离", [], _findings(report, "timeline_overlap"))
    report = _run("教育不混入工作", _text("2020-2022 甲公司") + "\n教育\n2019-2024 本科")
    _equal("教育不混入工作", [], _findings(report, "timeline_overlap"))
    return "timeline_overlap=high；指出两段；不混入教育区间"


def _check_invalid() -> str:
    """检查⑤；无参数，返回倒序与未来时间摘要，失败抛 AssertionError。"""
    # 年份倒序与同年月份倒序均检查，防止只比较年份。
    for interval in ("2023-2020", "2023.10-2023.02"):
        findings = _findings(_run(interval, _text(interval + " 某公司")), "timeline_invalid")
        _equal(f"倒序 {interval}", [("high", interval)], [(item.severity, item.evidence) for item in findings])
    future = date.today().year + 1
    # 动态生成未来年份，避免检查脚本跨年后把固定年份反例误判成合法。
    report = _run("未来年份", _text(f"2020-{future} 某公司"))
    _equal("结束年份晚于当前年", 1, len(_findings(report, "timeline_invalid")))
    return "timeline_invalid=high；年份倒序/月序倒序/未来结束年均覆盖"


def _check_open_ended() -> str:
    """检查⑥；无参数，返回开放时间摘要，失败抛 AssertionError。"""
    # 任务书原样例之外，另外两种开放措辞同样需人工确认。
    for ending in ("至今", "现在", "目前"):
        # 开放结束时间仍是原文，不能在证据里擅自替换成当前年月。
        report = _run(ending, _text(f"2021.07-{ending} 某公司"))
        _equal(f"{ending}/开放时间", ["medium"], [item.severity for item in _findings(report, "open_ended")])
    return "2021.07-至今及现在/目前均为 open_ended=medium"


def _check_relative_time() -> str:
    """检查⑦；无参数，返回相对时间摘要，失败抛 AssertionError。"""
    for word in ("去年", "今年", "最近", "近期", "应届"):
        # 逐个词运行，确认没有只针对任务书的「去年」写特殊分支。
        report = _run(word, _text(f"{word}离职"))
        _equal(f"{word}/相对时间", ["medium"], [item.severity for item in _findings(report, "relative_time")])
    # 使用绝对年份不应额外产生相对时间风险。
    _equal("绝对时间", [], _findings(_run("绝对时间", _text("2024 年离职")), "relative_time"))
    return "五种相对时间=medium；绝对年份不误报"


def _check_missing() -> str:
    """检查⑧；无参数，返回完整性与悬空证据摘要，失败抛 AssertionError。"""
    report = _run("缺联系方式", _text("负责接口交付 2 次", contact=""))
    _equal("contact=False", False, report.completeness["contact"])
    # 其他四项完整，才能准确确认这条 missing_section 指向联系方式。
    _equal("missing_section=high", ["high"], [item.severity for item in _findings(report, "missing_section")])
    # 手机单独有效；不是把缺邮箱错误理解成必须同时具备两种联系方式。
    report = _run("仅手机", _text("负责接口交付 2 次", contact="手机：13800138000"))
    _equal("手机可联系", True, report.completeness["contact"])
    report = _run("正则兜底", _text("负责接口交付 2 次"), _resume(None, None, ()))
    # 结构化漏抽不等于原文缺失；诊断不得覆盖调用方的结构字段。
    _equal("正则兜底五项", dict(name=True, years=True, education=True, skills=True, contact=True), report.completeness)
    # 空原文没有证据，五项缺失仍由 M0 统一过滤并准确记入 dropped。
    report = _run("空原文", "", _resume(None, None, ()))
    _equal("悬空条目全部丢弃", ([], 5), (report.findings, report.meta["dropped"]))
    # 故障注入一条伪证据，确认 diagnose 的最终出口确实执行 M0 校验。
    forged = DiagnosisFinding(code="no_quantify", severity="medium", location="其他 · 第 1 段", evidence="不存在的句子", detail="注入反例")
    with patch.object(module, "_candidate_findings", return_value=[forged]):
        # 只替换候选生成，最终出口的 M0 检索函数仍运行真实实现。
        report = _run("伪证据注入", _text("负责接口交付 2 次"))
    _equal("伪证据被拒", ([], 1), (report.findings, report.meta["dropped"]))
    return "missing_section=high；手机/邮箱正则；空原文 dropped=5、伪证据 dropped=1"


def _check_flat() -> str:
    """检查⑨；无参数，返回方向词同源摘要，失败抛 AssertionError。"""
    report = _run("仅岗位方向", _text("负责接口交付 2 次", skills="后端、开发"))
    _equal("skill_flat=medium", ["medium"], [item.severity for item in _findings(report, "skill_flat")])
    # 身份检查确保 import 共享原集合，不是值碰巧相同的复制品。
    _equal("方向词对象同源", True, module.ROLE_WORDS is scoring.ROLE_WORDS)
    _equal("族表对象同源", True, module.SKILL_FAMILIES is scoring.SKILL_FAMILIES)
    # 排序仅使测试输入固定，不改变共享集合，也不在本地保存其副本作词表。
    report = _run("全部共享方向词", _text("负责接口交付 2 次", skills="、".join(sorted(scoring.ROLE_WORDS))))
    _equal("全方向仍空泛", 1, len(_findings(report, "skill_flat")))
    # 存在具体技术即不满足「只有岗位方向词」的严格条件。
    report = _run("方向和技术", _text("负责接口交付 2 次", skills="后端、开发、Go"))
    _equal("有技术不空泛", [], _findings(report, "skill_flat"))
    # 技术换行书写仍属于同一技能节，不能把上一行方向词单独误报。
    report = _run("多行技能", _text("负责接口交付 2 次", skills="后端\nGo"))
    _equal("多行技能不空泛", [], _findings(report, "skill_flat"))
    return "skill_flat=medium；方向词与族表对象直接复用评分模块"


def _check_ats() -> str:
    """检查⑩；无参数，返回次数、位置、来源与去重摘要，失败抛 AssertionError。"""
    job = _job(("Go", "MySQL", "Go"), ("使用 Redis 与 Go 开发接口",))
    report = _run("ATS", _text("使用 Go 开发接口。\n通过 gO 支撑业务。"), job=job)
    coverage = {item.term: item for item in report.keyword_coverage}
    # 必备列表中的重复 Go 与职责中的 Go 最终只能有一行，并保留必备标记。
    _equal("关键词顺序与去重", ["Go", "MySQL", "Redis"], list(coverage))
    _equal("Go 次数与位置", (3, ["技能", "工作经历"], True),
           (coverage["Go"].count_in_resume, coverage["Go"].sections, coverage["Go"].from_must_have))
    _equal("缺 MySQL 与 Redis", ["MySQL", "Redis"], report.missing_keywords)
    # 职责来源单独保留 False，不能把所有英语关键词都声称为 JD 必备技能。
    _equal("职责词来源", False, coverage["Redis"].from_must_have)
    # 跨行去空白计数时仍需定位到正确的节，不能出现 count>0 但无位置。
    report = _run("跨行 ATS", _text("负责接口交付 2 次", skills="Re\ndis"), job=_job(("Redis",)))
    _equal("跨行词覆盖", (1, ["技能"]), (report.keyword_coverage[0].count_in_resume, report.keyword_coverage[0].sections))
    # 非 ASCII 字母小写展开与带空白的行内标题不能破坏关键词位置映射。
    report = _run("Unicode 与标题", "技能 ：İ Go", job=_job(("Go",)))
    _equal("Unicode 定位", (1, ["技能"]), (report.keyword_coverage[0].count_in_resume, report.keyword_coverage[0].sections))
    # 技能别名只从评分模块取，归一口径应与评分一致。
    report = _run("别名 ATS", _text("负责接口交付 2 次", skills="JavaScript、PostgreSQL"), job=_job(("JS", "Postgres")))
    _equal("别名 ATS 次数", [1, 1], [item.count_in_resume for item in report.keyword_coverage])
    return "Go=3、技能/工作经历；MySQL 缺失；职责词来源、别名、跨行定位正确"


def _check_family() -> str:
    """检查⑪；无参数，返回族内覆盖与真实缺口摘要，失败抛 AssertionError。"""
    report = _run("DeepSeek vs Qwen", _text("负责接口交付 2 次", skills="Qwen"), job=_job(("DeepSeek",)))
    _equal("同族不缺", (1, []), (report.keyword_coverage[0].count_in_resume, report.missing_keywords))
    # 遍历评分模块每个族，以真实成员作交叉命中，不在测试里复制一套族表。
    for family, members in scoring.SKILL_FAMILIES.items():
        # 输入族成员从原模块动态读取，新增共享成员时也不会维护两份测试词表。
        first, second = sorted(members)[:2]
        report = _run(f"同族 {family}", _text("负责接口交付 2 次", skills=second), job=_job((first,)))
        _equal(f"族 {family}/覆盖", [], report.missing_keywords)
    # 族内重叠字符串只计一次，例如 ChatGPT 不应再额外算一个 GPT。
    report = _run("族内计数", _text("负责接口交付 2 次", skills="ChatGPT GPT"), job=_job(("DeepSeek",)))
    _equal("族内不重复计数", 2, report.keyword_coverage[0].count_in_resume)
    report = _run("泛称不足", _text("负责接口交付 2 次", skills="LLM"), job=_job(("LLM 产品 API",)))
    # 上位概念属于族入口，但泛称本身不能替代实际使用过某成员的证据。
    _equal("泛称不冒充具体成员", ["LLM 产品 API"], report.missing_keywords)
    report = _run("Go 真缺口", _text("负责接口交付 2 次", skills="Python"), job=_job(("Go",)))
    _equal("非同族不放宽", ["Go"], report.missing_keywords)
    return "DeepSeek←Qwen 覆盖；四族交叉命中；泛称/非同族不放宽"


def _quality() -> str:
    """核对契约与规范；无参数，返回统计摘要，失败抛 AssertionError 或文件异常。"""
    expected = {
        KwCoverage: dict(term=str, count_in_resume=int, sections=list[str], from_must_have=bool),
        DiagnosisFinding: dict(code=str, severity=str, location=str, evidence=str, detail=str),
        DiagnosisReport: dict(findings=list[DiagnosisFinding], keyword_coverage=list[KwCoverage],
                              missing_keywords=list[str], completeness=dict[str, bool], meta=dict),
    }
    # PRD 内部契约逐字段检查，且必须明确标注不进入对外 API。
    for model, fields in expected.items():
        _equal(f"{model.__name__}/字段类型", fields, {name: field.annotation for name, field in model.model_fields.items()})
        _equal(f"{model.__name__}/内部说明", True, "内部结构，不进对外 API" in model.__doc__)
    ratios = []
    for relative in ("app/schemas_revision.py", "app/resume_diagnose.py", "tools/check_resume_diagnose.py"):
        # 空行同样纳入分母，字符串中的井号不会被当作注释。
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        comments = {token.start[0] for token in tokenize.generate_tokens(io.StringIO(source).readline)
                    if token.type == tokenize.COMMENT}
        ratio = len(comments) / len(source.splitlines())
        tree = ast.parse(source)
        nodes = [node for node in ast.walk(tree) if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
        _equal(f"{relative}/注释≥15%（{ratio:.1%}）", True, ratio >= 0.15)
        _equal(f"{relative}/docstring 100%", [], [getattr(node, "name", "模块") for node in nodes if not ast.get_docstring(node)])
        ratios.append(f"{Path(relative).name} {ratio:.1%}")
    # 既有模型客户端不能被本次离线检查意外导入。
    _equal("无模型或环境模块", [], [name for name in ("app.schemas", "app.agent", "app.providers", "app.fallback", "openai", "dotenv") if name in sys.modules])
    return "；".join(ratios) + "；docstring 100%"


def _check_determinism() -> str:
    """检查⑫；无参数，返回确定性、上限、排序与规范摘要，失败抛 AssertionError。"""
    text = _text("2020-2023 甲公司\n负责接口开发\n2022-2024 乙公司\n去年参与业务交付")
    results = [_run("确定性", text, job=_job(("Go", "MySQL"))) for _ in range(3)]
    # 耗时为观测元数据；全部业务字段必须一致，不仅比较 code 与地点。
    snapshots = []
    for report in results:
        # 只移除真实耗时，其他 meta 项也要参与三次确定性比较。
        snapshot = report.model_dump()
        snapshot["meta"].pop("elapsed_ms")
        snapshots.append(snapshot)
    _equal("三次业务结果一致", [snapshots[0]] * 3, snapshots)
    order = {code: index for index, code in enumerate(CODES)}
    weights = {"high": 0, "medium": 1, "low": 2}
    ranks = [(weights[item.severity], order[item.code]) for item in results[0].findings]
    _equal("严重度与 code 顺序", sorted(ranks), ranks)
    # 大量职责和超过三十个关键词需截断，并分别留下截断统计。
    report = _run("结果上限", _text("\n".join(["负责接口开发"] * 40)), job=_job(tuple(f"Tech{index}" for index in range(35))))
    _equal("发现/关键词上限", (20, 30), (len(report.findings), len(report.keyword_coverage)))
    _equal("截断统计", (60, 5, 65), (report.meta["truncated_findings"], report.meta["truncated_keywords"], report.meta["truncated"]))
    # 聚合十二组实际运行结果，保证八个 code 都有被真实触发的用例。
    _equal("八个 code 全覆盖", set(CODES), {finding.code for report in REPORTS for finding in report.findings})
    return "三次业务结果完全一致；排序/20条/30词/丢弃均正确；" + _quality()


def main() -> int:
    """运行十二组；无参数，返回退出码 0/1，每组异常打印双方状态后继续。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    REPORTS.clear()
    # 用例分组顺序与 M1 任务书 §二 ①～⑫ 一致，便于逐项复核。
    groups = (("① 已量化", _check_quantified), ("② 无量化", _check_no_quantify),
              ("③ 弱动词", _check_weak_verb), ("④ 时间重叠", _check_overlap),
              ("⑤ 无效时间", _check_invalid), ("⑥ 开放时间", _check_open_ended),
              ("⑦ 相对时间", _check_relative_time), ("⑧ 缺项与证据", _check_missing),
              ("⑨ 技能空泛", _check_flat), ("⑩ ATS", _check_ats),
              ("⑪ 族内命中", _check_family), ("⑫ 确定性与规范", _check_determinism))
    passed = 0
    # 只在整组断言通过后计数，单组失败不影响其余组执行。
    for label, check in groups:
        try:
            detail = check()
        except Exception as exc:
            print(f"{label}：失败；哪条={label}；期望=本组全部断言通过；实际={type(exc).__name__}: {exc}")
        else:
            passed += 1
            print(f"{label}：通过；{detail}")
    elapsed = perf_counter() - STARTED
    # 汇总耗时覆盖模型声明加载、全部样例以及契约和源码规范检查。
    print(f"耗时 {elapsed:.6f} 秒（含业务模块导入）；零模型、零网络")
    if elapsed >= 1:
        print(f"失败；哪条=耗时；期望=<1 秒；实际={elapsed:.6f} 秒")
    # 末行固定格式；耗时或规范不达标同样返回失败退出码。
    print(f"通过 {passed}/{len(groups)}")
    return 0 if passed == len(groups) and elapsed < 1 else 1


if __name__ == "__main__":
    # 返回非零退出码，让命令行调用者能可靠识别断言、规范或性能失败。
    raise SystemExit(main())
