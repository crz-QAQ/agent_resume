"""验证 S2 对普通 Word、表格 Word、双栏 PDF 和扫描件的统一行为。

前三份文件调用现有简历 Agent 并打印真实结果与费用；扫描件必须明确拒绝。
使用脚本所在位置寻找测试文件，因此 PyCharm 右键 Run 不依赖工作目录。
"""

import ast
import asyncio
import io
import json
from pathlib import Path
import sys
import tokenize

# tools 与 app 是兄弟目录，直接运行脚本时先加入项目根目录。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.resume_parser import (
    FileParseError,
    ResumeParseResult,
    ScannedPdfError,
    parse_resume_file,
)


# S2 任务书指定现成的四份文件，不重新生成或替换验收样本。
TEST_DIR = PROJECT_ROOT / "tools" / "test_files"
FILE_NAMES = ("clean_resume.docx", "table_resume.docx", "text_resume.pdf", "scan_resume.pdf")


def _assert_result(filename: str, result: ResumeParseResult) -> None:
    """核对真实文件的关键事实；参数为文件名与结果，返回无，失败抛 AssertionError。"""
    # 扫描件若走到正常返回分支，即使 status=failed 也不符合明确抛异常的要求。
    assert filename != "scan_resume.pdf", "扫描件未抛 ScannedPdfError"
    assert result.file_name == filename, "原文件名未保留"
    assert result.raw_text.strip(), "原始文本为空"
    assert result.status in {"ok", "needs_review"}, "有效简历抽取失败"
    # 必须验证实际字段，不能让只返回状态的默认空结构通过验收。
    assert result.data is not None, "未返回简历数据"
    # S2 允许沿用 Agent 的 repair，因此不另设单次费用门槛。
    assert result.cost_yuan >= 0, "成本不应为负数"
    # 技术名大小写不影响事实比较，避免 Java / JAVA 风格差异造成误报。
    skills = {skill.casefold() for skill in result.data.skills}
    if filename == "clean_resume.docx":
        # 固定时间样本没有相对时间词，按任务书必须进入 ok 分支。
        assert result.status == "ok"
        assert result.data.name == "陈明" and result.data.years == 10.0
        assert "陈明" in result.raw_text
        assert {"java", "spring", "mysql", "redis", "docker"} <= skills
    elif filename == "table_resume.docx":
        # 姓名和技能段落都抽到，也不能证明经历表格没有被遗漏。
        # 样本明确写了 6 年，不能根据「至今」重新估算成其他年数。
        assert result.data.name == "张伟" and result.data.years == 6.0
        assert {"go", "mysql", "redis"} <= skills
        # 时间、公司、职责来自表格本体，三类证据一起核对才能发现漏表格。
        for evidence in ("张伟", "2019.07 - 2023.03", "深圳某某科技有限公司", "订单系统和支付模块", "Go + MySQL + Redis"):
            assert evidence in result.raw_text, f"表格经历缺失：{evidence}"
    else:
        assert result.data.name == "李娜" and result.data.years == 7.0
        assert {"python", "fastapi", "mysql", "redis"} <= skills
        lines = result.raw_text.splitlines()
        # 历史双栏串行问题会把左右栏标题并在首行，必须直接检查保留的原文。
        assert lines[0].strip() == "李娜", "双栏 PDF 首行顺序异常"
        assert "工作经历" in result.raw_text
        assert not any("李娜" in line and "工作经历" in line for line in lines), "双栏内容串行"


def _check_documentation() -> bool:
    """统计新增文件的注释和函数文档；无参数，返回是否达标，读取错误向上抛出。"""
    valid = True
    print("\n=== 代码规范检查 ===")
    # 只核对本块新文件，旧模块的注释不能算作 S2 的完成依据。
    for relative in ("app/resume_parser.py", "tools/check_resume_parser.py"):
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        # tokenizer 只计真正的 # 注释，字符串或 docstring 内的井号不计入。
        comment_lines = {token.start[0] for token in tokenize.generate_tokens(io.StringIO(source).readline)
                         if token.type == tokenize.COMMENT}
        ratio = len(comment_lines) / len(source.splitlines())
        # AST 同时覆盖同步、异步与嵌套函数，避免只统计公开函数而漏报。
        functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        documented = sum(ast.get_docstring(node) is not None for node in functions)
        # 15% 与 100% 来自 PRD 红线⑧；空行也计入总行数，使用保守口径。
        passed = ratio >= 0.15 and documented == len(functions) and bool(ast.get_docstring(tree))
        valid = valid and passed
        print(f"{relative}: 注释 {len(comment_lines)}/{len(source.splitlines())} = {ratio:.1%}；"
              f"函数 docstring {documented}/{len(functions)}；{'PASS' if passed else 'FAIL'}")
    return valid


async def main() -> int:
    """运行四份文件验收；无参数，全部达标返回 0，否则返回 1，意外编程错误向上抛出。"""
    passed = 0
    total_cost = 0.0
    print("=== S2 简历侧输入统一验收 ===")
    for filename in FILE_NAMES:
        print(f"\n[{filename}]")
        try:
            # 用绝对位置读现有文件，右键运行时不会误读其他目录下的同名样本。
            raw = (TEST_DIR / filename).read_bytes()
            result = await parse_resume_file(filename, raw)
        except (FileParseError, RuntimeError, OSError) as exc:
            # 这里只在验收脚本中展示异常；统一入口自身不得捕获并返回空壳。
            print("  status = 无结果（异常向上抛出）")
            # null 仅表示异常没有生成数据，不能用这些展示占位值构造解析结果。
            print("  name = null\n  years = null\n  skills = null")
            print("  文本长度 = 不适用（未返回文本）")
            print("  成本 = 0.00000000 元（未获得 Agent 成本结果）")
            print(f"  异常 = {type(exc).__name__}: {exc}")
            try:
                # 扫描件只接受指定异常，普通解析失败或模型故障都不能冒充 PASS。
                assert filename == "scan_resume.pdf", "非扫描件发生异常"
                assert isinstance(exc, ScannedPdfError), "扫描件未抛 ScannedPdfError"
            except AssertionError as failure:
                print(f"  FAIL: {failure}")
            else:
                print("  PASS（扫描件明确拒绝）")
                passed += 1
            continue

        # 总费用沿用 Agent 汇总，repair 发生时也会包含在内。
        total_cost += result.cost_yuan
        data = result.data
        print(f"  status = {result.status}")
        print(f"  name = {json.dumps(data.name if data else None, ensure_ascii=False)}")
        print(f"  years = {json.dumps(data.years if data else None)}")
        print(f"  skills = {json.dumps(data.skills if data else None, ensure_ascii=False)}")
        print(f"  文本长度 = {len(result.raw_text)}")
        print(f"  成本 = {result.cost_yuan:.8f} 元")
        # 同时展示原因，人工能复核 needs_review 是否沿用了 Agent 的护栏结果。
        print(f"  reasons = {json.dumps(result.reasons, ensure_ascii=False)}")
        if filename == "text_resume.pdf":
            # 显示首行，让历史双栏问题的验证依据直接出现在交付输出里。
            lines = result.raw_text.splitlines()
            print(f"  首行 = {json.dumps(lines[0] if lines else None, ensure_ascii=False)}")
        try:
            _assert_result(filename, result)
        except AssertionError as exc:
            # 单例失败仍跑完四份文件，末行反映真实通过数与已有费用。
            print(f"  FAIL: {exc}")
        else:
            print("  PASS")
            passed += 1

    # 规范检查独立于四份文件计数，失败仍须用非零退出码报告。
    documentation_ok = _check_documentation()
    # 四项计数来自任务书；规范不达标同样通过退出码阻止验收成功。
    print(f"\n通过 {passed}/4；总成本 {total_cost:.8f} 元")
    return 0 if passed == 4 and documentation_ok else 1


if __name__ == "__main__":
    # Windows 控制台编码可能不是 UTF-8，统一后才便于原样记录中文输出。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(main()))
