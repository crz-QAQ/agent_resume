FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    HF_ENDPOINT=https://hf-mirror.com

WORKDIR /app

# 先装依赖（层缓存：requirements.txt 不变就不重装）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
        --index-url https://mirrors.aliyun.com/pypi/simple/

# ⚠️ 试过在同一层加 `pip uninstall -y pip setuptools` 来减小体积 —— 无效 ✗
#    镜像按层累加，后续层里删文件只是加一个 whiteout 标记，
#    字节仍然留在前一层里，`docker images` 看到的大小不会变小。
#    真要减体积必须用 multi-stage build（本项目的体积大头是 numpy/pillow/lxml
#    这类编译好的二进制轮子，删了功能就跑不了，所以选择接受 488MB）。

COPY app/ ./app/

# 非 root 用户运行

RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

# 从项目根目录加载app包，与本地启动命令一致。
WORKDIR /app

EXPOSE 8000
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
