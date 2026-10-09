# 安装

> **权威来源**：`README.md` §快速开始 / §关键配置 · `docs/contributing.md` §工具链版本。
> 本页只给「先装什么、怎么起、卡在哪」，具体命令以那两处为准。

## 先决条件

| 依赖 | 说明 |
|---|---|
| **PostgreSQL + pgvector** | 业务库；迁移由 Alembic 管 |
| **Redis** | 会话事件流 / 锁 / 心跳（多副本一致性的基础） |
| **Milvus · MinIO/S3** | 向量检索与对象存储（知识库 / 媒体） |
| Go / Python / Node + pnpm | 工具链版本见 `docs/contributing.md` §工具链版本 |

**容器形态里没有数据库**：PostgreSQL 由云厂商或 DBA 维护，**不在 compose 内** ⇒ 必须先把 `DATABASE_DSN`
指向一个可达且已迁移的库。

## 两条路

### 本地开发（非容器）

```bash
python scripts/init.py           # 交互式初始化：生成 APP_SECRET、写 .env、自检 PostgreSQL/Redis
python run.py setup              # 安装 Python / 前端依赖
python -m alembic upgrade head   # 数据库迁移（需 PostgreSQL 可达）
python run.py start              # 网关 :8080 + 引擎 :8000 + 前端 :5173
```

打开前端注册第一个账号 —— **它自动成为系统管理员（owner）**。

### 容器（多副本编排）

```bash
cp .env.example .env             # 填写 compose 必填项
python scripts/init.py --check   # 可选：自检必填项与 PostgreSQL/Redis 连通性

# 1) 先对目标 PostgreSQL 执行迁移（库不在 compose 内）
python -m pip install -r requirements-migrate.txt
DATABASE_DSN='postgresql://user:pwd@your-pg:5432/dbname' \
  python -m alembic -c alembic.ini upgrade head

# 2) 启动应用层
docker compose up -d redis minio etcd milvus
docker compose up -d --scale gateway=2 --scale python-engine=2
```

对外入口是 frontend 容器的 nginx：`http://localhost:3000`。

## 三个最容易踩的坑

1. **网关读 `REDIS_ADDR`，不是 `REDIS_URL`** —— 两者格式不同，混用会起不来。
2. **Go 命令要带 `-mod=mod`** —— 用来排除 `vendor/` 下的参考源码目录（那是只读对标仓库）。
3. **迁移链必须恰好 1 个 head** —— `python -m alembic heads` 自查；容器部署时
   `market/skills/` **必须挂载**（compose 已只读挂载并设 `CHIRON_BUILTIN_SKILLS_PATH`），
   源缺失会告警且技能列表不含内置技能。

## 下一步

- [快速入门](quickstart.md) —— 五条命令跑通一次对话
- **关键配置** —— 根 `README.md` §关键配置（环境变量清单）
- [贡献与验收](../contributing/overview.md) —— 想把整套测试跑起来看这里
