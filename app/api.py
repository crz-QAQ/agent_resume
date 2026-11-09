
from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from typing import Optional
from schemas import ResumeInfo
from fallback import call_with_fallback_async
from agent import AgentResult, parse_resume_agent
from file_parser import FileParseError, extract_text

from prompts import SYSTEM_RESUME,PROMPT_VERSION
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

app = FastAPI(
    title="FDE 学习项目",
    description="从 Go/PHP 转 Python + FastAPI 的第一个服务",
    version="0.0.4",
)

# 模拟内存库（真实场景换成数据库）
DB: dict[int, dict] = {}

class ResumeIn(BaseModel):
    # 请求体：上传简历原文
    text: str = Field(...,min_length=1,description="简历原文")

class ResumeOut(BaseModel):
    id: int
    name: Optional[str] = None
    years: Optional[float] = Field(None,ge=0,le=60)
    skills: list[str] = []

class UsageOut(BaseModel):
    input_tokens: int
    output_tokens: int
    cost_yuan: float

class ResumeLLMOut(ResumeOut):
    usage: UsageOut


class ResumeUploadOut(AgentResult):
    # 不修改 Day 11 的 AgentResult，只在上传响应上附加排查信息。
    file_name: str
    text_length: int


def fake_llm_extract(text: str) -> dict :
    # 模拟LLM提取
    known = ["Go","PHP","MySQL","Redis","Python","Docker","Linux"]
    skills = [s for s in known if s.lower() in text.lower()]
    return {"name":None,"years":None,"skills":skills}

@app.get("/",tags=["健康检查"])
async def health() -> dict:
    return {"message":"服务已启动","docs":"/docs"}


@app.post("/resume",response_model=ResumeOut,tags=["简历"])
async def create_resume(data: ResumeIn) -> ResumeOut:
    info = fake_llm_extract(data.text)
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
    # prompt = f"{SYSTEM_PROMPT}\n\n简历原文：\n{data.text}"
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
    DB[rid] = info.model_dump()
    usage = UsageOut(
        input_tokens=meter.prompt_tokens,
        output_tokens=meter.completion_tokens,
        cost_yuan=meter.cost(),
    )
    return ResumeLLMOut(id=rid, **DB[rid], usage=usage)


@app.post("/resume/agent",response_model=AgentResult,tags=["简历"])
async def create_resume_with_agent(data: ResumeIn) -> AgentResult:
    try:
        result = await parse_resume_agent(data.text)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="AI 服务暂时不可用，请稍后重试",
        ) from exc

    if len(result.steps) == 0:
        raise HTTPException(status_code=500)
    return result


@app.post(
    "/resume/upload",
    response_model=ResumeUploadOut,
    tags=["简历"],
    summary="上传 Word/PDF/TXT 简历并使用 Agent 解析",
)
async def upload_resume(file: UploadFile = File(...)) -> ResumeUploadOut:
    filename = file.filename or ""
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
            raise HTTPException(
                status_code=503,
                detail="AI 服务暂时不可用，请稍后重试",
            )
        if not result.steps:
            raise HTTPException(status_code=500)

        return ResumeUploadOut(
            **result.model_dump(),
            file_name=filename,
            text_length=len(text),
        )
    finally:
        await file.close()


@app.get("/resume",tags=["简历"])
async def list_resume(page:int = 1,size :int = 10)->dict:
    # 分页列出
    items = [{"id":k,**v} for k,v in DB.items()]
    start = (page-1)*size
    return {"page":page,"size":size,"total":len(items),"items":items[start:start+size]}


@app.get("/resume/{rid}",response_model=ResumeOut,tags=["简历"])
async def get_resume(rid:int)->ResumeOut:
    if rid not in DB:
        raise HTTPException(status_code=404,detail="简历不存在")
    return ResumeOut(id=rid,**DB[rid])


if __name__ == "__main__":
    import uvicorn
    print("服务启动 -> http://127.0.0.1:8000/docs")
    uvicorn.run(app,host="127.0.0.1",port=8000)

