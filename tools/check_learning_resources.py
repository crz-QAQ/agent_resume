"""离线执行 M8a 与 D-M8-02～07 回归，包含真实向量、共享词闸门和 PDF 提示。"""

from __future__ import annotations

import ast
from contextlib import ExitStack
from copy import deepcopy
from functools import lru_cache
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import perf_counter
import tokenize
from types import SimpleNamespace
from unittest.mock import patch
import warnings

# 先确定项目根目录，所有测试均可从其他目录启动。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.dont_write_bytecode = True

from app import learning_resources as module
from app.learning_resources import Material, build_learning_plan, load_materials, match_materials
from app.resume_diagnose import diagnose
from app.schemas_match import Deduction, JobInfo
from app.schemas_revision import DiagnosisReport
from app.scoring_rules import HardScore, score_hard
from tools.check_materials import probe_link, validate_quality_policy

# 真模型样本只保存合成缺口与匹配数值，不含个人简历或模型响应。
REAL_SAMPLES: dict = {}


def _expect(condition: bool, message: str) -> None:
    """断言独立期望，失败时携带具体原因。"""
    if not condition:
        raise AssertionError(message)


def _material(**changes) -> Material:
    """复制静态测试材料，可分别调整排序键和标签，不联网或改写种子库。"""
    # 合法基线来自官方白名单，反例只修改被检查的目标字段。
    values = dict(id="test-go", title="测试用 Go 材料", kind="external", locator="https://go.dev/doc/",
                  tags=["Go"], level="入门", quality=3, verified_at="2026-11-13", note="仅用于验收")
    return Material(**{**values, **changes})


def _diagnosis(terms: list[str]) -> DiagnosisReport:
    """构造只含指定上游缺词的报告，不在 M8 计算中抽取新词。"""
    # 每次复制词列表，后续快照能捕获意外原地修改。
    return DiagnosisReport(findings=[], keyword_coverage=[], missing_keywords=list(terms), completeness={}, meta={})


def _deduction(evidence: str, severity: str = "high", kind: str = "missing_skill") -> Deduction:
    """构造带明确 JD 原文的上游扣分记录。"""
    # 复用既有 Deduction 契约，不为学习材料新造扣分模型。
    return Deduction(item=f"必备技能：{evidence}", type=kind, deduct=5,
                     jd_evidence=evidence, resume_evidence="未体现", severity=severity)


def _inputs(terms: list[str] | None = None) -> dict:
    """返回全新输入，语义对象只含本块实际消费的既有字段。"""
    # SimpleNamespace 避免为离线验收而导入带本地模型依赖的评分模块。
    return dict(diagnosis=_diagnosis(["Go"] if terms is None else terms),
                hard=None, sem=None, job_title="后端工程师")


def _check_library() -> str:
    """① 校验种子库、非法字段、白名单绕过、重复标签警告与离线状态。"""
    materials, version = load_materials()  # 验收真实库，而非仅验收测试替身。
    _expect(18 <= len(materials) <= 21 and version == "1", "材料数量或版本错误")
    _expect(all(row.kind == "external" for row in materials), "种子来源不符")
    payload = json.loads(module.MATERIALS_PATH.read_text(encoding="utf-8"))  # 反例基于合法载荷复制。
    _expect(all(1 <= row.quality <= 5 for row in materials), "存在 0 分或越界材料")  # 真实库不得留占位分。
    _expect(isinstance(payload["quality_rubric"], str) and payload["quality_rubric"].strip(), "缺少评分口径")
    # URL 反例分别覆盖协议、域名后缀、子域、用户信息、端口和控制字符。
    bad_rows = [dict(id="BAD_id"), dict(title="x"), dict(title="  "), dict(note=""),
                dict(note="x" * 61), dict(kind="web"), dict(tags=[]), dict(tags=[""]),
                dict(tags=["Go"] * 7), dict(level="专家"), dict(quality=-1), dict(quality=0), dict(quality=6),
                dict(quality=True), dict(quality=3.0), dict(verified_at="2026-02-30"),
                dict(verified_at="2026-1-03"), dict(locator="http://go.dev/doc/"),
                dict(locator="https://go.dev.evil.example/doc/"), dict(locator="https://evil.go.dev/"),
                dict(locator="https://go.dev@evil.example/"), dict(locator="https://user@go.dev/doc/"),
                dict(locator="https://go.dev:444/doc/"), dict(locator="https://go.dev/\nx"),
                dict(kind="local", locator="docs/教材/../secret.md"),
                dict(kind="local", locator="docs/教材/m8a-nonexistent.md"), dict(unknown="额外字段")]
    invalid_count = 0  # 只有真实捕获 ValueError 才计入拒绝数。
    # 临时目录由系统分配；测试只读写自己新建的材料库文件。
    with TemporaryDirectory(prefix="m8a-materials-") as directory:
        path = Path(directory) / "materials.json"
        invalid_payloads = []  # 每例独立，避免前一处非法字段掩盖后一处问题。
        for changes in bad_rows:
            candidate = deepcopy(payload)
            candidate["materials"][0].update(changes)
            invalid_payloads.append(candidate)
        duplicate = deepcopy(payload)  # 单项字段全合法，专门检验跨条目的 id 唯一性。
        duplicate["materials"].append(deepcopy(duplicate["materials"][0]))
        # 元数据错误与材料行错误分开覆盖，不允许版本或更新日期被忽略。
        invalid_payloads.extend([duplicate, [], {**payload, "version": 1}, {**payload, "updated_at": "bad"},
                                 {**payload, "materials": {}}, {**payload, "updated_at": "2026-11-12"}])
        # 评分口径既要存在，也必须是非空文字，不能被默认值悄悄补齐。
        invalid_payloads.append({key: value for key, value in payload.items() if key != "quality_rubric"})
        invalid_payloads.extend({**payload, "quality_rubric": value} for value in ("", "  ", None, 0, True))
        for candidate in invalid_payloads:
            path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
            try:
                load_materials(path)
            except ValueError:
                invalid_count += 1
            else:
                raise AssertionError(f"非法材料库被接受：{candidate!r}")
        # 实跑 CLI，确认 0 分和缺口径确实导致退出码 1，且在联网前失败。
        zero = deepcopy(payload)  # 其他字段全部合法，确保失败确实来自 0 分。
        zero["materials"][0]["quality"] = 0
        absent = {key: value for key, value in payload.items() if key != "quality_rubric"}  # 不补默认口径。
        for candidate, expected in ((zero, "0 视为未评分"), (absent, "quality_rubric"),
                                    ({**payload, "quality_rubric": "  "}, "quality_rubric")):
            path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
            completed = subprocess.run([sys.executable, "-B", "tools/check_materials.py", "--path", str(path)],
                                       cwd=PROJECT_ROOT, capture_output=True, encoding="utf-8", timeout=10)
            _expect(completed.returncode == 1 and expected in completed.stdout
                    and "| HTTP" not in completed.stdout, "CLI 放过未评分/缺口径，或提前执行了联网")
        # 坏 JSON 和缺失文件同样统一为公开约定的 ValueError。
        path.write_text("{", encoding="utf-8")
        for invalid_path in (path, Path(directory) / "absent.json"):
            try:
                load_materials(invalid_path)
            except ValueError:
                invalid_count += 1
            else:
                raise AssertionError("坏 JSON 或缺失文件未报 ValueError")
        repeated = deepcopy(payload)  # 不同 id 配相同 tags，应该警告而非拒绝加载。
        repeated["materials"] = [{**payload["materials"][0], "id": f"duplicate-tags-{index}"} for index in range(3)]
        path.write_text(json.dumps(repeated, ensure_ascii=False), encoding="utf-8")
        with warnings.catch_warnings(record=True) as notices:
            warnings.simplefilter("always")
            load_materials(path)
        _expect(len(notices) == 1 and "tags" in str(notices[0].message), "重复标签不警告")
    # 即使宿主机可以上网，--offline 也必须在调用 curl 之前返回。
    with patch("tools.check_materials.subprocess.run", side_effect=AssertionError("离线不应联网")):
        offline = probe_link(materials[0], offline=True)
    _expect(offline["status"] == "unverified" and offline["status_code"] is None, "离线冒充已核实")
    # 实测失败与非 200 响应不能被伪装成 verified，白名单外重定向不能被访问。
    with patch("tools.check_materials.shutil.which", return_value="curl"):
        # 此处只测状态转换；真实 HTTP 结果由独立 check_materials 脚本取得。
        for code, expected in ((200, "verified"), (404, "failed"), (0, "unverified")):
            response = SimpleNamespace(stdout=json.dumps(dict(http_code=code)), returncode=0 if code else 7)
            with patch("tools.check_materials.subprocess.run", return_value=response):
                _expect(probe_link(materials[0])["status"] == expected, f"HTTP {code} 状态错误")
        # 重定向第二跳必须在访问前被拒绝，不能只检查最终状态码。
        redirect = SimpleNamespace(stdout=json.dumps(dict(http_code=302, redirect_url="https://evil.example/")), returncode=0)
        with patch("tools.check_materials.subprocess.run", return_value=redirect) as runner:
            _expect(probe_link(materials[0])["status"] == "unverified" and runner.call_count == 1,
                    "访问了白名单外重定向或伪报 verified")
    # 这里不引用历史实测的 200，避免离线验收被误读为本次联网核实。
    validate_quality_policy(module.MATERIALS_PATH)  # 已评分的真实材料库应通过新的硬断言。
    return f"{len(materials)} 条、字段/官方白名单合法；{invalid_count} 个非法反例拒绝；0 分/缺口径 CLI 硬失败；重复警告、HTTP/离线标注正确"


def _check_aliases() -> str:
    """② 现有别名表应使 Go 与 Golang 命中相同种子条目。"""
    materials, _ = load_materials()
    # 独立写出种子 id，防止两个空列表相等造成假通过。
    go = [row.id for row in match_materials("Go", materials)]
    golang = [row.id for row in match_materials("Golang", materials)]
    _expect(go == golang == ["go-official-doc"], "Go/Golang 别名未复用")
    # 另外覆盖既有 K8s/Postgres 别名，避免只为一个验收样例硬编码。
    _expect(match_materials("K8s", materials) == match_materials("Kubernetes", materials), "K8s 漏配")
    _expect(match_materials("Postgres", materials) == match_materials("PostgreSQL", materials), "Postgres 漏配")
    return "Go/Golang → go-official-doc；K8s、Postgres 别名一致"


def _check_families() -> str:
    """③ 使用既有族表命中上位标签，且不扩展跨族推断。"""
    # 合成标签仅用于验收同族规则，没有写入真实材料库。
    material = _material(tags=["LLM 产品"])
    _expect(match_materials("DeepSeek", [material]) == [material], "DeepSeek 未命中 LLM 产品")
    _expect(match_materials("Qwen", [material]) == [material], "同族成员漏配")
    _expect(match_materials("Redis", [material]) == [], "不同族凑数")  # 无关材料保持空态。
    return "DeepSeek/Qwen → LLM 产品；Redis 不误配"


def _check_unmatched() -> str:
    """④ 完全无关技能和长句保持空态，未启用兜底时不做模糊子串匹配。"""
    materials, _ = load_materials()
    _expect(match_materials("数控机床维修", materials) == [], "无关技能匹配到材料")
    _expect(match_materials("Django", materials) == [], "Django 因含 go 误配")  # 典型短词子串误配。
    _expect(match_materials("负责 Go 接口开发", materials) == [], "长句被偷偷做模糊匹配")
    plan = build_learning_plan(**_inputs(["数控机床维修"]), materials=materials)
    _expect(plan.unmatched == ["数控机床维修"] and plan.gaps[0].materials == [], "空态未进 unmatched")
    _expect(plan.meta["unmatched_reasons"] == {"数控机床维修": "材料库暂无对应条目"}, "未匹配原因缺失")
    empty = build_learning_plan(**_inputs(), materials=[])  # [] 与 None 的语义必须区分。
    _expect(empty.unmatched == ["Go"], "注入空库被默认库取代")
    return "无关技能进入 unmatched 并说明原因；Django/长句不误配；空库不凑数"


def _check_sources() -> str:
    """⑤ 用真实 M1 和硬评分产物，独立对照三个输入集合与 M3 已知事实。"""
    duties = ["负责订单系统接口开发", "维护服务监控与告警"]
    # 实际调用 M1 和硬评分，不手填其缺词或缺技能结论。
    job = JobInfo(title="后端工程师", company=None, required_years_min=3, required_years_max=None,
                  education="本科", must_have=["Go", "Redis"], nice_to_have=[], responsibilities=duties)
    resume = SimpleNamespace(name="测试", years=1, skills=["Go"])  # Go 已有，Redis 缺失。
    text = "姓名：测试\n工作年限：1 年\n学历：本科\n邮箱：test@example.com\n技能\nGo\n工作经历\n负责接口维护"
    diagnosis = diagnose(text, resume, job)
    hard = score_hard(resume, job, "本科")
    # best_sim 与 covered 故意矛盾，验证本块只消费上游 covered 而不重判。
    sem = SimpleNamespace(per_item=[SimpleNamespace(item=duties[0], covered=False, best_sim=0.99),
                                   SimpleNamespace(item=duties[1], covered=True, best_sim=0.01)],
                          uncovered=[duties[1]])
    before = deepcopy((diagnosis, hard, sem))  # 快照包括嵌套评分和诊断对象。
    plan = build_learning_plan(diagnosis=diagnosis, hard=hard, sem=sem, job_title=job.title, materials=[])
    # 期望直接对照 PRD 三个来源，不调用待测模块的 _collect_gaps。
    expected = set(diagnosis.missing_keywords) | {row.jd_evidence for row in hard.deductions if row.type == "missing_skill"}
    expected |= {row.item for row in sem.per_item if not row.covered}
    _expect({row.gap for row in plan.gaps} == expected and len(expected) <= 8, "缺口不与 M1/M3 同源")
    _expect(all(row.gap in row.why for row in plan.gaps), "why 丢失 JD 原文依据")
    _expect((diagnosis, hard, sem) == before, "修改了上游产物")
    _expect("维护服务监控与告警" not in expected, "使用了失真的 sem.uncovered")
    # 同一缺口多个来源去重；硬扣分严重度沿用，年限等不能成为学习材料缺口。
    custom = _inputs(["Go", "Go"])
    custom["hard"] = HardScore(0, {}, [_deduction("Go", "low"), _deduction("Redis", "low"),
                                      _deduction("年限", kind="years_short")])
    custom["sem"] = SimpleNamespace(per_item=[SimpleNamespace(item="Go", covered=False),
                                              SimpleNamespace(item="职责原文", covered=False)])
    result = build_learning_plan(**custom, materials=[])
    _expect([(row.gap, row.severity) for row in result.gaps] ==
            [("Go", "high"), ("职责原文", "medium"), ("Redis", "low")], "去重或严重度排序错误")
    return "真实 M1/硬评分产物的三源并集完全一致；只认 covered；去重、严重度、JD 依据正确"


def _check_limits() -> str:
    """⑥ 构造十二个均可匹配的缺口，核对三种额度与被截断的审计。"""
    terms = [f"验收技能{index}" for index in range(12)]  # 超过八项，必须发生截断。
    library = [_material(id=f"limit-{index}-{number}", tags=[term])
               for index, term in enumerate(terms) for number in range(3)]
    # 每个缺口都有三条材料，能区分真正执行上限与恰好没找到足够材料。
    plan = build_learning_plan(**_inputs(terms), materials=library)
    _expect(len(plan.gaps) == 8 and sum(len(row.materials) for row in plan.gaps) == 10, "8/10 额度错误")
    _expect(all(len(row.materials) <= 2 for row in plan.gaps), "每缺口超过两条")
    _expect(not plan.unmatched, "预算耗尽被冒充未匹配")  # 有候选但无额度不是相关性失败。
    _expect(plan.meta["truncated_gaps"] == 4 and plan.meta["omitted_by_material_limit"] == 6, "截断审计错误")
    return "12 缺口截为 8；材料共 10，每缺口 ≤2；预算截断不冒充 unmatched"


def _check_order() -> str:
    """⑦ 用打乱的候选逐级验证质量、级别、核实日期与字典序。"""
    # 较旧但高分的材料仍应先于较新的低分材料。
    library = [_material(id="low-new", quality=2), _material(id="advanced", quality=3, level="进阶"),
               _material(id="old", verified_at="2025-01-01"), _material(id="z-new"),
               _material(id="a-new"), _material(id="best-old", quality=5, level="专题", verified_at="2024-01-01")]
    expected = ["best-old", "a-new", "z-new", "old", "advanced", "low-new"]  # 每一层键均有竞争项。
    _expect([row.id for row in match_materials("Go", library)] == expected, "四级排序不正确")
    plan = build_learning_plan(**_inputs(), materials=list(reversed(library)))
    _expect([row.id for row in plan.gaps[0].materials] == expected[:2], "计划未按排序截取")
    return "quality 降序 → 入门优先 → verified_at 降序 → id 升序，乱序输入仍正确"


def _check_determinism() -> str:
    """⑧ 三次完整序列化相等；输入不变且输出不共享调用方可变对象。"""
    data = _inputs(["Go", "数控机床维修"])
    materials, _ = load_materials()
    before = deepcopy((data, materials))  # 比较内容，允许返回独立的新对象。
    plans = [build_learning_plan(**data, materials=materials) for _ in range(3)]
    _expect(plans[0].model_dump() == plans[1].model_dump() == plans[2].model_dump(), "三次结果不同")
    _expect((data, materials) == before, "输入被修改")
    # 输出还可能被 PDF 或 API 调用者修改，不能污染下一次推荐。
    plans[0].gaps[0].materials[0].tags.append("调用方修改")
    _expect((data, materials) == before and plans[1] == plans[2], "输出与输入或其他输出共享列表")
    # 默认库版本与注入库的来源必须区分，不能给任意测试库贴 version=1。
    _expect(build_learning_plan(**data).meta["materials_version"] == "1", "默认库版本丢失")
    _expect(plans[1].meta["materials_version"] == "provided", "注入库冒用默认版本")
    return "三次 model_dump 完全相同；输入未变、输出独立；版本来源可查"


def _check_semantic_fallback() -> None:
    """故障注入只验证模型未加载、坏向量及关闭路径；成功判据由后续真模型验证。"""
    data = _inputs(["负责订单系统接口开发"])
    materials = [_material(id=f"semantic-{index}", tags=["接口开发"]) for index in range(3)]
    # 故意让 RAG 不可用，验证不会为了兜底偷偷初始化或下载模型。
    with patch.dict(sys.modules, {"app.rag": None}):
        unavailable = build_learning_plan(**data, materials=materials, semantic_fallback=True)
    _expect(unavailable.meta["semantic_fallback"] == "unavailable" and unavailable.unmatched,
            "本地模型缺失被静默降级")
    _expect(unavailable.meta["semantic_errors"] and unavailable.meta["model_calls"] == 0, "缺失原因或调用数失真")
    # 错误输出替身不能作为新验收中的成功语义样本。
    model = SimpleNamespace(model_name="BAAI/bge-small-zh-v1.5", embed=lambda texts, **kwargs: [])
    rag = SimpleNamespace(EMBED_MODEL_NAME=model.model_name, _vector_model=model)
    with patch.dict(sys.modules, {"app.rag": rag}):
        # 一级命中时绝不能调用语义；关闭开关时也不调用语义。
        with patch.object(module, "_local_semantic_match", side_effect=AssertionError("不应调用兜底")):
            _expect(build_learning_plan(**_inputs(), materials=[_material()], semantic_fallback=True)
                    .meta["semantic_fallback"] == "not_needed", "一级非空仍尝试语义")
            build_learning_plan(**data, materials=materials)
        # 数量不足和非有限数都是模型不可用，不能作为空的成功匹配处理。
        for vectors in ([], [[float("nan")]] * 4):
            model.embed = lambda texts, values=vectors, **kwargs: values
            failed = build_learning_plan(**data, materials=materials, semantic_fallback=True)
            _expect(failed.meta["semantic_fallback"] == "unavailable" and failed.unmatched
                    and failed.meta["local_model_calls"] == {"embedding": 1, "reranker": 0}, "坏模型输出被接受或计数失真")


def _check_zero_model() -> str:
    """⑨ 源码扫描、独立进程封锁网络与模型导入、实际耗时及注释规范。"""
    source = (PROJECT_ROOT / "app/learning_resources.py").read_text(encoding="utf-8")
    _expect("openai" not in source.lower() and "await" not in source, "出现禁止的模型依赖或异步调用")
    # 新解释器避免前序导入缓存掩盖模型或网络依赖。
    code = '''import sys, socket, runpy
class DenyModels:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'openai', 'fastembed', 'onnxruntime'} or fullname in {'app.rag', 'app.scoring_semantic'}:
            raise AssertionError('unexpected model import: ' + fullname)
sys.meta_path.insert(0, DenyModels())
def denied(*args, **kwargs):
    raise AssertionError('unexpected network')
socket.socket.connect = denied
socket.create_connection = denied
scope = runpy.run_path('tools/check_learning_resources.py', run_name='isolated_check')
data = scope['_inputs'](['Go', '数控机床维修'])
plan = scope['build_learning_plan'](**data)
assert plan.meta['cost_yuan'] == 0 and plan.meta['model_calls'] == 0
assert plan.unmatched == ['数控机床维修']
print('isolated execution passed')
'''
    completed = subprocess.run([sys.executable, "-B", "-c", code], cwd=PROJECT_ROOT,
                               capture_output=True, encoding="utf-8", errors="replace", timeout=30)
    _expect(completed.returncode == 0 and "execution passed" in completed.stdout, completed.stdout + completed.stderr)
    data = _inputs(["Go", "Redis", "Docker", "数控机床维修"])
    timings = []  # 测实际调用，包括默认库读取；不把导入时间伪装成匹配耗时。
    for _ in range(3):
        started = perf_counter()
        plan = build_learning_plan(**data)
        timings.append((perf_counter() - started) * 1000)
        _expect(plan.meta["cost_yuan"] == 0 and plan.meta["model_calls"] == 0, "关闭兜底仍有成本或模型调用")
    # 以最慢一次作为门槛，平均值不能掩盖单次超过用户要求的情况。
    _expect(max(timings) < 50, f"最慢调用 {max(timings):.3f}ms 超过 50ms")
    _check_semantic_fallback()  # 关闭路径的零模型与开启路径的错误审计都要覆盖。
    stats = []
    for name in ("app/learning_resources.py", "tools/check_materials.py", "tools/check_learning_resources.py"):
        content = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        # tokenize 排除字符串中的 #；AST 包括模块、类、所有私有函数及方法。
        comments = {token.start[0] for token in tokenize.generate_tokens(io.StringIO(content).readline)
                    if token.type == tokenize.COMMENT}
        ratio = len(comments) / len(content.splitlines())
        nodes = [node for node in ast.walk(ast.parse(content))  # 嵌套函数也纳入 docstring 分母。
                 if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
        _expect(ratio >= 0.15 and all(ast.get_docstring(node) for node in nodes), f"{name} 注释 {ratio:.1%} 或 docstring 缺失")
        stats.append(f"{Path(name).name}={ratio:.1%}")
    return f"隔离进程禁网/禁模型通过；cost=0；最慢 {max(timings):.3f}ms；兜底门槛/失败通过；" + "、".join(stats) + "；docstring 100%"


def _check_external() -> str:
    """⑩ 检查 external/local 的链接标记与本地路径校验。"""
    # 标记只取最终实际展示的材料，而非整个库是否存在外链。
    _expect(build_learning_plan(**_inputs(), materials=[_material()]).meta["has_external"] is True,
            "external 未标记")
    # 文件只在本用例存活；不使用不存在的材料冒充可访问本地来源。
    with NamedTemporaryFile(prefix="m8a-check-", suffix=".md", dir=PROJECT_ROOT / "docs/教材") as temporary:
        locator = Path(temporary.name).relative_to(PROJECT_ROOT).as_posix() + "#L40"
        local = _material(kind="local", locator=locator)  # 调用真实的本地路径校验器。
        plan = build_learning_plan(**_inputs(), materials=[local])
        _expect(plan.meta["has_external"] is False and len(plan.gaps[0].materials) == 1, "local 标记错误")
    _expect(build_learning_plan(**_inputs([]), materials=[]).meta["has_external"] is False, "空计划误标外链")
    return "external=True；真实 local 文件（带 #L40）=False；空计划=False"


def _check_example_gap() -> str:
    """⑪ 展开上游举例格式，保留主名及明确列出的多个例子。"""
    materials, _ = load_materials()
    gap = "消息队列（举例：Kafka）"  # 对照 S3.5 自身产出的固定格式。
    _expect([row.id for row in match_materials(gap, materials)] == ["kafka-official-quickstart"],
            "上游消息队列举例格式未命中 Kafka")
    # 主名没有对应标签时，顿号分隔的两个例子仍应各自匹配；无效长句不会分词。
    _expect({row.id for row in match_materials("技能组合（举例：Golang、Redis）", materials)} ==
            {"go-official-doc", "redis-official-doc"}, "多个例子或既有别名漏配")
    _expect(match_materials("维修（举例：车床）", materials) == [], "无关举例被凑数")
    return "消息队列（举例：Kafka） → kafka-official-quickstart；多例子/别名可匹配，无关举例为空"


def _check_prefixed_gap() -> str:
    """⑫ 剥离固定前缀后复用既有别名和同族规则，不修改缺口原文。"""
    materials, _ = load_materials()
    gap = "必备技能：Go"  # 命中后仍应展示这一整段缺口原文。
    _expect([row.id for row in match_materials(gap, materials)] == ["go-official-doc"], "必备技能前缀未剥离")
    # 验证另外两个指定前缀，以及连续前缀、内部空白和既有 Golang 别名。
    for candidate in ("JD 职责：Go", "负责 Go", "JD 职责：负责 Golang"):
        _expect([row.id for row in match_materials(candidate, materials)] == ["go-official-doc"], "固定前缀处理错误")
    plan = build_learning_plan(**_inputs([gap]), materials=materials)
    _expect(plan.gaps[0].gap == gap and not plan.unmatched, "匹配改写了缺口原文或漏配")
    return "必备技能：Go → go-official-doc；JD 职责/负责/连续前缀可匹配，缺口原文保留"


def _check_prefixed_unmatched() -> str:
    """⑬ 去掉负责前缀仍无关的长句必须进入 unmatched，绝不按短词凑数。"""
    materials, _ = load_materials()
    gap = "负责数控机床维修保养"
    _expect(match_materials(gap, materials) == [], "无关长句意外匹配材料")
    plan = build_learning_plan(**_inputs([gap]), materials=materials)
    # unmatched 和理由继续引用原文，不展示剥离前缀后的内部候选。
    _expect(plan.unmatched == [gap] and plan.gaps[0].materials == []
            and plan.meta["unmatched_reasons"] == {gap: "材料库暂无对应条目"}, "未匹配空态被放宽")
    for candidate in ("必备技能：Django", "负责 Go 接口开发", "消息队列（举例：Kafka）与车床维修"):
        _expect(match_materials(candidate, materials) == [], "对子串或不完整举例格式乱拆词")
    return "负责数控机床维修保养 → unmatched；Django、职责长句和带额外后缀的括号不误配"


def _check_family_gap_dedup() -> str:
    """⑭ 真实 M1/S3 必备技能只有消息队列时，学习计划仅有一条无自指文案。"""
    job = JobInfo(title="后端工程师", company=None, required_years_min=None, required_years_max=None,
                  education=None, must_have=["消息队列"], nice_to_have=[], responsibilities=[])
    # 诊断和扣分均执行原有模块，不能手填已经修好的消息队列扣分文案。
    resume = SimpleNamespace(name="测试", years=3, skills=["Python"])
    text = "姓名：测试\n技能\nPython\n工作经历\n维护服务"
    diagnosis = diagnose(text, resume, job)
    hard = score_hard(resume, job, text)
    _expect(diagnosis.missing_keywords == ["消息队列"], "M1 消息队列缺口来源不符")
    _expect(hard.deductions[0].item == "必备技能：消息队列", "硬性文案仍含自指举例")
    # 合并后材料仍应关联消息队列族，不丢掉学习材料，也不新增其他缺口。
    plan = build_learning_plan(diagnosis=diagnosis, hard=hard, sem=None, job_title=job.title)
    _expect([row.gap for row in plan.gaps] == ["消息队列"], "同一消息队列缺口重复展示")
    _expect([row.id for row in plan.gaps[0].materials] == ["kafka-official-quickstart"], "去重破坏材料匹配")
    _expect("举例" not in plan.gaps[0].gap and hard.breakdown["skills"] == 0,
            "仍有自指括号或改变了技能分")
    return "真实 M1/S3 消息队列场景 → 仅 1 条缺口，无自指括号，Kafka 材料保留"


def _check_main_name_dedup() -> str:
    """⑮ 四条原文合并为三个主名，最高严重度与最短文案独立保留。"""
    data = _inputs(["Go", "消息队列"])
    # 模拟修复前的上游报告，证明旧括号文案仍能被新的收集器去重。
    data["hard"] = HardScore(25, {}, [_deduction("消息队列（举例：消息队列）", "medium")])
    duty = "负责订单系统接口开发"
    data["sem"] = SimpleNamespace(per_item=[SimpleNamespace(item=duty, covered=False)])
    before = deepcopy(data)
    plan = build_learning_plan(**data, materials=[])
    _expect([row.gap for row in plan.gaps] == ["Go", "消息队列", duty], "4 条缺口未按主名合并为 3 条")
    _expect(data == before and plan.meta["gap_count_before_limit"] == 3, "修改上游输入或合并计数不符")
    # 短文案来自低严重度条目，也必须配上同主名另一条的最高严重度。
    mixed = _inputs([])
    mixed["hard"] = HardScore(25, {}, [_deduction("JD 职责：负责消 息 队 列（举例：Kafka）", "high"),
                                       _deduction("消息队列", "low")])
    result = build_learning_plan(**mixed, materials=[])
    _expect([(row.gap, row.severity) for row in result.gaps] == [("消息队列", "high")],
            "未同时保留最短文案和最高严重度")
    # 去重仅处理同主名的格式变体；别名、同族产品与不同真实职责都分别保留。
    distinct = ["Go", "Golang", "Kafka", "RabbitMQ", duty, "负责订单系统接口优化"]
    separate = build_learning_plan(**_inputs(distinct), materials=[])
    _expect([row.gap for row in separate.gaps] == distinct, "合并了 Go/Golang、不同产品或不同职责")
    # JD 明确分别写 Go/Golang 时，实际 S3 的两条扣分也必须完整保留。
    alias_job = JobInfo(title="后端工程师", company=None, required_years_min=None, required_years_max=None,
                        education=None, must_have=["Go", "Golang"], nice_to_have=[], responsibilities=[])
    alias_data = _inputs([])
    alias_data["hard"] = score_hard(SimpleNamespace(years=3, skills=[]), alias_job, "")
    aliases = build_learning_plan(**alias_data, materials=[])
    _expect([row.gap for row in aliases.gaps] == ["Go", "Golang"], "真实 JD 的 Go/Golang 两条缺口被误合并")
    return "4 → 3：Go / 消息队列 / 负责订单系统接口开发；取最高严重度和最短文案，Go/Golang 等独立保留"


def _check_fde_evaluation() -> str:
    """⑯ 效果评测缺口命中内容明确包含评测集与指标的新增材料。"""
    materials, _ = load_materials()
    # 一级标签直接命中，不用重排替身或篡改用户缺口制造成功。
    rows = match_materials("效果评测", materials)
    _expect(any(row.id == "fde-evaluation" for row in rows), "效果评测没有命中 FDE 评测材料")
    # 加载默认库走真实校验，排除只在注入的测试列表里命中的情况。
    plan = build_learning_plan(**_inputs(["效果评测"]))
    _expect(any(row.id == "fde-evaluation" for row in plan.gaps[0].materials) and not plan.unmatched,
            "评测材料在完整计划中丢失")
    return "效果评测 → fde-evaluation；评测集/指标标签来自实际内容，缺口原文不变"


def _check_fde_requirements() -> str:
    """⑰ 需求梳理与拆解使用真正讲业务需求转用例的 FDE 材料。"""
    materials, _ = load_materials()
    rows = match_materials("需求梳理与拆解", materials)
    # 这里要求专门的需求材料，不能用相似词或同属 AI 的其他材料替代。
    _expect(any(row.id == "fde-requirements" for row in rows), "需求拆解未命中需求材料")
    # 不用 Kafka 或 Go 文档凑能力缺口，匹配后原始能力文案也必须保留。
    plan = build_learning_plan(**_inputs(["需求梳理与拆解"]))
    _expect(plan.gaps[0].gap == "需求梳理与拆解" and not plan.unmatched and
            all(row.id.startswith("fde-") for row in plan.gaps[0].materials), "需求缺口被无关材料凑数")
    return "需求梳理与拆解 → fde-requirements；官方业务问题→用例→优先级→成功指标指南"


def _check_semantic_switch() -> str:
    """⑱ 明确开关状态，真实向量满足双门槛才采用；异常降级未匹配。"""
    materials, _ = load_materials()
    data = _inputs(["把客户想解决的问题整理清楚，再列出优先顺序和成功标准"])
    disabled = build_learning_plan(**data, materials=materials, semantic_fallback=False)
    _expect(disabled.meta["semantic_fallback"] == "disabled" and disabled.meta["semantic_attempts"] == 0,
            "False 未关闭兜底")
    # 同一个评分缓存模型真实编码，成功路径不再用固定分数替身。
    _real_embedding_model()
    enabled = build_learning_plan(**data, materials=materials, semantic_fallback=True)
    _expect(enabled.meta["semantic_fallback"] != "disabled" and enabled.meta["semantic_attempts"] > 0
            and enabled.meta["cost_yuan"] == 0 and not enabled.unmatched, "True 未启用本地零费用兜底")
    _expect([row.id for row in enabled.gaps[0].materials] == ["fde-requirements"], "真实向量返回无关材料")
    # 即便本地推理抛出异常，计划仍可用且保留未匹配缺口，不能抛到 HTTP 500。
    with patch.object(module, "_local_semantic_match", side_effect=RuntimeError("PRIVATE_REORDER_FAILURE")):
        failed = build_learning_plan(**data, materials=materials, semantic_fallback=True)
    _expect(failed.meta["semantic_fallback"] == "unavailable" and failed.meta["semantic_errors"] and
            failed.unmatched == data["diagnosis"].missing_keywords and failed.meta["cost_yuan"] == 0,
            "重排异常未降级或漏记 semantic_errors")
    # 审计保留错误类型，原始本地异常正文不进入可公开的计划 meta。
    _expect("PRIVATE_REORDER_FAILURE" not in str(failed.meta), "重排异常详情泄漏")
    return "False=disabled/0次；True=used/1次真实向量/0元；异常=unavailable+unmatched+semantic_errors"


@lru_cache(maxsize=1)
def _real_embedding_model():
    """读取项目既有评分缓存中的 rag.EMBED_MODEL_NAME 模型，所有真样本共用一个实例。"""
    from app.scoring_semantic import _get_model
    # 先通过原评分工厂取已有缓存；学习模块本身没有创建模型的入口。
    model = _get_model()
    _expect(module._loaded_embedding_model() is model, "学习匹配未复用评分/RAG已有模型")
    return model


def _real_pair_cosine(gap: str, material: Material) -> float:
    """直接从真向量独立计算余弦，不用被测函数或预制分数作为期望。"""
    # 成对编码的输入包含原始标题与 tags，完全复用生产材料表述。
    vectors = [list(map(float, row)) for row in _real_embedding_model().embed(
        [gap, module._semantic_document(material)], batch_size=32)]
    left, right = vectors
    # 独立计算分子与两个范数，不把生产逻辑的通过/拒绝布尔值当作真实分数。
    return sum(a * b for a, b in zip(left, right)) / math.sqrt(sum(x * x for x in left) * sum(x * x for x in right))


def _check_keyword_evaluation() -> str:
    """⑲ 真长句先走关键词，明确的评测能力不能被泛化的业务片段抢占。"""
    gap = "建立评测和反馈机制并根据真实业务结果迭代"
    plan = build_learning_plan(**_inputs([gap]), semantic_fallback=True)
    row = plan.gaps[0]
    # 明确核对最终材料 id，不能只看非空或仅断言关键词函数被调用。
    _expect(row.meta["matched_by"] == "keyword" and [item.id for item in row.materials] == ["fde-evaluation"],
            "评测长句未确定命中 fde-evaluation")
    # 关键词命中无需调用模型，材料只在推荐副本中标注匹配方式。
    _expect(plan.meta["semantic_attempts"] == 0 and "匹配方式：关键词" in row.materials[0].note, "关键词证据或调用数错误")
    REAL_SAMPLES["evaluation_keyword"] = row.model_dump()
    # 完整推荐副本进入取证 JSON，包含真实匹配方式及关键词。
    return f"{gap} → fde-evaluation；matched_by=keyword；模型调用0"


def _check_keyword_business() -> str:
    """⑳ 真业务长句识别业务拆解的两字成分，不新增或修改任何材料标签。"""
    gap = "梳理业务流程、关键判断和专业经验并转化为AI场景方案"
    before = module.MATERIALS_PATH.read_bytes()
    # 对整个文件取快照，能捕捉为过用例而写入同义词等越界修改。
    plan = build_learning_plan(**_inputs([gap]), semantic_fallback=True)
    row = plan.gaps[0]
    _expect(row.meta["matched_by"] == "keyword" and [item.id for item in row.materials] == ["fde-requirements"],
            "业务拆解长句未确定命中需求材料")
    _expect(module.MATERIALS_PATH.read_bytes() == before, "关键词匹配污染材料库")
    # 快照还覆盖原始 note，允许的说明标注只能出现在返回副本中。
    # 单字中文、短英文及 fragment 内的 rag 都不能用作能力证据。
    for invalid in ("评", "AG", "fragment storage ai", "编写公司制度文档"):
        _expect(module._keyword_match(invalid, load_materials()[0])[0] == [], "短片段或英文子串误命中")
    REAL_SAMPLES["business_keyword"] = row.model_dump()
    return f"{gap} → fde-requirements；matched_by=keyword；命中词={row.meta['keywords']}；原tag未改"


def _check_honest_semantic_none() -> str:
    """㉑ 使用真模型拒绝不能明确区分材料的方案落地长句。"""
    _real_embedding_model()
    gap = "推动方案从演示走向实际使用并解决使用中的问题"
    # 使用用户给定原句，不改写成更容易被判为无关的负例。
    plan = build_learning_plan(**_inputs([gap]), semantic_fallback=True)
    row = plan.gaps[0]
    _expect(row.meta["matched_by"] == "none" and row.materials == [] and plan.unmatched == [gap], "模糊方案落地被乱配")
    _expect(plan.meta["semantic_attempts"] == 1 and not plan.meta["semantic_errors"], "没有真实执行语义判据")
    # 同时记录首位余弦及差距，能看出是判据拒绝，而非模型没有执行。
    _expect(row.meta["top1_cos"] < .45 or row.meta["margin"] < .03, "未满足拒绝依据")
    REAL_SAMPLES["honest_none"] = row.model_dump()
    # 实际 JD 可能抽取到 skill/skil；没有材料标签支持时不能猜成 Git 等工具。
    isolated = build_learning_plan(**_inputs(["skil"]), semantic_fallback=True)
    _expect(isolated.gaps[0].meta["matched_by"] == "none" and isolated.unmatched == ["skil"], "未知英文词被错误语义配对")
    return f"matched_by=none；top1={row.meta['top1_cos']:.6f}；margin={row.meta['margin']:.6f}；保持unmatched"


def _check_real_cosine_pairs() -> str:
    """㉒ 相关与无关样本实际编码，固定 0.45 分界，不 mock 模型或伪造分数。"""
    materials = {row.id: row for row in load_materials()[0]}
    # 正反样本均用同一批真实库内容；相关对与不相关对分别核对阈值两侧。
    positive = _real_pair_cosine("梳理业务流程、关键判断和专业经验并转化为AI场景方案", materials["fde-requirements"])
    # 无关对仍使用评测长句，检验 Git 文档不会被语言表面相似抬过阈值。
    negative = _real_pair_cosine("建立评测和反馈机制并根据真实业务结果迭代", materials["git-official-reference"])
    _expect(positive >= .45 and negative < .45, "真实相关/无关对未通过0.45分界")
    _expect(module.SEMANTIC_MIN_COS == .45 and module.SEMANTIC_MIN_MARGIN == .03, "放宽了用户指定判据")
    REAL_SAMPLES["cosine_pairs"] = {"related": positive, "unrelated": negative}
    return f"真实bge-small-zh：需求相关={positive:.6f}≥0.45；评测vsGit={negative:.6f}<0.45"


def _check_all_match_methods() -> str:
    """㉓ 一个真实计划包含四种方式，逐 gap 与总 meta 同源，复用同一模型实例。"""
    model = _real_embedding_model()
    # 第三句没有白名单能力词，必须真正进入语义层，不能由关键词提前命中。
    gaps = ["Go", "建立评测和反馈机制并根据真实业务结果迭代",
            "把客户想解决的问题整理清楚，再列出优先顺序和成功标准",
            "推动方案从演示走向实际使用并解决使用中的问题"]
    plan = build_learning_plan(**_inputs(gaps), semantic_fallback=True)
    # 同一次计划内比较四种方式，排除多次构造不同库而人为拼齐四值。
    actual = [row.meta["matched_by"] for row in plan.gaps]
    # 列表期望同时检查四种方式的顺序，防止 family 或 keyword 偷跑语义层。
    _expect(actual == ["family", "keyword", "semantic", "none"], "四种匹配方式缺失或优先级错误")
    _expect(plan.meta["matched_by"] == dict(zip(gaps, actual)), "逐gap的matched_by没有写入总meta")
    _expect([item.id for item in plan.gaps[2].materials] == ["fde-requirements"], "语义正例误配")
    _expect(module._loaded_embedding_model() is model and plan.meta["local_model_calls"] == {"embedding": 2, "reranker": 0}
            and plan.meta["cost_yuan"] == 0, "加载了第二个模型、重排或产生费用")
    REAL_SAMPLES["all_methods"] = plan.model_dump()
    # 模型实例身份与实际调用计数一起证明复用，没有冷加载另一套权重。
    return "family/keyword/semantic/none四值齐全；逐gap.meta与plan.meta一致；同一模型、2次编码、0重排、0元"


def _check_semantic_shared_gate() -> str:
    """㉔ 真模型的 AI 编程误配必须被共享词门槛拒绝，并保留原候选余弦。"""
    _real_embedding_model()
    gap = "AI编程工具"
    # 使用完整生产材料库与真实 bge 向量，不能靠缩小候选库制造拒绝结果。
    plan = build_learning_plan(**_inputs([gap]), semantic_fallback=True)
    row = plan.gaps[0]
    # 同时检查材料、逐项状态及 unmatched，避免仅改显示标记而仍附上误配材料。
    _expect(row.materials == [] and row.meta["matched_by"] == "none" and plan.unmatched == [gap],
            "AI编程工具仍被语义配到 Prompt 指南")
    _expect(row.meta["top1_cos"] >= .45 and row.meta["margin"] >= .03, "没有实际覆盖余弦通过后的拒绝路径")
    # 被拒分数应精确沿用原始 top1，不能填零或硬编码历史测量值。
    rejected = row.meta["rejected_semantic"]
    _expect(len(rejected) == 1 and rejected[0]["id"] == "fde-prompt-engineering"
            and math.isfinite(rejected[0]["cos"]) and rejected[0]["cos"] == row.meta["top1_cos"]
            and rejected[0]["reason"] == "no_shared_terms", "拒绝记录缺少原材料 id、真实余弦或原因")
    _expect(plan.meta["matched_by"][gap] == "none" and plan.meta["rejected_semantic"] == {gap: rejected}
            and not plan.meta["semantic_errors"], "拒绝审计未汇总或被误记为模型故障")
    # 各字段独立验证最短长度：不能靠单字、AI、跨空格或跨标签拼接放行。
    for field in ("tags", "title", "note"):
        values = dict(tags=["zzzz"], title="zzzz", note="zzzz")
        values[field] = ["编程", "PrOmPt"] if field == "tags" else "编程 PrOmPt"
        material = _material(**values)
        _expect(module._semantic_shared_terms("辅助编程", material) == ["编程"], f"{field} 两字共享未通过")
        _expect(module._semantic_shared_terms("PROMPT设计", material), f"{field} 英文大小写不敏感失效")
        for invalid in ("编工具", "AI工具", "PR工具", "编 程", "P R O"):
            _expect(not module._semantic_shared_terms(invalid, material), f"{field} 放过短片段：{invalid}")
    _expect(not module._semantic_shared_terms("编程", _material(tags=["编", "程"], title="zzzz", note="zzzz")),
            "把不同标签的单字拼成了共享词")
    _expect(module._semantic_shared_terms("abc工具", _material(tags=["xABCy"], title="zzzz", note="zzzz")) == ["abc"],
            "三个英文字母的共享子串未通过")
    REAL_SAMPLES["rejected_ai_coding"] = plan.model_dump()
    return f"AI编程工具 → none/unmatched；拒绝 fde-prompt-engineering cos={rejected[0]['cos']:.6f}；中文≥2字/英文≥3字母，含大小写与字段边界"


def _check_semantic_pdf_note() -> str:
    """㉕ 真语义推荐读回 PDF 提示，关键词与同族材料的独立 PDF 均无提示。"""
    from io import BytesIO
    import pdfplumber
    from app.revision_pdf import render_review_pdf
    from tools.check_revision_pdf import _inputs as pdf_inputs

    # 仅借用合成报告骨架；学习计划仍经过生产匹配器，匹配方式不能手工伪装。
    _real_embedding_model()
    note = "系统按语义相似推荐，请自行判断是否对得上"
    samples = (("把客户想解决的问题整理清楚，再列出优先顺序和成功标准", "semantic"),
               ("建立评测和反馈机制并根据真实业务结果迭代", "keyword"), ("Go", "family"))
    output = PROJECT_ROOT / "output/pdf/d_m8_07"
    output.mkdir(parents=True, exist_ok=True)
    for gap, method in samples:
        plan = build_learning_plan(**_inputs([gap]), semantic_fallback=True)
        _expect(plan.gaps[0].meta["matched_by"] == method and plan.gaps[0].materials, f"{method} 样本未真正匹配")
        report, diagnosis, hard = pdf_inputs()
        # 每种方式单独生成报告，防止关键词的误提示被另一条语义提示掩盖。
        data = render_review_pdf(report, diagnosis, job_title=report.profile.title, resume_name="语义提示验收",
                                 hard=hard, learning_plan=plan)
        with pdfplumber.open(BytesIO(data)) as document:
            # 按真实 PDF 自动换行去空白，再比较完整指定文案。
            text = "".join("".join((page.extract_text() or "").split()) for page in document.pages)
        _expect((note in text) == (method == "semantic"), f"{method} 的 PDF 提示条件不符")
        # 保存刚读回的同一份字节，供渲染目检；不另生成一份不同报告。
        (output / f"{method}_learning.pdf").write_bytes(data)
    return "真语义样本 PDF 可读完整提示；keyword/family 两份 PDF 均无该句"


def _check_keyword_regression() -> str:
    """㉖ 原评测、业务识别和流程梳理三条的材料与匹配方式均不受新闸门影响。"""
    samples = [("建立评测和反馈机制，通过真实业务结果判断方案是否有效，并打待续迭代", "fde-evaluation"),
               ("深入一线业务，与业务负责人和一线员工共同工作，识别真正值得AI借鉴的问题", "fde-requirements"),
               ("梳理业务流程、关键判断和专业经验，将其转化为AI可以理解和执行的场景方案", "fde-requirements")]
    # 直接封锁共享词与语义入口，确保精确层没有新增限制或模型依赖。
    with patch.object(module, "_semantic_shared_terms", side_effect=AssertionError("keyword 不应受共享词门槛限制")), \
            patch.object(module, "_local_semantic_match", side_effect=AssertionError("keyword 不应调用语义")):
        plan = build_learning_plan(**_inputs([gap for gap, _ in samples]), semantic_fallback=True)
        family = build_learning_plan(**_inputs(["Go"]), semantic_fallback=True)
    # 使用已登记的三句真实 JD 原话，逐项比较确定的材料 id 与匹配方式。
    for row, (_, expected) in zip(plan.gaps, samples):
        _expect(row.meta["matched_by"] == "keyword" and [item.id for item in row.materials] == [expected], "原三条 keyword 回归")
    _expect(plan.meta["semantic_attempts"] == 0 and plan.meta["rejected_semantic"] == {} and not plan.unmatched,
            "关键词精确命中受到语义门槛影响")
    _expect(family.gaps[0].meta["matched_by"] == "family" and [item.id for item in family.gaps[0].materials] == ["go-official-doc"],
            "同族匹配受共享词门槛影响")
    # 审计快照保留原关键词与返回材料，方便与 D-M8-06 的真实记录逐项对照。
    REAL_SAMPLES["keyword_regression"] = plan.model_dump()
    return "评测→fde-evaluation；业务识别/流程梳理→fde-requirements；三条均keyword、模型0次；family亦绕过新门槛"


def main() -> int:
    """执行全部二十六组检查，打印结果与最终通过数，失败返回非零。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    # 编号与任务书一一对应；分组中的任何断言失败都会影响最终退出码。
    checks = [("① 材料库", _check_library), ("② 别名匹配", _check_aliases), ("③ 同族匹配", _check_families),
              ("④ 诚实空态", _check_unmatched), ("⑤ 同源缺口", _check_sources), ("⑥ 三层限额", _check_limits),
              ("⑦ 确定排序", _check_order), ("⑧ 确定性/纯函数", _check_determinism),
              ("⑨ 零模型/性能/规范", _check_zero_model), ("⑩ 链接标注", _check_external),
              ("⑪ D-M8-02 举例匹配", _check_example_gap), ("⑫ D-M8-02 前缀匹配", _check_prefixed_gap),
              ("⑬ D-M8-02 无关长句", _check_prefixed_unmatched),
              ("⑭ D-M8-03 单一族缺口", _check_family_gap_dedup),
              ("⑮ D-M8-03 主名去重", _check_main_name_dedup),
              ("⑯ D-M8-04 效果评测", _check_fde_evaluation), ("⑰ D-M8-04 需求拆解", _check_fde_requirements),
              ("⑱ D-M8-05 语义兜底开关", _check_semantic_switch),
              ("⑲ D-M8-06 评测关键词", _check_keyword_evaluation), ("⑳ D-M8-06 业务关键词", _check_keyword_business),
              ("㉑ D-M8-06 诚实未匹配", _check_honest_semantic_none), ("㉒ D-M8-06 真余弦对", _check_real_cosine_pairs),
              ("㉓ D-M8-06 四种匹配方式", _check_all_match_methods),
              ("㉔ D-M8-07 共享词门槛", _check_semantic_shared_gate),
              ("㉕ D-M8-07 PDF 语义提示", _check_semantic_pdf_note),
              ("㉖ D-M8-07 keyword 不变", _check_keyword_regression)]
    passed = 0  # 只在 action 实际完成后递增，不把跳过算作通过。
    with ExitStack() as stack:
        # 真模型使用已有权重，整套验收禁止联网或下载，避免把远端响应当作本地分数。
        stack.enter_context(patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"}))
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo"):
            stack.enter_context(patch(target, side_effect=AssertionError("学习计划验收禁止联网")))
        for label, action in checks:
            try:
                detail = action()
            except Exception as exc:  # 一组失败仍继续打印其余组，留下完整诊断。
                print(f"{label}：失败；{type(exc).__name__}: {exc}", flush=True)
            else:
                passed += 1
                print(f"{label}：通过；{detail}", flush=True)
    output = PROJECT_ROOT / "output/d_m8_07"
    # 取证包含真模型浮点值，后续交付直接读此文件而非复制一组手填示例。
    output.mkdir(parents=True, exist_ok=True)
    (output / "real_model_samples.json").write_text(json.dumps(REAL_SAMPLES, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 末行保持任务书指定格式，便于其他脚本或人工核对。
    print(f"通过 {passed}/{len(checks)}", flush=True)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    # 断言失败必须传递到退出码，不能仅在终端显示失败。
    raise SystemExit(main())
