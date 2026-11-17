"""N2 段 B 十五项验收：默认三次真模型，构造反例单独替换传输层。"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
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
    return candidate


def _guard(sk, candidate: dict, *, category: str = "tech_depth", accepted: list | None = None):
    """执行真实五道单题护栏，保持每组反例使用同一个真实骨架。"""
    # 使用真骨架的依据，不能构造宽松白名单来让非法候选通过。
    slot = next(slot for slot in sk.slots if slot.category == category)
    # 反例 id 也由代码提供，不能采信伪造候选里的 id 或 part。
    return module._guard_candidate(slot, candidate, f"{ID_PREFIX[slot.part]}99", accepted or [],
                                   resume_text=INPUTS["resume_text"], jd_text=INPUTS["jd_text"])


async def _mock_plan(sk, responses: list, *, fail: bool = False):
    """只替换传输层，完整运行解析、七道护栏、模板及 N0 组装。"""
    # 这个计数只供反例断言使用，不会加到最后的真实 HTTP 次数。
    calls = 0

    async def fake(messages, state):
        """记录构造响应及计量，明确区分模拟请求与真实 HTTP 请求。"""
        nonlocal calls
        calls += 1
        # 故障发生在取得响应之前，不应凭空计入 tokens 成本。
        if fail:
            raise RuntimeError("构造：所有厂商失败")
        # 两次响应都计入生成器费用，测试不能免费化 JSON 重试。
        state["meter"].add(SimpleNamespace(prompt_tokens=10, completion_tokens=10, prompt_cache_hit_tokens=0))
        # 同样的第二个坏响应可以重放，但业务层仍只能请求两次。
        return responses[min(calls - 1, len(responses) - 1)]

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

    def copied_jd():
        """⑤ 在相关性成立时，逐字抄 JD 必须由第二道护栏拒绝。"""
        slot = next(slot for slot in sk.slots if slot.category == "tech_gap")
        candidate = _candidate()
        # 加疑问尾句仍应算抄写，不能只做完整字符串相等判断。
        candidate["question"] = slot.basis + "你会如何完成？"
        question, reason = _guard(sk, candidate, category="tech_gap")
        # 指定拒绝阶段，避免在字段长度等无关原因上失败也被记为通过。
        _expect(question is None and reason.startswith("护栏②"), reason)

    def invented_tech():
        """⑥ 题干含原文没有的 Kubernetes，必须由事实护栏拒绝。"""
        candidate = _candidate()
        # 保留原有 Agent 相关性，只追加不存在的技术词隔离第三道护栏。
        candidate["question"] += "如果改用 Kubernetes 应怎样验证？"
        question, reason = _guard(sk, candidate)
        _expect(question is None and "护栏③" in reason and "Kubernetes" in reason, reason)

    def invented_number():
        """⑦ 原文没有的量化指标不能进入题干。"""
        candidate = _candidate()
        # 选用原件不存在的完整数值，不能因为其中某个数字出现过就放行。
        candidate["question"] += "如何支撑 987654321 次请求？"
        question, reason = _guard(sk, candidate)
        _expect(question is None and "护栏③" in reason and "987654321" in reason, reason)

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


async def main(*, offline: bool = False) -> int:
    """执行十五项，真实结果和题干全文落盘；任何失败均返回非零退出码。"""
    # 输入只保存在当前检查进程，运行结束不缓存原始个人材料。
    global INPUTS
    # 复用段 A 与 N1 同一份真实材料，三次运行不会重新抽取模型或伪造简历。
    INPUTS = load_beike_inputs()
    sk = build_skeleton(**INPUTS)
    results = await _constructed_cases(sk)
    # 离线模式不初始化真实用量计量，也不会生成虚构的成功模型报告。
    runs, observed = [], dict(calls=0, meter=None, raw=[])
    if not offline:
        runs, observed = await _live_runs(sk)
        # 保存每一份实际返回，即便其中一份失败也不能从统计中删除。
        plans = [plan for plan, _ in runs]

        def quotas():
            """① 检查每次真实生成的四分区配额。"""
            # failed 空题册不能以忽略分区或跳过断言的方式变为配额通过。
            _expect(all(p.meta.status != "failed" and {part: len(p.parts[part]) for part in QUOTA} == QUOTA
                        and 5 <= len(p.parts["hr"]) <= 8 for p in plans), "真实题册配额不合格或生成失败")

        def coverage():
            """② 检查真实题单十个考点各一次。"""
            # 从实际题目重新计数，与模型自报覆盖摘要无关。
            expected = Counter(code for part in QUOTA for code in CATEGORY_BY_PART[part])
            _expect(all(Counter(q.category for q in _questions(p) if q.part != "hr") == expected for p in plans),
                    "十个 code 未各出现一次")

        def bases():
            """③ 逐槽位使用等号比较原始 basis，包括空白与换行。"""
            # 先检查完整题数，防止 zip 截短后让缺失的尾部依据逃过校验。
            _expect(all(len(_questions(p)) == len(sk.slots)
                        and all(q.basis == slot.basis for q, slot in zip(_questions(p), sk.slots))
                        for p in plans), "真实题册依据与骨架不逐字相等")

        def budget():
            """⑪ 不抹掉失败或重试响应的成本，单次成本不得超过预算。"""
            # 预算按单份题册检查，总成本另报，不能以三次平均值掩盖单次超支。
            _expect(all(0 < cost <= .02 and cost == p.meta.cost_yuan for p, cost in runs), "单次成本超过 0.02 元或未真实计费")
            _expect(abs(sum(cost for _, cost in runs) - observed["meter"].cost()) < 1e-10, "独立用量审计与返回成本不一致")

        def duration():
            """⑫ 每次真实出题从入口至返回均不得超过时限。"""
            # duration_ms 由生成入口的单调时钟计算，包含 fallback 与 JSON 重试。
            _expect(all(p.meta.duration_ms <= 45000 for p in plans), "真实生成超过 45s")

        def stable():
            """⑬ 三次真实调用保留相同结构与依据，不强求措辞必须变化。"""
            _expect(all(p.meta.status != "failed" for p in plans) and _structure(plans[0]) == _structure(plans[1]) == _structure(plans[2]),
                    "三次生成结构或依据不稳定")
            # 新 token 和至少三次 SDK 请求共同排除缓存同一份报告冒充三次运行。
            _expect(len({p.plan_id for p in plans}) == 3 and observed["calls"] >= 3, "未执行三次真实生成")

        def valid():
            """⑮ 对三份真实输出分别运行未经修改的 N0 校验。"""
            # 返回原校验器的空错误列表，不能自行过滤其中某类结构错误。
            _expect(all(validate_plan(p) == [] for p in plans), "生成题册未通过 N0 validate_plan")

        # 每项独立报错，成本或耗时不合格时仍给出其余检查的真实结果。
        for number, operation in ((1, quotas), (2, coverage), (3, bases), (11, budget),
                                  (12, duration), (13, stable), (15, valid)):
            try:
                operation()
            except Exception as error:
                results[number] = (False, f"{type(error).__name__}: {error}")
            else:
                results[number] = (True, "三次真实生成均通过")
    # 报告只落本地 ignored 目录，不复制个人简历原件或凭证。
    destination = ROOT / "reports/n2b"
    destination.mkdir(parents=True, exist_ok=True)
    # 输出先汇总十五项，再展示单次成本，最后一行固定为通过数和真实累计。
    lines = []
    # 未执行的真实测试必须明写未执行，离线反例不能冒充 15/15。
    for number in range(1, 16):
        if number in results:
            passed, detail = results[number]
            lines.append(f"{number:02d} {'通过' if passed else '失败'}：{detail}")
        else:
            lines.append(f"{number:02d} 未执行：--offline 不调用真模型")
    # 保存全部三次，方便核对配额和依据稳定性，而非只挑选成功的一份。
    for index, (plan, cost) in enumerate(runs, 1):
        lines.append(f"真实运行 {index}：status={plan.meta.status}；题数={len(_questions(plan))}；"
                     f"injected={plan.meta.injected_count}；耗时 {plan.meta.duration_ms}ms；成本 {cost:.8f} 元")
        (destination / f"interview_plan_{index}.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    # 网络不可用时给出可操作的连接原因，不输出请求体或任何鉴权信息。
    for message in dict.fromkeys(observed.get("errors", [])):
        lines.append(f"真实请求诊断：{message}")
    # 人读题干单独输出全文，JSON 同时保留完整答案、原始依据与审计信息。
    if runs:
        readable = "\n".join(f"{q.id} [{q.category}] source={q.source}\n{q.question}" for q in _questions(runs[0][0])) + "\n"
        (destination / "questions_16.txt").write_text(readable, encoding="utf-8")
        (destination / "model_responses.json").write_text(json.dumps(observed["raw"], ensure_ascii=False, indent=2), encoding="utf-8")
    # UsageMeter 的独立累计值包含所有成功响应，即便后续解析或字段被拒绝。
    total_cost = observed["meter"].cost() if observed["meter"] is not None else 0
    passed = sum(success for success, _ in results.values())
    # 构造用例的模拟计量不会混入真实成本和真实 HTTP 调用次数。
    lines.append(f"通过 {passed}/15；总成本 {total_cost:.8f} 元；真实调用次数 {observed['calls']}")
    report = "\n".join(lines) + "\n"
    # 控制台与落盘报告使用同一字符串，交付内容可以逐字复核。
    print(report, end="")
    (destination / ("check_interview_llm_offline.txt" if offline else "check_interview_llm.txt")).write_text(report, encoding="utf-8")
    return 0 if all(success for success, _ in results.values()) and (offline or passed == 15) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="仅构造反例，明确报告未执行的真模型验收")
    raise SystemExit(asyncio.run(main(offline=parser.parse_args().offline)))
