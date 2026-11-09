
from typing import Optional
from fastapi import FastAPI
from pydantic import BaseModel, Field, EmailStr, field_validator, ValidationError

app = FastAPI(
    title="Pydantic",
    version="0.0.2",
)

# 个人主要信息
class PersonalData(BaseModel):
    name:str = Field(...,min_length =1,description = "个人姓名")
    hobby:Optional[str] = Field(None,description="爱好可能缺失")

# 个人详情
class PersonalDetail(BaseModel):
    id_no:Optional[str] = Field(None,description="身份证必填")
    age:Optional[int] = Field(None,ge=0,le=100,description="年龄，0-100")
    years:Optional[float|int] = Field(None,ge=0,le=30,description="工作年限，0-30")
    skills:list[str] = Field(default_factory=list,description="技能列表")
    hobby: list[PersonalData] = Field(default_factory=list,description="爱好经历")
    email: Optional[EmailStr] = None

    @field_validator("id_no")
    @classmethod
    def id_no_not_validator(cls, v:Optional[str]) -> Optional[str]:
        if v is not None and not v.strip():
            raise ValueError("id_no不能是空白字符串")
        return v.strip() if v else v

    @field_validator("skills")
    def clean_skills(cls, v:list[str]) -> list[str]:
        # 去空白 + 去重
        seen , out = set(),[]
        for s in v:
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

# 响应
class ResponseResult(BaseModel):
    ok:bool
    msg:Optional[str] = None
    data:Optional[PersonalDetail] = None


# 接口
@app.post("/personal/result",response_model=ResponseResult)
def PersonalResult(personal_data: PersonalDetail):
    if not personal_data.id_no:
        return ResponseResult(ok=False,msg="缺失身份证")
    return ResponseResult(ok=True,msg="请求成功",data=personal_data)

@app.post("/personal/origin")
def origin(personal_data: PersonalDetail):
    return {"ok":True,"data":personal_data}

LLM_FAKE_OUTPUT_GOOD = '{"id_no":"440307********1777","age":2,"years":2,"skills":["Python","FastAPI"],"hobby":[{"name":"蔡*祖","hobby":"计算机"}]}'
LLM_FAKE_OUTPUT_BAD = '{"id_no":" ","years":999,"skills":["Go"],"email":"不是邮箱"}'

def validate_llm_output(raw:str) -> PersonalDetail|None :
    try:
        return PersonalDetail.model_validate_json(raw)
    except ValidationError as e:
        for err in e.errors():
            print(f"   [校验失败] 字段={err['loc']} 原因={err['msg']}")
        return None



if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        print("=== 本地自测：LLM 输出校验 ===")
        print("[好数据]")
        good = validate_llm_output(LLM_FAKE_OUTPUT_GOOD)
        print("   ->", good.model_dump() if good else None)
        print("[坏数据]")
        bad = validate_llm_output(LLM_FAKE_OUTPUT_BAD)
        print("   ->", bad)

        print("\n=== 三种构造方式对比 ===")
        print("字典构造 :", PersonalDetail(id_no="440307********1777", years=3).model_dump())
        print("JSON 构造:", PersonalDetail.model_validate_json('{"id_no":"440307********1777","years":"5"}').model_dump())
        print("注意：JSON 里的 \"5\" 被自动转成了数字 5 —— 这叫「类型强制转换」")
        print("      想禁止转换可以加 model_config = ConfigDict(strict=True)")

        print("\n=== 校验失败长什么样 ===")
        try:
            PersonalDetail(years=999)
        except ValidationError as e:
            print("  ", e.errors()[0]["loc"], "->", e.errors()[0]["msg"])
        sys.exit(0)

    # 默认起服务
    import uvicorn
    print("服务启动 -> http://127.0.0.1:8011/docs")
    uvicorn.run(app, host="127.0.0.1", port=8011)