"""M8a：校验可审计材料库，并用 M1/M3 已产出的缺口构建学习计划。"""

from __future__ import annotations

from collections import Counter
from datetime import date
import json
import math
from pathlib import Path
import re
import sys
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit
import warnings

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas_revision import DiagnosisReport
from app.scoring_rules import HardScore, normalize_skill, skill_family

# 评分类型仅用于标注；运行时不导入会加载模型或初始化客户端的模块。
if TYPE_CHECKING:
    from app.scoring_semantic import SemanticScore

# 精确匹配官方文档主机；不放行任意子域、后缀相似域或 URL 用户信息。
ALLOWED_DOMAINS: tuple[str, ...] = (
    "go.dev", "docs.python.org", "dev.mysql.com", "redis.io",
    "docs.docker.com", "fastapi.tiangolo.com", "docs.langchain.com",
    "docs.dify.ai", "git-scm.com", "kubernetes.io", "kafka.apache.org",
    "www.postgresql.org",
    # 新增域名：每个域名均已实测可访问（2026-11-14），不是仅凭官方身份放行。
    "python.langchain.com", "huggingface.co", "www.promptingguide.ai",
    "docs.trychroma.com", "www.anthropic.com", "docs.llamaindex.ai",
    "learn.microsoft.com",
)
MAX_GAPS = 8
MAX_MATERIALS_TOTAL = 10
MAX_MATERIALS_PER_GAP = 2
FDE_GAP_KEYWORDS = ("评测", "指标", "需求", "业务拆解", "知识库", "文档问答", "工作流",
                    "检索", "向量", "RAG", "交付", "验收", "Agent", "提示词", "Prompt")
SEMANTIC_MIN_COS = 0.45
# 两个判据必须同时成立；相似但没有明显领先的候选仍不能推荐。
SEMANTIC_MIN_MARGIN = 0.03
# 路径与启动目录无关；业务计算不读取外部网页。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
MATERIALS_PATH = PROJECT_ROOT / "materials/materials.json"
_LEVEL_RANK = {"入门": 0, "进阶": 1, "专题": 2}
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
_UNMATCHED_REASON = "材料库暂无对应条目"
# 这里只声明 M3/S3 已有文案格式，不增加技能词表或任意分词规则。
_GAP_PREFIX = re.compile(r"^(?:必备技能\s*[:：]|JD\s*职责\s*[:：]|负责)\s*", re.I)
_GAP_EXAMPLES = re.compile(r"(.+?)\s*(?:（\s*举例\s*[:：]\s*([^（）()]*)）|\(\s*举例\s*[:：]\s*([^（）()]*)\))")


def _validate_date(value: str) -> str:
    """要求日期使用真实存在的 YYYY-MM-DD，失败抛 ValueError。"""
    # 正则限定格式，日历校验另行拒绝二月三十日等不存在日期。
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("日期必须为 YYYY-MM-DD")
    date.fromisoformat(value)
    return value


def validate_external_locator(locator: str) -> str:
    """校验 HTTPS 官方域名，供材料加载和链接检查共用；不执行网络请求。"""
    # 拒绝解析器可能吞掉的空白、控制字符与反斜杠，避免显示和访问目标不同。
    if any(char.isspace() or ord(char) < 32 for char in locator) or "\\" in locator:
        raise ValueError("外链不得含空白、控制字符或反斜杠")
    parts = urlsplit(locator)
    if (parts.scheme != "https" or parts.hostname not in ALLOWED_DOMAINS
            or parts.username is not None or parts.password is not None
            or parts.port not in (None, 443)):
        raise ValueError("外链必须为 HTTPS 且主机在 ALLOWED_DOMAINS 内")
    return locator


def _validate_local_locator(locator: str) -> str:
    """校验仓库教材相对路径、可选行号锚点及目标文件是否存在。"""
    path, separator, anchor = locator.partition("#")
    # 仅支持任务书约定的 Markdown 教材和 #L40 形式，不允许目录穿越。
    if (not path.startswith("docs/教材/") or "\\" in path or ":" in path
            or any(part in ("", ".", "..") for part in path.split("/"))
            or not path.endswith(".md") or (separator and not re.fullmatch(r"L[1-9]\d*", anchor))):
        raise ValueError("本地材料必须为 docs/教材/*.md，可带 #L行号")
    target = (PROJECT_ROOT / path).resolve()
    corpus_root = (PROJECT_ROOT / "docs/教材").resolve()
    # resolve 后再检验边界，软链接也不能把本地材料指到语料目录外。
    if not target.is_relative_to(corpus_root) or not target.is_file():
        raise ValueError(f"本地材料文件不存在或不在教材目录：{path}")
    return locator


class Material(BaseModel):
    """PRD §6.2.2 的材料契约；评分、标题和地址均来自静态材料库。"""

    # 严格类型禁止把 True、5.0 或字符串分数自动转换为合法整数。
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")  # 稳定引用键不含空白或大写。
    title: str = Field(min_length=2, max_length=80)  # 只展示库内标题，不自动补写。
    kind: Literal["local", "external"]  # 地址验证和后续渲染均依此分流。
    locator: str = Field(min_length=1)  # 外链保留完整地址，本地保留相对路径。
    tags: list[str] = Field(min_length=1, max_length=6)  # 只与既有技能归一规则比较。
    level: Literal["入门", "进阶", "专题"]  # 同分时固定入门优先。
    quality: int = Field(ge=1, le=5)  # 只接收已评分的库内分值，0 是未评分。
    verified_at: str  # 历史核实日期，不能声称链接现在一定可用。
    note: str = Field(min_length=1, max_length=60)  # 一句话解释材料用途。

    @field_validator("title", "note")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        """拒绝仅由空白构成的标题和说明，保留材料库原文。"""
        if not value.strip():
            raise ValueError("标题和说明不能为空白")
        return value

    @field_validator("tags")
    @classmethod
    def _valid_tags(cls, values: list[str]) -> list[str]:
        """每个标签必须是非空文字，避免空标签匹配无关缺口。"""
        if any(not value.strip() for value in values):
            raise ValueError("tags 不得含空标签")
        return values

    @field_validator("verified_at")
    @classmethod
    def _valid_date(cls, value: str) -> str:
        """核实日期必须是合法日历日期；日期不替代实时链接检查。"""
        return _validate_date(value)

    @field_validator("locator")
    @classmethod
    def _valid_locator(cls, value: str, info) -> str:
        """按先前已校验的 kind 分流地址，外部与本地共用加载约束。"""
        if info.data.get("kind") == "external":
            return validate_external_locator(value)
        if info.data.get("kind") == "local":
            return _validate_local_locator(value)
        return value


class LearningGap(BaseModel):
    """记录一个原文缺口、JD 依据、严重度以及最多两条材料。"""

    # 保留原能力与材料字段；新增 meta 只记匹配审计，无材料时仍显示缺口空态。
    gap: str  # 上游缺口原文，不把技能族名替换成岗位原话。
    why: str  # 引用输入中的岗位依据，不包含简历原文。
    severity: Literal["high", "medium", "low"]  # 用于缺口展示优先级。
    materials: list[Material] = Field(max_length=MAX_MATERIALS_PER_GAP)  # 无匹配允许空列表。
    meta: dict = Field(default_factory=dict)  # 每个缺口保存匹配方式与可复核的关键词/余弦证据。


class LearningPlan(BaseModel):
    """汇总最多八个缺口、真实未匹配项及可重放的审计信息。"""

    # meta 不含墙钟时间或随机数，保证相同输入的 model_dump 完全一致。
    gaps: list[LearningGap] = Field(max_length=MAX_GAPS)  # 包括有材料和无材料的缺口。
    unmatched: list[str]  # 只记录真正匹配为空的已展示缺口。
    meta: dict  # 库版本、限额、外链与可选本地推理状态。


def load_materials(path: Path | None = None) -> tuple[list[Material], str]:
    """加载并校验材料库，返回 (materials, version)；任何校验失败抛 ValueError。"""
    try:
        payload = json.loads((MATERIALS_PATH if path is None else Path(path)).read_text(encoding="utf-8"))
        # 元数据也是契约的一部分；不能把没有版本的库静默当成第一版。
        if not isinstance(payload, dict) or set(payload) != {"quality_rubric", "version", "updated_at", "materials"}:
            raise ValueError("材料库必须含且仅含 quality_rubric、version、updated_at、materials")
        # 评分口径随库保存；缺失或纯空白不能冒充已经完成材料评审。
        rubric = payload["quality_rubric"]
        if not isinstance(rubric, str) or not rubric.strip():
            raise ValueError("quality_rubric 必须为非空字符串")
        version = payload["version"]
        if not isinstance(version, str) or not version.strip():
            raise ValueError("version 必须为非空字符串")
        if not isinstance(payload["updated_at"], str):
            raise ValueError("updated_at 必须为日期字符串")
        _validate_date(payload["updated_at"])
        if not isinstance(payload["materials"], list):
            raise ValueError("materials 必须为列表")
        materials = [Material.model_validate(row) for row in payload["materials"]]
        if len({row.id for row in materials}) != len(materials):
            raise ValueError("材料 id 不唯一")
        # 核实日期可以早于本次整理日期，不能晚于记录自身的更新时间。
        if any(row.verified_at > payload["updated_at"] for row in materials):
            raise ValueError("verified_at 不得晚于 updated_at")
        groups = Counter(tuple(sorted({normalize_skill(tag) for tag in row.tags})) for row in materials)
        for tags, count in sorted(groups.items()):
            if count > 2:
                warnings.warn(f"同一 tags 组合 {tags} 有 {count} 条材料", UserWarning, stacklevel=2)
        return materials, version
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        # 文件缺失、坏 JSON、坏日期和 Pydantic 校验均统一成公开约定的异常。
        raise ValueError(f"材料库校验失败：{exc}") from exc


def _material_key(material: Material) -> tuple:
    """按质量降序、学习级别、核实日期降序和 id 升序生成确定排序键。"""
    # 日期已按 ISO 格式校验，序数取负能稳定实现降序而不反转其他键。
    return (-material.quality, _LEVEL_RANK[material.level],
            -date.fromisoformat(material.verified_at).toordinal(), material.id)


def _gap_candidates(gap: str) -> list[str]:
    """剥离指定前缀并展开末尾的举例括号，保留主名和明确列出的例子。"""
    text = gap.strip()
    # 支持「JD 职责：负责…」这样的连续前缀，只从字符串开头逐次剥离。
    while match := _GAP_PREFIX.match(text):
        text = text[match.end():].strip()
    example = _GAP_EXAMPLES.fullmatch(text)
    if example:
        # 仅展开完整的举例格式及顿号分隔项；不从一般职责长句里猜技能。
        candidates = [example.group(1), *(example.group(2) or example.group(3) or "").split("、")]
    else:
        candidates = [text]
    return list(dict.fromkeys(value.strip() for value in candidates if value.strip()))


def match_materials(gap: str, materials: list[Material]) -> list[Material]:
    """按格式展开候选后，只用既有技能别名和同族规则匹配；无命中返回 []。"""
    normalized = [normalize_skill(candidate) for candidate in _gap_candidates(gap)]
    candidates = [(name, skill_family(name)) for name in normalized if name]
    matched = []  # 新建容器，排序不改变调用方传入的材料列表。
    for material in materials:
        # 每个候选仍完整精确比较，不因 Django 含 go 或长句含短词就凑材料。
        tags = [normalize_skill(tag) for tag in material.tags]
        if any(tag == name or (family is not None and skill_family(tag) == family)
               for name, family in candidates for tag in tags):
            matched.append(material)
    return sorted(matched, key=_material_key)


def _keyword_length(text: str) -> bool:
    """中文片段至少两字，纯英文片段至少三字母，不用短词做包含匹配。"""
    chinese = re.findall(r"[\u4e00-\u9fff]", text)
    # 中文按汉字计数，不能把一个汉字加空格或标点凑成两字。
    return len(chinese) >= 2 if chinese else len(re.findall(r"[a-z]", text.lower())) >= 3


def _keyword_match(gap: str, materials: list[Material]) -> tuple[list[Material], list[str]]:
    """只从白名单能力词提取片段，再与原有 tags 双向包含比较。"""
    text = "".join(gap.lower().split())
    full, partial = [], []
    for keyword in FDE_GAP_KEYWORDS:
        word = keyword.lower()
        if re.fullmatch(r"[a-z]+", word):
            # 英文用词边界定位，避免 fragment 因包含 rag 而误命中。
            if re.search(r"(?<![a-z0-9])" + word + r"(?![a-z0-9])", gap.lower()):
                full.append(word)
        elif word in text:
            full.append(word)
        elif word == "业务拆解":
            # 业务拆解按两字成分识别；文档问答等完整能力词不拆散，避免泛化误配。
            partial.extend(piece for piece in (word[:2], word[2:]) if piece in text)
    # 有明确的完整能力词时，不让泛化片段抢占它（评测优先于句中顺带提及的业务）。
    keywords = list(dict.fromkeys(full or partial))
    # 仅比较原标签，保留其别名归一方式；不把关键词写回材料库。
    matched = [row for row in materials if any(_keyword_length(tag) and _keyword_length(word)
        and (word in normalize_skill(tag) or normalize_skill(tag) in word)
        for tag in row.tags for word in keywords)]
    return sorted(matched, key=_material_key), keywords


def _gap_main_key(gap: str) -> str:
    """剥离格式后只归一主名大小写和空白，保留 Go/Golang 等独立别名。"""
    # 共用前缀与举例拆解，但只取主名，不以例子或技能族作为去重键。
    candidates = _gap_candidates(gap)
    main = candidates[0] if candidates else gap
    return "".join(main.lower().split())


def _collect_gaps(diagnosis: DiagnosisReport, hard: HardScore | None,
                  sem: SemanticScore | None) -> list[tuple[str, str]]:
    """复用 M3 的三个输入产物和严重度口径，不重新抽取 JD 词汇或判断覆盖。"""
    # M3 _build_targets：ATS 缺词 high、硬扣分沿用 severity、未覆盖职责 medium。
    candidates = [(term, "high") for term in diagnosis.missing_keywords]
    if hard is not None:
        candidates.extend((row.jd_evidence, row.severity if row.severity in _SEVERITY_RANK else "medium")
                          for row in hard.deductions if row.type == "missing_skill")
    if sem is not None:
        # 不使用 sem.uncovered 或 best_sim 重判，事实只取 per_item.covered。
        candidates.extend((row.item, "medium") for row in sem.per_item if not row.covered)
    unique: dict[str, str] = {}  # 保留首次出现顺序，同严重度时结果稳定。
    for gap, severity in candidates:
        # 按原文去重，与 M3 一致；Go/Golang 两条 JD 原文不能被偷偷合并丢掉。
        if gap not in unique or _SEVERITY_RANK[severity] < _SEVERITY_RANK[unique[gap]]:
            unique[gap] = severity
    merged: dict[str, tuple[str, str]] = {}
    for gap, severity in unique.items():
        key = _gap_main_key(gap)
        if key in merged:
            previous_gap, previous_severity = merged[key]
            # 文案和严重度分别选择；短文案即使来自低严重度也不能降低最终优先级。
            gap = min((previous_gap, gap), key=len)
            severity = min((previous_severity, severity), key=lambda value: _SEVERITY_RANK[value])
        merged[key] = (gap, severity)
    # 字典保留首次出现顺序，等长文案及同严重度缺口仍保持稳定。
    return sorted(merged.values(), key=lambda row: _SEVERITY_RANK[row[1]])


def _loaded_embedding_model():
    """复用 RAG 或评分已加载的同名 bge-small-zh 实例，绝不创建第二个模型。"""
    rag = sys.modules.get("app.rag")
    if rag is None:
        raise RuntimeError("本地向量模型尚未加载；未尝试联网下载")
    model = getattr(rag, "_vector_model", None)
    # 索引是否建好不影响模型复用，也不需要加载第二套文档索引。
    if model is None:
        # HTTP 评分缓存使用同一个 rag.EMBED_MODEL_NAME；只读取已命中的缓存。
        scoring = sys.modules.get("app.scoring_semantic")
        getter = getattr(scoring, "_get_model", None)
        if getter is not None and getter.cache_info().currsize:
            model = getter()
    if model is None or getattr(model, "model_name", None) != rag.EMBED_MODEL_NAME:
        raise RuntimeError("同名本地向量模型尚未加载；未尝试联网下载")
    return model


def _semantic_document(material: Material) -> str:
    """用稳定的标题和原始标签表示材料，不把泛化用途说明混入相关性判定。"""
    return f"{material.title}\n{' '.join(material.tags)}"


def _local_semantic_match(gap: str, materials: list[Material], calls: dict[str, int],
                          audit: dict | None = None) -> list[Material]:
    """仅当首位余弦与领先差距同时合格时采纳一条，否则保留未匹配。"""
    if not materials:
        return []
    # 已知词已由精确/关键词层处理；未知孤立英文词可能是专名或错字，不靠向量猜测。
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", gap.strip()):
        if audit is not None:
            audit["semantic_rejection"] = "未知英文词缺少任务上下文"
        return []
    model = _loaded_embedding_model()
    documents = [_semantic_document(row) for row in materials]
    # 次数仅记录本次实际调用；即使推理中途失败，也不能仍报告零模型调用。
    calls["embedding"] += 1
    vectors = [list(map(float, vector)) for vector in model.embed([gap, *documents], batch_size=32)]
    # 首行对应缺口，后续行严格按材料顺序对应；数量、维度和有限数缺一不可。
    if (len(vectors) != len(documents) + 1 or not vectors[0]
            or any(len(vector) != len(vectors[0]) for vector in vectors)
            or any(not math.isfinite(value) for vector in vectors for value in vector)):
        raise ValueError("本地向量输出不完整或包含非有限数")
    norms = [math.sqrt(sum(value * value for value in vector)) for vector in vectors]
    # 零向量没有可解释的余弦，必须显式降级，不能强行赋予一个相关性。
    if any(norm <= 0 or not math.isfinite(norm) for norm in norms):
        raise ValueError("本地向量范数无效")
    scores = [max(-1.0, min(1.0, sum(a * b for a, b in zip(vectors[0], vector)) / (norms[0] * norm)))
              for vector, norm in zip(vectors[1:], norms[1:])]
    order = sorted(range(len(scores)), key=lambda index: (-scores[index], materials[index].id))
    # 并列仅为审计固定顺序，margin 为零时仍会被拒绝，不靠 id 选择赢家。
    first, second = scores[order[0]], scores[order[1]] if len(order) > 1 else None
    margin = first - second if second is not None else None
    if audit is not None:
        audit.update(top1_id=materials[order[0]].id, top1_cos=first, top2_cos=second, margin=margin,
                     min_cos=SEMANTIC_MIN_COS, min_margin=SEMANTIC_MIN_MARGIN, model=model.model_name)
    # 没有第二名时也无法核对相对优势，不把唯一候选误当作有充分区分度。
    if first >= SEMANTIC_MIN_COS and margin is not None and margin >= SEMANTIC_MIN_MARGIN:
        return [materials[order[0]]]
    return []


def build_learning_plan(*, diagnosis: DiagnosisReport, hard: HardScore | None,
                        sem: SemanticScore | None, job_title: str,
                        materials: list[Material] | None = None,
                        semantic_fallback: bool = False) -> LearningPlan:
    """从 M1/M3 同源产物构建确定计划；默认零模型、零网络，不修改输入。"""
    if materials is None:
        library, version = load_materials()
    else:
        # 注入库不假冒默认库版本；空列表表示刻意使用空库。
        library, version = list(materials), "provided"
    sources = _collect_gaps(diagnosis, hard, sem)
    gaps, unmatched, reasons = [], [], {}
    total = omitted = attempts = 0  # 所有预算和审计状态仅在本次计算中存在。
    fallback_status = "not_needed" if semantic_fallback else "disabled"
    fallback_errors: dict[str, str] = {}
    local_calls = dict(embedding=0, reranker=0)  # 生成式模型调用始终不存在。
    for gap, severity in sources[:MAX_GAPS]:
        matched = match_materials(gap, library)
        audit = {"matched_by": "family" if matched else "none"}
        # 只有前一层完全为空才进入下一层，关键词不会替换既有精确/同族结果。
        if not matched:
            matched, keywords = _keyword_match(gap, library)
            if matched:
                audit.update(matched_by="keyword", keywords=keywords)
        if not matched and semantic_fallback:
            attempts += 1
            try:
                matched = _local_semantic_match(gap, library, local_calls, audit)
                # used 表示确实尝试过，是否采纳以逐项 matched_by 为准。
                audit["matched_by"] = "semantic" if matched else "none"
                if fallback_status != "unavailable":
                    fallback_status = "used"
            except Exception as exc:
                # 模型缺失、执行失败和坏输出都必须留在报告元数据中，不能静默忽略。
                fallback_status = "unavailable"
                fallback_errors[gap] = f"{type(exc).__name__}: 本地向量匹配不可用，已保留为未匹配"
        if not matched:
            unmatched.append(gap)
            reasons[gap] = _UNMATCHED_REASON
        # 全章额度按每一次展示的材料计数，同一材料跨缺口出现也占额度。
        wanted = matched[:MAX_MATERIALS_PER_GAP]
        selected = wanted[:max(0, MAX_MATERIALS_TOTAL - total)]
        omitted += len(wanted) - len(selected)
        total += len(selected)
        copies = [row.model_copy(deep=True) for row in selected]
        if audit["matched_by"] == "keyword":
            # 只标注本次推荐副本；原材料的标签、用途和人工质量分保持不变。
            suffix = "；匹配方式：关键词"
            for row in copies:
                row.note = row.note[:60 - len(suffix)] + suffix
        gaps.append(LearningGap(gap=gap, why=f"岗位「{job_title}」的 JD 要求：{gap}", severity=severity,
                                materials=copies, meta=audit))
    # 真实耗时由检查脚本测量；纯函数结果不伪填一个固定的实际耗时。
    meta = dict(materials_version=version, elapsed_ms=None, cost_yuan=0,
                model_calls=sum(local_calls.values()), local_model_calls=local_calls,
                has_external=any(row.kind == "external" for gap in gaps for row in gap.materials),
                semantic_fallback=fallback_status, semantic_attempts=attempts,
                semantic_errors=fallback_errors, unmatched_reasons=reasons,
                matched_by={row.gap: row.meta["matched_by"] for row in gaps},
                gap_count_before_limit=len(sources), truncated_gaps=max(0, len(sources) - MAX_GAPS),
                materials_total=total, omitted_by_material_limit=omitted)
    return LearningPlan(gaps=gaps, unmatched=unmatched, meta=meta)
