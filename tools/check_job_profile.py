"""离线检查 M2 岗位画像的十四组契约、隔离、确认与确定性要求。

失败逐项打印哪条、期望、实际；末行汇总通过数，性能不达标也返回非零。
"""

import ast
from hashlib import sha256
import json
from pathlib import Path
import sys
from time import perf_counter

# 与 M0/M1 自检保持相同口径：在业务模块导入前计时，包含 Pydantic 加载。
STARTED = perf_counter()
# 支持在任意工作目录运行；自检不生成项目内的字节码文件。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.dont_write_bytecode = True

from app.guard_revision import resolve_job_input
from app.job_profile import (
    GENERIC_MUST_HAVE, GENERIC_RESPONSIBILITIES, INFERRED_NOTE,
    LOW_CONFIDENCE_SUFFIX, PROFILE_TEMPLATES, PROFILE_VERSION,
    ProfileNotConfirmed, confirm_profile, infer_profile, match_family,
    profile_banner, profile_hash, require_confirmed,
)
from app.schemas_revision import JobProfile


def _equal(label: str, expected: object, actual: object) -> None:
    """比较条目；参数为名称、期望、实际，返回无，不同则抛含双方值的 AssertionError。"""
    # 不使用 assert，避免解释器开启优化后检查被静默跳过。
    if actual != expected:
        raise AssertionError(f"哪条={label}；期望={expected!r}；实际={actual!r}")


def _raises(label: str, error: type[Exception], action, *args,
            message: str | None = None, **kwargs) -> str:
    """验证异常；传入条目、异常类、调用及参数，返回错误文字，拒绝不符时抛断言。"""
    try:
        action(*args, **kwargs)
    except error as exc:
        # 只接收指定异常类型，其他异常由主循环作为失败打印。
        if message is not None:
            _equal(f"{label}/错误文案", message, str(exc))
        return str(exc)
    raise AssertionError(f"哪条={label}；期望=抛出 {error.__name__}；实际=未抛异常")


def _check_backend() -> str:
    """检查①与八族常量；无参数，返回覆盖摘要，失败抛 AssertionError。"""
    # 族名和各族样例独立列出，不能从模板反向生成同一份期望。
    titles = dict(backend="后端开发工程师", frontend="前端开发工程师",
                  algorithm="机器学习算法工程师", data="数据分析师", test="测试工程师",
                  ops="运维工程师", product="产品经理", delivery="解决方案工程师")
    # 名称集合必须完全相等，多加一族也视为违反 M2 边界。
    _equal("八个规定族名", set(titles), set(PROFILE_TEMPLATES))
    _equal("画像版本", "1", PROFILE_VERSION)
    for family, title in titles.items():
        template, profile = PROFILE_TEMPLATES[family], infer_profile(title)
        # 同时核对内容、条数与身份；值相同不能代替列表隔离。
        _equal(f"{title}/命中", (family, "精确"), match_family(title))
        _equal(f"{family}/必备6~10", True, 6 <= len(template["must_have"]) <= 10)
        _equal(f"{family}/职责≥4", True, len(template["responsibilities"]) >= 4)
        _equal(f"{family}/非空labels", True, bool(template["labels"]))
        # 两种列表都纳入身份比较，不能只复制 must_have 而漏掉职责。
        for field in ("must_have", "responsibilities"):
            _equal(f"{family}/{field}/内容", template[field], getattr(profile, field))
            _equal(f"{family}/{field}/新列表", False, template[field] is getattr(profile, field))
    # 首条验收明确打印后端的条数，同时验证全部族均可产生可用画像。
    profile = infer_profile("后端开发工程师")
    return f"backend；must_have={len(profile.must_have)}、responsibilities={len(profile.responsibilities)}；8 族结构通过"


def _check_java() -> str:
    """检查②；无参数，返回同族匹配摘要，失败抛 AssertionError。"""
    # is_match 对应 match_family 返回的第二项，不擅自给 JobProfile 增添字段。
    _equal("Java 与后端同族同匹配状态", match_family("后端开发工程师"), match_family("Java 后端工程师"))
    # 不比较 title 本身，因为两个输入的名称理应分别保留。
    left, right = infer_profile("后端开发工程师"), infer_profile("Java 后端工程师")
    _equal("同族使用同一套词序", (left.must_have, left.responsibilities), (right.must_have, right.responsibilities))
    return "Java 后端工程师与后端开发工程师同为 (backend, 精确)"


def _check_generic() -> str:
    """检查③；无参数，返回兜底及列表隔离摘要，失败抛 AssertionError。"""
    title, profile = "火星矿业队长", infer_profile("火星矿业队长")
    _equal("未知族", (None, "低置信"), match_family(title))
    # 四条兜底必备项遵循常量契约，不套用已知族的六条下限。
    _equal("通用条数", True, len(GENERIC_MUST_HAVE) == 4 and 2 <= len(GENERIC_RESPONSIBILITIES) <= 4)
    _equal("通用必备与职责", (GENERIC_MUST_HAVE, GENERIC_RESPONSIBILITIES), (profile.must_have, profile.responsibilities))
    # 推测声明与低置信提示都必须写进数据，不能只在报告抬头里表达。
    _equal("兜底声明", INFERRED_NOTE + LOW_CONFIDENCE_SUFFIX, profile.note)
    _equal("兜底未确认", False, profile.confirmed)
    _equal("置信度提示", True, "置信度低" in profile.note)
    # 对通用路径也执行破坏性调用，确保通用常量没有被画像反向污染。
    profile.must_have.append("X")
    profile.responsibilities.append("X")
    # 通过追加后重新生成检验兜底隔离，不只比较对象身份。
    fresh = infer_profile(title)
    _equal("通用模板不污染", (False, False), ("X" in fresh.must_have, "X" in fresh.responsibilities))
    return "GENERIC 兜底；note 含置信度低；confirmed=False；通用列表隔离"


def _check_delivery() -> str:
    """检查④；无参数，返回大小写及全角摘要，失败抛 AssertionError。"""
    # 分别覆盖纯英文、大小写混合、全角字母与全角空格，避免只靠中文交付命中。
    for title in ("fde 交付工程师", "fde engineer", "FdE Engineer", "ＦＤＥ　工程师"):
        _equal(f"{title}/delivery", ("delivery", "精确"), match_family(title))
    # 检查四个强制入口各自可用，避免仅保存了 label 却没有实际参与匹配。
    for label in ("FDE", "交付", "实施", "解决方案"):
        _equal(f"delivery 标签 {label}", True, label in PROFILE_TEMPLATES["delivery"]["labels"])
        _equal(f"{label}/推测入口", "inferred", infer_profile(label).source)
    # 匹配归一不应擅自改写用户的岗位文字。
    _equal("全角title保留", "ＦＤＥ　工程师", infer_profile("ＦＤＥ　工程师").title)
    return "fde/FdE/ＦＤＥ 均命中 delivery；FDE/交付/实施/解决方案标签齐全"


def _check_invalid_title() -> str:
    """检查⑤；无参数，返回 M0 复用摘要，失败抛 AssertionError。"""
    # 错误文本直接与 M0 比较；本检查不维护第二份业务长度阈值或 422 文案。
    for title in ("", " ", "\t\n", "　", "后", "a"):
        _, expected = resolve_job_input(title, None)
        _equal(f"{title!r}/M0拒绝", True, expected is not None)
        # 异常必须是 ValueError，并且错误消息完整一致，不能只包含关键词。
        _raises(f"无效岗位名 {title!r}", ValueError, infer_profile, title, message=expected)
    # 校验通过后只裁剪两端空白，不动岗位内部的文本。
    _equal("有效岗位名裁边", "后端", infer_profile(" \t后端\n").title)
    return "空串/空白/单字被拒；错误逐字透传 resolve_job_input"


def _check_isolation() -> str:
    """检查⑥；无参数，返回真实追加后再推测的结果，失败抛 AssertionError。"""
    # 必须真实执行 infer→append→infer；仅检查 id 不能证明模板没有被污染。
    before = json.dumps(PROFILE_TEMPLATES, ensure_ascii=False, sort_keys=True)
    first = infer_profile("后端")
    # 先改第一份结果，再创建第二份，不能预先创建两份后只测别名关系。
    first.must_have.append("X")
    first.responsibilities.append("X")
    second = infer_profile("后端")
    # 正反两边都断言，证明追加操作执行了，且第二次结果没有继承它。
    _equal("首次追加真实发生", (True, True), ("X" in first.must_have, "X" in first.responsibilities))
    _equal("再次推测不含X", (False, False), ("X" in second.must_have, "X" in second.responsibilities))
    # 模板全量快照不变，防止某个非当前输出字段被意外写入。
    _equal("全部模板未改", before, json.dumps(PROFILE_TEMPLATES, ensure_ascii=False, sort_keys=True))
    return "infer(后端)→append(X)→再 infer；两组列表均不含 X；模板未污染"


def _check_hash() -> str:
    """检查⑦；无参数，返回默认状态及指纹摘要，失败抛 AssertionError。"""
    profiles = [infer_profile("后端开发工程师") for _ in range(3)]
    for profile in profiles + [infer_profile("火星矿业队长")]:
        _equal("草案来源和确认状态", ("inferred", False), (profile.source, profile.confirmed))
    # 每次指纹来自独立生成的画像，不能只是对同一个缓存对象重复读值。
    hashes = [profile_hash(profile) for profile in profiles]
    _equal("同输入三次hash", [hashes[0]] * 3, hashes)
    # 按契约独立列出参与指纹的字段，避免忘记字段或把 confirmed 放进指纹。
    profile = profiles[0]
    payload = dict(title=profile.title, source=profile.source, must_have=profile.must_have,
                   responsibilities=profile.responsibilities, note=profile.note)
    expected = sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    _equal("规范JSON指纹", expected, hashes[0])
    # 每个非 confirmed 字段都应该防偷换，不只检查技能列表。
    for field, value in dict(title="另一岗位", source="user_jd", note="变更声明",
                             must_have=["变更要求"], responsibilities=["变更职责"]).items():
        changed = JobProfile.model_validate({**profile.model_dump(), field: value})
        _equal(f"指纹覆盖{field}", False, hashes[0] == profile_hash(changed))
    return "source=inferred、confirmed=False；三次 hash 一致；规范 JSON 与全部内容字段校验通过"


def _check_confirm() -> str:
    """检查⑧；无参数，返回新对象与未改原稿摘要，失败抛 AssertionError。"""
    draft = infer_profile("后端")
    before, digest = draft.model_dump(), profile_hash(draft)
    confirmed = confirm_profile(draft, digest)
    _equal("确认状态", True, confirmed.confirmed)
    _equal("确认返回新对象", False, id(draft) == id(confirmed))
    # 全字段快照也覆盖 note、来源及列表顺序，而不只是 confirmed 布尔值。
    _equal("原稿整体未改", before, draft.model_dump())
    _equal("原稿仍未确认", False, draft.confirmed)
    # 此断言单独证明 confirmed 不参与 hash，避免确认后前端指纹自动失效。
    _equal("无编辑指纹不变", digest, profile_hash(confirmed))
    # 不只检查未编辑列表的身份，追加后原稿也必须仍与快照完全相同。
    for field in ("must_have", "responsibilities"):
        _equal(f"确认后{field}为新列表", False, getattr(draft, field) is getattr(confirmed, field))
        getattr(confirmed, field).append("确认结果独有")
    _equal("确认结果修改不影响原稿", before, draft.model_dump())
    return "confirmed=True；id 不同；原稿未改且未确认；无 edits 时 hash 不变；列表隔离"


def _check_edits() -> str:
    """检查⑨；无参数，返回白名单编辑及输入隔离摘要，失败抛 AssertionError。"""
    draft = infer_profile("后端")
    before, digest = draft.model_dump(), profile_hash(draft)
    edits = {"must_have": ["用户确认的真实要求"]}
    confirmed = confirm_profile(draft, digest, edits)
    _equal("确认带编辑", (True, edits["must_have"]), (confirmed.confirmed, confirmed.must_have))
    # 有内容编辑时指纹必须变化，不能为满足上组而返回与内容无关的常量。
    _equal("编辑指纹改变", False, digest == profile_hash(confirmed))
    _equal("编辑不改原稿", before, draft.model_dump())
    # 两个方向都真实追加，确保调用方之后修改 edits 或结果也不会交叉污染。
    edits["must_have"].append("编辑输入独有")
    _equal("输入追加不改结果", ["用户确认的真实要求"], confirmed.must_have)
    confirmed.must_have.append("确认结果独有")
    _equal("结果追加不改输入", ["用户确认的真实要求", "编辑输入独有"], edits["must_have"])
    # 职责单独编辑时，未编辑的必备列表仍由确认函数复制。
    responsibilities = ["用户确认的职责"]
    second = confirm_profile(draft, digest, {"responsibilities": responsibilities})
    _equal("职责也允许编辑", responsibilities, second.responsibilities)
    _equal("职责编辑复制", False, responsibilities is second.responsibilities)
    _equal("职责单改时必备也复制", False, draft.must_have is second.must_have)
    # 空列表也是明确编辑，不得用 truthiness 回退到模板要求。
    cleared = confirm_profile(draft, digest, {"must_have": [], "responsibilities": []})
    _equal("空列表编辑沿用契约", ([], []), (cleared.must_have, cleared.responsibilities))
    _equal("全部编辑后原稿未改", before, draft.model_dump())
    return "must_have/职责编辑生效、hash 变化；原稿与 edits 均隔离；空列表保留"


def _check_stale_hash() -> str:
    """检查⑩；无参数，返回旧指纹拒绝摘要，失败抛 AssertionError。"""
    draft = infer_profile("后端")
    before, digest = draft.model_dump(), profile_hash(draft)
    _raises("错误hash", ValueError, confirm_profile, draft, "wrong-hash", message="画像已变更，请重新生成")
    # 同时提交非法编辑时，仍须先返回画像变更错误，检验确认流程执行顺序。
    _raises("先验hash后验edits", ValueError, confirm_profile, draft, "wrong-hash",
            {"note": "x"}, message="画像已变更，请重新生成")
    _equal("拒绝不改原稿", before, draft.model_dump())
    # 除了随意写错的摘要，也覆盖展示后画像内容真实发生变化的路径。
    draft.must_have.append("展示之后被改动")
    _raises("旧hash拒绝被替换草案", ValueError, confirm_profile, draft, digest, message="画像已变更，请重新生成")
    # 失败请求不能在抛错之前先打开确认位。
    _equal("拒绝后仍未确认", False, draft.confirmed)
    return "错误/过期 hash 被拒；消息含画像已变更；hash 校验先于编辑校验"


def _check_illegal_edits() -> str:
    """检查⑪；无参数，返回字段白名单与错误类型摘要，失败抛 AssertionError。"""
    draft = infer_profile("后端")
    before, digest = draft.model_dump(), profile_hash(draft)
    # note 是任务书指定反例；其他不可编辑字段一并覆盖，不能偷改来源或确认位。
    for field in ("note", "title", "source", "confirmed", "unknown"):
        _raises(f"非法字段{field}", ValueError, confirm_profile, draft, digest, {field: "x"})
    _raises("混合合法非法字段", ValueError, confirm_profile, draft, digest,
            {"must_have": ["不能提前应用"], "note": "x"})
    # 字符串不得被 list() 拆开，None 和非字符串元素也不符合既有列表契约。
    for field in ("must_have", "responsibilities"):
        for value in ("Go", None, [1]):
            _raises(f"非法编辑类型{field}={value!r}", ValueError, confirm_profile, draft, digest, {field: value})
    # 一个合法字段混入非法字段时也必须整体拒绝，不允许先应用合法部分。
    _equal("非法请求不改原稿", before, draft.model_dump())
    return "note/title/source/confirmed/未知键均拒绝；非法列表拒绝；原稿未改"


def _check_required_confirmation() -> str:
    """检查⑫；无参数，返回强制确认摘要，失败抛 AssertionError。"""
    # 已知族与未知族都受确认闸门约束，不能因模板精确命中而放行。
    for title in ("后端", "火星矿业队长"):
        draft = infer_profile(title)
        _raises(f"{title}/草案闸门", ProfileNotConfirmed, require_confirmed, draft,
                message="请先确认岗位画像，再生成修改意见")
        # 放行对象必须走真实确认函数，不能在测试里手动设置 confirmed=True。
        confirmed = confirm_profile(draft, profile_hash(draft))
        _equal(f"{title}/确认后放行", None, require_confirmed(confirmed))
        _equal(f"{title}/原稿仍待确认", False, draft.confirmed)
    return "未确认抛 ProfileNotConfirmed；已知/未知族确认后均放行"


def _check_banner() -> str:
    """检查⑬；无参数，返回来源声明摘要，失败抛 AssertionError。"""
    draft = infer_profile("后端")
    expected = "本报告基于岗位画像（推测），非真实 JD；建议粘贴真实岗位要求后重新生成"
    _equal("推测报告声明", expected, profile_banner(draft))
    _equal("含推测及非真实", True, "推测" in profile_banner(draft) and "非真实" in profile_banner(draft))
    # 确认不会把推测变成真实 JD，也不能移除数据中的推测说明。
    confirmed = confirm_profile(draft, profile_hash(draft))
    _equal("确认后仍声明推测", expected, profile_banner(confirmed))
    _equal("确认后数据声明保留", INFERRED_NOTE, confirmed.note)
    # 真实 JD 是既有模型允许的另一来源，不通过 infer_profile 构造。
    real = JobProfile(title="后端", source="user_jd", confirmed=True,
                      must_have=["真实要求"], responsibilities=["真实职责"], note="真实 JD")
    _equal("真实JD无推测抬头", "", profile_banner(real))
    return "inferred 含推测和非真实，确认后仍保留；user_jd 返回空串"


def _quality() -> str:
    """检查源码规范；无参数，返回注释与文档统计，文件或断言失败时抛异常。"""
    summaries = []
    for relative in ("app/job_profile.py", "tools/check_job_profile.py"):
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        walked = list(ast.walk(tree))
        # 空行计入分母；只统计整行注释，忽略行内注释以保持注释数量的保守下界。
        string_lines = {line for node in walked if isinstance(node, ast.Constant) and isinstance(node.value, str)
                        for line in range(node.lineno, node.end_lineno + 1)}
        # AST 定位并排除字符串覆盖的行，避免把多行字符串中的井号当作注释。
        comments = {line for line, text in enumerate(source.splitlines(), 1)
                    if text.lstrip().startswith("#") and line not in string_lines}
        ratio = len(comments) / len(source.splitlines())
        # 私有辅助函数也计入 docstring 覆盖率，不能只统计公开入口。
        nodes = [node for node in walked
                 if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
        _equal(f"{relative}/注释≥15%（{ratio:.1%}）", True, ratio >= 0.15)
        _equal(f"{relative}/docstring100%", [],
               [getattr(node, "name", "模块") for node in nodes if not ast.get_docstring(node)])
        if relative == "app/job_profile.py":
            # 业务模块只允许标准库与既有护栏、模型，不间接加载评分或模型客户端。
            imports = {node.module for node in walked if isinstance(node, ast.ImportFrom)}
            imports.update(alias.name for node in walked if isinstance(node, ast.Import)
                           for alias in node.names)
            _equal("画像导入边界", {"hashlib", "json", "unicodedata", "app.guard_revision", "app.schemas_revision"}, imports)
            _equal("画像无异步", [], [type(node).__name__ for node in walked
                                      if isinstance(node, (ast.AsyncFunctionDef, ast.Await))])
            # 对象身份确认复用了 M0 的真实函数，不能复制出一套碰巧等价的阈值逻辑。
            _equal("校验函数直接复用M0", True, sys.modules["app.job_profile"].resolve_job_input is resolve_job_input)
        summaries.append(f"{Path(relative).name} 注释 {ratio:.1%}、docstring 100%")
    # 检查实际导入的模块，补充源码依赖白名单对间接副作用的约束。
    forbidden = ("app.api", "app.schemas", "app.agent", "app.providers", "app.fallback",
                 "app.scoring_rules", "app.scoring_llm", "app.scoring_semantic", "openai", "dotenv")
    _equal("未加载模型/评分/环境模块", [], [name for name in forbidden if name in sys.modules])
    return "；".join(summaries)


def _check_determinism() -> str:
    """检查⑭与规范；无参数，返回确定性摘要，失败抛 AssertionError。"""
    # 已知族与低置信兜底都连续调用三次，比较完整模型及有序列表的逐字结果。
    for title in ("后端开发工程师", "前端工程师", "火星矿业队长"):
        profiles = [infer_profile(title) for _ in range(3)]
        _equal(f"{title}/三次完整输出", [profiles[0].model_dump()] * 3, [profile.model_dump() for profile in profiles])
        # 三份列表同时存活，id 去重才有意义，避免对象释放后的 id 复用。
        for field in ("must_have", "responsibilities"):
            _equal(f"{title}/{field}/三次独立列表", 3, len({id(getattr(profile, field)) for profile in profiles}))
    # 多族命中取最长 label；等长时按模板族顺序决定，与词在 title 中的顺序无关。
    for title, expected in (("Go 解决方案工程师", "delivery"), ("Java 算法工程师", "backend"),
                            ("前端 后端工程师", "backend"), ("后端 前端工程师", "backend")):
        # 不通过遍历待测模板计算赢家，期望值独立写死以暴露匹配次序错误。
        _equal(f"{title}/最长与同长决胜", [(expected, "精确")] * 3, [match_family(title) for _ in range(3)])
    return "三次 must_have/职责逐字一致；最长及等长匹配确定；" + _quality()


def main() -> int:
    """运行十四组；无参数，返回 0/1，失败打印具体条目、期望与实际并继续其余组。"""
    started = perf_counter()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    # 分组编号与任务书一致，不把附加断言拆成更多组来虚增验收数字。
    groups = (("① 后端与八族结构", _check_backend), ("② Java 同族", _check_java),
              ("③ 低置信兜底", _check_generic), ("④ 大小写与全角", _check_delivery),
              ("⑤ 无效岗位名", _check_invalid_title), ("⑥ 模板隔离", _check_isolation),
              ("⑦ 默认状态与指纹", _check_hash), ("⑧ 无编辑确认", _check_confirm),
              ("⑨ 编辑确认", _check_edits), ("⑩ 画像变更", _check_stale_hash),
              ("⑪ 非法编辑", _check_illegal_edits), ("⑫ 强制确认", _check_required_confirmation),
              ("⑬ 报告声明", _check_banner), ("⑭ 确定性与规范", _check_determinism))
    # 汇总业务与规范断言，耗时另作退出条件，二者均须满足才返回成功。
    passed = 0
    for label, check in groups:
        # 只有整组全部通过才计数；单组失败也要给出其余组的诊断结果。
        try:
            detail = check()
        except Exception as exc:
            print(f"{label}：失败；哪条={label}；期望=本组全部断言通过；实际={type(exc).__name__}: {exc}")
        else:
            passed += 1
            print(f"{label}：通过；{detail}")
    elapsed, check_elapsed = perf_counter() - STARTED, perf_counter() - started
    # 同时披露检查阶段耗时；总耗时包含业务依赖加载，严格按总耗时判定性能。
    print(f"耗时 {elapsed:.6f} 秒（含业务模块导入；其中检查 {check_elapsed:.6f} 秒）；零模型、零网络")
    if elapsed >= 0.3:
        print(f"失败；哪条=耗时；期望=<0.3 秒；实际={elapsed:.6f} 秒")
    # 最后一行保持任务书要求的通过数字，性能失败也必须返回非零退出码。
    print(f"通过 {passed}/{len(groups)}")
    return 0 if passed == len(groups) and elapsed < 0.3 else 1


if __name__ == "__main__":
    # 让命令行、后续验收脚本均能识别业务断言或性能条件失败。
    raise SystemExit(main())
