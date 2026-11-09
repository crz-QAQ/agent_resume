FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

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

# 只拷 练习/ —— .env 绝不 COPY

COPY app/ ./app/

# 非 root 用户运行

RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

# ⚠️ 关键：必须是 /app/app

#    因为 Day7_project.py 用的是平铺导入（from Day8_n3_structured import ...）

#    Python 只在 cwd 里找兄弟模块

WORKDIR /app/app

EXPOSE 8000
CMD ["uvicorn", "Day7_project:app", "--host", "0.0.0.0", "--port", "8000"]