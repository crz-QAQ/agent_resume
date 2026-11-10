"""验收 S6：三组真实评分链路、八个档位边界及报告证据与状态护栏。"""

from __future__ import annotations

import argparse
import ast
import asyncio
from copy import deepcopy
from datetime import datetime
import io
import json
import os
from pathlib import Path
import sys
from time import perf_counter
import tokenize
from types import SimpleNamespace
from unittest.mock import patch

# 与 S4/S5 一样支持任意工作目录和 PyCharm 直接运行。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
# embedding 使用已缓存权重；只有 S5 复核会请求网络。
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from app import fallback, scoring_llm
from app.match_report import _verdict, build_report
from app.prompts import PROMPT_VERSION
from app.schemas_match import JobInfo, MatchReport
from app.scoring_llm import LlmItem, LlmScore
from app.scoring_rules import score_hard
from app.scoring_semantic import SemanticScore
from tools.check_llm_score import _inputs


# 边界纯函数验证不请求模型；顺序对应任务书列出的八个输入。
VERDICT_CASES = ((85, "高度匹配"), (84, "匹配"), (70, "匹配"), (69, "勉强"),
                 (55, "勉强"), (54, "不匹配"), (100, "高度匹配"), (0, "不匹配"))


def _assert_report(report: MatchReport, inputs: dict) -> None:
    """检查守恒、扣分搬运、缺口来源、建议上限和关键词原文次数。"""
    # 总分必须严格守恒，即使上游状态不是 ok 也不改变计算方式。
    hard, sem, llm = inputs["hard"], inputs["sem"], inputs["llm"]
    assert report.score.total == hard.score + sem.score + llm.score
    assert 0 <= report.score.total <= 100 and report.verdict == _verdict(report.score.total)
    # 三项及逐项明细分开核验，防止只保留总分而丢失审计材料。
    breakdown = report.score.breakdown
    assert (breakdown["hard"], breakdown["semantic"], breakdown["llm"]) == (hard.score, sem.score, llm.score)
    assert breakdown["hard_detail"] == hard.breakdown and breakdown["semantic_detail"] == sem.score
    assert breakdown["llm_detail"] == {item.key: item.score for item in llm.items}
    assert report.deductions == hard.deductions
    # 校验对象身份，除了字段相同，还确保原扣分对象没有重建或修改。
    assert all(actual is expected for actual, expected in zip(report.deductions, hard.deductions))
    # 技能缺失不混进年限/学历不符；四类未知与不足都保留。
    expected_mismatches = [item for item in hard.deductions if item.type in
                           {"years_short", "years_unknown", "education", "education_unknown"}]
    assert report.mismatches == expected_mismatches
    # 用来源白名单限制缺口，LLM 的理由和建议都不属于白名单。
    allowed_missing = set(sem.uncovered) | {item.jd_evidence for item in hard.deductions
                                          if item.type == "missing_skill"}
    for item in report.missing:
        # 每条既要非空，也要逐字落在本次 JD 原文中，不能只写一个来源标签。
        assert item.jd_evidence.strip() and item.jd_evidence in inputs["jd_text"]
        assert item.item in allowed_missing and item.type == "missing" and item.deduct == 0
        assert item.resume_evidence == "简历未体现"
    # 去重不能靠最后截断建议掩盖，missing 本身也必须唯一。
    assert len({item.item.casefold() for item in report.missing}) == len(report.missing)
    assert len(report.suggestions) <= 10 and len(set(report.suggestions)) == len(report.suggestions)
    assert all(item.startswith(("[LLM] ", "[缺口] ")) for item in report.suggestions)
    for suggestion in report.suggestions:
        if suggestion.startswith("[缺口]"):
            # 模板必须完全等于指定字串，不接受附加技术名或公司名。
            assert suggestion in {f"[缺口] 补充「{item.item}」相关经历或把已有经验改成可对应的说法"
                                  for item in report.missing}
    for keyword in report.keywords[:3]:
        # 每组抽前三项；第三组真实语料没有技能，由下方专门用例补足三项。
        assert keyword.count > 0
        assert keyword.count == inputs["resume_text"].casefold().count(keyword.name.casefold())
    # 已知费用按阶段求和；长度取原始材料，不能取模型截断后的3000字。
    assert report.meta["cost_yuan"] == sum(getattr(stage, "cost_yuan", 0.0) for stage in (hard, sem, llm))
    assert report.meta["resume_chars"] == len(inputs["resume_text"])
    assert report.meta["jd_chars"] == len(inputs["jd_text"])
    assert report.meta["prompt_version"] == PROMPT_VERSION
    assert datetime.fromisoformat(report.meta["generated_at"]).tzinfo is not None
    # 序列化往返同时检查 S0 契约扩展确实能承载 Deduction 和嵌套明细。
    assert MatchReport.model_validate_json(report.model_dump_json()) == report


async def _check_live(index: int) -> tuple[float, bool]:
    """原样复用 S5 语料，真实运行 S3/S4/S5，再调用 S6 并打印报告。"""
    # 计时从 S3/S4 计算之前开始，提供可解释的完整评分链路耗时。
    started = perf_counter()
    label, resume, job, text, hard, sem = _inputs(index)
    # S5 样本没有独立 JD 文件，以下只拼接原有字段值，不编写新的岗位要求。
    jd_text = "\n".join([job.title or "", *job.must_have, *job.responsibilities])
    # 包装真实响应提取器只观察 model；请求、评分、证据校验仍执行原实现。
    with patch.object(fallback, "_extract_text", wraps=fallback._extract_text) as extract:
        llm = await scoring_llm.score_llm(resume, job, text, hard, sem)
    # 读取本次真实服务响应的 model，不能用默认厂商名代替降级后的结果。
    model = getattr(extract.call_args.args[0], "model", None) if extract.call_args else None
    inputs = dict(resume=resume, job=job, resume_text=text, jd_text=jd_text,
                  hard=hard, sem=sem, llm=llm,
                  meta={"model": model, "elapsed_ms": round((perf_counter() - started) * 1000)})
    try:
        report = build_report(**inputs)
    except Exception as exc:
        # 构造失败仍计入已经发生的 LLM 费用，不能只累计成功报告。
        print(f"[{index + 1}] FAIL：构造报告；{type(exc).__name__}：{exc}")
        return llm.cost_yuan, False
    # 业务样本逐组展示全部关键指标，输出可以直接保存进实施记录。
    print(f"\n[{index + 1}] {label}")
    print(f"  总分={report.score.total}/100；三项={hard.score}+{sem.score}+{llm.score}；档位={report.verdict}")
    print(f"  关键字数={len(report.keywords)}；关键字=" +
          json.dumps([item.model_dump() for item in report.keywords], ensure_ascii=False))
    print("  highlights=" + json.dumps([item.model_dump() for item in report.highlights], ensure_ascii=False))
    print(f"  missing={len(report.missing)}条；mismatches={len(report.mismatches)}条")
    # 数量以外也输出全部缺口证据，便于人工检查 JD 回查断言。
    print("  missing依据=" + json.dumps([item.model_dump() for item in report.missing], ensure_ascii=False))
    print("  suggestions（前3条）=" + json.dumps(report.suggestions[:3], ensure_ascii=False))
    print(f"  status={report.status}；reasons={json.dumps(report.reasons, ensure_ascii=False)}")
    print(f"  model={report.meta['model']}；成本={report.meta['cost_yuan']:.8f} 元")
    try:
        _assert_report(report, inputs)
        # 失败也交付三项之和，状态不能被报告汇总器悄悄恢复成 ok。
        assert report.status == llm.status
        if llm.status != "ok":
            assert report.reasons
    except Exception as exc:
        # 断言失败照样把费用带回主入口，避免失败样本漏记真实消耗。
        print(f"  FAIL：报告断言未通过；{type(exc).__name__}：{exc}")
        return llm.cost_yuan, False
    print("  PASS：完整链路汇总、总分守恒、证据、建议和关键词断言通过")
    return llm.cost_yuan, True


def _controlled_inputs() -> dict:
    """构造仅用于报告护栏的输入，硬分真实计算，LLM/语义为受控数据。"""
    # 同时覆盖重复技能、大小写、符号技能以及表外业务词。
    skills = ["Go", "PYTHON", "Redis", "MySQL", "Docker", "Git", "FastAPI", "CI/CD", "业务域", "Go"]
    text = ("张三\n2年工作经验，专科\n最近公司：云杉有限公司\n"
            "Go go GO Python PYTHON Redis MySQL Docker Git FastAPI CI/CD 业务域\n"
            "优化查询耗时降低50%。完成接口交付20项。提升吞吐量30%。")
    # 报告只消费字段，这个离线替身不运行任何简历抽取或模型请求。
    resume = SimpleNamespace(name="张三", years=2.0, skills=skills)
    job = JobInfo(title="接口工程师", company=None, required_years_min=3,
                  required_years_max=None, education="本科及以上", must_have=["Kafka"],
                  nice_to_have=[], responsibilities=["Kafka", "负责接口开发"])
    jd_text = "3年工作经验，本科及以上\n必备技能：Kafka\nKafka\n负责接口开发"
    # 年限少一年、学历不足、Kafka未命中，生成可复核的三笔真实硬分扣分。
    hard = score_hard(resume, job, text)
    # 重复职责及与缺失技能同名的条目，用于验证跨来源合并去重。
    sem = SemanticScore(10, [], ["Kafka", "负责接口开发", "Kafka"])
    # 不存在于 JD 的技术故意只写在 reason，检查它不会成为缺口来源。
    item = LlmItem("stack_depth", "技术栈深度", 6, 12, "负责接口开发",
                   "完成接口交付20项", "模型措辞中的Kubernetes缺口不得进入missing")
    # 此处0.003仅为计量测试数据；真实总成本只在 _check_live 返回后累计。
    llm = LlmScore(6, [item], ["突出已有接口交付", "突出已有接口交付"], [], "ok", [], 0.003)
    return dict(resume=resume, job=job, resume_text=text, jd_text=jd_text, hard=hard, sem=sem, llm=llm)


def _check_redlines() -> None:
    """构造自报分档、模型缺口、重复和越界输入，验证 S6 的来源红线。"""
    inputs = _controlled_inputs()
    before = deepcopy(inputs)
    # 额外模型字段故意与真实总分冲突，不修改 S5 的任何解析算法。
    inputs["llm"].total, inputs["llm"].verdict = 100, "高度匹配"
    report = build_report(**inputs)
    _assert_report(report, inputs)
    # 固定预期来自8+10+6，能直接识别误用模型自报100分。
    assert report.score.total == 24 and report.verdict == "不匹配"
    assert [item.item for item in report.missing] == ["Kafka", "负责接口开发"]
    assert all("Kubernetes" not in item.item for item in report.missing)
    # 精确检查建议顺序、来源前缀、完全相同去重及模板正文。
    assert report.suggestions == ["[LLM] 突出已有接口交付",
        "[缺口] 补充「Kafka」相关经历或把已有经验改成可对应的说法",
        "[缺口] 补充「负责接口开发」相关经历或把已有经验改成可对应的说法"]
    # 比较原始扣分全部字段，确认汇总未写回或新增扣分。
    assert inputs["hard"] == before["hard"] and inputs["sem"] == before["sem"]
    # 超过10条时保持LLM在先的规定顺序，不偷偷让缺口替换已有建议。
    inputs["llm"].suggestions = ["重复"] * 2 + [f"已有经历表述{index}" for index in range(12)]
    capped = build_report(**inputs)
    assert len(capped.suggestions) == 10 and capped.suggestions[0] == "[LLM] 重复"
    assert all(suggestion.startswith("[LLM] ") for suggestion in capped.suggestions)
    for invalid in (-100, 101):
        # 越界拒绝，而非篡改子分或静默 clamp 到100。
        inputs["llm"].score = invalid
        try:
            build_report(**inputs)
        except ValueError:
            pass
        else:
            raise AssertionError("总分越界未拒绝")
    # JD 无法落地的职责和空职责说明均不能伪造 JD 证据。
    inputs = _controlled_inputs()
    # 结构化字段与 JD 原文冲突时只标记复核，不能把字段伪造成原文。
    inputs["job"].responsibilities = ["不存在的JD职责"]
    inputs["sem"].uncovered = ["不存在的JD职责", "JD 无可比对职责"]
    # 暂去技能扣分以隔离职责证据分支，避免合法 Kafka 缺口干扰断言。
    inputs["hard"].deductions = []
    ungrounded = build_report(**inputs)
    assert not ungrounded.missing and ungrounded.status == "needs_review" and ungrounded.reasons
    print("报告红线 PASS：忽略模型total=100/verdict；缺口仅两类来源；去重/零扣分/上限10；越界拒绝；JD原文核验")


def _check_highlights_keywords() -> None:
    """抽查三项次数及全部类别，核实亮点原文、缺省字段与最近公司。"""
    inputs = _controlled_inputs()
    report = build_report(**inputs)
    _assert_report(report, inputs)
    # 原样保留关键字展示名称；只有计数和分类忽略大小写。
    keywords = {item.name: item for item in report.keywords}
    # 三个明确出现次数能发现实现将“出现过”误写成固定次数1的问题。
    assert [keywords[name].count for name in ("Go", "PYTHON", "Redis")] == [3, 2, 1]
    expected_categories = {"Go": "语言", "FastAPI": "框架", "Redis": "中间件", "MySQL": "数据库",
                           "Docker": "云", "Git": "工具", "CI/CD": "工具", "业务域": "其他"}
    assert all(keywords[name].category == category for name, category in expected_categories.items())
    # 姓名、年限、学历、公司和技术栈均检查实际值，避免空字段也算通过。
    values = {item.label: item.value for item in report.highlights}
    assert values["姓名"] == "张三" and values["工作年限"] == "2年工作经验"
    assert values["学历"] == "专科" and values["最近公司"] == "云杉有限公司"
    assert values["技术栈"] == "Go / PYTHON / Redis / MySQL / Docker"
    # 第三条量化成果存在于原文，但输出最多只能保留前两条。
    achievements = [item.value for item in report.highlights if item.label == "成果亮点"]
    assert achievements == ["优化查询耗时降低50%", "完成接口交付20项"]
    assert all(value in inputs["resume_text"] for value in achievements)
    # 没有依据时不填未知姓名、年限、学历、公司、技术栈或数字成果。
    inputs["resume"] = SimpleNamespace(name="原文没有的姓名", years=8, skills=[])
    inputs["resume_text"] = "负责日常事务。"
    assert build_report(**inputs).highlights == []
    # 履历顺序故意从旧到新，检验最近公司依据日期而非取第一行。
    inputs["resume_text"] = "2018.01–2020.02 古松有限公司\n2021.03–至今 新竹有限公司"
    assert {item.label: item.value for item in build_report(**inputs).highlights}["最近公司"] == "新竹有限公司"
    print("原文主要项 PASS：Go=3/PYTHON=2/Redis=1；七类映射；六类亮点、成果≤2；无依据省略；最近公司按日期")


def _check_status_meta() -> None:
    """验证三项任意位置的失败优先级、费用相加、元数据来源与输入不变。"""
    inputs = _controlled_inputs()
    # 当前硬分和语义分为零成本；附加计量只隔离验证通用三项求和，不计入真实费用。
    inputs["hard"].cost_yuan, inputs["sem"].cost_yuan = 0.001, 0.002
    supplied = {"model": "observed-model", "elapsed_ms": 123,
                "cost_yuan": 999, "prompt_version": "伪造版本", "generated_at": "伪造日期"}
    before = dict(supplied)
    report = build_report(**inputs, meta=supplied)
    # 调用者的字典不可变，防止多次构造报告后污染共享元数据。
    assert supplied == before and report.status == "ok"
    assert report.meta["cost_yuan"] == 0.001 + 0.002 + 0.003
    assert report.meta["model"] == "observed-model" and report.meta["elapsed_ms"] == 123
    assert report.meta["prompt_version"] == PROMPT_VERSION and report.meta["generated_at"] != "伪造日期"
    # 未知实际模型如实标识，不能从环境默认值推测降级链的成功模型。
    assert build_report(**inputs).meta["model"] == "unknown"
    for name in ("hard", "sem", "llm"):
        for status in ("needs_review", "failed"):
            variant = deepcopy(inputs)
            # 每个来源分别变状态，避免只验证 LLM 而遗漏其他阶段。
            setattr(variant[name], "status", status)
            setattr(variant[name], "reasons", ["受控原因"])
            result = build_report(**variant)
            assert result.status == status and any("受控原因" in reason for reason in result.reasons)
            assert result.score.total == report.score.total and result.deductions == report.deductions
    # 同时有failed和needs_review时，failed必须优先且保留两侧原因。
    variant = deepcopy(inputs)
    variant["hard"].status, variant["llm"].status = "failed", "needs_review"
    variant["llm"].reasons = ["待复核原因"]
    failed = build_report(**variant)
    assert failed.status == "failed" and any("hard：failed" == reason for reason in failed.reasons)
    assert any("待复核原因" in reason for reason in failed.reasons)
    # 报告缺省耗时只计汇总函数；完整链路耗时由调用者显式提供。
    assert isinstance(failed.meta["elapsed_ms"], int) and failed.meta["elapsed_ms"] >= 0
    print("状态与元数据 PASS：三项分别failed/needs_review；failed优先且保留分数/原因；三项费用相加；版本/时间/字数由代码生成")


def _check_documentation() -> None:
    """对所有本次代码改动用 tokenize/AST 检查注释≥15%及 docstring100%。"""
    for relative in ("app/match_report.py", "app/schemas_match.py", "tools/check_match_report.py",
                     "app/scoring_semantic.py", "tools/check_semantic.py"):
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        # 分母包括空行，字符串内的#与文档字符串不充当独立注释。
        comments = {token.start[0] for token in tokenize.generate_tokens(io.StringIO(source).readline)
                    if token.type == tokenize.COMMENT and not token.line[:token.start[1]].strip()}
        ratio = len(comments) / len(source.splitlines())
        # 模块和每个公开/私有函数都纳入文档率，不能只检查入口。
        tree = ast.parse(source)
        nodes = [tree] + [node for node in ast.walk(tree)
                          if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
        documented = sum(ast.get_docstring(node) is not None for node in nodes)
        assert ratio >= 0.15, f"{relative} 注释不足15%：{ratio:.1%}"
        assert documented == len(nodes), f"{relative} docstring未达100%"
        print(f"规范 PASS：{relative}；注释={len(comments)}/{len(source.splitlines())}（{ratio:.1%}）；"
              f"docstring={documented}/{len(nodes)}（100%）")


async def main(*, offline: bool = False) -> int:
    """独立计数三组、八边界和四类护栏，末行给出通过数与真实总成本。"""
    # 离线模式显式减少分母，不能把未运行的三组真实链路计为通过。
    passed, total_cost = 0, 0.0
    total = len(VERDICT_CASES) + 4 + (0 if offline else 3)
    print("=== S6 汇总报告验收 ===" if not offline else "=== S6 离线报告护栏验收（不含真实链路） ===")
    if not offline:
        for index in range(3):
            try:
                # 失败照样收集成本；每组只真实调用一次，不挑选重试结果。
                cost, success = await _check_live(index)
                total_cost += cost
                passed += int(success)
            except Exception as exc:
                print(f"[{index + 1}] FAIL：{type(exc).__name__}：{exc}")
    print()
    # 八个边界逐条计数和输出，任何一条失败都会使进程退出码非零。
    for value, expected in VERDICT_CASES:
        actual = _verdict(value)
        success = actual == expected
        passed += int(success)
        print(f"档位边界 {'PASS' if success else 'FAIL'}：{value} → {actual}（期望{expected}）")
    # 四类护栏独立运行，一类失败不妨碍其他检查提供诊断结果。
    for label, check in (("报告红线", _check_redlines), ("原文主要项", _check_highlights_keywords),
                         ("状态与元数据", _check_status_meta), ("规范", _check_documentation)):
        try:
            check()
        except Exception as exc:
            print(f"{label} FAIL：{type(exc).__name__}：{exc}")
        else:
            passed += 1
    # 仅真实 S5 复核计入费用，受控输入的模拟成本不算实际账单。
    print(f"通过 {passed}/{total}；总成本 {total_cost:.8f} 元")
    return 0 if passed == total else 1


if __name__ == "__main__":
    # Windows 输出强制 UTF-8，并及时刷出耗时较长的真实调用进度。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="只跑八边界、受控护栏和规范，不调用LLM")
    raise SystemExit(asyncio.run(main(offline=parser.parse_args().offline)))
