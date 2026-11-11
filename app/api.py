"""HTTP协议适配：保留七个旧接口，匹配业务全部委托独立编排模块。"""

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Optional
from urllib.parse import quote
from uuid import uuid4
from starlette.concurrency import run_in_threadpool

from app.schemas import ResumeInfo
from app.fallback import call_with_fallback_async
from app.agent import AgentResult, parse_resume_agent
from app.file_parser import FileParseError, extract_text
from app import match_pipeline, report_store
from app.job_parser import JobParseError
from app.match_upload import MAX_UPLOAD_BYTES, MemoryUploadRoute
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
    return {"message":"服务已启动","docs":"/docs","ui":"/ui"}
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


if __name__ == "__main__":
    # 直接运行与uvicorn包入口同为本机监听，默认不对公网开放。
    import uvicorn
    print("服务启动 -> http://127.0.0.1:8000/docs")
    uvicorn.run(app,host="127.0.0.1",port=8000)

