"""N0 十九组离线验收：使用固定内存样例，不调用模型、不写任何产物。"""

from __future__ import annotations

from copy import deepcopy
from difflib import SequenceMatcher
from pathlib import Path
import sys
from time import perf_counter
from typing import Callable

# 计时包含 Pydantic 和业务模块导入，不只统计断言本身。
STARTED = perf_counter()  # 使用单调高精度计时，避免系统时钟调整干扰。
# 允许从其他目录直接执行脚本，且不生成 __pycache__ 文件。
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Windows 管道默认可能使用 GBK，显式 UTF-8 保留十九个圈号及中文输出。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from pydantic import BaseModel, ValidationError

from app.interview_validate import (
    check_coverage, check_duplicate, check_id_prefix, check_quota,
    normalize_question, validate_plan,
)
from app.schemas_interview import (
    CATEGORY_BY_PART, ID_PREFIX, PART_ORDER, QUOTA, SCORE_WEIGHTS,
    AnswerItem, AnswerSheet, ConsistencyIssue, GradeItem, GradeReport,
    GradeTotal, InterviewMeta, InterviewPlan, NextAction, Question, RejectedItem,
)


def _expect(condition: object, message: str) -> None:
    """使用不会被 python -O 移除的断言，失败时提供原因。"""
    if not condition:
        raise AssertionError(message)


def _reject(model: type[BaseModel], payload: dict, field: str) -> None:
    """要求真实模型构造失败，且错误定位包含指定字段。"""
    try:
        model.model_validate(payload)
    except ValidationError as error:
        # 只接受字段校验错误，其他异常不能被当作通过。
        _expect(any(field in item["loc"] for item in error.errors()), f"错误未定位到 {field}")
    else:
        raise AssertionError(f"{model.__name__}.{field} 未拦截非法输入")


def build_valid_plan() -> InterviewPlan:
    """构造固定的六条 HR 与 6/2/2 技术面样例，供验收及 JSON 展示复用。"""
    # 固定题干仅用于契约验收，N0 不实现出题服务或调用生成模型。
    texts = {
        "hr": (
            ("motivation", "这次考虑离开当前岗位时，你最希望新工作解决哪些职业发展问题？"),
            ("salary", "结合岗位职责与自身经验，你对固定薪资和整体薪酬有哪些具体期望？"),
            ("plan", "未来一段时间内，你准备如何安排专业能力建设与职业方向选择？"),
            ("collaboration", "请讲一次团队成员对交付方案存在分歧时，你推动协作的实际过程。"),
            ("years_gap", "面对岗位要求与个人工作年限之间的差距，你会怎么证明胜任能力？"),
            ("timeline", "请讲简历中相邻两段工作经历之间的时间安排以及当时的主要活动。"),
        ),
        "tech": (
            ("tech_depth", "在高并发订单查询中，你如何分析数据库索引失效与执行计划的关系？"),
            ("tech_gap", "若岗位要求引入消息队列而简历尚未体现，你会怎么验证迁移方案的可靠性？"),
            ("impl_detail", "请讲你实现接口幂等处理时使用的请求标识、状态保存与重试处理流程。"),
            ("tradeoff", "业务数据需要同时支持快速读取和可靠持久化时，为什么选择当前缓存策略？"),
            ("debug", "线上服务出现间歇性超时且日志没有明显错误时，你会如何逐步缩小排查范围？"),
            ("growth", "谈谈最近学习的一项技术，从实验验证到实际使用之间还需要补齐哪些工作。"),
        ),
        "scenario": (
            ("scenario_design", "客户需要建设内部知识检索服务，你会怎么拆解需求并组织最小可用方案？"),
            ("scenario_risk", "交付现场发现外部系统无法按期提供关键接口，你会如何说明风险并调整计划？"),
        ),
        "project": (
            ("project_random_1", "请讲订单平台项目中你负责的业务边界、关键决策，以及最终验证交付的方式。"),
            ("project_random_2", "谈谈知识检索项目从需求澄清到上线验收的过程，并说明个人承担的具体职责。"),
        ),
    }
    # 示范文字不含专名、成果数字或承诺，只作为长度合法的合成字段样例。
    reference = (  # 所有题复用合成示范，去重只比较 question 字段。
        "我会先交代任务的背景、目标和自己负责的范围，再说明当时掌握的事实与限制。"
        "接着按照识别问题、比较方案、执行验证和检查结果的顺序描述自己的做法，"
        "并解释每一步选择的原因。如果需要引用规模或效果，我只使用能够核实的原始记录，"
        "没有记录的部分会明确说明暂时无法确认。最后补充过程中遇到的风险、采用的应对方式，"
        "以及后续仍然需要改进的地方，让面试官能够沿着真实工作过程继续追问。"
    )
    parts: dict[str, list[Question]] = {}  # 每次调用都构造新对象，反例修改互相隔离。
    for part in PART_ORDER:
        parts[part] = []
        for index, (category, text) in enumerate(texts[part], 1):
            # 每个样例都走真实字段校验，不能用 model_construct 跳过约束。
            parts[part].append(Question(
                id=f"{ID_PREFIX[part]}{index:02d}", part=part, category=category,
                difficulty=2, question=text, assesses="问题分析与事实表达",
                basis="参与业务需求分析、方案实现与交付验证。", basis_source="resume",
                answer_points=["说明背景与个人职责", "解释方案选择与具体做法", "给出可核实的结果与反思"],
                reference_answer=reference, bonus=["说明取舍依据"], pitfalls=["把团队成果说成个人成果"],
                follow_ups=["你如何验证上述判断？", "出现偏差时会怎么调整？"],
                flag="简历未提及，考察迁移能力" if category == "tech_gap" else None,
                source="injected",  # 样例由固定代码提供，与 model 来源明确区分。
            ))
    return InterviewPlan(
        # 固定令牌仅用于内存验收，不创建线上会话或持久化记录。
        plan_id="0123456789abcdef0123456789abcdef", job_title="后端开发工程师",
        source="user_jd", profile_confirmed=True, difficulty="standard", parts=parts,
        coverage={code: 1 for part in QUOTA for code in CATEGORY_BY_PART[part]}, rejected=[],
        meta=InterviewMeta(status="ok", model=None, prompt_version="interview_v1.0", injected_count=16),
        pdf_url=None, next_action=NextAction(type="answer", suggest=["Q-T-01"]),
    )


def _question_payload(**updates: object) -> dict:
    """复制合法技术深度题的原始字段，再替换本组需要验证的字段。"""
    payload = build_valid_plan().parts["tech"][0].model_dump()
    # 每次重新构造，防止前一用例污染后一用例。
    return {**payload, **updates}


def _contract_edges(plan: InterviewPlan) -> None:
    """补查十九组之外的字段边界，归入合法契约组，不增加用例组数。"""
    question = plan.parts["tech"][0].model_dump()
    for length in (20, 120):
        # 端点必须接受，同时覆盖要点、参考答案和列表数量的两个端点。
        Question.model_validate({**question, "question": "测" * (length - 1) + "？"})
    for count, length, difficulty in ((3, 120, 1), (6, 300, 3)):
        Question.model_validate({**question, "answer_points": ["点" * 30] * count,
                                 "reference_answer": "答" * length, "difficulty": difficulty})
    for field in ("bonus", "pitfalls", "follow_ups"):
        for count in (1, 3):
            Question.model_validate({**question, field: ["说明"] * count})
        for count in (0, 4):
            _reject(Question, {**question, field: ["说明"] * count}, field)
    for field, value in (("category", "scenario_design"), ("difficulty", 0),
                         ("difficulty", 4), ("assesses", "能" * 21), ("basis", " \t")):
        _reject(Question, {**question, field: value}, field)  # 每个反例只改当前目标字段。
    # 默认 None 不能让 tech_gap 漏填标记后绕过校验。
    gap = plan.parts["tech"][1].model_dump()
    gap.pop("flag")  # 与显式传入 None 分开检查 Pydantic 默认值路径。
    _reject(Question, gap, "flag")
    payload = plan.model_dump()
    for keys in (("hr", "tech", "scenario"), (*PART_ORDER, "unknown")):
        parts = {key: payload["parts"].get(key, []) for key in keys}
        _reject(InterviewPlan, {**payload, "parts": parts}, "parts")
    misplaced = deepcopy(payload)  # 防止浅拷贝改写其他用例复用的合法数据。
    misplaced["parts"]["tech"][0] = payload["parts"]["scenario"][0]
    _reject(InterviewPlan, misplaced, "parts")  # 单题合法，但所在容器分区错误。
    for field, value in (("coverage", {}), ("job_title", "  ")):
        _reject(InterviewPlan, {**payload, field: value}, field)
    # 直接覆盖所有附属契约，核实枚举、默认值和报告序列化。
    issue = ConsistencyIssue(kind="new_tech", value="待核实技术")  # 保留具体冲突项契约。
    rejected = RejectedItem(part="tech", reason="依据缺失")
    _expect(bool(rejected.reason), "拒绝原因丢失")  # 覆盖拒绝记录的字段命名。
    item = GradeItem(id="Q-T-01", part="tech", score=7, level="可投", depth=2,
                     consistency={"ok": False, "issues": [issue.model_dump()]})
    total = GradeTotal(tech=70, hr_hit_rate=0.5, band="可投", answered=1, total_questions=16)
    report = GradeReport(plan_id=plan.plan_id, items=[item], total=total)
    _expect(GradeReport.model_validate_json(report.model_dump_json()) == report, "评分报告不能往返")  # 核实嵌套字典序列化。
    for field, value in (("depth", -1), ("depth", 3), ("score", -1)):
        _reject(GradeItem, {**item.model_dump(), field: value}, field)
    for field, value in (("tech", -1), ("tech", 101), ("hr_hit_rate", -0.1), ("hr_hit_rate", 1.1)):
        _reject(GradeTotal, {**total.model_dump(), field: value}, field)
    for depth in (0, 2):
        for score in (0, 10):
            GradeItem(id="Q-T-01", part="tech", score=score, level="可投", depth=depth)
    for tech, rate in ((0, 0.0), (100, 1.0)):
        GradeTotal(tech=tech, hr_hit_rate=rate, band="可投", answered=1, total_questions=16)
    for action in ("answer", "answer_remaining", "can_grade", "done", "restart"):
        NextAction(type=action)
    _expect(sum(SCORE_WEIGHTS.values()) == 10, "评分维度上限不合计十分")
    _expect(AnswerItem(id="Q-T-01", answer="").answer == "", "空作答必须允许")
    AnswerItem(id="Q-T-01", answer="答" * 2000)  # 上限本身可用，避免把边界写成开区间。
    # 可变默认值必须隔离，避免答案状态串到另一个会话。
    first, second = AnswerSheet(plan_id=plan.plan_id, answers=[]), AnswerSheet(plan_id=plan.plan_id, answers=[])
    first.missing_ids.append("Q-T-01")  # 只修改第一份答案表，第二份必须保持空列表。
    _expect(second.missing_ids == [], "答案表共享默认列表")
    a = GradeItem(id="Q-T-01", part="tech", score=0, level="危险")
    b = GradeItem(id="Q-T-02", part="tech", score=0, level="危险")
    a.consistency["issues"].append(issue.model_dump())  # 检查的是嵌套默认列表，不只是外层 dict。
    _expect(b.consistency == {"ok": True, "issues": []}, "评分共享嵌套默认值")


def case_01() -> None:
    """① 合法题册、JSON 往返、纯函数无副作用和其他字段边界。"""
    plan = build_valid_plan()
    before = plan.model_dump()  # 保存调用前快照，核实纯函数不会修改字段。
    _expect(validate_plan(plan) == [], "合法题册被拒绝")
    _expect(plan.model_dump() == before, "纯函数修改了输入")
    _expect(InterviewPlan.model_validate_json(plan.model_dump_json()) == plan, "JSON 不能往返")
    _expect(check_quota(plan.parts) == [] and check_coverage(plan.parts) == [], "分项检查失败")
    _contract_edges(plan)  # 在主合法用例内补查任务书没有单列的字段约束。
    # HR 合法上下界均检查；新增题干与现有题目不同，避免干扰去重。
    plan.parts["hr"] = plan.parts["hr"][:5]  # 下界验收与标准六条 HR 样例分开。
    _expect(validate_plan(plan) == [], "HR 五题应合法")
    extra = (("other", "入职后面对陌生的组织流程和沟通习惯，你会如何建立有效的反馈渠道？"),
             ("other", "当个人任务优先级与团队临时安排发生冲突时，你通常依据什么原则协调？"))
    plan = build_valid_plan()
    for index, (category, text) in enumerate(extra, 7):
        data = {**plan.parts["hr"][0].model_dump(), "id": f"Q-HR-{index:02d}", "category": category, "question": text}
        plan.parts["hr"].append(Question.model_validate(data))
    _expect(validate_plan(plan) == [], "HR 八题应合法且允许重复类别")


def case_02() -> None:
    """② tech 五题应报告明确配额错误。"""
    plan = build_valid_plan()
    plan.parts["tech"].pop()  # 删除第六条，不依赖模型错误构造缺额反例。
    _expect(any("tech 需要 6 条" in error for error in validate_plan(plan)), "tech 缺额未拦截")


def case_03() -> None:
    """③ scenario 三题应报告超额。"""
    plan = build_valid_plan()
    plan.parts["scenario"].append(plan.parts["scenario"][0])  # 直接检查配额文案，避免其他错误代替通过。
    _expect(any("scenario 需要 2 条" in error for error in validate_plan(plan)), "scenario 超额未拦截")


def case_04() -> None:
    """④ project 一题应报告缺额。"""
    plan = build_valid_plan()
    plan.parts["project"].pop()  # 四个分区仍齐全，仅项目题数量不足。
    _expect(any("project 需要 2 条" in error for error in validate_plan(plan)), "project 缺额未拦截")


def case_05() -> None:
    """⑤ HR 四题应报告少于下限。"""
    plan = build_valid_plan()
    plan.parts["hr"] = plan.parts["hr"][:4]  # 四条比 HR_MIN 少一，必须失败。
    _expect(any("hr 需要 5~8 条" in error for error in validate_plan(plan)), "HR 少于下限未拦截")


def case_06() -> None:
    """⑥ HR 九题应报告超过上限。"""
    plan = build_valid_plan()
    plan.parts["hr"].extend(plan.parts["hr"][:3])  # 六加三得到九条，检查 HR_MAX 上界。
    _expect(any("hr 需要 5~8 条" in error for error in validate_plan(plan)), "HR 超过上限未拦截")


def case_07() -> None:
    """⑦ 相同考点出现两次应报告考点重复。"""
    plan = build_valid_plan()
    data = plan.parts["tech"][2].model_dump()  # 使用非缺口题，避免 flag 干扰重复考点检查。
    plan.parts["tech"][2] = Question.model_validate({**data, "category": "tech_depth"})
    _expect(any("考点重复" in error for error in validate_plan(plan)), "重复考点未拦截")


def case_08() -> None:
    """⑧ 错误 id 前缀同时由字段模型和纯函数拦截。"""
    payload = build_valid_plan().model_dump()
    payload["parts"]["tech"][0]["id"] = "Q-S-01"
    _reject(InterviewPlan, payload, "part")  # id 在 part 之前声明，关联错误定位于 part。
    # 直接修改现有对象，仅用于独立验证纯函数，不冒充合法模型。
    question = build_valid_plan().parts["tech"][0]
    question.id = "Q-S-01"  # 测试构造后对象被改写时的纯函数检查。
    _expect(any("前缀" in error for error in check_id_prefix([question])), "纯函数漏查前缀")


def case_09() -> None:
    """⑨ 同一题册内编号重复应报告错误。"""
    plan = build_valid_plan()
    plan.parts["tech"][1].id = plan.parts["tech"][0].id  # 前缀仍正确，只触发编号唯一性约束。
    _expect(any("id 重复" in error for error in validate_plan(plan)), "重复 id 未拦截")


def case_10() -> None:
    """⑩ 十五字、一百三十字与无疑问语气的题干均非法。"""
    for length in (15, 130):
        # 保留问号，确保这两个反例因长度失败。
        _reject(Question, _question_payload(question="题" * (length - 1) + "？"), "question")
    _reject(Question, _question_payload(question="这是一个描述已有项目实现过程与业务结果的普通陈述句。"), "question")


def case_11() -> None:
    """⑪ 两条、七条、三十一字单条以及换行要点均非法。"""
    for points in (["要点"] * 2, ["要点"] * 7, ["点" * 31, "分析", "验证"]):
        _reject(Question, _question_payload(answer_points=points), "answer_points")
    # 包括 Windows CRLF 与 Unicode 换行，防止多行段落绕过短句约束。
    for newline in ("\n", "\r\n", "\u2028"):
        _reject(Question, _question_payload(answer_points=[f"前{newline}后", "分析", "验证"]), "answer_points")


def case_12() -> None:
    """⑫ 一百字或三百一十字的参考答案均非法。"""
    for length in (100, 310):  # 上下界之外分别失败，不能只测其中一端。
        _reject(Question, _question_payload(reference_answer="答" * length), "reference_answer")


def case_13() -> None:
    """⑬ 长度合规但含标准答案或必过的示范作答均非法。"""
    for word in ("标准答案", "必过"):
        # 长度合规，确保触发的是禁用词规则。
        _reject(Question, _question_payload(reference_answer="答" * 120 + word), "reference_answer")


def case_14() -> None:
    """⑭ tech_gap 缺失或填写错误标记均非法。"""
    for flag in (None, "", "其他标记"):  # None、空串和错误非空值均不满足固定标记。
        _reject(Question, _question_payload(category="tech_gap", flag=flag), "flag")


def case_15() -> None:
    """⑮ 非 tech_gap 不允许携带任何非空或空字符串标记。"""
    for flag in ("简历未提及，考察迁移能力", "未定位到第二段项目", ""):  # 非缺口题必须使用 None。
        _reject(Question, _question_payload(flag=flag), "flag")
    # PRD 项目降级描述不覆盖本次更明确的 flag 限定。
    project = build_valid_plan().parts["project"][1].model_dump()
    _reject(Question, {**project, "flag": "未定位到第二段项目"}, "flag")


def case_16() -> None:
    """⑯ 归一化后相似度恰为 0.85，验证默认阈值和含等号边界。"""
    # 两个二十字符序列中十七字相同；独立手算比例为 2*17/40=0.85。
    left_text, right_text = "abcdefghijklmnopqrst", "abcdefghijklmnopqxyz"
    left = Question.model_validate(_question_payload(id="Q-T-91", question="请，" + left_text.upper() + "？"))
    right = Question.model_validate(_question_payload(id="Q-T-92", question="谈谈：" + right_text + "？"))
    normalized_left, normalized_right = normalize_question(left.question), normalize_question(right.question)
    _expect((normalized_left, normalized_right) == (left_text, right_text), "归一化错误")
    _expect(normalize_question(" \t请介绍一下，说说 ABC？！\n") == "abc", "组合前缀未去除")
    ratio = SequenceMatcher(None, normalized_left, normalized_right).ratio()  # 独立计算而不读取被测函数内部值。
    _expect(ratio == 0.85, "反例相似度并非 0.85")
    _expect(any("重复" in error for error in check_duplicate([left, right])), "默认阈值未拦截")
    _expect(bool(check_duplicate([left, right], threshold=0.85)), "相似度等于阈值应拦截")
    _expect(check_duplicate([left, right], threshold=0.86) == [], "可配置阈值未生效")
    # 再测默认阈值恰为 0.8 的边界，防止默认值被误改。
    equal = Question.model_validate(_question_payload(id="Q-T-93", question="说说abcdefghijklmnopwxyz？"))
    _expect(SequenceMatcher(None, left_text, normalize_question(equal.question)).ratio() == 0.8, "边界样例错误")
    _expect(bool(check_duplicate([left, equal])), "默认阈值等号未拦截")
    _expect(check_duplicate([left, equal], threshold=0.81) == [], "0.8 被错误视为大于 0.81")


def case_17() -> None:
    """⑰ 单题答案二千零一字非法。"""
    _reject(AnswerItem, {"id": "Q-T-01", "answer": "答" * 2001}, "answer")  # 不以截断代替拒绝。


def case_18() -> None:
    """⑱ 单题评分十一分非法。"""
    _reject(GradeItem, {"id": "Q-T-01", "part": "tech", "score": 11, "level": "强"}, "score")  # 十分上限保持不变。


def case_19() -> None:
    """⑲ 三十一位、含大写及末尾换行的令牌在三个载体均非法。"""
    plan = build_valid_plan()
    total = GradeTotal(tech=0, hr_hit_rate=0, band="危险", answered=0, total_questions=16)
    models = ((InterviewPlan, plan.model_dump()), (AnswerSheet, {"answers": []}),
              (GradeReport, {"items": [], "total": total.model_dump()}))
    # 同一正则覆盖题册与答案表，评分报告也不能引用错误令牌。
    for token in ("a" * 31, "A" + "a" * 31, "a" * 32 + "\n"):
        for model, payload in models:
            _reject(model, {**payload, "plan_id": token}, "plan_id")


def main() -> int:
    """执行十九组验收，打印全部结果及固定格式末行，失败时返回非零。"""
    cases: tuple[Callable[[], None], ...] = (
        case_01, case_02, case_03, case_04, case_05, case_06, case_07,
        case_08, case_09, case_10, case_11, case_12, case_13, case_14,
        case_15, case_16, case_17, case_18, case_19,
    )
    passed = 0  # 只统计整个用例组没有异常的情况。
    for index, case in enumerate(cases, 1):
        try:
            case()  # 十九组各自独立执行，某组失败不会掩盖后续结果。
        except Exception as error:
            # 输出失败详情并继续剩余组；不得把异常转换成成功计数。
            print(f"{index:02d} 失败：{case.__doc__} {type(error).__name__}: {error}")
        else:
            passed += 1  # 子断言全部满足后才记为一组通过。
            print(f"{index:02d} 通过：{case.__doc__}")
    duration_ms = (perf_counter() - STARTED) * 1000  # 从秒转换为任务书要求的毫秒。
    # 所有业务导入均为本次两个纯模块，没有模型客户端、网络或环境变量读取。
    print(f"通过 {passed}/19；耗时 {duration_ms:.1f}ms；模型调用 0；成本 0 元")
    return 0 if passed == 19 else 1  # 让终端和 CI 都能识别验收失败。


if __name__ == "__main__":
    raise SystemExit(main())
