---
name: resume-job-matcher
description: Score a resume against a job description with evidence.
version: 0.1.0
author: 蔡荣祖 (cairongzu), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Resume, Job Match, Scoring, FastAPI, Evidence]
    related_skills: [match-scoring-design]
---

# Resume × Job Matcher

给一份简历文件和一段岗位 JD，输出 **100 分制匹配报告**：硬性(40) + 语义(30) + LLM复核(30)，
每条扣分都带 JD 原文与简历原文依据，并可导出 PDF。分档由代码计算，模型不参与。

不做什么：不 OCR 扫描件、不做批量多岗位、不改写简历正文（只给建议）。

## When to Use

- 用户上传/给出一份简历，同时给出岗位 JD，问「匹配度多少 / 差在哪 / 要不要投」
- 需要一份可以转发的 PDF 初筛报告
- 需要在自动化流程里对多份简历做初筛（每次调用一份简历 + 一份 JD）

**Don't use for：**

- 纯图片/扫描件 PDF（无文本层 → 会返回「无法解析」，需要 OCR，本 skill 不做）
- 只给简历没有 JD（没有对照基准，分数无意义）
- 需要最终录用决策（报告定位是初筛参考，页脚有免责声明）

## Prerequisites

1. 仓库已 clone 且装好依赖：`python -m venv .venv` → `.venv/Scripts/pip install -r requirements.txt`
   （Windows 用 `Scripts/`，macOS/Linux 用 `bin/`）
2. `.env` 里有 `DEEPSEEK_API_KEY`（`cp .env.example .env` 后填）—— LLM 复核那 30 分需要它
3. 首次运行会下载本地模型（embedding 约 100MB、重排模型约 1GB）。国内失败时设
   `HF_ENDPOINT=https://hf-mirror.com`（镜像里已内建）
4. 若用 `--api` 模式，服务需已启动：`tools/start_server.bat` 或
   `terminal(command="python -m uvicorn app.api:app --host 127.0.0.1 --port 8000")`

## How to Run

**路径 1（推荐，零服务依赖）**：

```
terminal(command="python skills/resume-job-matcher/scripts/score.py --local \
  --resume <简历.pdf|docx> --jd <jd.txt> --out <报告.pdf>", timeout=300)
```

`--jd` 可直接传文本文件，也可用 `--jd-text "整段 JD"` 直接贴。

**路径 2（服务已在跑）**：

```
terminal(command="python skills/resume-job-matcher/scripts/score.py --api \
  --resume <简历.pdf> --jd <jd.txt> --base-url http://127.0.0.1:8000", timeout=300)
```

**加 `--json`** 输出机器可读的 MatchReport JSON（字段与 `prd/PRD_简历岗位匹配打分_v1.md` §三 一致），
便于上层程序消费；不加则打印人读摘要。

## Output

```
① 终端人读版：总分 / 档位 / 三项得分 / 扣分明细 / 缺口 / 前 3 条建议
② --json：MatchReport 原样 JSON
③ --out：PDF 绝对路径（同时打印）
④ 退出码：0 成功 · 2 输入错误（文件不存在/格式不支持/扫描件/JD<20字）· 3 服务不可用 · 4 内部错误
```

## Procedure

1. **确认输入**：简历是 PDF/docx（不是图片扫描件），JD ≥ 20 字。
   判据：文件存在且非空、`--jd` 文件能被 UTF-8 读取。
2. **跑一次 `--local`**：读退出码。
   判据：退出码 0，且终端里有「总分 / 档位 / 硬性 / 语义 / LLM」五个要素。
3. **核对可追溯性**：任取一条扣分，看它的 `jd_evidence` 与 `resume_evidence` 是否真在原文里出现过。
   判据：能在 JD 与简历文本里各自找到该片段；找不到 = 这条结论不可信，需人工复核。
4. **看 status**：`ok` 可直接用；`needs_review` 要在交付时说明原因（常见：简历含「至今」→ 年限待确认）。
5. **导出 PDF（可选）**：`--out <path>`。
   判据：文件 > 8KB，且用 `pdfplumber` 能读回「总分」「语义覆盖明细」「本报告由 AI 生成」。

## How to Read the Result

| 字段 | 含义 | 注意 |
|---|---|---|
| `score.total` | 0-100 | 三项之和，代码算 |
| `verdict` | 高度匹配(≥85) / 匹配(≥70) / 勉强(≥55) / 不匹配 | 由代码按分档算，模型说了不算 |
| `deductions` | 硬性分扣分项 | 每条带 JD 原文 + 简历现状 + 严重度 |
| `semantic_items` | 每条 JD 职责的覆盖判定 | `covered` 由重排分 ≥0 决定，附带余弦与命中句 |
| `missing` | JD 要、简历没有 | `deduct=0`（缺口不是扣分）|
| `mismatches` | 对不上或无法确认 | `years_unknown`/`education_unknown` = **简历没写**，不是不合格 |
| `suggestions` | 改写建议 | 带 `[LLM]` / `[缺口]` 来源前缀；不许编造经历 |

## Pitfalls

1. **扫描件 PDF 会被拒绝**（退出码 2）。这是护栏不是 bug：没有文本层时任何抽取都是瞎猜。
2. **JD 少于 20 字会被拒绝**。太短的 JD 抽不出职责，语义分恒为 0。
3. **文件 > 10MB 返回 413**（`--api` 模式）。
4. **`--api` 模式的 PDF 只有 30 分钟有效期**（TTL），过期再取会 404，需要重跑。
5. **首次调用慢（30-90 秒）**：在下载/加载本地模型，第二次起约 6-10 秒一份。
6. **语义分可能为 0 是正常的**：简历只写公司名与职位、没有职责描述时，JD 职责找不到对应文本。
   这时低分是诚实的，不要「调参数让它好看一点」。
7. **服务默认只监听 127.0.0.1 且无鉴权**：不要把端口暴露到公网；简历是敏感个人信息。
8. **chromadb 不在运行时依赖里**：只有复现「换库对照」才需要 `requirements-optional.txt`。

## Verification

- 同一份简历 + 同一段 JD，`--local` 与 `--api` 的 `score.total`、`deductions` 条数、
  `semantic_items` 覆盖数必须完全一致（模型可能有 ±2 分波动，其余字段必须相同）
- `--json` 输出能被 `json.loads` 解析，且含 `score.total`、`verdict`、`semantic_items` 三个键
- 拿一份扫描件跑 → 退出码 2，且提示里出现「没有可解析的文本」
- 服务未启动时跑 `--api` → 退出码 3（不是 1、不是崩栈）
