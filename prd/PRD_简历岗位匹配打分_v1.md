# PRD｜简历 × 岗位匹配打分（v1）

> 项目：resume-agent
> 作者：蔡荣祖 + Hermes
> 状态：待评审（**评审通过才开工，不允许一次性交付整个 PRD**）
> 开发方式：**按 S0→S7 分块，每块完成 → 你 review 消化 → 我再出下一块**

---

## 一、功能定义

### 1.1 一句话

上传一份 **PDF 简历** + 粘贴一段 **岗位 JD**，输出一份 **PDF 打分报告**：
匹配度总分 + 分项得分 + 扣分理由（带原文证据）+ 简历关键字 + 简历主要项 + 缺失项与建议。

### 1.2 输入 / 输出

| 项 | 内容 |
|---|---|
| 输入 1 | 简历 **PDF 文件**（主形态；同时兼容 docx/txt，但 PDF 是主要目标） |
| 输入 2 | 岗位 JD（**纯文本**，粘贴即可；v1 不做 JD 文件解析） |
| 输出 1 | JSON（接口返回，供程序消费） |
| 输出 2 | **PDF 报告**（给人看，可直接发给 HR / 业务方） |

### 1.3 明确不做（v1 范围外）

```
✗ 不做简历修改建议的「改写句子」（那是下一功能 ③）
✗ 不做面试题生成（功能 ④）
✗ 不做多岗位批量对比（v2）
✗ 不做 OCR（扫描件 PDF 仍返回 422）
✗ 不做前端页面（Swagger + 返回的 PDF 链接足够）
```

---

## 二、打分模型（**每一分都要能追溯，否则不算完成**）

### 2.1 总分构成（100 分）

| 分项 | 分值 | 计算方式 | 是否调模型 |
|---|---|---|---|
| **A. 硬性条件** | 40 | 纯规则，确定性 | 否 |
| **B. 语义相近** | 30 | 向量相似度（bge-small-zh，与 RAG 同一个模型） | 否 |
| **C. LLM 复核** | 30 | 按 rubric 打分，必须给理由 | 是 |
| 合计 | 100 | 三项相加，取整 | — |

### 2.2 A 硬性条件（40 分）明细

| 子项 | 分值 | 规则 |
|---|---|---|
| 工作年限 | 15 | 简历年限 ≥ JD 要求 → 满分；差 1 年 → 扣 7；差 ≥2 年 → 0 分 |
| 学历 | 10 | 达标 → 满分；未达标 → 0 分（JD 未写 → 直接满分，不猜） |
| 必备技能命中率 | 15 | `命中数 / 必备数 × 15`，四舍五入 |

### 2.3 B 语义相近（30 分）

```
做法：JD 的每条 responsibilities 与简历的每条项目/经历描述做向量相似度
得分：取「每条 JD 职责的最高相似度」的平均，再 ×30
门槛：相似度 < 0.6 的条目不计入平均（视为明显不相关），但要记入「未覆盖职责」
```

### 2.4 C LLM 复核（30 分）

**rubric（写死在 prompt 里，不让模型自由发挥）**：

| 维度 | 分值 | 打分依据 |
|---|---|---|
| 项目复杂度匹配 | 12 | 简历项目与 JD 职责的复杂度是否同一档（如 JD 要「高并发/分布式」，简历只有 CRUD → 低分） |
| 技术栈匹配 | 10 | 语言/框架/中间件的方向一致性 |
| 经历相关性 | 8 | 行业/业务场景相关性 |

**硬性要求**：每一项都必须给出 `reason` 与 `evidence`（引用简历原文片段），
`evidence` 为空的项 **直接判 failed**（这是代码 bug，不是业务问题）。

### 2.5 扣分理由（gaps）结构

每条扣分必须能回答三个问题：

```json
{
  "item": "必备技能: K8s",
  "type": "missing_skill",          // missing_skill / years_short / education / weak_project / mismatch
  "deduct": 5,                       // 扣了多少分
  "jd_evidence": "JD 原文：要求熟悉 K8s",
  "resume_evidence": "简历未提及",     // 或 简历原文片段
  "severity": "high"                  // high / medium / low
}
```

---

## 三、输出契约（JSON，先定死再写代码）

```python
class MatchScore(BaseModel):
    total: int                          # 0-100
    breakdown: dict[str, int]           # {"hard": 32, "semantic": 21, "llm": 18}

class ResumeKeyword(BaseModel):
    name: str                           # 关键字文本，如 "Go"
    category: str                       # language / framework / middleware / db / cloud / tool / domain
    count: int                          # 在简历中出现的次数（衡量权重）

class ResumeHighlight(BaseModel):
    label: str                          # 如 "工作年限" / "最近项目" / "技术栈"
    value: str                          # 如 "3 年，后端"

class Deduction(BaseModel):
    item: str
    type: str
    deduct: int
    jd_evidence: str
    resume_evidence: str
    severity: str

class MatchReport(BaseModel):
    score: MatchScore
    verdict: str                        # 高度匹配 / 匹配 / 勉强匹配 / 不匹配（按总分分档）
    keywords: list[ResumeKeyword]        # 【需求3】简历关键字提取
    highlights: list[ResumeHighlight]    # 【需求4】简历主要项
    deductions: list[Deduction]          # 【需求2】扣分理由
    missing: list[str]                   # 【需求8】与岗位描述中缺失的地方
    mismatches: list[str]                # 【需求8】与岗位不符的地方
    suggestions: list[str]               # 【需求5】还需要补充什么（诚实建议，禁止编造经历）
    meta: dict                           # 简历文件名 / JD 摘要 / 模型 / 成本 / 耗时
```

**分档规则**（写死在代码里，不让模型定）：

```
≥85 高度匹配   ≥70 匹配   ≥55 勉强匹配   <55 不匹配
```

---

## 四、PDF 报告结构（**需求 7、8 的落点**）

一页正文 + 最多两页附录，中文字体必须内嵌。固定章节：

| 页 | 章节 | 内容 |
|---|---|---|
| 1 | 抬头 | 简历文件名 / 岗位 / 生成时间 |
| 1 | **总分** | 大字 78 / 100 + 档位标签 + 三项分值得分条 |
| 1 | **扣分理由** | 表格：扣分项 / 扣几分 / JD 依据 / 简历现状 / 严重度 |
| 2 | **简历关键字** | 按类别分组的标签，出现次数标注 |
| 2 | **简历主要项** | 姓名 / 年限 / 学历 / 最近公司 / 技术栈 / 代表项目（各一行） |
| 2 | **对比表** | 左列 JD 要求，右列简历情况，中间 ✓ / ✗ / ?（部分符合）|
| 3 | **缺失与不符** | 缺失项列表 + 不符项列表（分两栏或分两段） |
| 3 | **建议补充** | 3-6 条，只针对「缺失但可补」的项，**禁止建议编造经历** |
| 页脚 | 免责 | 本报告由 AI 生成，分数仅作初筛参考；关键结论请人工复核 |

---

## 五、开发分块（**每块完成必须停，等 review**）

### S0｜地基：依赖 + 中文字体 + 契约（0.5 天）

- **做什么**
  1. `requirements.txt` 增加 `reportlab`（当前没有，PDF 生成必需）
  2. 新增 `app/pdf_font.py`：注册中文字体（`STSong-Light` CID 字体 或 系统 `msyh.ttc`），提供 `get_cjk_font()`；**必须验证中文不显示成方框**
  3. 新增 `app/schemas_match.py`：把第三节的 5 个模型写死（只定义，不实现）
- **验收**
  - `python -c "import reportlab"` 通过
  - 生成一个含中文的测试 PDF，`pdfplumber` 能读回中文（不是方块/乱码）
  - `from schemas_match import MatchReport` 通过
- **禁止**：这一步不写任何打分逻辑
- **为什么先做**：中文字体是 PDF 环节最容易翻车的点，先花半天解决，别等报告写完才发现是方块

#### S0 实施记录（2026-11-09）

**改动文件**
- 新增 `app/pdf_font.py`：统一字体注册、缓存和回退。
- 新增 `app/schemas_match.py`：5 个数据模型，各字段中文注释与 Field(description)。
- 新增 `tools/verify_font.py`：生成并读回中文 PDF。
- 修改 `requirements.txt`：按任务书分组，补齐 5 个依赖。
- 新建 `.venv/`（已被 gitignore 忽略）；本节仅插入记录，不修改 PRD 正文。

**输入了什么**
- 已阅读全文：`prd/S0_任务书.md`、本 PRD（重点 §三输出契约与 §六红线⑦）。
- 环境命令：`uv venv .venv`，使用阿里云 PyPI 镜像安装完整 requirements.txt。
- 字体候选：`C:\Windows\Fonts\msyh.ttc`（subfontIndex=0）→ `simhei.ttf` → `STSong-Light` → RuntimeError。
- PDF 两行固定文本：简历匹配分析报告；Go 3 年 / 后端 / 匹配度 78 分。

**实现过程**
1. 只新增两个 app 模块，未修改已有 app 文件；没有实现打分、匹配、抽取逻辑，没有调用 LLM 或读写 .env。
2. 注册函数使用模块级缓存和锁；实际命中第 1 级 msyh。中文字体族将加粗/斜体映射到常规字体，避免 CID 缺少粗体报错。所有候选失败时明确抛 RuntimeError，绝不回退 Helvetica。
3. 五个模型严格沿用 PRD 字段及类型，22 个字段全部必填、有中文注释及描述；无计算、解析、渲染代码。
4. verify_font.py 使用 canvas.setFont 写入一页 PDF，再用 pdfplumber 读回，忽略空白差异比较全文；失败返回 exit 1。
5. 遇到的坑：首次 PowerShell 默认解码使中文显示乱码，改为 UTF-8 读取后正常；uv 缓存访问遭沙箱拒绝，经执行权限批准后成功；uv 跨盘无法硬链接，自动改为完整复制，安装成功。一次辅助统计命令的引号转义导致 SyntaxError，改为 PowerShell here-string 后重新执行成功。
6. 完整安装未超过 10 分钟，无需精简集；依赖导入及模型检查通过。另外用 mock 验证黑体、CID、全失败异常三条路径，验证重复调用只注册一次，以及普通字体/CID 的 Paragraph 加粗回退。回退测试是故障注入，实际本机命中字体仍为 msyh。

**实际输出**（以下为对应命令的原样输出；安装部分为完成阶段输出）
```text
$ uv venv .venv
Using CPython 3.11.16
Creating virtual environment at: .venv
Activate with: .venv\Scripts\activate

$ uv pip install --python .venv/Scripts/python.exe -r requirements.txt --index-url https://mirrors.aliyun.com/pypi/simple/
Installed 118 packages in 19.44s
 + aiohappyeyeballs==2.7.1
 + aiohttp==3.14.5
 + aiohttp-retry==2.9.1
 + aiosignal==1.4.0
 + annotated-doc==0.0.5
 + annotated-types==0.8.0
 + anyio==4.16.0
 + attrs==26.1.0
 + bcrypt==5.0.0
 + build==1.6.1
 + certifi==2026.10.23
 + cffi==2.1.1
 + charset-normalizer==3.5.2
 + chromadb==1.5.9
 + click==8.5.1
 + colorama==0.4.6
 + cryptography==50.0.2
 + distro==1.9.0
 + durationpy==0.11
 + fastapi==0.162.2
 + fastembed==0.9.1
 + filelock==4.2.9
 + flatbuffers==25.12.19
 + frozenlist==1.8.0
 + fsspec==2026.9.0
 + googleapis-common-protos==1.76.0
 + grpcio==1.85.1
 + h11==0.16.0
 + hf-xet==1.8.0
 + httpcore==1.0.9
 + httpcore2==2.16.0
 + httptools==0.9.0
 + httpx==0.28.1
 + httpx2==2.16.0
 + huggingface-hub==1.33.0
 + idna==3.20
 + importlib-resources==7.1.0
 + jiter==0.19.0
 + jsonpatch==1.44
 + jsonpointer==3.10
 + jsonschema==4.26.0
 + jsonschema-specifications==2025.9.1
 + kubernetes==37.0.2
 + langchain-core==1.7.6
 + langchain-openai==1.7.4
 + langchain-protocol==0.0.21
 + langsmith==0.14.17
 + loguru==0.7.3
 + lxml==6.1.3
 + markdown-it-py==4.2.0
 + mdurl==0.1.2
 + mmh3==5.3.1
 + multidict==7.2.1
 + numpy==2.4.6
 + oauthlib==4.0.1
 + onnxruntime==1.31.1
 + openai==3.46.0
 + opentelemetry-api==1.45.2
 + opentelemetry-exporter-otlp-common==0.66b2
 + opentelemetry-exporter-otlp-proto-common==1.45.2
 + opentelemetry-exporter-otlp-proto-grpc==1.45.2
 + opentelemetry-proto==1.45.2
 + opentelemetry-sdk==1.45.2
 + opentelemetry-semantic-conventions==0.66b2
 + orjson==3.13.0
 + overrides==7.7.0
 + packaging==26.3
 + pdfminer-six==20260107
 + pdfplumber==0.11.10
 + pillow==13.0.0
 + propcache==0.5.4
 + protobuf==7.36.2
 + py-rust-stemmers==0.1.8
 + pybase64==1.5.1
 + pycparser==3.12
 + pydantic==2.14.3
 + pydantic-core==2.50.4
 + pydantic-settings==2.16.2
 + pygments==2.21.0
 + pypdfium2==5.15.1
 + pypika==0.51.1
 + pyproject-hooks==1.3.3
 + python-dateutil==4.0.0
 + python-docx==1.2.0
 + python-dotenv==1.2.4
 + python-multipart==0.0.33
 + pyyaml==6.0.3
 + rank-bm25==0.2.2
 + referencing==0.37.0
 + regex==2026.11.7
 + reportlab==5.1.0
 + requests==2.34.2
 + requests-oauthlib==2.0.0
 + requests-toolbelt==1.0.0
 + rich==15.0.0
 + rpds-py==2026.9.1
 + shellingham==1.5.4
 + sniffio==1.3.1
 + starlette==1.10.0
 + tenacity==9.2.1
 + tiktoken==0.14.0
 + tokenizers==0.23.3
 + tqdm==4.70.1
 + truststore==0.10.5
 + typer==0.28.0
 + typing-extensions==4.16.0
 + typing-inspection==0.4.5
 + tzdata==2026.6
 + urllib3==2.8.0
 + uuid-utils==0.19.0
 + uvicorn==0.54.0
 + watchfiles==1.3.0
 + websocket-client==1.9.2
 + websockets==17.4
 + win32-setctime==1.2.1
 + xxhash==4.0.2
 + yarl==1.25.1
 + zstandard==0.25.0

$ .venv/Scripts/python.exe -X utf8 tools/verify_font.py
实际字体: msyh
字体名: msyh
PDF: C:\Users\Lenovo\AppData\Local\Temp\font_test.pdf
写入: 简历匹配分析报告
Go 3 年 / 后端 / 匹配度 78 分
读回: 简历匹配分析报告
Go 3 年 / 后端 / 匹配度 78 分
PASS (exit 0)

$ .venv/Scripts/python.exe -X utf8 -c "import reportlab, chromadb, fastembed, langchain_openai; print('deps OK')"
deps OK

$ .venv/Scripts/python.exe -X utf8 -c "from app.schemas_match import MatchScore, ResumeKeyword, ResumeHighlight, Deduction, MatchReport; models = (MatchScore, ResumeKeyword, ResumeHighlight, Deduction, MatchReport); assert all(f.description and f.is_required() for m in models for f in m.model_fields.values()); print('schema OK'); print('5 models; 22 required fields with descriptions')"
schema OK
5 models; 22 required fields with descriptions
```

**结论**
- 达到 S0 判据：是；中文 PDF 往返、完整依赖导入和模型契约检查通过。
- 遗留问题：无 S0 阻塞项。S1-S7 尚未实施，停在 S0 等 review。

#### S0 补充：注释完善（2026-11-09）

**改动文件**
- 仅完善 `app/pdf_font.py`、`tools/verify_font.py` 的注释与 docstring；本节追加于原 S0 实施记录末尾、S1 之前。
- 未修改其他源码文件，函数签名、输出格式与执行逻辑保持不变。

**输入了什么**
- 本次用户逐项注释要求及 PRD §六红线⑧。
- 修改前按实际文件统计：pdf_font.py 为 11/68 = 16.18%，verify_font.py 为 10/61 = 16.39%（上一轮已补过注释，并非最初的 9% / 2%）。
- 统计口径：`wc -l` 为总行数，`grep -c '^[[:space:]]*#'` 为注释行数，docstring 不计入注释行。

**实现过程**
1. pdf_font.py 补充默认字体中文方块问题、非 Windows 的 CID 兜底、缓存与并发锁的原因、三种候选的顺序及 TTC 子字体参数、Paragraph 粗体变体映射、禁止静默降级 Helvetica 的原因，以及调用方统一入口说明。
2. verify_font.py 补充注册成功不代表真正嵌入、兄弟目录导入路径、中英文混排、坐标与行距、extract_text 读回、忽略空白防止误报及失败退出码的原因。
3. 修改后占比：pdf_font.py 从 16.18% 升至 21.79%（17/78）；verify_font.py 从 16.39% 升至 20.90%（14/67）。
4. 去除 docstring 后的 AST 与修改前一致；三个公开函数 docstring 覆盖 3/3。重跑原脚本仍 PASS。

**实际输出**（原样）
```text
$ wc -l app/pdf_font.py
78 app/pdf_font.py
$ grep -c '^[[:space:]]*#' app/pdf_font.py
17
$ wc -l tools/verify_font.py
67 tools/verify_font.py
$ grep -c '^[[:space:]]*#' tools/verify_font.py
14

$ .venv/Scripts/python.exe -X utf8 tools/verify_font.py
实际字体: msyh
字体名: msyh
PDF: C:\Users\Lenovo\AppData\Local\Temp\font_test.pdf
写入: 简历匹配分析报告
Go 3 年 / 后端 / 匹配度 78 分
读回: 简历匹配分析报告
Go 3 年 / 后端 / 匹配度 78 分
PASS (exit 0)
```

**结论**
- 达到判据：是；两文件注释行均 ≥15%，函数 docstring 覆盖率 100%，PDF 验证退出码为 0。
- 遗留问题：无；本次只完善注释，不推进 S1。

### S1｜JD 结构化抽取（0.5-1 天）

- **做什么**：新增 `app/job_parser.py`
  - `parse_job(jd_text: str) -> JobInfo`
  - `guard_job(jd_text, data) -> list[str]`：护栏 3 条
    - JD 空壳（must_have 与 responsibilities 都空）→ needs_review
    - 年限区间异常（上下限颠倒 / 无单位）→ needs_review
    - 技能列表里混进「后端 / 开发」这类岗位方向词 → 剔除并记录
  - `JobInfo` 字段：`title / company / required_years_min / required_years_max / education / must_have / nice_to_have / responsibilities`
- **验收**
  - 3 段真实 JD 文本 → 抽取结果人工核对（必填项不为空、技能是具体技术）
  - 护栏 3 条各有一条反例被拦下
- **禁止**：不许顺手写匹配逻辑

### S2｜简历侧输入统一（0.5 天）

- **做什么**
  - 复用 `file_parser.extract_text()` + `agent` 的抽取，封装 `parse_resume_pdf(pdf_bytes) -> (ResumeInfo, raw_text)`
  - PDF 特有处理：双栏、表格、扫描件（422）—— 已有能力，只做封装与边界校验
  - 输出 `raw_text` 必须保留，后续所有 evidence 都从它里面截取
- **验收**
  - 4 份测试简历（普通 docx / 表格 docx / 双栏 pdf / 扫描件）行为与现有接口一致
  - 扫描件 → 明确异常，不返回空内容
- **禁止**：不许改 file_parser 的内部逻辑（只封装）

### S3｜A 硬性条件打分（0.5 天，**纯规则、零模型调用**）

- **做什么**：`app/scoring_rules.py`
  - `score_hard(resume: ResumeInfo, job: JobInfo) -> tuple[int, list[Deduction]]`
  - 年限 / 学历 / 必备技能命中率，按 2.2 的规则实现
  - 每个 `Deduction` 必须带 `jd_evidence` 与 `resume_evidence`
- **验收**
  - **≥8 条单测**（无网络、无模型）：年限刚好够/差 1 年/差 3 年/JD 未写年限；学历达标/不达标/JD 未写；技能全命中/命中一半/零命中
  - 断言：扣分合计 = 40 - 得分
- **禁止**：不许在这里调 LLM

### S4｜B 语义相近打分（0.5-1 天，零模型调用）

- **做什么**：`app/scoring_semantic.py`
  - 复用 `rag` 的 `bge-small-zh-v1.5`（**不许换模型**，否则与 RAG 的分数不可比）
  - `score_semantic(resume_text, job) -> tuple[int, list[str]]`（返回分数 + 未覆盖职责列表）
  - 按 2.3 规则：每条 JD 职责取与简历各句的最高相似度，<0.6 记为未覆盖
- **验收**
  - 同一份简历对同一 JD 跑 3 次，分数完全一致（确定性）
  - 打印每条 JD 职责的最高相似度与命中句，人工核对 2 条是否合理
- **禁止**：不许调 LLM；不许换 embedding 模型

### S5｜C LLM 复核打分（1 天）

- **做什么**：`app/scoring_llm.py`
  - `score_llm(resume_text, job) -> tuple[int, list[SubScore], float]`（分数 / 分项理由 / 成本）
  - rubric 写进 prompt（12 + 10 + 8），要求结构化输出
  - 代码层强校验：任一分项 `evidence` 为空 → 抛 `ScoreEvidenceError`
  - 复用 `fallback.py` 的降级链与 `providers.UsageMeter` 成本统计
- **验收**
  - 5 条用例跑通，每条打印三项得分 + 理由 + 证据 + 成本
  - `evidence` 缺失用例必须触发 failed（构造一个反例验证）
  - 单次成本 < 0.002 元
- **禁止**：不许让模型自己决定总分或档位（档位由代码按分档规则算）

### S6｜汇总 + 缺口生成（0.5 天）

- **做什么**：`app/scoring.py`
  - `score_resume(resume, job, resume_text) -> MatchReport`
  - 合并 A/B/C、按分档规则出 `verdict`、汇总 `deductions`
  - 生成 `keywords`（按类别 + 出现次数）、`highlights`（主要项）
  - 生成 `missing` / `mismatches` / `suggestions`（**suggestions 禁止编造事实**：
    出现简历原文里没有的公司名/技术名 → 该条丢弃并记日志）
- **验收**
  - 3 份简历 × 2 个 JD = 6 组，输出完整 MatchReport，无字段缺失
  - 总分 = 三项之和；档位与分数一致
  - suggestions 里不出现简历未提及的技术名（用集合比对断言）
- **禁止**：不许在这一层再调模型（除了 S5 已算好的）

### S7｜PDF 报告渲染 + 接口 + 验收（1 天）

- **做什么**
  - `app/report_pdf.py`：`render_report(rep: MatchReport) -> bytes`，按第四节章节排版
  - `app/api.py` 增加 `POST /match/score`：`multipart`（PDF 文件 + jd_text 表单字段）
    - 返回 JSON（MatchReport）+ `report_id`
  - `POST /match/report/{report_id}` 或直接返回 `report_url`（v1：写临时目录 + 返回文件路径）
  - `tools/verify_match.py`：端到端验证脚本
- **验收**
  - 上传 PDF + JD → 返回 200，JSON 结构符合契约
  - 下载 PDF，用 `pdfplumber` 读回，**能读到总分、扣分表、关键字**（证明中文正常）
  - 3 份简历 × 2 个 JD → 6 份 PDF 全部生成，人工查看 1 份排版无重叠/截断
  - 扫描件 PDF → 422；JD 为空 → 422
- **禁止**：不许为了好看伪造数据；PDF 里每个数字必须来自 MatchReport

---

## 六、全局红线

```
① 每一分都必须有 evidence；无证据的分数 = bug
② 档位由代码算，不由模型定
③ 不许编造简历里没有的事实（suggestions / mismatches 都要校验）
④ 语义分必须用 RAG 同一个 embedding 模型（bge-small-zh-v1.5）
⑤ 测试集与语料分离：评测用简历不能进 RAG 语料
⑥ 每块完成即停，等 review；不许把 S1-S7 塞给 Codex 一次做完
⑦ **每块完成后，必须把实施记录「续写」到本文对应步骤小节末尾**（不许覆盖、不许新建文件）：
   - 写在哪个小节：S0 的记录就追加在「五、开发分块 → S0」这一小节**之后**
   - 必含四段：**改动文件** / **输入了什么** / **实现过程** / **实际输出（原样贴命令输出，不许改写数字）**
   - 若该块没有达到判据，记录里要写「未通过 + 原因 + 下一步」，不许只写做完了

**⑧ 代码必须写清晰的注释**（用户明确要求，每块都适用）：

```
· 每个模块顶部：这个文件干什么、为什么需要它
· 每个公开函数：一段 docstring，写清 参数 / 返回 / 抛什么异常
· 复杂逻辑旁边：写「为什么这么做」，不是复述代码在做什么
· 数值与阈值：必须注明出处（例如「0.6 来自 S4 的相似度门槛，低于此行视为不覆盖」）
· 踩过的坑：在踩坑的那行上方留一行注释，说明为什么不能那样写
· 禁止：整段代码零注释；也禁止把注释写成代码的中文翻译（如 # 循环 items）
验收线：新增文件注释行占比 ≥ 15%，且函数 docstring 覆盖率 100%
```

**实施记录模板（每块照抄）**：

```markdown
#### Sn 实施记录（YYYY-MM-DD）

**改动文件**
- 新增 `app/xxx.py`（N 行）
- 修改 `requirements.txt`（+M 行）

**输入**
- 参考文档：prd/Sn_任务书.md、PRD §x.y
- 外部输入：<测试文件 / JD 文本 / 命令>

**实现过程**
1. …（做了什么、为什么这么做）
2. …（遇到什么坑、怎么绕过的）

**实际输出**（原样，不许改写）
```
$ <命令>
<输出>
```

**结论**
- 达到判据：是/否
- 遗留问题：…
```
```

---

## 七、验收总表（全部做完才算功能完成）

| # | 判据 | 复现方式 |
|---|---|---|
| 1 | PDF 简历 → 结构化，扫描件 422 | `tools/verify_match.py` |
| 2 | A 硬性分 8 条单测全过 | `pytest` 或脚本 |
| 3 | B 语义分 3 次结果一致 | 脚本 |
| 4 | C 复核分 evidence 缺失 → failed | 反例脚本 |
| 5 | 总分 = A+B+C，档位与分数一致 | 断言 |
| 6 | 6 组用例生成 6 份 PDF，中文可读回 | `pdfplumber` 校验 |
| 7 | 单次成本 < 0.002 元 | `UsageMeter` |
| 8 | suggestions 无编造（集合比对 0 命中） | 断言 |

---

## 八、待你确认的 3 件事

1. **需求 5「你认为你需要的」** —— 我理解为「AI 认为候选人还缺什么、建议补什么」
   （输出为 `suggestions`，禁止编造经历）。理解对吗？
2. **PDF 报告的品牌位**：抬头要不要写「简历匹配分析报告」+ 你的名字/项目名？还是留空？
3. **JD 输入形态**：v1 只要纯文本粘贴，还是也要支持 JD 文件（PDF/截图 OCR）？
   我建议 v1 只做文本，OCR 明确不做。

---

## 九、开发顺序与时间

```
S0 字体与契约   0.5d
S1 JD 抽取      0.5-1d
S2 简历输入     0.5d
S3 硬性分       0.5d   ← 零模型，最好写、最好测，先拿它建立信心
S4 语义分       0.5-1d
S5 LLM 复核     1d
S6 汇总缺口     0.5d
S7 PDF+接口     1d
────────────────────
合计 5-6 天（每天 2-3h）
```

**为什么把 S3 放在 S5 前面**：纯规则部分零成本、零不确定性，
先把它测通，后面模型部分出问题时能立刻判断「是模型的问题还是汇总的问题」。
