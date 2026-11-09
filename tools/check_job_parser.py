"""运行 S1 的三段真实 JD 与三个护栏反例，打印可复核的六项验收结果。

真实抽取消耗模型费用；反例只运行纯代码，禁止用模型为护栏判对错。
可在 PyCharm 右键 Run，也可从任意工作目录使用项目虚拟环境执行。
"""

import ast
import asyncio
import io
import json
from pathlib import Path
import sys
import tokenize

# tools 与 app 是兄弟目录，直接运行脚本时必须显式添加项目根。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.job_parser import JobParseResult, guard_job, parse_and_guard
from app.schemas_match import JobInfo


# 样例逐字来自本次 S1 指令，不使用模型生成的 JD 替换验收输入。
# JD-C 故意不给具体岗位要求，用来核对「成功解析」与「可以采信」的区别。
JD_CASES = (
    ("JD-A", "招聘后端开发工程师，3-5 年经验，熟悉 Go 与 MySQL，了解 Redis 优先，负责订单系统接口开发与性能优化，本科及以上学历。"),
    ("JD-B", "AI 应用工程师（FDE），1-3 年经验，熟练 Python，有 RAG / LLM 落地经验，了解 Dify 或 LangChain，负责客户现场 AI 交付与文档知识库搭建。"),
    ("JD-C", "我们是一家创业公司，正在扩张团队，欢迎有志之士加入，待遇面议。"),
)
# 单次费用上限来自 S1 §二；必须检查实际成本，不能只打印参考价格。
COST_LIMIT_YUAN = 0.002


def _assert_extraction(label: str, result: JobParseResult) -> None:
    """断言真实抽取的关键事实；参数为样例名与结果，返回无，失败抛 AssertionError。"""
    data = result.data
    assert data is not None, f"{label} 未返回可解析数据"
    # 检查状态与费用之外，也检查原文保留，避免证据入口意外被改写。
    assert result.raw_text == dict(JD_CASES)[label], "原始 JD 未完整保留"
    assert 0 <= result.cost_yuan < COST_LIMIT_YUAN, "单次成本未满足 < 0.002 元"
    if label == "JD-C":
        # 宣传文案应进入复核；成功识别空壳本身就是本样例的正确行为。
        assert result.status == "needs_review", "无具体要求的 JD 不得标为 ok"
        assert result.reasons == ["空壳:JD未抽取到要求信息"]
        # 创业公司不是公司名称；这类泛称不能变成结构化的确定事实。
        assert data.title is None and data.company is None and data.education is None
        # None 与 0 必须分开断言，避免把「没提」错误解释成「无经验门槛」。
        assert data.required_years_min is None and data.required_years_max is None
        assert not data.must_have and not data.nice_to_have and not data.responsibilities
        return

    # 对有效 JD 校验事实，职责允许合理分句，避免强迫模型逐字复刻答案。
    assert result.status == "ok" and not result.reasons, "有效 JD 未通过护栏"
    assert data.title and data.company is None and data.responsibilities
    # 技术名大小写不影响事实核对，不能因模型写作 GO / Go 而误报。
    skills = {skill.casefold() for skill in data.must_have}
    if label == "JD-A":
        assert (data.required_years_min, data.required_years_max) == (3.0, 5.0)
        assert {"go", "mysql"} <= skills
        # Redis 的「优先」不能丢失，否则下游会把加分技能误当作必备条件。
        assert "redis" in {skill.casefold() for skill in data.nice_to_have}
        assert data.education and "本科" in data.education
    else:
        assert (data.required_years_min, data.required_years_max) == (1.0, 3.0)
        assert {"python", "rag", "llm"} <= skills
        assert data.education is None
        # 「了解」的归类容许模型根据原文判断，但技术名不能丢失。
        all_skills = skills | {skill.casefold() for skill in data.nice_to_have}
        assert {"dify", "langchain"} <= all_skills


def _check_guards() -> int:
    """构造三个独立护栏反例；无参数，返回通过数，失败会打印并继续。"""
    passed = 0
    # 固定合法基线保证反例只改变被验证的因素，不依赖真实抽取是否成功。
    baseline = JobInfo(
        title="后端开发工程师", company=None,
        required_years_min=3.0, required_years_max=5.0, education=None,
        must_have=["Go", "MySQL"], nice_to_have=["Redis"],
        responsibilities=["负责订单系统接口开发与性能优化"],
    )
    # 每例使用深拷贝；护栏原地修改列表时，不能污染另一条反例的输入。
    # 反例①没有具体要求；不是把一次真实模型失败冒充护栏拦截。
    empty = baseline.model_copy(deep=True)
    empty.required_years_min = empty.required_years_max = None
    empty.must_have, empty.nice_to_have, empty.responsibilities = [], [], []
    # 反例②的颠倒区间来自任务书，保留原文中的年限便于护栏核对。
    inverted = baseline.model_copy(update={"required_years_min": 5.0, "required_years_max": 3.0}, deep=True)
    # 反例③显式注入「后端」，验证 prompt 失守时仍会执行实际删除。
    role_word = baseline.model_copy(deep=True)
    role_word.must_have.append("后端")
    cases = (
        ("空壳", "我们公司在招人", empty, "空壳:JD未抽取到要求信息"),
        ("年限区间", JD_CASES[0][1], inverted, "年限区间异常:min=5.0 max=3.0"),
        ("方向词", JD_CASES[0][1], role_word, "技能过滤:剔除非技术词 后端"),
    )
    print("\n=== 三条纯代码护栏反例 ===")
    for label, jd_text, data, expected in cases:
        reasons = guard_job(jd_text, data)
        print(f"[{label}] reasons = {json.dumps(reasons, ensure_ascii=False)}")
        try:
            assert expected in reasons, f"未返回预期原因：{expected}"
            if label == "方向词":
                # 返回 reason 不等于完成过滤，必须同时核对两份最终技能列表。
                print(f"  过滤后 must_have = {json.dumps(data.must_have, ensure_ascii=False)}")
                print(f"  过滤后 nice_to_have = {json.dumps(data.nice_to_have, ensure_ascii=False)}")
                assert "后端" not in data.must_have + data.nice_to_have
                assert data.must_have == baseline.must_have, "具体技术被误删"
            assert reasons == [expected], "反例混入了非目标护栏问题"
        except AssertionError as exc:
            print(f"  FAIL: {exc}")
        else:
            # 护栏计数只在所有断言通过后累加，不能用打印了原因代替验证。
            print("  PASS")
            passed += 1
    return passed


def _check_documentation() -> bool:
    """统计新增文件注释与函数文档；无参数，返回达标与否，读取错误继续抛出。"""
    valid = True
    print("\n=== 代码规范检查 ===")
    # 只统计本块新增的两个代码文件，避免把旧文件的历史注释算作本块成果。
    for relative in ("app/job_parser.py", "tools/check_job_parser.py"):
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        # 使用 tokenizer 而非搜索 #，避免把字符串内的井号误当成注释。
        comment_lines = {token.start[0] for token in tokenize.generate_tokens(io.StringIO(source).readline)
                         if token.type == tokenize.COMMENT}
        ratio = len(comment_lines) / len(source.splitlines())
        # ast.walk 同时覆盖同步、异步与方法，避免漏掉异常类的初始化函数。
        functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        documented = sum(ast.get_docstring(node) is not None for node in functions)
        # 15% 与 100% 来自 PRD §六红线⑧；保守地把空行也纳入分母。
        passed = ratio >= 0.15 and documented == len(functions) and bool(ast.get_docstring(tree))
        valid = valid and passed
        print(f"{relative}: 注释 {len(comment_lines)}/{len(source.splitlines())} = {ratio:.1%}；"
              f"函数 docstring {documented}/{len(functions)}；{'PASS' if passed else 'FAIL'}")
    return valid


async def main() -> int:
    """运行六项验收；无参数，返回全过为 0 否则 1，意外编程错误继续抛出。"""
    passed = 0
    total_cost = 0.0
    print("=== S1 JD 结构化抽取验收 ===")
    for label, jd_text in JD_CASES:
        print(f"\n[{label}] 输入：{jd_text}")
        result = await parse_and_guard(jd_text)
        # failed 也计入已知费用，防止只统计成功样例低估实际成本。
        total_cost += result.cost_yuan
        # 无数据时也展示八个字段名；这些 null 只是输出占位，不创建 JobInfo 空壳。
        fields = result.data.model_dump() if result.data else dict.fromkeys(JobInfo.model_fields)
        # JSON 输出让 null、数组和中文原文保持清晰，也便于实施记录逐字复核。
        for field, value in fields.items():
            print(f"  {field} = {json.dumps(value, ensure_ascii=False)}")
        print(f"  status = {result.status}")
        # reasons 与状态一起展示，人工可以直接判断每次复核的触发依据。
        print(f"  reasons = {json.dumps(result.reasons, ensure_ascii=False)}")
        print(f"  成本 = {result.cost_yuan:.8f} 元")
        try:
            _assert_extraction(label, result)
        except AssertionError as exc:
            # 单个抽取不达标仍跑完其他样例，末行才能给出真实 X/6。
            print(f"  FAIL: {exc}")
        else:
            print("  PASS")
            passed += 1

    # 三条护栏不调用模型，故总成本只来自上面的三段真实 JD。
    passed += _check_guards()
    documentation_ok = _check_documentation()
    # 末行格式来自用户交付要求；规范检查失败同样通过退出码阻止验收成功。
    print(f"\n通过 {passed}/6；总成本 {total_cost:.8f} 元")
    return 0 if passed == 6 and documentation_ok else 1


if __name__ == "__main__":
    # Windows 控制台可能默认 GBK；统一 UTF-8 后原样输出才能写入实施记录。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(main()))
