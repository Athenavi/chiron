# 快速入门

> **权威来源**：`README.md` §快速开始 / §常用命令。目标是**跑通一次真实对话**，不是读文档。

## 五步

```bash
# 1) 初始化：生成 APP_SECRET、写 .env、自检 PostgreSQL/Redis
python scripts/init.py

# 2) 装依赖（Python + 前端）
python run.py setup

# 3) 迁移到最新
python -m alembic upgrade head

# 4) 起三个进程：网关 :8080 + 引擎 :8000 + 前端 :5173
python run.py start
```

**5)** 打开前端，注册第一个账号 —— **它自动成为系统管理员（owner）**，然后新建会话发一条消息。

## 验证是不是真的起来了

```bash
curl -sS localhost:8080/health && curl -sS localhost:8080/ready   # 都应 200
```

`/health` 是进程活着，`/ready` 是依赖就绪 —— **两者都 200 才算好**。

## 只跑后端时（不启前端）

```bash
# 起本地 Redis
redis-server --port 6390 --appendonly no --save ""

# 起网关（注意是 REDIS_ADDR，不是 REDIS_URL）
REDIS_ADDR=127.0.0.1:6390 go run ./cmd/chiron
```

## 想看它和单机 harness 的差别

- [架构](../core/architecture.md) —— 一条请求穿过网关 / 引擎 / 存储的路径
- [设计理念](philosophy.md) —— 为什么有些能力（多副本一致性、租户审计）在这里是必需的
