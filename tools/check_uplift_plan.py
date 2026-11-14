"""M9a 验收：公式、反例、M3 字段闸门及完整 17/17 真实回归。

默认运行全部检查；--offline 明确跳过真实 M3 回归，不将跳过项计为通过。
"""

from __future__ import annotations

import argparse
import ast
from copy import deepcopy
import io
import os
from pathlib import Path
import subprocess
import sys
import tokenize
from types import SimpleNamespace

# 固定仓库路径，终端从其他目录调用时仍能导入与写入本次验收产物。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.dont_write_bytecode = True

from app.schemas_match import JobInfo
from app.schemas_revision import DiagnosisReport, ReviewIssue, UpliftItem, UpliftPlan
from app.scoring_rules import score_hard
from app.uplift_plan import apply_score_impact, build_uplift_plan

# M9a 只消费这些既有评分属性，不导入评分模型或调用语义推理。
DUTIES = ["负责订单系统接口开发与性能优化", "负责数据库查询优化与缓存设计"]


def _expect(condition: object, message: str) -> None:
    """条件不成立时抛异常，python -O 也不会跳过验收。"""
    if not condition:
        raise AssertionError(message)


def _inputs(*, skills=None, covered=(True, False), years=3, education="本科") -> dict:
    """提供真实硬性评分和规则诊断结构，语义采用明确标注的受控上游结果。"""
    # 已有 Go，缺 MySQL；两个职责中一个覆盖，一个缺失，便于独立手算。
    job = JobInfo(title="后端工程师", company=None, required_years_min=3,
                  required_years_max=None, education="本科", must_have=skills if skills is not None else ["Go", "MySQL"],
                  nice_to_have=[], responsibilities=list(DUTIES[:len(covered)]))
    resume = SimpleNamespace(years=years, skills=["Go"])
    # 不伪造 hard.deductions，始终调用现有零模型硬性评分器得到它们。
    text = f"姓名：合成候选人\n{education}\n技能：Go\n负责订单系统接口开发"
    hard = score_hard(resume, job, text)
    sem = SimpleNamespace(score=12 if covered == (True, False) else 24,
                          per_item=[SimpleNamespace(item=duty, covered=flag, best_sim=0.8 if flag else 0.71)
                                    for duty, flag in zip(job.responsibilities, covered)])
    # 上游覆盖由用例直接给定，不需要为测试涨分策略执行任何 embedding。
    # score 只供上限文案使用，potential 必须由 JD 总数而非该值导出。
    # 诊断本身不会增加评分项；后续单独反例检验 ATS 缺词不得冒充缺技能。
    diagnosis = DiagnosisReport(findings=[], keyword_coverage=[], missing_keywords=[], completeness={}, meta={})
    return dict(hard=hard, sem=sem, job=job, diagnosis=diagnosis, llm_score=22)


def _issue(**changes) -> ReviewIssue:
    """生成合法旧契约意见，可覆盖类别和证据以隔离回填行为。"""
    # 不主动传 score_impact，以真实验证新增字段的默认值。
    row = dict(id="R-001", category="missing", severity="high", location="技能 · 第 1 段",
               original_text="技能：Go", suggested_text="", why="JD 要求 MySQL，简历未体现",
               gain="未体现技能 → 核实真实经历后补充", jd_evidence="MySQL",
               risk_note="如确有相关经历请补充，否则不建议写")
    return ReviewIssue(**{**row, **changes})


def _check_schema() -> str:
    """检查默认零兼容旧意见，新增两模型逐字段符合 §6.2.4。"""
    _expect(_issue().score_impact == 0, "旧意见缺少默认零")
    # 兼容旧输入不是允许模型自报分数；后面的 Q 组单独验证输入裁剪。
    # 此处同时覆盖模型的导入、构造和可选字段的默认行为。
    # 名称和顺序均独立写明，不能把实现的字段列表当成自己的期望。
    _expect(list(UpliftItem.model_fields) == ["part", "item", "current", "potential", "action", "requirement", "blocked"], "UpliftItem 契约漂移")
    _expect(list(UpliftPlan.model_fields) == ["items", "achievable_if_true", "unreachable", "ceiling_note", "notes", "meta"], "UpliftPlan 契约漂移")
    return "旧意见默认 score_impact=0；两模型字段与 PRD 一致"


def _check_hard() -> str:
    """检查 missing_skill 按 S3.5 同族分组计算，而非原技能数。"""
    data = _inputs(skills=["Go", "MySQL", "Kafka", "RabbitMQ"])
    plan = build_uplift_plan(**data)
    rows = [row for row in plan.items if row.part == "hard"]
    # 不含 blocked 的本例中，所有 hard 项都必须来自真实的 missing_skill。
    # Go 已覆盖不会产生条目；同族的两个队列名只出现一个组项。
    # 四个原技能合为三组；只缺 MySQL 和消息队列两组，15/3=5。
    _expect(len(rows) == 2 and [row.potential for row in rows] == [5, 5], "同族分母或缺项数错误")
    _expect(all(not row.blocked and "真实" in row.action and "真的用过" in row.requirement for row in rows), "技能缺真实前提")
    return "4 项技能合为 3 组；两条 missing_skill 各 5 分"


def _check_deduction_values() -> str:
    """检查每条技能潜力与 S3 实际扣分相等，包含最后一条吸收余数的情况。"""
    data = _inputs(skills=["Go", "MySQL", "Redis", "Python", "Docker", "Git"])
    rows = [row for row in build_uplift_plan(**data).items if row.part == "hard"]
    # 六个非族技能的分母保持六，不得做通用去重或把 Go 排除出分母。
    # 不按排序位置配对，而以原项名称独立对照，避免排序掩盖条目错配。
    expected = {row.item: row.deduct for row in data["hard"].deductions if row.type == "missing_skill"}
    _expect(len(rows) == 5 and {row.item: row.potential for row in rows} == expected, "潜力与 S3 扣分未逐项相等")
    _expect(sorted(row.potential for row in rows) == [0, 3, 3, 3, 3], "未复用 S3 最后一条余数")
    return "5 条技能 potential 与 deduct 逐项相等，含最后一条余数"


def _check_one_missing() -> str:
    """D-M9-01：JD 两项必备只缺一项时，精确潜力为七分而非平均八分。"""
    data = _inputs()
    deduction = next(row for row in data["hard"].deductions if row.type == "missing_skill")
    plan = build_uplift_plan(**data)
    rows = [row for row in plan.items if row.part == "hard"]
    # 使用真实 S3 评分器产生的扣分，不能在测试中手填一个七分模型来凑通过。
    _expect(len(rows) == 1 and rows[0].potential == deduction.deduct == 7, "缺一项仍按平均值给八分")
    _expect(apply_score_impact([_issue()], plan)[0].score_impact == 7, "派生 score_impact 未同步精确扣分")
    return "JD 2 项、缺 1 项：potential=deduct=7；score_impact=7"


def _check_all_missing() -> str:
    """D-M9-01：两项全不命中时潜力必须守恒为技能项总分十五。"""
    data = _inputs()
    data["hard"] = score_hard(SimpleNamespace(years=3, skills=[]), data["job"], "本科")
    plan = build_uplift_plan(**{**data, "llm_estimated": True})
    rows = [row for row in plan.items if row.part == "hard"]
    # S3 第一条八分、最后一条七分；不得把两条都平均取整成八分。
    _expect(len(rows) == 2 and sorted(row.potential for row in rows) == [7, 8], "两项扣分尾差未保留")
    _expect(sum(row.potential for row in rows) == 15 and plan.achievable_if_true == 27, "技能潜力不守恒或汇总错误")
    return "JD 2 项、缺 2 项：potential=8+7=15；含语义合计=27"


def _check_blocked() -> str:
    """分别触发年限不足/未知、学历不足/未知四种阻断反例。"""
    # 每个反例通过硬性评分器产生实际类型，不手工给 blocked 赋值来凑通过。
    for years, education, types in ((1, "大专", {"years_short", "education"}),
                                     (None, "", {"years_unknown", "education_unknown"})):
        data = _inputs(years=years, education=education)
        _expect(types <= {row.type for row in data["hard"].deductions}, "反例未触发真实扣分")
        plan = build_uplift_plan(**data)
        # 通过项目名找到硬门槛，不依赖它们在最终排序中的具体位置。
        # 非零扣分也必须保留零潜力，避免把已扣分数直接当作可恢复分数。
        rows = [row for row in plan.items if row.part == "hard" and row.item in ("工作年限", "学历")]
        _expect(len(rows) == 2 and all(row.blocked and row.potential == 0 for row in rows), "年限/学历被计作涨分")
        _expect(all(any(row.item in note for note in plan.unreachable) for row in rows), "不可达说明丢失")
    return "years_short/years_unknown/education/education_unknown 全部 blocked、0 分"


def _check_semantic() -> str:
    """检查分母含全部职责，covered 布尔值不能用余弦大小重判。"""
    data = _inputs()
    # 未覆盖项故意给很高余弦，覆盖项给很低余弦，仍只认上游 covered。
    data["sem"].per_item[0].best_sim = 0.1
    data["sem"].per_item[1].best_sim = 0.99
    rows = [row for row in build_uplift_plan(**data).items if row.part == "semantic"]
    # 分母是全部两条职责，不能只对一条未覆盖职责取平均得到二十四分。
    # 原文必须可回查，以便后续 missing 意见按 jd_evidence 关联。
    _expect(len(rows) == 1 and rows[0].potential == 12 and DUTIES[1] in rows[0].item, "语义公式或覆盖来源错误")
    _expect("真实" in rows[0].action and "新写" in rows[0].requirement, "缺少新经历前提")
    return "只取 covered=False；round(30/2×0.8)=12"


def _check_ceiling() -> str:
    """全职责覆盖时不造语义涨分，必须给明确上限说明。"""
    plan = build_uplift_plan(**_inputs(covered=(True, True)))
    # 两条都覆盖时语义项为空；低于三十分也不能当作尚有一条缺失职责。
    # 余弦剩余空间只进文案，不产生确定的改写涨分条目。
    _expect(not any(row.part == "semantic" for row in plan.items), "全覆盖还列新增覆盖")
    _expect(plan.ceiling_note == "语义分已接近上限（24/30），再改措辞只会 ±1", "上限说明不符合 PRD")
    return "全 covered 无语义潜力项；ceiling_note 含 24/30 与 ±1"


def _check_llm() -> str:
    """LLM 剩余潜力可展示，但本次不重跑所以必须 blocked。"""
    plan = build_uplift_plan(**_inputs())
    row = next(row for row in plan.items if row.part == "llm")
    # 必须同时检查差额和 blocked，只有差额正确仍可能污染合计。
    # action 的本次不重算说明也属于本版 LLM 行为契约。
    _expect(row.potential == 8 and row.blocked and "本次不重算" in row.action, "LLM 差额或阻断错误")
    # 硬性沿用 S3 实际扣分 7，语义 12；LLM 的 8 不参与合计。
    _expect(plan.achievable_if_true == 19, "blocked LLM 混入可达合计")
    return "30-22=8，blocked；achievable=7+12=19"


def _check_estimated() -> str:
    """缺测 LLM 不显示，零分占位和 None 都不能伪装成三十分潜力。"""
    for value in (None, 0):
        # 缺测标记优先于占位分数；None 和零两条路径都必须可用。
        # 不借调用方填零来推测真的存在三十分可获空间。
        plan = build_uplift_plan(**{**_inputs(), "llm_score": value, "llm_estimated": True})
        _expect(not any(row.part == "llm" for row in plan.items), "缺测 LLM 仍出现")
        _expect(any("LLM 不可用" in note for note in plan.notes), "缺测说明丢失")
    return "llm_estimated=True 不出 LLM 项；notes 明示不可用"


def _check_empty() -> str:
    """空职责/空必备项不除零，也不能声称语义已接近上限。"""
    plan = build_uplift_plan(**{**_inputs(skills=[], covered=()), "llm_estimated": True})
    # all([]) 虽然为真，但空 JD 不意味着职责已全部覆盖。
    # 即使没有可列项目，也仍需留下没有可比对职责的原因。
    _expect(plan.items == [] and plan.achievable_if_true == 0 and not plan.ceiling_note, "空输入被冒充全覆盖")
    _expect(any("无可比对职责" in note for note in plan.notes), "空职责缺说明")
    return "空职责、空技能无除零；items=[]、合计=0、无假全覆盖"


def _check_limit() -> str:
    """检查降序、同分稳定、十条上限及截断后的合计与不可达审计。"""
    data = _inputs(skills=["MySQL"], years=1, education="大专")
    # 十二条职责均缺失，单条 round(30/12×0.8)=2；技能项 15 排第一。
    duties = [f"职责 {index}" for index in range(12)]
    data["job"].responsibilities = duties
    data["sem"].per_item = [SimpleNamespace(item=duty, covered=False, best_sim=0.2) for duty in duties]
    plan = build_uplift_plan(**{**data, "llm_estimated": True})
    # 十五个候选截去五个；其中年限、学历不能从不可达说明中消失。
    # 合计只包含用户看得到的十条，不可暗含另外三条未展示职责。
    _expect(len(plan.items) == 10 and [row.potential for row in plan.items] == [15] + [2] * 9, "降序/上限错误")
    _expect([row.item for row in plan.items[1:]] == [f"JD 职责：{duty}" for duty in duties[:9]], "同分不稳定")
    _expect(plan.achievable_if_true == 33 and plan.meta["truncated_count"] == 5, "展示合计或截断审计错误")
    _expect(all(any(word in note for note in plan.unreachable) for word in ("年限", "学历")), "截断导致不可达说明丢失")
    return "降序稳定、最多 10 条；合计=15+9×2=33；截断限制仍可查"


def _check_impact() -> str:
    """只给对应 missing 赋原项 potential，表达类、未匹配和 blocked 归零。"""
    plan = build_uplift_plan(**_inputs(years=1))
    issues = [_issue(), _issue(jd_evidence=DUTIES[1])]
    # 用过期非零值验证每条都会重算归零，而非直接保留旧 score_impact。
    issues.extend(_issue(category=category, score_impact=99) for category in ("weak_evidence", "buried", "noise", "inconsistent"))
    issues.extend([_issue(jd_evidence="Rust", score_impact=99), _issue(jd_evidence="工作年限", score_impact=99)])
    result = apply_score_impact(issues, plan)
    # 期望按输入顺序写死，能发现函数私自排序、漏项或只更新部分意见。
    # 两条 missing 分别回查硬性和语义，不能把两个潜力加在同一意见上。
    _expect([row.score_impact for row in result] == [7, 12, 0, 0, 0, 0, 0, 0], "回填来源或归零错误")
    return "missing 命中 7/12；其余四类、未匹配、blocked 全为 0"


def _check_matching() -> str:
    """技能别名和同族明示原名可匹配，短词子串与歧义多配不猜分。"""
    plan = build_uplift_plan(**_inputs(skills=["Golang", "Kafka", "RabbitMQ"]))
    # 原输入 Go 已命中，只有消息队列缺失；Kafka 与 RabbitMQ 回链同一组。
    result = apply_score_impact([_issue(jd_evidence="Kafka"), _issue(jd_evidence="RabbitMQ")], plan)
    _expect([row.score_impact for row in result] == [7, 7], "族原名无法回链")
    data = _inputs(skills=["Go"])
    data["hard"] = score_hard(SimpleNamespace(years=3, skills=[]), data["job"], "本科")
    go_plan = build_uplift_plan(**data)
    # GoLang 沿用既有规范化别名；Django 则只是包含字母 go 的无关词。
    # 这条边界防止用任意子串推断原本不存在的技术缺口。
    result = apply_score_impact([_issue(jd_evidence="GoLang"), _issue(jd_evidence="Django")], go_plan)
    _expect([row.score_impact for row in result] == [15, 0], "别名漏配或 Go/Django 子串误配")
    # 同一证据同时对应硬性/语义两项时不相加，也不随机选一项。
    duplicate = go_plan.items[0].model_copy(update={"part": "semantic", "item": "JD 职责：Go"})
    ambiguous = go_plan.model_copy(update={"items": [*go_plan.items, duplicate]})
    _expect(apply_score_impact([_issue(jd_evidence="Go")], ambiguous)[0].score_impact == 0, "歧义匹配擅自报分")
    return "技能别名/组内原名可回链；Django 不碰 Go；歧义不叠加"


def _check_purity() -> str:
    """相同输入三次逐字段一致，输入评分、诊断、策略与意见全部不变。"""
    data = _inputs()
    snapshot = deepcopy(data)
    plans = [build_uplift_plan(**data) for _ in range(3)]
    _expect(plans[0] == plans[1] == plans[2] and data == snapshot, "计算不确定或污染输入")
    issues = [_issue(score_impact=91)]
    before = deepcopy((issues, plans[0]))
    # 快照包含嵌套 Pydantic 列表，能捕获只复制最外层造成的隐蔽修改。
    # 不检查对象地址是否恒定；相同内容应返回可独立修改的新对象。
    result = apply_score_impact(issues, plans[0])
    _expect((issues, plans[0]) == before and result[0] is not issues[0], "原地回填输入")
    # 结果可被调用方继续修改，不影响下一次返回或原输入。
    result[0].risk_note = "调用方修改"
    plans[0].notes.append("调用方修改")
    _expect(build_uplift_plan(**data) == plans[1] and issues[0].risk_note == before[0][0].risk_note, "输出共享可变状态")
    return "三次逐字段相等；评分/诊断/策略/意见输入不变；输出独立"


def _check_notes() -> str:
    """独立对照 PRD 原文解释，诊断及 ATS 缺词不凭空产生潜力。"""
    data = _inputs()
    plan = build_uplift_plan(**data)
    # PRD 含 Markdown 加粗，去掉标记后按实际原文检索 notes 的固定解释。
    prd = (PROJECT_ROOT / "prd/PRD_简历修改意见_v1.md").read_text(encoding="utf-8").replace("**", "")
    _expect(all(note in prd for note in plan.notes[:5]), "§3.4 解释未原样保留")
    data["diagnosis"].missing_keywords = ["Rust", "任意关键词"]
    other = build_uplift_plan(**data)
    # 只允许诊断统计说明变化，不能为 ATS 缺词增加新的潜力项。
    # 完整输出检索营销承诺，同时要求所有展示项都有明确前提。
    _expect(other.items == plan.items and other.achievable_if_true == plan.achievable_if_true, "ATS 缺词擅自计分")
    _expect("保证涨" not in other.model_dump_json() and all(row.requirement for row in other.items), "承诺涨分或缺前提")
    return "§3.4 五段解释逐字对照 PRD；ATS 缺词不另算；无保证涨分"


def _check_invalid() -> str:
    """越界 LLM 与矛盾分母显式失败，不生成负潜力或静默除零。"""
    # 逐个独立输入验证，不能因前一例修改状态造成假失败。
    cases = [{**_inputs(), "llm_score": value} for value in (-1, 31, 22.0, True, None)]
    bad_group = _inputs()
    bad_group["job"].must_have = []
    bad_duty = _inputs()
    bad_duty["job"].responsibilities = []
    for case in [*cases, bad_group, bad_duty]:
        # 缺分母属于矛盾输入，不能回退为一或悄悄拿最大分替代。
        # 显式 ValueError 使调用方有机会处理问题，不把坏数据写成涨分建议。
        try:
            build_uplift_plan(**case)
        except ValueError:
            pass
        else:
            raise AssertionError("非法分数或矛盾分母被接受")
    return "5 类非法 LLM、缺技能分母、缺职责分母均 ValueError"


def _check_zero_model() -> str:
    """新进程禁导模型模块且封锁网络，实际调用计算与回填证明零模型。"""
    # 在全新解释器执行，避免前面的 M3 导入缓存掩盖运行时模型依赖。
    code = '''import sys, socket, runpy
class DenyModels:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'openai', 'fastembed', 'onnxruntime'} or fullname in {'app.scoring_semantic', 'app.rag'}:
            raise AssertionError('unexpected model import: ' + fullname)
sys.meta_path.insert(0, DenyModels())
def denied(*args, **kwargs):
    raise AssertionError('unexpected network')
socket.socket.connect = denied
socket.create_connection = denied
scope = runpy.run_path('tools/check_uplift_plan.py', run_name='isolated_check')
plan = scope['build_uplift_plan'](**scope['_inputs']())
scope['apply_score_impact']([scope['_issue']()], plan)
assert plan.meta['cost_yuan'] == 0 and plan.meta['model_calls'] == 0
print('isolated zero-model execution passed')
'''
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=PROJECT_ROOT, capture_output=True,
                            encoding="utf-8", errors="replace", timeout=30)
    # 断言真实子进程退出码，不仅依赖源码里没有模型调用字样。
    # 标记文本只有计算和回填实际完成后才输出，导入成功不算完成验收。
    _expect(result.returncode == 0 and "execution passed" in result.stdout, result.stdout + result.stderr)
    return "全新进程封锁网络及模型导入；实际计算/回填成功，成本 0"


def _check_m3_fields() -> str:
    """不含 score_impact 的真实模型候选必须通过 M3，模型自报值必须被忽略。"""
    # 只在 M3 闸门用例导入调用层，纯计算模块始终不依赖它。
    from app import revision_llm as revision
    original = "负责订单系统接口开发"
    candidate = dict(category="weak_evidence", severity="medium", location="工作经历 · 第 1 段",
                     original_text=original, suggested_text="负责订单系统接口开发，说明接口交付范围",
                     why="JD 要求负责订单系统接口开发与性能优化；原句只列职责，缺少交付范围",
                     gain="只列职责 → 说明接口交付范围", risk_note="只保留真实交付内容，不虚构成果",
                     jd_evidence=DUTIES[0])
    kept, rejected = revision._guard_candidates([candidate], original, DUTIES, [candidate])
    # 候选不含新增字段，专门防止缺少必填字段 score_impact 的整批失活。
    # 非空 kept 与空 rejected 必须同时成立，不能用拒绝审计冒充正常接受。
    _expect("score_impact" not in revision._ISSUE_FIELDS and "score_impact" not in candidate, "派生字段混入提取白名单")
    _expect(len(kept) == 1 and not rejected and kept[0].score_impact == 0, f"合法候选被拒：{rejected}")
    # 额外自报字段仍经过既有白名单裁剪，不能用默认值兼容为由接受模型加分。
    for value in (99, "99", None):
        spoof = dict(candidate, id="fake", source="injected", total=100, score_impact=value)
        kept, rejected = revision._guard_candidates([spoof], original, DUTIES, [candidate])
        _expect(len(kept) == 1 and kept[0].score_impact == 0 and "source" not in kept[0].model_dump(), "模型自报派生值或来源被采纳")
    return "不含 score_impact 的候选正常接受；模型伪报分数/来源均丢弃"


def _check_m3_strict() -> str:
    """逐字段反例证明 M3 可选默认和字符串校验未放宽。"""
    from app import revision_llm as revision
    # 用已有 M3 固定合法候选，逐项删除或改类型，护栏均执行真实实现。
    from tools.check_revision_llm import _fixture
    text, _, job, _, _, candidate = _fixture()
    _expect(revision._OPTIONAL_DEFAULTS == {"location": "—", "risk_note": "涉及数字、规模、成果的建议，必须是你真实的数据"}, "可选默认字典被改动")
    for field in revision._ISSUE_FIELDS:
        # 包括可缺省字段在内，显式错误类型都必须拒绝，不能套用默认值。
        # 删除字段则只允许原有的 location/risk_note 两项，核心字段继续必填。
        wrong = {**candidate, field: 123}
        kept, rejected = revision._guard_candidates([wrong], text, job.responsibilities, [candidate])
        _expect(not kept and len(rejected) == 1, f"字段 {field} 非字符串仍放行")
        missing = {key: value for key, value in candidate.items() if key != field}
        kept, rejected = revision._guard_candidates([missing], text, job.responsibilities, [candidate])
        _expect(bool(kept) == (field in ("location", "risk_note")), f"必填字段 {field} 规则改变：{rejected}")
    return "全部提取字段拒绝非字符串；仅 location/risk_note 可缺省"


def _check_docs() -> str:
    """用 tokenizer 和 AST 检查新增文件注释比例与所有 docstring。"""
    stats = []
    for relative in ("app/uplift_plan.py", "tools/check_uplift_plan.py", "app/schemas_revision.py"):
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        # 分母含空行；字符串中的井号不会被 tokenizer 误算为注释。
        comments = {token.start[0] for token in tokenize.generate_tokens(io.StringIO(source).readline) if token.type == tokenize.COMMENT}
        ratio = len(comments) / len(source.splitlines())
        nodes = [node for node in ast.walk(ast.parse(source)) if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
        # 模块、类、私有辅助函数和入口全部计入，不能只看公开函数。
        # 同时验证新增数据契约文件，防止只给计算函数补注释而遗漏模型。
        _expect(ratio >= 0.15 and all(ast.get_docstring(node) for node in nodes), f"{relative} 注释 {ratio:.1%} 或缺 docstring")
        stats.append(f"{Path(relative).name} {ratio:.1%}")
    return "；".join(stats) + "；docstring 全部 100%"


def _check_regression(output_dir: Path) -> str:
    """实跑未经跳过的完整 M3 脚本，保存原始输出，核验退出码和 17/17 末行。"""
    print("M3 回归执行中：包含 A/C1/D4 的真实调用……", flush=True)
    # 不使用 --offline、mock 或历史输出，子进程实际执行原有十七个分组。
    result = subprocess.run([sys.executable, "-B", "tools/check_revision_llm.py"], cwd=PROJECT_ROOT,
                            capture_output=True, encoding="utf-8", errors="replace", timeout=600,
                            env={**os.environ, "PYTHONUTF8": "1"})
    log_path = output_dir / "M9a_check_revision_llm.txt"
    log_path.write_text(result.stdout + result.stderr, encoding="utf-8")
    # 末行来自子进程实际 stdout，不能在本脚本无条件打印一个 17/17。
    last_line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "（无输出）"
    print(f"check_revision_llm.py 末行：{last_line}", flush=True)
    _expect(result.returncode == 0 and last_line == "通过 17/17", f"M3 回归失败，完整输出：{log_path}")
    return "真实子进程退出码 0；末行 通过 17/17"


def main() -> int:
    """执行所有检查，打印完整结果，任一失败或完整回归未通过均返回非零。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="跳过 M3 真实回归，不计为通过")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "output/m9a")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    # 每个检查有独立标签；一项失败仍执行后续项，完整暴露回归情况。
    checks = [("A 契约", _check_schema), ("B 硬性分组", _check_hard), ("C 逐项扣分", _check_deduction_values),
              ("D 年限学历", _check_blocked), ("E 语义公式", _check_semantic), ("F 语义上限", _check_ceiling),
              ("G LLM 阻断", _check_llm), ("H LLM 缺测", _check_estimated), ("I 空输入", _check_empty),
              ("J 排序限额", _check_limit), ("K 分数回填", _check_impact), ("L 回链边界", _check_matching),
              ("M 纯函数", _check_purity), ("N 原文解释", _check_notes), ("O 非法输入", _check_invalid),
              ("P 零模型", _check_zero_model), ("Q M3 白名单", _check_m3_fields),
              ("R M3 严格字段", _check_m3_strict), ("S 注释文档", _check_docs),
              ("U 缺一项精确值", _check_one_missing), ("V 全缺技能守恒", _check_all_missing)]
    passed = 0
    for label, action in checks:
        try:
            detail = action()
        except Exception as exc:
            print(f"{label}：失败；{type(exc).__name__}: {exc}", flush=True)
        else:
            passed += 1
            print(f"{label}：通过；{detail}", flush=True)
    executed = len(checks)
    if args.offline:
        # 不能把零模型部分的通过数冒充包括真实回归的全部通过。
        print("T M3 完整回归：跳过（--offline；不计为通过）", flush=True)
    else:
        executed += 1
        try:
            detail = _check_regression(args.output_dir)
        except Exception as exc:
            print(f"T M3 完整回归：失败；{type(exc).__name__}: {exc}", flush=True)
        else:
            passed += 1
            print(f"T M3 完整回归：通过；{detail}", flush=True)
    print(f"通过 {passed}/{executed}" + ("（离线；真实回归未执行）" if args.offline else ""), flush=True)
    return 0 if passed == executed else 1


if __name__ == "__main__":
    # 失败必须反映到进程退出码，不能只输出描述再返回成功。
    raise SystemExit(main())
