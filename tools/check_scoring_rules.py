"""离线断言 S3 原九组及 S3.5 四组规则，失败展示双方值，退出码为 1。"""

from __future__ import annotations

import ast
import io
from pathlib import Path
import sys
from time import perf_counter
import tokenize
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

# 从导入业务模块前计时，覆盖纯数据模型加载与所有断言的执行时间。
STARTED = perf_counter()
# 直接运行或 PyCharm 右键 Run 都使用脚本所在项目，避免依赖当前目录。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.schemas_match import JobInfo
from app.scoring_rules import FAMILY_ALIASES, SKILL_FAMILIES, HardScore, score_hard

# 简历模型模块存在客户端初始化副作用；离线输入只复用其字段契约。
if TYPE_CHECKING:
    from app.schemas import ResumeInfo

# 收集每个样本的实际结果，供第八组逐项复核守恒及证据。
RESULTS: list[tuple[str, HardScore]] = []


def _equal(label: str, expected: object, actual: object) -> None:
    """断言实际等于期望，失败信息包含具体子场景和双方值。"""
    # 保留 repr，才能在失败时看清 None、空串和数字的区别。
    assert actual == expected, f"{label}；期望={expected!r}；实际={actual!r}"


def _resume(years: float | None = None, skills: tuple[str, ...] = ()) -> ResumeInfo:
    """构造与现有简历字段一致的轻量输入，避免导入客户端初始化模块。"""
    # 每次复制技能序列，样本之间不共享可变列表。
    return cast("ResumeInfo", SimpleNamespace(name=None, years=years, skills=list(skills)))


def _job(minimum: float | None = None, maximum: float | None = None,
         education: str | None = None, skills: tuple[str, ...] = ()) -> JobInfo:
    """使用 S1 的真实纯数据模型，构造所有八个必需字段。"""
    # 显式保留 min/max 的 None，不能在测试构造器中提前填成零。
    return JobInfo(title=None, company=None, required_years_min=minimum,
                   required_years_max=maximum, education=education, must_have=list(skills),
                   nice_to_have=[], responsibilities=[])


def _score(label: str, resume: ResumeInfo, job: JobInfo, text: str = "") -> HardScore:
    """执行真实打分并收集结果，所有组（包括第九组）都逐次验证守恒。"""
    # 不用预先编好的分数对象替代业务调用，始终检查实际规则实现。
    result = score_hard(resume, job, text)
    # 此处的检查也覆盖第八组之后执行的别名样本，避免遗漏最后一组。
    _equal(f"{label}/扣分守恒", 40 - result.score, sum(d.deduct for d in result.deductions))
    RESULTS.append((label, result))
    return result


def _check_years_met() -> str:
    """第一组：刚好达标取满分，同时核对下限优先和有效的零年门槛。"""
    # 上限特意设置为 5，错选上限会导致这个刚好达标样本失分。
    result = _score("刚好达标", _resume(3), _job(3, 5), "3 年工作经验")
    _equal("刚好达标/年限分", 15, result.breakdown["years"])
    # 其他两项没有要求，因此任何扣分记录都说明误伤了这个候选人。
    _equal("刚好达标/扣分", [], result.deductions)
    # 下限 0 仍然有效，不能用布尔 or 选成上限 3。
    zero = _score("零年下限", _resume(0), _job(0, 3))
    _equal("零年下限/年限分", 15, zero.breakdown["years"])
    return "years=15；无扣分；min 优先且 0 有效"


def _check_years_near() -> str:
    """第二组：少一年扣七分，小数差值严格小于两年也走该分支。"""
    # 同时断言类型、金额和原文，防止分数正确而解释记录错误。
    result = _score("差一年", _resume(2), _job(3), "2 年工作经验")
    _equal("差一年/年限分", 8, result.breakdown["years"])
    _equal("差一年/扣分", [("years_short", 7)], [(d.type, d.deduct) for d in result.deductions])
    # 原文中的空格与措辞不被结构化数字替换。
    _equal("差一年/原文证据", "2 年工作经验", result.deductions[0].resume_evidence)
    # 1.999 年差值用于区分 <2 和错误的整数截断。
    decimal = _score("差1.999年", _resume(1.001), _job(3), "1.001 年经验")
    _equal("差1.999年/年限分", 8, decimal.breakdown["years"])
    return "years=8；扣 7；小数边界通过"


def _check_years_short() -> str:
    """第三组：明确零年与恰差两年均不达标，但类型仍为已知年限不足。"""
    # 0 是确实零年；必须与下一组的未知 None 分开。
    for years in (0, 1):
        # 两个样本分别覆盖任务书指定差三年和严格差两年的边界。
        result = _score(f"已知{years}年", _resume(years), _job(3), f"{years} 年经验")
        _equal(f"已知{years}年/年限分", 0, result.breakdown["years"])
        _equal(f"已知{years}年/扣分", [("years_short", 15)],
               [(d.type, d.deduct) for d in result.deductions])
    return "years=0；扣 15；差 2 年也为 0"


def _check_years_optional() -> str:
    """第四组：JD 没写年限先给满分；只有上限时确实使用上限作为要求。"""
    # 双方都缺年限的样本必须完全无扣分，而不只是 years 明细正确。
    result = _score("JD无年限", _resume(), _job())
    _equal("JD无年限/年限分", 15, result.breakdown["years"])
    _equal("JD无年限/扣分", [], result.deductions)
    # 双方都未知时先判 JD；不能先对简历的 None 扣分。
    upper = _score("仅有上限", _resume(2), _job(maximum=3), "2 年经验")
    _equal("仅有上限/年限分", 8, upper.breakdown["years"])
    return "JD 未写 years=15；仅 max 时正确回退"


def _check_years_unknown() -> str:
    """第五组：有年限门槛而简历未知时，保留独立类型和明确的缺失证据。"""
    # 原文也不含年限，确保这里验证的是明确的信息缺失分支。
    result = _score("未知年限", _resume(), _job(3), "熟悉 Python")
    _equal("未知年限/年限分", 0, result.breakdown["years"])
    # 一次核对四个字段，确保未知与不足不是仅在展示文案上区分。
    _equal("未知年限/扣分", [("years_unknown", 15, "high", "简历未提及工作年限")],
           [(d.type, d.deduct, d.severity, d.resume_evidence) for d in result.deductions])
    return "years=0；years_unknown；high；扣 15"


def _check_education() -> str:
    """第六组：四个指定学历场景，附同义等级、最高学历和明确豁免。"""
    # 本组的学历来源只有原文，轻量简历输入不提供额外学历字段。
    # 本科或硕士同时出现时取最高等级，不能取字典中的第一次匹配。
    cases = (("本科达标", "本科", "本科毕业", 10, []),
             ("大专不足", "本科", "大专毕业", 0, ["education"]),
             ("JD不限", "不限", "", 10, []),
             ("学历未知", "本科", "熟悉 Python", 0, ["education_unknown"]),
             ("JD无要求", "本科，学历无要求", "", 10, []),
             ("JD未写", None, "", 10, []),
             ("最高学历", "本科或硕士", "本科毕业\n硕士毕业", 10, []),
             ("学士同义", "本科及以上", "学士学位", 10, []),
             ("研究生同义", "硕士", "研究生毕业", 10, []))
    for label, requirement, text, expected, types in cases:
        # 无年限要求、无必备技能，让学历样本不会混入其他扣分。
        result = _score(label, _resume(), _job(education=requirement), text)
        _equal(f"{label}/学历分", expected, result.breakdown["education"])
        _equal(f"{label}/扣分类型", types, [d.type for d in result.deductions])
        # 所有学历扣分均为 high；低学历证据必须来自输入原文。
        if types:
            _equal(f"{label}/严重度", "high", result.deductions[0].severity)
            evidence = text if types == ["education"] else "简历未提及"
            _equal(f"{label}/原文证据", evidence, result.deductions[0].resume_evidence)
    return "本科/大专/不限/未知=10/0/10/0；最高学历及同义词通过"


def _check_skills() -> str:
    """第七组：全中、一半、零中、空要求，并核对原文兜底和余数分配。"""
    required = ("Python", "Redis")
    # 六项一中得 2.5 分：必须变成 3，才能真正识别银行家舍入的差异。
    cases = (("全命中", required, "", required, 15, []),
             ("命中一半", ("Python",), "", required, 8, [7]),
             ("零命中", (), "", required, 0, [8, 7]),
             ("空必备", (), "", (), 15, []),
             ("原文兜底", (), "熟悉 PYTHON、REDIS", required, 15, []),
             ("原文子串", (), "完成 myredis 集成", ("Redis",), 15, []),
             ("五分之三", ("A", "B", "C"), "", ("A", "B", "C", "D", "E"), 9, [3, 3]),
             ("六项一中", ("A",), "", ("A", "B", "C", "D", "E", "F"), 3, [3, 3, 3, 3, 0]),
             ("六项一半", ("A", "B", "C"), "", ("A", "B", "C", "D", "E", "F"), 8, [3, 3, 1]))
    for label, skills, text, must_have, expected, amounts in cases:
        # 两项半命中的 7.5 和六项半命中的 7.5 都应向上取到 8。
        result = _score(label, _resume(skills=skills), _job(skills=must_have), text)
        _equal(f"{label}/技能分", expected, result.breakdown["skills"])
        _equal(f"{label}/逐项扣分", amounts, [d.deduct for d in result.deductions])
        # 未命中每条一笔；不能只生成一笔合并扣分，或丢掉最后的余数。
        _equal(f"{label}/扣分类型", ["missing_skill"] * len(amounts),
               [d.type for d in result.deductions])
        for deduction in result.deductions:
            # JD 证据保留未命中条目的原始名称，而不只输出归一化后的词。
            _equal(f"{label}/技能证据", True, deduction.jd_evidence in must_have)
            _equal(f"{label}/简历证据", "简历未提及", deduction.resume_evidence)
            _equal(f"{label}/严重度", "high", deduction.severity)
    return "全中/一半/零中/空要求=15/8/0/15；原文兜底及余数通过"


def _check_conservation() -> str:
    """第八组：检查已有样本和跨规则组合，逐项复核总分、扣分及证据。"""
    # 同时缺少三种条件，保证守恒不只在单项扣分时成立。
    # 4 个年限状态 × 3 个学历状态 × 3 个技能状态，构成 36 个固定输入。
    for years in (None, 0, 2, 3):
        for education in ("", "大专毕业", "本科毕业"):
            for skills in ((), ("Python",), ("Python", "Redis")):
                _score("组合守恒", _resume(years, skills), _job(3, 5, "本科", ("Python", "Redis")),
                       f"{education}\n" + (f"{years} 年经验" if years is not None else ""))
    for label, result in RESULTS:
        # 使用返回明细重算总分，不能只验证一份内部数字的自洽。
        _equal(f"{label}/总分等于明细", result.score, sum(result.breakdown.values()))
        _equal(f"{label}/扣分加总分", 40, result.score + sum(d.deduct for d in result.deductions))
        _equal(f"{label}/分值范围", True, 0 <= result.score <= 40)
        # 缺失证据必须明确写未提及，不能用空串凑齐字段。
        for deduction in result.deductions:
            _equal(f"{label}/证据非空", True, bool(deduction.jd_evidence and deduction.resume_evidence))
    return "各组及 36 个三项组合均满足 sum(deduct)=40-score，证据齐全"


def _check_aliases() -> str:
    """第九组：核对全部指定别名、双向归一化和技能名内部空白。"""
    # 首个样本逐字对应任务书：must_have=[Go]，skills=[golang]。
    aliases = (("Go", "golang"), ("Go", "go语言"), ("PostgreSQL", "postgres"),
               ("JavaScript", "js"), ("TypeScript", "ts"), ("Kubernetes", "k8s"))
    # 原文留空，防止子串兜底掩盖技能集合的归一化错误。
    for required, skill in aliases:
        for must_have, extracted in ((required, skill), (skill, required)):
            # 交换两侧验证别名表同时应用于 JD 与简历抽取列表。
            result = _score("别名", _resume(skills=(f" {extracted.upper()} ",)), _job(skills=(must_have,)))
            _equal(f"{must_have}/{extracted}/技能分", 15, result.breakdown["skills"])
            _equal(f"{must_have}/{extracted}/扣分", [], result.deductions)
    # 首尾、内部空格以及制表符均在技能名归一化阶段移除。
    spaced = _score("内部空白", _resume(skills=(" G O \t L A N G ",)), _job(skills=(" Go ",)))
    _equal("内部空白/技能分", 15, spaced.breakdown["skills"])
    return "Go/golang 命中；全部别名双向、大小写及内部空白通过"


def _check_family_members() -> str:
    """F1：同族举例只算一项，任一成员及其他三族的替代成员均可命中。"""
    result = _score("F1", _resume(skills=("DeepSeek",)), _job(skills=("DeepSeek", "Qwen", "OpenAI")))
    _equal("F1/技能分", 15, result.breakdown["skills"])
    _equal("F1/扣分", [], result.deductions)
    # 非 JD 举例中的同族成员仍符合任务书的任一成员规则。
    cases = (("Kafka", "RabbitMQ", "Pulsar"), ("Chroma", "Milvus", "Qdrant"),
             ("AWS", "阿里云", "Azure"), ("DeepSeek", "Qwen", "Claude"))
    for first, second, member in cases:
        alternative = _score("F1/同族替代", _resume(skills=(member,)), _job(skills=(first, second)))
        _equal("F1/同族替代技能分", 15, alternative.breakdown["skills"])
        _equal("F1/同族替代扣分", [], alternative.deductions)
    return "同族三项 + DeepSeek → skills=15；无扣分；四族任一成员均可命中"


def _check_family_mixed() -> str:
    """F2：同族与独立技能分别计分，未命中的族显示固定中文名及原始举例。"""
    result = _score("F2", _resume(skills=("DeepSeek",)), _job(skills=("DeepSeek", "Qwen", "Go")))
    _equal("F2/分母2技能分", 8, result.breakdown["skills"])
    _equal("F2/Go照扣", [("必备技能：Go", "Go", 7)],
           [(item.item, item.jd_evidence, item.deduct) for item in result.deductions])
    # 反向缺口验证族文案；只使用 JD 中的举例，不补造其他厂商证据。
    missing = _score("F2/族缺失", _resume(skills=("Go",)), _job(skills=("DeepSeek", "Qwen", "Go")))
    _equal("F2/族缺失技能分", 8, missing.breakdown["skills"])
    _equal("F2/族扣分文案", "必备技能：LLM 产品（举例：DeepSeek、Qwen）", missing.deductions[0].item)
    _equal("F2/族JD依据", "LLM 产品（举例：DeepSeek、Qwen）", missing.deductions[0].jd_evidence)
    # 多次运行同时比较明细及完整扣分列表，禁止集合遍历影响报告顺序。
    snapshots = []
    for _ in range(3):
        repeated = _score("F2/三次确定性", _resume(), _job(skills=("Qwen", "DeepSeek", "Go", "MySQL")))
        snapshots.append((repeated.breakdown, [item.model_dump() for item in repeated.deductions]))
        _equal("F2/技能扣分守恒", 15 - repeated.breakdown["skills"],
               sum(item.deduct for item in repeated.deductions if item.type == "missing_skill"))
    _equal("F2/第1次等于第2次", snapshots[0], snapshots[1])
    _equal("F2/第2次等于第3次", snapshots[1], snapshots[2])
    return "分母2；skills=8；Go扣7；族文案、技能守恒及三次确定性通过"


def _check_family_real_gap() -> str:
    """F3：Python 不能命中 Go，真缺口不因新增概念族而放宽。"""
    result = _score("F3", _resume(skills=("Python",)), _job(skills=("Go",)), "熟悉 Python")
    _equal("F3/技能分", 0, result.breakdown["skills"])
    _equal("F3/Go扣满", [("missing_skill", "Go", 15)],
           [(item.type, item.jd_evidence, item.deduct) for item in result.deductions])
    # 两个非同族技能继续是两项，而不是被合并成一个“编程语言”要求。
    separate = _score("F3/非同族", _resume(skills=("Go",)), _job(skills=("Go", "Python")))
    _equal("F3/非同族分母2", 8, separate.breakdown["skills"])
    _equal("F3/Python照扣", 7, separate.deductions[0].deduct)
    return "Go + Python简历 → skills=0；Go扣15；Go/Python不合并"


def _check_family_concepts() -> str:
    """F4：上位概念可反查族；全部指定别名、空格大小写及原文兜底生效。"""
    result = _score("F4", _resume(skills=("DeepSeek",)), _job(skills=("LLM 产品 API",)))
    _equal("F4/上位概念技能分", 15, result.breakdown["skills"])
    _equal("F4/上位概念扣分", [], result.deductions)
    # 固定排序只是让测试输入可复现，业务输出不从 set 选择代表名。
    for family, aliases in FAMILY_ALIASES.items():
        member = sorted(SKILL_FAMILIES[family])[0]
        for alias in sorted(aliases):
            check = _score("F4/全部别名", _resume(skills=(member.upper(),)), _job(skills=(f" {alias.upper()} ",)))
            _equal("F4/别名反查技能分", 15, check.breakdown["skills"])
            _equal("F4/别名扣分守恒", 15 - check.breakdown["skills"], sum(item.deduct for item in check.deductions))
    # 模型漏抽成员时允许原文兜底；只有泛称则不能假装存在具体产品经验。
    raw = _score("F4/原文兜底", _resume(), _job(skills=("LLM 产品 API",)), "集成 DeepSeek API")
    _equal("F4/原文技能分", 15, raw.breakdown["skills"])
    generic = _score("F4/泛称非成员", _resume(skills=("LLM",)), _job(skills=("LLM 产品 API",)))
    _equal("F4/泛称不冒充产品", 0, generic.breakdown["skills"])
    return "LLM 产品 API + DeepSeek → skills=15；全部别名反查、原文兜底通过"


def _check_documentation() -> None:
    """检查两个新代码文件的注释占比及全部模块、类和函数的文档。"""
    for relative in ("app/scoring_rules.py", "tools/check_scoring_rules.py"):
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        lines = source.splitlines()
        # 分母含空行；只统计独立注释行，不把 docstring 计作注释。
        comments = {token.start[0] for token in tokenize.generate_tokens(io.StringIO(source).readline)
                    if token.type == tokenize.COMMENT and not token.line[:token.start[1]].strip()}
        ratio = len(comments) / len(lines)
        # 规范失败同样携带文件名、期望和实际，不能只输出笼统的 FAIL。
        _equal(f"{relative}/注释占比至少15%", True, ratio >= 0.15)
        tree = ast.parse(source)
        # 同时检查内部辅助函数与数据类，不能只计算公开入口。
        definitions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.ClassDef))]
        documented = sum(ast.get_docstring(node) is not None for node in definitions)
        _equal(f"{relative}/模块文档", True, bool(ast.get_docstring(tree)))
        _equal(f"{relative}/定义文档覆盖", len(definitions), documented)
        print(f"{relative}: 注释 {len(comments)}/{len(lines)}={ratio:.1%}；"
              f"docstring {documented}/{len(definitions)}；PASS")


def main() -> int:
    """运行原九组和新增四组独立断言，输出规范检查和真实通过数。"""
    # 清理上次收集的数据，便于同一解释器中再次执行验收。
    RESULTS.clear()
    # 原九组对应 S3 §四，追加四组来自 S3.5；失败后仍跑完后续组。
    cases = (_check_years_met, _check_years_near, _check_years_short, _check_years_optional,
             _check_years_unknown, _check_education, _check_skills, _check_conservation, _check_aliases,
             _check_family_members, _check_family_mixed, _check_family_real_gap, _check_family_concepts)
    passed = 0
    print("=== S3 + S3.5 硬性条件纯规则验收 ===")
    for index, check in enumerate(cases, 1):
        # 原九组编号保持不变，新四组按任务书标为 F1～F4。
        label = str(index) if index <= 9 else f"F{index - 9}"
        try:
            detail = check()
        except AssertionError as failure:
            # 业务入口的守恒断言没有消息时，也打印具体用例及双方状态。
            print(f"[{label}] FAIL：{check.__name__}；期望=本组断言全部成立；实际={failure!r}")
        else:
            print(f"[{label}] PASS：{detail}")
            passed += 1
    quality_ok = True
    try:
        _check_documentation()
        # 计时包含模型定义加载和规范检查；超过任务书的一秒也返回非零。
        elapsed = perf_counter() - STARTED
        _equal("运行耗时小于1秒", True, elapsed < 1)
    except AssertionError as failure:
        print(f"[规范] FAIL：{failure}")
        quality_ok = False
    print(f"耗时 {perf_counter() - STARTED:.3f} 秒；纯本地，无网络")
    # 末行格式固定；额外规范失败仍须用退出码阻止验收被误判为成功。
    print(f"通过 {passed}/{len(cases)}")
    return 0 if passed == len(cases) and quality_ok else 1


if __name__ == "__main__":
    # Windows 控制台统一 UTF-8，保证中文原样输出并可贴入实施记录。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
