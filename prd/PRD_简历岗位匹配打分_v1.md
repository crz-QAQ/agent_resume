# PRD｜简历 × 岗位匹配打分（v1.3）

> 项目：resume-agent
> 作者：蔡荣祖 + Hermes
> **版本：v1.3（2026-11-11）** —— 新增 S1.5 / S3.5 修 D-001（举例项被当必备），变更见文末「十、变更记录」
> 状态：待评审（**评审通过才开工，不允许一次性交付整个 PRD**）
> 开发方式：**按 S0→S7 分块（含 S4.5），每块完成 → 你 review 消化 → 我再出下一块**
>
> ⚠️ **需求变动一律先改本 PRD**，评审通过后再改代码；不许先写代码再补 PRD。

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
| **A. 硬性条件** | 40 | 纯规则，确定性（含「未知≠0」处理） | 否 |
| **B. 语义相近** | 30 | 向量召回（bge-small-zh）+ 交叉编码器精判（两段式，v1.1 修订） | 否 |
| **C. LLM 复核** | 30 | 按 rubric 打分，必须给理由与原文证据 | 是 |
| 合计 | 100 | 三项相加，取整 | — |

### 2.2 A 硬性条件（40 分）明细

| 子项 | 分值 | 规则 |
|---|---|---|
| 工作年限 | 15 | 简历年限 ≥ JD 要求 → 满分；差 1 年 → 扣 7；差 ≥2 年 → 0 分 |
| 学历 | 10 | 达标 → 满分；未达标 → 0 分（JD 未写 → 直接满分，不猜） |
| 必备技能命中率 | 15 | `命中数 / 必备数 × 15`，四舍五入（**v1.3：分母按「概念族」去重**，见下） |

**v1.3 技能口径补充（修 D-001）**

```
JD 里的「并列举例」不等于「并列必备」。同一个概念的同族产品名，只能算一个要求：

① 抽取层（S1.5）：「熟练使用 DeepSeek、Qwen、OpenAI 等大模型产品的 API」
   → must_have 抽成 1 项上位概念「LLM 产品 API」，原文举例写进 jd_evidence
   ✗ 不许拆成 DeepSeek / Qwen / OpenAI 三条并列必备

② 打分层（S3.5）：即使上游仍拆成多项，也要按「概念族」去重后再算分母
   LLM_PROVIDERS = {deepseek, qwen, openai, gpt, chatgpt, claude, gemini,
                    通义, 文心, 豆包, kimi, glm, 智谱, llama, mistral}
   → 族内 N 项 → 视为 1 个必备项；简历命中族内任一项 → 该项算命中
   → 分母 = 去重后的必备项数（避免举例项稀释真实必选项）

③ 报告可追溯：命中族内哪一项、属于哪个族，必须能在明细里看出（不许只给一个分数）

红线：放宽只能来自「同一概念的同族归一」；
     ✗ 不许把真缺口（如 JD 要 Go、简历只有 Python）也放过
```

**为什么必须两层都做**：S1.5 治本（语义正确、报告能解释）；S3.5 兜底（
上游已抽错的历史数据、或族内新写法没进提示词时，也不会冤枉候选人）。

### 2.3 B 语义相近（30 分）—— **v1.1 修订：两段式**

**为什么改**（v1.0 单靠向量余弦的实测缺陷）：

```
同一份「Go 订单系统」简历，对 4 条 JD 职责的余弦相似度：
  开发订单接口              0.785   ← 真相关
  负责分布式数据库内核开发   0.711   ← 完全无关，只差 0.07
  负责芯片验证              0.623   ← 完全无关
  负责餐厅后厨管理          0.579   ← 完全无关
→ 中文技术句都是「负责 + 技术名 + 开发/优化」的句式，小向量模型判不出领域差异，
  无关简历也能拿到 14~21 分语义分，报告不可信；调阈值救不了（0.65 仍误判 2/3）
```

```
做法（两段式）：
  ① 向量召回：每条 JD 职责对简历全部句子算余弦，取 top-RERANK_TOP_K(3) 候选句
  ② 交叉编码器精判：对候选句打重排分（logit），取最高 → rerank_score
  ③ 覆盖判定：covered = rerank_score >= RERANK_THRESHOLD(0.0)
              （实测真相关 +5.53，无关 -0.86 / -1.10 / -1.70 —— 0 点天然分界）
  ④ 得分：score = int(30 × Σ(已覆盖条目的余弦) / 职责总数 + 0.5)
              · 分母是「职责总数」，未覆盖的按 0 进分母（覆盖率必须进分母）
              · 余弦只提供「有多像」的幅度，重排只提供「算不算覆盖」的判定
              · 不允许把重排 logit 直接当 0-1 相似度乘 30（会引入 sigmoid 温度等魔法参数）
  ⑤ 未覆盖的职责 → 写入 uncovered，S6 用它生成「缺失项」
```

**取这两段的原因**：两个模型分工，各自做的事都有明确语义，阈值只有 3 个，
且都能用实测数字解释；「覆盖率低但有一句很像」不再拿高分（v1.0 的 22 分 → 现 4 分）。

**降级规则**：重排模型不可用（首次下载失败等）→ 回退余弦阈值 0.6，
并在 reasons 记「重排模型不可用，已回退余弦阈值0.6，结果偏宽松」，状态至少 `needs_review`。**禁止静默降级**。

**内部数据结构变更**：`ItemSim` 增加 `rerank_score: float`；
`covered` 由 `rerank_score` 决定（不再由余弦阈值决定）；`best_sim` 仍保留余弦最高值。

### 2.4 C LLM 复核（30 分）

**rubric（写死在 prompt 里，不让模型自由发挥）—— v1.1 修订：三项改为与 A 项不重叠**

| key | 维度 | 分值 | 打分依据 |
|---|---|---|---|
| `stack_depth` | 技术栈深度 | 12 | JD 必备技能在简历里是「主导/独立负责」还是「了解/用过」；同一条技能按深度分档 |
| `domain_fit` | 业务场景相似度 | 10 | 候选人做过的业务与岗位业务是否同类（订单↔订单 vs 餐饮↔数据库） |
| `proof_strength` | 成果可信度 | 8 | 是「有规模/有数字/有结果」的成果，还是只有职责罗列 |

> **为什么改（v1.0 → v1.1）**：v1.0 写的是「项目复杂度 12 / 技术栈匹配 10 / 经历相关性 8」，
> 与 A 项（必备技能命中率 15）严重重复——同一个「会不会 Go」被算了两次，等于重复扣分。
> v1.1 把三项改为「深度 / 场景 / 成果」：A 项只判断「有没有」，C 项判断「有多深、像不像、可不可信」，
> 两层不重叠。S5 已按 v1.1 实现。

**硬性要求**：每一项都必须给出 `reason`、`jd_evidence`（JD 原文）、`resume_evidence`（简历原文）。

- 任一 `evidence` 去空白后 < 4 字 → 该项**直接判 failed**（没有依据不许给分）
- `evidence` 在对应原文里找不到 → 不 failed，但状态降级为 `needs_review` 并记录字段名
- 模型自报的「总分」「档位」**一律忽略**；总分只由 `sum(三项)` 得来

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

## 三、输出契约（JSON，**v1.2 已与实现对齐**）

```python
class MatchScore(BaseModel):
    total: int                        # 0-100
    breakdown: dict                   # 见下方说明（v1.2：允许嵌套结构化明细）
    # breakdown = {
    #   "hard": 33, "semantic": 21, "llm": 25,          ← 三项分值（int）
    #   "hard_detail": {"years": 15, "education": 10, "skills": 8},
    #   "semantic_detail": 21,                            ← 冗余保留，兼容旧消费方
    #   "llm_detail": {"stack_depth": 9, "domain_fit": 10, "proof_strength": 6},
    # }

class ResumeKeyword(BaseModel):
    name: str                         # 关键字文本，如 "Go"
    category: str                     # **中文**：语言 / 框架 / 中间件 / 数据库 / 云 / 工具 / 其他
    count: int                        # 在简历原文中出现的次数

class ResumeHighlight(BaseModel):
    label: str                        # 姓名 / 工作年限 / 学历 / 最近公司 / 技术栈 / 成果亮点
    value: str

class Deduction(BaseModel):
    item: str
    type: str                         # missing_skill / missing / years_short / years_unknown /
                                      # education / education_unknown / ...
    deduct: int                       # 缺口类为 0（不是扣分，是缺口）
    jd_evidence: str                  # JD 原文；必填
    resume_evidence: str              # 简历原文；无则写「简历未体现」
    severity: str                     # high / medium / low

class SemanticItem(BaseModel):        # v1.2 新增（S4.5 的 per_item，PDF 要用）
    item: str                         # JD 的一条职责
    best_sim: float                   # 余弦最高值
    rerank_score: float               # 交叉编码器重排分（logit）
    best_sentence: str                # 命中的简历原句
    covered: bool                     # rerank_score >= 0

class MatchReport(BaseModel):
    score: MatchScore
    verdict: str                      # 高度匹配 / 匹配 / **勉强** / 不匹配（v1.2 对齐实现）
    keywords: list[ResumeKeyword]     # 【需求3】简历关键字提取
    highlights: list[ResumeHighlight] # 【需求4】简历主要项
    deductions: list[Deduction]       # 【需求2】扣分理由（只来自硬分）
    missing: list[Deduction]          # 【需求8】JD 要、简历没有（v1.2：由 str 改为 Deduction）
    mismatches: list[Deduction]       # 【需求8】对不上 / 无法确认（v1.2：同上）
    suggestions: list[str]            # 【需求5】带 [LLM] / [缺口] 来源前缀
    semantic_items: list[SemanticItem]# v1.2 新增：语义覆盖明细（PDF 第 4 节用）
    status: str                       # v1.2 新增：ok / needs_review / failed
    reasons: list[str]                # v1.2 新增：状态原因（失败/降级的说明）
    meta: dict                        # model / prompt_version / cost_yuan / elapsed_ms /
                                      # resume_chars / jd_chars / generated_at
```

**契约必须与代码一致**：本节的字段名、类型、取值，与 `app/schemas_match.py` + `app/match_report.py` 的实现一致；
若后续实现要改契约，**先改本节再改代码**。

**分档规则**（写死在代码里，不让模型定）：

```
≥85 高度匹配   ≥70 匹配   ≥55 勉强   <55 不匹配
```

---

## 四、PDF 报告结构（**需求 7、8 的落点**，v1.2 修订）

**页数**：v1.2 **不设页数上限**——本版本接受「特别详细的分析」，内容完整优先于篇幅。
中文字体必须内嵌（`pdf_font.get_cjk_font()`，4 级回退，绝不静默降级 Helvetica）。

**文件命名**：`简历评分_<简历文件名主干>_<岗位标题或JD前8字>_<YYYYMMDD-HHMM>.pdf`

### 4.1 章节顺序（固定）

| # | 章节 | 内容 | 数据来源（**每个数字都必须来自 MatchReport**）|
|---|---|---|---|
| 1 | 抬头 | 简历文件名 / 岗位标题 / 生成时间 / **状态标签** | `meta`、`job.title`、`status` |
| 2 | **总分** | 大字总分 + 档位 + 三项得分条（硬性 40 / 语义 30 / LLM 30） | `score.total`、`verdict`、`score.breakdown` |
| 3 | **扣分理由** | 表格：扣分项 / 扣几分 / JD 依据 / 简历现状 / 严重度 | `deductions` |
| 4 | **语义覆盖明细**（v1.2 新增）| 表格：JD 职责 / 余弦 / 重排分 / 是否覆盖 / 命中的简历原句 | `semantic_items` |
| 5 | **简历关键字** | 按类别分组标签 + 出现次数 | `keywords` |
| 6 | **简历主要项** | 姓名 / 年限 / 学历 / 最近公司 / 技术栈 / 成果亮点（各一行，缺失则不印该行）| `highlights` |
| 7 | **对比表** | 左 JD 要求 / 中判定 / 右简历情况（见 4.2 拼装规则）| `deductions` + `mismatches` + `semantic_items` |
| 8 | **缺失与不符** | 两段：缺失（`missing`）/ 不符或待确认（`mismatches`）| `missing`、`mismatches` |
| 9 | **建议补充** | 带来源标签，最多 10 条 | `suggestions` |
| 10 | 页脚（每页）| 免责 + 审计小字（见 4.4）| `meta` |

### 4.2 对比表拼装规则（v1.2 新增，防止自由发挥）

```
行来源与顺序：
  1) deductions 里 type ∈ {years_short, years_unknown, education, education_unknown}
     → 左列 = jd_evidence，右列 = resume_evidence，判定见下表
  2) deductions 里 type == missing_skill
     → 左列 = 「必备技能：<item>」，右列 = 「简历未体现」，判定 ✗
  3) semantic_items 里每条职责一行（已覆盖的进 ✓ 行，未覆盖的进 ✗ 行）

判定符号（四态，不许只用三态）：
  ✓  达标 / 已覆盖
  ✗  不符 / 未覆盖
  ?  无法确认（**专用于 years_unknown / education_unknown**，即简历没写，不是不合格）
  —  JD 未提该项要求（如 JD 没写学历 → 不印这一行，避免制造无意义的 ✗）
```

### 4.3 「待确认」必须与「不符」分开（v1.2 强调）

```
简历没写年限 → years_unknown → 对比表判 ?，缺失与不符那节归入「待确认（需人工补充信息）」
年限 2 年 vs 要求 3 年 → years_short → 对比表判 ✗，归入「不符」

✗ 禁止把 unknown 印成「不符合」：那等于替候选人下了错误结论。
```

### 4.4 页脚（免责 + 审计）

```
免责：本报告由 AI 生成，分数仅作初筛参考；关键结论请人工复核。

审计（8pt 小字，一行）：
  模型 <meta.model> · prompt <meta.prompt_version> · 成本 ¥<meta.cost_yuan>
  · 耗时 <meta.elapsed_ms>ms · 生成于 <meta.generated_at>
```

### 4.5 排版硬要求

```
· 所有长文本用 Paragraph（自动换行），禁止 drawString 画长串
· 章节标题与首个表格不断页（keepWithNext / KeepTogether）
· 表格 repeatRows=1（跨页重复表头）
· 状态 needs_review → 黄底告警条「本报告存在待确认项，请人工复核」；
  failed → 红底「部分环节失败，分数仅供参考」
· 生成 3 页以上必须能正常翻页，不许内容溢出画布（用 platypus，不要手工 y 坐标）
```

**按列宽分档截断（v1.2.1 修订：单一 120 字上限会让窄列挤成 8 行）**：

| 列宽 | 典型字段 | 截断字数 |
|---|---|---|
| **≤55mm** | 语义明细「JD 职责」（55mm）、「必备技能：x」| **40 字** |
| **56–120mm** | 对比表「JD 要求」（75mm）与「简历情况」（84mm）、扣分表「JD 依据」「简历现状」| **80 字** |
| **>120mm** | 语义明细「命中的简历原句」（170mm）| **120 字** |

> v1.2.1 补充两点：
> ① 分档按**区间**判定，不留缝隙（旧表述「95–110mm」漏掉 84mm 的对比表右列 → 用了 120 字上限、挤成 5 行）。
> ② 表内所有列统一走 `_limit_for_width(列宽)` 查表，不许手挑档位；**以后加列自动套用**。

**扣分数字统一带负号**：「扣几分」列一律印 `-5` 而不是 `5`（缺口类 `deduct=0` 印 `—`，避免让人以为「扣了 0 分」）。

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

### S1.5｜JD 抽取：并列举例 → 上位概念（0.5 天）—— **v1.3 新增（修 D-001）**

- **起因**：JD 写「熟练使用过 DeepSeek、Qwen、OpenAI 等大模型产品的 API」，
  S1 抽成 3 条并列必备 → 只用过 DeepSeek 的候选人硬性技能 8/15，白扣 7 分。
  实测证据见 `docs/实施记录/简历打分/已知缺陷.md` D-001。
- **做什么**：改 `app/prompts.py` 的 `SYSTEM_JOB`，新增一条硬规则
  - 出现「等 / 例如 / 诸如 / 如 A、B、C / A、B、C 等」这类**并列举例**时：
    抽取「上位概念」作为 1 条 must_have（如「LLM 产品 API」「消息队列」「云厂商」）
  - 举例的具体名称不进入 `must_have`；原文整句写入该条的 `jd_evidence`
  - 反例（必须避免）：把举例拆成 N 条并列必备、或把上位概念写成空泛词（「技术栈」）
- **验收**
  - 新增用例：上述 JD → `must_have` 只有 1 条上位概念（不含 Qwen/OpenAI）
  - 回归：原 S1 的 6/6 用例仍通过（JD-A/JD-B 的 must_have 不变）
  - 对照用例：JD 写「必须会 Go 和 MySQL」（无「等」字）→ 仍是 2 条并列，**不许误合并**
- **禁止**：不许为了过用例把「无等字的并列」也合并；不许改 S3 的算法

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

### S3.5｜必备技能按「概念族」去重（0.5 天，纯规则）—— **v1.3 新增（修 D-001）**

- **起因**：同上。S1.5 治抽取；本块保证**即使上游仍拆成多项**也不会冤枉候选人。
- **做什么**：改 `app/scoring_rules.py`
  - 新增模块级常量 `SKILL_FAMILIES: dict[str, set[str]]`（族名 → 归一化后的成员集合），
    首批至少包含：
    ```
    llm_providers = {deepseek, qwen, openai, gpt, chatgpt, claude, gemini,
                     通义, 文心, 豆包, kimi, glm, 智谱, llama, mistral}
    message_queue = {kafka, rabbitmq, rocketmq, pulsar}
    vector_db     = {chroma, chromadb, milvus, faiss, qdrant, weaviate, pgvector}
    cloud         = {aws, 阿里云, 腾讯云, 华为云, azure, gcp}
    ```
  - `must_have` 归一化后：
    ① 把同族的多项合并为 1 项（分母去重）
    ② 简历命中族内任一项 / 或简历技能落在同族 → 视为该族命中
  - 去重逻辑必须**确定性**（同一族取哪一项作为代表要写死：取族名，不取成员名）
  - 报告里该项的 `item` 写成 `必备技能：<族名>（举例：A、B、C）`，让人看出为什么算命中
- **验收**
  - 用例：`must_have=[DeepSeek, Qwen, OpenAI]` + 简历只写 DeepSeek → 技能 **15/15**，无扣分
  - 用例：`must_have=[DeepSeek, Qwen, Go]` + 简历只有 DeepSeek → 分母 2，技能 8/15（Go 照扣）
  - 用例：`must_have=[Go]` + 简历只有 Python → **仍扣满**（不许因为族表而放过真缺口）
  - 回归：S3 原有 9 条单测全过；扣分守恒 `Σdeduct == 40 - score` 仍成立
- **禁止**：不许把非同族项合并（如把 Go 与 Python 并成「编程语言」）；
  不许用模型判断「这两个是不是同族」（族表必须是代码里的确定性数据）

### S4｜B 语义相近打分（0.5-1 天，零模型调用）

- **做什么**：`app/scoring_semantic.py`（**v1.0 初版，仅向量余弦**；精判由 S4.5 补）
  - 复用 `rag` 的 `bge-small-zh-v1.5`（**不许换模型**，否则与 RAG 的分数不可比）
  - `score_semantic(resume_text, job) -> SemanticScore(score, per_item, uncovered)`
  - 简历切句（按 。！？；换行，丢弃 <6 字短句）→ 每条 JD 职责取最高余弦 → 低于门槛记未覆盖
- **验收**
  - 同一份简历对同一 JD 跑 3 次，分数完全一致（确定性）
  - 打印每条 JD 职责的最高相似度与命中句，人工核对 2 条是否合理
- **禁止**：不许调 LLM；不许换 embedding 模型

### S4.5｜B 语义相近加「重排精判」（0.5 天，零 LLM 调用）—— **v1.1 新增**

- **起因**：S4 上线后实测发现向量余弦分不开「同句式不同领域」——
  「负责分布式数据库内核开发」对 Go 订单简历拿到 0.711（真相关才 0.785），
  无关简历也能拿 14~21 分语义分。详见 2.3 的实测表。
- **做什么**：`app/scoring_semantic.py` 改为两段式
  - 余弦取 top-3 候选句 → 对候选句跑 cross-encoder 重排 → `covered = rerank_score >= 0`
  - `score = int(30 × Σ(已覆盖条目的余弦) / 职责总数 + 0.5)`（未覆盖按 0 进分母）
  - `ItemSim` 增加 `rerank_score` 字段；重排模型复用手写版已有的 `rag._get_reranker`
  - 重排不可用时降级到余弦 0.6 阈值，并在 `reasons` 明确记录，状态至少 `needs_review`
- **验收**
  - 「1 条真相关 + 3 条无关职责」用例：3 条无关的 `covered` 必须全 False，总分 ≤10
  - 3 条真相关用例：`covered` 全 True，总分 ≥15
  - 同一输入 3 次一致（含 `rerank_score`）
  - 单次耗时打印且 <3 秒；`check_match_report.py` 重跑仍通过
- **禁止**：不许换 embedding / reranker 模型；不许把重排 logit 直接当 0-1 相似度乘 30；
  不许回退「覆盖率进分母」的修正

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
  1. `app/report_pdf.py`：`render_report(rep: MatchReport, job: JobInfo) -> bytes`
     - 按第四节 4.1~4.5 排版（含语义覆盖明细、对比表四态、页脚审计小字、状态告警条）
     - 中文字体只能用 `pdf_font.get_cjk_font()`；每个数字必须取自 `MatchReport`
  2. `app/api.py` 新增 3 个接口（v1.2 定死）：

```
POST /match/score          multipart: file(简历 PDF/docx) + jd_text(表单字段)
  → 200 JSON: { report_id: "<uuid>", report: <MatchReport> }
  → 422  文件类型不支持 / 扫描件 / jd_text 为空或 <20 字
  → 413  文件 > 10MB
  → 503  模型全部不可用（fallback 链耗尽）
  → 500  内部错误（含 PDF 渲染失败）

GET  /match/report/{report_id}
  → 200 application/pdf（Content-Disposition: attachment; filename="<4.1 的命名>"）
  → 404  report_id 不存在或已过期

GET  /match/stats
  → 200 { cached_reports: n, ttl_minutes: 30 }   ← 便于本地排查，不暴露任何简历内容
```

  3. **`report_id` 与存储**
     - `report_id = uuid4().hex`；PDF 写 `<项目根>/reports/<report_id>.pdf`
     - **TTL 30 分钟**：超过即删（防止简历在磁盘长期驻留）；`reports/` 必须进 `.gitignore`
     - 简历原文**不落盘**，只在内存里走完流程；落盘的只有最终 PDF
  4. **监听地址**：默认 `127.0.0.1`，v1 **不做鉴权**（仅本机可用）；
     README 必须写明「不要直接暴露公网」
  5. `tools/verify_match.py`：端到端脚本（3 份简历 × 2 个 JD → 6 份 PDF）

- **验收**
  - 上传 PDF + JD → 200，JSON 结构符合第三节契约（字段齐全、类型正确）
  - 下载 PDF，`pdfplumber` 读回，**必须能读到**：总分数字、扣分项、语义明细里的某条 JD 职责、审计小字里的 `prompt` 版本
  - 6 份 PDF 全部生成；人工看 1 份：无重叠、无截断、表格换行正常
  - 构造 `years_unknown` 用例 → PDF 对比表该行是 `?` 而不是 `✗`
  - 扫描件 → 422；JD 空 → 422；11MB 文件 → 413
  - `report_id` 过期后 GET → 404
  - `reports/` 目录在 `.gitignore` 里（`git check-ignore` 验证）

- **禁止**
  - 不许为了好看伪造数据（每个数字必须来自 MatchReport）
  - 不许把简历原文写进磁盘日志
  - 不许把 `unknown` 印成「不符合」
  - 不许静默降级字体（中文出方块 = 事故）


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

**⑦·五 文档归属（用户明确要求，务必遵守）**：

```
公开（推 GitHub）：  prd/PRD_<功能>_v1.md    ← 只放需求、打分模型、契约、分块定义、红线
仅本地（不推）：      docs/任务书/<功能>/Sn_任务书.md     ← 每块给 Codex 的任务书
                     docs/实施记录/<功能>/Sn.md          ← 每块的实施记录（五段）
                     docs/教材、docs/修改记录、docs/规划   ← 学习与过程材料
规则：prd/ 目录下不许出现任务书、实施记录；任务书从开工起就写在 docs/ 下
```

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
- 参考文档：docs/任务书/<功能名>/Sn_任务书.md、PRD §x.y
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
| 2.1 | **举例项归一**：JD「DeepSeek、Qwen、OpenAI 等」+ 简历只写 DeepSeek → 技能 **15/15** | 脚本（D-001 用例）|
| 2.2 | **真缺口仍扣**：JD 要 Go、简历只有 Python → Go 照扣满，不许被族表放过 | 脚本 |
| 3 | B 语义分 3 次结果一致 | 脚本 |
| 3.5 | **B 语义分：无关职责 `covered` 必须全 False（重排精判生效）** | 1 真相关 + 3 无关用例，断言总分 ≤10 |
| 4 | C 复核分 evidence 缺失 → failed | 反例脚本 |
| 5 | 总分 = A+B+C，档位与分数一致 | 断言 |
| 6 | 6 组用例生成 6 份 PDF，中文可读回 | `pdfplumber` 校验 |
| 6.1 | PDF 含**语义覆盖明细**节（能读回某条 JD 职责 + 重排分）| `pdfplumber` 逐节校验 |
| 6.2 | PDF 页脚含**审计小字**（模型 / prompt 版本 / 成本）| 文本 grep |
| 6.3 | `years_unknown` 用例在对比表是 `?` 而非 `✗` | 断言 |
| 7 | 单次成本 < 0.002 元 | `UsageMeter` |
| 8 | suggestions 无编造（集合比对 0 命中） | 断言 |
| 9 | 接口错误码语义正确（413/422/404/503）| `tools/verify_match.py` |

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
S0   字体与契约    0.5d
S1   JD 抽取       0.5-1d
S1.5 举例→上位概念  0.5d   ← v1.3 新增（修 D-001）
S2   简历输入      0.5d
S3   硬性分        0.5d   ← 零模型，最好写、最好测，先拿它建立信心
S3.5 技能族去重    0.5d   ← v1.3 新增（修 D-001）
S4   语义分        0.5-1d
S4.5 语义重排精判  0.5d   ← v1.1 新增：修 S4 的假阳性（S7 之前必须做）
S5   LLM 复核      1d
S6   汇总缺口      0.5d
S7   PDF+接口      1-1.5d  ← v1.2：加语义明细节、四态对比表、审计页脚、3 个接口
────────────────────────
合计 7.5-8.5 天（每天 2-3h）
```

**为什么把 S3 放在 S5 前面**：纯规则部分零成本、零不确定性，
先把它测通，后面模型部分出问题时能立刻判断「是模型的问题还是汇总的问题」。

**为什么 S4.5 排在 S7 前面**：S7 要出 PDF 报告给人看。语义分假阳性会让一份不匹配的简历
显示 14~21 分语义分，报告一旦发出去就是硬伤——所以必须在排版之前修掉。

## 十、变更记录

| 版本 | 日期 | 变更 | 原因 |
|---|---|---|---|
| v1.0 | 2026-11-09 | 初版：100 = 硬性 40 + 语义 30（向量余弦）+ LLM 30 | — |
| v1.1 | 2026-11-10 | ① 语义分改两段式（余弦召回 + 重排精判），新增 S4.5 分块<br>② 语义分「覆盖率进分母」<br>③ LLM rubric 三项改为 `stack_depth`/`domain_fit`/`proof_strength`<br>④ 硬性分明确「未知≠0」（`years_unknown` / `education_unknown` 单独类型）<br>⑤ 文档归属：PRD 推 GitHub，任务书与实施记录留 `docs/` 本地 | ① 余弦分不开同句式不同领域（0.711 vs 0.785），无关简历也能拿高分<br>② 「6 条只对上 1 条」与「6 条全对上」同分，不合理<br>③ v1.0 的「技术栈匹配」与 A 项必备技能重复计分<br>④ 未知被当成 0 会误判候选人<br>⑤ 过程材料不该进公开仓库 |
| v1.2 | 2026-11-10 | ① **§三 契约与实现对齐**：`missing`/`mismatches` 由 `list[str]` 改为 `list[Deduction]`；`breakdown` 允许嵌套明细；verdict 文案统一为「勉强」；keyword.category 改中文七类；新增 `status`/`reasons`/`semantic_items`（含 `SemanticItem` 模型）<br>② **§四 PDF 结构重写**：去掉页数上限（内容优先）；新增「语义覆盖明细」节；新增对比表拼装规则与四态符号（✓/✗/?/—）；明确「待确认」与「不符」必须分开；页脚加审计小字；补排版硬要求与状态告警条<br>③ **§五 S7 接口定死**：`POST /match/score`、`GET /match/report/{id}`、`GET /match/stats`，含 413/422/404/503 语义、`report_id` 用 uuid、TTL 30 分钟、只落盘 PDF 不落盘简历原文、默认监听 127.0.0.1 且 v1 不鉴权<br>④ §七 验收加 6.1/6.2/6.3/9 四条 | ① 契约漂移会让 S7 的 JSON 与文档不一致，外部调用方无法信任<br>② 报告要给人看：语义明细是「可解释性」的核心证据，缺了等于只有分数没有依据<br>③ 接口不写死，Codex 会自由发挥（路径/错误码/存储位置各写一套）<br>④ 新增内容必须有对应验收，否则等于没做 |
| v1.2.1 | 2026-11-10 | ① §4.5 截断改为**按列宽分档**（窄列 40 字 / 中列 80 字 / 宽列 120 字）<br>② 「扣几分」列统一带负号，缺口类印 `—` | S7a 压力用例实测：单一 120 字上限让 55mm 窄列挤成 8 行，观感差；扣分显示正数容易被误读成「得了 5 分」 |
| v1.3 | 2026-11-11 | ① §2.2 必备技能口径补充：**并列举例 ≠ 并列必备**，同族归一<br>② 新增 **S1.5**（抽取层：举例 → 上位概念）<br>③ 新增 **S3.5**（打分层：`SKILL_FAMILIES` 概念族去重，分母按族算）<br>④ §九 工期 +1 天（合计 7.5-8.5 天） | **D-001 实测**：JD「DeepSeek、Qwen、OpenAI 等」+ 简历只写 DeepSeek → must_have 被拆成 3 条，技能 8/15 白扣 7 分。JD 语义是「用过任意一个」，需求量被放大 3 倍、分母错了。不修则「完全符合岗位意图的候选人被判不匹配」 |
