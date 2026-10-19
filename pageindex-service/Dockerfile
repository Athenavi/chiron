# PageIndex 运行时镜像 —— 检索服务与索引 worker 共用。
#
# **与 python-engine 的依赖完全隔离**是这张 Dockerfile 存在的理由：
# pageindex → litellm 要求 openai<3.0.0，而引擎固定 openai==3.29.0，两者的依赖树在同一
# 个环境里无法解析。分开后两边各自演进：升级 pageindex 只重建本镜像。
#
# 两个入口（compose 用同一镜像起两个服务）：
#   检索服务：uvicorn app.search_api:app --host 0.0.0.0 --port 8100
#   索引 worker：python -m app.index_worker
FROM python:3.11-slim

WORKDIR /app

# 系统依赖：pypdfium2（PDF 渲染）要 libglib/libstdc++；curl 供健康检查用。
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libstdc++6 \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用与契约测试一起进镜像：升级 pageindex 时先在构建阶段跑契约测试（tests/test_sdk_contract.py），
# "API 面变了"在构建就失败，而不是上线后才发现。
COPY conftest.py ./
COPY app/ app/
COPY tests/ tests/

RUN useradd -m -u 1000 pi
USER pi

EXPOSE 8100

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8100/healthz || exit 1

CMD ["uvicorn", "app.search_api:app", "--host", "0.0.0.0", "--port", "8100"]
