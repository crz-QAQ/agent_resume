"""限制匹配上传大小，并确保multipart解析不会把简历缓冲到临时磁盘。"""

from fastapi import HTTPException, Request
from fastapi.routing import APIRoute
from starlette.formparsers import MultiPartException, MultiPartParser

# 在文件分片进入缓冲前拦截；不能只在端点读取UploadFile时才判断大小。
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class _UploadTooLarge(MultiPartException):
    """让multipart层的超限与格式错误分别映射为413、422。"""
    # 继承解析器已处理的异常基类，使其在失败时关闭所有已创建的句柄。


class _MemoryParser(MultiPartParser):
    """在达到磁盘切换阈值之前拒绝超限文件，合法上传始终留在内存。"""

    # 比允许上限多一字节，on_part_data会在缓冲增长到该值之前抛错。
    spool_max_size = MAX_UPLOAD_BYTES + 1

    def on_part_begin(self) -> None:
        """沿用标准multipart结构，只额外记录当前文件分片累计大小。"""
        super().on_part_begin()
        # 多个分片属于同一文件时累计，同一表单的下一part重新从零计数。
        self._file_bytes = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        """文件越过10MB立即拒绝，不把超限片段交给临时文件缓冲。"""
        if self._current_part.file is not None:
            # 回调在Starlette排队写入分片之前执行，超限片段不会进入缓冲。
            self._file_bytes += end - start
            if self._file_bytes > MAX_UPLOAD_BYTES:
                raise _UploadTooLarge("文件超过10MB")
        # JD等文本字段继续使用Starlette自身的字段长度限制。
        super().on_part_data(data, start, end)


class MemoryUploadRoute(APIRoute):
    """仅为匹配接口安装内存multipart解析，不改变旧接口的解析策略。"""

    def get_route_handler(self):
        """预解析表单交给FastAPI校验参数，并确保失败路径也关闭文件。"""
        original = super().get_route_handler()

        async def handle(request: Request):
            """复用同一表单缓存，区分超限和坏multipart，关闭所有内存句柄。"""
            form = None
            try:
                if request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
                    try:
                        # 每次请求独立解析器，不修改Starlette的全局spool设置。
                        form = await _MemoryParser(request.headers, request.stream(), max_files=1, max_fields=1).parse()
                    except _UploadTooLarge as exc:
                        raise HTTPException(413, "文件超过10MB上传上限") from exc
                    except (MultiPartException, ValueError) as exc:
                        # Starlette默认的400不属于匹配接口契约，在这里明确转422。
                        raise HTTPException(422, "上传表单格式无效") from exc
                    request._form = form
                    # FastAPI复用已经解析的表单，不再运行默认磁盘缓冲解析器。
                return await original(request)
            finally:
                # 包含参数校验失败和业务异常，均释放内存中的简历字节。
                if form is not None:
                    await form.close()

        return handle
        # 路由包装器只处理传输协议，不解析简历、不调用任何评分阶段。
