# 数据库迁移镜像（只在发布流程里跑一次，不是应用运行时镜像）
#
# 为什么需要单独一个镜像：**应用从不执行 DDL**（网关启动只做只读的 schema 版本校验，
# 见 internal/db/schema_version.go；不一致直接 `FATAL: refusing to start`）。因此
# "先迁移、后滚动"是发布流程的硬前置。而两个运行时镜像都不适合跑迁移：
#   * chiron/gateway 只有二进制 + migrations/，没有 python/alembic；
#   * chiron/python-engine 有 python，但 requirements.txt 里**没有** alembic
#     （迁移依赖刻意单独放在 requirements-migrate.txt，避免把 SQLAlchemy 塞进运行时）。
#
# 构建（**在仓库根目录**执行，上下文需要 requirements-migrate.txt / alembic.ini / migrations/）：
#   docker build -f deploy/migrations/Dockerfile -t chiron/migrations:dev .
#   docker push <registry>/chiron/migrations@sha256:<digest>   # 生产用 digest，别用浮动 tag
#
# 运行等价于：
#   DATABASE_DSN=... python -m alembic -c alembic.ini upgrade head
#
# 只想给 DBA 出 SQL（离线审阅、在受控窗口执行）时改用：
#   python -m alembic -c alembic.ini upgrade head --sql
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 只装迁移依赖：这个镜像不该带应用运行时依赖（也不会被应用引用）。
COPY requirements-migrate.txt ./
RUN pip install --no-cache-dir -r requirements-migrate.txt

# env.py 按 script_location = migrations 找迁移链；alembic.ini 与 migrations/ 必须同层。
COPY alembic.ini ./
COPY migrations/ ./migrations/

# 非 root：与清单里 Job 的 runAsNonRoot/runAsUser 一致。
# UID 固定 1001，避免"镜像里是别的 uid"导致挂载卷写不进去。
RUN useradd --system --uid 1001 --user-group migrate && chown -R 1001:1001 /app
USER 1001

# 迁移是幂等的（alembic 只执行未应用过的 revision），但**不要**并发跑多个：
# 两个 Job 同时 upgrade 会在 alembic_version 行锁上互相等待，甚至报 version 冲突。
ENTRYPOINT ["python", "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"]
