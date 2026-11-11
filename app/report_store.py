"""只缓存最终PDF，使用文件修改时间执行30分钟TTL，不创建原文或侧车文件。"""

from datetime import datetime
from io import BytesIO
from pathlib import Path
import re
from threading import RLock
import time

# 路径以项目根定位，改变启动工作目录不会形成第二个缓存目录。
TTL_MINUTES = 30
REPORT_DIR = Path(__file__).resolve().parents[1] / "reports"
_ID = re.compile(r"[0-9a-f]{32}")
# 同一进程中的保存、清理、读取互斥；可重入锁允许这些方法调用purge。
_lock = RLock()
# 仅保存下载名，不保存原始简历；PDF过期时一起移除。
_filenames: dict[str, str] = {}


def _valid(report_id: str) -> bool:
    """在任何路径拼接之前验证完整的32位小写hex标识。"""
    return isinstance(report_id, str) and _ID.fullmatch(report_id) is not None
    # fullmatch会同时拒绝斜杠、扩展名、大小写变化和尾部额外字符。


def purge_expired() -> int:
    """惰性删除超过30分钟的报告，返回实际删除数量。"""
    with _lock:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        # mtime来自最终报告落盘时间，重启服务后TTL仍有效。
        cutoff, removed = time.time() - TTL_MINUTES * 60, 0
        # 只管理本存储器生成的UUID文件，不误删S7a人工验收报告。
        for path in REPORT_DIR.glob("*.pdf"):
            if not _valid(path.stem) or path.is_symlink():
                # 不跟随目录符号链接，缓存清理始终局限于本目录的报告文件。
                continue
            try:
                if path.stat().st_mtime <= cutoff:
                    path.unlink()
                    # PDF与仅驻内存的下载名同步失效，不保留长期姓名索引。
                    _filenames.pop(path.stem, None)
                    removed += 1
            except FileNotFoundError:
                # 其他进程已清理同一文件时，视作过期缓存已经消失。
                continue
        return removed


def save_report(report_id: str, pdf: bytes, *, filename: str | None = None) -> Path:
    """校验ID、清理过期项，再独占写入最终PDF，可选下载名只缓存于内存。"""
    if not _valid(report_id):
        raise ValueError("非法report_id")
    # 非法ID在创建目录和打开文件之前失败，不会形成任意路径副作用。
    with _lock:
        purge_expired()
        path = REPORT_DIR / f"{report_id}.pdf"
        # 独占创建避免覆盖既有文件或跟随同名符号链接。
        with path.open("xb") as stream:
            try:
                stream.write(pdf)
            except BaseException:
                stream.close()
                path.unlink(missing_ok=True)
                raise
        if filename is not None:
            # 下载名是少量元数据，不创建JSON/TXT侧车文件。
            _filenames[report_id] = filename
        return path


def load_report(report_id: str) -> bytes | None:
    """非法、不存在或过期ID返回None；合法ID读取前也执行惰性清理。"""
    if not _valid(report_id):
        return None
    # 不对非法输入调用Path，目录穿越字符串无法触达文件系统。
    with _lock:
        purge_expired()
        path = REPORT_DIR / f"{report_id}.pdf"
        if path.is_symlink():
            # UUID形状也不能为外部文件的同名符号链接提供下载能力。
            return None
        try:
            return path.read_bytes()
        except FileNotFoundError:
            # 文件在清理和读取之间消失时，与普通缓存未命中使用同一契约。
            return None


def download_filename(report_id: str, pdf: bytes) -> str:
    """取内存下载名；进程重启后从最终PDF抬头恢复PRD命名所需的字段。"""
    if not _valid(report_id):
        raise ValueError("非法report_id")
    with _lock:
        cached = _filenames.get(report_id)
    if cached:
        return cached
    # 缓存只优化常规请求；进程重启后仍可从持久化PDF恢复正常下载名。
    # 仅解析已经允许落盘的报告，不读取或重新构造上传原文。
    import pdfplumber
    from app.match_pipeline import report_filename

    with pdfplumber.open(BytesIO(pdf)) as document:
        text = document.pages[0].extract_text() or ""
    match = re.search(r"简历：(.*?)\s*岗位：(.*?)\s*生成：(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4})", text, re.S)
    if not match:
        # 不猜测候选人或岗位字段，不能把损坏缓存包装成成功命名。
        raise ValueError("报告缺少下载命名信息")
    resume, title, stamp = match.groups()
    generated_at = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S %z").isoformat()
    return report_filename(resume, title, "", generated_at)


def stats() -> dict:
    """返回当前有效缓存数量和TTL，不暴露简历字段或报告内容。"""
    with _lock:
        # 读取统计也先去掉过期项，cached_reports不会包含已经失效的ID。
        purge_expired()
        count = sum(_valid(path.stem) and path.is_file() and not path.is_symlink()
                    for path in REPORT_DIR.glob("*.pdf"))
        return {"cached_reports": count, "ttl_minutes": TTL_MINUTES}
