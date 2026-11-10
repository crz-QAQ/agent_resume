"""运行 S4 三组真实本地语义匹配，核对确定性、分数与职责证据。"""

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
    # 机械加工职责用于确认低相似度项保留在缺口、但不拉低 covered 平均。
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


def _job(responsibilities: tuple[str, ...]) -> JobInfo:
    """构造真实 JobInfo 的全部八个字段，保留职责原文及顺序。"""
    # 岗位其他字段不参与语义打分，明确设为空而不补充推测信息。
    return JobInfo(title=None, company=None, required_years_min=None,
                   required_years_max=None, education=None, must_have=[],
                   nice_to_have=[], responsibilities=list(responsibilities))


def _assert_result(result: semantic.SemanticScore, job: JobInfo) -> None:
    """核对分值、完整职责、覆盖阈值和未覆盖集合，失败抛 AssertionError。"""
    # 每轮都验证范围，避免只验证三次结果中碰巧正常的一次。
    assert 0 <= result.score <= semantic.MAX_SCORE, f"分数越界：{result.score}"
    # 逐条职责都要保留，不能只在明细中展示覆盖项。
    assert [entry.item for entry in result.per_item] == job.responsibilities
    # 按任务书比较集合；职责原顺序另由上一条明细断言验证。
    assert set(result.uncovered) == {entry.item for entry in result.per_item if not entry.covered}
    for entry in result.per_item:
        # 同时检查阈值关系，防止集合自洽但覆盖判断错误。
        assert -1 <= entry.best_sim <= 1, f"余弦越界：{entry}"
        assert entry.covered == (entry.best_sim >= semantic.COVER_THRESHOLD)
    # 单独复算平均，识别把未覆盖相似度也纳入分母的错误。
    covered = [entry.best_sim for entry in result.per_item if entry.covered]
    expected = int(30 * (sum(covered) / len(covered)) + 0.5) if covered else 0
    assert result.score == expected, f"平均与舍入错误：期望={expected}，实际={result.score}"


def _check_case(index: int, label: str, text: str, items: tuple[str, ...]) -> None:
    """同组真实运行三次，核对结果逐字段相同及每轮仅有两批编码。"""
    job = _job(items)
    started = perf_counter()
    # 包装真实编码函数，仅记录调用；不替换任何向量或相似度结果。
    with patch.object(semantic, "_encode_batch", wraps=semantic._encode_batch) as encode:
        # 模型只缓存权重，三次业务调用必须重新计算而非读取结果缓存。
        results = [semantic.score_semantic(text, job) for _ in range(3)]
        assert encode.call_count == 6, f"三轮必须各两批编码，实际={encode.call_count}"
        # 确认职责列表确实整批输入，避免一次函数调用暗中按职责重复编码。
        for call in encode.call_args_list[1::2]:
            assert call.args[1] == job.responsibilities, "职责未整批编码"
    elapsed = perf_counter() - started
    for result in results:
        _assert_result(result, job)
        # dataclass 的列表比较逐字段检查，浮点不取整、不使用近似容差。
        assert result.score == results[0].score, f"{label} 分数不确定"
        assert result.per_item == results[0].per_item, f"{label} 职责明细不确定"
        # 缺口也必须稳定，防止后续 S6 报告在相同输入下发生变化。
        assert result.uncovered == results[0].uncovered, f"{label} 缺口不确定"
    result = results[0]
    print(f"\n[{index}] {label}：score={result.score}/30；三次耗时={elapsed:.4f} 秒")
    for entry in result.per_item:
        # 输出完整命中句及足够精度的相似度，供人工检查语义是否合理。
        assert entry.best_sentence in text, "证据不在原文中"
        assert len(entry.best_sentence) >= semantic.MIN_SENTENCE_LEN
        print(f"  职责：{entry.item}")
        print(f"    best_sim={entry.best_sim:.6f}；covered={entry.covered}")
        print(f"    命中简历句：{entry.best_sentence}")
    print(f"  uncovered={result.uncovered}")
    # 只有本组全部断言完成后才打印 PASS，失败由主入口统一报告。
    print("  PASS：三次 score、per_item 完全一致；范围、未覆盖集合、两批编码通过")


def _check_empty_inputs() -> None:
    """核对空职责及无有效简历句的防御返回，并保证不加载模型。"""
    # 空职责的说明不是职责条目，不适用正常 per_item/uncovered 集合断言。
    with patch.object(semantic, "_get_model", side_effect=AssertionError("空输入不应加载模型")):
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
            assert result.per_item == [semantic.ItemSim(job.responsibilities[0], 0.0, "", False)]
    print("防御分支：空职责、空简历、全短句通过；未加载模型")


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
    """预热后运行三组验收和防御、规范检查，失败返回非零退出码。"""
    # 总耗时覆盖模型权重加载、预热、九次正式打分及附加检查。
    started = perf_counter()
    passed = 0
    print("=== S4 语义相近打分验收（本地 embedding，零 LLM） ===")
    print(f"模型：{EMBED_MODEL_NAME}；CPU 单线程；L2 归一化")
    try:
        # 按任务书先空跑一次，预热耗时与后续三轮编码耗时分别报告。
        warm_started = perf_counter()
        semantic.score_semantic(CASES[0][1], _job(CASES[0][2]))
        print(f"模型加载与预热耗时：{perf_counter() - warm_started:.4f} 秒")
        # 每组独立计数，N 始终为三组业务样本，而非三轮重复次数。
        for index, (label, text, items) in enumerate(CASES, 1):
            try:
                _check_case(index, label, text, items)
            except Exception as failure:
                # 单组失败仍继续其余组，保留可定位的原始错误。
                print(f"[{index}] FAIL：{label}；{type(failure).__name__}：{failure}")
            else:
                passed += 1
        print()
        # 防御分支单独检查，不混入正常职责的 uncovered 集合约定。
        _check_empty_inputs()
        _check_documentation()
    except Exception as failure:
        # 加载、附加规范或防御检查失败时，末行仍保留规定汇总格式。
        print(f"验收失败：{type(failure).__name__}：{failure}")
        return_code = 1
    else:
        # 三组任一失败均返回 1，便于终端和自动化流程识别失败。
        return_code = 0 if passed == len(CASES) else 1
    print(f"总耗时（含模型加载与预热）：{perf_counter() - started:.4f} 秒")
    print(f"通过 {passed}/{len(CASES)}")
    return return_code


if __name__ == "__main__":
    # Windows 控制台固定 UTF-8，中文输出可原样粘贴到实施记录。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
