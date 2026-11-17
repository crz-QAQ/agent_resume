"""N1 离线验收：原文硬校验、语义池隔离与两份 DOCX 的真实产物。"""

from __future__ import annotations

import ast
from copy import deepcopy
from difflib import SequenceMatcher
from hashlib import sha256
import json
from pathlib import Path
import random
import re
import socket
import sys
from time import perf_counter
from types import SimpleNamespace
from typing import Callable

# 脚本可以从任意目录执行，且不为验收生成额外的字节码文件。
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
# Windows 管道可能默认使用 GBK，显式 UTF-8 保留圈号和中文输出。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 单独记录业务导入新增的模块，避免把检查框架自身的依赖算在 N1 头上。
_BEFORE_BUSINESS = frozenset(sys.modules)
from app.file_parser import extract_text
from app.guard_revision import find_original, normalize_text, resolve_job_input
import app.interview_skeleton as module
from app.interview_skeleton import (
    InterviewSkeleton, Slot, build_skeleton, extract_projects, is_career_switch, pick_basis, pick_projects,
)
from app.job_profile import GENERIC_MUST_HAVE, GENERIC_RESPONSIBILITIES, infer_profile
from app.resume_diagnose import diagnose
from app.schemas_interview import CATEGORY_BY_PART, PART_ORDER, TECH_CATEGORY_ORDER
from app.schemas_match import JobInfo
from app.schemas_revision import DiagnosisFinding, JobProfile
from app.scoring_rules import score_hard
# unittest.mock 自身会导入 asyncio，必须在完成业务依赖审计后再导入。
BUSINESS_IMPORTS = frozenset(sys.modules) - _BEFORE_BUSINESS
from unittest.mock import patch


# 固定种子仅用于重放，不登记题册、不写会话文件。
SEED = "0123456789abcdef"
MEASUREMENTS: list[float] = []
_REAL_INPUTS: dict | None = None
# 真实材料只读外部原件，不复制个人简历进仓库，也不将合成数据冒充贝壳案例。
_BEIKE_INPUTS: dict | None = None
BEIKE_RESUME = Path("D:/HermesDoc/简历修改/蔡荣祖_简历_Word版.docx")
BEIKE_JD = Path("D:/HermesDoc/scratch/beike_jd.txt")
# 项目反例采用人工写定的输入，绝不往真实简历中插入虚构项目。
PROJECT_TEXT = """项目经历
项目名称：订单系统
2021.01-2021.06
负责订单接口与验收。
通知平台 2022年3月-2022年8月
负责消息通知与重试。
检索平台 2023/03-2023/08
负责知识检索与数据校验。
"""
# 用户确认：原文件是双意向简历，跨行断言使用独立纯 Go 样本。
PURE_GO_TEXT = """求职意向：Golang 开发工程师
期望薪资：按后端岗位薪酬范围协商。
工作经历
某科技公司 | 后端开发工程师 | 2021.01-2023.12
使用 Go 与 MySQL 开发订单接口。
使用 Redis 缓存业务数据。
使用 Go 开发连接复用与并发请求处理。
使用 MySQL 排查索引与慢查询问题。
技能
Go、MySQL、Redis
个人优势
能够通过日志和监控定位线上故障。
项目经历
订单系统项目 2018.01-2018.06：负责订单业务验收。
检索平台项目 2020.01-2020.06：负责内容检索验收。
"""


def _expect(condition: object, message: str) -> None:
    """断言条件成立，优化模式也保留失败退出行为。"""
    # 不使用 assert，避免 python -O 把验收条件整体去掉。
    if not condition:
        raise AssertionError(message)


def _jd_cases() -> dict[str, str]:
    """只用 AST 读取既有 S1 的 JD 字面量，不导入其模型调用脚本。"""
    tree = ast.parse((ROOT / "tools/check_job_parser.py").read_text(encoding="utf-8"))
    # 导入旧检查脚本会引入 asyncio 和模型入口，字面量解析没有这些副作用。
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "JD_CASES" for target in node.targets))
    # literal_eval 不执行文件中的导入、函数或模型请求，只读取原样的 JD 字符串。
    return dict(ast.literal_eval(assignment.value))


def _semantic(job: JobInfo) -> SimpleNamespace:
    """提供既有 SemanticScore 属性形状的人工覆盖标注，不运行向量模型。"""
    # N1 消费评分产物而不生产评分；这个测试替身只表达第一条职责尚未覆盖。
    items = [SimpleNamespace(item=item, best_sim=0.42, best_sentence="", covered=False,
                             rerank_score=-1.0) for item in job.responsibilities]
    return SimpleNamespace(score=0, per_item=items, uncovered=list(job.responsibilities), status="ok", reasons=[])


def load_real_inputs() -> dict:
    """读任务书指定 DOCX 与既有 Go JD-A，返回不共享可变状态的验收输入。"""
    global _REAL_INPUTS
    # 文件读取只发生在测试准备层，传给业务构建器的始终是已提取的文本。
    if _REAL_INPUTS is None:
        path = ROOT / "tools/test_files/go_backend_resume.docx"
        text = extract_text(path.name, path.read_bytes())
        jd = _jd_cases()["JD-A"]
        # 字段逐项来自原文件，不调解析模型，也不把 AI 交付经历从原件中删掉。
        resume = SimpleNamespace(name="王浩", years=8.0, skills=[
            "Go", "Python", "PHP", "MySQL", "Redis", "Kafka", "Docker", "Gin",
            "LangChain", "Dify", "Git", "Linux", "RAG",
        ])
        job = JobInfo(title="后端开发工程师", company=None, required_years_min=3,
                      required_years_max=5, education="本科及以上学历", must_have=["Go", "MySQL"],
                      nice_to_have=["Redis"], responsibilities=["负责订单系统接口开发与性能优化"])
        # 真实硬分与诊断仅运行既有纯规则；语义覆盖测试替身已在上方明确标注。
        _REAL_INPUTS = dict(resume_text=text, resume=resume, job=job, profile=None,
            diagnosis=diagnose(text, resume, job), hard=score_hard(resume, job, text), sem=_semantic(job),
            job_title=job.title, seed=SEED, jd_text=jd)
    # 每个用例拿到独立副本，某组改造反例不能影响后面的真实样例。
    return deepcopy(_REAL_INPUTS)


def load_beike_inputs() -> dict:
    """读取缺陷台账涉及的原 Word 简历与贝壳 JD，准备零模型输入。"""
    global _BEIKE_INPUTS
    if _BEIKE_INPUTS is None:
        # 缺少材料时明确失败，不跳过真实验收，也不退回旧的 Go 测试文件。
        text = extract_text(BEIKE_RESUME.name, BEIKE_RESUME.read_bytes())
        # 用保存的原 JD 文本直接核验证据，不能把结构化技能字段重新拼成一份招聘原文。
        jd = BEIKE_JD.read_text(encoding="utf-8-sig")
        resume = SimpleNamespace(name=None, years=3.0, skills=[
            "Golang", "Gin", "Gorm", "MySQL", "Redis", "RabbitMQ", "PHP", "ThinkPHP",
            "HTML5", "JavaScript", "Agent", "LLM", "DeepSeek", "CodeX",
        ])
        # 字段保留原 JD 拼写，包括原文的 Al 和 skil，不自行修正成不存在的引文。
        lines = jd.splitlines()
        # 只截取原 JD 现有的职责区，不能将岗位介绍或「我们希望你」标签混成职责。
        start = next(index for index, line in enumerate(lines) if "你要做的事情" in line) + 1
        end = next(index for index in range(start, len(lines)) if lines[index].startswith("我们希望你"))
        job = JobInfo(title="AI业务落地专家(FDE)", company=None, required_years_min=None,
            required_years_max=None, education=None, must_have=["Agent", "知识库", "工作流", "Al编程工具"],
            nice_to_have=["Python", "JavaScript", "API"], responsibilities=[line.strip() for line in lines[start:end] if line.strip()])
        _BEIKE_INPUTS = dict(resume_text=text, resume=resume, job=job, profile=None,
            diagnosis=diagnose(text, resume, job), hard=score_hard(resume, job, text), sem=_semantic(job),
            job_title=job.title, seed=SEED, jd_text=jd)
    # 诊断与硬分运行既有规则；语义覆盖仍为人工离线标注，不加载语义模型。
    return deepcopy(_BEIKE_INPUTS)


def load_inferred_inputs() -> dict:
    """真简历 × 调用方提供的技能型岗位画像；无 JD，不将通用模板当技能画像。"""
    arguments = load_beike_inputs()
    # 这是明确列出的离线画像输入，不声称它由 infer_profile 的通用模板生成。
    profile = JobProfile(title="AI 业务落地专家", source="inferred", confirmed=False,
        must_have=["Agent", "知识库", "工作流", "AI编程工具"],
        responsibilities=["设计企业知识库检索与评测方案", "验证 AI 工作流的运行与交付风险"],
        note="按岗位名提供的待确认技能型画像（离线验收输入），非真实 JD")
    job = JobInfo(title=profile.title, company=None, required_years_min=None, required_years_max=None,
                  education=None, must_have=list(profile.must_have), nice_to_have=[],
                  responsibilities=list(profile.responsibilities))
    arguments.pop("jd_text")
    arguments.update(profile=profile, job=job, job_title=profile.title, sem=_semantic(job),
                     hard=score_hard(arguments["resume"], job, arguments["resume_text"]),
                     diagnosis=diagnose(arguments["resume_text"], arguments["resume"], job))
    return arguments


def load_go_beike_inputs() -> dict:
    """第二份真实 DOCX 也用贝壳 JD，真实硬分包含 Agent 缺口，不虚造扣分。"""
    arguments = load_real_inputs()
    target = load_beike_inputs()
    arguments.update(job=target["job"], sem=target["sem"], jd_text=target["jd_text"], job_title=target["job_title"])
    arguments.update(hard=score_hard(arguments["resume"], arguments["job"], arguments["resume_text"]),
                     diagnosis=diagnose(arguments["resume_text"], arguments["resume"], arguments["job"]))
    return arguments


def load_title_only_inputs(title: str, loader: Callable[[], dict] = load_beike_inputs,
                           profile: JobProfile | None = None) -> dict:
    """只按岗位名或调用方明确提供的画像重算输入，不沿用真实 JD 的职责及扣分。"""
    arguments = loader()
    profile = deepcopy(profile) if profile is not None else infer_profile(title)
    profile.title = title
    job = JobInfo(title=title, company=None, required_years_min=None, required_years_max=None,
                  education=None, must_have=list(profile.must_have), nice_to_have=[],
                  responsibilities=list(profile.responsibilities))
    # 无 JD 用例真正删除 JD，硬评分、语义标注和诊断均与当前画像保持一致。
    arguments.pop("jd_text", None)
    arguments.update(profile=profile, job=job, job_title=title, sem=_semantic(job),
                     hard=score_hard(arguments["resume"], job, arguments["resume_text"]),
                     diagnosis=diagnose(arguments["resume_text"], arguments["resume"], job))
    return arguments


def load_beike_profile_inputs() -> dict:
    """复现缺陷输入：真 JD 与按岗位名生成的画像同时传入。"""
    arguments = load_beike_inputs()
    arguments["profile"] = infer_profile("AI 业务落地专家")
    return arguments


def _run(arguments: dict) -> InterviewSkeleton:
    """计时一次真实构建，并逐项确认所有传入对象保持不变。"""
    before = deepcopy(arguments)
    # 读文件、诊断、准备输入和深拷贝不计入 build 阶段耗时。
    started = perf_counter()
    result = build_skeleton(**arguments)
    MEASUREMENTS.append((perf_counter() - started) * 1000)
    # 比较包含模型、字典和嵌套列表的全部输入，而不只比较最外层引用。
    _expect(arguments == before, "build_skeleton 修改了输入对象")
    # 每组都验证零成本与范围，防止专门的性能用例之外走了其他生成路径。
    _expect(result.meta["model_calls"] == 0 and result.meta["cost_yuan"] == 0, "骨架产生模型成本")
    _expect(all(1 <= slot.difficulty <= 3 and len(slot.focus) <= 30 for slot in result.slots), "槽位字段越界")
    evidence_jd = arguments.get("jd_text") or ""
    _assert_original_basis(result.slots, arguments["resume_text"], evidence_jd, arguments.get("profile"))
    _assert_unique_basis(result.slots)
    missing = next((item for item in arguments["hard"].deductions if item.type == "missing_skill"), None)
    gap = next(slot for slot in result.slots if slot.category == "tech_gap")
    if gap.anchor.get("profile_fallback"):
        # 只有整份画像均为通用项时才允许基础能力题，不把模板句冒充具体缺口技能。
        profile = arguments["profile"]
        rows = [*profile.must_have, *profile.responsibilities]
        _expect(not evidence_jd.strip() and rows
                and all(row in [*GENERIC_MUST_HAVE, *GENERIC_RESPONSIBILITIES] for row in rows), "非通用画像放宽了技能依据")
        _expect(gap.anchor["skill"] == "" and gap.anchor["missing_skill"] is False, "通用模板被标为具体缺口")
    else:
        _expect(missing is not None, "无真实扣分却生成了具体缺口")
        expected_skill = re.sub(r"^必备技能\s*[:：]\s*", "", missing.item.strip()).strip()
        _expect(gap.anchor["skill"] == expected_skill, "缺口技能没有绑定首条 missing_skill 扣分项")
    return result


def _assert_unique_basis(slots: list[Slot]) -> None:
    """独立检查所有依据的归一化唯一性，不豁免标题和任何说明。"""
    # 不调用模型校验器来证明模型自己正确；第十九组还会故意喂入重复值测试本断言。
    # 空格、标点与英文大小写变化不产生新事实，不能借这些排版差异逃过全局去重。
    keys = [normalize_text(slot.basis).casefold() for slot in slots]
    _expect(len(keys) == len(set(keys)), "真实依据重复")


def _assert_original_basis(slots: list[Slot], resume_text: str, jd_text: str,
                           profile: JobProfile | None = None) -> None:
    """独立检查说明文字、原文命中和实际来源，每一条都参与验收。"""
    has_jd = bool(jd_text and jd_text.strip())
    profile_rows = [*profile.must_have, *profile.responsibilities] if profile else []
    generic_items = {normalize_text(row) for row in [*GENERIC_MUST_HAVE, *GENERIC_RESPONSIBILITIES]}
    generic_profile = bool(profile_rows) and all(normalize_text(row) in generic_items for row in profile_rows)
    for slot in slots:
        _expect(not re.search(r"兜底|可追溯|占位|TODO", slot.basis, re.I),
                f"{slot.category} 的 basis 含说明文字：{slot.basis}")
        in_resume = find_original(resume_text, slot.basis)
        in_jd = find_original(jd_text, slot.basis)
        in_profile = any(find_original(row, slot.basis) for row in profile_rows)
        _expect(in_resume or in_jd or in_profile, f"{slot.category} 的依据不在原文里：{slot.basis}")
        expected_source = "resume" if in_resume else "jd"
        if has_jd and slot.category in ("tech_gap", "scenario_design", "scenario_risk"):
            _expect(in_jd, f"{slot.category} 没有从 JD 家族取证")
            expected_source = "jd"
        elif not has_jd and in_profile and (slot.category in ("tech_gap", "scenario_design", "scenario_risk")
                                            or not (in_resume or in_jd)):
            expected_source = "profile"
            expected_flag = ("岗位画像为通用兜底：建议补充真实 JD 后重新生成"
                             if generic_profile and slot.category == "scenario_design"
                             else "依据来自岗位画像（推测），非真实 JD")
            _expect(slot.flag == expected_flag, "画像依据缺少推测或通用画像标记")
        if has_jd:
            _expect(slot.basis_source != "profile", "传了 JD 却仍使用画像来源")
        _expect(slot.basis_source is not None and slot.basis_source == expected_source,
                f"{slot.category} 来源错误：{slot.basis_source}，实际为 {expected_source}")


def _expect_value_error(arguments: dict, phrase: str) -> None:
    """反例必须抛出指定原因的 ValueError，不能退化成返回警告骨架。"""
    try:
        build_skeleton(**arguments)
    except ValueError as error:
        _expect(phrase in str(error), f"异常原因错误：{error}")
    else:
        raise AssertionError(f"没有硬拒绝：{phrase}")


def load_complete_inputs() -> dict:
    """构造具有十五条独立可用依据的固定样例，专测去重而不依赖缺失豁免。"""
    # 每个语义池都有独立事实；HR 的两条协作句不带技术词，不抢占技术池的专属依据。
    text = """求职意向：Golang 后端开发工程师
期望薪资：面议，按后端开发岗位职责和薪酬方案协商。
工作经历
甲科技公司 | 后端开发工程师 | 2016.01-2017.12
使用 MySQL 排查慢查询并核对执行计划。
乙科技公司 | 后端开发工程师 | 2024.01-2025.12
负责跨团队交付协作，协调产品、研发与现场同事澄清职责边界、确认验收节点，并共同复核接口联调中的阻塞问题。
负责沟通团队内外的验收安排，确认风险同步的节奏，并在交付前组织相关同事复核方案与责任分工。
使用 Go 实现并发请求处理和连接复用。
使用 Python、Redis 与 Docker 完成后台任务。
使用 Kafka 支撑订单事件传递与异步处理。
技能
Go、MySQL、Python、Redis、Docker、Kafka、LangChain
个人优势
使用 LangChain 做学习验证并记录应用边界。
项目经历
订单系统项目 2018.01-2018.06：负责业务验收与协作排期。
检索平台项目 2020.01-2020.06：设计内容发布与权限核对流程。
"""
    duty = "负责订单业务需求拆解与接口验收安排。"
    jd = "\n".join([
        "岗位要求：熟悉 Go 与 MySQL，并能开发可靠接口。",
        "熟悉 Milvus 向量检索，能够设计检索验证流程。",
        "本科及以上学历，理解软件设计与工程验证流程。",
        duty,
        "了解 LangChain 的应用边界与验证方式。",
    ])
    # 原句都在输入正文中完整保留，模型抽取和网络调用不会参与这个确定性的正例。
    # 没有年限缺口或时间异常，四条必含 HR 加一个未用 code，恰好形成十五个槽位。
    resume = SimpleNamespace(name=None, years=3, skills=["Go", "MySQL", "Python", "Redis", "Docker", "Kafka", "LangChain"])
    job = JobInfo(title="后端开发工程师", company=None, required_years_min=None, required_years_max=None,
        education="本科及以上学历", must_have=["Go", "MySQL", "Milvus"], nice_to_have=["LangChain"], responsibilities=[duty])
    # 缺失学历产生风险槽位的独立 JD 要求句；缺失 Milvus 产生另一条技术缺口依据。
    return dict(resume_text=text, resume=resume, job=job, profile=None, diagnosis=diagnose(text, resume, job),
        hard=score_hard(resume, job, text), sem=_semantic(job), job_title=job.title, seed=SEED, jd_text=jd)


def _pure_go_inputs(target: str) -> dict:
    """给纯 Go 合成简历准备目标岗位输入，与真实文件验收完全分离。"""
    arguments = load_complete_inputs()
    # 对真实样例的副本替换整份文本，不在真实文件里删改 AI 交付事实。
    arguments["resume_text"] = PURE_GO_TEXT
    arguments["resume"] = SimpleNamespace(name=None, years=3, skills=["Go", "MySQL", "Redis"])
    # AI 目标的要求原句复用既有 JD-B，不在原始简历里假造 FDE 经历。
    if target == "AI 业务落地专家":
        arguments["job"] = JobInfo(title=target, company=None, required_years_min=1,
            required_years_max=3, education=None, must_have=["Python", "RAG", "LLM"],
            nice_to_have=["Dify", "LangChain"], responsibilities=["负责客户现场 AI 交付与文档知识库搭建"])
        arguments["jd_text"] = _jd_cases()["JD-B"]
    # 此函数的 JD 和简历都是明确标记的构造样本；给标题降级保留原文标题行。
    arguments["jd_text"] = "招聘" + target + "\n" + arguments["jd_text"]
    arguments["job_title"] = target
    arguments["hard"] = score_hard(arguments["resume"], arguments["job"], PURE_GO_TEXT)
    arguments["diagnosis"] = diagnose(PURE_GO_TEXT, arguments["resume"], arguments["job"])
    arguments["sem"] = _semantic(arguments["job"])
    return arguments


def case_01() -> None:
    """① 完整规则样例：十五槽位，6/2/2 技术面及五条 HR。"""
    result = _run(load_complete_inputs())
    # 同时查总数和每分区数量，防止十五个槽位只是错误配比凑出的巧合。
    _expect(result.meta["part_counts"] == {"hr": 5, "tech": 6, "scenario": 2, "project": 2}, "分区配额错误")
    _expect(len(result.slots) == 15, "真实文件样例应恰好十五槽位")
    # 输出走真实模型序列化往返，核实字段名和内部契约没有遗失。
    _expect(InterviewSkeleton.model_validate_json(result.model_dump_json()) == result, "骨架 JSON 不能往返")
    hr = [slot.category for slot in result.slots if slot.part == "hr"]
    _expect(hr[:4] == ["motivation", "salary", "plan", "collaboration"], "HR 必含顺序错误")
    arguments = _pure_go_inputs("Golang 后端开发工程师")
    # 没有条件问题时使用 other 补到五条，不能凭空触发时间或跨行问题。
    arguments["diagnosis"].findings.clear()
    _expect([slot.category for slot in _run(arguments).slots[:5]][-1] == "other", "HR 下限补足错误")
    # 多条时间发现合并为一个 timeline，不能再靠重复类别凑成八条 HR。
    arguments = _pure_go_inputs("AI 业务落地专家")
    arguments["job"].required_years_min = 6
    # 构造年限反例时同步给出原文要求，HR 年限与场景风险各有独立原句可用。
    arguments["jd_text"] = "要求6年工程经验，能够负责客户方案交付。\n" + arguments["jd_text"]
    arguments["jd_text"] += "\n具备6年业务开发经验，能够独立处理现场交付风险。"
    evidence = "某科技公司 | 后端开发工程师 | 2021.01-2023.12"
    arguments["diagnosis"].findings = [DiagnosisFinding(code="open_ended", severity="high", location=str(i),
        evidence=evidence, detail="时间需确认") for i in range(6)]
    result = _run(arguments)
    # 条件优先级保留原约定，只改变同类发现重复展开的错误行为。
    _expect([slot.category for slot in result.slots if slot.part == "hr"] == ["motivation", "salary", "plan", "collaboration",
        "career_switch", "years_gap", "timeline"], "HR 条件顺序或同类合并错误")


def case_02() -> None:
    """② 分区顺序固定，六个技术考点严格遵循 TECH_CATEGORY_ORDER。"""
    result = _run(load_complete_inputs())
    order = [PART_ORDER.index(slot.part) for slot in result.slots]
    # 使用分区序号检查单调性，不能只检查每个分区各自出现过。
    _expect(order == sorted(order), "槽位分区顺序错误")
    # 同分区检查不能代替技术考点的具体顺序检查。
    _expect(tuple(slot.category for slot in result.slots if slot.part == "tech") == TECH_CATEGORY_ORDER, "技术考点顺序错误")
    _expect(all(slot.category in CATEGORY_BY_PART[slot.part] for slot in result.slots), "出现未定义考点")
    # 独立合成输入给六个规则各留一条明确证据，逐类核实取句算法而非只查结构。
    text = """求职意向：Golang 开发工程师
期望薪资：按岗位薪资范围协商。
工作经历
甲科技公司 | 后端开发工程师 | 2019.01-2020.12
负责跨团队交付协作，协调产品、研发与现场同事澄清职责边界、确认验收节点，并共同复核联调中的阻塞问题。
负责沟通团队内外的验收安排，确认风险同步的节奏，并在交付前组织相关同事复核方案与责任分工。
使用 MySQL 分析慢查询并核对执行计划。
使用 Go 处理并发请求和连接复用。
使用 Python、Redis 与 Docker 完成后台任务。
使用 Kafka 支撑订单事件传递。
乙科技公司 | 后端开发工程师 | 2021.01-2022.12
使用 Golang 维护业务接口并完成灰度上线。
技能
Go、Golang、Python、MySQL、Redis、Kafka、Docker、LangChain
个人优势
使用 LangChain 做学习验证并记录应用边界。
项目经历
订单系统项目 2017.01-2017.06：负责订单业务验收。
检索平台项目 2018.01-2018.06：负责内容检索验收。
"""
    # Go/Golang 合并后频次最高；实现细节则应选三项不同技术聚集的经历句。
    arguments = load_complete_inputs()
    resume = SimpleNamespace(name=None, years=4, skills=["Go", "Python", "MySQL", "Redis", "Kafka", "Docker", "LangChain"])
    job = JobInfo(title="后端开发工程师", company=None, required_years_min=None, required_years_max=None,
                  education=None, must_have=["Go", "MySQL", "Milvus"], nice_to_have=["LangChain"],
                  responsibilities=["负责订单接口与任务调度"])
    jd = "熟悉 Go 与 MySQL；熟悉 Milvus 向量检索；了解 LangChain 优先；负责订单接口与任务调度。\n熟悉 Milvus 查询接口的交付风险与验证。"
    arguments.update(resume_text=text, resume=resume, job=job, jd_text=jd,
                     hard=score_hard(resume, job, text), diagnosis=diagnose(text, resume, job), sem=_semantic(job))
    selected = {slot.category: slot for slot in _run(arguments).slots if slot.part == "tech"}
    # 每个期望片段由本用例独立指定，不从被测函数的常量或输出反推正确答案。
    expected = {"tech_depth": "使用 Go 处理", "tech_gap": "熟悉 Milvus 向量检索",
                "impl_detail": "使用 Python、Redis 与 Docker", "tradeoff": "使用 Kafka 支撑",
                "debug": "使用 MySQL 分析", "growth": "使用 LangChain 做学习验证"}
    for category, phrase in expected.items():
        _expect(phrase in selected[category].basis, f"{category} 未按表中规则选依据")
    # 替代方案必须来自同一技能族，不能把任意语言都当作 Kafka 的替代方案。
    _expect(selected["tradeoff"].anchor["alternative"] in {"rabbitmq", "rocketmq", "pulsar"}, "同族替代项错误")
    # 不改变字段里的大小写，只在证据检索与频率比较时处理技能别名。
    _expect(selected["tech_depth"].anchor["skill"] == "Go", "核心技能原拼写丢失")


def case_03() -> None:
    """③ 逐条在对应原文核验 basis，悬空依据零条。"""
    arguments = load_complete_inputs()
    result = _run(arguments)
    sources = {"resume": arguments["resume_text"], "jd": arguments["jd_text"]}
    dangling = [slot.category for slot in result.slots if not find_original(sources[slot.basis_source], slot.basis)]
    _expect(dangling == [], f"悬空依据：{dangling}")
    # 恶意诊断证据不能因为来自已有产物就直接信任。
    arguments = load_complete_inputs()
    sources = {"resume": arguments["resume_text"], "jd": arguments["jd_text"]}
    arguments["diagnosis"].findings = [DiagnosisFinding(code="open_ended", severity="high", location="测试",
        evidence="原文里不存在的虚构公司项目时间", detail="不可信输入")]
    result = _run(arguments)
    _expect(all(find_original(sources[slot.basis_source], slot.basis) for slot in result.slots), "引用了假原句")
    timeline = next(slot for slot in result.slots if slot.category == "timeline")
    _expect(timeline.flag == "无可追溯依据，需人工补充" and timeline.anchor["evidence"] == [],
            "错误证据没有转为原文标题并标记人工补充")
    _expect(any("timeline" in warning and "需人工补充" in warning for warning in result.warnings), "标题降级未告警")


def case_04() -> None:
    """④ 技术依据互不复用；稀疏及空输入必须抛错，不产出虚假依据。"""
    result = _run(load_complete_inputs())
    tech = [slot for slot in result.slots if slot.part == "tech"]
    # 去掉标点并忽略大小写后再去重，不能把同一句的排版差异当作不同依据。
    _expect(len(tech) == 6, "技术槽位数量回归")
    _assert_unique_basis(tech)
    arguments = load_complete_inputs()
    arguments.update(resume_text="使用 Go 开发接口。", jd_text="", resume=SimpleNamespace(name=None, years=None, skills=["Go"]))
    arguments["diagnosis"].findings.clear()
    _expect_value_error(arguments, "未传 jd_text 时必须传 profile")
    # 保留稀疏原文的拒绝判据，提供非空 JD 以独立触发原有标题耗尽分支。
    arguments["jd_text"] = "招聘要求：熟悉 Go 接口开发。"
    _expect_value_error(arguments, "无可追溯且不重复的岗位标题依据")
    arguments.update(resume_text="", jd_text="")
    _expect_value_error(arguments, "未传 jd_text 时必须传 profile")
    arguments["jd_text"] = "招聘要求：熟悉 Go 接口开发。"
    _expect_value_error(arguments, "无可追溯且不重复的岗位标题依据")


def case_05() -> None:
    """⑤ 技能缺口槽位始终带固定迁移能力标记。"""
    arguments = _pure_go_inputs("AI 业务落地专家")
    result = _run(arguments)
    gap = next(slot for slot in result.slots if slot.category == "tech_gap")
    # 缺口标记关系到提问前提的诚实性，不允许只有警告而遗漏槽位上的固定说明。
    _expect(gap.flag == "简历未提及，考察迁移能力", "tech_gap 标记错误")
    # 首条 missing_skill 为 Python；basis 应定位 JD 原句，而不是重新拼一句要求。
    _expect(gap.anchor["skill"] == "Python" and gap.basis_source == "jd", "缺口未取第一项")
    _expect(find_original(arguments["jd_text"], gap.basis), "缺口技能引用了非 JD 原句")


def case_06() -> None:
    """⑥ 同 seed 抽同一对，换 seed 至少一次改变，且全局随机状态不变。"""
    projects = extract_projects(PROJECT_TEXT)
    before = deepcopy(projects)
    # 调用前记录全局 RNG 状态，证明局部 seed 没有影响其他功能的随机行为。
    state = random.getstate()
    first = pick_projects(projects, SEED)
    _expect(first == pick_projects(projects, SEED), "同 seed 项目抽取不同")
    # 比较无序项目对，不能把仅交换输出顺序误判成抽中了不同项目。
    names = {project.name for project in first}
    _expect(any({p.name for p in pick_projects(projects, str(seed))} != names for seed in range(16)), "换 seed 始终同一对")
    _expect(projects == before and random.getstate() == state, "抽样修改输入或全局随机状态")


def case_07() -> None:
    """⑦ 项目名称不同且时间不重叠，覆盖相邻行日期及共享月份边界。"""
    projects = extract_projects(PROJECT_TEXT)
    _expect(len(projects) == 3, "三种项目日期格式未完整识别")
    # 关键词本身也可作为项目名，Demo 不能因为前面没有其他字符而被漏掉。
    _expect(len(extract_projects("Demo\n2023年3月")) == 1, "单月份或独立 Demo 未识别")
    left, right = pick_projects(projects, SEED)
    # 名称与区间分别断言，不能因时间不同就允许同一项目占据两个槽位。
    _expect(normalize_text(left.name).casefold() != normalize_text(right.name).casefold(), "抽中同名项目")
    _expect(left.end < right.start or right.end < left.start, "项目时间重叠")
    # 两个项目共享结束/开始月仍属于重叠，不能只按年份粗略比较。
    overlap = extract_projects("订单系统 2023.01-2023.06\n通知平台 2023.06-2023.12")
    _expect(len(pick_projects(overlap, SEED)) == 1, "闭区间重叠未拦截")
    same = extract_projects("订单系统 2021.01-2021.06\n订单系统 2022.01-2022.06")
    _expect(len(pick_projects(same, SEED)) == 1, "同名项目未拦截")
    # 必须遍历随机排列中的可行组合，不能抽到坏对就直接退化。
    _expect(len(pick_projects([*overlap, projects[0]], SEED)) == 2, "存在合法项目对却退化")


def case_08() -> None:
    """⑧ 纯 Go 合成简历 × AI 业务落地专家：判定跨行并保留依据。"""
    arguments = _pure_go_inputs("AI 业务落地专家")
    # 使用用户确认的纯 Go 合成样本，避免对双意向原文件预设错误结论。
    switched, reason = is_career_switch(PURE_GO_TEXT, arguments["resume"], arguments["job"], arguments["job_title"])
    _expect(switched and "求职意向" in reason, "意向跨行规则未触发")
    slot = next(slot for slot in _run(arguments).slots if slot.category == "career_switch")
    _expect(slot.anchor["career_switch"] is True and slot.anchor["career_switch_reason"] == reason, "跨行依据未记录")
    # 去掉求职意向再测最近职位，确保第二条不是被第一条遮住的死分支。
    no_intent = PURE_GO_TEXT.split("\n", 1)[1]
    _expect("最近职位" in is_career_switch(no_intent, arguments["resume"], arguments["job"], arguments["job_title"])[1], "最近职位规则未触发")
    # 只给技能且正文无目标族，独立验证第三条与不可判定分支。
    _expect("技能族" in is_career_switch("技能：Go、MySQL", arguments["resume"], arguments["job"], arguments["job_title"])[1], "技能族规则未触发")
    unknown = SimpleNamespace(name=None, years=None, skills=[])
    _expect(is_career_switch("姓名：测试", unknown, arguments["job"], arguments["job_title"]) == (False, "未能判定"), "未知被误判跨行")


def case_09() -> None:
    """⑨ 纯 Go 合成简历 × Golang 后端开发工程师：不出跨行槽位。"""
    arguments = _pure_go_inputs("Golang 后端开发工程师")
    result = _run(arguments)
    # Golang 与 Go 复用既有岗位族匹配，不应因为英文拼写不同被当作换行业。
    _expect(not result.meta["career_switch"], "同族被误判跨行")
    # 直接检查槽位，不仅相信 meta 中自报的布尔结果。
    _expect(not any(slot.category == "career_switch" for slot in result.slots), "同族仍出现跨行槽位")


def case_10() -> None:
    """⑩ 同输入三次完整输出一致；种子优先级、难度分维与六成边界正确。"""
    arguments = load_complete_inputs()
    outputs = [_run(arguments).model_dump() for _ in range(3)]
    # 比较完整序列化字典，连警告顺序和 meta 都必须一致，不能只比抽中的项目。
    _expect(outputs[0] == outputs[1] == outputs[2], "同输入输出不确定")
    # seed 明确传入时优先于 plan_id；未传时才按 hex 字符串本身求 SHA-256。
    arguments["plan_id"] = "a" * 32
    _expect(_run(arguments).project_seed == SEED, "seed 优先级错误")
    arguments.pop("seed")
    _expect(_run(arguments).project_seed == sha256(("a" * 32).encode()).hexdigest()[:16], "plan_id 种子派生错误")
    arguments.pop("plan_id")
    result = _run(arguments)
    _expect(result.project_seed == "0" * 16 and any("未提供 seed" in w for w in result.warnings), "默认 seed 未告警")
    # 分项使用既有 LlmScore/LlmItem 属性形状，避免导入其模型调用模块。
    arguments["llm_score"] = SimpleNamespace(status="ok", items=[
        SimpleNamespace(key="stack_depth", score=7, max_score=12),
        SimpleNamespace(key="domain_fit", score=6, max_score=10),
        SimpleNamespace(key="proof_strength", score=4, max_score=8),
    ])
    for mode, base in (("junior", 1), ("standard", 2), ("senior", 3)):
        # 同一低分信号在 senior 档只能保持三星，不能再次增加到四星。
        arguments["difficulty"] = mode
        result = _run(arguments)
        for slot in result.slots:
            expected = min(3, base + int(slot.part in ("tech", "project")))
            _expect(slot.difficulty == expected, f"{mode}/{slot.category} 难度错误")
    # domain_fit 恰好六成不能加档；纯整数也不能凭空推断三个子项。
    arguments.update(difficulty="standard", llm_score=0)
    result = _run(arguments)
    _expect(all(slot.difficulty == 2 for slot in result.slots), "整数总分被猜成分项")
    _expect(any("无法按子项调整" in warning for warning in result.warnings), "整数分数未说明限制")
    arguments["profile"] = JobProfile(title="后端开发", source="inferred", confirmed=True,
        must_have=["Milvus", "Kafka"],
        responsibilities=list(arguments["job"].responsibilities), note="推测岗位画像")
    _expect(_run(arguments).source == "inferred", "传入原文后丢失了推测画像的来源声明")
    arguments["jd_text"] = None
    # 未传 JD 时改为诚实引用画像，不能给画像要求伪造 jd 来源。
    inferred = _run(arguments)
    _expect(next(slot for slot in inferred.slots if slot.category == "tech_gap").basis_source == "profile",
            "未传 JD 时没有引用画像要求")
    arguments = load_complete_inputs()
    arguments["profile"] = JobProfile(title="后端开发", source="inferred", confirmed=True,
        must_have=["熟悉 Milvus 向量检索，能够设计检索验证流程。", "能够识别检索交付风险并设计故障恢复方案"],
        responsibilities=list(arguments["job"].responsibilities), note="推测岗位画像")
    arguments["jd_text"] = None
    inferred = _run(arguments)
    _expect(next(slot for slot in inferred.slots if slot.category == "tech_gap").basis_source == "profile",
            "画像技能要求被伪装成真实 JD")


def case_11() -> None:
    """⑪ 构建阶段禁止文件、环境、网络和全局随机调用，单次小于 100ms。"""
    arguments = load_complete_inputs()
    # 真实案例也必须经过构建禁网与禁文件检查，材料读取均在进入补丁前完成。
    beike_arguments = load_beike_inputs()
    inferred_arguments = load_inferred_inputs()
    # 禁止的模块可能由测试框架引入，因此只归责业务导入与实际构建路径。
    forbidden = {"openai", "httpx", "asyncio", "app.schemas", "app.scoring_llm", "app.scoring_semantic"}
    _expect(not forbidden.intersection(BUSINESS_IMPORTS), "业务导入了模型客户端或异步依赖")
    original_import = __import__
    # 保留真实导入函数以处理无关的标准库请求，避免检查器本身破坏正常运行。

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        """禁止构建过程中临时导入模型、网络客户端及异步入口。"""
        # 不只看模块是否曾经加载，缓存命中的 import 语句同样必须被禁止。
        if any(name == item or name.startswith(item + ".") for item in forbidden):
            raise AssertionError(f"禁止业务导入 {name}")
        return original_import(name, *args, **kwargs)
    # 阻断真实 I/O 入口，违规调用必须抛错，不能只凭源码自称纯函数。
    error = AssertionError("build 阶段禁止 I/O、环境读取或全局随机")
    with patch("builtins.open", side_effect=error), patch("io.open", side_effect=error), \
         patch("os.getenv", side_effect=error), patch.object(socket.socket, "connect", side_effect=error), \
         patch.object(socket.socket, "connect_ex", side_effect=error), patch("socket.create_connection", side_effect=error), \
         patch("random.sample", side_effect=error), patch("random.seed", side_effect=error), \
         patch("random.random", side_effect=error), patch("builtins.__import__", side_effect=guarded_import):
        _run(arguments)
        _run(beike_arguments)
        _run(inferred_arguments)
    # 文件与网络入口恢复发生在上下文退出时，不给后续项目边界测试留下全局补丁。
    # 纯度检查覆盖实际导入后的构建，不把未执行的函数当作零模型证明。
    _expect(max(MEASUREMENTS) < 100, f"最慢单次 build {max(MEASUREMENTS):.2f}ms 超过 100ms")


def case_12() -> None:
    """⑫ 项目缺失仅用原文岗位标题并标记人工补充；标题用尽必须报错。"""
    arguments = load_complete_inputs()
    arguments["resume_text"] = arguments["resume_text"].split("项目经历", 1)[0]
    arguments["jd_text"] = "招聘后端开发工程师\n" + arguments["jd_text"]
    _expect(extract_projects(arguments["resume_text"]) == [], "无项目段文本被误抽取")
    result = _run(arguments)
    projects = [slot for slot in result.slots if slot.part == "project"]
    # 要保留两个固定项目槽位，并对两个槽位都说明定位失败，不能直接丢一题。
    _expect(len(projects) == 2 and all(slot.flag == "无可追溯依据，需人工补充" for slot in projects), "无项目段降级标记错误")
    _expect(all(any(find_original(title, slot.basis) for slot in projects)
                for title in (arguments["job_title"], "招聘后端开发工程师")), "缺项目未使用原文标题")
    _expect(all(slot.anchor["original_flag"] == "未定位到项目段" for slot in projects), "丢失原项目定位状态")
    # 单项目用独立合成段落，不修改任务书指定的真实 DOCX 文件。
    arguments["resume_text"] += "\n项目经历\n项目名称：订单系统\n2020.01-2020.06\n负责订单接口。"
    projects = [slot for slot in _run(arguments).slots if slot.part == "project"]
    _expect(projects[0].flag is None and projects[1].flag == "无可追溯依据，需人工补充", "单项目退化错误")
    _expect(projects[1].anchor["original_flag"] == "未定位到第二段项目", "单项目状态丢失")
    arguments["resume_text"] = arguments["resume_text"].split("项目经历", 1)[0]
    arguments["jd_text"] = arguments["jd_text"].split("\n", 1)[1]
    _expect_value_error(arguments, "岗位标题依据")


def case_13() -> None:
    """⑬ 所有分区使用统一入口；质量过滤后按最长技术句、非空原句降级。"""
    arguments = load_beike_inputs()
    # 观察所有分区实际使用统一入口，不能只给新增用例单独套一层过滤。
    with patch.object(module, "pick_basis", wraps=pick_basis) as unified:
        result = _run(arguments)
    seen = {text for call in unified.call_args_list for text in call.args[0]}
    # 观察真实调用的候选集合，而非给旧输出另做一遍过滤后再冒充修复结果。
    _expect(all(any(find_original(text, slot.basis) for text in seen) for slot in result.slots),
            "有槽位绕过统一依据入口")
    boundary = "负责开发订单接口模块"
    selected, reason = pick_basis(["内容:", boundary], category="test")
    _expect(find_original(boundary, selected) and reason == "", "首选事实句过滤回归")
    # 以下均不满足首选事实句条件，显式检验 D-N1-05 的 a/b 两级顺序。
    short = "Go开发"
    longer = "Go/MySQL"
    _expect(find_original(longer, pick_basis(["内容:", short, longer], category="test")[0]), "没有选择池内最长技术句")
    _expect(find_original("普通业务信息", pick_basis(["", "普通业务信息"], category="test")[0]), "不含技术词的原句被丢弃")
    _expect(pick_basis([boundary + "\n"], category="test")[0] == "", "strip 掩盖了换行")
    _expect(pick_basis([], category="test") == ("", "test 无可用依据"), "空池把 reason 写成了 basis")
    alias = "熟悉Postgres并理解其事务隔离机制"
    selected, reason = pick_basis([alias], category="test")
    _expect(find_original(alias, selected) and reason == "", "未复用硬评分技术别名")


def case_14() -> None:
    """⑭ 真简历抽出至少三段项目，名称归一化可查，教育段零条。"""
    arguments = load_beike_inputs()
    text = arguments["resume_text"]
    projects = extract_projects(text)
    # 数量只是必要条件，下面还需检查真实名称、项目类型、时间来源及教育误报。
    _expect(len(projects) >= 3, f"真实项目仅抽到 {len(projects)} 段")
    # 教育正文写了系统开发也不能被继承到项目里；核查原文而不只查抽取数量。
    education = text.split("教育经历", 1)[-1].split("资格证书", 1)[0]
    for project in projects:
        _expect(find_original(text, project.name), "项目名不能归一化回查原文")
        _expect(project.name not in education and project.period != "2020-2024", "教育段被当成项目")
        _expect(bool(re.search(r"系统|平台|项目|Demo|中台|模块|链路", project.name, re.I)), "项目名不满足名词约束")
        _expect(project.name not in {"系统", "平台", "项目", "模块", "链路"}, "抽到了泛化项目名")
    names = {project.name for project in projects}
    _expect("恒祥达Saas平台管理系统" in names and "合伙人功能模块" in names, "公司行时间未继承到嵌套项目")
    selected = pick_projects(projects, SEED)
    # 同一公司子项目继承同一个区间，不能为凑两段而给它们虚构不重叠的月份。
    _expect(len(selected) == 2 and (selected[0].end < selected[1].start or selected[1].end < selected[0].start), "随机项目时间重叠")
    # 无技术词的无标题教育段也要拦截；「工具」和「通知中心」不能擅自加入项目名白名单。
    _expect(extract_projects("某大学 | 本科 | 2020-2024\n系统学习数据结构和核心课程") == [], "无标题教育段未拦截")
    _expect(extract_projects("检索工具 2023.01-2023.06\n通知中心 2024.01-2024.06") == [], "放宽了项目名词过滤")


def case_15() -> None:
    """⑮ HR 类别无重复，时间发现合并为一题并保留全部原始证据。"""
    arguments = load_beike_inputs()
    result = _run(arguments)
    hr = [slot for slot in result.slots if slot.part == "hr"]
    categories = [slot.category for slot in hr]
    # 同时检查类别唯一性和 HR 条数，不能通过删光条件槽位来获得「无重复」。
    _expect(5 <= len(hr) <= 8 and len(categories) == len(set(categories)), "HR 条数或同类合并错误")
    # 合并的是槽位，不是证据；不能丢掉后面的时间段，只保留第一条 finding。
    timeline = [slot for slot in hr if slot.category == "timeline"]
    expected = list(dict.fromkeys(line.strip() for finding in arguments["diagnosis"].findings
        if finding.code in ("timeline_overlap", "open_ended") for line in finding.evidence.splitlines()
        if find_original(arguments["resume_text"], line.strip())))
    _expect(len(timeline) == 1 and len(timeline[0].anchor["evidence"]) >= 2, "未保留多段时间证据")
    _expect(timeline[0].anchor["evidence"] == expected, "时间线合并丢失、改写或打乱了原始证据")
    # 明确检查单行化，不能把完整的多行 finding 直接复制到最终 basis 中。
    _expect("\n" not in timeline[0].basis and "\r" not in timeline[0].basis, "合并后的时间线依据仍是多行")
    # 没有任何条件问题时仍以一个尚未使用的 HR code 补到五条，不重复必含四类。
    arguments = _pure_go_inputs("Golang 后端开发工程师")
    arguments["diagnosis"].findings.clear()
    hr = [slot for slot in _run(arguments).slots if slot.part == "hr"]
    _expect(len(hr) == 5 and len({slot.category for slot in hr}) == 5, "HR 补位使用了重复类别")


def case_16() -> None:
    """⑯ 构造十五条与真简历十六条依据全部参与去重，零豁免、零重复。"""
    complete = _run(load_complete_inputs())
    _expect(len(complete.slots) == 15, "完整样例不足十五条")
    _assert_unique_basis(complete.slots)
    _expect(complete.meta["status"] == "ok", "完整骨架被错误标记为待复核")
    real = _run(load_beike_inputs())
    _expect(len(real.slots) == 16, "真简历必须有十六条依据参与去重")
    _assert_unique_basis(real.slots)
    # 真实材料的不足必须显式报告，不能以任意其他原句填满并冒充完整依据。
    missing = [slot.category for slot in real.slots if slot.flag == "无可追溯依据，需人工补充"]
    _expect(real.meta["missing_categories"] == missing, "缺失统计与实际槽位不一致")


def case_17() -> None:
    """⑰ tech_gap 只用含该技能的 JD 行，来源为 jd，并保留完整 skill 与 jd_lines。"""
    arguments = load_beike_inputs()
    with patch.object(module, "pick_basis", wraps=pick_basis) as unified:
        result = _run(arguments)
    gap = next(slot for slot in result.slots if slot.category == "tech_gap")
    skill = gap.anchor.get("skill")
    _expect(bool(skill) and skill.casefold() in gap.basis.casefold() and gap.basis_source == "jd", "缺口依据或来源错误")
    expected = [line for line in arguments["jd_text"].splitlines() if skill.casefold() in line.casefold()]
    # anchor 保存完整原池，既包括选中句，也包括未选中行，便于复查为什么没有借用简历。
    _expect(gap.anchor.get("jd_lines") == expected and any(find_original(line, gap.basis) for line in expected),
            "未保留全部含技能的 JD 原行")
    # 直接观察传入统一入口的池，不能仅凭最后选中一句来自 JD 就认定池没有混入简历。
    calls = [call for call in unified.call_args_list if call.kwargs.get("category") == "tech_gap"]
    # 去重可以缩小候选集合，但不能出现不属于这份 JD 技能池的额外原句。
    _expect(bool(calls) and all(any(find_original(line, text) for line in expected)
                             for call in calls for text in call.args[0]), "缺口池混入了其他语义原句")


def case_18() -> None:
    """⑱ scenario_design 回查未覆盖职责；各语义池耗尽后不向其他池借句。"""
    arguments = load_complete_inputs()
    result = _run(arguments)
    duty = arguments["sem"].per_item[0].item
    # 未覆盖职责本身就是定位凭据，依据和来源统一按归一化口径回链。
    design = next(slot for slot in result.slots if slot.category == "scenario_design")
    _expect(find_original(duty, design.basis) and design.anchor["responsibility"] == duty, "未覆盖职责被改写或替换")
    # 首尾空白不改变原句回链结果，依据仍来自这一条职责。
    padded = load_complete_inputs()
    padded_duty = "  " + duty + "  "
    padded["jd_text"] = padded["jd_text"].replace(duty, padded_duty)
    padded["sem"].per_item[0].item = padded_duty
    padded_design = next(slot for slot in _run(padded).slots if slot.category == "scenario_design")
    _expect(find_original(padded_duty, padded_design.basis), "未覆盖职责不能归一化回查")
    risk = next(slot for slot in result.slots if slot.category == "scenario_risk")
    _expect(find_original("本科及以上学历，理解软件设计与工程验证流程。", risk.basis)
            and risk.basis_source == "jd", "风险未取对应学历要求句")
    # 项目只允许本段首句，不能选择后续一句、另一个项目或普通经历中的句子。
    picked = pick_projects(extract_projects(arguments["resume_text"]), SEED)
    project_slots = [slot for slot in result.slots if slot.part == "project"]
    _expect(len(project_slots) == len(picked) and all(find_original(project.basis, slot.basis)
            for slot, project in zip(project_slots, picked)), "项目依据跨段或不是首句")

    # 单句职责预留给场景设计，缺口题使用同技能的另一条 JD 原行。
    repeated = load_complete_inputs()
    gap_line = next(line for line in repeated["jd_text"].splitlines() if "Milvus" in line)
    repeated["sem"].per_item[0].item = gap_line
    repeated["jd_text"] += "\n熟悉 Milvus 查询接口并能设计验证方案。"
    reserved = _run(repeated)
    design = next(slot for slot in reserved.slots if slot.category == "scenario_design")
    gap = next(slot for slot in reserved.slots if slot.category == "tech_gap")
    _expect(find_original(gap_line, design.basis) and normalize_text(gap.basis) != normalize_text(gap_line),
            "场景原句被前序槽位耗尽")

    # 职责抽取串只在简历出现时仍须回 JD 对齐，不能直接借简历原句标作 JD。
    wrong_source = load_complete_inputs()
    resume_only = "负责开发订单接口模块"
    wrong_source["resume_text"] += "\n" + resume_only
    wrong_source["sem"].per_item[0].item = resume_only
    # 新句只加到简历，JD 中其他合格句保持不变，隔离检验来源边界而非质量过滤。
    aligned = next(slot for slot in _run(wrong_source).slots if slot.category == "scenario_design")
    _expect(find_original(wrong_source["jd_text"], aligned.basis) and aligned.basis_source == "jd"
            and aligned.anchor["item"] == resume_only, "改写职责未回 JD 对齐或借用了简历来源")

    # 没有未覆盖职责时，即便 job.responsibilities 非空，也不允许回退到另一条已覆盖职责。
    covered = load_complete_inputs()
    for item in covered["sem"].per_item:
        item.covered = True
    covered["jd_text"] = "招聘后端开发工程师\n" + covered["jd_text"]
    # 修改覆盖标志不会删除原文，后续缺失应完全来自该类别的语义池为空。
    exhausted = next(slot for slot in _run(covered).slots if slot.category == "scenario_design")
    _expect(find_original(covered["jd_text"], exhausted.basis) and exhausted.basis_source == "jd"
            and exhausted.flag == "无可追溯依据，需人工补充",
            "没有未覆盖职责却扩大了场景池")

    # JD 唯一技能行首选过滤失败后，按新规则仍在本池降级，绝不借简历句。
    bad_gap = load_complete_inputs()
    bad_line = "技术栈：Milvus向量检索接口设计与开发"
    bad_gap["jd_text"] = "\n".join(line for line in bad_gap["jd_text"].splitlines() if "Milvus" not in line) + "\n" + bad_line
    bad_gap["resume_text"] += "\n负责开发 Milvus 检索服务并设计召回验证流程。"
    # 故意保留旧扣分产物模拟上游信息不一致，池边界仍应保护缺口题不引用简历。
    exhausted = next(slot for slot in _run(bad_gap).slots if slot.category == "tech_gap")
    _expect(find_original(bad_line, exhausted.basis) and exhausted.basis_source == "jd", "缺口过滤失败后借用了简历句")
    _expect(exhausted.anchor["jd_lines"] == [bad_line] and exhausted.anchor["skill"] == "Milvus", "降级丢失了技能及原池")

    # 年限/学历池确实用尽时，才按明确允许的语义规则选另一条缺失技能 JD 行。
    risk_fallback = load_complete_inputs()
    alternative = "熟悉 Milvus 数据验证并能排查检索故障。"
    risk_fallback["jd_text"] = "\n".join(line for line in risk_fallback["jd_text"].splitlines() if "学历" not in line) + "\n" + alternative
    # 第一条缺口行已被 tech_gap 使用；只有新增的同技能 JD 行才是合法的下一候选。
    selected_risk = next(slot for slot in _run(risk_fallback).slots if slot.category == "scenario_risk")
    _expect(find_original(alternative, selected_risk.basis) and selected_risk.anchor["risk_type"] == "missing_skill",
            "风险兜底越过了允许的缺口 JD 池")

    # 场景与项目只能共享同一原句时，项目只能降级为原文标题，不能借另一段。
    project_used = load_complete_inputs()
    first = pick_projects(extract_projects(project_used["resume_text"]), SEED)[0]
    project_used["sem"].per_item[0].item = first.basis
    project_used["jd_text"] += "\n" + first.basis
    selected_project = next(slot for slot in _run(project_used).slots if slot.part == "project" and slot.anchor.get("project") == first.name)
    _expect(find_original(project_used["job_title"], selected_project.basis)
            and selected_project.flag == "无可追溯依据，需人工补充", "项目首句被使用后越池补句")


def case_19() -> None:
    """⑲ 重复依据独立断言与模型均报错；basis_source 必填且不可为 None。"""
    result = _run(load_complete_inputs())
    payload = result.model_dump()
    # 改变大小写与标点仍是同一依据，负例必须验证归一化后的检查而非只比较原始字符串。
    payload["slots"][1]["basis"] = payload["slots"][0]["basis"].upper() + "，"
    payload["slots"][1]["basis_source"] = payload["slots"][0]["basis_source"]
    duplicates = [Slot.model_validate(item) for item in payload["slots"]]
    # 单题字段本身均合法，必须由跨槽位唯一性检查发现这个反例，而非其他字段报错。
    try:
        _assert_unique_basis(duplicates)
    except AssertionError as error:
        _expect("重复" in str(error), "重复断言的错误原因不明确")
    else:
        raise AssertionError("构造重复依据未触发独立断言")
    try:
        InterviewSkeleton.model_validate(payload)
    except ValueError as error:
        _expect("依据重复" in str(error), "模型重复校验原因不明确")
    else:
        raise AssertionError("模型接受了重复真实依据")
    first = result.slots[0].model_dump()
    missing_source = {key: value for key, value in first.items() if key != "basis_source"}
    for invalid in (missing_source, {**first, "basis_source": None}, {**first, "basis_source": "other"}):
        try:
            Slot.model_validate(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("basis_source 接受了缺省、None 或其他来源")


def case_20() -> None:
    """⑳ 四类说明文字逐项拒绝；即使文字出现在原文中也不能当作依据。"""
    slot = _run(load_complete_inputs()).slots[0]
    for note in ("可追溯原句兜底", "可追溯", "占位", "TODO", "todo"):
        basis = f"负责开发接口模块，{note}说明。"
        _expect(pick_basis([basis], category="test")[0] == "", "说明文字进入了统一入口输出")
        # 绕过单槽位模型构造坏产物，证明独立验收本身也会拦截，不能只靠模型报错。
        bad = slot.model_copy(update={"basis": basis})
        try:
            _assert_original_basis([bad], basis, "")
        except AssertionError as error:
            _expect("说明文字" in str(error), "说明文字不是被专项断言拦截")
        else:
            raise AssertionError("检查脚本接受了说明 basis")
        try:
            Slot.model_validate({**slot.model_dump(), "basis": basis})
        except ValueError:
            pass
        else:
            raise AssertionError("Slot 接受了说明 basis")


def case_21() -> None:
    """㉑ 污染末尾槽位必须触发原文硬错误；末尾按实际命中重新推导来源。"""
    arguments = load_complete_inputs()
    take = module._take_basis
    forged = "这条虚构经历在简历和招聘原文中均不存在" * 3

    def corrupt(*args: object, **kwargs: object) -> tuple[str, str, bool]:
        """先正常选句，只在最后一个槽位注入未命中原文的非空句。"""
        result = take(*args, **kwargs)
        return (forged, "", False) if kwargs["category"] == "project_random_2" else result

    with patch.object(module, "_take_basis", side_effect=corrupt):
        _expect_value_error(arguments, f"project_random_2 的依据不在原文里：{forged[:30]}")
    slot_type = module.Slot

    def wrong_origin(**kwargs: object) -> Slot:
        """构造合法槽位后故意篡改来源，独立检验末尾是否重新实查。"""
        result = slot_type(**kwargs)
        result.basis_source = "jd" if result.basis_source == "resume" else "resume"
        return result

    # 同文同时存在于双方时按用户要求优先命中简历。
    arguments["jd_text"] += "\n" + arguments["resume_text"].splitlines()[2]
    with patch.object(module, "Slot", side_effect=wrong_origin):
        result = _run(arguments)
    _expect(result.slots[0].basis_source == "resume", "同时命中双方时没有优先简历")
    # 检查器也必须能发现一个仍可追溯但来源错误的值。
    bad = result.slots[0].model_copy(update={"basis_source": "jd"})
    try:
        _assert_original_basis([bad], arguments["resume_text"], arguments["jd_text"])
    except AssertionError as error:
        _expect("来源错误" in str(error), "未单独发现来源不一致")
    else:
        raise AssertionError("检查脚本接受了错误来源")


def case_22() -> None:
    """㉒ 两份真实 DOCX 逐条核验原文、说明文字、来源、唯一性及三次完整一致。"""
    # 必须实际读取两份原件、各自调用构建器，绝不以构造输入替代真产物。
    for loader, total in ((load_go_beike_inputs, 16), (load_beike_inputs, 16)):
        arguments = loader()
        results = [_run(arguments) for _ in range(3)]
        _expect(results[0].model_dump() == results[1].model_dump() == results[2].model_dump(), "真实产物三次不一致")
        result = results[0]
        _expect(len(result.slots) == total, f"真实产物数量错误，预期 {total}")
        _expect(result.meta["part_counts"] == {"hr": total - 10, "tech": 6, "scenario": 2, "project": 2}, "真实产物配额回归")
        _assert_original_basis(result.slots, arguments["resume_text"], arguments["jd_text"])
        _assert_unique_basis(result.slots)
        gap = next(slot for slot in result.slots if slot.category == "tech_gap")
        _expect(len(gap.anchor["jd_lines"]) >= 1 and any(find_original(line, gap.basis) for line in gap.anchor["jd_lines"]),
                "真产物技能行为空或依据离池")
        _expect(all(gap.anchor["skill"].casefold() in line.casefold() for line in gap.anchor["jd_lines"]), "技能行不含目标技能")
        original = next(item.item for item in arguments["sem"].per_item if not item.covered)
        design = next(slot for slot in result.slots if slot.category == "scenario_design")
        _expect(find_original(arguments["jd_text"], design.basis) and original == design.anchor["item"]
                and design.basis == design.anchor["aligned"],
                "真产物未回链未覆盖职责")
        if loader is load_beike_inputs:
            _expect(gap.anchor["skill"] == "知识库" and gap.basis_source == "jd", "真简历未考察知识库缺口")


def case_23() -> None:
    """㉓ 无 JD 且无画像、JD 确无技能、技能池过滤空分别硬拒绝；短职责仍可回查。"""
    for empty in (None, "", " \t\r\n"):
        arguments = load_complete_inputs()
        arguments["jd_text"] = empty
        # 故意给另一项错误，证明模式校验先于其他参数和选句校验。
        arguments["difficulty"] = "invalid"
        _expect_value_error(arguments, "未传 jd_text 时必须传 profile")
    arguments = load_complete_inputs()
    arguments.pop("jd_text")
    _expect_value_error(arguments, "未传 jd_text 时必须传 profile")
    arguments = load_complete_inputs()
    arguments["jd_text"] = "\n".join(line for line in arguments["jd_text"].splitlines() if "Milvus" not in line)
    _expect_value_error(arguments, "tech_gap 的 JD 中确实没有该技能原句：Milvus")
    arguments["jd_text"] += "\nTODO：熟悉 Milvus 检索设计。"
    _expect_value_error(arguments, "tech_gap 的JD技能池被过滤空")
    # 只有技能原句却被职责预留占用时，同样硬报池过滤空，不得换成标题。
    reserved = load_complete_inputs()
    reserved["sem"].per_item[0].item = next(line for line in reserved["jd_text"].splitlines() if "Milvus" in line)
    _expect_value_error(reserved, "tech_gap 的JD技能池被过滤空")
    arguments = load_complete_inputs()
    original = arguments["sem"].per_item[0].item
    short = "场景梳理"
    arguments["jd_text"] = arguments["jd_text"].replace(original, short)
    arguments["sem"].per_item[0].item = short
    design = next(slot for slot in _run(arguments).slots if slot.category == "scenario_design")
    _expect(find_original(short, design.basis), "过滤失败后没有降级到该职责原文")
    arguments = load_complete_inputs()
    arguments["jd_text"] = arguments["jd_text"].replace("本科及以上学历，理解软件设计与工程验证流程。", "本科学历")
    risk = next(slot for slot in _run(arguments).slots if slot.category == "scenario_risk")
    _expect(find_original("本科学历", risk.basis), "主风险池有原句时却越到缺口池")


def case_24() -> None:
    """㉔ ★ 真简历 × 真 JD 必须 build 成功，16 条可追溯；职责及年限允许跨行。"""
    arguments = load_beike_inputs()
    result = _run(arguments)
    _expect(len(result.slots) == 16, "真简历 × 贝壳 JD 未成功生成十六条依据")
    _assert_original_basis(result.slots, arguments["resume_text"], arguments["jd_text"])
    original = next(item for item in arguments["sem"].per_item if not item.covered)
    raw_duty = original.item
    # 仅改变内存副本的折行与标点排版，不改原 JD 文件或业务内容。
    folded_duty = raw_duty[:6] + "\n" + raw_duty[6:18] + "\n" + raw_duty[18:]
    arguments["jd_text"] = arguments["jd_text"].replace(raw_duty, folded_duty)
    original.item = raw_duty.replace(",", "，").rstrip("。")
    _expect(find_original(arguments["jd_text"], original.item), "折行反例不能归一化命中 JD")
    _expect(not any(find_original(line, original.item) for line in arguments["jd_text"].splitlines()),
            "折行反例仍能从 JD 单行取到完整职责")
    results = [_run(arguments) for _ in range(3)]
    _expect(results[0].model_dump() == results[1].model_dump() == results[2].model_dump(), "折行真产物三次不一致")
    design = next(slot for slot in results[0].slots if slot.category == "scenario_design")
    _expect(len(results[0].slots) == 16 and find_original(arguments["jd_text"], design.basis),
            "跨行职责导致真实骨架失败或不可追溯")
    _expect(find_original(arguments["jd_text"], design.basis) and design.anchor["item"] == original.item
            and design.anchor["cos"] == original.best_sim and design.basis_source == "jd", "职责原文或相似度锚点丢失")
    # item 本身也有换行时仍回原文行，不能要求 basis 与跨行 item 逐字相等。
    original.item = folded_duty
    multiline = next(slot for slot in _run(arguments).slots if slot.category == "scenario_design")
    _expect(multiline.basis in arguments["jd_text"].splitlines()
            and multiline.anchor["item"] == folded_duty and multiline.anchor["aligned"] == multiline.basis,
            "多行职责串没有回 JD 原行对齐")

    # 对另外两个 JD 要求槽位独立取证，保留真实跨行证据，不降级成标题或技能句。
    years = load_complete_inputs()
    years["job"].required_years_min = 6
    years_evidence = "要求6\n年以上开发经验，能够设计并交付业务接口。"
    years["jd_text"] = years_evidence + "\n" + years["jd_text"]
    # HR 已消费第一条年限证据，风险题仍须有本家族内独立的 JD 要求。
    years["jd_text"] += "\n具备6年工程交付经验，能够设计风险验证方案。"
    years["hard"] = score_hard(years["resume"], years["job"], years["resume_text"])
    next(item for item in years["hard"].deductions if item.type == "years_short").jd_evidence = years_evidence
    selected = {slot.category: slot for slot in _run(years).slots}
    _expect(find_original(years_evidence, selected["years_gap"].basis) and "\n" in selected["years_gap"].basis,
            "年限差距槽位拒绝跨行要求")
    risk = load_complete_inputs()
    education = "本科及以上学\n历，理解软件设计与工程验证流程。"
    risk["jd_text"] = risk["jd_text"].replace("本科及以上学历，理解软件设计与工程验证流程。", education)
    next(item for item in risk["hard"].deductions if item.type == "education_unknown").jd_evidence = education
    selected_risk = next(slot for slot in _run(risk).slots if slot.category == "scenario_risk")
    _expect(find_original(risk["jd_text"], selected_risk.basis)
            and selected_risk.anchor["item"] == education and selected_risk.anchor["aligned"] == selected_risk.basis,
            "风险槽位拒绝跨行学历要求")


def case_25() -> None:
    """㉕ inferred 不传 jd_text 成功，tech_gap / scenario_design 引用画像且带推测 flag。"""
    arguments = load_inferred_inputs()
    profile = arguments["profile"]
    results = [_run(arguments) for _ in range(3)]
    _expect(results[0].model_dump() == results[1].model_dump() == results[2].model_dump(), "画像模式三次不一致")
    _expect(results[0].source == "inferred" and results[0].meta["jd_evidence_scope"] == "profile", "推测来源被伪装为 JD")
    for category, rows in (("tech_gap", profile.must_have), ("scenario_design", profile.responsibilities)):
        slot = next(slot for slot in results[0].slots if slot.category == category)
        _expect(slot.basis_source == "profile" and slot.flag == "依据来自岗位画像（推测），非真实 JD",
                f"{category} 未声明画像来源和推测标记")
        _expect(any(find_original(row, slot.basis) for row in rows), f"{category} 依据越过对应画像字段")
    gap = next(slot for slot in results[0].slots if slot.category == "tech_gap")
    _expect(bool(gap.anchor.get("profile_items")) and not gap.anchor.get("jd_lines"), "画像要求被标成真实 JD 行")
    # 未传 JD 时同文仍保留画像出处；一旦传入 JD，就必须切换整个 JD 家族。
    arguments["resume_text"] += "\n" + gap.basis
    same = next(slot for slot in _run(arguments).slots if slot.category == "tech_gap")
    _expect(same.basis_source == "profile" and same.flag == "依据来自岗位画像（推测），非真实 JD", "画像同文时来源丢失")
    arguments["jd_text"] = "\n".join([*profile.must_have, *profile.responsibilities,
                                       "熟悉知识库检索的交付风险并设计验证方案。"])
    with_jd = _run(arguments)
    _expect(all(slot.basis_source == "jd" for slot in with_jd.slots
                if slot.category in ("tech_gap", "scenario_design", "scenario_risk")), "传 JD 后仍从画像取证")
    for empty in (None, "", " \t\r\n"):
        arguments = load_inferred_inputs()
        arguments["jd_text"] = empty
        _run(arguments)
    # 推测模式也须硬拒绝伪造依据，不能因为允许画像就放宽原文红线。
    take = module._take_basis

    def corrupt(*args: object, **kwargs: object) -> tuple[str, str, bool]:
        result = take(*args, **kwargs)
        return ("不存在于画像、简历或JD的虚构技术要求", "", False) if kwargs["category"] == "tech_gap" else result

    with patch.object(module, "_take_basis", side_effect=corrupt):
        _expect_value_error(load_inferred_inputs(), "tech_gap 的依据不在原文里")


def case_26() -> None:
    """㉖ ★ 传 jd_text 时 profile 来源为零，画像 source 只影响结果标注。"""
    for loader in (load_go_beike_inputs, load_beike_inputs):
        baseline = _run(loader())
        for label in ("inferred", "user_jd"):
            arguments = loader()
            arguments["profile"] = infer_profile("AI 业务落地专家")
            arguments["profile"].source = label
            results = [_run(arguments) for _ in range(3)]
            _expect(results[0].model_dump() == results[1].model_dump() == results[2].model_dump(), "JD 家族三次不一致")
            result = results[0]
            _expect(result.source == label and result.meta["jd_evidence_scope"] == "jd_text", "来源标注与取证家族混淆")
            _expect(result.meta["basis_sources"]["profile"] == 0
                    and all(slot.basis_source != "profile" for slot in result.slots), "有 JD 却使用画像来源")
            _expect(result.slots == baseline.slots, "无关画像改变了真实 JD 的依据、技能或锚点")
            _expect(all(slot.basis_source == "jd" for slot in result.slots
                        if slot.category in ("tech_gap", "scenario_design", "scenario_risk")), "三个要求槽位没有全部走 JD")
            if loader is load_beike_inputs:
                _expect(len(result.slots) == 16 and result.meta["basis_sources"] == {"resume": 13, "jd": 3, "profile": 0},
                        "贝壳真产物来源分布回归")


def case_27() -> None:
    """㉗ ★ tech_gap 绑定 missing_skill 的知识库，JD 原行非空且不能借画像补票。"""
    for item in ("必备技能：知识库", "必备技能:知识库", " 必备技能： 知识库 ", "知识库"):
        arguments = load_beike_profile_inputs()
        missing = next(row for row in arguments["hard"].deductions if row.type == "missing_skill")
        missing.item = item
        parsed = re.sub(r"^必备技能\s*[:：]\s*", "", item.strip()).strip()
        result = _run(arguments)
        gap = next(slot for slot in result.slots if slot.category == "tech_gap")
        _expect(gap.anchor["skill"] == parsed == "知识库" and "知识库" in gap.basis
                and gap.basis_source == "jd" and len(gap.anchor.get("jd_lines", [])) >= 1,
                "缺口技能、依据或 JD 来源与扣分不一致")
        _expect(all(find_original(arguments["jd_text"], line) and "知识库" in line for line in gap.anchor["jd_lines"]),
                "技能原行丢失或不含缺失技能")
    # JD 原文确实没有知识库时，即使画像里有同名技能，也必须硬拒绝。
    arguments = load_beike_profile_inputs()
    arguments["profile"].must_have = ["知识库", "工作流"]
    arguments["jd_text"] = "\n".join(line for line in arguments["jd_text"].splitlines() if "知识库" not in line)
    _expect_value_error(arguments, "JD 中确实没有该技能原句：知识库")
    # 没有 missing_skill 的旧真 DOCX × Go JD 不能再虚造一个 tech_gap。
    _expect_value_error(load_real_inputs(), "tech_gap 必须有 hard.deductions 的 missing_skill")
    arguments = load_beike_inputs()
    next(row for row in arguments["hard"].deductions if row.type == "missing_skill").item = "必备技能： "
    _expect_value_error(arguments, "missing_skill 缺少技能名")


def case_28() -> None:
    """㉘ ★ 无 JD 的画像依据逐条回查，知识库取条目本身，混合模板及跨条拼句硬拒绝。"""
    for label in ("inferred", "user_jd"):
        for empty in (None, "", " \t\r\n"):
            arguments = load_inferred_inputs()
            arguments["profile"].source = label
            arguments["jd_text"] = empty
            result = _run(arguments)
            _expect(result.source == label and result.meta["jd_evidence_scope"] == "profile", "无 JD 判定依赖了画像 source")
            profile = arguments["profile"]
            rows = [*profile.must_have, *profile.responsibilities]
            profile_slots = [slot for slot in result.slots if slot.basis_source == "profile"]
            _expect({slot.category for slot in profile_slots} == {"tech_gap", "scenario_design", "scenario_risk"},
                    "无 JD 时三个要求槽位没有全部取自画像")
            _expect(all(any(find_original(row, slot.basis) for row in rows) for slot in profile_slots), "画像依据不属于任何单条条目")
            gap = next(slot for slot in profile_slots if slot.category == "tech_gap")
            _expect(gap.anchor["skill"] == "知识库" and find_original("知识库", gap.basis)
                    and gap.flag == "依据来自岗位画像（推测），非真实 JD", "缺口被替换成通用句或丢失标记")
    # 混合画像不适用整份通用画像的降级例外，不能借简历或旧 JD 的原句。
    arguments = load_inferred_inputs()
    arguments["profile"].must_have = list(infer_profile("火星矿业队长").must_have)
    _expect_value_error(arguments, "岗位画像 must_have 中确实没有该缺口技能条目：知识库")
    arguments = load_inferred_inputs()
    arguments["profile"].responsibilities = list(infer_profile("火星矿业队长").responsibilities)
    _expect_value_error(arguments, "岗位画像没有具体职责条目，不能使用通用模板句")
    take = module._take_basis

    def join_items(*args: object, **kwargs: object) -> tuple[str, str, bool]:
        selected = take(*args, **kwargs)
        return ("Agent 知识库", "", False) if kwargs["category"] == "tech_gap" else selected

    with patch.object(module, "_take_basis", side_effect=join_items):
        _expect_value_error(load_inferred_inputs(), "tech_gap 的依据不在原文里")
    arguments = load_inferred_inputs()
    generic = infer_profile("火星矿业队长").must_have[0]
    arguments["profile"].must_have.append(generic)

    def generic_risk(*args: object, **kwargs: object) -> tuple[str, str, bool]:
        selected = take(*args, **kwargs)
        return (generic, "", False) if kwargs["category"] == "scenario_risk" else selected

    # 即使通用句属于画像条目，末尾硬校验也必须拒绝，不能只依靠选句分支过滤。
    with patch.object(module, "_take_basis", side_effect=generic_risk):
        _expect_value_error(arguments, "scenario_risk 的画像依据不能使用通用模板句")


def case_29() -> None:
    """㉙ ★ 不传 JD 的通用画像 build 成功，场景 flag 含通用兜底，全部依据可回查。"""
    generic = infer_profile("火星矿业队长")
    for loader in (load_beike_inputs, load_go_beike_inputs):
        for empty in (None, "", " \t\r\n"):
            arguments = load_title_only_inputs("AI 业务落地专家", loader, generic)
            arguments["jd_text"] = empty
            results = [_run(arguments) for _ in range(3)]
            result = results[0]
            _expect(result.model_dump() == results[1].model_dump() == results[2].model_dump(), "通用画像三次不一致")
            _expect(len(result.slots) == 16 and result.meta["part_counts"] == {"hr": 6, "tech": 6, "scenario": 2, "project": 2},
                    "通用画像丢失槽位或分区配额")
            design = next(slot for slot in result.slots if slot.category == "scenario_design")
            _expect(design.basis == generic.must_have[0] and design.basis_source == "profile"
                    and design.flag == "岗位画像为通用兜底：建议补充真实 JD 后重新生成", "通用画像未按指定依据和标记降级")
            _expect("画像低置信，题目针对性弱" in result.warnings, "通用画像没有低置信警告")
    # 兼容旧调用方仍传入具体缺口的情况；不把缺口伪装成通用画像已证实的技能。
    arguments = load_beike_inputs()
    arguments.pop("jd_text")
    arguments["profile"] = generic
    _run(arguments)
    # 没有缺口扣分也应保留通用基础能力槽位；真正空简历仍拒绝构建。
    arguments = load_title_only_inputs("AI 业务落地专家", profile=generic)
    arguments["hard"].deductions = [row for row in arguments["hard"].deductions if row.type != "missing_skill"]
    _run(arguments)
    arguments["resume_text"] = " \t\n"
    _expect_value_error(arguments, "无可追溯")
    # 末尾硬校验必须仍拒绝不属于任何一条画像的伪造依据。
    take = module._take_basis

    def corrupt(*args: object, **kwargs: object) -> tuple[str, str, bool]:
        selected = take(*args, **kwargs)
        return ("不存在于任何画像条目的虚构依据", "", False) if kwargs["category"] == "scenario_design" else selected

    with patch.object(module, "_take_basis", side_effect=corrupt):
        _expect_value_error(load_title_only_inputs("AI 业务落地专家", profile=generic), "scenario_design 的依据不在原文里")


def case_30() -> None:
    """㉚ ★ scenario_design 引用 JD 原行，anchor 保留 item / aligned / similarity。"""
    for loader in (load_beike_inputs, load_go_beike_inputs, load_complete_inputs):
        arguments = loader()
        design = next(slot for slot in _run(arguments).slots if slot.category == "scenario_design")
        item = next(row.item for row in arguments["sem"].per_item if not row.covered)
        similarity = SequenceMatcher(None, normalize_text(item).casefold(),
                                     normalize_text(design.basis).casefold(), autojunk=False).ratio()
        _expect(design.basis in arguments["jd_text"].splitlines() and design.basis_source == "jd", "场景设计未引用 JD 原行")
        _expect(design.anchor["item"] == item and design.anchor["aligned"] == design.basis
                and design.anchor["similarity"] == similarity >= 0.55, "场景对齐审计字段或相似度错误")
    # 低于阈值但有实质共同内容时允许近似对齐，并明确告知原文措辞不同。
    arguments = load_complete_inputs()
    approximate = "订单接口验收，以及需求澄清、测试计划、发布排期、灰度回滚与多端调用协议管理的协同安排"
    arguments["sem"].per_item[0].item = approximate
    design = next(slot for slot in _run(arguments).slots if slot.category == "scenario_design")
    _expect(0 < design.anchor["similarity"] < 0.55 and design.flag == "依据为近似对齐，原文表述不同"
            and find_original(arguments["jd_text"], design.basis), "近似对齐未标记或没有引用原文")
    # 完全无关时必须明确降级通用题，不能拿最高的低分无关行伪装成对齐。
    arguments = load_complete_inputs()
    arguments["sem"].per_item[0].item = "ZXQVV"
    design = next(slot for slot in _run(arguments).slots if slot.category == "scenario_design")
    _expect(design.anchor.get("generic") is True and design.anchor["aligned"] == ""
            and design.anchor["similarity"] == 0 and design.flag == "无可追溯依据，需人工补充"
            and design.basis == arguments["jd_text"].strip(), "无相关原行时没有进入通用场景降级")


def case_31() -> None:
    """㉛ ★ scenario_risk 依据在 JD 原文可查，改写的风险要求也须对齐回原行。"""
    for loader in (load_beike_inputs, load_go_beike_inputs, load_complete_inputs):
        arguments = loader()
        risk = next(slot for slot in _run(arguments).slots if slot.category == "scenario_risk")
        _expect(find_original(arguments["jd_text"], risk.basis) and risk.basis_source == "jd", "场景风险没有引用 JD 原文")
        _expect(risk.anchor["aligned"] == risk.basis and 0 <= risk.anchor["similarity"] <= 1, "风险对齐审计信息缺失")
    arguments = load_complete_inputs()
    rewritten = "本科毕业，理解软件工程与验证流程"
    next(row for row in arguments["hard"].deductions if row.type == "education_unknown").jd_evidence = rewritten
    risk = next(slot for slot in _run(arguments).slots if slot.category == "scenario_risk")
    _expect(not find_original(arguments["jd_text"], rewritten) and find_original(arguments["jd_text"], risk.basis)
            and risk.anchor["item"] == rewritten and risk.anchor["aligned"] == risk.basis, "风险沿用了改写串或丢失上游条目")


def case_32() -> None:
    """㉜ ★ 上游七条职责压缩改写后逐条 build 成功，16 条可追溯、唯一、三次全等。"""
    rewritten = [
        "深入一线业务识别值得AI解决的问题",
        "梳理业务流程、关键判断和专业经验并转化为AI场景方案",
        "使用大模型、Agent、知识库、工作流及相关AI工具完成原型搭建和效果验证",
        "推动方案从演示走向实际使用并解决使用习惯、使用环境和体验问题",
        "建立评测和反馈机制并通过真实业务结果判断方案有效性",
        "将验证有效的场景沉淀为可复制的skill、工具、方法和推广手册",
        "与产品、技术、业务和管理团队协同推动AI能力复制",
    ]
    for loader in (load_beike_inputs, load_go_beike_inputs):
        for selected, item in enumerate(rewritten):
            arguments = loader()
            arguments["job"].responsibilities = list(rewritten)
            arguments["sem"] = _semantic(arguments["job"])
            for index, row in enumerate(arguments["sem"].per_item):
                row.covered = index != selected
            _expect(not find_original(arguments["jd_text"], item), "压缩反例意外仍与原文逐字一致")
            results = [_run(arguments) for _ in range(3)]
            result = results[0]
            _expect(result.model_dump() == results[1].model_dump() == results[2].model_dump(), "改写职责三次构建不一致")
            _expect(len(result.slots) == 16 and result.meta["basis_sources"]["profile"] == 0
                    and result.meta["part_counts"] == {"hr": 6, "tech": 6, "scenario": 2, "project": 2}, "改写职责导致配额或来源回归")
            design = next(slot for slot in result.slots if slot.category == "scenario_design")
            _expect(design.basis in arguments["jd_text"].splitlines() and design.anchor["item"] == item
                    and design.anchor["aligned"] == design.basis and not design.anchor.get("generic"), "改写职责未对齐到相关 JD 原行")


def case_33() -> None:
    """㉝ ★ growth 优先成长句；真简历精确选中 DeepSeek / 聘才猫落地 AI 原句。"""
    for loader in (load_beike_inputs, load_complete_inputs):
        arguments = loader()
        result = _run(arguments)
        growth = next(slot for slot in result.slots if slot.category == "growth")
        _expect(any(word in growth.basis for word in ("最近", "学", "落地", "应用", "上手", "尝试")), "成长槽位仍为无成长动作的技能清单")
        _expect("degraded" not in growth.anchor and find_original(arguments["resume_text"], growth.basis), "有成长句却降级或引用失真")
        if loader is load_beike_inputs:
            expected = next(row.text for row in module._sentences(arguments["resume_text"])
                            if row.text.startswith("4. 基于 DeepSeek / 聘才猫落地 AI"))
            _expect(growth.basis == expected, "真简历 growth 没有选中落地 AI 原句")


def case_34() -> None:
    """㉞ ★ debug 优先排障或技术动作句，风险依据按 hard_gate / missing_skill 留痕。"""
    for loader in (load_beike_inputs, load_complete_inputs):
        arguments = loader()
        result = _run(arguments)
        debug = next(slot for slot in result.slots if slot.category == "debug")
        _expect(bool(re.search(r"排查|故障|超时|监控|线上|报警|定位|慢|异步回调|定时任务|分库|重试|幂等|压测", debug.basis)),
                "排障槽位仍为纯业务范围句")
        _expect("degraded" not in debug.anchor, "有排障或技术动作句却降级")
        risk = next(slot for slot in result.slots if slot.category == "scenario_risk")
        expected_priority = "missing_skill" if loader is load_beike_inputs else "hard_gate"
        _expect(risk.anchor["risk_priority"] == expected_priority, "风险优先级与最终依据身份不一致")
        if loader is load_beike_inputs:
            expected = next(row.text for row in module._sentences(arguments["resume_text"])
                            if "微信支付定时任务拉取，招行子账户异步回调" in row.text)
            _expect(debug.basis == expected, "真简历 debug 没有选中资金与合同链路技术动作句")
    # 技能降级行即使同时提到学历，也不能把实际的技能池选择伪记为门槛池选择。
    arguments = load_complete_inputs()
    arguments["hard"].deductions = [row for row in arguments["hard"].deductions if row.type == "missing_skill"]
    arguments["jd_text"] += "\n本科及以上，熟悉 Milvus 的故障排查与风险验证。"
    risk = next(slot for slot in _run(arguments).slots if slot.category == "scenario_risk")
    _expect(risk.anchor["risk_priority"] == "missing_skill", "按关键词而非实际取证池记录了风险优先级")


def case_35() -> None:
    """㉟ ★ 实现→成长→排障→深度→取舍先占先得，句子独占，池内类型耗尽才标降级。"""
    arguments = load_beike_inputs()
    take = module._take_basis
    with patch.object(module, "_take_basis", wraps=take) as selected:
        result = _run(arguments)
    categories = {"impl_detail", "growth", "debug", "tech_depth", "tradeoff"}
    calls = [call.kwargs["category"] for call in selected.call_args_list if call.kwargs["category"] in categories]
    _expect(calls == ["impl_detail", "growth", "debug", "tech_depth", "tradeoff"], "技术槽位实际分配次序错误")
    slots = {slot.category: slot for slot in result.slots}
    _expect("独立设计并实现" in slots["impl_detail"].basis and slots["impl_detail"].basis != slots["growth"].basis,
            "实现槽位没有独立依据或复制了成长句")
    _expect(len({normalize_text(slots[category].basis).casefold() for category in categories}) == 5, "五个技术槽位依据不独占")
    # 同时满足实现和成长的句子先归实现；成长须找另一条，不能复制或伪装成不同排版。
    arguments = load_complete_inputs()
    shared = "独立设计并实现 Go 检索服务，最近学习 LangChain 并应用到接口验证。"
    arguments["resume_text"] = arguments["resume_text"].replace("使用 Go 实现并发请求处理和连接复用。", shared)
    result = _run(arguments)
    slots = {slot.category: slot for slot in result.slots}
    _expect(slots["impl_detail"].basis == shared and slots["growth"].basis != shared
            and "学习验证" in slots["growth"].basis, "冲突句没有先占先得或成长未另选类型句")
    # 真正删除成长类型句后，仅能降到本池未占用原句，并明确记录原因。
    arguments = load_complete_inputs()
    arguments["resume_text"] = arguments["resume_text"].replace(
        "使用 LangChain 做学习验证并记录应用边界。", "说明 LangChain 工具的适用边界并记录核对结果。")
    with patch.object(module, "_take_basis", wraps=take) as selected:
        result = _run(arguments)
    growth = next(slot for slot in result.slots if slot.category == "growth")
    call = next(call for call in selected.call_args_list if call.kwargs["category"] == "growth")
    _expect(growth.anchor.get("degraded") == "no_typed_candidate"
            and any(text == growth.basis and origin == "resume" for text, origin in call.args[0]),
            "类型句耗尽时未记降级或越过了本池")


def case_36() -> None:
    """㊱ ★ 项目优先开发类；受时间约束选中测试项目时明确标记，纯测试第二条也带 flag。"""
    for loader in (load_beike_inputs, load_go_beike_inputs):
        result = _run(loader())
        projects = [slot for slot in result.slots if slot.part == "project"]
        _expect(len(projects) == 2 and bool(re.search(r"开发|设计|实现|主导|搭建|上线", projects[0].basis)), "第一条项目不是开发类")
        _expect(bool(re.search(r"开发|设计|实现|主导|搭建|上线", projects[1].basis))
                or "测试类" in (projects[1].flag or ""), "第二条非开发项目缺少测试类标记")
    # 有两个合法开发段时，无论 seed 如何都不能让测试段抢占它们。
    text = """订单系统项目 2018.01-2018.06：主导开发并上线订单接口。
检索平台项目 2020.01-2020.06：设计并实现内容检索服务。
测试系统项目 2022.01-2022.06：参与系统测试并记录验证结果。
"""
    for seed in range(16):
        projects = pick_projects(extract_projects(text), str(seed))
        _expect(len(projects) == 2 and all("测试" not in project.basis for project in projects), "合法开发项目被测试段抢占")
    arguments = load_complete_inputs()
    arguments["resume_text"] = arguments["resume_text"].split("项目经历", 1)[0] + "项目经历\n" + """测试平台项目 2018.01-2018.06：参与系统测试并记录验证结果。
测试系统项目 2020.01-2020.06：执行接口测试并核对验证结果。
"""
    projects = [slot for slot in _run(arguments).slots if slot.part == "project"]
    _expect(len(projects) == 2 and "测试类" in (projects[1].flag or ""), "仅有测试项目时第二条未标记")


def check_input_combinations() -> None:
    """逐份简历重放四种输入；无岗位信息时确认入口护栏拒绝，尚无 N4 HTTP 路由。"""
    for label, loader in (("真简历", load_beike_inputs), ("测试简历", load_go_beike_inputs)):
        baseline = loader()
        print(f"四种输入组合：{label}")
        for name, title, jd in (("name+jd", "AI 业务落地专家", baseline["jd_text"]),
                                ("only name", "AI 业务落地专家", None),
                                ("only jd", None, baseline["jd_text"]), ("都没有", None, None)):
            mode, error = resolve_job_input(title, jd)
            if error is not None:
                _expect(name == "都没有" and not mode and error == "请至少填写岗位名称，或粘贴岗位 JD", "岗位输入护栏返回异常结果")
                print(f"{name} | 输入护栏拒绝（供接口返回 422，未调用 build） | {error}")
                continue
            arguments = load_title_only_inputs(title, loader) if mode == "inferred" else loader()
            if mode == "user_jd":
                # 仅 JD 时使用原 JD 已解析出的岗位名，不能重新推测画像或伪造输入名称。
                arguments.update(profile=None, job_title=title or arguments["job"].title or "")
            result = _run(arguments)
            design = next(slot for slot in result.slots if slot.category == "scenario_design")
            print(f"{name} | build 成功 | source={result.source} | 槽位={len(result.slots)} | 结构={result.meta['part_counts']}"
                  f" | scenario_design.basis_source={design.basis_source} | basis={design.basis!r} | flag={design.flag!r}")
        # delivery 已能精确命中；额外输出未知岗位名的低置信结果，证明没有一律 delivery。
        arguments = load_title_only_inputs("火星矿业队长", loader)
        result = _run(arguments)
        design = next(slot for slot in result.slots if slot.category == "scenario_design")
        print(f"only name（未知岗位） | build 成功 | 槽位={len(result.slots)} | basis={design.basis!r} | flag={design.flag!r}")


def main() -> int:
    """执行全部验收，输出两份真实产物明细和最慢单次构建耗时。"""
    cases: tuple[Callable[[], None], ...] = (
        case_01, case_02, case_03, case_04, case_05, case_06,
        case_07, case_08, case_09, case_10, case_11, case_12, case_13, case_14, case_15,
        case_16, case_17, case_18, case_19, case_20, case_21, case_22, case_23, case_24, case_25,
        case_26, case_27, case_28, case_29, case_30, case_31, case_32,
        case_33, case_34, case_35, case_36,
    )
    passed = 0
    messages = [""] * len(cases)
    # 性能组最后执行，覆盖新增真产物和负例的全部构建；仍按编号打印，不能中途验收后漏计慢调用。
    run_order = [index for index, case in enumerate(cases) if case is not case_11] + [cases.index(case_11)]
    # 计数按完整用例组计算；组内任一子断言失败，该组就不能算通过。
    for position in run_order:
        index, case = position + 1, cases[position]
        try:
            case()
        except Exception as error:
            # 某组失败仍运行后续组，最终退出码不得伪装成成功。
            messages[position] = f"{index:02d} 失败：{case.__doc__} {type(error).__name__}: {error}"
        else:
            passed += 1
            messages[position] = f"{index:02d} 通过：{case.__doc__}"
    for message in messages:
        print(message)
    # 明细是本次真实构建的直接输出，保留 repr 中的空格与制表符以便逐字复核。
    for path, loader in ((ROOT / "tools/test_files/go_backend_resume.docx", load_go_beike_inputs),
                         (BEIKE_RESUME, load_beike_profile_inputs)):
        print(f"真简历 basis 明细：{path}")
        try:
            result = _run(loader())
        except Exception as error:
            print(f"产物失败：{type(error).__name__}: {error}")
            return 1
        for index, slot in enumerate(result.slots, 1):
            degraded = f" | degraded={slot.anchor['degraded']!r}" if "degraded" in slot.anchor else ""
            print(f"{index:02d} {slot.category} | basis_source={slot.basis_source} | basis={slot.basis!r} | flag={slot.flag!r}{degraded}")
        print(f"结构={result.meta['part_counts']}；来源={result.meta['basis_sources']}；悬空=0；重复=0；说明文字=0")
    print("真简历 × 贝壳 JD：五个技术槽位 basis 全文：")
    result = _run(load_beike_inputs())
    selected = {slot.category: slot for slot in result.slots}
    for category in ("growth", "debug", "impl_detail", "tech_depth", "tradeoff"):
        print(f"{category}: {selected[category].basis}")
    print(f"scenario_risk.risk_priority={selected['scenario_risk'].anchor['risk_priority']}")
    print("inferred 模式 tech_gap 明细（未传 jd_text）：")
    print("画像输入：调用方提供的技能型条目（离线验收），不将 infer_profile 通用模板冒充具体技能")
    try:
        inferred = _run(load_inferred_inputs())
    except Exception as error:
        print(f"产物失败：{type(error).__name__}: {error}")
        return 1
    gap = next(slot for slot in inferred.slots if slot.category == "tech_gap")
    print(f"source={inferred.source} | basis_source={gap.basis_source} | skill={gap.anchor['skill']!r} | flag={gap.flag!r} | basis={gap.basis!r}")
    print("真简历 × 贝壳 JD：上游职责压缩后的 scenario_design 完整 anchor：")
    arguments = load_beike_inputs()
    arguments["job"].responsibilities[0] = "深入一线业务识别值得AI解决的问题"
    arguments["sem"] = _semantic(arguments["job"])
    design = next(slot for slot in _run(arguments).slots if slot.category == "scenario_design")
    print(json.dumps(design.anchor, ensure_ascii=False, indent=2))
    print(f"basis={design.basis!r} | basis_source={design.basis_source} | flag={design.flag!r}")
    try:
        check_input_combinations()
    except Exception as error:
        print(f"四种输入组合失败：{type(error).__name__}: {error}")
        return 1
    # 所有明细及四组合也计入性能，不能在它们执行前就宣告验收成功。
    duration_ms = max(MEASUREMENTS, default=0.0)
    print("计时口径：最慢单次 build_skeleton，不含文件读取与输入准备")
    print(f"通过 {passed}/{len(cases)}；耗时 {duration_ms:.2f}ms；模型调用 0；成本 0 元")
    if duration_ms >= 100:
        print(f"性能失败：最慢单次 build {duration_ms:.2f}ms 不小于 100ms")
    return 0 if passed == len(cases) and duration_ms < 100 else 1


if __name__ == "__main__":
    raise SystemExit(main())
