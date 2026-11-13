"""HTTP协议适配：保留七个旧接口，匹配业务全部委托独立编排模块。"""

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.responses import FileResponse, Response
import json
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Optional
from urllib.parse import quote
from uuid import uuid4
from starlette.concurrency import run_in_threadpool
from starlette.formparsers import MultiPartException
from time import monotonic

from app.schemas import ResumeInfo
from app.fallback import call_with_fallback_async
from app.agent import AgentResult, parse_resume_agent
from app.file_parser import FileParseError, extract_text
from app import match_pipeline, report_store
from app import revise_pipeline
from app.job_profile import ProfileNotConfirmed, infer_profile, profile_hash as make_profile_hash
from app.job_parser import JobParseError
from app.match_upload import MAX_UPLOAD_BYTES, MemoryUploadRoute
from app.match_upload import _MemoryParser, _UploadTooLarge
from app.report_pdf import render_report
from app.schemas_match import MatchReport

from app.prompts import SYSTEM_RESUME,PROMPT_VERSION
# 运行入口、业务类型和测试替身均使用同一app包，避免类型身份重复。
# 上传大小常量与内存解析器共用，防止传输层和端点出现两个不同上限。

app = FastAPI(
    title="FDE 学习项目",
    description="从 Go/PHP 转 Python + FastAPI 的第一个服务",
    version="0.0.4",
)
# 模块导入时只清理一次过期报告；后续保存、读取也分别执行惰性清理。
report_store.purge_expired()

# 模拟内存库（真实场景换成数据库）
DB: dict[int, dict] = {}
# 旧演示库与PDF缓存相互独立，新匹配接口不把上传原文写入这个字典。

class ResumeIn(BaseModel):
    """保留旧接口的文本请求契约。"""
    # 请求体：上传简历原文
    text: str = Field(...,min_length=1,description="简历原文")
    # 该旧字段约束保持不变，新匹配请求另用multipart表单。

class ResumeOut(BaseModel):
    """保留旧接口结构化简历响应字段。"""
    id: int
    # 旧模型字段保持可空，包导入整理不改变既有JSON契约。
    name: Optional[str] = None
    years: Optional[float] = Field(None,ge=0,le=60)
    skills: list[str] = []

class UsageOut(BaseModel):
    """保留旧LLM接口的实际token和成本响应。"""
    input_tokens: int
    # 沿用旧UsageMeter的计量口径，协议层不估算token费用。
    output_tokens: int
    cost_yuan: float

class ResumeLLMOut(ResumeOut):
    """旧LLM接口同时返回简历和调用计量。"""
    usage: UsageOut
    # 旧接口调用者仍可通过usage读取费用，不需要适配新匹配响应。


class ResumeUploadOut(AgentResult):
    """旧上传接口保留Agent结果和文件排查字段。"""
    # 不修改 Day 11 的 AgentResult，只在上传响应上附加排查信息。
    file_name: str
    text_length: int


def fake_llm_extract(text: str) -> dict :
    """沿用旧的纯规则演示提取，不参与新匹配评分。"""
    # 模拟LLM提取
    known = ["Go","PHP","MySQL","Redis","Python","Docker","Linux"]
    skills = [s for s in known if s.lower() in text.lower()]
    # 演示提取保持原始词表和顺序，不借导入整理修改旧逻辑。
    return {"name":None,"years":None,"skills":skills}

@app.get("/",tags=["健康检查"])
async def health() -> dict:
    """返回原有健康检查，供本地服务和验收探活。"""
    return {"message":"服务已启动","docs":"/docs","ui":"/ui","revise":"/ui"}
    # 健康检查不访问模型，模型依赖故障时仍可用于确认进程可用。


@app.get("/ui", response_class=FileResponse, tags=["网页"])
async def match_ui() -> FileResponse:
    """返回本地单文件评分网页，仅复用已有匹配与报告下载接口。"""
    # 相对模块定位静态文件，不依赖启动目录或机器绝对路径。
    return FileResponse(Path(__file__).parent / "static" / "index.html", media_type="text/html")


@app.post("/resume",response_model=ResumeOut,tags=["简历"])
async def create_resume(data: ResumeIn) -> ResumeOut:
    """沿用旧演示接口的内存入库行为。"""
    info = fake_llm_extract(data.text)
    # 主键生成与入库行为沿用旧实现，支持原来的列表和详情接口。
    rid = len(DB) + 1
    DB[rid] = info
    return ResumeOut(id=rid,**info)


@app.post(
    "/resume/llm",
    response_model=ResumeLLMOut,
    tags=["简历"],
    summary="使用 LLM 解析简历",
)
async def create_resume_with_llm(data: ResumeIn) -> ResumeLLMOut:
    """保留旧LLM抽取入口、费用字段和异常状态码。"""
    # 复用既有降级链；该旧路由不参与S7b的六阶段编排。
    try:
        text, meter = await call_with_fallback_async(data.text,system=SYSTEM_RESUME)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="AI 服务暂时不可用，请稍后重试",
        ) from exc

    try:
        info = ResumeInfo.model_validate_json(text)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail="简历解析失败，请检查内容",
        ) from exc

    rid = len(DB) + 1
    # 只有通过结构校验的字段才进入旧库，原始模型响应不会入库。
    DB[rid] = info.model_dump()
    usage = UsageOut(
        # 原费用对象只提供已发生usage，不以请求字数推算费用。
        input_tokens=meter.prompt_tokens,
        output_tokens=meter.completion_tokens,
        cost_yuan=meter.cost(),
    )
    return ResumeLLMOut(id=rid, **DB[rid], usage=usage)


@app.post("/resume/agent",response_model=AgentResult,tags=["简历"])
async def create_resume_with_agent(data: ResumeIn) -> AgentResult:
    """保留旧Agent入口，将依赖故障翻译为503。"""
    try:
        result = await parse_resume_agent(data.text)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="AI 服务暂时不可用，请稍后重试",
        ) from exc

    if len(result.steps) == 0:
        # 原接口把没有执行记录视为内部故障，此处保持既有语义。
        raise HTTPException(status_code=500)
    return result


@app.post(
    "/resume/upload",
    response_model=ResumeUploadOut,
    tags=["简历"],
    summary="上传 Word/PDF/TXT 简历并使用 Agent 解析",
)
async def upload_resume(file: UploadFile = File(...)) -> ResumeUploadOut:
    """保留旧文件上传入口及已有413/422/503语义。"""
    filename = file.filename or ""
    # 七个旧端点的返回结构和状态码不因S7b引入新语义。
    try:
        # 多读 1 字节即可判断是否超限，避免把超大文件完整读进内存。
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail="文件超过 10MB 上传上限",
            )

        try:
            text = extract_text(filename, raw)
        except FileParseError as exc:
            # 后缀、损坏、空文件和无文本层扫描件都属于客户端输入问题。
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        try:
            result = await parse_resume_agent(text)
        except RuntimeError as exc:
            raise HTTPException(
                status_code=503,
                detail="AI 服务暂时不可用，请稍后重试",
            ) from exc

        if result.status == "failed":
            # 该旧路由原有failed到503映射继续保留。
            raise HTTPException(
                status_code=503,
                detail="AI 服务暂时不可用，请稍后重试",
            )
        if not result.steps:
            # 即使有结构化数据，缺少执行记录仍按旧契约返回内部错误。
            raise HTTPException(status_code=500)

        return ResumeUploadOut(
            # 附加的只有文件名和长度，不把文件字节加入JSON响应。
            **result.model_dump(),
            file_name=filename,
            text_length=len(text),
        )
    finally:
        # 旧文件路由也保证成功、失败都关闭句柄。
        await file.close()


@app.get("/resume",tags=["简历"])
async def list_resume(page:int = 1,size :int = 10)->dict:
    """按旧契约分页返回演示内存库内容。"""
    # 分页列出
    items = [{"id":k,**v} for k,v in DB.items()]
    # 旧列表只读原DB，新报告由独立UUID接口下载。
    start = (page-1)*size
    return {"page":page,"size":size,"total":len(items),"items":items[start:start+size]}


@app.get("/resume/{rid}",response_model=ResumeOut,tags=["简历"])
async def get_resume(rid:int)->ResumeOut:
    """按旧契约读取已存简历，不存在返回404。"""
    if rid not in DB:
        # 详情404与新报告404是独立资源，不做跨缓存查找。
        raise HTTPException(status_code=404,detail="简历不存在")
    return ResumeOut(id=rid,**DB[rid])


# 独立路由类只负责匹配上传的内存策略，七个旧端点保持原路由行为。
match_routes = APIRouter(route_class=MemoryUploadRoute)


class MatchResponse(BaseModel):
    """S7b成功响应：UUID标识加完整的S6报告契约。"""

    report_id: str
    # 直接嵌入S6契约，不在HTTP层重新定义或压平评分字段。
    report: MatchReport
    # MatchReport新增字段由统一模型自然透出，API无需逐字段拼装结果。


@match_routes.post("/match/score", response_model=MatchResponse, tags=["匹配"])
async def match_score(file: UploadFile = File(...), jd_text: str = Form("")) -> MatchResponse:
    """校验上传、委托完整编排、渲染并缓存PDF，按契约翻译异常。"""
    try:
        # 最多读取上限加一字节；multipart层已确保整个上传过程不写临时磁盘。
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "文件超过10MB上传上限")
        # 空表单字段与缺字段均进入422，不使用400表示内容校验失败。
        if len(jd_text.strip()) < 20:
            raise HTTPException(422, "岗位描述不能为空且至少20字")
        # HTTP层没有解析、评分或汇总算法，全部委托同一个可复用入口。
        outcome = await match_pipeline.run_match(file.filename or "", raw, jd_text)
        # PDF渲染放入工作线程，HTTP事件循环仍可响应统计和下载请求。
        pdf = await run_in_threadpool(render_report, outcome.report, outcome.job, resume_name=outcome.resume_name)
        # ID只由服务端生成；上传文件名永远不会用于构造缓存路径。
        report_id = uuid4().hex
        report_store.save_report(report_id, pdf, filename=outcome.report.meta["report_filename"])
        # 只在最终PDF成功落盘后交付ID，避免产生可见但不可下载的报告。
        return MatchResponse(report_id=report_id, report=outcome.report)
    except HTTPException:
        # 已明确分类的413/422保持原状态，不被兜底异常覆盖。
        raise
    except (FileParseError, JobParseError) as exc:
        # 输入内容错误不泄露模型校验详情，也不会混成依赖服务错误。
        raise HTTPException(422, "简历或岗位描述无法解析，请检查输入") from exc
    except match_pipeline.ModelUnavailableError as exc:
        # 模型耗尽必须503；此分支在内部错误兜底之前。
        raise HTTPException(503, "AI服务暂时不可用，请稍后重试") from exc
    except Exception as exc:
        # 不把原始异常文本或简历内容回显给客户端，也不写入日志。
        raise HTTPException(500, "匹配报告生成失败") from exc
    finally:
        # 包含PDF失败和模型异常，均释放上传句柄。
        await file.close()


app.include_router(match_routes)
# 路由注册只发生于模块加载阶段，不在请求处理过程中动态增删路由。


@app.get("/match/report/{report_id}", tags=["匹配"])
async def match_report_download(report_id: str) -> Response:
    """下载有效PDF；存储层校验ID和TTL后才可能读取文件。"""
    pdf = report_store.load_report(report_id)
    # load_report先校验ID；路由从不使用report_id自行拼接磁盘路径。
    if pdf is None:
        # 不存在、过期和非法ID使用同一个404，避免泄露磁盘结构。
        raise HTTPException(404, "报告不存在或已过期")
    # RFC5987只编码下载名，不把中文直接放入HTTP的Latin-1头部。
    filename = report_store.download_filename(report_id, pdf)
    # 原始缓存路径使用UUID，下载名只出现在响应头，不影响文件读取位置。
    disposition = "attachment; filename*=UTF-8''" + quote(filename, safe="")
    # Response使用内存PDF字节，不把本机绝对路径发送给客户端。
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": disposition})


@app.get("/match/stats", tags=["匹配"])
async def match_stats() -> dict:
    """返回缓存数量和30分钟TTL，不包含用户材料。"""
    return report_store.stats()
    # 统计返回固定两个字段，客户端无法通过它枚举候选人的个人信息。


# 修改工作区的原始材料仅缓存在进程内，TTL 到期后从内存删除，不创建原文侧车文件。
_review_sessions: dict[str, tuple[float, revise_pipeline.ReviseOutcome, bytes, str]] = {}


def _expire_reviews() -> None:
    """惰性释放超过统一 TTL 的修改会话和上传字节，不更新会话创建时间。"""
    # 单调时钟不受系统校时影响，刷新下载或统计不会延长原件驻留时间。
    now = monotonic()
    for identifier, session in list(_review_sessions.items()):
        if session[0] <= now:
            _review_sessions.pop(identifier, None)


class _ReviseRoute(APIRoute):
    """复用内存解析器，仅修改路由允许五个表单字段，旧评分上传策略不变。"""

    def get_route_handler(self):
        """在参数解析前限制文件大小，并把本组协议错误统一翻译为中文 422。"""
        original = super().get_route_handler()

        async def handle(request: Request):
            """确保超限、坏表单和参数错误均不会落盘或回显请求材料。"""
            # 每个请求拥有独立解析器和缓冲，异常时也在 finally 关闭。
            form = None
            try:
                if request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
                    try:
                        # 五个文本字段只适用于修改接口，旧评分路由仍只允许 JD 一个字段。
                        form = await _MemoryParser(request.headers, request.stream(), max_files=1, max_fields=5).parse()
                    except _UploadTooLarge as exc:
                        raise HTTPException(413, "文件超过10MB上传上限") from exc
                    except (MultiPartException, ValueError) as exc:
                        raise HTTPException(422, "上传表单格式无效") from exc
                    # FastAPI 复用已解析表单；不运行会切换到磁盘的默认上传路径。
                    request._form = form
                return await original(request)
            except RequestValidationError as exc:
                # 默认校验响应可能携带 input；修改接口只返回中文提示，不回显材料。
                raise HTTPException(422, "请求参数无效，请检查文件、岗位信息或勾选条目") from exc
            except HTTPException:
                raise
            except Exception as exc:
                # 下载、统计等只读路由的内部错误也须收口，避免服务日志出现裸栈。
                raise HTTPException(500, "修改服务暂时无法处理请求，请稍后重试") from exc
            finally:
                if form is not None:
                    await form.close()

        return handle


revise_routes = APIRouter(route_class=_ReviseRoute)


class ReviseApplyRequest(BaseModel):
    """修改请求只接收服务端报告 ID 和意见编号，不接受客户端传入改写正文。"""

    # 拒绝额外 before/after 参数，客户端无权把任意改写正文塞进应用请求。
    model_config = {"extra": "forbid"}
    review_id: str
    # None 默认自动选择；空列表表示用户没有选择，必须明确返回 422。
    selected_ids: list[str] | None = None


def _revise_stats(outcome: revise_pipeline.ReviseOutcome) -> dict:
    """投影已分析的统计数字，不把诊断对象、抽取结果或完整原文发给前端。"""
    # 严重度只统计现成意见，不在协议层重新评估问题等级。
    counts = {key: sum(issue.severity == key for issue in outcome.report.issues)
              for key in ("high", "medium", "low")}
    return {**counts, "missing": len(outcome.diagnosis.missing_keywords),
            "cost_yuan": outcome.report.meta.get("cost_yuan", 0),
            "elapsed_ms": outcome.report.meta.get("elapsed_ms", 0),
            "resume_chars": outcome.report.meta.get("resume_chars", 0),
            "jd_chars": outcome.report.meta.get("jd_chars", 0)}


def _public_revise(outcome: revise_pipeline.ReviseOutcome, value):
    """仅在展示投影中隐去姓名和原始教育标题，不修改内部意见、定位或改写内容。"""
    name = outcome._resume.name if outcome._resume is not None else None

    def redact(item):
        """递归处理分析证据，保留服务器标识和数字字段原值。"""
        if isinstance(item, dict):
            # ID 必须原样保留；显示脱敏不参与勾选项回链或缓存路径构造。
            return {key: content if key in {"id", "issue_id", "review_id", "auto_applicable_ids", "review_pdf_url", "resume_url"}
                    else redact(content) for key, content in item.items()}
        if isinstance(item, list):
            return [redact(content) for content in item]
        if isinstance(item, str):
            # 只投影响应副本，M5 始终读取会话中未经脱敏的真实原句。
            return (item.replace(name, "候选人") if name else item).replace("教育背景", "教育信息")
        return item

    return redact(value)


@revise_routes.get("/revise/profile", tags=["修改意见"])
async def revise_profile(job_title: str = "") -> dict:
    """零模型生成待确认画像和稳定指纹，无效岗位名返回 422。"""
    # M2 是纯模板函数，该路由既不抽取简历也不调用远端模型。
    try:
        profile = infer_profile(job_title)
        return {"profile": profile.model_dump(), "profile_hash": make_profile_hash(profile)}
    except ValueError as exc:
        # 画像输入校验只处理岗位名称，消息来自 M0 的固定中文契约。
        raise HTTPException(422, str(exc)) from exc


@revise_routes.post("/revise/review", tags=["修改意见"])
async def revise_review(file: UploadFile = File(...), job_title: str = Form(""), jd_text: str = Form(""),
                        profile_hash: str | None = Form(None), profile_edits: str | None = Form(None)) -> dict:
    """委托修改编排，保存最终意见 PDF，上传字节只留在限时内存会话中。"""
    # 上传解析器与此处读上限双重限制，不将超大上传交给业务层。
    try:
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "文件超过10MB上传上限")
        # JSON 字符串只解释为画像编辑对象，永远不作为代码执行。
        edits = None
        if profile_edits is not None:
            try:
                edits = json.loads(profile_edits)
            except (ValueError, TypeError) as exc:
                raise HTTPException(422, "岗位画像编辑须为有效 JSON 对象") from exc
            if not isinstance(edits, dict):
                raise HTTPException(422, "岗位画像编辑须为 JSON 对象")
        # 真实 JD、画像确认、诊断和预估全部委托同一个可独立调用的编排入口。
        outcome = await revise_pipeline.run_review(filename=file.filename or "", raw=raw,
            job_title=job_title, jd_text=jd_text, profile_hash=profile_hash, profile_edits=edits)
        pdf = await revise_pipeline.review_pdf(outcome)
        # 所有下载标识由服务端生成，与原始文件名和候选人姓名无关。
        identifier = uuid4().hex
        await run_in_threadpool(report_store.save_artifact, identifier, "review", pdf,
                               filename=f"修改意见_{Path(outcome.resume_name).stem}.pdf")
        # 只有意见 PDF 已成功保存才创建可见会话，原始字节仍只在内存。
        _expire_reviews()
        _review_sessions[identifier] = (monotonic() + report_store.TTL_MINUTES * 60, outcome, raw, outcome.resume_name)
        # 被拒的模型候选属于内部审计，不在响应中泄漏未经筛选的模型文本。
        public_report = outcome.report.model_dump(exclude={"rejected_issues"})
        public_report["rejected_issues"] = []
        # 显式选择响应字段，禁止自动序列化携带原文的内部 Outcome。
        return _public_revise(outcome, {"review_id": identifier, "report": public_report, "profile": outcome.profile.model_dump(),
                "mode": outcome.profile.source, "stats": _revise_stats(outcome),
                "delta": outcome.delta.model_dump() if outcome.delta else None})
    except HTTPException:
        raise
    except ProfileNotConfirmed as exc:
        raise HTTPException(422, "请先确认岗位画像，再生成修改意见") from exc
    except match_pipeline.ModelUnavailableError as exc:
        raise HTTPException(503, "AI 服务暂时不可用，请稍后重试") from exc
    # 文件、参数和依赖故障保持不同状态码，异常正文不向客户端透传。
    except FileParseError as exc:
        raise HTTPException(422, "简历文件无法解析；扫描件请先转换为文本版") from exc
    except ValueError as exc:
        raise HTTPException(422, "简历或岗位信息无效，请检查文件类型、JD 长度及画像确认") from exc
    except Exception as exc:
        # 不打印异常对象、栈或模型请求，防止材料从失败日志泄漏。
        raise HTTPException(500, "修改意见报告生成失败，请稍后重试") from exc
    finally:
        await file.close()


@revise_routes.post("/revise/apply", tags=["修改意见"])
async def revise_apply(data: ReviseApplyRequest) -> dict:
    """以缓存原件应用勾选项，只有两份最终文件均保存成功后才返回下载链接。"""
    # 必须先清理过期会话，不能因为内存仍有字节就允许过期报告继续应用。
    _expire_reviews()
    if not report_store._valid(data.review_id) or data.review_id not in _review_sessions:
        raise HTTPException(404, "修改意见不存在或已过期，请重新分析")
    if data.selected_ids == []:
        raise HTTPException(422, "没有选择可自动修改的条目")
    _, original, raw, filename = _review_sessions[data.review_id]
    # 只允许当前报告的意见编号，不能引用其它报告或提交自造条目。
    known = {issue.id for issue in original.report.issues}
    if data.selected_ids is not None and set(data.selected_ids) - known:
        raise HTTPException(422, "勾选条目无效，请重新选择")
    try:
        # 每次操作使用深拷贝，并行或重复申请都从同一原件出发，不覆盖原始分析。
        outcome = original.model_copy(deep=True)
        result = await revise_pipeline.run_apply(outcome=outcome, raw=raw, filename=filename,
                                                 selected_ids=data.selected_ids)
        if not result.applied:
            raise HTTPException(422, "没有可自动修改的条目，请按未采用原因核实真实经历")
        # 两份文件各有独立产物 ID，使 load_artifact(id) 的返回始终唯一。
        review_id, resume_id = uuid4().hex, uuid4().hex
        await run_in_threadpool(report_store.save_artifact, review_id, "review", result.review_pdf,
                               filename=f"修改意见_{Path(filename).stem}.pdf")
        await run_in_threadpool(report_store.save_artifact, resume_id, "resume", result.resume_bytes,
                               filename=f"{Path(filename).stem}_修改版.{result.resume_ext}")
        # 响应仅含必要的逐条证据与 delta，不返回 Outcome、原始文件或解析结构。
        return _public_revise(outcome, {"review_pdf_url": f"/revise/file/{review_id}/review",
                "resume_url": f"/revise/file/{resume_id}/resume", "resume_ext": result.resume_ext,
                "applied": result.applied, "skipped": result.skipped,
                "delta": outcome.delta.model_dump() if outcome.delta else None})
    except HTTPException:
        raise
    except match_pipeline.ModelUnavailableError as exc:
        raise HTTPException(503, "AI 服务暂时不可用，请稍后重试") from exc
    except ValueError as exc:
        raise HTTPException(422, "所选修改无法应用，请核对原文件与修改意见") from exc
    except Exception as exc:
        raise HTTPException(500, "修改版文件生成失败，请稍后重试") from exc


@revise_routes.get("/revise/file/{artifact_id}/{kind}", tags=["修改意见"])
async def revise_file(artifact_id: str, kind: str) -> Response:
    """仅下载有效 ID 对应的实际 kind，类型与展示名均从受控存储读取。"""
    # kind 和 ID 都在读文件之前验证，合法 ID 也不能下载另一类型的产物。
    if kind not in report_store.ARTIFACT_KINDS or report_store.artifact_kind(artifact_id) != kind:
        raise HTTPException(404, "文件不存在或已过期")
    artifact = await run_in_threadpool(report_store.load_artifact, artifact_id)
    if artifact is None:
        raise HTTPException(404, "文件不存在或已过期")
    data, filename = artifact
    # 展示名的扩展名已经由存储白名单约束，下载头使用 RFC5987 编码。
    media = "application/pdf" if filename.lower().endswith(".pdf") else (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    disposition = "attachment; filename*=UTF-8''" + quote(filename, safe="")
    return Response(data, media_type=media, headers={"Content-Disposition": disposition})


@revise_routes.get("/revise/stats", tags=["修改意见"])
async def revise_stats() -> dict:
    """返回多产出统计并清理过期原件会话，不枚举任何用户或文档信息。"""
    # 统计路径也释放原件缓存，避免只有分析请求才触发会话 TTL。
    _expire_reviews()
    return await run_in_threadpool(report_store.stats_artifacts)


app.include_router(revise_routes)
# 新路由注册独立于原评分 router，不修改旧接口的表单字段数或异常处理。


if __name__ == "__main__":
    # 直接运行与uvicorn包入口同为本机监听，默认不对公网开放。
    import uvicorn
    print("服务启动 -> http://127.0.0.1:8000/docs")
    uvicorn.run(app,host="127.0.0.1",port=8000)

