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
# 多产出使用独立命名空间；下载名只留在内存，不生成包含个人字段的侧车文件。
ARTIFACT_KINDS = ("review", "resume")
ARTIFACT_EXTS = {"review": "pdf", "resume": None}
_ARTIFACT = re.compile(r"([0-9a-f]{32})__(review|resume)\.(pdf|docx)")
_artifact_filenames: dict[str, str] = {}


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
        # 旧报告的命名与清理逻辑保持原样，新增产物另按严格文件名白名单清理。
        return removed + _purge_artifacts(cutoff)


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


def _artifact_paths(artifact_id: str) -> list[Path]:
    """只用已校验 ID 和服务端固定后缀定位产物，不接受展示名或任意路径。"""
    # 非法值不会触发目录创建、通配搜索或任何用户控制的路径解析。
    if not _valid(artifact_id):
        return []
    # ID 合法之后才构造路径；每个 ID 唯一对应一个产物。
    return [REPORT_DIR / f"{artifact_id}__{kind}.{ext}" for kind, ext in
            (("review", "pdf"), ("resume", "pdf"), ("resume", "docx"))]


def _purge_artifacts(cutoff: float) -> int:
    """清理多产出的 PDF/Word 文件与下载名，仅由已持锁的统一清理入口调用。"""
    removed = 0
    for path in REPORT_DIR.iterdir():
        match = _ARTIFACT.fullmatch(path.name)
        # 非本模块文件及符号链接不跟随、不删除，review 仅允许 PDF。
        if not match or path.is_symlink() or not path.is_file() or match.groups()[1:] == ("review", "docx"):
            continue
        try:
            if path.stat().st_mtime <= cutoff:
                path.unlink()
                _artifact_filenames.pop(match[1], None)
                removed += 1
        except FileNotFoundError:
            # 与其它进程同时清理时，已消失文件不算本次删除。
            continue
    return removed


def save_artifact(artifact_id: str, kind: str, data: bytes, *, filename: str) -> Path:
    """独占保存最终意见或修改版，展示名只提供白名单扩展名与下载头元数据。"""
    # 保存也先拒绝非法 ID，不能等到打开文件时才依赖操作系统阻止穿越。
    if not _valid(artifact_id):
        raise ValueError("非法 artifact_id")
    if kind not in ARTIFACT_KINDS:
        raise ValueError("不支持的产物类型")
    # 不用 filename 拼接路径，只读取末尾扩展名并验证固定白名单。
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in {"pdf", "docx"} or (ARTIFACT_EXTS[kind] and ext != ARTIFACT_EXTS[kind]):
        raise ValueError("产物仅支持 pdf/docx，意见报告必须是 pdf")
    # 复用同一把锁，使旧报告与新产物的 TTL 清理保持原子性。
    with _lock:
        purge_expired()
        # 一个 ID 不能保存多个 kind，load_artifact(id) 才始终没有歧义。
        if any(path.exists() or path.is_symlink() for path in _artifact_paths(artifact_id)):
            raise FileExistsError("产物 ID 已存在")
        path = REPORT_DIR / f"{artifact_id}__{kind}.{ext}"
        # 独占创建同时阻止覆盖、重用 ID 与跟随预放置的同名符号链接。
        with path.open("xb") as stream:
            try:
                stream.write(data)
            except BaseException:
                stream.close()
                path.unlink(missing_ok=True)
                raise
        # 文件名即使带路径分隔符也只会出现在已编码的下载头中。
        _artifact_filenames[artifact_id] = filename
        return path


def load_artifact(artifact_id: str) -> tuple[bytes, str] | None:
    """非法、不存在、过期或有歧义的产物返回 None，成功返回文件字节和下载名。"""
    if not _valid(artifact_id):
        return None
    # 合法的 UUID 也必须在读前执行 TTL，不允许通过旧下载链接绕过过期。
    with _lock:
        purge_expired()
        paths = [path for path in _artifact_paths(artifact_id) if path.is_file() and not path.is_symlink()]
        # 外部误放同 ID 的多个产物时拒绝猜测，保持 ID 到文件的一对一关系。
        if len(paths) != 1:
            return None
        path = paths[0]
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        # 重启后没有内存下载名时使用中性文件名，不从上传原文恢复个人身份。
        fallback = ("修改意见报告" if "__review." in path.name else "修改后简历") + path.suffix
        return data, _artifact_filenames.get(artifact_id, fallback)


def artifact_kind(artifact_id: str) -> str | None:
    """返回已校验产物的实际类型，供下载路由拒绝 ID 与 kind 不匹配的请求。"""
    # 类型查询采用与读取相同的白名单，HTTP 路由不自行扫描任意磁盘路径。
    if not _valid(artifact_id):
        return None
    with _lock:
        purge_expired()
        paths = [path for path in _artifact_paths(artifact_id) if path.is_file() and not path.is_symlink()]
        return paths[0].stem.split("__", 1)[1] if len(paths) == 1 else None


def stats_artifacts() -> dict:
    """统计有效意见报告数及统一 TTL，不枚举产物 ID、下载名或原文。"""
    # resume 文件不重复计为意见报告；旧评分 PDF 同样不进入这一计数。
    with _lock:
        purge_expired()
        count = sum(bool(_ARTIFACT.fullmatch(path.name)) and path.is_file() and not path.is_symlink()
                    for path in REPORT_DIR.glob("*__review.pdf"))
        return {"cached_reviews": count, "ttl_minutes": TTL_MINUTES}
