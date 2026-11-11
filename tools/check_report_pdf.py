"""S7a 离线验收：合成三阶段结果，生成并读回三份 PDF；禁止联网与模型执行。"""

from __future__ import annotations

import ast
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO, StringIO
from pathlib import Path
import re
import sys
from time import perf_counter
import tokenize
from types import SimpleNamespace
from unittest.mock import patch

import pdfplumber

# 支持从任意工作目录直接运行脚本，产物始终写入项目 reports/。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.match_report import build_report
from app.report_pdf import DISCLAIMER, _limit_for_width, render_report
from app.schemas_match import Deduction, JobInfo, MatchReport

# 固定证据便于从PDF真实文本定位，不通过读取渲染器的内部行结构断言。
YEARS_JD = "至少3年后端工作经验"
EDUCATION_JD = "本科及以上学历"
TRUNCATED_TAIL = "超限尾标不应出现"
LONG_EVIDENCE = "<验收&原句>" + "负责订单服务开发并提供可回查的职责证据。" * 9 + TRUNCATED_TAIL
# 所有标题按输出契约列出，避免只检查某一页中偶然出现的三个关键词。
SECTION_NAMES = ("扣分理由", "语义覆盖明细", "简历关键字", "简历主要项", "对比表", "缺失与不符", "建议补充")


@dataclass
class Artifact:
    """保留本次真实输出的大小、页数、耗时与读回文本，供独立断言使用。"""

    # 文件路径为绝对路径，交付时不依赖调用者的当前目录。
    case: str
    report: MatchReport
    path: Path
    byte_count: int
    # 文档页数来自PDF解析，不能通过正文长度推测。
    pages: int
    elapsed_ms: float
    # 合并文本只用于内容断言，页脚和坐标另逐页读取。
    text: str


def _deduction(item: str, kind: str, deduct: int, jd: str, resume: str) -> Deduction:
    """用合成证据建立扣分或待确认条目，不执行硬分规则。"""
    # 本脚本只检验排版数据传递，合成分数不代表真实规则的评分输出。
    return Deduction(item=item, type=kind, deduct=deduct, jd_evidence=jd,
                     resume_evidence=resume, severity="medium")


def _reports() -> tuple[JobInfo, list[tuple[str, MatchReport]]]:
    """直接实例化 HardScore/SemanticScore/LlmScore，构造指定 A/B/C 数据。"""
    # 导入发生在 main 的联网封锁内；只创建数据类，不触发 score_*。
    from app.scoring_llm import LlmItem, LlmScore
    from app.scoring_rules import HardScore
    from app.scoring_semantic import ItemSim, SemanticScore

    responsibilities = ["负责 Go 订单接口开发", "优化 MySQL 查询性能", "维护 Kubernetes 集群"]
    # JD字段固定，重复运行不会下载语料或读取真实个人简历。
    job = JobInfo(title="Go 后端工程师", company=None, required_years_min=3,
                  required_years_max=None, education=EDUCATION_JD, must_have=["Go", "Kubernetes"],
                  nice_to_have=[], responsibilities=responsibilities)
    resume_text = ("张三\n3年工作经验，专科\n最近公司：云杉有限公司\n"
                   "Go Go Go PHP PHP MySQL Redis\n负责 Go 订单接口开发，优化 MySQL 查询性能。"
                   "优化订单查询耗时降低50%。")
    # SimpleNamespace 避免导入旧 ResumeInfo 模块时初始化模型客户端。
    resume = SimpleNamespace(name="张三", years=3.0, skills=["Go", "PHP", "MySQL", "Redis"])
    hard = HardScore(33, {"years": 15, "education": 5, "skills": 13}, [
        _deduction("学历", "education", 5, EDUCATION_JD, "专科学历"),
        _deduction("Kubernetes", "missing_skill", 2, responsibilities[2], "简历未体现"),
    ])
    # 余弦特意包含0.5以下值，重排包含负数与多于三位的小数。
    sem = SemanticScore(24, [
        ItemSim(responsibilities[0], .892, "负责 Go 订单接口开发", True, 5.5312),
        ItemSim(responsibilities[1], .814, "优化 MySQL 查询性能", True, 3.2134),
        ItemSim(responsibilities[2], .412, "负责 Go 订单接口开发", False, -1.0536),
    ], [responsibilities[2]])
    # 三个 rubric 只提供现成分数、证据和建议，不运行 LLM 校验或请求。
    llm = LlmScore(21, [
        LlmItem("stack_depth", "技术栈深度", 8, 12, responsibilities[0], "负责 Go 订单接口开发", "合成依据"),
        LlmItem("domain_fit", "业务场景相似度", 7, 10, responsibilities[1], "优化 MySQL 查询性能", "合成依据"),
        LlmItem("proof_strength", "成果可信度", 6, 8, responsibilities[1], "优化订单查询耗时降低50%", "合成依据"),
    ], ["补充已有订单接口的职责与成果证据"], [], "ok", [], 0.0)
    jd_text = "\n".join([YEARS_JD, EDUCATION_JD, *responsibilities])
    # 2310是合成报告的评分耗时，真实PDF渲染耗时另用perf_counter计量。
    meta = {"model": "synthetic-offline", "elapsed_ms": 2310}
    common = dict(resume=resume, job=job, resume_text=resume_text, jd_text=jd_text)
    a = build_report(**common, hard=hard, sem=sem, llm=llm, meta=meta)
    # 职责缺口与 missing_skill 使用相同JD证据，S6去重后恰好一个missing。
    assert (a.score.total, a.score.breakdown["hard"], a.score.breakdown["semantic"],
            a.score.breakdown["llm"]) == (78, 33, 24, 21)
    assert len(a.semantic_items) == 3 and sum(item.covered for item in a.semantic_items) == 2
    # A的形状先断言，防止汇总器行为变化后静默改变任务书指定样本。
    assert (len(a.deductions), len(a.missing), len(a.mismatches)) == (2, 1, 1)
    unknown = HardScore(15, {"years": 0, "education": 0, "skills": 15}, [
        _deduction("工作年限待确认", "years_unknown", 0, YEARS_JD, "简历未写年限"),
        _deduction("学历待确认", "education_unknown", 0, EDUCATION_JD, "简历未写学历"),
    ])
    # B 另外包含六条长职责，实际覆盖至少三页，检查分页而非只造一页样本。
    long_items = [ItemSim(f"扩展职责{index}：{LONG_EVIDENCE}", .812, LONG_EVIDENCE, True, 2.3456)
                  for index in range(1, 7)]
    b_sem = SemanticScore(24, [*sem.per_item, *long_items], list(sem.uncovered))
    # 长职责都沿用覆盖状态；分页压力不通过模型推理生成。
    review = LlmScore(21, list(llm.items), list(llm.suggestions), [], "needs_review",
                      ["简历未写年限及学历，需人工补充"], 0.0)
    # B 的简历原文同时去掉年限学历，验证缺失主要项不会被补造。
    b_common = dict(common, resume=SimpleNamespace(name="张三", years=None, skills=resume.skills),
                    resume_text="张三\nGo Go Go PHP PHP MySQL Redis\n负责 Go 订单接口开发。")
    b = build_report(**b_common, hard=unknown, sem=b_sem, llm=review, meta=meta)
    # 失败原因显式非空，确保红色告警之外也能追溯具体阶段。
    failed = LlmScore(0, [], [], [], "failed", ["合成 LLM 环节失败，未执行任何模型调用"], 0.0)
    c = build_report(**common, hard=hard, sem=sem, llm=failed, meta=meta)
    # C只把LLM设零，不改变既有硬分或语义分，更不能把整个报告归零。
    # 先验证生产汇总器的真实透传，再渲染，避免Pydantic忽略明细而验收空表。
    assert a.semantic_items[0].rerank_score == sem.per_item[0].rerank_score
    assert b.status == "needs_review" and c.status == "failed" and c.reasons
    assert c.score.breakdown["llm"] == 0
    return job, [("A", a), ("B", b), ("C", c)]


def _generate(job: JobInfo, reports: list[tuple[str, MatchReport]]) -> list[Artifact]:
    """仅计时渲染，写入三份PDF并用pdfplumber真实读回页数和文本。"""
    directory = PROJECT_ROOT / "reports"
    directory.mkdir(exist_ok=True)
    # 不删除或覆盖同目录里的其他报告；文件名带本次生成时间。
    artifacts = []
    for case, report in reports:
        # 文件名保留PRD结构，并增加用例及总分，便于对照读回验收数字。
        stamp = datetime.fromisoformat(report.meta["generated_at"]).astimezone().strftime("%Y%m%d-%H%M")
        path = directory / f"简历评分_合成{case}_{report.score.total}分_{job.title}_{stamp}.pdf"
        before = report.model_dump_json()
        # 完整序列化前后比较，捕获对任何分项、证据或元数据的写回。
        started = perf_counter()
        data = render_report(report, job, resume_name=f"合成{case}.pdf")
        elapsed_ms = (perf_counter() - started) * 1000
        # 渲染不能修改输入，业务耗时2310ms也不能被PDF生成计时覆盖。
        assert report.model_dump_json() == before
        path.write_bytes(data)
        # 验收读取最终落盘文件，不读取生成之前的缓存文本。
        with pdfplumber.open(path) as document:
            pages = len(document.pages)
            text = "\n".join(page.extract_text() or "" for page in document.pages)
        artifacts.append(Artifact(case, report, path.resolve(), len(data), pages, elapsed_ms, text))
        print(f"用例 {case}：总分={report.score.total}；status={report.status}；"
              f"字节数={len(data)}；页数={pages}；渲染耗时={elapsed_ms:.2f}ms；成本=0元")
    return artifacts


def _files(artifacts: list[Artifact]) -> None:
    """检查规定①：三份真实输出均成功且超过8KB。"""
    assert len(artifacts) == 3
    # stat 同时核实返回字节实际完整落盘，而非只检查内存长度。
    assert all(item.path.stat().st_size == item.byte_count > 8 * 1024 for item in artifacts)


def _required_text(artifacts: list[Artifact]) -> None:
    """检查规定②：每份PDF有总分、章节和免责文字。"""
    for item in artifacts:
        # 文件名总分和正文总分必须对得上，不能三份都套同一模板数字。
        assert f"_{item.report.score.total}分_" in item.path.name
        assert str(item.report.score.total) in item.text
        assert all(text in item.text for text in ("扣分理由", "语义覆盖明细", "建议补充", "本报告由 AI 生成"))
    # v1.2.1只检查实际扣分行，不能借重排负数或其他章节中的数字通过。
    deductions = artifacts[0].text.split("扣分理由", 1)[1].split("语义覆盖明细", 1)[0]
    assert "-5" in deductions and not re.search(r"(?<![-\d])5(?!\d)", deductions), deductions
    # B的零扣分必须显示横线，工作年限证据中的3年不受影响。
    zeros = artifacts[1].text.split("扣分理由", 1)[1].split("语义覆盖明细", 1)[0]
    # 扣分表后可能翻页，按证据定位两行，排除页脚中的零成本数字。
    zero_rows = [line for line in zeros.splitlines() if YEARS_JD in line or EDUCATION_JD in line]
    assert len(zero_rows) == 2 and all("—" in line and not re.search(r"(?<!\d)0(?!\d)", line)
                                      for line in zero_rows), zero_rows


def _unknown(artifacts: list[Artifact]) -> None:
    """检查规定③：只截取B对比表区域，逐条核实未知年限学历为问号。"""
    b = artifacts[1]
    # 同名证据也存在于扣分表和缺失节，限定区域才能真正检查对比表。
    comparison = b.text.split("对比表", 1)[1].split("缺失与不符", 1)[0]
    for evidence in (YEARS_JD, EDUCATION_JD):
        # 证据与判定同处一行，不能借图例或其他行中的问号蒙混过关。
        rows = [line for line in comparison.splitlines() if evidence in line]
        assert len(rows) == 1, f"对比表找不到唯一证据行：{evidence}"
        assert "?" in rows[0] and "✗" not in rows[0] and "不符合" not in rows[0], rows[0]
    assert "待确认（需人工补充）" in b.text


def _audit(artifacts: list[Artifact]) -> None:
    """检查规定④：逐页核实完整免责、模型/prompt/成本/耗时/生成时间。"""
    for item in artifacts:
        # 每次重新打开最终PDF，页脚检查不依赖生成器内存中的段落列表。
        with pdfplumber.open(item.path) as document:
            for page in document.pages:
                # 固定页脚区域读回，防止正文里出现关键词掩盖页脚缺失。
                text = page.crop((0, page.height - 110, page.width, page.height)).extract_text() or ""
                assert all(word in text for word in (DISCLAIMER, "模型 synthetic-offline", "prompt",
                                                       "成本 ¥0.0", "耗时 2310ms", "生成于")), text


def _alerts(artifacts: list[Artifact]) -> None:
    """检查规定⑤及告警底色：失败红底，待确认黄底，正常组无告警。"""
    assert "部分环节失败，分数仅供参考" in artifacts[2].text
    assert "本报告存在待确认项，请人工复核" in artifacts[1].text
    assert "请人工复核" not in artifacts[0].text.split("总分", 1)[0].split("状态：", 1)[0]
    for artifact, expected in ((artifacts[1], (254, 240, 199)), (artifacts[2], (254, 228, 226))):
        # pdfplumber 同时读绘图对象，红黄底必须真实存在于第一页面。
        with pdfplumber.open(artifact.path) as document:
            fills = [rect.get("non_stroking_color") for rect in document.pages[0].rects]
            # PDF颜色为0至1浮点，按一档RGB误差比较以容纳序列化舍入。
            assert any(isinstance(fill, (tuple, list)) and len(fill) == 3 and
                       all(abs(actual * 255 - target) < 1 for actual, target in zip(fill, expected))
                       for fill in fills), fills


def _chinese(artifacts: list[Artifact]) -> None:
    """检查规定⑥及四态：中文可读，勾叉问号横线均可从PDF提取。"""
    for item in artifacts:
        # 透明的符号文本同样必须正确编码，不能只有肉眼可见的勾叉路径。
        assert all(word in item.text for word in ("简历", "岗位", "匹配", "✓", "✗", "?", "—"))
        assert "\ufffd" not in item.text


def _layout(artifacts: list[Artifact]) -> None:
    """检查A4边界、长字段截断、章节顺序及长表跨页重复表头。"""
    assert artifacts[1].pages >= 3
    # 列宽阈值两侧及84mm漏改点均覆盖，小数宽度也不能落入规则缝隙。
    assert [_limit_for_width(width) for width in (55, 55.5, 56, 75, 84, 120, 120.5, 170)] == \
           [40, 80, 80, 80, 80, 80, 120, 120]
    # 下界是实际页数，不把表格的理论分页数当作成功输出。
    assert TRUNCATED_TAIL not in artifacts[1].text and "…" in artifacts[1].text
    # 特殊字符必须按原文读回，验证Paragraph转义没有漏掉小于号和与号。
    assert "<验收&原句>" in artifacts[1].text
    for item in artifacts:
        offsets = [item.text.index(name) for name in SECTION_NAMES]
        assert offsets == sorted(offsets)
        with pdfplumber.open(item.path) as document:
            for page in document.pages:
                assert abs(page.width - 595.276) < 1 and abs(page.height - 841.89) < 1
                # 对实际字符边界检查，连页脚也不能进入18mm外边距。
                assert all(char["x0"] >= 50 and char["x1"] <= page.width - 50 and
                           char["top"] >= 50 and char["bottom"] <= page.height - 49
                           for char in page.chars), f"{item.case}第{page.page_number}页文本越界"
    with pdfplumber.open(artifacts[1].path) as document:
        text_pages = [page.extract_text() or "" for page in document.pages]
        # 短截断后语义表可整页容纳；实际发生跨页时，每一片仍须有表头。
        first = next(index for index, text in enumerate(text_pages) if "语义覆盖明细" in text)
        last = next(index for index, text in enumerate(text_pages) if "简历关键字" in text)
        assert all("命中的简历原句" in text for text in text_pages[first:last + 1]
                   if text.split("简历关键字", 1)[0].strip())
        jd_cells = {"JD 职责": [], "JD 要求": []}
        resume_cells = []
        for page in document.pages:
            # 从PDF表头定位实际列界，职责55mm、对比表75/84mm分别验收。
            for header in page.search(r"JD (?:职责|要求)", return_chars=False):
                semantic = header["text"] == "JD 职责"
                following = next(item for item in page.search("余弦" if semantic else "判定", return_chars=False)
                                 if abs(item["top"] - header["top"]) < 1)
                left, right = header["x0"] - 5, following["x0"] - (1 if semantic else 5)
                edges = [left, right, page.width - 45]
                if not semantic:
                    # 加入第三列边界，逐格读取“简历情况”，不能拿整页文本代替。
                    resume_header = next(item for item in page.search("简历情况", return_chars=False)
                                         if abs(item["top"] - header["top"]) < 1)
                    edges.insert(2, resume_header["x0"] - 5)
                crop = page.crop((left, header["top"] - 6, page.width - 45, page.height - 110))
                tables = crop.extract_tables({"vertical_strategy": "explicit", "horizontal_strategy": "lines",
                                              "explicit_vertical_lines": edges,
                                              "intersection_tolerance": 8})
                # 合并段落自然换行后检查单元格，防止120字被多行切开掩盖超长。
                jd_cells[header["text"]].extend("".join(row[0].split()) for table in tables for row in table
                                                if row[0] and row[0].startswith("扩展职责"))
                if not semantic:
                    # 判定符号识别数据行，排除后续图例及其他章节内容。
                    resume_cells.extend("".join((row[-1] or "").split()) for table in tables for row in table
                                        if len(row) == 3 and (row[1] or "").strip() in {"✓", "✗", "?", "—"})
            # 排除固定页脚后再检查尾行，页脚不能掩盖孤立章节标题。
            body = page.crop((0, 0, page.width, page.height - 110)).extract_text() or ""
            lines = body.splitlines()
            assert not lines or lines[-1] not in SECTION_NAMES, "章节标题孤立在页尾"
        # 六条长职责在两张表各出现一次；75mm左列按区间也应取80字。
        assert all(len(cells) == 6 for cells in jd_cells.values()), jd_cells
        assert all(len(cell) <= 41 and not re.search(r"[\u4e00-\u9fff]{46,}", cell)
                   for cell in jd_cells["JD 职责"]), jd_cells
        assert all(len(cell) <= 81 for cell in jd_cells["JD 要求"]), jd_cells
        # B对比表两条待确认加九条职责，共11格；每格必须满足85字验收上限。
        assert len(resume_cells) == 11 and all(len(cell) <= 85 for cell in resume_cells), resume_cells


def _empty_and_metadata(artifacts: list[Artifact]) -> None:
    """检查空表、标题缺省、零分和长审计字段；额外PDF仅存在于内存。"""
    job = JobInfo(title=None, company=None, required_years_min=None, required_years_max=None,
                  education=None, must_have=[], nice_to_have=[], responsibilities=[])
    report = artifacts[0].report.model_copy(deep=True)
    # 深拷贝受控报告，边界样本不会污染交付的A/B/C三份PDF。
    report.deductions, report.mismatches, report.semantic_items = [], [], []
    report.highlights, report.keywords, report.missing, report.suggestions = [], [], [], []
    # 故意使总分与分项不相加，确认渲染器不会重算业务数据或改写档位。
    report.score.total, report.verdict = 9, "上游给定档位"
    report.score.breakdown = {"hard": 0, "semantic": 0, "llm": 0}
    report.meta.update(model="审计模型" * 45, prompt_version="版本" * 75,
                       cost_yuan=.00112345, elapsed_ms=17)
    with pdfplumber.open(BytesIO(render_report(report, job))) as document:
        # 长审计压缩正文空间，额外验证页脚自动换行时仍能正常生成。
        text = "\n".join(page.extract_text() or "" for page in document.pages)
        assert all(value in text for value in ("无扣分项", "无可比项", "岗位：未识别", "上游给定档位",
                                               "0.00112345", "耗时 17ms", "/ 100"))
        # 混合字号的文字提取顺序可能不同，以40pt字符直接定位总分。
        assert "".join(char["text"] for char in document.pages[0].chars if char["size"] >= 39).strip() == "9"
        assert not any(value in text for value in ("姓名", "最近公司", "工作年限"))
    # 字体失效必须显式抛出，不能吞错后生成无法阅读的PDF。
    with patch("app.pdf_font.get_cjk_font", side_effect=RuntimeError("受控字体不可用")):
        # 该patch只覆盖边界测试，交付报告已经使用真实字体成功生成。
        try:
            render_report(report, job)
        except RuntimeError as exc:
            # 只接受原字体异常，不接受回退生成结果或者无关错误。
            assert str(exc) == "受控字体不可用"
        else:
            raise AssertionError("字体错误被静默吞掉")


def _documentation(artifacts: list[Artifact]) -> None:
    """用tokenize/AST检查新增及必要适配代码的注释和完整docstring。"""
    for relative in ("app/report_pdf.py", "tools/check_report_pdf.py", "app/schemas_match.py", "app/match_report.py"):
        # 将字段透传的必要改动也纳入规范验收，不仅检查新增文件。
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        comments = {token.start[0] for token in tokenize.generate_tokens(StringIO(source).readline)
                    if token.type == tokenize.COMMENT and not token.line[:token.start[1]].strip()}
        tree = ast.parse(source)
        # 独立注释行计数，空行也进入分母；docstring不冒充注释率。
        # 模块、类、公开/私有方法和内嵌回调都纳入文档率分母。
        nodes = [tree] + [node for node in ast.walk(tree)
                          if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
        assert len(comments) / len(source.splitlines()) >= .15, f"{relative}注释不足15%"
        assert all(ast.get_docstring(node) for node in nodes), f"{relative}docstring不足100%"
        if relative == "app/report_pdf.py":
            # 字符拼接避免把被禁名称引入到渲染器自身。
            assert "Hel" + "vetica" not in source and "drawString" not in source
        print(f"  规范：{relative}；注释={len(comments)}/{len(source.splitlines())}；docstring={len(nodes)}/{len(nodes)}")


def main() -> int:
    """在模型与网络均被封锁的上下文运行所有检查，失败返回非零退出码。"""
    print("=== S7a PDF 报告离线验收（零模型、零成本） ===")
    with ExitStack() as stack:
        # ExitStack保证异常退出也会恢复patch，不污染调用者的网络和模型状态。
        # socket双重封锁，避免数据类导入或将来改动意外发出请求。
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo"):
            stack.enter_context(patch(target, side_effect=AssertionError("离线验收禁止联网")))
        # 同时拦住评分入口与本地模型加载；调用即失败，不返回伪成功。
        for target in ("app.scoring_semantic.score_semantic", "app.scoring_semantic._get_model",
                       "app.scoring_semantic._get_reranker", "app.scoring_rules.score_hard",
                       "app.scoring_llm.score_llm", "app.scoring_llm.call_with_fallback_async",
                       "fastembed.TextEmbedding.__init__", "fastembed.rerank.cross_encoder.TextCrossEncoder.__init__"):
            stack.enter_context(patch(target, side_effect=AssertionError("离线验收禁止调用模型或评分")))
        try:
            job, reports = _reports()
            artifacts = _generate(job, reports)
        except Exception as exc:
            # 生成失败立即退出；不存在的文件不能被内容断言计作通过。
            print(f"生成失败：{type(exc).__name__}：{exc}")
            print("通过 0/9；成本 0 元")
            return 1
        checks = [("① 三份PDF成功且大于8KB", _files), ("② 总分、章节与免责可读回；扣分负号/零值横线", _required_text),
                  ("③ unknown逐行判定为?，没有✗", _unknown), ("④ 每页完整免责及审计字段", _audit),
                  ("⑤ failed红底/needs_review黄底告警", _alerts), ("⑥ 中文与四态符号可读回", _chinese),
                  ("列宽区间分档、右列≤85字、多页表头与A4边界", _layout),
                  ("空表、缺省、原数值、长审计与字体失败", _empty_and_metadata),
                  ("注释≥15%、docstring100%与渲染红线", _documentation)]
        passed = 0
        # 前六组逐项对应任务书①至⑥；后三组补充排版、空值和源码规范。
        # 各组单独计数；失败继续跑其余断言，提供完整诊断而非只报第一处。
        for label, check in checks:
            try:
                check(artifacts)
            except Exception as exc:
                print(f"FAIL {label}：{type(exc).__name__}：{exc}")
            else:
                passed += 1
                print(f"PASS {label}")
        # 最后一行同时交付通过数、成本、三份绝对路径及各自真实页数。
        paths = "；".join(f"{item.case}：{item.path}（{item.pages}页）" for item in artifacts)
        # 任意组失败必须返回1，供命令行或后续CI直接判定验收失败。
        print(f"通过 {passed}/{len(checks)}；成本 0 元；{paths}")
        return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    # Windows终端按UTF-8输出四态符号，避免GBK编码错误中断验收。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    raise SystemExit(main())
