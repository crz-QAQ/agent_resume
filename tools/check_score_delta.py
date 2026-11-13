"""M6 A～I：断网实跑本地评分、检查全部契约，并独立回归 M4 的全部 PDF 用例。"""

from __future__ import annotations

import ast
from contextlib import ExitStack
from copy import deepcopy
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter
import tokenize
from types import SimpleNamespace
from unittest.mock import patch

import pdfplumber

# 使用固定仓库根目录，支持终端和 IDE 从任意位置运行。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.dont_write_bytecode = True

from app import resume_rewrite, score_delta as delta, scoring_semantic
# PDF 契约仅接收计算结果，验收不会让渲染器反向依赖评分模块。
from app.revision_pdf import render_review_pdf
from app.schemas_match import Deduction, JobInfo, MatchReport, MatchScore, SemanticItem
from app.schemas_revision import AdaptedText, ScoreDelta
from app.scoring_rules import HardScore, score_hard
from app.scoring_semantic import ItemSim, SemanticScore, score_semantic
from tools.check_revision_pdf import _inputs as pdf_inputs


# 两条跨领域职责使覆盖与未覆盖没有争议；不通过修改阈值凑目标分数。
ITEMS = ["负责订单系统接口开发", "负责机械零件加工与数控机床维修"]
COOKING = "负责餐厅菜品烹饪，按照食谱完成川菜和粤菜制作"
HYGIENE = "每日检查食材新鲜度，清洁厨房设备，遵守食品卫生操作规范"
STAMP = "2026-11-12T13:00:00+08:00"
# 样例只保存实跑结果；测试之间不缓存评分器结果来伪装重复运行。
RESULTS: dict[str, ScoreDelta] = {}
TIMINGS: list[float] = []


def _expect(condition: object, message: str) -> None:
    """失败显式抛异常，python -O 也不能跳过验收。"""
    if not condition:
        raise AssertionError(message)


def _adapt(before: str, after: str, issue_id: str = "R-001") -> AdaptedText:
    """构造固定修改记录，不调用抽取或改写服务。"""
    # 所有改前与改后文字由相应合成场景明确给定。
    return AdaptedText(issue_id=issue_id, location="工作经历 · 第 1 段", before=before,
                       after=after, added_terms=[], kept_numbers=[])


def _inputs() -> tuple:
    """提供评分器既有结构属性与完整 JobInfo，不初始化有副作用的抽取模块。"""
    # 与 ResumeInfo 相同的属性输入；不复制模型定义，不需要任何厂商密钥。
    resume = SimpleNamespace(name="合成候选人", years=3, skills=["Go"])
    # 必备技能已有结构化事实，A/B/C 的变动只来自语义证据。
    job = JobInfo(title="合成岗位", company=None, required_years_min=3, required_years_max=None,
                  education="本科", must_have=["Go"], nice_to_have=[], responsibilities=list(ITEMS))
    return resume, job


def _text(*sentences: str) -> str:
    """添加不会覆盖岗位职责的固定学历、技能及年限证据。"""
    # 职责与元数据分行，避免断句差异将人为头部拼进职责向量。
    return "姓名：合成候选人\n本科\nGo\n3 年经验\n" + "。".join(sentences) + "。"


def _run(text: str, adaptations: list[AdaptedText], **kwargs) -> ScoreDelta:
    """真实执行主入口，检查全部输入不变并记录预热后耗时。"""
    resume, job = _inputs()
    # 快照包含报告及每条建议，能发现嵌套列表被评分或替换逻辑原地修改。
    snapshot = deepcopy((vars(resume), job.model_dump(), [row.model_dump() for row in adaptations], kwargs))
    started = perf_counter()
    # 这里只计主入口耗时；后面的独立复算和 PDF 解析不混入性能值。
    result = delta.estimate_delta(resume=resume, job=job, resume_text=text,
                                  adaptations=adaptations, **kwargs)
    TIMINGS.append(perf_counter() - started)
    _expect(snapshot == (vars(resume), job.model_dump(), [row.model_dump() for row in adaptations], kwargs), "修改传入对象")
    # 用独立入口复算改后分数，防止只靠 notes 自称精确重算。
    rewritten, _ = delta.rewrite_text(text, adaptations)
    if rewritten != text or kwargs.get("before_report") is None:
        # 正式报告无改动时保留报告口径，其余路径逐项与独立评分比较。
        _expect(result.after_parts["hard"] == score_hard(resume, job, rewritten).score, "硬性分与独立复算不同")
        independent = score_semantic(rewritten, job)
        # 降级回退不能冒充正常重排验收通过，必须检查真实状态。
        _expect(independent.status == "ok" and not independent.reasons, "独立语义评分发生降级")
        _expect(result.after_parts["semantic"] == independent.score, "语义分与独立复算不同")
    # 每条主路径都核对预估标记，避免只在单独 D 例中偶然通过。
    _expect(result.estimated_fields == ["llm"] and result.llm_tolerance == 2, "LLM 预估契约错误")
    return result


def _check_a() -> str:
    """实测两条职责从零覆盖到覆盖一条，并检查共享替换的边界。"""
    # 新句是第一条职责的明确证据，另一条机械职责仍保持缺口。
    result = _run(_text(COOKING, HYGIENE), [_adapt(COOKING, ITEMS[0])], before_llm=25)
    _expect(result.covered_before == 0 and result.covered_after == 1, "覆盖不是 0 → 1")
    _expect(result.delta_parts["semantic"] > 0 and "覆盖条数" in "".join(result.notes), "新增覆盖没有涨分或解释")
    # 重复原句只改第一处；规范化命中须映射回原文，保留周围字节。
    with patch.object(delta, "_replace_pdf_text", wraps=resume_rewrite._replace_pdf_text) as shared:
        rewritten, count = delta.rewrite_text("前文\n负责 订单|系统/接口·开发\n负责订单系统接口开发\n后文",
                                             [_adapt(ITEMS[0], "订单接口交付")])
    # 包裹调用必须真正命中共享函数，不能复制其代码后仅做无用导入。
    _expect(shared.call_count == 1 and shared.call_args.kwargs == {"normalize": True}, "未 import 复用共享替换")
    _expect(rewritten == "前文\n订单接口交付\n负责订单系统接口开发\n后文" and count == 1, "规范化/首次定位错误")
    # 两条相同建议与后写文字不能链式命中，未匹配也不能意外插入正文。
    changed, count = delta.rewrite_text("原始职责描述\n原始职责描述", [
        _adapt("原始职责描述", "改写后的职责"), _adapt("原始职责描述", "错误的第二次修改", "R-002"),
        _adapt("改写后的职责", "禁止链式修改", "R-003"), _adapt("不存在的句子", "不得插入", "R-004")])
    _expect(changed == "改写后的职责\n原始职责描述" and count == 1, "重复/未匹配/链式替换错误")
    # 保留本次实算的对象，最后直接序列化交付，禁止重建示例分数。
    RESULTS["A"] = result
    return f"覆盖 0 → 1；语义 {result.before_parts['semantic']} → {result.after_parts['semantic']}；首次/规范化/跳过/冲突通过"


def _check_b() -> str:
    """只调整不相关句的措辞，覆盖两条的精确证据与得分保持不变。"""
    # 两条完全覆盖的职责不改，只调整第三条不相关描述的表达。
    result = _run(_text(*ITEMS, HYGIENE), [_adapt(HYGIENE, "按食品卫生规范检查食材新鲜度并清洁厨房设备")], before_llm=25)
    _expect(result.covered_before == result.covered_after == 2, "覆盖数量改变")
    _expect(result.delta_parts["semantic"] == 0 and "覆盖条数未变" in "".join(result.notes), "措辞变化被伪造为提分")
    # 必须同时检查计数和分数，不能用条数不变推断分数不变。
    RESULTS["B"] = result
    return "覆盖 2 → 2；语义变化 0；notes 含覆盖条数未变"


def _check_c() -> str:
    """实测把已覆盖职责改坏，负分与丢失职责必须完整保留。"""
    # 替换掉机械职责后，订单职责保留；这是真实的负向反例。
    result = _run(_text(*ITEMS), [_adapt(ITEMS[1], COOKING)], before_llm=25)
    _expect((result.covered_before, result.covered_after) == (2, 1), "覆盖不是 2 → 1")
    _expect(result.after_total_estimate < result.before_total and result.delta_parts["semantic"] < 0, "负 delta 被隐藏")
    _expect("不再被判定为覆盖" in "".join(result.notes) and ITEMS[1] in "".join(result.notes), "没有点名丢失职责")
    # 同一负向结果会继续交给 PDF，验证界面没有另行抹平负分。
    RESULTS["C"] = result
    return f"覆盖 2 → 1；总分 {result.before_total} → {result.after_total_estimate}；变化 {result.after_total_estimate - result.before_total}"


def _check_d() -> str:
    """检查 LLM 沿用标注、缺失原分报错、正式报告优先及旧报告兼容。"""
    # 重用已实算对象只为读字段，不替代 G 中的三次评分执行。
    result = RESULTS["A"]
    _expect(result.estimated_fields == ["llm"] and result.after_parts["llm"] == 25, "LLM 被重算或漏标预估")
    _expect(any("±2" in note and "未重算" in note for note in result.notes), "LLM 解释不完整")
    resume, job = _inputs()
    # 缺分时不得先进行本地推理，更不能伪造一个默认 LLM 分。
    with patch.object(delta, "score_semantic", side_effect=AssertionError("缺 LLM 不应推理")):
        try:
            delta.estimate_delta(resume=resume, job=job, resume_text=_text(COOKING), adaptations=[])
        except ValueError:
            pass
        else:
            raise AssertionError("缺 before_llm 未抛 ValueError")
    # 人为放入与现算不同的历史正式分，验证不能用现算分悄悄覆盖。
    sem = score_semantic(_text(COOKING, HYGIENE), job)
    report = MatchReport(score=MatchScore(total=53, breakdown={"hard": 30, "semantic": 0, "llm": 23,
                         "hard_detail": {"years": 15, "education": 10, "skills": 5}}),
                         verdict="不匹配", keywords=[], highlights=[], deductions=[], missing=[], mismatches=[],
                         suggestions=[], meta={}, semantic_items=[SemanticItem(**vars(row)) for row in sem.per_item])
    formal = _run(_text(COOKING, HYGIENE), [_adapt(COOKING, ITEMS[0])], before_report=report, before_llm=1)
    # 显式传入相冲突的 1 分，正式报告仍必须优先给出 23 分。
    _expect(formal.before_total == 53 and formal.before_parts == {"hard": 30, "semantic": 0, "llm": 23}, "未优先采用正式报告")
    _expect("改前分为评分报告口径" in formal.notes, "未披露历史分口径")
    # 旧版本没有 semantic_items 时，补算明细后仍应得到同一个差值对象。
    legacy = report.model_copy(update={"semantic_items": []})
    same = _run(_text(COOKING, HYGIENE), [_adapt(COOKING, ITEMS[0])], before_report=legacy)
    _expect(same == formal, "旧报告补算覆盖证据改变正式分")
    return "estimated_fields=['llm']；沿用 25；±2；缺分报错；正式报告及旧报告口径通过"


def _check_e() -> str:
    """空建议及全部跳过时返回零差值，并验证硬性/取整解释模板。"""
    # 空列表既不能凭空改字，也不能引入重新估计的 LLM 波动。
    result = _run(_text(*ITEMS), [], before_llm=25)
    _expect(all(value == 0 for value in result.delta_parts.values()), "空修改产生分差")
    _expect("未改动任何句子" in "".join(result.notes), "缺空改动说明")
    # 非空列表但零命中也需要明确的成功/未替换计数。
    skipped = _run(_text(*ITEMS), [_adapt("原文中不存在", "不应写入")], before_llm=25)
    _expect(all(value == 0 for value in skipped.delta_parts.values()) and "未替换 1 条" in "".join(skipped.notes), "全部跳过未说明")
    # 本组额外隔离模板的稀有分支；主 A/B/C 分值均来自实际模型。
    hard = HardScore(40, {}, [])
    missing = Deduction(item="必备技能：Go", type="missing_skill", deduct=3,
                        jd_evidence="Go", resume_evidence="简历未提及", severity="high")
    low = HardScore(37, {}, [missing])
    no_coverage = SemanticScore(0, [ItemSim("职责", .01, "原句", False, -1)], ["职责"])
    tiny_coverage = SemanticScore(0, [ItemSim("职责", .01, "改后原句", True, 1)], [])
    gained = delta.delta_from_scores(before_hard=low, before_sem=no_coverage, before_llm=25,
                                    after_hard=hard, after_sem=tiny_coverage)
    # 将同一扣分反向应用，验证不能只实现有利的解除扣分模板。
    lost = delta.delta_from_scores(before_hard=hard, before_sem=tiny_coverage, before_llm=25,
                                  after_hard=low, after_sem=no_coverage)
    _expect("解除「必备技能：Go」扣分" in gained.notes[0] and "新增「必备技能：Go」扣分" in lost.notes[0], "硬性正负模板缺失")
    _expect("取整" in gained.notes[1] and "覆盖条数 0 → 1" in gained.notes[1], "覆盖变化但整数不变未解释")
    return "空改动/全部跳过均为零；硬性正负原因及覆盖取整模板通过"


def _check_f() -> str:
    """通过主入口 monkeypatch 极端硬性分，确认上下界均抛 ValueError。"""
    # 对上游注入非法结果而非直接抛异常，真正触发被测汇总器的边界保护。
    resume, job = _inputs()
    for invalid, valid_semantic in ((80, SemanticScore(0, [], [])), (-30, SemanticScore(0, [], []))):
        # 上界与下界分别覆盖，排除只写 min(100, total) 的单向隐藏。
        with patch.object(delta, "score_hard", return_value=HardScore(invalid, {}, [])), \
                patch.object(delta, "score_semantic", return_value=valid_semantic):
            try:
                delta.estimate_delta(resume=resume, job=job, resume_text=COOKING,
                                     adaptations=[_adapt(COOKING, ITEMS[0])], before_llm=25)
            except ValueError as exc:
                # 异常消息必须指向总分，不接受其他参数错误造成的假通过。
                _expect("after_total_estimate" in str(exc), "异常来自其他路径")
            else:
                raise AssertionError(f"越界总分 {invalid + 25} 被 clamp 或静默接受")
    return "after_total_estimate=105 / -5 均抛 ValueError；未 clamp"


def _check_g() -> str:
    """相同输入真实重算三次，序列化 JSON 与每一个字段完全相同。"""
    # 调用真实评分器并包裹计数，不返回任何预制分值。
    with patch.object(delta, "score_hard", wraps=score_hard) as hard, \
            patch.object(delta, "score_semantic", wraps=score_semantic) as semantic:
        results = [_run(_text(COOKING, HYGIENE), [_adapt(COOKING, ITEMS[0])], before_llm=25) for _ in range(3)]
    # 主入口各三轮两次；_run 的独立复算直接调用原函数，不混入包裹计数。
    _expect(hard.call_count == semantic.call_count == 6, "三轮未各自重算改前改后")
    # 比较 JSON 可同时检查所有字段、notes 顺序与中文正文。
    _expect(results[0].model_dump_json() == results[1].model_dump_json() == results[2].model_dump_json(), "三次运行不一致")
    return "三次逐字段/JSON 完全相同；每轮硬性与语义各实算两次；输入快照未变"


def _check_h() -> str:
    """检查契约源码、注释/docstring、同步零远端调用及实测性能。"""
    # 文本检查配合 AST 检查，既满足 grep 红线，也覆盖别名异步语法。
    source = (PROJECT_ROOT / "app/score_delta.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    _expect("openai" not in source.lower() and "await" not in source, "源码引入远端或异步入口")
    _expect(not any(isinstance(node, (ast.Await, ast.AsyncFunctionDef)) for node in ast.walk(tree)), "出现异步逻辑")
    # tokenizer 只计算真实注释；docstring 和颜色字符串不混入注释比例。
    summaries = []
    for relative in ("app/score_delta.py", "app/revision_pdf.py", "tools/check_score_delta.py"):
        content = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        comments = {token.start[0] for token in tokenize.generate_tokens(StringIO(content).readline) if token.type == tokenize.COMMENT}
        ratio = len(comments) / len(content.splitlines())
        # 私有函数、模块和嵌套回调都计入文档覆盖，空行也计入比例分母。
        nodes = [node for node in ast.walk(ast.parse(content)) if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
        _expect(ratio >= .15 and all(ast.get_docstring(node) for node in nodes), f"{relative} 注释 {ratio:.1%} 或 docstring 不完整")
        summaries.append(f"{Path(relative).name} {ratio:.1%}")
    # 从 PRD 的 Python 片段提取字段，逐字核对模型签名，避免自写预期掩盖契约偏移。
    prd = (PROJECT_ROOT / "prd/PRD_简历修改意见_v1.md").read_text(encoding="utf-8")
    contract = "class ScoreDelta" + prd.split("class ScoreDelta", 1)[1].split("```", 1)[0]
    expected = [(node.target.id, ast.unparse(node.annotation)) for node in ast.parse(contract).body[0].body if isinstance(node, ast.AnnAssign)]
    actual = [(name, str(field.annotation).replace("<class 'int'>", "int")) for name, field in ScoreDelta.model_fields.items()]
    # 字段顺序、名称、类型及容差默认值都属于 PRD 固定契约。
    _expect(actual == expected and ScoreDelta.model_fields["llm_tolerance"].default == 2, "ScoreDelta 字段与 PRD 不一致")
    _expect("内部结构，不进对外 API" in ScoreDelta.__doc__, "内部结构文档声明缺失")
    # 取所有真实主入口运行的最慢值，不能挑最快的一轮宣称性能达标。
    _expect(max(TIMINGS) < 3, f"预热后主入口耗时 {max(TIMINGS):.4f}s 超过 3s")
    return f"源码无远端/异步入口；成本 0 元；预热后最大 {max(TIMINGS):.4f}s；" + "；".join(summaries) + "；docstring 100%"


def _pdf_text(data: bytes) -> str:
    """从真实 PDF 字节提取全部页面文字，不复用生成 story 的字符串。"""
    with pdfplumber.open(BytesIO(data)) as document:
        return "\n".join(page.extract_text() or "" for page in document.pages)


def _check_i() -> str:
    """读回正负分 PDF、三列与红字，并在新进程完整运行 M4 回归。"""
    # 意见和诊断复用 M4 的既有真实数据结构，只额外传入 ScoreDelta。
    report, diagnosis, hard = pdf_inputs()
    report.adaptations = [_adapt(COOKING, ITEMS[0])]
    snapshot = report.model_dump_json(), diagnosis.model_dump_json()
    # 基线和显式 None 在同一页面解析口径比较，排除 PDF 文件 ID 随时间变化。
    baseline = render_review_pdf(report, diagnosis, job_title="M6 合成岗位", hard=hard)
    explicit_none = render_review_pdf(report, diagnosis, job_title="M6 合成岗位", hard=hard, delta=None)
    _expect(_pdf_text(baseline) == _pdf_text(explicit_none) and "分数对比" not in _pdf_text(baseline), "delta=None 改变旧行为")
    output = PROJECT_ROOT / "output/pdf/m6"
    output.mkdir(parents=True, exist_ok=True)
    for key, label in (("A", "提分"), ("C", "降分")):
        # 将实际检查过的同一份字节写成交付样张，不再渲染一份不同结果。
        result = RESULTS[key]
        sample = report.model_copy(deep=True, update={"adaptations": [
            _adapt(COOKING, ITEMS[0]) if key == "A" else _adapt(ITEMS[1], COOKING)]})
        sample_snapshot = sample.model_dump_json()
        data = render_review_pdf(sample, diagnosis, job_title="M6 合成岗位", hard=hard, delta=result)
        _expect(sample_snapshot == sample.model_dump_json(), "PDF 修改逐条对照输入")
        text = _pdf_text(data)
        compact = "".join(text.split())
        _expect(all(word in text for word in ("智能修改", "分数对比", "改前", "改后预估", "变化", "预估 ±2")), "PDF 缺比较表/LLM 标签")
        _expect(text.index("分数对比") < text.index("意见 原文 改后"), "分数对比位于 before/after 表之后")
        # 检查真实页面中三个表头的同行坐标，横线表格无需添加视觉竖线。
        with pdfplumber.open(BytesIO(data)) as document:
            header_rows = []
            for page in document.pages:
                words = page.extract_words()
                for word in words:
                    if word["text"] == "改前":
                        header_rows.append([row["text"] for row in words if abs(row["top"] - word["top"]) < 1])
            _expect(["改前", "改后预估", "变化"] in header_rows, "实际 PDF 表头不是三列")
            # PDF 的 RGB 会按小数位编码，比较实际字符颜色时允许编码舍入误差。
            warning_chars = [char for page in document.pages for char in page.chars
                             if isinstance(color := char.get("non_stroking_color"), (tuple, list))
                             and len(color) == 3 and all(abs(a - b) < 1e-5 for a, b in
                                                        zip(color, (180 / 255, 35 / 255, 24 / 255)))]
        change = result.after_total_estimate - result.before_total
        _expect(str(change) in compact, "PDF 隐藏了真实变化")
        if change < 0:
            # 警告全文及其红色字符两者都要存在，排除只打印负号没有提醒。
            _expect("本次改写后预估分下降，建议只采用部分建议" in compact, "负分警告缺失")
            _expect("本次改写后预估分下降" in "".join(char["text"] for char in warning_chars), "警告没有真正使用红色文字")
        (output / f"M6_{label}.pdf").write_bytes(data)
    # 渲染器也必须遵守不修改传入对象，而不是只有计算模块满足纯函数。
    _expect(snapshot == (report.model_dump_json(), diagnosis.model_dump_json()), "PDF 修改输入")
    # M4 原验收断言导入图不含语义模块，因此必须使用干净进程，不能污染其 sys.modules。
    # 原自检会落盘四份历史样张，回归前后恢复原字节，避免污染已有基准。
    historical = [PROJECT_ROOT / f"output/pdf/m4/M4_合成{key}.pdf" for key in "ABC"]
    historical.append(PROJECT_ROOT / "tmp/pdfs/M4_G_分页.pdf")
    originals = {path: path.read_bytes() for path in historical if path.exists()}
    try:
        regression = subprocess.run([sys.executable, "-B", "tools/check_revision_pdf.py"], cwd=PROJECT_ROOT,
                                    capture_output=True, text=True, encoding="utf-8", timeout=120)
    finally:
        for path, content in originals.items():
            path.write_bytes(content)
    print("M4 回归完整输出：\n" + regression.stdout.rstrip())
    # 成败只由子进程退出码决定，新增用例不应因为历史通过数量而被误判失败。
    _expect(regression.returncode == 0, "M4 回归失败：" + regression.stderr)
    # 末行汇总只用于日志展示；即使汇总格式改变也不能推翻成功的退出码。
    summary = next((line.split("；", 1)[0] for line in reversed(regression.stdout.splitlines())
                    if line.startswith("通过 ")), "未提供通过数")
    return f"三列/三项明细/LLM ±2/负数红字读回通过；delta=None 保持原行为；M4 {summary}"


def main() -> int:
    """全程封锁网络和远端评分，预热本地模型后运行 A～I 并输出真实对象。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    started = perf_counter()
    RESULTS.clear()
    TIMINGS.clear()
    passed = 0
    groups = (("A 覆盖新增/替换复用", _check_a), ("B 措辞不增覆盖", _check_b), ("C 改坏降分", _check_c),
              ("D LLM 标注/正式报告", _check_d), ("E 空改动/解释模板", _check_e), ("F 越界保护", _check_f),
              ("G 三次一致", _check_g), ("H 零模型/性能/规范", _check_h), ("I PDF 集成/回归", _check_i))
    with ExitStack() as stack:
        # 权重只允许从现有本地缓存加载，缓存缺失应失败，不能下载冒充零网络。
        stack.enter_context(patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"}))
        network = [stack.enter_context(patch(target, side_effect=AssertionError("M6 禁止联网"))) for target in
                   ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo")]
        # 多层拦截同时覆盖同步、异步厂商请求；任何调用都会让该组失败。
        calls = [stack.enter_context(patch(target, side_effect=AssertionError("M6 禁止远端评分"))) for target in
                 ("openai.resources.chat.completions.Completions.create", "openai.resources.chat.completions.AsyncCompletions.create",
                  "app.fallback.call_with_fallback_async", "app.providers.call_with_fallback")]
        print("预热：本地 embedding + 重排；网络与远端评分已拦截")
        warm_started = perf_counter()
        try:
            _, job = _inputs()
            warm = scoring_semantic.score_semantic(_text(*ITEMS), job)
            _expect(warm.status == "ok" and not warm.reasons, "本地语义评分发生降级")
        except Exception as exc:
            print(f"预热失败：{type(exc).__name__}: {exc}")
            print(f"通过 0/{len(groups)}；耗时 {perf_counter() - started:.3f}s")
            return 1
        print(f"预热完成：{perf_counter() - warm_started:.3f}s；正式计时从 A 开始")
        # 预热时间单列报告，本地权重初始化不纳入单次主入口的三秒门槛。
        for label, action in groups:
            # 失败后继续后面的独立组，最终退出码准确反映全部结果。
            try:
                detail = action()
            except Exception as exc:
                print(f"{label}：失败；{type(exc).__name__}: {exc}")
            else:
                passed += 1
                print(f"{label}：通过；{detail}")
        # 即使某层捕获了拦截异常，也不能隐藏已经尝试发出的请求。
        _expect(all(call.call_count == 0 for call in calls + network), "发生远端评分或网络调用")
    if "A" in RESULTS:
        # 打印真正由本地评分结果汇总的对象，notes 全文不能只输出摘要。
        print("真实 ScoreDelta（合成用例 A，本地评分器实算）：")
        print(json.dumps(RESULTS["A"].model_dump(), ensure_ascii=False, indent=2))
    print(f"通过 {passed}/{len(groups)}；耗时 {perf_counter() - started:.3f}s；模型调用 0；网络请求 0；成本 0 元")
    return 0 if passed == len(groups) else 1


if __name__ == "__main__":
    raise SystemExit(main())
