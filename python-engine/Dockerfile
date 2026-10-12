# Multi-stage build — Python AI Engine v0.1.260825.01
FROM python:3.11-slim AS base

WORKDIR /app

# 系统依赖（ffmpeg：video_analyze 抽帧需要；**不引 Python 依赖**，见 app/tools/video.py）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libffi-dev \
    libmagic1 \
    file \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# 依赖安装（缓存层）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码（排除 examples/ 目录，防止生产环境意外加载 mock 代码）
COPY app/ app/

# 非 root 用户
RUN useradd -m -u 1000 appuser
USER appuser

# 端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

# 启动
CMD ["python", "-m", "app.main"]
