"""按S7b依次串联S1至S6，原始简历及上传字节仅存在于本次调用内存。"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from time import perf_counter

import httpx
import openai
from starlette.concurrency import run_in_threadpool

from app import job_parser, match_report, resume_parser, scoring_llm, scoring_rules, scoring_semantic
from app.file_parser import FileParseError
from app.job_parser import JobParseError
from app.schemas_match import JobInfo, MatchReport


class ModelUnavailableError(RuntimeError):
    """模型调用或本地模型加载不可用，由HTTP层翻译为503。"""
    # 单独类型避免PDF渲染RuntimeError也被误翻译成模型故障。


@dataclass
class MatchOutcome:
    """交付报告、岗位及文件名，不把简历原文带到持久化层。"""

    report: MatchReport
    # Outcome只向HTTP层交付渲染必需字段，原文在run_match退出后释放引用。
    job: JobInfo
    resume_name: str


def report_filename(resume_name: str, title: str | None, jd_text: str, generated_at: str) -> str:
    """按PRD文件命名规则生成下载名，清理路径符号和控制字符。"""
    stem = Path(resume_name.replace("\\", "/")).stem or "简历"
    # 无岗位标题时只用JD前八字，不把完整JD存入报告元数据。
    # 文件名片段不是路径，避免浏览器把原始上传路径带入下载位置。
    parts = [re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value) for value in (stem, title or jd_text.strip()[:8])]
    stamp = datetime.fromisoformat(generated_at).astimezone().strftime("%Y%m%d-%H%M")
    return f"简历评分_{parts[0]}_{parts[1]}_{stamp}.pdf"


async def run_match(filename: str, raw: bytes, jd_text: str) -> MatchOutcome:
    """上传文件依次执行六阶段，失败立即停止，模型依赖异常转专用异常。"""
    started = perf_counter()
    # ① 文件解析异常保持原类型；模型异常不混同于扫描件或坏文件。
    try:
        resume = await resume_parser.parse_resume_file(filename, raw)
    except (RuntimeError, openai.OpenAIError, httpx.HTTPError) as exc:
        raise ModelUnavailableError("简历抽取模型不可用") from exc
    if resume.data is None or resume.status == "failed":
        # 简历没有可用结构时立即停止，JD抽取及后续评分均不得继续。
        raise FileParseError("简历结构解析失败，请检查文件内容")
    # ② S1把模型异常收成failed结果，使用其明确的失败原因前缀恢复503语义。
    try:
        job = await job_parser.parse_and_guard(jd_text)
    except (RuntimeError, openai.OpenAIError, httpx.HTTPError) as exc:
        raise ModelUnavailableError("岗位抽取模型不可用") from exc
    if job.data is None or job.status == "failed":
        if any(reason.startswith("JD模型调用失败:") for reason in job.reasons):
            # 该前缀来自S1固定失败契约，格式错误与网络失败仍可明确区分。
            raise ModelUnavailableError("岗位抽取模型不可用")
        raise JobParseError("岗位描述解析失败，请检查JD内容", job.cost_yuan)
    # ③ 纯规则评分原样调用，不在编排层增加、减少或覆盖分数。
    hard = scoring_rules.score_hard(resume.data, job.data, resume.raw_text)
    if getattr(hard, "status", "ok") == "failed":
        raise ValueError("硬性评分失败")
    # ④ 本地推理使用工作线程，避免首次模型加载阻塞事件循环。
    try:
        sem = await run_in_threadpool(scoring_semantic.score_semantic, resume.raw_text, job.data)
    except (RuntimeError, OSError, httpx.HTTPError) as exc:
        raise ModelUnavailableError("语义模型不可用") from exc
    if sem.status == "failed":
        # 失败语义结果不能流入LLM上下文并伪装为正常的零分。
        raise ModelUnavailableError("语义模型不可用")
    # ⑤ S5保留零分failed结果；网络调用失败需要503，坏评分结构属于内部错误。
    try:
        llm = await scoring_llm.score_llm(resume.data, job.data, resume.raw_text, hard, sem)
    except (RuntimeError, openai.OpenAIError, httpx.HTTPError) as exc:
        raise ModelUnavailableError("复核模型不可用") from exc
    if llm.status == "failed":
        if any(reason.startswith("LLM调用失败:") for reason in llm.reasons):
            raise ModelUnavailableError("复核模型不可用")
        raise ValueError("复核结果校验失败")
    # ⑥ S6唯一负责总分、档位和证据；这里只补充完整链路的审计计量。
    report = match_report.build_report(resume=resume.data, job=job.data, resume_text=resume.raw_text,
                                       jd_text=jd_text, hard=hard, sem=sem, llm=llm,
                                       meta={"elapsed_ms": round((perf_counter() - started) * 1000)})
    # 抽取步骤也产生费用，不能只把S5费用冒充完整接口成本。
    report.meta["cost_yuan"] += resume.cost_yuan + job.cost_yuan
    # 此处只合计已发生费用；三项评分、总分和档位始终由S6负责。
    for name, stage in (("resume", resume), ("job", job)):
        if stage.status == "needs_review":
            # 抽取护栏也影响完整报告状态，不能在成功评分后悄悄丢掉。
            report.status = "needs_review"
            report.reasons.extend(f"{name}：{reason}" for reason in stage.reasons)
    # 仅下载名进入元数据，原始简历、上传字节及完整JD不进入缓存字段。
    report.meta["report_filename"] = report_filename(filename, job.data.title, jd_text, report.meta["generated_at"])
    return MatchOutcome(report, job.data, filename)
