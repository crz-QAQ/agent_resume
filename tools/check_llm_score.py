"""验收 S5：五次真实复核、三次波动、纯函数与失败护栏及代码规范。"""

from __future__ import annotations

import argparse
import ast
import asyncio
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
import tokenize
from unittest.mock import AsyncMock, patch

# 支持在任意目录或 PyCharm 中直接运行，不要求设置 PYTHONPATH。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
# S4 权重已在本地；禁止验收时为 embedding 意外下载另一份模型。
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from app import scoring_llm as llm
from app.providers import UsageMeter
from app.scoring_rules import score_hard
from tools.check_semantic import CASES
from app.schemas import ResumeInfo
from app.schemas_match import JobInfo
from app.scoring_semantic import SemanticScore, score_semantic


def _inputs(index: int, *, with_embedding: bool = True) -> tuple:
    """原样复用 S4 简历与职责，补齐八字段，并计算真实的 S3/S4 上下文。"""
    label, text, responsibilities = CASES[index]
    # S4 原文不补年限、数字或项目，用相同语料检查新增复核模块。
    skills = (("Go", "MySQL", "Redis"), ("Python",), ())[index]
    # 岗位标题仅作八字段上下文，三项证据仍要引用实际字段值。
    title = ("后端开发工程师", "AI交付工程师", "系统软件工程师")[index]
    # 没有来源的公司、年限、学历保留空值，不为演示补写候选人事实。
    job = JobInfo(title=title, company=None, required_years_min=None,
                  required_years_max=None, education=None, must_have=list(skills),
                  nice_to_have=[], responsibilities=list(responsibilities))
    resume = ResumeInfo(name=None, years=None, skills=list(skills))
    hard = score_hard(resume, job, text)
    # 不把 S4 记录中的旧分数写死到真实验收里。
    # 离线护栏只消费数据结构；真实五次调用都使用 score_semantic 的实际结果。
    semantic = score_semantic(text, job) if with_embedding else SemanticScore(0, [], [])
    return label, resume, job, text, hard, semantic


def _print_result(label: str, result: llm.LlmScore) -> None:
    """完整输出每项分数、双证据、理由、建议与费用，失败信息也保留。"""
    print(f"\n[{label}] score={result.score}/30；status={result.status}")
    for item in result.items:
        # 重复轮也打印全部证据，不能用第一轮输出代替另外两次结果。
        print(f"  {item.key}（{item.label}）：{item.score}/{item.max_score}")
        print(f"    jd_evidence={item.jd_evidence}")
        print(f"    resume_evidence={item.resume_evidence}")
        print(f"    reason={item.reason}")
    print(f"  suggestions={json.dumps(result.suggestions, ensure_ascii=False)}")
    # 两类建议分别展示，便于检查模型是否产生了需删除的技术词。
    print(f"  dropped_suggestions={json.dumps(result.dropped_suggestions, ensure_ascii=False)}")
    print(f"  reasons={json.dumps(result.reasons, ensure_ascii=False)}")
    print(f"  成本={result.cost_yuan:.8f} 元")


def _assert_live(result: llm.LlmScore, text: str, job: JobInfo) -> None:
    """每次真实调用都核对完整三项、原文落地、守恒以及单次成本预算。"""
    assert result.status == "ok", f"真实样本未通过护栏：{result.reasons}"
    # 完整性和守恒分开检查，避免缺项但合计恰好在范围内的假通过。
    assert [item.key for item in result.items] == [row[0] for row in llm.RUBRIC]
    assert result.score == sum(item.score for item in result.items)
    assert 0 <= result.score <= 30
    # 使用字段值而非序列化键名，保持与正式评分相同的来源边界。
    jd_text = llm._job_text(job)
    for item, (key, label, maximum) in zip(result.items, llm.RUBRIC):
        # 核对固定名称和权重，防止模型偷偷改变项目上限。
        assert (item.key, item.label, item.max_score) == (key, label, maximum)
        assert 0 <= item.score <= maximum and item.reason.strip()
        for evidence, source in ((item.jd_evidence, jd_text), (item.resume_evidence, text)):
            # 长度与落地分别断言，空字符串本来会匹配任意原文。
            assert len("".join(evidence.split())) >= 4
            assert llm._normalize_evidence(evidence) in llm._normalize_evidence(source)
    # 已交付建议再过滤应不变；这也检查建议没有绕过过滤路径。
    assert llm.filter_suggestions(result.suggestions, text, jd_text) == (result.suggestions, [])
    # 预算沿用项目 UsageMeter 口径，费用数字逐轮原样打印。
    assert 0 <= result.cost_yuan <= 0.003, f"单次成本超预算：{result.cost_yuan:.8f}"


def _check_variation(results: list[llm.LlmScore]) -> None:
    """打印三轮实际整数、最大最小分差，并断言总分差≤2及每项差≤1。"""
    assert len(results) == 3 and all(len(result.items) == 3 for result in results)
    print("\n=== 第1组三次分数波动（真实调用，无评分结果缓存） ===")
    vectors = [[item.score for item in result.items] for result in results]
    # 每轮总分取真实结果，并已由 _assert_live 验证是子项之和。
    totals = [result.score for result in results]
    for index, (vector, total) in enumerate(zip(vectors, totals), 1):
        # 所有轮次的原始数字都输出，不能只报告最终通过与否。
        print(f"  第{index}次：stack_depth={vector[0]}，domain_fit={vector[1]}，"
              f"proof_strength={vector[2]}，合计={total}")
    differences = [max(values) - min(values) for values in zip(*vectors)]
    # 用三轮最大减最小，能识别两次重复分别向相反方向漂移的情况。
    total_diff = max(totals) - min(totals)
    print("  每项分差=" + json.dumps(dict(zip([row[0] for row in llm.RUBRIC], differences))))
    print(f"  总分差={total_diff}")
    # 一致仅指三项分数；不把可能不同的理由措辞宣称为完整响应一致。
    print("  三次完全一致（分数）" if vectors[0] == vectors[1] == vectors[2] else "  三次分数不一致，以上为实测分差")
    assert total_diff <= 2 and all(value <= 1 for value in differences), "实测波动超限"


def _check_filter() -> None:
    """不调用模型，验证 Kubernetes 丢弃、来源白名单、大小写及输入不变。"""
    suggestions = ["建议补充 Kubernetes 经验", "建议突出交付能力"]
    original = list(suggestions)
    # 两侧均无 Kubernetes 时必须丢弃，不能只在 prompt 中写禁止编造。
    kept, dropped = llm.filter_suggestions(suggestions, "使用 Python 交付应用", "负责应用部署")
    assert kept == [suggestions[1]] and dropped == [suggestions[0]]
    assert suggestions == original, "纯函数不能修改输入列表"
    allowed = ["突出 PYTHON 交付", "突出 Redis 缓存", "使用 Go.", "介绍 C++、C# 和 .NET"]
    # 来源分别位于简历/JD，兼查大小写与真实技术名中的符号。
    assert llm.filter_suggestions(allowed, "Python Go C++ C# .NET", "Redis") == (allowed, [])
    # 按任务书，JD 出现的词允许通过过滤；这不代表它已是简历经历。
    assert llm.filter_suggestions([suggestions[0]], "交付应用", "Kubernetes") == ([suggestions[0]], [])
    print(f"纯函数 PASS：suggestions={json.dumps(kept, ensure_ascii=False)}；"
          f"dropped={json.dumps(dropped, ensure_ascii=False)}；Kubernetes 必须丢弃")


def _payload(text: str, job: JobInfo) -> dict:
    """生成带有真实原句的受控响应，只用于护栏测试，不冒充真实模型结果。"""
    evidence = text.split("\n", 1)[1].split("。", 1)[0]
    # 保留简历中的完整原句，不用手写近义句掩盖证据回查问题。
    # 三项共用一句合法引文足以隔离解析/校验行为，不评价模型打分质量。
    return {"items": [{"key": key, "score": score,
                       "jd_evidence": job.responsibilities[0],
                       "resume_evidence": evidence, "reason": "受控护栏测试"}
                      for (key, _, _), score in zip(llm.RUBRIC, (6, 10, 6))],
            "suggestions": ["建议补充 Kubernetes 经验", "建议突出已有订单接口交付"]}


async def _check_guards() -> None:
    """注入响应验证红线、解析降级、clamp、证据回查和3000字截断。"""
    _, resume, job, text, hard, semantic = _inputs(0, with_embedding=False)
    baseline = _payload(text, job)
    # 模拟计量仅核验失败仍保留费用，不纳入真实调用总成本。
    meter = UsageMeter(prompt_tokens=100, completion_tokens=10)
    cases = []
    for response in ("```json\n" + json.dumps(baseline) + "\n```", json.dumps(baseline)):
        # 围栏兼容和正常解析都必须保留过滤动作。
        cases.append(("围栏或裸JSON", response, "ok", 22))
    spoofed = deepcopy(baseline)
    # 故意让自报总分与三项之和冲突，证明额外字段没有生效。
    spoofed.update({"score": 100, "total": 100, "verdict": "高度匹配"})
    for entry in spoofed["items"]:
        entry.update({"label": "伪造名称", "max_score": 999})
    cases.append(("忽略模型自报总分档位上限", json.dumps(spoofed), "ok", 22))
    clamped = deepcopy(baseline)
    # 同一响应覆盖上界、下界及第三项上界，不只测试一个权重。
    for item, value in zip(clamped["items"], (99, -8, 99)):
        item["score"] = value
    cases.append(("分数clamp", json.dumps(clamped), "needs_review", 20))
    ungrounded = deepcopy(baseline)
    # 双证据均足够长但找不到，应降级而非混同空证据失败。
    ungrounded["items"][0].update(jd_evidence="原文没有的岗位要求", resume_evidence="原文没有的工作经验")
    cases.append(("查不回双证据降级", json.dumps(ungrounded), "needs_review", 22))
    spaced = deepcopy(baseline)
    # 真实字词完全不变，只有空白和标点可被归一化忽略。
    for item in spaced["items"]:
        item["resume_evidence"] = item["resume_evidence"].replace(" ", "\n").replace("，", "；")
    cases.append(("忽略引文空白标点", json.dumps(spaced), "ok", 22))
    # 全空证据是明确验收项；另查短证据和只缺一侧，不能误给部分分。
    for evidence in ("", " \t\n ", "Go", "。。。。"):
        invalid = deepcopy(baseline)
        for item in invalid["items"]:
            item.update(jd_evidence=evidence, resume_evidence=evidence)
        cases.append(("全空或短证据", json.dumps(invalid), "failed", 0))
    for field in ("jd_evidence", "resume_evidence"):
        invalid = deepcopy(baseline)
        invalid["items"][0][field] = ""
        cases.append(("单侧空证据", json.dumps(invalid), "failed", 0))
    # 缺项、重复、未知 key 以及分数类型都必须失败，而非补默认值继续计分。
    for mode in ("missing", "duplicate", "unknown", "score", "suggestions", "reason"):
        invalid = deepcopy(baseline)
        if mode == "missing":
            invalid["items"].pop()
        elif mode == "duplicate":
            invalid["items"][1]["key"] = "stack_depth"
        elif mode == "unknown":
            invalid["items"][1]["key"] = "other"
        elif mode == "score":
            invalid["items"][0]["score"] = True
        elif mode == "suggestions":
            invalid["suggestions"] = "不是数组"
        else:
            invalid["items"][0]["reason"] = ""
        cases.append((mode, json.dumps(invalid), "failed", 0))
    for response in ("{", "说明文字{}", "", None, "null", "[]"):
        cases.append(("JSON或顶层结构错误", response, "failed", 0))
    for label, response, status, score in cases:
        # 每个响应单独注入，避免上一个替身或返回值污染下一个用例。
        mock = AsyncMock(return_value=(response, meter))
        with patch.object(llm, "call_with_fallback_async", mock):
            result = await llm.score_llm(resume, job, text, hard, semantic)
        assert (result.status, result.score) == (status, score), (label, result)
        assert result.cost_yuan == meter.cost(), "失败不能丢失已知费用"
        # 调用参数只允许 system，不传 temperature，也不能让 mocked 测试掩盖多次调用。
        mock.assert_awaited_once()
        assert mock.call_args.kwargs == {"system": llm.SYSTEM_MATCH_RUBRIC}
        if status == "failed":
            # 失败时不能交付任何原始建议，也不能留下可用于汇总的子项。
            assert not result.items and not result.suggestions and result.reasons
        else:
            # 成功和待复核分支都必须过滤，不能只给 ok 结果加护栏。
            assert result.dropped_suggestions == [baseline["suggestions"][0]]
            assert result.score == sum(item.score for item in result.items)
        if label == "查不回双证据降级":
            assert len(result.reasons) == 2
            assert all(reason.startswith("证据未在原文中找到:") for reason in result.reasons)
        if label == "分数clamp":
            assert len(result.reasons) == 3 and [item.score for item in result.items] == [12, 0, 8]
        if response in ("{", "说明文字{}", "", None):
            assert result.reasons[0].startswith("JSON解析失败:")
    # 边界文本只进入 user 的简历区，确保尾部没有从上下文摘要泄漏回 prompt。
    long_text = text + "长" * (llm.MAX_RESUME_CHARS - len(text)) + "TAIL_SHOULD_NOT_BE_SENT"
    mock = AsyncMock(return_value=(json.dumps(baseline), meter))
    with patch.object(llm, "call_with_fallback_async", mock):
        await llm.score_llm(resume, job, long_text, hard, semantic)
    prompt = mock.call_args.args[0]
    # 同时验证前3000字完整存在与后缀不存在，不能用空 prompt 假装截断。
    assert "TAIL_SHOULD_NOT_BE_SENT" not in prompt and long_text[:3000] in prompt
    # 厂商调用异常返回失败原因类型，不能把可能含凭据的异常详情写进结果。
    with patch.object(llm, "call_with_fallback_async", AsyncMock(side_effect=RuntimeError("受控失败"))):
        failed = await llm.score_llm(resume, job, text, hard, semantic)
    assert failed.status == "failed" and failed.score == 0 and not failed.items
    print(f"护栏 PASS：{len(cases)}组受控响应；全空证据failed/0；clamp=12+0+8；"
          "证据查不回needs_review；忽略自报100；3000字截断；调用失败归零")


def _check_documentation() -> None:
    """用 tokenize/AST 检查新增文件的独立注释≥15%及文档字符串100%。"""
    for relative in ("app/scoring_llm.py", "tools/check_llm_score.py"):
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        # 注释分母含空行，docstring 与字符串内 # 都不计为独立注释。
        comments = {token.start[0] for token in tokenize.generate_tokens(io.StringIO(source).readline)
                    if token.type == tokenize.COMMENT and not token.line[:token.start[1]].strip()}
        ratio = len(comments) / len(source.splitlines())
        tree = ast.parse(source)
        nodes = [tree] + [node for node in ast.walk(tree)
                          if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
        documented = sum(ast.get_docstring(node) is not None for node in nodes)
        # 模块和数据类也计入覆盖率，不能只保证公开函数有 docstring。
        assert ratio >= 0.15, f"{relative} 注释不足15%：{ratio:.1%}"
        assert documented == len(nodes), f"{relative} docstring未达100%"
        print(f"规范 PASS：{relative}；注释={len(comments)}/{len(source.splitlines())}"
              f"（{ratio:.1%}）；docstring={documented}/{len(nodes)}（100%）")


async def main(*, offline: bool = False) -> int:
    """跑完独立验收后打印真实通过数与总成本；失败返回1，离线不伪造实测。"""
    passed, total_cost = 0, 0.0
    total = 3 if offline else 7
    first_results: list[llm.LlmScore] = []
    first_inputs = None
    # 总数包含三组、波动、纯函数、护栏、规范，重复轮不重复算业务组。
    print("=== S5 LLM复核验收 ===" if not offline else "=== S5 离线护栏验收（不含真实调用与波动） ===")
    if not offline:
        # 先三组各一次，再第一组两次；五次调用均走真实 fallback，不重试挑结果。
        for index in range(5):
            case_index = index if index < 3 else 0
            label = f"{case_index + 1}组第1次" if index < 3 else f"1组第{index - 1}次"
            try:
                inputs = _inputs(case_index) if index < 3 else first_inputs
                assert inputs is not None, "第1组上下文未能构建"
                name, resume, job, text, hard, semantic = inputs
                if index == 0:
                    # 两次重复复用同一输入对象，排除上下文变化造成的假波动。
                    first_inputs = inputs
                result = await llm.score_llm(resume, job, text, hard, semantic)
                # 校验失败的真实响应同样计费，不能从成本与波动中删除。
                total_cost += result.cost_yuan
                if case_index == 0:
                    first_results.append(result)
                _print_result(f"{label} {name}", result)
                _assert_live(result, text, job)
            except Exception as exc:
                print(f"  FAIL：{label}；{type(exc).__name__}：{exc}")
            else:
                if index < 3:
                    passed += 1
                print("  PASS：完整三项、双证据、守恒与单次成本≤0.003元")
        try:
            # 缺少任何真实轮次都不能用两次结果宣称三次一致。
            _check_variation(first_results)
            # 重复两次的状态/费用也必须达标，不能仅凭分差小就通过。
            for result in first_results:
                _assert_live(result, first_inputs[3], first_inputs[2])
        except Exception as exc:
            print(f"波动 FAIL：{type(exc).__name__}：{exc}")
        else:
            passed += 1
    # 三个附加检查独立计数，任一失败都会影响末行与退出码。
    for label, check in (("纯函数", _check_filter), ("护栏", _check_guards), ("规范", _check_documentation)):
        try:
            if asyncio.iscoroutinefunction(check):
                await check()
            else:
                check()
        except Exception as exc:
            print(f"{label} FAIL：{type(exc).__name__}：{exc}")
        else:
            passed += 1
    # 费用仅来自五次真实调用；模拟 usage 与本地 embedding 不计入账单。
    print(f"通过 {passed}/{total}；总成本 {total_cost:.8f} 元")
    return 0 if passed == total else 1


if __name__ == "__main__":
    # UTF-8 保证 Windows 的完整输出能无损写入实施记录。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="仅验收纯函数、受控护栏和代码规范")
    raise SystemExit(asyncio.run(main(offline=parser.parse_args().offline)))
