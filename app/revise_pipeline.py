"""M7 修改编排：复用 M0～M6，原始文件和解析结果仅在内存中流转。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath
from time import perf_counter

import httpx
import openai
from pydantic import BaseModel, Field, PrivateAttr
from starlette.concurrency import run_in_threadpool

from app import guard_revision, job_parser, resume_parser, resume_rewrite, revision_llm, score_delta, scoring_llm
from app.job_profile import PROFILE_VERSION, ProfileNotConfirmed, confirm_profile, infer_profile
from app.match_pipeline import ModelUnavailableError
from app.resume_diagnose import diagnose
from app.revision_pdf import render_review_pdf
from app.schemas import ResumeInfo
from app.schemas_match import JobInfo
from app.schemas_revision import AdaptedText, DiagnosisReport, JobProfile, RevisionReport, ScoreDelta
from app.scoring_rules import HardScore, score_hard
from app.scoring_semantic import score_semantic


class ReviseOutcome(BaseModel):
    """内部编排结果；原文排除序列化，HTTP 层只能投影报告及审计字段。"""

    # 模型与诊断保持原契约，编排结果本身不是 HTTP 响应类型。
    report: RevisionReport
    diagnosis: DiagnosisReport
    delta: ScoreDelta | None
    profile: JobProfile
    resume_name: str
    # 画像模式保留 job=None，使 M3 按已确认画像声明来源。
    job: JobInfo | None
    resume_text: str = Field(exclude=True, repr=False)
    meta: dict
    # 抽取模型和硬分仅供本次进程后续重算、渲染，不进入响应模型。
    _resume: ResumeInfo | None = PrivateAttr(default=None)
    _hard: HardScore | None = PrivateAttr(default=None)
    # 改前仅评分一次；应用修改和重复预估均沿用同一个基准。
    _before_llm: int = PrivateAttr(default=0)


def _score_job(profile: JobProfile, job: JobInfo | None) -> JobInfo:
    """将已确认画像映射到既有评分输入，不推测学历、年限或公司要求。"""
    # 真实 JD 的已知学历和年限必须完整保留，不能被通用画像覆盖。
    if job is not None:
        return job
    # 推测画像没有学历、年限信息时使用 None，不能伪造零年要求。
    return JobInfo(title=profile.title, company=None, required_years_min=None,
                   required_years_max=None, education=None, must_have=list(profile.must_have),
                   nice_to_have=[], responsibilities=list(profile.responsibilities))


def _jd_items(outcome: ReviseOutcome) -> list[str]:
    """只取本次真实 JD 或已确认画像的必备项与职责，供 M5 雷同闸门使用。"""
    # 不混入岗位标题、加分项或未经用户确认的旁路草案。
    source = outcome.job if outcome.job is not None else outcome.profile
    return [*source.must_have, *source.responsibilities]


def _applied_adaptations(outcome: ReviseOutcome, rows: list[dict]) -> list[AdaptedText]:
    """把 M5 确实写入的记录转回 M6 输入，审计字段复用 M0 的同一核验函数。"""
    adaptations = []
    for row in rows:
        # 只接收 M5 返回的 applied，未采用条目没有回流此列表的路径。
        item = AdaptedText(**row, added_terms=[], kept_numbers=[])
        item.added_terms = guard_revision.new_tech_terms(item.after, outcome.resume_text, "")
        item.added_terms.extend(guard_revision.new_proper_nouns(item.after, outcome.resume_text, ""))
        # 数字审计沿用 M5 同数归一；不在这里执行一次新的改写。
        item.kept_numbers = list(dict.fromkeys(guard_revision._number_tokens(item.after)))
        adaptations.append(item)
    return adaptations


async def _estimate(outcome: ReviseOutcome, adaptations: list[AdaptedText]) -> ScoreDelta:
    """在线程池重算 M6，沿用改前 LLM 原分，缺失时明确披露降级。"""
    # estimate_delta 内部会再次调用语义模型，因此必须整体在线程池执行。
    try:
        result = await run_in_threadpool(score_delta.estimate_delta, resume=outcome._resume,
            job=_score_job(outcome.profile, outcome.job), resume_text=outcome.resume_text,
            adaptations=adaptations, before_llm=outcome._before_llm)
    except (RuntimeError, OSError, httpx.HTTPError) as exc:
        raise ModelUnavailableError("本地语义模型不可用") from exc
    # M6 仍保留 estimated_fields=['llm'] 和 ±2；改后不增加远端评分请求。
    if outcome.report.meta.get("llm_estimated") is True:
        # 降级零分没有可信原分来源，不能继续展示正常的「沿用原分 ±2」。
        result.notes = [note for note in result.notes if not note.startswith("LLM 30 分未重算")]
        result.notes.append("LLM 复核分未取到，改前总分不含 LLM 项；改后预估也不含 LLM 项，请重新评分")
    return result


def _fill_delta(outcome: ReviseOutcome, delta: ScoreDelta) -> None:
    """只回填 M6 已计算的分数，不在编排层增加评分规则或隐藏负差值。"""
    # 保留零变化与负变化，不能为了展示效果另行修改 after_total_estimate。
    outcome.delta = delta
    outcome.report.score_before = delta.before_total
    outcome.report.score_after_estimate = delta.after_total_estimate


async def run_review(*, filename: str, raw: bytes, job_title: str | None, jd_text: str | None,
                     profile_hash: str | None = None, profile_edits: dict | None = None) -> ReviseOutcome:
    """按岗位四组合串联解析、诊断、评分、M3 与 M6，原始材料不落盘。"""
    started = perf_counter()
    title, jd = (job_title or "").strip(), (jd_text or "").strip()
    # 有内容但不足二十字的 JD 必须明确报错，不能悄悄改走画像模式。
    if jd and len(jd) < 20:
        raise ValueError("岗位 JD 至少需要20字，请补充完整岗位要求")
    # 四组合的优先级只由 M0 决定，JD 存在时不采用岗位名推测。
    mode, error = guard_revision.resolve_job_input(title, jd)
    if error:
        raise ValueError(error)
    # 上传名仅用作输入格式与报告显示，实际缓存路径由存储层的 UUID 构造。
    resume_name = PurePosixPath(filename.replace("\\", "/")).name
    if PurePosixPath(resume_name).suffix.lower() not in {".pdf", ".docx"}:
        raise ValueError("修改意见仅支持 PDF 或 Word（.docx）简历")
    # 确认检查先于简历模型调用；草案指纹和编辑规则完全复用 M2。
    job, job_cost, reasons = None, 0.0, []
    if mode == "inferred":
        draft = infer_profile(title)
        if not profile_hash:
            raise ProfileNotConfirmed("请先确认岗位画像，再生成修改意见")
        # 旧指纹、越权字段或错误类型编辑仍由 M2 拒绝，不自动重新确认。
        profile = confirm_profile(draft, profile_hash, profile_edits)
    else:
        try:
            parsed_job = await job_parser.parse_and_guard(jd)
        except (RuntimeError, openai.OpenAIError, httpx.HTTPError) as exc:
            raise ModelUnavailableError("岗位抽取模型不可用") from exc
        # S1 将模型调用失败收为结果，必须恢复为明确的模型依赖故障。
        if parsed_job.data is None or parsed_job.status == "failed":
            if any(reason.startswith("JD模型调用失败:") for reason in parsed_job.reasons):
                raise ModelUnavailableError("岗位抽取模型不可用")
            raise ValueError("岗位描述无法解析，请补充完整岗位要求")
        job, job_cost = parsed_job.data, parsed_job.cost_yuan
        # 仅缺标题时回退到用户岗位名，已抽出的 JD 标题始终优先。
        if not job.title:
            job = job.model_copy(update={"title": title or "未命名岗位"})
        profile = JobProfile(title=job.title, source="user_jd", confirmed=True,
            must_have=list(job.must_have), responsibilities=list(job.responsibilities), note="基于用户提供的真实 JD")
        # 同时有岗位名和 JD 时以 JD 为准；提示不复制完整输入材料。
        if title and title != job.title:
            reasons.append("岗位名称与 JD 标题不同，本次以 JD 解析的岗位为准")
        elif not title:
            reasons.append("本次岗位名称从 JD 提取，建议下次补充岗位名称")
        if parsed_job.status == "needs_review":
            reasons.append("岗位抽取信息需要人工复核")
    # 简历文件解析保留扫描件异常；只把模型依赖异常转换为 503 对应类型。
    try:
        resume = await resume_parser.parse_resume_file(resume_name, raw)
    except (RuntimeError, openai.OpenAIError, httpx.HTTPError) as exc:
        raise ModelUnavailableError("简历抽取模型不可用") from exc
    # 无可信抽取结构时停止链路，不生成一份看起来成功的空报告。
    if resume.data is None or resume.status == "failed":
        raise ModelUnavailableError("简历抽取模型不可用")
    if resume.status == "needs_review":
        reasons.append("简历抽取信息需要人工复核")
    score_job = _score_job(profile, job)
    # 诊断与硬分完全复用既有函数，只改变执行位置，不修改规则。
    diagnosis = await run_in_threadpool(diagnose, resume.raw_text, resume.data, score_job)
    hard = await run_in_threadpool(score_hard, resume.data, score_job, resume.raw_text)
    # 本地语义推理包括首次权重加载，整个调用必须离开 HTTP 事件循环。
    try:
        sem = await run_in_threadpool(score_semantic, resume.raw_text, score_job)
    except (RuntimeError, OSError, httpx.HTTPError) as exc:
        raise ModelUnavailableError("本地语义模型不可用") from exc
    # 本地推理失败同样是依赖不可用，不能伪装为真实语义零分。
    if sem.status == "failed":
        raise ModelUnavailableError("本地语义模型不可用")
    if sem.status == "needs_review":
        reasons.append("本地语义覆盖需要人工复核")
    # D-M7-01：与 /match/score 使用完全相同的改前五项输入，只调用一次。
    before_llm, llm_cost, llm_estimated = 0, 0.0, False
    try:
        llm = await scoring_llm.score_llm(resume.data, score_job, resume.raw_text, hard, sem)
    except Exception:
        # 复核失败不阻断意见生成；不向报告泄漏厂商异常或材料。
        llm_estimated = True
    else:
        # S5 通常以 failed 结果表达失败；已知费用即使失败也不能清零。
        llm_cost = llm.cost_yuan
        llm_estimated = llm.status == "failed"
        if not llm_estimated:
            before_llm = llm.score
            if llm.status == "needs_review":
                reasons.append("LLM 复核需要人工复核")
    if llm_estimated:
        reasons.append("LLM 复核分未取到，改前总分不含 LLM 项")
    # M3 继续只使用本次诊断及两项已有证据，不另行评分。
    try:
        report = await revision_llm.generate_issues(resume.raw_text, resume.data, job, profile,
                                                   diagnosis, hard=hard, sem=sem)
    except (RuntimeError, openai.OpenAIError, httpx.HTTPError) as exc:
        raise ModelUnavailableError("修改意见模型不可用") from exc
    # M3 会吞掉厂商异常并标记 failed，编排层必须把失败传递给接口。
    if report.meta.get("status") == "failed":
        raise ModelUnavailableError("修改意见模型不可用，请稍后重试")
    # 合计两次抽取、改前复核与 M3；单列新增费用以核对单次预算。
    revision_cost = report.meta.get("cost_yuan", 0.0)
    report.meta.update(cost_yuan=revision_cost + resume.cost_yuan + job_cost + llm_cost,
        revision_cost_yuan=revision_cost, resume_cost_yuan=resume.cost_yuan, job_cost_yuan=job_cost,
        llm_cost_yuan=llm_cost, llm_estimated=llm_estimated,
        generated_at=datetime.now(timezone.utc).isoformat(), profile_version=PROFILE_VERSION,
        source=mode, confirmed=profile.confirmed, resume_chars=len(resume.raw_text), jd_chars=len(jd))
    # 聚合原因只用阶段说明，避免把原始请求或供应商错误详情带入报告。
    report.meta["reasons"] = reasons
    if reasons and report.meta.get("status") == "ok":
        report.meta["status"] = "needs_review"
    # 原文被明确排除序列化；原始字节由 HTTP 内存会话单独保存。
    outcome = ReviseOutcome(report=report, diagnosis=diagnosis, delta=None, profile=report.profile,
        resume_name=resume_name, job=job, resume_text=resume.raw_text, meta={})
    outcome._resume, outcome._hard = resume.data, hard
    outcome._before_llm = before_llm
    # 预估候选也先过 M5：只在内存预演，JD 雷同、定位失败或冲突不能贡献虚假提分。
    preview = await run_in_threadpool(resume_rewrite.apply_revisions, rep=report, diagnosis=diagnosis,
        raw=raw, filename=resume_name, job_title=profile.title, resume_text=resume.raw_text,
        hard=hard, jd_items=_jd_items(outcome))
    # 界面可据此灰显不能采用的意见，用户勾选也不能绕过实际应用时的再次检查。
    report.skipped = preview.skipped
    report.meta["auto_applicable_ids"] = [row["issue_id"] for row in preview.applied]
    # 预估不保存预演简历；这里只保留分数和可采用编号。
    _fill_delta(outcome, await _estimate(outcome, _applied_adaptations(outcome, preview.applied)))
    report.meta["elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
    outcome.meta = dict(report.meta)
    return outcome


async def review_pdf(outcome: ReviseOutcome) -> bytes:
    """使用 M4 排版现成分析结果；生成 PDF 也在线程池中完成。"""
    # 每次传入已回填的 delta，初始意见下载也有真实的分项对比和免责声明。
    return await run_in_threadpool(render_review_pdf, outcome.report, outcome.diagnosis,
        job_title=outcome.profile.title, resume_name=outcome.resume_name, hard=outcome._hard, delta=outcome.delta)


async def run_apply(*, outcome: ReviseOutcome, raw: bytes, filename: str,
                    selected_ids: list[str] | None = None) -> resume_rewrite.ApplyOutcome:
    """先由 M5 决定实际替换，再只用 applied 重算 M6 并重排带 delta 的意见报告。"""
    # 选中、缺失项、原句定位、数字和 JD 闸门的先后顺序均由 M5 唯一决定。
    result = await run_in_threadpool(resume_rewrite.apply_revisions, rep=outcome.report,
        diagnosis=outcome.diagnosis, raw=raw, filename=filename, job_title=outcome.profile.title,
        resume_text=outcome.resume_text, hard=outcome._hard, selected_ids=selected_ids,
        jd_items=_jd_items(outcome))
    # 未采用项原样回填；不依据勾选项数量或 M3 候选数伪造成功替换。
    outcome.report.adaptations = _applied_adaptations(outcome, result.applied)
    outcome.report.skipped = result.skipped
    outcome.report.meta.update(rewrite_requested=True, resume_ext=result.resume_ext, rewrite_cost_yuan=0)
    # 用户只选部分条目时按本次实际 applied 重算，不能沿用全选预估。
    _fill_delta(outcome, await _estimate(outcome, outcome.report.adaptations))
    result.review_pdf = await review_pdf(outcome)
    return result
