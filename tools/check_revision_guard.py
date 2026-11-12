"""离线检查 M0 的八组契约与护栏，失败打印具体条目、期望和实际。

直接运行即可；只导入纯模型和纯函数，不调用模型、不联网、不读取 .env。
"""

import ast
import io
from pathlib import Path
import sys
from time import perf_counter
import tokenize

# 从业务模块导入前计时，包含 Pydantic 模型加载和全部检查。
STARTED = perf_counter()
# 支持任意工作目录直接运行；禁止导入过程生成项目内的字节码文件。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.dont_write_bytecode = True

from pydantic import BaseModel, ValidationError

from app.guard_revision import (
    CN_DIGITS, PROPER_SUFFIX, QUOTED, TECH_TOKEN, find_original, new_numbers,
    new_proper_nouns, new_tech_terms, normalize_text, resolve_job_input,
    validate_adaptation, validate_issue, _number_tokens,
)
from app.schemas_revision import AdaptedText, JobProfile, ReviewIssue, RevisionReport


def _equal(label: str, expected: object, actual: object) -> None:
    """逐项比较；参数为条目、期望、实际，返回无，不等时抛含双方值的 AssertionError。"""
    # repr 保留空白、空串和 None 的区别，便于定位边界错误。
    assert actual == expected, f"哪条={label}；期望={expected!r}；实际={actual!r}"


def _issue(**updates: str) -> ReviewIssue:
    """构造意见；参数为覆盖字段，返回合法模型，非法输入抛 ValidationError。"""
    # 使用固定基线，每条反例只改变正在核对的字段。
    data = dict(id="R-001", category="weak_evidence", severity="high", location="项目经历",
                original_text="使用 Go 开发接口，响应耗时两秒。", suggested_text="使用 Go 开发接口，耗时 2 秒。",
                why="职责缺少清晰表达", gain="让接口职责更易核对", jd_evidence="负责接口开发", risk_note="仅使用真实数据")
    data.update(updates)
    return ReviewIssue(**data)


def _adapted(**updates: object) -> AdaptedText:
    """构造改写；参数为覆盖字段，返回合法模型，非法输入抛 ValidationError。"""
    # 列表保持为空，确保正文中的伪造信息不会因元数据漏记而漏检。
    data = dict(issue_id="R-001", location="项目经历", before="使用 Go 开发接口，响应耗时两秒。",
                after="使用 Go 开发接口，耗时 2 秒。", added_terms=[], kept_numbers=[])
    data.update(updates)
    return AdaptedText(**data)


def _rejects(model: type[BaseModel], data: dict) -> bool:
    """检查拒收；参数为模型类和输入字典，返回是否抛 ValidationError，其他异常继续抛出。"""
    # 不把任意异常当作通过，避免程序错误掩盖契约问题。
    try:
        model.model_validate(data)
    except ValidationError:
        return True
    return False


def _documentation() -> str:
    """核对规范；无参数，返回三文件统计，读取错误或不达标会抛异常。"""
    summaries = []
    # 空行也计入分母；只计算 tokenizer 识别的真实注释，不把 docstring 充作注释。
    for relative in ("app/schemas_revision.py", "app/guard_revision.py", "tools/check_revision_guard.py"):
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        comments = {token.start[0] for token in tokenize.generate_tokens(io.StringIO(source).readline)
                    if token.type == tokenize.COMMENT}
        ratio = len(comments) / len(source.splitlines())
        # 同时核对模块、类、所有函数，私有辅助函数也不能遗漏文档。
        nodes = [node for node in ast.walk(tree)
                 if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
        missing = [getattr(node, "name", "模块") for node in nodes if not ast.get_docstring(node)]
        _equal(f"{relative}/注释≥15%（实际 {ratio:.1%}）", True, ratio >= 0.15)
        _equal(f"{relative}/docstring 100%", [], missing)
        # 公共与私有护栏函数都必须说明参数、返回、异常。
        if relative == "app/guard_revision.py":
            for node in nodes:
                if isinstance(node, ast.FunctionDef):
                    doc = ast.get_docstring(node) or ""
                    _equal(f"{node.name}/文档三要素", True,
                           all(word in doc for word in ("参数：", "返回：", "异常：")))
            # 依赖限于标准库与新契约，不能间接加载已有模型客户端或 .env。
            imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
            imports.update(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                           for alias in node.names)
            # Decimal 仅用于精确量级转换，仍无外部服务或客户端依赖。
            _equal("护栏导入边界", {"re", "unicodedata", "decimal", "app.schemas_revision"}, imports)
            _equal("护栏无异步", [], [type(node).__name__ for node in ast.walk(tree)
                                      if isinstance(node, (ast.AsyncFunctionDef, ast.Await))])
        summaries.append(f"{Path(relative).name} 注释 {ratio:.1%}、docstring 100%")
    return "；".join(summaries)


def _check_contract() -> str:
    """核对①；无参数，返回模型及规范摘要，断言或契约失败会抛异常。"""
    # 期望字段独立逐字列自 PRD §六，不从待测模型反向生成期望。
    expected = {
        ReviewIssue: dict(id=str, category=str, severity=str, location=str, original_text=str,
                          suggested_text=str, why=str, gain=str, jd_evidence=str, risk_note=str),
        JobProfile: dict(title=str, source=str, confirmed=bool, must_have=list[str],
                         responsibilities=list[str], note=str),
        AdaptedText: dict(issue_id=str, location=str, before=str, after=str,
                          added_terms=list[str], kept_numbers=list[str]),
        RevisionReport: dict(issues=list[ReviewIssue], rejected_issues=list[dict], profile=JobProfile,
                             score_before=int | None, score_after_estimate=int | None,
                             adaptations=list[AdaptedText], skipped=list[dict], meta=dict),
    }
    # 类型、顺序、必填性和中文描述分别核对，不能只看能否实例化。
    for model, fields in expected.items():
        _equal(f"{model.__name__}/字段", list(fields), list(model.model_fields))
        _equal(f"{model.__name__}/类型", fields,
               {name: field.annotation for name, field in model.model_fields.items()})
        _equal(f"{model.__name__}/必填", True, all(field.is_required() for field in model.model_fields.values()))
        _equal(f"{model.__name__}/中文描述", True,
               all(field.description and any("\u3400" <= char <= "\u9fff" for char in field.description)
                   for field in model.model_fields.values()))
    issue, adapted = _issue(), _adapted()
    # 四个真实模型一起实例化；分数字段同时核对允许 None 与整数两种情况。
    # 嵌套对象使用真实实例，防止字段误写为无类型字典仍然通过检查。
    profile = JobProfile(title="后端开发", source="user_jd", confirmed=True,
                         must_have=["Go"], responsibilities=["接口开发"], note="真实 JD")
    report = RevisionReport(issues=[issue], rejected_issues=[], profile=profile,
                            score_before=None, score_after_estimate=None,
                            adaptations=[adapted], skipped=[], meta={})
    _equal("四模型实例化与嵌套", (issue, profile, adapted), (report.issues[0], report.profile, report.adaptations[0]))
    _equal("评分可为 None", (None, None), (report.score_before, report.score_after_estimate))
    # 同时检查整数正常接收，避免把可空字段错误限定成只能为 None。
    scored = RevisionReport.model_validate({**report.model_dump(), "score_before": 60, "score_after_estimate": 70})
    _equal("评分可为整数", (60, 70), (scored.score_before, scored.score_after_estimate))
    # 缺失类建议可为空串；不允许把必需的字符串字段变成 None。
    _equal("missing 空建议", "", _issue(category="missing", suggested_text="").suggested_text)
    _equal("建议不接受 None", True, _rejects(ReviewIssue, {**issue.model_dump(), "suggested_text": None}))
    # 可空不等于可以省略，PRD 中两个可空评分仍是必需字段。
    _equal("分数字段不能省略", True,
           _rejects(RevisionReport, {key: value for key, value in report.model_dump().items() if key != "score_before"}))
    return "4 个模型字段/类型/None 全部一致；" + _documentation()


def _check_original() -> str:
    """核对②；无参数，返回原句检索摘要，不符时抛 AssertionError。"""
    # 跨行、多个空格、全角标点都属于允许的检索规范化。
    resume = "工作经历：使用  Go\n开发接口，响应耗时两秒。"
    _equal("跨行多空格", True, find_original(resume, "使用 Go 开发接口，响应耗时两秒。"))
    _equal("只改一个字", False, find_original(resume, "使用 Go 开发接头，响应耗时两秒。"))
    _equal("常见中英文标点", "甲乙丙丁", normalize_text(" 甲，乙。\n丙, 丁! "))
    # 防止 Python 的空子串规则错误放行未提供的引用。
    for original in ("", " \n\t", "，。！？"):
        _equal(f"拒绝空原句 {original!r}", False, find_original(resume, original))
    _equal("空简历", False, find_original("", "职责"))
    return "跨行/空白/标点可检索；改单字与空原句被拒"


def _check_tech() -> str:
    """核对③；无参数，返回英文技术词摘要，不符时抛 AssertionError。"""
    # 正反例各自只使用简历和 JD，不向词表添加 Kubernetes 或任何通用词。
    _equal("编造 Kubernetes", ["Kubernetes"], new_tech_terms("使用 Kubernetes", "使用 Go", "熟悉 Redis"))
    _equal("原文并集", [], new_tech_terms("Go 与 Redis", "使用 Go", "熟悉 Redis"))
    _equal("大小写不敏感", [], new_tech_terms("GO redis", "Go", "Redis"))
    _equal("无通用词白名单", ["Implemented"], new_tech_terms("Implemented", "", ""))
    # 完整词与技术符号不可退化成子串或别名匹配。
    _equal("英文子串不放行", ["Go"], new_tech_terms("Go", "Django", ""))
    # 即使业界常将二者视为同一技术，也不能替用户补写未出现的拼写。
    _equal("别名不放行", ["Postgres"], new_tech_terms("Postgres", "PostgreSQL", ""))
    _equal("符号技术名", [], new_tech_terms("C++ C# Node.js", "C++ C#", "Node.js"))
    _equal("句末标点", [], new_tech_terms("Redis.", "Redis", ""))
    # 不能拼接两份原文；超长英文词也不能只取已有前缀。
    _equal("跨原文拼词", ["Redis"], new_tech_terms("Redis", "Re", "dis"))
    # 长词反例与来源共享较长前缀，能够揭露正则长度截断造成的误放行。
    long_term = "LongTechnologyNameForRevisionCheckExtra"
    _equal("长词完整核对", [long_term], new_tech_terms(long_term, long_term[:-5], ""))
    _equal("稳定去重", ["Kubernetes"], new_tech_terms("Kubernetes kubernetes Kubernetes", "", ""))
    # 常量仍保留任务书规定的候选形态，不替换为第三方技术名词库。
    _equal("技术正则常量", r"[A-Za-z][A-Za-z0-9+#.\-]{1,20}", TECH_TOKEN.pattern)
    return "编造词拦截；简历∪JD/大小写通过；子串/别名/跨原文拼词拒绝"


def _check_proper() -> str:
    """核对④；无参数，返回中文专名边界摘要，不符时抛 AssertionError。"""
    # 用户要求的两条核心反例：后缀名称与引号项目均不能凭空出现。
    _equal("编造银行", ["某某银行"], new_proper_nouns("某某银行", "", ""))
    _equal("编造项目", ["XX 项目"], new_proper_nouns("《XX 项目》", "", ""))
    _equal("银行和项目原文已有", [], new_proper_nouns("某某银行、《XX 项目》", "任职某某银行", "参与《XX 项目》"))
    # 逐项检查固定后缀，避免只对验收中的「银行」写特殊分支。
    for suffix in PROPER_SUFFIX:
        name = f"星河{suffix}"
        _equal(f"后缀 {suffix}/编造", [name], new_proper_nouns(name, "", ""))
        _equal(f"后缀 {suffix}/简历依据", [], new_proper_nouns(name, name, ""))
        _equal(f"后缀 {suffix}/JD 依据", [], new_proper_nouns(name, "", name))
    # 四组引号均沿用任务书常量；正文出现即可成为依据，无需同样带引号。
    for left, right in (("《", "》"), ("「", "」"), ("『", "』"), ('"', '"')):
        quoted = f"{left}订单项目{right}"
        _equal(f"引号 {left}{right}/编造", ["订单项目"], new_proper_nouns(quoted, "", ""))
        _equal(f"引号 {left}{right}/已有", [], new_proper_nouns(quoted, "订单项目", ""))
    # 普通中文职责可以重写；本块不尝试分词或判断无后缀中文是不是名称。
    _equal("普通中文不校验", [], new_proper_nouns("推动需求澄清，优化交付流程", "接口开发", ""))
    # 简历末尾与 JD 开头恰好拼成名称，也不能构成任何一份原文的依据。
    _equal("跨原文拼专名", ["星河大学"], new_proper_nouns("星河大学", "星河", "大学"))
    _equal("不去空格拼专名", ["星河大学"], new_proper_nouns("星河大学", "星河 大学", ""))
    # 不允许通过剥去名称前面的未知部分，把一个新专名缩成已有专名。
    _equal("不剥前缀放行", ["伪造星河大学"], new_proper_nouns("伪造星河大学", "星河大学", ""))
    # 同一专名被后缀法与引号法同时命中时只返回一次。
    _equal("专名去重", ["某某银行"], new_proper_nouns("《某某银行》", "", ""))
    _equal("固定后缀词表", ("公司", "科技", "集团", "大学", "学院", "银行", "医院", "某某", "股份", "有限"), PROPER_SUFFIX)
    _equal("四组引号常量", ["项目名称"], QUOTED.findall("「项目名称」"))
    return "后缀词表与四组引号均覆盖；普通中文不校验；白名单不拼接、不扩展"


def _check_numbers() -> str:
    """核对⑤；无参数，返回数字归一摘要，不符时抛 AssertionError。"""
    # 核心验收同时检查双向替换，不能只给「两秒」写特例。
    _equal("两秒转 2 秒", [], new_numbers("耗时 2 秒", "响应两秒"))
    _equal("2 秒转两秒", [], new_numbers("耗时两秒", "响应 2 秒"))
    _equal("编造 200 毫秒", ["200"], new_numbers("耗时 200 毫秒", "响应两秒"))
    # 遍历公开数字表，检查二与两、十及其他中文数字。
    for chinese, arabic in CN_DIGITS.items():
        _equal(f"{chinese}→{arabic}", [], new_numbers(f"{arabic} 秒", f"{chinese}秒"))
        _equal(f"{arabic}→{chinese}", [], new_numbers(f"{chinese}秒", f"{arabic} 秒"))
    # 十的组合使用位值；大单位检查避免把两百误当成二。
    for chinese, arabic in (("十二", "12"), ("二十", "20"), ("二十二", "22"), ("零", "0"),
                             ("两百", "200"), ("一千零二", "1002"), ("二〇二四", "2024")):
        _equal(f"整数归一 {chinese}", [], new_numbers(arabic, chinese))
    _equal("两百不是二", ["2"], new_numbers("2 毫秒", "两百毫秒"))
    # 反向量级变化也要拦截：已有 200 不能成为新增 2 的依据。
    _equal("数字不按子串", ["2"], new_numbers("2 秒", "200 秒"))
    _equal("小数不拼接", ["25"], new_numbers("25 秒", "2.5 秒"))
    # 已有小数不表示其整数部分是另一项可以任意使用的事实。
    _equal("小数不截断", ["2"], new_numbers("2 秒", "2.5 秒"))
    # 日期范围两端是完整数字，不因连字符而合并或截断年份。
    _equal("日期原有年份", [], new_numbers("2021 年", "2021-2024"))
    _equal("日期编造年份", ["2025"], new_numbers("2021-2025", "2021-2024"))
    # 不相关数量之间的空白和标点必须保留分隔意义。
    _equal("空白不拼数字", ["23"], new_numbers("23 个", "2 年、3 个"))
    _equal("数字稳定去重", ["200", "3"], new_numbers("200 3 200", "2 秒"))
    # D-M3-01 四条新增回归留在数字组，保留 M0 原八组的验收口径。
    for label, after, original, expected in (
            ("XX 万级", "支撑日均 XX 万级下单请求", "负责接口开发", []),
            ("5 万单原文已有", "支撑日均 5 万单", "支撑日均 5 万单", []),
            ("200 万单原文没有", "支撑日均 200 万单", "支撑日均 5 万单", ["2000000"]),
            ("XX%", "请求量降低 XX%", "负责接口开发", [])):
        _equal(f"D-M3-01/{label}", expected, new_numbers(after, original))
    # 不能仅同写法比较通过，还必须确实解析成完整数值而非两个 token。
    _equal("5 万整体数值", ["50000"], _number_tokens("5 万"))
    _equal("1 千整体数值", ["1000"], _number_tokens("1 千"))
    _equal("量级跨写法", [], new_numbers("5 万单，1 千请求，0.5 万条", "50000 单，1000 请求，5000 条"))
    # 各种占位符整体删除，但不能顺便删掉旁边的真实未知数字。
    for placeholder in ("XX", "XX万", "XX 万", "XX%", "XX 万级"):
        _equal(f"整体占位符 {placeholder}", [], _number_tokens(placeholder))
        _equal(f"占位符旁数字 {placeholder}", ["200"], new_numbers(placeholder + "，200 毫秒", "120 毫秒"))
    _equal("已有百分比", [], new_numbers("下降 40%", "访问量下降 40%"))
    _equal("不虚构百分比", ["50"], new_numbers("下降 50%", "访问量下降 40%"))
    _equal("原文 120 不得改为 200", ["200"], new_numbers("200 毫秒", "120 毫秒"))
    return "两秒↔2 秒及中文整数通过；200/小数/年份/量级伪造被拦截；D-M3-01 新增 4/4（XX 万级、5 万单、200 万单、XX%）"


def _check_issue() -> str:
    """核对⑥；无参数，返回意见综合检查摘要，不符时抛 AssertionError。"""
    resume, jd = "使用 Go 开发接口，响应耗时两秒。", "负责 Go 接口开发，熟悉 Redis"
    issue = _issue()
    # 保存输入快照，检查纯函数调用不会修改模型。
    before = issue.model_dump()
    _equal("意见全部合法", (True, ""), validate_issue(issue, resume, jd))
    _equal("意见输入不变", before, issue.model_dump())
    _equal("原句优先且原因含原句", (False, "原句无法在简历原文中检索到"),
           validate_issue(_issue(original_text="没有这句", suggested_text="Kubernetes 200 秒"), resume, jd))
    # 名词白名单只取原文，不采信 why / jd_evidence 里的自述。
    _equal("意见技术词", (False, "新增技术词无原文依据：Kubernetes"),
           validate_issue(_issue(suggested_text="使用 Kubernetes", jd_evidence="Kubernetes"), resume, jd))
    _equal("意见公司名", (False, "新增专名无原文依据：某某银行"),
           validate_issue(_issue(suggested_text="某某银行"), resume, jd))
    _equal("意见新数字", (False, "新增数字无原文依据：200"),
           validate_issue(_issue(suggested_text="响应 200 毫秒"), resume, jd))
    # JD 可以提供技术名词，但不能为候选人提供不存在的成绩数字。
    _equal("意见 JD 技术依据", (True, ""), validate_issue(_issue(suggested_text="Go 与 Redis"), resume, jd))
    # 同一数字只存在于 JD 时，必须与两份原文均不存在时一样被拒绝。
    _equal("JD 数字无效", (False, "新增数字无原文依据：200"),
           validate_issue(_issue(suggested_text="响应 200 毫秒"), resume, jd + "，响应 200 毫秒"))
    _equal("missing 可空建议", (True, ""), validate_issue(_issue(category="missing", suggested_text=""), resume, jd))
    return "合法意见通过；原句优先；技术名/专名/数字逐类拦截；JD 数字不放行"


def _check_adaptation() -> str:
    """核对⑦；无参数，返回改写综合检查摘要，不符时抛 AssertionError。"""
    resume, jd = "使用 Go 开发接口，响应耗时两秒。", "负责 Go 接口开发，熟悉 Redis"
    adapted = _adapted()
    # before 与 after 都走真实护栏，元数据为空不能绕过正文核对。
    before = adapted.model_dump()
    _equal("改写全部合法", (True, ""), validate_adaptation(adapted, resume, jd))
    _equal("改写输入不变", before, adapted.model_dump())
    _equal("before 不可检索", (False, "原句无法在简历原文中检索到"),
           validate_adaptation(_adapted(before="没有这句"), resume, jd))
    # 三种伪造均在 after 重新抽取，不依赖 added_terms / kept_numbers 的完整性。
    for text, reason in (("Kubernetes", "新增技术词无原文依据：Kubernetes"),
                         ("某某银行", "新增专名无原文依据：某某银行"),
                         ("响应 200 毫秒", "新增数字无原文依据：200")):
        _equal(f"改写正文 {text}", (False, reason), validate_adaptation(_adapted(after=text), resume, jd))
    _equal("改写 JD 技术依据", (True, ""), validate_adaptation(_adapted(after="Go 与 Redis"), resume, jd))
    # 意见校验和实际改写的数字来源必须一致，不能在第二条入口放宽。
    _equal("改写 JD 数字无效", (False, "新增数字无原文依据：200"),
           validate_adaptation(_adapted(after="响应 200 毫秒"), resume, jd + "，响应 200 毫秒"))
    return "合法改写通过；before 不可检索拒绝；忽略虚假记录、重查正文"


def _check_job_input() -> str:
    """核对⑧；无参数，返回岗位组合与阈值摘要，不符时抛 AssertionError。"""
    jd = "招聘后端开发工程师，负责接口开发与系统性能优化。"
    error = ("", "请至少填写岗位名称，或粘贴岗位 JD")
    # 四种组合逐项断言，JD 与 title 同在时仍优先真实 JD。
    _equal("岗位+JD", ("user_jd", None), resolve_job_input("后端开发", jd))
    _equal("只有岗位", ("inferred", None), resolve_job_input("后端开发", None))
    _equal("只有 JD", ("user_jd", None), resolve_job_input(None, jd))
    _equal("都没填/422 文案", error, resolve_job_input(None, None))
    # 输入冲突不在纯规则层推断语义或产生错误。
    _equal("岗位与 JD 不一致", ("user_jd", None), resolve_job_input("财务会计", jd))
    _equal("全空白", error, resolve_job_input(" \n ", " \t "))
    # strip 后阈值必须精确命中：title 两字、JD 二十字。
    _equal("岗位两字", ("inferred", None), resolve_job_input(" 后端 ", ""))
    _equal("岗位一字", error, resolve_job_input(" 后 ", None))
    _equal("JD 二十字", ("user_jd", None), resolve_job_input(None, " " + "职" * 20 + " "))
    _equal("JD 十九字", error, resolve_job_input(None, " " + "职" * 19 + " "))
    # 未达 JD 阈值可以回退；有效 JD 则不应被过短岗位名阻挡。
    _equal("短 JD 回退岗位", ("inferred", None), resolve_job_input("后端", "职" * 19))
    _equal("短岗位不影响有效 JD", ("user_jd", None), resolve_job_input("后", jd))
    return "四组合、名称冲突、strip 后 2/20 字阈值及 422 文案全部正确"


def main() -> int:
    """运行八组检查；无参数，返回退出码 0/1，异常逐组打印后继续，不向外抛出。"""
    # 显式使用 UTF-8，保证 Windows 终端中的中文期望与实际可读。
    sys.stdout.reconfigure(encoding="utf-8")
    groups = (("① 契约", _check_contract), ("② 原句检索", _check_original),
              ("③ 英文技术词", _check_tech), ("④ 中文专名", _check_proper),
              ("⑤ 数字", _check_numbers), ("⑥ 意见校验", _check_issue),
              ("⑦ 改写校验", _check_adaptation), ("⑧ 岗位输入", _check_job_input))
    # 通过数只在组内全部断言成功后递增，不用打印成功代替真实验证。
    passed = 0
    # 每组独立捕获异常，某组失败也能看到其余组的真实结果。
    for label, check in groups:
        try:
            summary = check()
        except Exception as exc:
            print(f"{label}：失败；哪条={label}；期望=本组全部断言通过；实际={type(exc).__name__}: {exc}")
        else:
            passed += 1
            print(f"{label}：通过；{summary}")
    # 性能约束从业务导入之前计时；超过一秒时不能返回成功退出码。
    elapsed = perf_counter() - STARTED
    print(f"耗时 {elapsed:.6f} 秒（含业务模块导入）")
    if elapsed >= 1:
        print(f"失败：哪条=耗时；期望=<1 秒；实际={elapsed:.6f} 秒")
    # 按任务书要求，最后一行始终保留固定的通过数格式。
    print(f"通过 {passed}/{len(groups)}")
    return 0 if passed == len(groups) and elapsed < 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
