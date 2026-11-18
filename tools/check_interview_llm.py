"""N2 验收：A01~A15 为确定性硬检查，B01~B05 为真实模型质量观察。"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from unittest.mock import patch

# 直接执行脚本也能定位项目；结果文件与个人材料均不提交。
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app import interview_llm as module
from app.interview_skeleton import build_skeleton
from app.interview_validate import validate_plan
from app.schemas_interview import CATEGORY_BY_PART, ID_PREFIX, PART_ORDER, QUOTA
from tools.check_interview_skeleton import load_beike_inputs


def _expect(condition: object, message: str) -> None:
    """失败时显式抛出异常，避免优化模式静默跳过断言。"""
    if not condition:
        raise AssertionError(message)


def _questions(plan) -> list:
    """按四分区顺序返回题目，不依赖模型或字典输入次序。"""
    # 显式分区次序与骨架保持一致，逐字依据校验才不会把排序变化当原文变化。
    return [question for part in PART_ORDER for question in plan.parts[part]]


def _candidate() -> dict:
    """构造可过前四道护栏的技术题，仅用于插入单项违规。"""
    # 复制模板防止反例改写常量，通用示范不需要引入额外事实。
    candidate = deepcopy(module.TEMPLATE_QUESTIONS["tech_depth"])
    candidate["question"] = "在使用 Agent 完成重复性开发任务时，你会怎样划分执行边界并检查交付结果？"
    candidate["answer_points"] = ["交代真实背景", "说明任务边界", "解释验证方法", "核对原始记录"]
    candidate["bonus"] = ["说明判断依据", "明确职责边界"]
    candidate["pitfalls"] = ["编造经历成果", "混淆个人贡献"]
    candidate["follow_ups"] = ["如何核对原始记录？", "方案边界是什么？"]
    return candidate


def _guard(sk, candidate: dict, *, category: str = "tech_depth", accepted: list | None = None):
    """执行真实五道单题护栏，保持每组反例使用同一个真实骨架。"""
    # 使用真骨架的依据，不能构造宽松白名单来让非法候选通过。
    slot = next(slot for slot in sk.slots if slot.category == category)
    # 反例 id 也由代码提供，不能采信伪造候选里的 id 或 part。
    return module._guard_candidate(slot, candidate, f"{ID_PREFIX[slot.part]}99", accepted or [],
                                   resume_text=INPUTS["resume_text"], jd_text=INPUTS["jd_text"])


def _valid_candidates(sk) -> list[dict]:
    """构造逐槽位相关、互不重复且满足新生成要求的完整响应。"""
    questions = {
        "motivation": "你在恒祥达担任后端开发工程师，为什么考虑新的岗位机会，会如何说明真实诉求？",
        "salary": "对于13-15K这个区间，你会如何说明考虑因素，并结合岗位职责沟通整体条件？",
        "plan": "围绕Golang开发工程师这一方向，你会如何安排近期学习实践，并核验长期职业目标？",
        "collaboration": "多端后端开发中出现跨团队协作分歧时，你会如何澄清目标、核对事实并推动共同安排？",
        "career_switch": "从已有经历转向Golang方向时，你会如何说明真实动机、可迁移经验以及需要补齐的能力？",
        "timeline": "围绕2024.04这段测试相关经历，你会如何说明时间安排、个人职责与后续工作的衔接？",
        "tech_depth": _candidate()["question"],
        "tech_gap": "如果需要搭建知识库，你会如何利用已有Agent经验确认需求并设计小范围验证？",
        "impl_detail": "合伙人申请到签约和利润分成发放的链路中，你会如何说明状态设计、异常处理及核验方式？",
        "tradeoff": "在Golang后端开发中，你会如何判断RabbitMQ与同步调用的适用边界、成本及验证方式？",
        "debug": "定时任务拉取与异步回调发生资金状态不一致时，你会如何定位原因并核验处理结果？",
        "growth": "围绕DeepSeek落地AI功能的经历，你会如何说明学习过程、效果验证以及后续改进安排？",
        "scenario_design": "如果让你深入一线业务识别值得用AI解决的问题，你会如何梳理需求、安排原型并检验实际效果？",
        "scenario_risk": "如果客户现场需要尚未熟悉的AI能力，你会如何如实说明边界、提出替代方案并约定验证节点？",
        "project_random_1": "企业端员工与薪资链路模块中，你会如何说明MySQL分库隔离的职责边界、实现过程与交付证据？",
        "project_random_2": "堡垒机多版本系统测试这段经历中，你会如何说明负责的模块、边界测试方式以及缺陷复核过程？",
    }
    candidates = []
    for slot in sk.slots:
        item = _candidate()
        item["question"] = questions[slot.category]
        if sk.difficulty == "senior":
            item["follow_ups"].append("怎样验证复杂约束？")
        candidates.append(item)
    return candidates


async def _mock_plan(sk, responses: list, *, fail: bool = False, messages_log: list | None = None,
                     initial_http_failures: int = 0):
    """只替换传输层，完整运行解析、七道护栏、模板及 N0 组装。"""
    # 这个计数只供反例断言使用，不会加到最后的真实 HTTP 次数。
    calls = 0

    async def fake(messages, state):
        """记录构造响应及计量，明确区分模拟请求与真实 HTTP 请求。"""
        nonlocal calls
        calls += 1
        state["calls"] += 1 + (initial_http_failures if calls == 1 else 0)
        if messages_log is not None:
            messages_log.append(deepcopy(messages))
        # 故障发生在取得响应之前，不应凭空计入 tokens 成本。
        if fail:
            raise RuntimeError("构造：所有厂商失败")
        response = responses[min(calls - 1, len(responses) - 1)]
        if isinstance(response, Exception):
            raise response
        # 两次响应都计入生成器费用，测试不能免费化 JSON 重试。
        state["meter"].add(SimpleNamespace(prompt_tokens=10, completion_tokens=10, prompt_cache_hit_tokens=0))
        # 同样的第二个坏响应可以重放，但业务层仍只能请求两次。
        return response

    # patch 仅存在于此调用的上下文，真实运行阶段恢复原传输函数。
    with patch.object(module, "_call_interview_model", new=fake):
        plan, cost = await module.generate_questions(sk, resume_text=INPUTS["resume_text"], jd_text=INPUTS["jd_text"])
    return plan, cost, calls


async def _exhausted_plan(sk):
    """三家假客户端全部失败，但保留真实的 fallback、退避和失败组装路径。"""
    from app import fallback, providers

    calls = []

    class UnavailableClient:
        """只在此用例中替换 HTTP 传输，不访问任何真实服务。"""

        def __init__(self, provider, timeout):
            """保留厂商标签用于验证三家顺序，超时参数不触发实际等待。"""
            self.name = provider["name"]
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def __aenter__(self):
            """匹配真实异步客户端的上下文管理协议。"""
            return self

        async def __aexit__(self, *args):
            """不吞掉故障，由原 fallback 代码决定是否切换。"""
            return False

        async def create(self, **kwargs):
            """模拟非重试错误，保证三家都被真实控制流尝试一次。"""
            calls.append(self.name)
            raise ValueError("构造：服务不可用")

    fake_providers = [dict(name=f"offline-{index}", model="offline") for index in range(3)]
    # 假 key 仅供本用例跳过配置检查，假客户端从不发起网络请求。
    with patch.object(providers, "PROVIDERS", fake_providers), patch.object(providers, "_provider_key", return_value="offline"), \
         patch.object(fallback, "_make_async_provider_client", new=UnavailableClient):
        plan, cost = await module.generate_questions(sk, resume_text=INPUTS["resume_text"], jd_text=INPUTS["jd_text"])
    return plan, cost, calls


def _request_count(plan) -> int:
    """读取服务端记录的真实请求计数，与传输层观察值独立核对。"""
    values = [int(message.split("：", 1)[1]) for message in plan.meta.warnings if message.startswith("模型请求次数：")]
    _expect(len(values) == 1, "缺少或重复记录模型请求次数")
    return values[0]


async def _rewrite_cases(sk) -> str:
    """通过传输层构造验证重写、失败降级、原索引和三次请求上限。"""
    valid = _valid_candidates(sk)
    index = next(index for index, slot in enumerate(sk.slots) if slot.category == "tech_depth")
    first = deepcopy(valid)
    first[index]["answer_points"] = first[index]["answer_points"][:2]
    corrected = deepcopy(valid[index])
    corrected["answer_points"] = ["重写后交代背景", "重写后明确边界", "重写后解释验证", "重写后提供记录"]
    messages = []
    plan, cost, calls = await _mock_plan(sk, [json.dumps(first, ensure_ascii=False),
                                           json.dumps([corrected], ensure_ascii=False)], messages_log=messages)
    _expect(calls == _request_count(plan) == 2, "两条要点没有触发第二次模型请求")
    _expect(plan.meta.status == "ok" and plan.meta.injected_count == 0 and validate_plan(plan) == [], "修复成功仍退模板")
    rewritten = _questions(plan)[index]
    _expect(rewritten.source == "model" and rewritten.answer_points == corrected["answer_points"], "不是模型返回的完整重写内容")
    _expect(len(first[index]["answer_points"]) == 2 and len(plan.rejected) == 1, "本地补齐字段或丢弃首轮拒绝记录")
    _expect("answer_points 只有 2 条（要求正好 4 条）" in plan.rejected[0].reason, "缺少实测诊断")
    retry_content = messages[1][1]["content"]
    _expect(len(re.findall(r"^\[\d+\] part=", retry_content, re.MULTILINE)) == 1
            and f"原始槽位号 {index + 1}" in retry_content and "上一轮这几题不合法" in retry_content,
            "重写消息没有限定到唯一失败槽位或丢失原始编号")
    for position, question in enumerate(_questions(plan)):
        if position != index:
            _expect(all(getattr(question, field) == value for field, value in valid[position].items()), "通过题目被修改")
    _expect(abs(cost - .0001) < 1e-12, "未累计重写响应费用")
    proof = ROOT / "reports/d_n2_04/constructed_rewrite.json"
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_text(json.dumps({"kind": "constructed_transport", "model_requests": calls,
                                 "rewritten_original_slots": [index + 1], "injected_count": plan.meta.injected_count,
                                 "status": plan.meta.status, "rejected": [item.model_dump() for item in plan.rejected],
                                 "rewrite_user_content": retry_content, "rewritten_question": rewritten.model_dump()},
                                ensure_ascii=False, indent=2), encoding="utf-8")
    # JSON 修复与字段重写共用三次总额度，不能互相挤掉必要的重写或产生第四次请求。
    plan, cost, calls = await _mock_plan(sk, ['[{"question":', json.dumps(first, ensure_ascii=False),
                                            json.dumps([corrected], ensure_ascii=False)])
    _expect(calls == _request_count(plan) == 3 and plan.meta.injected_count == 0
            and abs(cost - .00015) < 1e-12, "JSON 修复后的字段重写或累计费用不正确")

    # 非相邻失败槽位的局部编号不可覆盖原骨架上的其他题目。
    positions = [0, index]
    multiple = deepcopy(valid)
    multiple[0]["answer_points"] = multiple[0]["answer_points"][:2]
    multiple[index]["reference_answer"] = "答" * 98
    plan, _, calls = await _mock_plan(sk, [json.dumps(multiple, ensure_ascii=False),
                                         json.dumps([valid[pos] for pos in positions], ensure_ascii=False)])
    _expect(calls == 2 and plan.meta.injected_count == 0 and len(plan.rejected) == 2, "多个失败槽位未完整重写")
    _expect(all(q.basis == slot.basis for q, slot in zip(_questions(plan), sk.slots)), "重写错绑原始依据")

    # 重写仍不合法时才退模板，保留两轮诊断及明确 warning。
    invalid_retry = deepcopy(valid[index])
    invalid_retry["answer_points"] = invalid_retry["answer_points"][:2]
    plan, _, calls = await _mock_plan(sk, [json.dumps(first, ensure_ascii=False), json.dumps([invalid_retry], ensure_ascii=False)])
    _expect(calls == 2 and plan.meta.injected_count == 1 and plan.meta.status == "needs_review", "重写失败没有正常模板降级")
    _expect("tech_depth 重写后仍不合法" in plan.meta.warnings and len(plan.rejected) == 2, "缺少重写失败 warning 或诊断")

    # 重写不是放行通道：真抄写与提升 40% 在重写轮仍必须被相应护栏拒绝。
    for field, content, prefix in (("question", sk.slots[index].basis + "？", "护栏②"),
                                   ("reference_answer", valid[index]["reference_answer"] + "提升了 40%", "护栏③")):
        unsafe = deepcopy(valid[index])
        unsafe[field] = content
        plan, _, calls = await _mock_plan(sk, [json.dumps(first, ensure_ascii=False), json.dumps([unsafe], ensure_ascii=False)])
        _expect(calls == 2 and plan.meta.injected_count == 1
                and any(item.reason.startswith(prefix) for item in plan.rejected), "重写绕过抄写或事实护栏")

    # 模拟首轮已经消耗三次 HTTP 请求，不得再调用重写传输。
    plan, _, calls = await _mock_plan(sk, [json.dumps(first, ensure_ascii=False)], initial_http_failures=2)
    _expect(calls == 1 and _request_count(plan) == 3 and plan.meta.injected_count == 1, "耗尽额度后继续请求")
    # 重写服务故障不能撤销首轮已通过题目，也不能漏掉原响应费用。
    plan, cost, calls = await _mock_plan(sk, [json.dumps(first, ensure_ascii=False), RuntimeError("构造：重写服务故障")])
    _expect(calls == 2 and plan.meta.injected_count == 1 and validate_plan(plan) == []
            and abs(cost - .00005) < 1e-12, "重写故障破坏已通过题目或费用累计")

    senior = sk.model_copy(update={"difficulty": "senior"})
    senior_valid = _valid_candidates(senior)
    senior_first = deepcopy(senior_valid)
    senior_first[index]["follow_ups"] = []
    messages = []
    plan, _, calls = await _mock_plan(senior, [json.dumps(senior_first, ensure_ascii=False),
                                              json.dumps([senior_valid[index]], ensure_ascii=False)], messages_log=messages)
    _expect(calls == 2 and plan.meta.injected_count == 0 and all(len(q.follow_ups) == 3 for q in _questions(plan)),
            "senior 没有严格要求三条追问并重写")
    _expect("follow_ups 正好 3 条" in messages[0][0]["content"], "senior 提示词仍是两条")
    return "2 条要点触发第 2 次请求；仅重写失败槽位；无本地补齐；重写仍过安全护栏；额度/senior/故障回归通过"


def _noun_cases(sk) -> str:
    """隔离三条相关性通路，证明正例仅靠名词命中通过、反例全部未命中。"""
    slot = next(slot for slot in sk.slots if slot.category == "impl_detail")
    positive = _candidate()
    positive["question"] = "面对合伙人分成争议，你会怎样调查事实、协调各方诉求并解释处理结论？"
    nouns = module._noun_phrases(slot.basis)
    _expect({"合伙人", "分成", "申请"} <= nouns, f"未抽出业务名词：{nouns}")
    _expect(all(len(noun) >= 2 and noun in slot.basis for noun in nouns), "名词不是原文连续片段")
    _expect(not {"设计", "实现", "支撑", "负责"} & nouns, "把叙述动作当成名词")
    _expect(not module._noun_phrases("独立设计并实现，负责开发和维护。"), "纯动词句产生名词")
    _expect("安通" not in module._noun_phrases("长安通知中心"), "枚举任意二字子串放宽相关性")
    _expect(not module._technical_words(positive["question"]) & module._technical_words(slot.basis), "正例意外走技术词通路")
    positive_overlap = module._character_overlap(positive["question"], slot.basis)
    _expect(positive_overlap < .25, f"正例意外走字符重合通路：{positive_overlap}")
    question, reason = _guard(sk, positive, category="impl_detail")
    _expect(question is not None, f"名词命中仍被误杀：{reason}")

    # 使用非 HR 槽位承载真实薪资依据，确保拒绝确实来自三通路全不命中。
    salary = next(slot for slot in sk.slots if slot.category == "salary")
    unrelated_slot = slot.model_copy(update={"basis": salary.basis, "basis_source": salary.basis_source})
    unrelated = _candidate()
    unrelated["question"] = "Redis 集群出现脑裂时，你会如何确认节点状态、定位网络问题并恢复一致性？"
    technical_hits = module._technical_words(unrelated["question"]) & module._technical_words(salary.basis)
    noun_hits = {noun for noun in module._noun_phrases(salary.basis) if noun in unrelated["question"]}
    overlap = module._character_overlap(unrelated["question"], salary.basis)
    _expect(not technical_hits and not noun_hits and overlap < .25, "反例不是三条通路全未命中")
    rejected, reason = module._guard_candidate(unrelated_slot, unrelated, "Q-T-99", [],
                                               resume_text=INPUTS["resume_text"], jd_text=INPUTS["jd_text"])
    _expect(rejected is None and reason.startswith("护栏①"), f"薪资依据搭配 Redis 题未被相关性护栏拒绝：{reason}")
    proof = {"positive": {"basis": slot.basis, "question": positive["question"],
                           "noun_hits": sorted(noun for noun in nouns if noun in positive["question"]),
                           "character_overlap": positive_overlap, "accepted": question is not None},
             "negative": {"basis": salary.basis, "question": unrelated["question"],
                           "technical_hits": sorted(technical_hits), "noun_hits": sorted(noun_hits),
                           "character_overlap": overlap, "accepted": rejected is not None, "reason": reason}}
    destination = ROOT / "reports/n2b"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "relevance_proof.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf-8")
    return "合伙人/分成仅凭名词通过；薪资→Redis 三路全未命中，护栏①拒绝"


async def _constructed_cases(sk) -> dict[int, tuple[bool, str]]:
    """逐组运行反例，某组失败后仍继续其余组并保留具体原因。"""
    # 结果按用户要求的原编号保存，构造组与真模型组最后统一输出。
    results = {}

    def record(number: int, operation) -> None:
        """将同步反例结果记录为独立用例，不以打印文案代替断言。"""
        # 某个反例失败不阻止收集后续问题，最终退出码仍为失败。
        try:
            operation()
        except Exception as error:
            results[number] = (False, f"{type(error).__name__}: {error}")
        else:
            results[number] = (True, "构造用例通过")

    def forged_metadata():
        """④ 伪造元数据全部忽略，七个可写字段仍按真实槽位审核。"""
        candidate = _candidate()
        # 一次覆盖所有不可信元数据，特别检查 basis 不能反向扩充白名单。
        candidate.update(id="Q-HR-fake", part="hr", category="salary", basis="伪造的依据原文",
                         basis_source="jd", flag="模型自定标记", difficulty=99, source="injected")
        question, reason = _guard(sk, candidate)
        # 合法的可写部分必须保留下来，不能靠整条拒绝伪造字段冒充忽略成功。
        _expect(question is not None, reason)
        slot = next(slot for slot in sk.slots if slot.category == "tech_depth")
        _expect(question.basis == slot.basis and question.category == slot.category
                and question.part == slot.part and question.flag == slot.flag
                and question.basis_source == slot.basis_source and question.difficulty == slot.difficulty
                and question.id == "Q-T-99" and question.source == "model", "模型元数据被采信")
        # HR 相关性只认当前依据实体；技术题仍不能凭一个无关实体通过。
        for category, entity in (("motivation", "恒祥达"), ("salary", "13-15K"), ("plan", "Golang"),
                                 ("collaboration", "后端开发"), ("career_switch", "Golang"), ("timeline", "2024.04")):
            item = _candidate()
            item["question"] = f"围绕{entity}这段背景，你会怎样说明自己的选择依据、真实限制和后续安排？"
            question, reason = _guard(sk, item, category=category)
            _expect(question is not None, f"HR 实体被误杀：{category} {reason}")
            item["question"] = "你会如何说明接下来的职业目标，并判断当前选择是否支持长期方向？"
            question, reason = _guard(sk, item, category=category)
            _expect(question is None and reason.startswith("护栏①"), f"无实体 HR 题被放行：{category} {reason}")

    def copied_jd():
        """⑤ 在相关性成立时，逐字抄 JD 必须由第二道护栏拒绝。"""
        slot = next(slot for slot in sk.slots if slot.category == "tech_gap")
        candidate = _candidate()
        for suffix in ("", "？", "你会如何完成？"):
            candidate["question"] = slot.basis + suffix
            question, reason = _guard(sk, candidate, category="tech_gap")
            # 指定拒绝阶段，避免在字段长度等无关原因上失败也被记为通过。
            _expect(question is None and reason.startswith("护栏②"), reason)
        # 累计重合很高也不能当成连续照抄；分母必须是依据而非短题干。
        _expect(module.INTERVIEW_COPY_LIMIT == .7, "兼容常量被修改")
        _expect(module._copy_overlap("甲乙丙丁X戊己庚辛", "甲乙丙丁戊己庚辛") == .5, "零散匹配被累计")
        _expect(module._copy_overlap("甲乙", "甲乙丙丁戊己庚辛壬癸") == .2, "错误使用短题干作分母")
        _expect(module._copy_overlap("甲，乙 丙。", "甲乙丙") == 1, "未归一化标点空白")
        # 80% 连续引用在疑问句中允许，90% 仍须拒绝；非疑问形式从 80% 起拒绝。
        basis = "甲乙丙丁戊己庚辛壬癸"
        boundary_slot = next(slot for slot in sk.slots if slot.category == "tech_depth").model_copy(update={"basis": basis})
        for quoted, suffix, copied in ((basis[:8], "？", False), (basis[:9], "？", True), (basis[:8], "。", True)):
            item = _candidate()
            item["question"] = quoted + "相关的处理过程与风险边界分别是什么、还需要核对哪些记录" + suffix
            question, reason = module._guard_candidate(boundary_slot, item, "Q-T-99", [], resume_text=basis, jd_text="")
            _expect((question is None and reason.startswith("护栏②")) if copied else question is not None,
                    f"80%/90% 边界不符合约定：{reason}")
        # 真实 debug 依据中的两个短语必须可引用，不靠取消护栏来避免注入。
        candidate["question"] = "定时任务拉取与异步回调发生资金状态不一致时，你会如何定位原因并核验处理结果？"
        question, reason = _guard(sk, candidate, category="debug")
        _expect(question is not None, f"引用关键短语被误杀：{reason}")

    def invented_tech():
        """⑥ 题干含原文没有的 Kubernetes，必须由事实护栏拒绝。"""
        candidate = _candidate()
        # 保留原有 Agent 相关性，只追加不存在的技术词隔离第三道护栏。
        candidate["question"] += "如果改用 Kubernetes 应怎样验证？"
        question, reason = _guard(sk, candidate)
        _expect(question is None and "护栏③" in reason and "Kubernetes" in reason, reason)
        # 真实回归出现的泛称不能当作名称，候选字段保持原样。
        for phrase in ("全程避免贬低原公司", "不涉及对公司的评价", "若Python实践有限，先说明边界", "技术栈拓展空间有限",
                       "我在其中承担的模块应按真实记录说明", "我在实际工作中已用Golang和Gin搭建过核心模块",
                       "我在企业管理系统等项目中用Golang和Gin搭建过核心模块"):
            item = _candidate()
            item["reference_answer"] += phrase
            original = deepcopy(item)
            question, reason = _guard(sk, item)
            _expect(question is not None and item == original, f"泛称被当作专名或字段被修改：{reason}")
        for phrase in ("我在星河科技有限公司负责业务", "我在虚构支付系统负责实现", "我在星河公司评价原公司", "曾在自在公司负责业务",
                       "我在企业管理系统虚构模块负责实现"):
            item = _candidate()
            item["reference_answer"] += phrase
            question, reason = _guard(sk, item)
            _expect(question is None and reason.startswith("护栏③"), f"虚构实体因泛称过滤被放行：{phrase}")

    def invented_number():
        """⑦ 原文没有的量化指标不能进入题干。"""
        candidate = _candidate()
        # 原「次请求」是无单位裸计数，按新口径改用确实受限的性能指标。
        for field in ("question", "reference_answer"):
            for claim, number in (("性能提升了 40%", "40"), ("具备 987654321 年经验", "987654321"),
                                  ("支撑 987654321 QPS", "987654321"), ("延迟 987654321 ms", "987654321"),
                                  ("提升到987654321QPS的性能", "987654321"), ("降低到987654321ms的延迟", "987654321"),
                                  ("金额 987654321 元", "987654321")):
                item = deepcopy(candidate)
                item[field] += claim
                question, reason = _guard(sk, item)
                _expect(question is None and "护栏③" in reason and number in reason and "上下文" in reason, reason)
        # 原文中的序号 3 或裸数字不能替「3 年经验」背书。
        _expect(module._new_fact_numbers("具备 3 年经验", "3. 负责接口开发"), "编造年限被序号背书")
        _expect(not module._new_fact_numbers("具备 3 年经验", "三年工作经验"), "已有年限被误杀")
        _expect(not module._new_fact_numbers("2024年4月", "2024.04"), "日历日期被当成年限")
        _expect(module._new_fact_numbers("薪资 15K", "预算 15 元"), "金额量级被忽略")
        _expect(not module._new_fact_numbers("薪资 15K", "预算 15000 元"), "等额金额被误杀")
        _expect(module._new_fact_numbers("薪资 18-20K", "薪资 13-20K"), "金额区间下界未核验")
        _expect(module._new_fact_numbers("耗时 50 ms", "支撑 50 QPS"), "不同性能单位混用")
        _expect(not module._new_fact_numbers("共处理 987 万次请求", ""), "无金额语义的计数被误杀")
        _expect(module._new_fact_numbers("预算 987 万", ""), "缺少元字的明确金额被放行")
        # 每个结构计数词都覆盖，包括紧邻空白与多字单位「阶段」。
        for suffix in ("条", "步", "个", "点", "项", "层", "阶段", "种", "类", "分"):
            item = deepcopy(candidate)
            item["reference_answer"] += f"我按 987654321 {suffix}组织说明。"
            question, reason = _guard(sk, item)
            _expect(question is not None, f"结构计数被误杀：{suffix} {reason}")
        item = deepcopy(candidate)
        item["reference_answer"] += "第 987654321 阶段先核实材料。"
        question, reason = _guard(sk, item)
        _expect(question is not None, f"序号被误杀：{reason}")
        _expect(not module._audit_experience("2024.04到2024.08", INPUTS["resume_text"]), "相同时段只改连接词被误杀")
        _expect(module._audit_experience("2024.04到2024.09", INPUTS["resume_text"]), "伪造结束月份被放行")
        _expect(module._audit_experience("2020.01到2021.09", "2020.01-2020.03；2021.05-2021.09"),
                "不同经历的端点被拼接背书")
        item = deepcopy(candidate)
        item["question"] = "客户现场遇到你没用过的知识库能力，你怎么补上短板并说明交付风险？"
        question, reason = _guard(sk, item, category="tech_gap")
        _expect(question is not None, f"明确能力缺口被误判为已有经验：{reason}")
        item["question"] = "你熟悉知识库的搭建，请讲讲你负责过的相关实现过程以及交付成果？"
        question, reason = _guard(sk, item, category="tech_gap")
        _expect(question is None and reason.startswith("护栏③"), "缺口技术被冒充已有经验")

    def duplicate():
        """⑧ 不同考点提出同一句话时，真实去重函数拒绝后一个。"""
        # 两个不同考点都能关联到 Agent，确保前四道护栏不是拒绝原因。
        first, reason = _guard(sk, _candidate())
        _expect(first is not None, reason)
        # id 不同也必须去重，相似度判断仅关注归一化后的题干。
        second, reason = _guard(sk, _candidate(), category="tech_gap", accepted=[first])
        _expect(second is None and reason.startswith("护栏⑤"), reason)

    # 构造反例不消耗真实调用，避免为每道护栏重复付费出题。
    for number, operation in ((4, forged_metadata), (5, copied_jd), (6, invented_tech),
                              (7, invented_number), (8, duplicate)):
        record(number, operation)
    record(21, lambda: _noun_cases(sk))
    try:
        # ⑨ 覆盖一次修复成功与两次都半截；两者均须稳定模板补齐。
        # 根结构修复为合法空数组时也应补题，不必再浪费第三次生成。
        for responses in (['[{"question":', '[]'], ['[{"question":', '[{"question":']):
            plan, cost, calls = await _mock_plan(sk, responses)
            _expect(plan.meta.status == "needs_review" and calls == 2 and len(_questions(plan)) == 16,
                    f"半截 JSON 未补齐：{plan.meta.status}，calls={calls}")
            # 模拟的两次计量单独检查；最后报告的人民币成本只来自真实响应。
            _expect(abs(cost - .0001) < 1e-12 and validate_plan(plan) == [], "重试费用或 N0 校验不通过")
        results[9] = (True, "一次 JSON 重试；仍失败时补齐，费用累计正确")
    except Exception as error:
        results[9] = (False, f"{type(error).__name__}: {error}")
    try:
        # ⑩ 假传输层让三家顺序失败，主入口必须交付 failed 空题册。
        plan, cost, calls = await _exhausted_plan(sk)
        # 失败时不得为了凑配额掩盖全挂，必须保留空题册供接口转 503。
        _expect(plan.meta.status == "failed" and not _questions(plan) and cost == 0
                and calls == ["offline-0", "offline-1", "offline-2"],
                "全挂未返回 failed 空题册")
        results[10] = (True, "全挂返回 failed，未抛给调用方")
    except Exception as error:
        results[10] = (False, f"{type(error).__name__}: {error}")
    try:
        # ⑭ 合法空数组迫使所有模板出现，检查标记与真实经历提示。
        plan, _, _ = await _mock_plan(sk, ['[]'])
        # 强制没有合法候选，确保 source 标记检查不会因恰好没模板而空通过。
        questions = _questions(plan)
        _expect(len(questions) == 16 and plan.meta.injected_count == 16, "模板未完整注入")
        _expect(all(q.source == "injected" and "（结合你的真实经历填写）" in q.reference_answer
                    for q in questions), "模板 source 或答题框架提示不合格")
        # 模板还要通过完整 N0，不能只满足字符串中带有指定提醒。
        _expect(validate_plan(plan) == [], "模板题册未过 N0")
        results[14] = (True, "16 道模板均为 injected，并包含真实经历填写提示")
    except Exception as error:
        results[14] = (False, f"{type(error).__name__}: {error}")

    def field_diagnostics():
        """⑲ 非空构造组证明字段诊断确实来自实测值，不能靠空 rejected 通过。"""
        cases = [("answer_points", ["解释背景", "说明边界"], "answer_points 只有 2 条（要求正好 4 条）"),
                 ("reference_answer", "答" * 98, "reference_answer 98 字（要求 150~250"),
                 ("answer_points", ["说明背景", "字" * 35, "核实数据", "总结边界"], "answer_points[2] 35 字（要求 ≤28"),
                 ("bonus", [], "bonus 只有 0 条（要求正好 2 条）"),
                 ("follow_ups", ["怎样核实记录？"] * 4, "follow_ups 有 4 条（要求正好 2 条）")]
        for field, value, expected in cases:
            candidate = _candidate()
            candidate[field] = value
            _, reason = _guard(sk, candidate)
            _expect(reason.startswith("护栏④") and expected in reason, f"缺少实测字段诊断：{reason}")
        candidate = _candidate()
        del candidate["question"]
        failures = []
        _, reason = module._guard_candidate(next(slot for slot in sk.slots if slot.category == "tech_depth"),
                                             candidate, "Q-T-99", [], resume_text=INPUTS["resume_text"],
                                             jd_text=INPUTS["jd_text"], field_failures=failures)
        _expect(reason.startswith("护栏④") and failures[0][0] == "question" and "字（要求" in reason,
                "缺失题干没有归入可诊断、可重写的字段失败")
        # 生成目标只是安全余量：N0 合法的短题干、144 字示范和三条要点不能被额外误杀。
        candidate = _candidate()
        candidate["question"] = "使用Agent处理重复任务时，你如何核验实际交付结果？"
        candidate["reference_answer"] = candidate["reference_answer"][:144]
        candidate["answer_points"] = candidate["answer_points"][:3]
        question, reason = _guard(sk, candidate)
        _expect(question is not None, f"提示词目标被误用为 N0 拒绝线：{reason}")
    record(19, field_diagnostics)
    try:
        results[20] = (True, await _rewrite_cases(sk))
    except Exception as error:
        results[20] = (False, f"{type(error).__name__}: {error}")
    return results


async def _live_runs(sk):
    """执行三次真实生成，独立观察 SDK 请求次数与所有实际响应的费用。"""
    from app import fallback
    from app.providers import UsageMeter

    # 独立审计在 SDK 外沿计数，避免直接相信被测模块自报的调用次数。
    observed = dict(calls=0, meter=UsageMeter(), raw=[], errors=[])
    original_factory = fallback._make_async_provider_client

    def observed_factory(provider, timeout):
        """保留真实客户端，仅为每个真实 HTTP 请求增加审计计数。"""
        # 客户端、密钥选择和返回响应都来自真实配置，不伪造成功响应。
        client = original_factory(provider, timeout)
        original_create = client.chat.completions.create

        async def counted_create(**kwargs):
            """记录成功和失败请求，返回的仍是未经修改的真实响应。"""
            # 失败请求也计数，即便没有产生可计费的 usage。
            observed["calls"] += 1
            try:
                response = await original_create(**kwargs)
            except Exception as error:
                # 只记录连接底层原因；API 鉴权错误正文可能含凭证，禁止输出。
                root_error = error
                while root_error.__cause__ is not None:
                    root_error = root_error.__cause__
                message = str(root_error) if type(root_error).__name__ in (
                    "ConnectError", "ProxyError", "SSLCertVerificationError", "gaierror",
                ) else type(root_error).__name__
                observed["errors"].append(message)
                raise
            if response.usage is not None:
                observed["meter"].add(fallback._usage_with_defaults(response.usage))
            # 原始响应仅保存在本地报告目录，供检查半截 JSON 与模型措辞。
            observed["raw"].append(response.choices[0].message.content)
            return response

        client.chat.completions.create = counted_create
        return client

    # 三次生成互不依赖；每个入口独立计时、计费及执行完整护栏。
    with patch.object(fallback, "_make_async_provider_client", new=observed_factory):
        runs = await asyncio.gather(*(module.generate_questions(sk, resume_text=INPUTS["resume_text"],
                                                               jd_text=INPUTS["jd_text"]) for _ in range(3)))
    return runs, observed


def _structure(plan) -> list:
    """比较考点与逐字依据，忽略模型措辞、随机 plan_id 和实际耗时。"""
    # 不使用去空白或近似比较，尾随空格与制表符也必须完全相同。
    return [(q.part, q.category, q.basis) for q in _questions(plan)]


_A_NAMES = {
    "A01": "固定响应的 6/2/2 配额与 HR 数量",
    "A02": "十个技术考点各一次、编号及分区顺序",
    "A03": "basis 逐字、来源与槽位绑定",
    "A04": "原 N0 字段条数、字数及完整题册契约",
    "A05": "相关性三通路、HR 实体与元数据隔离",
    "A06": "整句照抄拦截与短语引用边界",
    "A07": "技术及专名事实护栏",
    "A08": "数字事实、结构计数及经历时间段",
    "A09": "跨考点重复题拦截",
    "A10": "JSON 修复、降级与费用累计",
    "A11": "三家全挂返回 failed 空题册",
    "A12": "字段局部重写、请求上限及 senior/故障路径",
    "A13": "字段拒绝诊断包含实测值",
    "A14": "模板来源及真实经历填写提示",
    "A15": "结构稳定、成本上限、耗时与独立请求计费审计",
}
_B_NAMES = {
    "B01": "每次真实题册 injected_count ≤1",
    "B02": "每次真实生成 status==ok",
    "B03": "HR 六类全为 model 且命中依据实体",
    "B04": "技术/场景/项目全为 model（含 debug/impl_detail）",
    "B05": "模板题带 flag 及真实经历填写提示",
}


def _record_result(results: dict, case: str, operation) -> None:
    """逐项收集硬检查或质量观察，不因前项失败丢失后续结果。"""
    try:
        operation()
    except Exception as error:
        results[case] = (False, f"{type(error).__name__}: {error}")
    else:
        results[case] = (True, "通过")


def _check_fields(plan) -> None:
    """独立核对 N0 原有边界，生成提示词的目标不替代 N0 红线。"""
    for question in _questions(plan):
        _expect(3 <= len(question.answer_points) <= 6 and all(len(point) <= 30 for point in question.answer_points),
                f"{question.id} 要点不符合 N0")
        _expect(120 <= len(question.reference_answer) <= 300 and 20 <= len(question.question) <= 120,
                f"{question.id} 字数不符合 N0")
        _expect(all(1 <= len(getattr(question, field)) <= 3 for field in ("bonus", "pitfalls", "follow_ups")),
                f"{question.id} 短列表数量不符合 N0")
    _expect(validate_plan(plan) == [], "未通过原 N0 validate_plan")


async def _deterministic_cases(sk) -> dict[str, tuple[bool, str]]:
    """A 类全部以固定传输响应执行；联网前就能完整得到 15 项结果。"""
    legacy = await _constructed_cases(sk)
    results = {}
    groups = {"A05": (4, 21), "A06": (5,), "A07": (6,), "A08": (7,), "A09": (8,),
              "A10": (9,), "A11": (10,), "A12": (20,), "A13": (19,), "A14": (14,)}
    for case, numbers in groups.items():
        failures = [legacy[number][1] for number in numbers if not legacy[number][0]]
        results[case] = (not failures, "；".join(failures) if failures else "固定响应及反例通过")

    payload = json.dumps(_valid_candidates(sk), ensure_ascii=False)
    fixtures = []
    # patch 传输层逐次恢复，不并发嵌套全局 patch。
    for _ in range(3):
        fixtures.append(await _mock_plan(sk, [payload]))
    plans = [plan for plan, _, _ in fixtures]

    def quotas():
        for plan in plans:
            _expect(plan.meta.status == "ok" and plan.meta.injected_count == 0, "固定合法响应意外被拒绝")
            _expect({part: len(plan.parts[part]) for part in QUOTA} == QUOTA and 5 <= len(plan.parts["hr"]) <= 8,
                    "固定响应配额不合格")

    def coverage():
        expected = Counter(code for part in QUOTA for code in CATEGORY_BY_PART[part])
        for plan in plans:
            _expect(Counter(q.category for q in _questions(plan) if q.part != "hr") == expected, "十码未各出现一次")
            for part in PART_ORDER:
                _expect([q.id for q in plan.parts[part]] == [f"{ID_PREFIX[part]}{index:02d}" for index in range(1, len(plan.parts[part]) + 1)],
                        "分区编号顺序不正确")

    def bases():
        for plan in plans:
            _expect(len(_questions(plan)) == len(sk.slots), "固定响应缺少槽位")
            _expect(all(q.basis == slot.basis and q.basis_source == slot.basis_source
                        for q, slot in zip(_questions(plan), sk.slots)), "依据或来源被改写/错绑")

    def fields():
        for plan in plans:
            _check_fields(plan)
            _expect(all(len(q.answer_points) == 4 and len(q.bonus) == len(q.pitfalls) == len(q.follow_ups) == 2
                        for q in _questions(plan)), "固定的四要点、双短列表响应被代码改动")

    def audit():
        _expect(_structure(plans[0]) == _structure(plans[1]) == _structure(plans[2]), "固定输入结构或依据不稳定")
        _expect(len({plan.plan_id for plan in plans}) == 3, "复用同一题册代替执行生成入口")
        for plan, cost, calls in fixtures:
            _expect(0 < cost <= .02 and cost == plan.meta.cost_yuan and abs(cost - .00005) < 1e-12, "固定响应计费不正确")
            _expect(_request_count(plan) == calls == 1 and plan.meta.duration_ms <= 45000, "请求计数或耗时上限不正确")

    for case, operation in (("A01", quotas), ("A02", coverage), ("A03", bases), ("A04", fields), ("A15", audit)):
        _record_result(results, case, operation)
    return results


def _audit_live(sk, runs, observed, results) -> None:
    """额外审计真实返回的代码不变量；模型 failed 按既有空题册契约核验。"""
    def merge(case, operation):
        current = {}
        _record_result(current, case, operation)
        if not current[case][0]:
            prior = results[case]
            results[case] = (False, (prior[1] + "；" if not prior[0] else "") + "真实返回审计：" + current[case][1])

    # 生成失败不是配额坏了：此分支必须确实为空，由 B 类记录模型质量失败。
    for plan, _ in runs:
        if plan.meta.status == "failed":
            merge("A11", lambda plan=plan: _expect(not _questions(plan) and plan.meta.injected_count == 0,
                                                   "failed 题册携带半成品或模板"))
            continue
        merge("A01", lambda plan=plan: _expect({part: len(plan.parts[part]) for part in QUOTA} == QUOTA
                                               and 5 <= len(plan.parts["hr"]) <= 8, "真实配额不合格"))
        merge("A02", lambda plan=plan: _expect(Counter(q.category for q in _questions(plan) if q.part != "hr")
                                               == Counter(code for part in QUOTA for code in CATEGORY_BY_PART[part]), "真实十码不完整"))
        merge("A03", lambda plan=plan: _expect(len(_questions(plan)) == len(sk.slots)
                                               and all(q.basis == slot.basis and q.basis_source == slot.basis_source
                                                       for q, slot in zip(_questions(plan), sk.slots)), "真实依据错绑或被改写"))
        merge("A04", lambda plan=plan: _check_fields(plan))

    def diagnostics():
        for plan, _ in runs:
            for item in plan.rejected:
                if "护栏④" in item.reason:
                    _expect("条（要求" in item.reason or "字（要求" in item.reason, f"缺少实测字段诊断：{item.reason}")
    merge("A13", diagnostics)

    def budget():
        _expect(len(runs) == 3, "没有保留全部三次真实运行")
        for plan, cost in runs:
            # 网络失败可以无费用；已收到的失败响应费用照常累计，不能跳过成本上限。
            _expect(0 <= cost <= .02 and cost == plan.meta.cost_yuan, f"单次成本 {cost:.8f} 元超过上限或未真实计费")
            _expect(0 <= _request_count(plan) <= 3 and plan.meta.duration_ms <= 45000, "真实请求或耗时超过上限")
        _expect(abs(sum(cost for _, cost in runs) - observed["meter"].cost()) < 1e-10, "实际用量与题册费用不一致")
        _expect(sum(_request_count(plan) for plan, _ in runs) == observed["calls"], "自报请求次数与 SDK 审计不一致")
    merge("A15", budget)


def _quality_cases(plans) -> tuple[dict[str, tuple[bool, str]], list[str]]:
    """B 类完整保留，逐次检查后按三次最差结果计分；失败只写 warning。"""
    entities = {"motivation": ("恒祥达", "后端开发工程师", "2024.10"), "salary": ("13-15K",),
                "plan": ("Golang",), "collaboration": ("后端开发",),
                "career_switch": ("Golang", "13-15K"), "timeline": ("2024.04", "2024.08")}

    def injection(plan):
        _expect(len(_questions(plan)) == 16 and plan.meta.injected_count <= 1,
                f"题数={len(_questions(plan))}，injected={plan.meta.injected_count}")

    def status(plan):
        _expect(plan.meta.status == "ok", f"status={plan.meta.status}")

    def hr(plan):
        questions = plan.parts["hr"]
        _expect(len(questions) == 6 and {q.category for q in questions} == set(entities), "HR 非六类各一题")
        for question in questions:
            _expect(question.source == "model", f"{question.category} 为模板题")
            _expect(any(entity.casefold() in question.question.casefold() for entity in entities[question.category]),
                    f"{question.category} 缺少依据实体")

    def technical(plan):
        questions = [q for q in _questions(plan) if q.part != "hr"]
        _expect(len(questions) == 10 and all(q.source == "model" for q in questions),
                "技术/场景/项目缺题或含模板：" + "、".join(q.category for q in questions if q.source != "model"))

    def flags(plan):
        for question in _questions(plan):
            if question.source == "injected":
                _expect(bool(question.flag) and "（结合你的真实经历填写）" in question.reference_answer,
                        f"模板 {question.id} flag={question.flag!r}，需保留提示；不为此修改 N0")

    results, warnings = {}, []
    for case, operation in (("B01", injection), ("B02", status), ("B03", hr), ("B04", technical), ("B05", flags)):
        failures = []
        if len(plans) != 3:
            failures.append("未取得三份真实返回")
        for index, plan in enumerate(plans, 1):
            try:
                operation(plan)
            except Exception as error:
                detail = f"真实运行 {index}：{error}"
                failures.append(detail)
                warning = f"{case} 模型质量（仅供参考）：{detail}"
                warnings.append(warning)
                if warning not in plan.meta.warnings:
                    plan.meta.warnings.append(warning)
        results[case] = (not failures, "；".join(failures) if failures else "三次均通过（按最差一次计）")
    return results, warnings


def _exit_code(a_results: dict[str, tuple[bool, str]]) -> int:
    """返回码仅由完整的十五项 A 类结果决定，不接收 B 类参数。"""
    return 0 if set(a_results) == set(_A_NAMES) and all(passed for passed, _ in a_results.values()) else 1


def _summary(a_results, b_results, cost: float, calls: int) -> str:
    a_passed = sum(passed for passed, _ in a_results.values())
    b_passed = sum(passed for passed, _ in b_results.values())
    return f"通过 A {a_passed}/15（确定性）；B {b_passed}/5（模型质量，仅供参考）；成本 {cost:.8f} 元；真实调用 {calls} 次"


async def main(*, offline: bool = False) -> int:
    """先执行十五项确定性检查，再观察三次真模型质量，退出码只看 A 类。"""
    global INPUTS
    INPUTS = load_beike_inputs()
    sk = build_skeleton(**INPUTS)
    a_results = await _deterministic_cases(sk)
    runs, observed = [], dict(calls=0, meter=None, raw=[], errors=[])
    b_results, warnings = {}, []
    if not offline:
        runs, observed = await _live_runs(sk)
        _audit_live(sk, runs, observed, a_results)
        b_results, warnings = _quality_cases([plan for plan, _ in runs])

    destination = ROOT / "reports/n2b"
    destination.mkdir(parents=True, exist_ok=True)
    lines = []
    for case, label in _A_NAMES.items():
        passed, detail = a_results[case]
        lines.append(f"{case} {'通过' if passed else '失败'}：{label}；{detail}")
    for case, label in _B_NAMES.items():
        if case not in b_results:
            lines.append(f"{case} 未执行：{label}；--offline 不调用真模型")
        else:
            passed, detail = b_results[case]
            lines.append(f"{case} {'通过' if passed else '未达标'}：{label}；{detail}（仅供参考）")
    for index, (plan, cost) in enumerate(runs, 1):
        lines.append(f"真实运行 {index}：injected={plan.meta.injected_count}；status={plan.meta.status}；"
                     f"题数={len(_questions(plan))}；耗时 {plan.meta.duration_ms}ms；成本 {cost:.8f} 元；请求 {_request_count(plan)} 次")
        (destination / f"interview_plan_{index}.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    for message in dict.fromkeys(observed.get("errors", [])):
        lines.append(f"真实请求诊断：{message}")
    for warning in warnings:
        lines.append(f"warning：{warning}")
    if runs:
        readable = "\n".join(f"{q.id} [{q.category}] source={q.source}\n{q.question}" for q in _questions(runs[0][0])) + "\n"
        (destination / "questions_16.txt").write_text(readable, encoding="utf-8")
        (destination / "model_responses.json").write_text(json.dumps(observed["raw"], ensure_ascii=False, indent=2), encoding="utf-8")
        rejected = "\n".join(f"真实运行 {index}：" + json.dumps([item.model_dump() for item in plan.rejected], ensure_ascii=False)
                             for index, (plan, _) in enumerate(runs, 1)) + "\n"
        (destination / "rejected.txt").write_text(rejected, encoding="utf-8")
    (destination / "warnings.json").write_text(json.dumps({"warnings": warnings, "B": b_results}, ensure_ascii=False, indent=2), encoding="utf-8")
    total_cost = observed["meter"].cost() if observed["meter"] is not None else 0
    lines.append(_summary(a_results, b_results, total_cost, observed["calls"]))
    report = "\n".join(lines) + "\n"
    print(report, end="")
    (destination / ("check_interview_llm_offline.txt" if offline else "check_interview_llm.txt")).write_text(report, encoding="utf-8")
    return _exit_code(a_results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="完整执行 A 类固定响应检查；B 类明确记为未执行")
    raise SystemExit(asyncio.run(main(offline=parser.parse_args().offline)))
