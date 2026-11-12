"""按固定岗位族模板生成待确认画像，并提供确认、指纹与报告声明。

本模块只处理传入数据；模板表达通用推测，不代表任何公司的真实 JD。
"""

from hashlib import sha256
import json
import unicodedata

from app.guard_revision import resolve_job_input
from app.schemas_revision import JobProfile


# 模板内容变更须递增版本，供后续报告 meta 追溯；不向既有模型追加字段。
PROFILE_VERSION = "1"
# 族名严格沿用 M2 契约；插入顺序同时作为等长 label 的稳定决胜顺序。
PROFILE_TEMPLATES: dict[str, dict] = {
    # 后端的语言名只用于识别方向，不要求候选人同时掌握所有语言。
    "backend": {
        "labels": ("后端", "服务端", "Java", "Go", "PHP", "Python 后端"),
        "must_have": [
            # 基础开发能力与接口设计是典型要求，未指定公司或精确年限。
            "掌握至少一种后端开发语言及其常用框架",
            "理解 HTTP 协议与接口设计原则",
            "具备关系型数据库建模与查询优化能力",
            # 可靠性与协作能力不冒充候选人已经具备的经历。
            "理解缓存、并发处理与常见性能问题",
            "具备单元测试、日志分析与问题定位能力",
            "能够使用版本管理工具并协同完成代码评审",
        ],
        # 职责按需求、实现、优化、维护顺序排列，输出时保持原序。
        "responsibilities": [
            "参与业务需求分析与后端技术方案设计",
            "开发和维护业务接口及数据访问逻辑",
            "定位性能瓶颈并改进服务稳定性",
            "编写测试与技术文档，配合发布和问题修复",
        ],
    },
    # 前端涵盖网页与客户端方向，具体技术栈仍应以真实 JD 为准。
    "frontend": {
        "labels": ("前端", "Frontend", "Web 开发", "网页开发"),
        "must_have": [
            # 先描述浏览器基础，再描述组件与工程协作。
            "掌握 HTML、CSS 与 JavaScript 基础",
            "理解浏览器渲染机制与常见兼容性问题",
            "具备组件化开发与状态管理能力",
            # 不把某个框架写成所有前端岗位的硬性事实。
            "能够使用常见前端框架与构建工具",
            "具备接口联调、页面调试与测试能力",
            "关注页面性能、可访问性与交互体验",
        ],
        # 交付职责覆盖页面实现及持续维护。
        "responsibilities": [
            "根据产品需求与设计稿实现页面和交互",
            "维护通用组件并参与前端工程建设",
            "与后端协作完成接口联调和问题定位",
            "优化页面性能并修复兼容性与体验问题",
        ],
    },
    # 算法族使用更具体的方向词，最长匹配会优先于短语言 label。
    "algorithm": {
        "labels": ("算法", "Algorithm", "机器学习", "深度学习", "人工智能"),
        "must_have": [
            # 数学、编程与模型基础组成可追溯的通用推测。
            "具备概率统计、线性代数与优化方法基础",
            "掌握算法开发常用编程语言与数据结构",
            "理解常见机器学习模型及其适用边界",
            # 数据与评估能力和具体模型选型分开表达。
            "具备数据清洗、特征处理与实验设计能力",
            "能够设计评估指标并分析模型误差",
            "了解模型部署、推理性能与效果监控",
        ],
        # 不假设特定论文、模型产品或候选人的研究成果。
        "responsibilities": [
            "分析业务问题并制定算法实验方案",
            "处理训练数据并完成模型训练与验证",
            "评估模型效果，分析误差并迭代优化",
            "协作推进算法部署并跟踪实际运行效果",
        ],
    },
    # 数据族同时覆盖分析与工程的基础能力，不推测具体业务指标。
    "data": {
        "labels": ("数据", "Data", "数仓", "商业分析", "商业智能"),
        "must_have": [
            # 基础项围绕查询、质量及建模。
            "掌握 SQL 查询与常见数据处理方法",
            "理解数据质量校验与数据清洗流程",
            "具备数据建模与指标口径梳理能力",
            # 分析结论需要能解释和复核。
            "了解统计分析与数据可视化方法",
            "能够使用脚本或数据工具完成分析任务",
            "具备业务理解、结果解释与文档沟通能力",
        ],
        # 职责不声称某家公司已有特定数据规模。
        "responsibilities": [
            "梳理业务数据需求并统一指标口径",
            "建设和维护数据处理流程与数据模型",
            "开展数据分析并形成可复核的业务结论",
            "监控数据质量并处理数据异常问题",
        ],
    },
    # 测试族只推测验证能力，不承诺任何真实项目的覆盖率。
    "test": {
        "labels": ("测试", "Test", "质量保障", "质量保证", "QA"),
        "must_have": [
            # 需求理解先于测试工具选择。
            "理解软件测试流程与常用测试设计方法",
            "能够分析需求并设计测试用例",
            "具备接口测试与问题复现能力",
            # 自动化、数据库和缺陷跟踪补足基本工程能力。
            "掌握至少一种自动化脚本或测试工具",
            "能够通过数据库查询与日志辅助定位问题",
            "具备缺陷跟踪、风险沟通与质量意识",
        ],
        # 性能与专项要求留给真实 JD，不在通用职责里虚构。
        "responsibilities": [
            "参与需求评审并制定测试计划与用例",
            "执行功能与接口测试，记录并跟踪缺陷",
            "建设和维护自动化测试脚本",
            "汇总测试结果并协作控制发布质量风险",
        ],
    },
    # 运维族包括常见英文名称，均由同一套匹配规则处理。
    "ops": {
        "labels": ("运维", "SRE", "DevOps", "Operations", "系统管理"),
        "must_have": [
            # 先列系统、网络及自动化基础。
            "掌握 Linux 系统管理与常见故障排查方法",
            "理解网络协议、域名解析与服务访问链路",
            "能够使用脚本完成重复操作的自动化",
            # 运行可靠性不等于对候选人作可用率承诺。
            "了解服务部署、配置管理与发布流程",
            "具备监控告警、日志分析与应急处置能力",
            "理解权限控制、备份恢复与基础安全要求",
        ],
        # 固定顺序覆盖部署、观测、处置和沉淀。
        "responsibilities": [
            "维护运行环境并协作完成服务部署发布",
            "建设监控告警并跟踪服务运行状态",
            "排查故障并参与应急响应与复盘",
            "编写自动化脚本和运维文档以改进效率",
        ],
    },
    # 产品族以需求与交付协作为核心，不引入公司商业目标。
    "product": {
        "labels": ("产品", "Product", "需求分析", "业务分析"),
        "must_have": [
            # 用户研究和需求分析提供决策依据。
            "具备用户需求调研与问题分析能力",
            "能够梳理业务流程并识别需求优先级",
            "掌握原型设计与需求文档表达方法",
            # 协作、验收及数据反馈形成通用闭环。
            "理解产品研发流程与验收标准制定方法",
            "具备跨团队沟通与推进问题解决的能力",
            "能够结合数据和用户反馈评估产品效果",
        ],
        # 每条职责只说明典型工作，不描述既得成果。
        "responsibilities": [
            "收集用户反馈并分析业务需求",
            "制定产品方案、原型与需求说明",
            "协调研发和测试推进需求交付与验收",
            "跟踪上线效果并提出后续迭代建议",
        ],
    },
    # FDE、交付、实施、解决方案必须作为同一 delivery 族的入口。
    "delivery": {
        "labels": ("FDE", "交付", "实施", "解决方案", "技术支持"),
        "must_have": [
            # 客户场景理解与技术落地能力共同组成交付要求。
            "能够理解客户业务场景并澄清技术需求",
            "具备技术方案设计与系统集成能力",
            "能够完成环境部署、配置与接口联调",
            # 强调可验收的协作能力，不虚构客户案例或厂商认证。
            "具备日志分析、问题排查与故障沟通能力",
            "能够编写交付文档并开展用户培训",
            "具备项目推进、风险识别与验收协作能力",
        ],
        # 实施过程从澄清需求到验收支持，顺序保持不变。
        "responsibilities": [
            "调研客户需求并制定实施或解决方案",
            "推进系统部署、集成与现场问题处理",
            "协作完成交付验收、文档移交与用户培训",
            "收集使用反馈并推动产品和交付流程改进",
        ],
    },
}

# 未匹配时严格使用四条通用必备项，不补写某一技术族的具体要求。
GENERIC_MUST_HAVE = [
    "理解目标岗位的业务目标与基本工作流程",
    "具备完成岗位任务所需的基础知识与工具使用能力",
    "能够清晰沟通、协作推进并记录工作过程",
    "能够分析问题、学习新知识并对工作结果进行复核",
]
# 通用职责保留四条，与已知族一致覆盖完整工作过程。
GENERIC_RESPONSIBILITIES = [
    "澄清工作目标并制定执行计划",
    "协作完成岗位相关任务并同步进展",
    "记录工作结果、问题与待确认事项",
    "根据反馈复盘并改进工作方法",
]
# 推测声明进入数据本身，确认后也不得移除。
INFERRED_NOTE = "以下要求由系统按岗位名称推测，非岗位真实 JD，仅供参考"
# 低置信声明只在没有命中任何已知族时追加。
LOW_CONFIDENCE_SUFFIX = "；未匹配到已知岗位族，置信度低，请务必贴真实 JD"


class ProfileNotConfirmed(ValueError):
    """草案未确认时禁止下游生成意见（PRD §5.2）。"""


def match_family(title: str) -> tuple[str | None, str]:
    """大小写及全角归一后按最长 label 匹配岗位族。

    参数：title 为岗位名称；本函数只匹配，输入有效性由推测入口统一核对。
    返回：(族名, "精确")；未命中为 (None, "低置信")，等长按模板族顺序决定。
    异常：无（参数须符合类型契约）。
    """
    # NFKC 只用于匹配；返回画像的 title 仍保留用户原来的文字。
    normalized = unicodedata.normalize("NFKC", title).casefold()
    family, longest = None, 0
    for name, template in PROFILE_TEMPLATES.items():
        for label in template["labels"]:
            candidate = unicodedata.normalize("NFKC", label).casefold()
            # 严格大于保留等长时的首个命中，不依赖集合遍历或随机顺序。
            if candidate in normalized and len(candidate) > longest:
                family, longest = name, len(candidate)
    return family, "精确" if family is not None else "低置信"


def infer_profile(title: str) -> JobProfile:
    """从岗位名生成未确认草案：已知族六项必备、四项职责，未知族四项通用必备。

    参数：title 为待推测的岗位名称，复用 M0 判断是否有效。
    返回：source="inferred"、confirmed=False 的新 JobProfile，两个列表均独立。
    异常：无效 title 抛 ValueError，逐字透传 M0 供 422 使用的错误文案。
    """
    # 只提供岗位名，不传 JD，避免在此复写 M0 的输入优先级或长度阈值。
    _, error = resolve_job_input(title, None)
    if error is not None:
        raise ValueError(error)
    family, _ = match_family(title)
    template = PROFILE_TEMPLATES[family] if family is not None else None
    # 无论匹配与否，先显式复制；不能依赖模型库恰好会复制输入列表。
    must_have = list(template["must_have"] if template is not None else GENERIC_MUST_HAVE)
    responsibilities = list(template["responsibilities"] if template is not None else GENERIC_RESPONSIBILITIES)
    # 用户确认只代表认可推测内容，不会把来源变成真实 JD。
    note = INFERRED_NOTE + (LOW_CONFIDENCE_SUFFIX if family is None else "")
    return JobProfile(title=title.strip(), source="inferred", confirmed=False,
                      must_have=must_have, responsibilities=responsibilities, note=note)


def profile_hash(profile: JobProfile) -> str:
    """计算不含确认状态的确定性 SHA-256 画像指纹。

    参数：profile 为当前画像。
    返回：按 sort_keys=True、ensure_ascii=False 序列化后 UTF-8 摘要的十六进制串。
    异常：无（参数须符合 JobProfile 契约）。
    """
    # 只排除 confirmed；名称、来源、声明、两组有序条目都参与防偷换校验。
    payload = profile.model_dump(exclude={"confirmed"})
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return sha256(canonical.encode("utf-8")).hexdigest()


def confirm_profile(profile: JobProfile, expected_hash: str,
                    edits: dict | None = None) -> JobProfile:
    """先核对展示过的画像指纹，再应用白名单编辑并返回新的已确认对象。

    参数：profile 为原画像，expected_hash 为展示时的指纹，edits 仅允许两组条目。
    返回：confirmed=True 的新 JobProfile，与原画像及编辑输入均不共享列表。
    异常：指纹不符、非法编辑字段或非字符串列表编辑均抛 ValueError。
    """
    # 指纹校验必须先于编辑；过期请求不能通过提交修改来替换待确认草案。
    if profile_hash(profile) != expected_hash:
        raise ValueError("画像已变更，请重新生成")
    updates = edits if edits is not None else {}
    # 不允许借确认请求改来源、名称、声明或确认状态。
    if any(key not in ("must_have", "responsibilities") for key in updates):
        raise ValueError("仅允许编辑 must_have / responsibilities")
    for key, value in updates.items():
        # 拒绝把字符串拆成单字列表，也不把空列表误当成没有提交编辑。
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"{key} 必须为字符串列表")
    # 即使没有 edits，两组列表也逐一复制；失败请求不会提前修改原对象。
    return JobProfile(title=profile.title, source=profile.source, confirmed=True,
                      must_have=list(updates.get("must_have", profile.must_have)),
                      responsibilities=list(updates.get("responsibilities", profile.responsibilities)),
                      note=profile.note)


def require_confirmed(profile: JobProfile) -> None:
    """在下游生成修改意见前强制检查确认状态。

    参数：profile 为即将使用的画像。
    返回：已确认时返回 None，不修改画像。
    异常：未确认时抛 ProfileNotConfirmed，并提示先确认再生成意见。
    """
    # 草案和低置信草案一视同仁；不能因模板命中就自动跳过用户确认。
    if not profile.confirmed:
        raise ProfileNotConfirmed("请先确认岗位画像，再生成修改意见")


def profile_banner(profile: JobProfile) -> str:
    """提供推测画像的唯一报告抬头文案。

    参数：profile 为报告使用的岗位画像。
    返回：inferred 为固定推测声明，user_jd 为空字符串，确认状态不影响声明。
    异常：无（参数须符合 JobProfile 来源契约）。
    """
    # 后续 PDF 模块调用此入口，避免确认后遗漏来源提示或另写不一致文案。
    if profile.source == "inferred":
        return "本报告基于岗位画像（推测），非真实 JD；建议粘贴真实岗位要求后重新生成"
    return ""
