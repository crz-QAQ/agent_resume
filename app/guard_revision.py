"""以纯函数实现 M0 的原句、名词、数字红线与岗位输入组合规则。

只使用传入的原文；不调用模型、不联网、不读取环境，也不修改输入对象。
"""

import re
import unicodedata

from app.schemas_revision import AdaptedText, ReviewIssue


# 四个公开常量逐项沿用任务书 §一.2，不引入外部词库或通用词白名单。
TECH_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]{1,20}")
# 中文专名只按这些后缀与下方引号识别，不扩展到普通中文词汇。
PROPER_SUFFIX = ("公司", "科技", "集团", "大学", "学院", "银行", "医院", "某某", "股份", "有限")
# 保留任务书指定的四组引号；返回的是引号内原文。
QUOTED = re.compile(r'[《「『"]([^》」』"]{2,40})[》」』"]')
# 「两」与「二」表示同一个数，转换必须发生在数字比较之前。
CN_DIGITS = {"一": "1", "二": "2", "两": "2", "三": "3", "四": "4", "五": "5",
             "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}
# D-M3-01：占位符连同量级、百分号整体跳过，不能剩下一个「万」再当作数字。
NUMBER_PLACEHOLDER = re.compile(r"(?<![A-Za-z0-9_])XX(?![A-Za-z0-9_])(?:\s*[十百千万亿]+)?(?:\s*[%％])?(?:\s*级)?")


def normalize_text(text: str) -> str:
    """去除空白与常见标点，供原句检索使用。

    参数：text 为待规范化的原始文字。
    返回：去除 Unicode 空白、标点后的字符串，不变更文字或数字。
    异常：无（参数须符合类型契约）。
    """
    # 不用于名词白名单：拼接词语会把本来不存在的专名制造出来。
    # 数字也须先独立取词，不能把「2 年、3 个」拼成「23」。
    # 使用 Unicode 标点类别，覆盖中文全角标点与英文标点的排版差异。
    return "".join(char for char in text
                   if not char.isspace() and not unicodedata.category(char).startswith("P"))


def find_original(resume_text: str, original_text: str) -> bool:
    """核对红线④：规范化后的原句必须是简历原文的子串。

    参数：resume_text 为完整简历原文，original_text 为意见或改写引用的原句。
    返回：原句去空白标点后非空且可检索时为 True，否则为 False。
    异常：无（参数须符合类型契约）。
    """
    original = normalize_text(original_text)
    # 空字符串也是任意字符串的子串，必须明确拒绝空白或纯标点原句。
    # 检索只针对简历，JD 中有相同句子也不能补足原句证据。
    return bool(original) and original in normalize_text(resume_text)


def _tech_terms(text: str) -> list[str]:
    """抽取完整英文候选词，保留正文拼写。

    参数：text 为任意一份待核对原文或改写。
    返回：任务书正则可识别的英文词列表，剥除句末句点与连字符。
    异常：无（参数须符合类型契约）。
    """
    # 先取完整连续词，避免正则的 21 字符上限把长词截成已有词的前缀。
    # C++ / C# / Node.js 的内部符号保留，不做别名、词干或同义词扩展。
    return [token.rstrip(".-") for token in re.findall(r"[A-Za-z][A-Za-z0-9+#.\-]*", text)
            if TECH_TOKEN.match(token)]


def new_tech_terms(after: str, resume_text: str, jd_text: str) -> list[str]:
    """按红线①找出简历与 JD 中均不存在的英文技术词。

    参数：after 为建议或改写正文，resume_text、jd_text 为各自完整原文。
    返回：违规词列表，按首次出现排序、忽略大小写去重；空列表表示通过。
    异常：无（参数须符合类型契约）。
    """
    # 两份原文分别取词再求并集，不能跨文本边界拼接成新词。
    # 大小写归一只用于英文比较，返回值仍保留正文首次出现的拼写。
    allowed = {term.casefold() for text in (resume_text, jd_text) for term in _tech_terms(text)}
    violations: dict[str, str] = {}
    for term in _tech_terms(after):
        # 严格比较完整词；Go 不会因为 Django 存在就获准。
        if term.casefold() not in allowed:
            violations.setdefault(term.casefold(), term)
    return list(violations.values())


def new_proper_nouns(after: str, resume_text: str, jd_text: str) -> list[str]:
    """按红线①核对后缀专名与引号内容。

    参数：after 为待核对正文，resume_text、jd_text 为简历和 JD 原文。
    返回：未在任一原文逐字出现的候选专名，按发现顺序去重。
    异常：无（参数须符合类型契约）。
    """
    # 边界只有两类：连续中英数字加指定后缀、任务书引号内的内容。
    # 不分词、不猜普通中文是否专名，避免把正常职责润色当作编造经历。
    # 后缀法不能可靠分开紧邻名称的中文上下文；不据此引入停用词或放宽白名单。
    suffixes = "|".join(re.escape(suffix) for suffix in PROPER_SUFFIX)
    candidates = re.findall(rf"[\u3400-\u9fffA-Za-z0-9]+(?:{suffixes})", after)
    candidates.extend(QUOTED.findall(after))
    # 只在各自原文中检索，不拼接原文，不从模型字段或外部词库补充依据。
    # 引号内容也必须整体出现，不能用各个字都出现过来代替名称依据。
    return list(dict.fromkeys(name for name in candidates
                              if name not in resume_text and name not in jd_text))


def _chinese_number(token: str) -> str:
    """把一段中文整数转换为阿拉伯数字写法。

    参数：token 为数字抽取器提供的中文整数串，也允许递归传入空串。
    返回：数字字符串；空串返回空串，仅用于大单位的省略部分。
    异常：无（参数须符合内部数字取词契约）。
    """
    # 补充零的常见写法只是同数归一，不增加任何允许使用的数字。
    digits = {**CN_DIGITS, "零": "0", "〇": "0"}
    if not any(unit in token for unit in "十百千万亿"):
        # 年份「二〇二四」按逐位数字处理，不能相加成 8。
        return "".join(digits[char] for char in token)
    # 先处理大单位，避免把「两百」只取成 2，从而误放行量级变化。
    for unit, scale in (("亿", 100_000_000), ("万", 10_000)):
        if unit in token:
            head, tail = token.split(unit, 1)
            return str(int(_chinese_number(head) or "1") * scale
                       + int(_chinese_number(tail) or "0"))
    total, digit = 0, 0
    # 十是位值而非字符串「10」；十二应为 12，不能替换成 102。
    for char in token:
        if char in "十百千":
            total += (digit or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            digit = 0
        else:
            digit = int(digits[char])
    return str(total + digit)


def _number_tokens(text: str) -> list[str]:
    """提取数字整体，跳过 XX 占位符，并把数字加量级归一成实际数值。

    参数：text 为简历原文或改写正文。
    返回：归一化数字串列表；无量级的阿拉伯数保留原写法，日期仍逐段比较。
    异常：无（参数须符合类型契约）。
    """
    # 用空白替换而非删除，避免占位符两侧的数字拼成另一个数。
    text = NUMBER_PLACEHOLDER.sub(" ", text)
    pattern = re.compile(r"(?P<arabic>[0-9]+(?:\.[0-9]+)?)(?:\s*(?P<scale>[十百千万亿]+))?"
                         r"|(?P<chinese>[零〇一二两三四五六七八九十百千万亿]+(?:\s*[十百千万亿]+)?)")
    numbers = []
    for match in pattern.finditer(text):
        if match["chinese"]:
            numbers.append(_chinese_number(re.sub(r"\s+", "", match["chinese"])))
        elif match["scale"]:
            # Decimal 保证 0.1 万等量级转换不引入浮点误差。
            # 只在实际转换量级时导入，岗位输入等纯文本路径不增加启动依赖。
            from decimal import Decimal

            value = format(Decimal(match["arabic"]) * int(_chinese_number(match["scale"])), "f")
            numbers.append(value.rstrip("0").rstrip(".") if "." in value else value)
        else:
            # 百分号不另算数字；2021、120 和 2.5 等仍按原有完整数字串比较。
            numbers.append(match["arabic"])
    return numbers


def new_numbers(after: str, resume_text: str) -> list[str]:
    """按红线②找出简历原文未出现的数字，允许中文同数写法变化。

    参数：after 为建议或改写正文，resume_text 为唯一的数字依据。
    返回：违规的归一化数字串，按首次出现去重；空列表表示通过。
    异常：无（参数须符合类型契约）。
    """
    # JD 里的数字不能证明候选人的经历；此函数刻意不接收 JD 参数。
    allowed = set(_number_tokens(resume_text))
    # 比较完整数值，不能用原文里的 5 或「万」分别为 5 万背书。
    return list(dict.fromkeys(number for number in _number_tokens(after) if number not in allowed))


def _validate_text(original: str, after: str, resume_text: str, jd_text: str) -> tuple[bool, str]:
    """按固定优先级核对一组原句和改写。

    参数：original 为引用原句，after 为新正文，其他两项为各自原文。
    返回：(True, "") 或 (False, 首个违规原因与具体词或数字)。
    异常：无（参数须符合类型契约）。
    """
    # 先定位再检查新事实，确保失败条目总能说明最基础的问题。
    # 所有失败都保留原输入；调用方自行记录拒绝原因，不在此原地修正。
    if not find_original(resume_text, original):
        return False, "原句无法在简历原文中检索到"
    for label, violations in (
        ("新增技术词", new_tech_terms(after, resume_text, jd_text)),
        ("新增专名", new_proper_nouns(after, resume_text, jd_text)),
        ("新增数字", new_numbers(after, resume_text)),
    ):
        # 返回具体违规内容供 rejected_issues / skipped 直接保留。
        if violations:
            return False, f"{label}无原文依据：{'、'.join(violations)}"
    return True, ""


def validate_issue(issue: ReviewIssue, resume_text: str, jd_text: str) -> tuple[bool, str]:
    """综合校验修改意见的原句、名词和数字。

    参数：issue 为意见模型，resume_text、jd_text 为未经改写的两份原文。
    返回：(True, "") 表示通过；否则为 (False, 具体原因)。
    异常：无（参数须符合类型契约）。
    """
    # 仅核对将进入简历的建议正文，解释字段不充当白名单来源。
    return _validate_text(issue.original_text, issue.suggested_text, resume_text, jd_text)


def validate_adaptation(adapted: AdaptedText, resume_text: str, jd_text: str) -> tuple[bool, str]:
    """综合校验实际改写，要求 before 在简历原文可检索。

    参数：adapted 为改写模型，resume_text、jd_text 为两份原文。
    返回：(True, "") 表示通过；否则为 (False, 具体原因)。
    异常：无（参数须符合类型契约）。
    """
    # 重新检查 after，不相信调用方给出的 added_terms / kept_numbers 记录。
    return _validate_text(adapted.before, adapted.after, resume_text, jd_text)


def resolve_job_input(title: str | None, jd_text: str | None) -> tuple[str, str | None]:
    """按 PRD §5.0 与任务书阈值选择岗位输入模式。

    参数：title 为可空岗位名，jd_text 为可空 JD 原文，均按 strip 后长度判断。
    返回：有效 JD 优先为 ("user_jd", None)；否则有效岗位名为 ("inferred", None)；
        两者均无效为 ("", 人话错误)，供调用方返回 422。
    异常：无（参数须符合类型契约）。
    """
    # 不比较岗位名语义；名称与 JD 不一致的提示留给调用方。
    if len((jd_text or "").strip()) >= 20:
        return "user_jd", None
    # 不足阈值的 JD 视为缺失，仍允许有效岗位名走推测模式。
    # 此处仅返回模式与文案，HTTP 状态码由后续接口层负责。
    if len((title or "").strip()) >= 2:
        return "inferred", None
    return "", "请至少填写岗位名称，或粘贴岗位 JD"
