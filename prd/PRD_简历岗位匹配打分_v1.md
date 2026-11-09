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
⑦ **每块完成后，实施记录写到 `docs/实施记录/<功能名>/Sn.md`（本地文件，不进 GitHub）**：
   - **PRD 只保留需求/打分模型/契约/分块定义**，不写实施过程、不写结果、不写价值段
   - 记录文件四段必填：**改动文件** / **输入了什么** / **实现过程** / **解决了什么（价值）**
   - 实际输出（命令原样输出）也放在记录文件里，便于复查
   - 若该块没有达到判据，记录里要写「未通过 + 原因 + 下一步」，不许只写做完了
   - 原因：`docs/` 已被 `.gitignore` 忽略 —— 实施记录属于过程材料，公开仓库只放 PRD 与代码

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

**⑨ 每块记录必须写「解决了什么」**（用户明确要求，每块都适用）：

```
不是复述「做了什么」——做了什么上面已经有了。
要写的是：这一块消除了哪一类风险、为什么不能省、没有它会怎样。

写法要求：
· 至少 3 条，每条都要带「不做的后果」
· 用业务/交付语言，不用技术流水账
  ✗ 「新增了 pdf_font.py，注册了 4 级回退」
  ✓ 「消除了 PDF 中文变方块的风险；不解决的话 S7 排完版才发现，整个报告要重做」
· 如果这一块只是重构/补注释，就写它降低了什么维护风险
· 禁止空话（如「提升了可维护性」），必须能落到具体后果上
```

**实施记录模板（每块照抄，写入 `docs/实施记录/<功能名>/Sn.md`，不写进本 PRD）**：

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

**解决了什么**（不是复述做了什么，而是这块消除了哪类风险、没有它会怎样）
1. …
2. …
3. …

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
