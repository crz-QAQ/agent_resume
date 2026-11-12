"""声明 PRD §六的四个简历修改模型，统一意见与智能修改的数据契约。

字段描述供结构化输出使用；本模块只定义数据，不执行生成、改写或校验逻辑。
"""

from pydantic import BaseModel, Field


class ReviewIssue(BaseModel):
    """承载一条修改意见的定位、建议、理由与原文证据。"""

    # PRD §六：意见编号由调用方递增生成。
    id: str = Field(description="意见编号，如 R-001")
    # 五类问题保留 PRD 的字符串契约，不在模型中另设枚举或转换。
    category: str = Field(description="类别：missing/weak_evidence/buried/noise/inconsistent")
    # 严重度由后续诊断模块提供。
    severity: str = Field(description="严重度：high/medium/low")
    # 定位供报告与后续修改共同引用。
    location: str = Field(description="修改位置，如工作经历 · 第 2 段")
    # 原句必须由护栏在简历中检索确认。
    original_text: str = Field(description="简历原句，必须能在简历原文检索到")
    # missing 类允许空字符串，不能把没有的经历写成已有。
    suggested_text: str = Field(description="建议改成的文字；missing 类可为空字符串")
    # 理由同时说明岗位要求与简历的具体问题。
    why: str = Field(description="为什么改，引用 JD 要求与简历的问题")
    # 收益应给出改前与改后的具体对比。
    gain: str = Field(description="改完的好处，说明改前到改后的具体变化")
    # 推测模式必须明示依据来自岗位画像。
    jd_evidence: str = Field(description="JD 原文依据；inferred 模式写岗位画像及其要求")
    # 数字、规模与成果只能使用真实信息。
    risk_note: str = Field(description="诚实提醒，数字、规模与成果必须真实")


class JobProfile(BaseModel):
    """承载真实 JD 或待确认岗位画像的来源与要求。"""

    # 岗位名称保留完整方向信息。
    title: str = Field(description="岗位名称")
    # 来源区分真实 JD 与按岗位名称推测的画像。
    source: str = Field(description="来源：user_jd/inferred")
    # 推测画像需由用户确认后供后续模块使用。
    confirmed: bool = Field(description="岗位画像是否已经用户确认")
    # 必备技能和职责分开保存。
    must_have: list[str] = Field(description="岗位必备要求列表")
    # 每条职责保留完整语义。
    responsibilities: list[str] = Field(description="岗位职责要点列表")
    # inferred 模式在此声明画像为推测。
    note: str = Field(description="岗位画像说明，inferred 时须包含推测免责声明")


class AdaptedText(BaseModel):
    """承载一条被采纳修改的前后对照与事实核对记录。"""

    # 每次修改必须回链已有意见。
    issue_id: str = Field(description="关联的意见编号")
    # 位置与意见中的定位对应。
    location: str = Field(description="修改位置")
    # 改前文字须能在简历原文检索。
    before: str = Field(description="修改前的简历原句")
    # 改后正文由护栏重新抽取名词和数字核对。
    after: str = Field(description="修改后的文字")
    # 此列表仅作记录，不能替代护栏对正文的检查。
    added_terms: list[str] = Field(description="改写出现的新名词，须全部通过白名单校验")
    # 数字依据只能来自简历原文。
    kept_numbers: list[str] = Field(description="改写保留的数字，须全部在简历原文出现")


class RevisionReport(BaseModel):
    """汇总修改意见、岗位画像、可选改写与审计信息。"""

    # PRD §六约定最多十二条；本块仅声明字段。
    issues: list[ReviewIssue] = Field(description="修改意见列表，最多 12 条")
    # 被拒条目保留原因，便于后续排查。
    rejected_issues: list[dict] = Field(description="被红线丢弃的意见及其原因")
    # 保留画像来源与用户确认状态。
    profile: JobProfile = Field(description="岗位画像与来源说明")
    # 尚无评分时明确使用 None，不能伪装成零分。
    score_before: int | None = Field(description="修改前评分总分；未评分时为 null")
    # 后续模块负责重算与标注模型部分的预估波动。
    score_after_estimate: int | None = Field(description="采纳修改后的预估总分；未估算时为 null")
    # 仅智能修改时填写逐条对照。
    adaptations: list[AdaptedText] = Field(description="智能修改的逐条前后对照")
    # 未采用项须说明原因。
    skipped: list[dict] = Field(description="未采用的建议及其原因")
    # 审计元数据由调用方提供。
    meta: dict = Field(description="模型、提示词版本、成本、耗时与生成时间等元数据")


class KwCoverage(BaseModel):
    """内部结构，不进对外 API；记录一个 JD 关键词的 ATS 覆盖情况。"""

    # PRD §6.2：保留 JD 必备技能或职责关键词的原始拼写。
    term: str = Field(description="JD 中的必备技能或职责关键词")
    # 次数按原文大小写不敏感、去空白后统计，族内命中也算覆盖。
    count_in_resume: int = Field(description="关键词或适用同族成员在简历原文出现的次数")
    # 使用内部统一的五类节名，按原文出现顺序去重。
    sections: list[str] = Field(description="命中的段落所属节：技能/工作经历/教育/项目/其他")
    # 必备技能优先于同名职责词，不能被后出现的职责词覆盖来源。
    from_must_have: bool = Field(description="是否来自 JD 必备技能列表")


class DiagnosisFinding(BaseModel):
    """内部结构，不进对外 API；记录一条有简历原文证据的规则诊断。"""

    # code 只能由 PRD §6.3 的八条纯规则产生。
    code: str = Field(description="PRD §6.3 规定的八种诊断 code 之一")
    # 固定严重度与规则表一致，由诊断模块排序。
    severity: str = Field(description="严重度：high/medium/low")
    # 段落从所在节的第一段开始编号，标题本身不占段号。
    location: str = Field(description="原文位置，如工作经历 · 第 2 段")
    # 证据必须通过 M0 find_original 检索，无法检索的条目不得保留。
    evidence: str = Field(description="简历原文片段，必须能在原文中检索到")
    # 说明具体问题，不生成修改意见或虚构事实。
    detail: str = Field(description="一句人话说明发现的具体缺陷")


class DiagnosisReport(BaseModel):
    """内部结构，不进对外 API；汇总规则缺陷、ATS 覆盖与完整性检查。"""

    # 与对外 ReviewIssue 区分，本阶段只诊断，不调用模型生成意见。
    findings: list[DiagnosisFinding] = Field(description="按严重度与规则顺序排列的诊断缺陷，最多 20 条")
    # 关键词保留输入顺序并去重，最多三十条。
    keyword_coverage: list[KwCoverage] = Field(description="ATS 关键词覆盖情况，最多 30 条")
    # 仅列出未覆盖的关键词原名，不自行添加岗位要求。
    missing_keywords: list[str] = Field(description="JD 中有而简历未覆盖的关键词")
    # 五个固定键分别表示姓名、年限、学历、技能、联系方式是否齐全。
    completeness: dict[str, bool] = Field(description="name/years/education/skills/contact 的完整性")
    # 成本恒为零；悬空证据丢弃数与截断数量也写入此处供复核。
    meta: dict = Field(description="原文字数、发现数、耗时、零成本及丢弃和截断统计")
