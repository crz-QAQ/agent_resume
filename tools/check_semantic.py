"""验收 S4.5 两段式：假阳性、真相关、三次一致性、耗时和显式降级。"""

from __future__ import annotations

import ast
import io
from pathlib import Path
import sys
from time import perf_counter
import tokenize
from unittest.mock import patch

# 支持从任意工作目录直接运行，以及 PyCharm 右键 Run。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app import scoring_semantic as semantic
from app import rag
from app.rag import EMBED_MODEL_NAME
from app.schemas_match import JobInfo


# 三组分别检查相近改写、混合覆盖和跨行业不相关，职责直接提供，不走抽取。
# 使用明确的项目动作描述，让命中句可以直接与岗位职责人工比对。
CASES = (
    (
        "后端经历与职责相近",
        "个人简历\n使用 Go 开发电商订单服务，完成订单创建、查询和支付接口。"
        "针对 MySQL 慢查询优化索引，将订单查询耗时从两秒降至一百毫秒；"
        "引入 Redis 缓存热点商品数据，降低数据库访问压力。",
        ("负责订单系统接口开发", "负责数据库查询性能优化", "使用 Redis 缓存提升系统性能"),
    ),
    # 机械加工职责保留在缺口，并以零分进入全部职责的平均。
    (
        "AI 交付经历与部分职责相近",
        "项目经历\n使用 Python 搭建企业文档知识库，实现文档切分、向量检索和问答。"
        "在客户现场部署知识库应用，收集业务反馈并完成交付验收！"
        "编写部署手册并培训客户使用系统？",
        ("负责企业文档知识库搭建", "负责客户现场应用部署与交付", "负责机械零件加工与数控机床维修"),
    ),
    # 跨行业场景检验所有职责未覆盖时是否正确返回零分。
    (
        "餐饮经历与软件职责不相关",
        "个人简历\n负责餐厅菜品烹饪，按照食谱完成川菜和粤菜制作。"
        "每日检查食材新鲜度，清洁厨房设备，遵守食品卫生操作规范。"
        "根据就餐人数安排备菜数量，控制食材损耗。",
        ("负责分布式数据库内核开发", "负责编译器代码生成和程序性能优化"),
    ),
)


# 单独新增假阳性输入，保留 CASES 原样，避免改变 S5/S6 导入的三组语料。
# 同句式的数据库内核职责对这份订单简历余弦达 0.711，旧方案误判覆盖。
FALSE_POSITIVE_CASE = (
    "A 假阳性：1 条真相关 + 3 条无关",
    "负责 Go 订单系统后端开发，设计订单创建、支付与查询接口，"
    "优化 MySQL 查询性能并接入 Redis 缓存。",
    ("开发订单接口", "负责分布式数据库内核开发", "负责芯片验证", "负责餐厅后厨管理"),
)


def _job(responsibilities: tuple[str, ...]) -> JobInfo:
    """构造真实 JobInfo 的全部八个字段，保留职责原文及顺序。"""
    # 岗位其他字段不参与语义打分，明确设为空而不补充推测信息。
    return JobInfo(title=None, company=None, required_years_min=None,
                   required_years_max=None, education=None, must_have=[],
                   nice_to_have=[], responsibilities=list(responsibilities))


def _assert_result(result: semantic.SemanticScore, job: JobInfo) -> None:
    """核对分值、完整职责、当前模式的覆盖阈值和覆盖率修正公式。"""
    # 每轮都验证范围，避免只验证三次结果中碰巧正常的一次。
    assert 0 <= result.score <= semantic.MAX_SCORE, f"分数越界：{result.score}"
    # 逐条职责都要保留，不能只在明细中展示覆盖项。
    assert [entry.item for entry in result.per_item] == job.responsibilities
    # 按任务书比较集合；职责原顺序另由上一条明细断言验证。
    assert set(result.uncovered) == {entry.item for entry in result.per_item if not entry.covered}
    for entry in result.per_item:
        # 同时检查阈值关系，防止集合自洽但覆盖判断错误。
        assert -1 <= entry.best_sim <= 1, f"余弦越界：{entry}"
        # 只有明确标记的降级模式才允许使用余弦；正常模式必须检查 logit。
        if semantic.RERANK_FALLBACK_REASON in result.reasons:
            assert result.status == "needs_review"
            assert entry.covered == (entry.best_sim >= semantic.LEGACY_COS_THRESHOLD)
            assert entry.rerank_score == float("-inf")
        else:
            assert entry.covered == (entry.rerank_score >= semantic.RERANK_THRESHOLD)
    # 单独复算 S6 修正公式：未覆盖项计零，分母包含全部职责。
    covered = [entry.best_sim for entry in result.per_item if entry.covered]
    expected = int(30 * sum(covered) / len(job.responsibilities) + 0.5)
    assert result.score == expected, f"平均与舍入错误：期望={expected}，实际={result.score}"


def _check_case(index: int, label: str, text: str, items: tuple[str, ...],
                expected_covered: tuple[bool, ...], minimum: int, maximum: int) -> None:
    """真实运行三次，输出三列、单次耗时，并检查明确的覆盖向量及分数区间。"""
    job = _job(items)
    rr = semantic._get_reranker()
    results, timings = [], []
    # 包装真实编码函数，仅记录调用；不替换任何向量或相似度结果。
    with patch.object(semantic, "_encode_batch", wraps=semantic._encode_batch) as encode, \
            patch.object(rr, "rerank", wraps=rr.rerank) as rerank:
        # 模型只缓存权重，三次业务调用必须重新计算而非读取结果缓存。
        for _ in range(3):
            started = perf_counter()
            results.append(semantic.score_semantic(text, job))
            timings.append(perf_counter() - started)
        assert encode.call_count == 6, f"三轮必须各两批编码，实际={encode.call_count}"
        assert rerank.call_count == 3 * len(items), "每轮每条职责都必须真实重排"
        # 确认职责列表确实整批输入，避免一次函数调用暗中按职责重复编码。
        for call in encode.call_args_list[1::2]:
            assert call.args[1] == job.responsibilities, "职责未整批编码"
        # 实际交给模型的候选不得超过三条，不因某条职责不相关而扩大召回。
        assert all(1 <= len(call.args[1]) <= semantic.RERANK_TOP_K for call in rerank.call_args_list)
    result = results[0]
    # 先输出实测值再断言，失败时仍能看见导致失败的 logit 和耗时。
    print(f"\n[{index}] {label}：score={result.score}/30")
    print("  职责 | cosine | rerank | covered")
    for entry in result.per_item:
        print(f"  {entry.item} | {entry.best_sim:.6f} | {entry.rerank_score:+.6f} | {entry.covered}")
        print(f"    命中简历句：{entry.best_sentence}")
        assert entry.best_sentence in text, "证据不在原文中"
        assert len(entry.best_sentence) >= semantic.MIN_SENTENCE_LEN
    print("  单次耗时（预热后）=" + " / ".join(f"{elapsed:.4f} 秒" for elapsed in timings))
    print("  <3 秒目标：" + ("通过" if max(timings) < 3 else "超出，以上为实际耗时"))
    # 三位小数的实际序列逐轮打印；同时保留原有逐字段完全相同的强断言。
    signatures = [(result.score, [round(entry.rerank_score, 3) for entry in result.per_item])
                  for result in results]
    print(f"  C 三次（score, rerank保留3位）={signatures}")
    print(f"  uncovered={result.uncovered}；status={result.status}；reasons={result.reasons}")
    for result in results:
        _assert_result(result, job)
        # 真实验收禁止通过余弦降级冒充两段式通过。
        assert result.status == "ok" and not result.reasons
        assert tuple(entry.covered for entry in result.per_item) == expected_covered
        assert minimum <= result.score <= maximum, f"分数不在[{minimum}, {maximum}]"
        # dataclass 的列表比较逐字段检查，浮点不取整、不使用近似容差。
        assert result.score == results[0].score, f"{label} 分数不确定"
        assert result.per_item == results[0].per_item, f"{label} 职责明细不确定"
        # 缺口也必须稳定，防止后续 S6 报告在相同输入下发生变化。
        assert result.uncovered == results[0].uncovered, f"{label} 缺口不确定"
    assert signatures[0] == signatures[1] == signatures[2], "三位重排分不一致"
    # 只有本组全部断言完成后才打印 PASS，失败由主入口统一报告。
    print("  PASS：覆盖向量、分数区间、三次一致性、两批编码及 top-3 候选通过")


def _check_empty_inputs() -> None:
    """核对空职责及无有效简历句的防御返回，并保证不加载模型。"""
    # 空职责的说明不是职责条目，不适用正常 per_item/uncovered 集合断言。
    with patch.object(semantic, "_get_model", side_effect=AssertionError("空输入不应加载模型")), \
            patch.object(semantic, "_get_reranker", side_effect=AssertionError("空输入不应加载重排")):
        empty_job = semantic.score_semantic("负责订单系统开发。", _job(()))
        # 固定检查防御说明原文，不能用空列表掩盖无法比对的原因。
        assert empty_job == semantic.SemanticScore(0, [], ["JD 无可比对职责"])
        job = _job(("负责订单系统接口开发",))
        for text in ("", "个人简历。姓名！技能？学历；简介\n"):
            # 每个短片段均不足六字符，应全部过滤且不伪造命中句。
            result = semantic.score_semantic(text, job)
            _assert_result(result, job)
            assert result.score == 0
            # 没有可引用的原句时明确为空，禁止拿职责本身充当简历证据。
            assert result.per_item == [semantic.ItemSim(job.responsibilities[0], 0.0, "", False,
                                                       float("-inf"))]
    print("防御分支：空职责、空简历、全短句通过；未加载模型")


def _check_coverage_formula() -> None:
    """用受控向量和重排隔离公式，确保高余弦但负 logit 的职责按零计分。"""
    outputs = []
    for count, covered, similarity, expected in ((6, 6, 0.75, 23), (6, 1, 0.75, 4),
                                                  (3, 3, 0.70, 21), (3, 1, 0.70, 7)):
        job = _job(tuple(f"岗位职责第{index}条" for index in range(count)))
        # 未覆盖项故意给更高的 0.9 余弦，必须由重排排除，不能再靠余弦阈值。
        sims = [similarity] * covered + [0.9] * (count - covered)
        vectors = semantic.np.asarray([[sim, (1 - sim ** 2) ** 0.5] for sim in sims])
        # 仅此隔离回归使用受控向量；不加载模型、不影响上方真实验收。
        with patch.object(semantic, "_get_reranker") as getter, \
                patch.object(semantic, "_get_model"), patch.object(
                semantic, "_encode_batch", side_effect=[semantic.np.asarray([[1.0, 0.0]]), vectors]):
            # 每条只有一个候选，重排结果仅决定覆盖，不充当相似度幅度。
            getter.return_value.rerank.side_effect = [[5.0]] * covered + [[-1.0]] * (count - covered)
            result = semantic.score_semantic("负责订单系统接口开发。", job)
        _assert_result(result, job)
        assert result.score == expected and len(result.uncovered) == count - covered
        outputs.append(f"覆盖{covered}/{count}（sim={similarity:.2f}）={result.score}")
    # 22.5 按指定 int(x+0.5) 为23；任务书示例中的22不用于覆盖明确公式。
    print("覆盖率回归：" + "；".join(outputs) + "；通过")


def _check_rerank_contract() -> None:
    """验证稳定 top-3、重排证据切换、原最高余弦及 logit=0 的闭区间边界。"""
    sentences = [f"简历候选原句第{index}项" for index in range(4)]
    # 中间两句余弦并列；第四句必须被排除，验证稳定排序和候选数量。
    sims = [0.8, 0.7, 0.7, 0.6]
    vectors = semantic.np.asarray([[sim, (1 - sim ** 2) ** 0.5] for sim in sims])
    job = _job(("负责订单系统开发",))
    with patch.object(semantic, "_get_model"), \
            patch.object(semantic, "_get_reranker") as getter, \
            patch.object(semantic, "_encode_batch", side_effect=[vectors, semantic.np.asarray([[1., 0.]])] * 2):
        # 第一轮重排最高为余弦第二句；第二轮最高恰为零，也必须覆盖。
        getter.return_value.rerank.side_effect = [[1., 5., -1.], [0., -1., -2.]]
        first = semantic.score_semantic("。".join(sentences), job)
        boundary = semantic.score_semantic("。".join(sentences), job)
        for call in getter.return_value.rerank.call_args_list:
            assert call.args == (job.responsibilities[0], sentences[:3])
        # 每次打分只在循环外取模型；不得按候选句重新加载。
        assert getter.call_count == 2
    assert first.per_item == [semantic.ItemSim(job.responsibilities[0], 0.8, sentences[1], True, 5.)]
    assert boundary.per_item[0].covered and boundary.per_item[0].rerank_score == 0.
    assert first.score == boundary.score == 24, "幅度必须取余弦，不能用 logit 乘 30"
    print("重排契约：稳定 top-3；证据切换到重排最高句；best_sim仍为0.8；logit=0覆盖；通过")


def _broken_rerank():
    """模拟消费重排生成器期间失败，而非仅在调用入口抛异常。"""
    yield -1.0
    # FastEmbed 使用生成器，必须实际消费到结束才能认定这条职责精判成功。
    raise RuntimeError("受控重排生成器失败")


def _check_fallback() -> None:
    """验证加载、部分推理、缺项和非有限 logit 均整批回退且传播复核原因。"""
    job = _job(("负责订单系统开发", "负责芯片验证"))
    vectors = semantic.np.asarray([[0.8, 0.6], [0.5, 3 ** 0.5 / 2]])
    # 第一条先得负分，第二条再失败，最终第一条必须重新按余弦判为覆盖。
    failures = ("加载失败", "生成器中途失败", "结果缺项", "NaN", "无穷")
    for label in failures:
        with patch.object(semantic, "_get_model"), \
                patch.object(semantic, "_get_reranker") as getter, \
                patch.object(semantic, "_encode_batch", side_effect=[semantic.np.asarray([[1., 0.]]), vectors]):
            if label == "加载失败":
                getter.side_effect = OSError("受控模型不可用")
            else:
                # 第二个输出分别覆盖迭代异常、长度错误和不可信的数值。
                bad = {"生成器中途失败": _broken_rerank(), "结果缺项": [],
                       "NaN": [float("nan")], "无穷": [float("inf")]}[label]
                getter.return_value.rerank.side_effect = [[-1.0], bad]
            result = semantic.score_semantic("负责订单系统后端接口开发。", job)
            assert getter.call_count == 1
        _assert_result(result, job)
        # 不能保留已完成部分的重排结论，否则结果会混用两种覆盖规则。
        assert result.score == 12 and [entry.covered for entry in result.per_item] == [True, False]
        assert result.status == "needs_review" and result.reasons == [semantic.RERANK_FALLBACK_REASON]
        assert all(entry.best_sentence == "负责订单系统后端接口开发" for entry in result.per_item)
    print("降级护栏：加载/生成器/缺项/NaN/无穷五类失败，均整批回退；status=needs_review")
    print(f"  reasons={result.reasons}；缺测rerank=-inf；score=12（0.8×30/2）")


def _check_documentation() -> None:
    """使用 tokenize 与 AST 检查新增代码注释比例及全部文档字符串。"""
    for relative in ("app/scoring_semantic.py", "tools/check_semantic.py"):
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        # 保守统计独立 # 注释行；分母含空行，docstring 不计为注释。
        comments = {token.start[0] for token in tokenize.generate_tokens(io.StringIO(source).readline)
                    if token.type == tokenize.COMMENT and not token.line[:token.start[1]].strip()}
        ratio = len(comments) / len(source.splitlines())
        tree = ast.parse(source)
        # 模块、数据类以及所有辅助函数都必须有 docstring。
        nodes = [tree] + [node for node in ast.walk(tree)
                          if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
        documented = sum(ast.get_docstring(node) is not None for node in nodes)
        # 两项规范分别断言，报错能准确区分注释不足和文档缺失。
        assert ratio >= 0.15, f"{relative} 注释不足 15%：{ratio:.1%}"
        assert documented == len(nodes), f"{relative} docstring 未达 100%"
        print(f"规范：{relative}；注释={ratio:.1%}；docstring={documented}/{len(nodes)}（100%）")


def main() -> int:
    """预热后逐项统计四组真实输入和五类护栏，末行通过 X/N。"""
    # 总耗时包含首次加载；性能目标单独使用预热后的逐次真实推理耗时。
    started = perf_counter()
    passed = 0
    # 原三组同时作为 S5/S6 固定语料，不因新增假阳性而挪动它们的下标。
    cases = (FALSE_POSITIVE_CASE, *CASES)
    expected = (((True, False, False, False), 0, 10), ((True, True, True), 15, 30),
                ((True, True, False), 10, 20), ((False, False), 0, 0))
    checks = (_check_empty_inputs, _check_coverage_formula, _check_rerank_contract,
              _check_fallback, _check_documentation)
    total = len(cases) + len(checks)
    print("=== S4.5 两段式语义打分验收（本地模型，零 LLM） ===")
    print(f"余弦模型：{EMBED_MODEL_NAME}；CPU 单线程；L2 归一化")
    print(f"重排模型：BAAI/bge-reranker-base（复用rag）；top-{semantic.RERANK_TOP_K}；阈值={semantic.RERANK_THRESHOLD}")
    try:
        # 包装真实构造器观察次数，不替换权重或模型推理；覆盖预热与全部真实轮。
        with patch.object(rag, "TextCrossEncoder", wraps=rag.TextCrossEncoder) as factory:
            warm_started = perf_counter()
            warm = semantic.score_semantic(CASES[0][1], _job(CASES[0][2]))
            print(f"模型加载与预热耗时：{perf_counter() - warm_started:.4f} 秒")
            assert warm.status == "ok", f"预热发生降级：{warm.reasons}"
            for index, ((label, text, items), (covered, minimum, maximum)) in enumerate(zip(cases, expected), 1):
                try:
                    _check_case(index, label, text, items, covered, minimum, maximum)
                except Exception as failure:
                    # 单组失败不隐藏其他结果；降级也不能作为真实模型验收通过。
                    print(f"[{index}] FAIL：{label}；{type(failure).__name__}：{failure}")
                else:
                    passed += 1
            assert factory.call_count == 1, f"重排模型实际加载{factory.call_count}次"
            print(f"\n模型复用：13次真实打分（含预热），reranker只加载{factory.call_count}次")
    except Exception as failure:
        print(f"验收失败：{type(failure).__name__}：{failure}")
        # 模型复用或加载的整体断言失败，即使业务组通过也不能返回成功。
        passed = 0
    # 五类护栏分别计入分母，一类失败仍继续；不让附加失败藏在通过 4/4 后面。
    for check in checks:
        try:
            check()
        except Exception as failure:
            print(f"{check.__name__} FAIL：{type(failure).__name__}：{failure}")
        else:
            passed += 1
    print(f"总耗时（含模型加载与预热）：{perf_counter() - started:.4f} 秒")
    print(f"通过 {passed}/{total}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    # Windows 控制台固定 UTF-8，中文输出可原样粘贴到实施记录。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    raise SystemExit(main())
