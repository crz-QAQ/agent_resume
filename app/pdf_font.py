"""为 PDF 提供统一的中文字体入口，避免默认字体缺字及各处重复注册。

ReportLab 默认字体不含中文，不处理就是方块。
非 Windows 环境没有 msyh.ttc 等系统字体时，依赖 STSong-Light CID 兜底。
按系统字体到内置 CID 的顺序回退，供 Canvas、Paragraph 和 Table 复用。
"""

from threading import Lock

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont

# 缓存成功结果供后续 PDF 复用，避免重复 registerFont 导致注册冲突或抛错。
_registered: str | None = None
# 并发首次调用可能同时发现缓存为空；加锁避免重复注册。
_lock = Lock()


def register_cjk_font() -> str:
    """按固定优先级注册中文字体，重复调用复用成功结果。

    参数：无；字体候选及顺序由 S0 任务书固定。
    返回：已注册的 ReportLab 中文字体名。
    异常：RuntimeError，全部候选字体均无法注册时抛出。
    """
    global _registered
    with _lock:
        if _registered is not None:
            return _registered
        # 回退顺序优先使用 Windows 系统字体，再用无需字体文件的 CID 兜底。
        candidates = (
            # 第一级 msyh：.ttc 是多字体集合，必须用 subfontIndex=0 指定子字体，
            # 否则 TTFont 可能因子字体选择或格式兼容问题报错。
            ("msyh", lambda: TTFont("msyh", r"C:\Windows\Fonts\msyh.ttc", subfontIndex=0)),
            # 第二级 simhei：单文件 TTF 避开集合兼容问题，各 Windows 版本最稳，作为第二顺位。
            ("simhei", lambda: TTFont("simhei", r"C:\Windows\Fonts\simhei.ttf")),
            # 第三级 STSong-Light：ReportLab 内置 CID 字体，不需要任何字体文件，
            # 用于无 Windows 字体的服务器/Linux 环境。
            ("STSong-Light", lambda: UnicodeCIDFont("STSong-Light")),
        )
        # 保留各级失败原因，最终错误可直接用于定位字体环境问题。
        errors = []
        for name, factory in candidates:
            try:
                font = factory()
                pdfmetrics.registerFont(font)
                # 没有独立粗体时，Paragraph 的粗体/斜体仍使用中文常规字体。
                # Paragraph 出现 <b> 时会找 bold 变体；找不到可能抛错或使用默认字体，
                # 所以四个变体都指向同一个中文字体，保证中文仍可显示。
                pdfmetrics.registerFontFamily(
                    name, normal=name, bold=name, italic=name, boldItalic=name
                )
            except Exception as exc:
                # 单一候选失败仍可尝试后续字体，不能提前终止整条回退链。
                errors.append(f"{name}: {exc}")
                print(f"字体候选失败: {name}: {exc}")
                continue
            # 仅缓存已完成注册和字体族映射的候选，避免复用半初始化结果。
            _registered = name
            print(f"实际字体: {name}")
            return name
        # 第四级：全部失败必须显式报错，让调用方知道 PDF 无法可靠显示中文。
        # 这里必须抛异常。绝不能静默降级成 Helvetica —— 那样 PDF 里全是方块，
        # 属于数据事故而不是显示问题。
        raise RuntimeError("没有可用的中文字体；" + "；".join(errors))


def get_cjk_font() -> str:
    """提供统一字体入口；首次调用触发注册，后续复用缓存。

    等价于先注册再返回；调用方（Canvas / Paragraph / Table）只需要记这一个入口。

    参数：无。
    返回：可用于 Canvas、Paragraph 和 Table 的中文字体名。
    异常：RuntimeError，没有任何可用中文字体时由注册函数抛出。
    """
    return register_cjk_font()
