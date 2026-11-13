# Resume Parsing Agent · 简历解析 Agent

**中文** | [English](README.en.md)

把「简历 → 结构化字段 → 业务护栏 → HTTP 服务 → 基于资料的问答」做成一个**能交付、能验证、能对比**的项目。

同一套需求做了三种实现（手写 / LangChain / Dify），用同一批用例交叉验证。

> 后端出身（Go/Gin + PHP，3 年）转 AI 交付方向，这是补「LLM 应用交付」这一层的实操项目。
> 关注点不是「让模型更聪明」，而是 **让 AI 的结果可验证、可拦截、可解释成本**。

## 五种使用方式

| 方式 | 状态 | 如何使用 |
|---|---|---|
| ① 本地部署 | ✓ | `docker compose up -d`（或 `tools/start_server.bat`）→ 打开 `http://127.0.0.1:8000/docs` |
| ② API 调用 | ✓ | 打分：`POST /match/score`（简历 + JD）→ `GET /match/report/{id}` 下载 PDF<br>修改意见：`POST /revise/review` → `POST /revise/apply` → 两份产出 |
| ③ Skill 给 agent | ✓ | 把 `skills/` 下的目录复制到你 agent 的 skills 目录；见下 |
| ④ MCP 给客户端 | ✓ | 可选安装 `requirements-mcp.txt`，以 stdio 运行 `mcp_server.py`；见下 |
| ⑤ 单页网页 | ✓ | 打开 `http://127.0.0.1:8000/ui`，两个标签页：**简历评分** / **修改意见** |

**③ 两个 skill，用途不同**：

| skill | 给谁 | 干什么 |
|---|---|---|
| [`skills/resume-job-matcher`](skills/resume-job-matcher/SKILL.md) | 想让 agent **会调用**的 | 一条命令：简历 + JD → 100 分报告 + PDF |
| [`skills/match-scoring-design`](skills/match-scoring-design/SKILL.md) | 想**照着设计**同类系统的 | 六个设计决策 + 反例 + 各自代码落点 |

最短用法（不需要起服务）：

```bash
python skills/resume-job-matcher/scripts/score.py --local \
  --resume 简历.pdf --jd jd.txt --out 报告.pdf
```

加 `--json` 输出机器可读的 `MatchReport`；退出码 `0` 成功 / `2` 输入错误 / `3` 服务不可用 / `4` 内部错误。

**④ Claude Desktop / 支持 MCP 的客户端**：先按「快速开始」安装主依赖并配置 `.env`，再执行
`python -m pip install -r requirements-mcp.txt`。使用 mcp 2.x 的 `MCPServer`；升级 3.x 前先读
[migration guide](https://py.sdk.modelcontextprotocol.io/v2/migration/)。

在 `claude_desktop_config.json` 中配置（将占位路径替换为克隆目录，Windows 路径使用 `/`）：

```json
{
  "mcpServers": {
    "resume-agent": {
      "command": "<项目绝对路径>/.venv/Scripts/python.exe",
      "args": ["<项目绝对路径>/mcp_server.py"]
    }
  }
}
```

macOS / Linux 的解释器路径改用 `<项目绝对路径>/.venv/bin/python`。MCP 提供
`parse_resume(resume_path)`、`score_match(resume_path, jd_text)`、
`make_report(resume_path, jd_text, out_path=None)`，相对路径以项目根目录为基准，简历支持 PDF/DOCX、上限 10MB。
解析结果只含结构化字段和字数；评分返回既有 `MatchReport`（含逐项证据），完整简历正文不进入工具结果。
省略 `out_path` 时 PDF 保存到 `reports/`。运行 `python tools/verify_mcp.py` 做真实 stdio 验收与 CLI 对账；
只检查协议和输入护栏可加 `--offline`，该模式不调用模型。

**⑤ 单页网页（给非技术同事）**：启动服务后打开 `http://127.0.0.1:8000/ui`，
上传简历 + 粘贴 JD → 查看分数与明细 → 下载 PDF，无需操作 Swagger 或复制报告标识。
页面无外部 CDN，也不保存输入，刷新即清空。服务请只监听 `127.0.0.1`；**v1 无鉴权，请勿暴露公网。**

## 隐私提醒

简历包含个人敏感信息。**请勿将真实简历上传到公共部署环境。**
`/match/score` 与 `/revise/review` 都在内存中处理简历原文，服务端只把最终文件写入 `reports/`
（打分 PDF / 修改意见 PDF / 改后简历），有效期为 30 分钟；接口响应里不含简历全文。
网页不写 `localStorage`、不植 cookie，刷新即清空。

---

## 能做什么

| # | 能力 | 关键实现 |
|---|---|---|
| 1 | **多格式简历解析** | `.docx`（含表格）/ `.pdf`（含双栏）/ `.txt` `.md`；扫描件明确返回 422 而不是空内容 |
| 2 | **结构化抽取** | `name / years / skills`，提示词约束 + pydantic 校验 |
| 3 | **业务护栏（纯代码）** | 空壳 / 相对时间 / 招聘要求 / 技能笼统 四条规则，**不依赖模型自觉** |
| 4 | **容错与止损** | 多厂商降级链、重试、预算与步数护栏（调用**前**检查）、单次成本精确到元 |
| 5 | **RAG 问答** | 检索 + 引用反查 + 资料外拒答；三档检索（等权 / 加权 RRF / rerank）可切换 |
| 6 | **换实现不改语义** | 手写 numpy 余弦 ↔ chromadb，8 题 top-1 逐题一致 |
| 7 | **对照 JD 给简历打分** | 100 分 = 硬性 40 + 语义 30 + LLM 30；每一分可追溯到「简历哪句、JD 哪句」；出 7 章 PDF |
| 8 | **对照 JD 生成修改意见** | 五类问题 → 四段结构（原文 / 建议改为 / 为什么改 / 有什么好处）；另出一份改后简历，版式保留 |

---

## 对照 JD：打分与修改意见

两条能力共用同一套 JD 抽取与护栏，区别是「评价」与「改写」。

### 打分（100 分 = 40 + 30 + 30）

| 层 | 分值 | 怎么做 | 为什么这么切 |
|---|---:|---|---|
| 硬性 | 40 | **纯规则**：年限 15 / 学历 10 / 必备技能 15 | 零成本、零不确定；模型飘的时候能立刻判断是模型还是汇总的锅 |
| 语义 | 30 | 切句 → bge-small-zh 余弦**召回** → cross-encoder **精判**是否覆盖 | 单靠余弦分不开「同句式不同领域」（实测「分布式数据库内核」对 Go 订单简历也有 0.711）；重排后真相关为正值、无关全负 |
| LLM 复核 | 30 | 技术栈深度 12 / 业务场景 10 / 成果可信 8 | 只出子项，**不许模型决定总分与档位** |

几条硬规则：**未知 ≠ 0**（简历没写年限 → `years_unknown` 交人确认，不是判不匹配）；语义分按**覆盖率**进分母（6 条职责只对上 1 条不该拿同样的分）；技能按**概念族**去重（JD 写「用过 DeepSeek、Qwen、OpenAI 等」时，只写 DeepSeek 不算缺）——放宽只发生在同族内部，真缺口一分不让。

### 修改意见（两份产出）

```
① 修改意见报告 PDF：抬头 → 结论摘要 → 逐条意见（四段卡片）→ 缺口清单
                    → ATS 关键词覆盖表 → 不建议改清单 → 智能修改对比 → 页脚审计
② 修改后的简历：docx 进 → docx 出（原地替换段落，字体/表格/页边距全保留）
                pdf 进 → pdf 出（重排单栏，并明确告知版式已重排）
```

**四条不可越过的红线**（都在代码里，不靠模型自觉）：

| 红线 | 落地方式 |
|---|---|
| 不许新增原文与 JD 都没有的名词 | 技术词/专名白名单校验，命中即丢弃该条 |
| 不许篡改数字 | 数字必须在原简历里找得到（允许「两秒 → 2 秒」这种写法归一） |
| 不许把「没有」写成「有」 | `missing` 类意见**不生成建议正文**，只提示「如确有经历请补充」|
| 不许抄 JD 措辞刷分 | 与 JD 任一条重合 ≥0.7 的改写不采用（抄 JD 能把语义分从 0 刷到 24，这条专治它）|

`suggested_text` 为空或与原句完全相同时，PDF 不印空栏，而印明确的人工动作指引
（「（不可代写：如确有该经历，请在技能栏补充…）」「（此行需你补全：按实际在职月份填写，系统不代拟）」）。

**改前/改后分数**：硬性 40 与语义 30 **精确重算**，LLM 30 **不重跑**、沿用原分并标「预估 ±2」；
变化为负时如实显示并提示「建议只采用部分建议」。

---

## 架构

![架构图](demo/架构图.png)

```mermaid
flowchart LR
    A[客户端] --> B{FastAPI 接口层}
    B -->|POST /resume/upload| C[文件解析层<br/>docx · pdf · txt]
    B -->|POST /resume/agent| D[Agent 层]
    C --> D
    D --> E[抽取<br/>LLM + 结构化输出]
    E --> F[护栏层<br/>4 条纯代码规则]
    F -->|needs_review| G[人工复核]
    F -->|ok| H[结果入库]
    D -.降级链.-> I[(多厂商模型)]
    B -->|POST /resume/llm| J[RAG 链路]
    J --> K[BM25 + 向量 + RRF]
    K --> L[(向量库<br/>chromadb)]
    K --> I
    J --> M[引用反查 + 拒答]
```

**分层原则**：Agent 层说业务语言（`ok / needs_review / failed`），HTTP 层说协议语言（`200 / 422 / 503 / 500`），两层之间必须翻译。

---

## 快速开始（5 分钟）

```bash
# 1. 依赖
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt          # Windows
# .venv/bin/pip install -r requirements.txt            # macOS / Linux

# 2. 配置
cp .env.example .env
# 编辑 .env，填入自己的 DEEPSEEK_API_KEY

# 3. 从项目根目录启动
.venv/Scripts/python.exe -m uvicorn app.api:app --host 127.0.0.1 --port 8000

# 4. 打开 Swagger 点着测
# http://localhost:8000/docs
```

服务默认仅监听 `127.0.0.1`，v1 没有鉴权，不要直接暴露公网。

### 用 Docker Compose 启动

在 Linux 上，请先用 `mkdir -p reports` 创建报告目录，并确保容器内的 `appuser` 可以写入。

```bash
cp .env.example .env
# 按上面的配置步骤编辑 .env，再启动服务
docker compose up -d
```

打开 [Swagger UI](http://127.0.0.1:8000/docs) 试用接口。

### 首次运行要注意

- 首次使用向量模型时会下载 `bge-small-zh-v1.5`（约 100 MB），首次使用重排时会另行下载 `bge-reranker-base`（约 1 GB）。镜像内置 `HF_ENDPOINT=https://hf-mirror.com`，可在运行时覆盖。
- `docs/` 由 `.gitignore` 排除，克隆后 RAG 语料为空是刻意设计。复现评测时请在本地准备相同版本的语料。
- 本地启动和 Docker Compose 默认都只通过宿主机 `127.0.0.1` 提供访问。**v1 无鉴权，请勿暴露到公网。**

### 三种调用方式

```bash
# ① Agent：抽取 + 护栏 + 降级链 + 成本统计
curl -X POST http://localhost:8000/resume/agent \
  -H "Content-Type: application/json" \
  -d '{"text":"赵六，2016 年入行，去年离职，做后端"}'
# → {"status":"needs_review","reasons":["相对时间:年限需人工确认"],...}

# ② 上传真实文件（docx / pdf，≤10MB）
curl -X POST http://localhost:8000/resume/upload \
  -F "file=@tools/test_files/table_resume.docx"

# ③ 扫描件会被明确拒绝
curl -X POST http://localhost:8000/resume/upload \
  -F "file=@tools/test_files/scan_resume.pdf"
# → 422 这份 PDF 没有文本层（很可能是扫描件/图片），需要 OCR 才能解析
```

**打分与修改意见（两条最常用的命令）**

```bash
# 打分：简历 + JD → 100 分 + report_id
curl -X POST http://localhost:8000/match/score \
  -F "file=@tools/test_files/go_backend_resume.docx" \
  -F "jd_text=招聘后端开发工程师，必须会 Go 和 MySQL…"
# → {"report_id":"...","report":{"score":{"total":90,...},"verdict":"高度匹配",...}}

# 修改意见：只用岗位名（先取画像 → 确认 → 分析）
curl "http://localhost:8000/revise/profile?job_title=FDE%20交付工程师"
# → {"profile":{...},"profile_hash":"..."}
curl -X POST http://localhost:8000/revise/review \
  -F "file=@tools/test_files/go_backend_resume.docx" \
  -F "job_title=后端开发工程师" \
  -F "jd_text=招聘后端开发工程师，必须会 Go 和 MySQL…"
# → {"review_id":"...","report":{"issues":[...],"score_before":90},"mode":"user_jd"}

# 生成修改版：一次产两份（意见报告 + 改后简历）
curl -X POST http://localhost:8000/revise/apply \
  -H "Content-Type: application/json" -d '{"review_id":"<上一步的 id>"}'
# → {"review_pdf_url":"/revise/file/<id>/review","resume_url":"/revise/file/<id>/resume",
#    "resume_ext":"docx","applied":[...],"skipped":[...],"delta":{...}}
```

---

## 接口清单

| 方法 | 路径 | 说明 | 状态码 |
|---|---|---|---|
| GET | `/` | 健康检查 | 200 |
| POST | `/resume` | 结构化数据直接入库 | 200 |
| POST | `/resume/llm` | 纯 LLM 抽取（无护栏） | 200 / 422 / 503 |
| POST | `/resume/agent` | **Agent：抽取 + 护栏 + 降级 + 成本** | 200 / 503 |
| POST | `/resume/upload` | 上传 docx/pdf/txt/md 解析后走 Agent | 200 / 413 / 422 |
| GET | `/resume` | 分页列表 `?page=1&size=10` | 200 |
| GET | `/resume/{rid}` | 按 ID 查询 | 200 / 404 |

**打分与修改意见**

| 方法 | 路径 | 说明 | 状态码 |
|---|---|---|---|
| POST | `/match/score` | 简历 + JD → 100 分报告 + `report_id` | 200 / 413 / 422 / 503 |
| GET | `/match/report/{report_id}` | 下载打分 PDF | 200 / 404 |
| GET | `/match/stats` | 缓存报告数与 TTL | 200 |
| GET | `/revise/profile` | 岗位名 → 岗位画像草案 + 指纹（**零模型**）| 200 / 422 |
| POST | `/revise/review` | 简历 + 岗位名 +（可选）JD → 修改意见 | 200 / 413 / 422 / 503 / 500 |
| POST | `/revise/apply` | 勾选条目 → 一次产两份（意见报告 + 改后简历）| 200 / 404 / 422 |
| GET | `/revise/file/{artifact_id}/{kind}` | 下载 `kind ∈ {review, resume}` | 200 / 404 |
| GET | `/revise/stats` | 缓存数与本轮 TTL | 200 |
| GET | `/ui` | 单页网页（两个标签页）| 200 |

**岗位输入四组合**：有 JD（用 JD，最准）· 只有岗位名（生成画像草案，**必须用户确认后才分析**）· 只有 JD（允许，提示补岗位名）· 都没有（422）。

**修改意见的两个错误码语义**：`422` 是输入问题（扫描件 / JD 太短 / 画像未确认 / 勾选为空），`503` 是模型全挂；本组接口**不使用 400**。

### 状态码语义（交付现场最容易被忽略的一条）

| 内部状态 | HTTP | 客户端该做什么 |
|---|---|---|
| `ok` | 200 | 正常 |
| `needs_review` | **200** | 业务正常，交人复核（不是错） |
| `failed`（上游全挂） | **503** | 该重试 |
| 未捕获异常 | **500** | 该改代码，重试无用 |

---

## 关键设计

### 1. 护栏为什么写在代码里，不写在提示词里

LLM 的失败是**静默的**：接口 200、pydantic 通过、内容是错的。

```
{"name":null,"years":null,"skills":[]}   ← 合法 JSON，但什么都没抽到
```

`check_resume()` 用纯代码做四件事：

| 规则 | 触发条件 | 为什么交人而不是让模型重试 |
|---|---|---|
| 空壳 | 三字段全空 | 原文可能就没有信息，重试也抽不出来 |
| 相对时间 | 含「去年/今年/目前/至今/现在/应届」 | 模型不知道今天几号，硬算必错（实测把「2016 入行、去年离职」算成 9 年） |
| 招聘要求 | 含「岗位要求/任职要求/招聘」 | 这是岗位描述，不是本人经历 |
| 技能笼统 | skills 为空但原文写了「做后端」 | 数据不完整，下游需要人补 |

**关键教训**：规则判断依据要取**原文**，不能取**模型输出** ——
否则同一条输入，模型这次输出 `["后端"]`、下次输出 `[]`，判定就跟着飘。

### 2. 预算与步数护栏必须在调用之前

```python
if len(steps) >= max_steps: return needs_review(...)
if cost >= budget_yuan:     return needs_review(...)
```

### 3. 评测必须先防污染

```
✗ 评测题写在语料里   → 测的是「抄答案」
✗ 答案不在语料里     → 测的是运气
✓ 题目文字不在语料 + 答案在语料 + 标注语料版本
```

### 4. 路径必须锚定到文件位置

```python
DEFAULT_PATH = Path(__file__).resolve().parent / ".chroma"   # ✓
path = "练习/.chroma"                                          # ✗ 换目录就生成两个库
```

客户内网的启动目录不可控，所有路径都从 `__file__` 派生。

---

## 实测数字

| 指标 | 数值 | 复现方式 |
|---|---|---|
| 语料规模 | 430 chunk / 25 文件（本地语料，未进仓库） | `rag.load_chunks()` |
| 检索三档命中 | 等权 5/8 · 加权 RRF 6/8 · rerank **7/8** | `tools/eval_retrieval.py` |
| 单题延迟 | 5 ms · 5 ms · **6849 ms** | 同上 |
| 向量库一致性 | 8 题 top-1 **8/8 相同** | `tools/eval_vector_store.py` |
| 向量库延迟 | 手写 2.54~3.14 ms vs chroma 14.17~16.70 ms | 两次实测 |
| 单次抽取成本 | **0.00028 元** | `UsageMeter` |
| 单次打分成本 | **0.0018~0.0021 元**（3 次模型调用）| `tools/d1_check.py` |
| 单次修改意见成本 | **0.0049 元**（JD 抽取 + 意见生成 + LLM 复核）| `tools/verify_revise_http.py` |
| 语义打分耗时 | 冷启动 3.02 s（含模型加载）→ 预热后 **0.12~0.23 s/次** | `tools/check_semantic.py` |
| 打分验收 | **19/19**（6 组简历×JD、错误码、目录穿越）| `tools/verify_match.py` |
| 修改意见验收 | 修改意见生成 **17/17** · PDF **10/10** · 版式保留 **18/18** · 提分预估 **9/9** · 端到端 **34/34** | `tools/check_revision_*.py`、`verify_revise_http.py` |
| Docker 镜像 | **726 MB**（非 root、0 个真 key；含 fastembed/onnxruntime 本地推理）| `docker images` |
| 接口用例 | 7/7 通过（含扫描件 422 / 11MB 413） | `tools/verify_delivery.py` |
| 三版本行为一致 | status 4/6、抽取 6/6 | 见下方「三种实现」 |

> **关于语料**：检索命中率与延迟在原始学习语料（439 chunk / 26 文件）上实测；
> 本仓库随附的本地语料为 430 chunk / 25 文件，复现时命中率可能有 ±1 题偏差。
> 语料本身是学习文档，未随仓库分发（`.gitignore` 里的 `docs/`），这是刻意的：
> **评测要能复现，但语料不该进代码仓库**。

> **关于「换库更快」**：424 个 chunk 上 chromadb 比手写全量余弦**慢 5 倍**。
> 换库买到的是增量 add、metadata 过滤、向量落盘 —— 不是当前规模的性能。

---

## 三种实现对照

| 项 | 手写 | LangChain | Dify |
|---|---|---|---|
| 护栏 | `check_resume()` 26 行 | 复用同一份 | 代码节点 63 行 |
| 部署体积 | **726 MB** | 726 MB + 9 MB | **13.07 GB / 15 容器** |
| 启动 | 秒级 | 秒级 | 4 分 05 秒（重启 40 秒） |
| 改一条规则 | 改代码重跑 | 改代码重跑 | **改节点秒级生效** |
| 成本统计 | 精确到元 | 需挂 callback | 面板不显示 |
| 适用 | 内网、规则复杂、要进 git | 多模型多数据源 | 业务方自助调 |

> **体积口径说明**：Day15 时手写版是 488 MB（只有大模型 API 调用）；S4 语义分引入
> 本地 embedding + cross-encoder 重排（fastembed/onnxruntime）后增至 **726 MB**；
> D1 时把只用于「换库对照」的 chromadb 移出运行时依赖，从 1.05 GB 降回 726 MB
> （chromadb 会拖进 kubernetes 126 MB 等约 324 MB）。

**复刻的副产品**：用 Dify 重写一遍时，发现手写版「招聘要求」拦截条件多写了一个 `is_student`，
导致纯招聘广告被放行 —— 换一种实现复刻，本身就是一次代码 review。

---

## 目录结构

```
resume-agent/
├─ app/                          # 业务代码
│  ├─ api.py                     # FastAPI 接口层（/resume* · /match* · /revise* · /ui）
│  ├─ agent.py                   # Agent：抽取 → 护栏 → 修复 → 结果
│  ├─ schemas.py                 # 结构化输出 schema（ResumeInfo / AgentResult）
│  ├─ prompts.py                 # 提示词（SYSTEM_RESUME / SYSTEM_JOB / SYSTEM_MATCH_RUBRIC / SYSTEM_REVISION）
│  ├─ providers.py               # 多厂商定义 + 成本统计（UsageMeter）
│  ├─ fallback.py                # 异步调用 + 降级链
│  ├─ resilience.py              # 超时控制 + 指数退避
│  ├─ file_parser.py             # Word/PDF 解析（表格 + 双栏 + 扫描件）
│  ├─ rag.py                     # RAG：BM25 / 向量 / RRF / rerank / 拒答
│  ├─ vector_store.py            # chromadb 版检索（等价手写基线）
│  ├─ langchain_extract.py       # LangChain 版抽取（对照用）
│  ├─ langchain_agent.py         # LangChain 版 Fallback + 修复（对照用）
│  ├─ check_agent.py             # 6 条断言自检
│  ├─ check_vector_store.py      # 3 条断言自检
│  │
│  ├─ ── 打分（S0~S7b）──
│  ├─ schemas_match.py           # JobInfo / MatchReport / Deduction / SemanticItem
│  ├─ job_parser.py              # JD → JobInfo（3 条护栏）
│  ├─ resume_parser.py           # 文件 → 文本 + ResumeInfo（扫描件抛错）
│  ├─ scoring_rules.py           # 硬性 40（纯规则 + 概念族 SKILL_FAMILIES）
│  ├─ scoring_semantic.py        # 语义 30（余弦召回 + cross-encoder 精判 + 覆盖率）
│  ├─ scoring_llm.py             # LLM 复核 30（证据校验 + 建议白名单）
│  ├─ match_pipeline.py          # 编排：解析 → 三层打分 → 汇总
│  ├─ match_report.py            # 总分 / 档位（85/70/55）/ 缺口 / 建议
│  ├─ report_pdf.py              # 打分 PDF（7 章 + 四态对比表）
│  ├─ report_store.py            # 报告与产出的 TTL 存储（防目录穿越）
│  ├─ pdf_kit.py                 # 两份 PDF 共用的排版件（表格/字体/截断/页脚）
│  │
│  ├─ ── 修改意见（M0~M7）──
│  ├─ schemas_revision.py        # ReviewIssue / JobProfile / DiagnosisReport / ScoreDelta
│  ├─ guard_revision.py          # 红线校验：新名词 / 新数字 / 原句可检索
│  ├─ resume_diagnose.py         # 缺陷诊断（8 类 code）+ ATS 关键词覆盖
│  ├─ job_profile.py             # 岗位画像族模板 + 指纹 + 确认流
│  ├─ revision_llm.py            # 修改意见生成（七道护栏 + 缺口一致性 + JD 雷同闸门）
│  ├─ revision_pdf.py            # 修改意见 PDF（7 章 + 三态渲染 + 不建议改清单）
│  ├─ resume_rewrite.py          # docx 原地替换保版式 / PDF 重排 / 一次产两份
│  ├─ score_delta.py             # 改前改后分数对比（硬性+语义精确，LLM 标预估）
│  ├─ revise_pipeline.py         # 编排：岗位 → 诊断 → 意见 → 预估 → 应用
│  └─ static/index.html          # 单页网页（两个标签页，无 CDN、无本地存储）
├─ tools/                        # 评测与验证
│  ├─ bootstrap.py               # 工具脚本的导入引导
│  ├─ eval_retrieval.py          # 8 题 × 三档检索
│  ├─ eval_vector_store.py       # 8 题双跑，比 top-1
│  ├─ verify_delivery.py         # 11 条交付验证
│  ├─ verify_match.py            # 打分接口 19 条
│  ├─ verify_mcp.py              # MCP stdio 14 条
│  ├─ check_job_parser.py        # JD 抽取 8 条
│  ├─ check_resume_parser.py     # 简历输入 4 条
│  ├─ check_scoring_rules.py     # 硬性分 13 条
│  ├─ check_semantic.py          # 语义分 9 条
│  ├─ check_llm_score.py         # LLM 复核 7 条
│  ├─ check_match_report.py      # 汇总 15 条
│  ├─ check_report_pdf.py        # 打分 PDF 9 条
│  ├─ check_revision_guard.py    # 红线校验 8 条
│  ├─ check_resume_diagnose.py   # 缺陷诊断 12 条
│  ├─ check_job_profile.py       # 岗位画像 14 条
│  ├─ check_revision_llm.py      # 意见生成 17 条
│  ├─ check_revision_pdf.py      # 意见 PDF 10 条
│  ├─ check_resume_rewrite.py    # 版式保留 18 条
│  ├─ check_score_delta.py       # 提分预估 9 条
│  ├─ verify_revise_http.py      # 修改意见端到端 34 条
│  ├─ make_test_files.py         # 生成测试 docx/pdf
│  └─ test_files/                # 测试简历（含扫描件与高分样本）
├─ skills/                       # 两个 agent skill（能力型 / 方法论型）
├─ mcp_server.py                 # MCP server（stdio，3 个工具）
├─ demo/                         # 演示材料
│  ├─ 架构图.png / .html
│  └─ 演示脚本.md                # 3 分钟逐句讲稿
├─ .env.example
├─ requirements.txt / requirements-mcp.txt / requirements-optional.txt
└─ Dockerfile                    # 726MB，非 root，无密钥
```

---

## Docker

> 请优先使用快速开始里的 Docker Compose 步骤。手动运行下方示例时，请将端口参数设为 `-p 127.0.0.1:8000:8000`，让宿主机仅在回环地址上发布端口。

```bash
docker build -t resume-ai:latest .
docker run -p 8000:8000 --env-file .env resume-ai:latest
```

镜像 **726 MB**（`docker images` 实测），非 root 用户运行，`.dockerignore` 排除 `.env`（扫过：0 个真实密钥）。
手写 RAG vs chromadb 换库对照需要额外安装 `requirements-optional.txt`，运行时镜像不含 chromadb（它会把 kubernetes 等 300MB 拖进来）。

---

## 限制与未做

| 项 | 说明 |
|---|---|
| 无 OCR | 扫描件直接 422 拒绝 |
| 无流式输出 | 全部同步返回 |
| 无鉴权 | 单机 demo，未接 JWT / API Key |
| 存储是内存 | 重启丢数据，没接数据库 |
| Dify 版无降级链 | 平台能力，本次未验证 |
| **PDF 简历无法保留版式** | PDF 不可编辑，只能重排单栏；要保版式请上传 docx |
| **文本框 / 艺术字 / 页眉内文字不自动改** | 这些位置取不到 run，只会标记为「建议手动改」|
| **含修订痕迹的 docx 整份拒绝** | 会搅乱别人未处理的修订，要求先在 Word 里接受/拒绝 |
| **改后简历不改结构** | 不增删段落、不改章节顺序、不动字体字号——只替换被勾选的那几句 |
| 修改意见不做批量 / 无历史记录 | 刷新即清空；v1 不存任何简历内容 |
| 招聘要求规则的旧版本 bug | **本仓库已修复**（旧条件多了 `is_student`，纯招聘广告会被放行）；修复原因见 `app/agent.py` 规则 3 注释 |

---

## 规划中

| 功能 | 依赖 | 状态 |
|---|---|---|
| ① 简历关键字解析 | 已有 | ✅ |
| ② 对照 JD 给简历打分（分项 + 证据可回溯） | 新增 JD 抽取 + 匹配引擎 | ✅ |
| ③ 对照 JD 生成简历修改意见（禁止编造事实） | ② 的差异项 | ✅（含改后简历与提分预估）|
| ④ 对照 JD + 简历生成面试问答 | ② 的差异项 | ⬜ |

统一抽象：JD 抽取与简历抽取复用同一套护栏与结构化输出；
匹配分数 = 规则项 + 语义项 + LLM 复核，**每一分都要能追溯到「简历哪句、JD 哪句」**；
修改意见沿同一条链，**每条建议都要能指回原句、且不许引入原文与 JD 之外的事实**。

---

## 许可

个人学习项目，可自由参考。
