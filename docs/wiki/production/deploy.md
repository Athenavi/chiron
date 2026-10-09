# 生产使用：部署

> **权威来源**：`README.md` §快速开始（容器）· [多实例部署](../../deployment-multi-instance.md) ·
> `deploy/k8s/README.md`。本页只给「上线要过哪几道」。

## 三种形态

| 形态 | 入口 | 适用 |
|---|---|---|
| **本地开发** | 前端 `:5173` | 开发调试（见 [快速入门](../getting-started/quickstart.md)） |
| **容器（单机）** | frontend 容器的 nginx `:3000` | 小规模 / 演示 |
| **容器（多副本）** | 同上，`--scale gateway=N --scale python-engine=N` | 生产（见 [多实例](multi-instance.md)） |

## 上线四步

```bash
cp .env.example .env                  # 1) 填必填项（compose 里 ${VAR:?…} 声明的都是必填）
python scripts/init.py --check        # 2) 自检必填项与 PostgreSQL/Redis 连通性

# 3) 先迁移（库由云厂商/DBA 维护，不在 compose 内）
python -m pip install -r requirements-migrate.txt
DATABASE_DSN='postgresql://…' python -m alembic -c alembic.ini upgrade head

# 4) 起应用层
docker compose up -d redis minio etcd milvus
docker compose up -d --scale gateway=2 --scale python-engine=2
```

## 四个必须检查的点

1. **迁移链恰好 1 个 head** —— `python -m alembic heads`；多 head 会让 `upgrade head` 变成歧义操作。
   离线环境用 `upgrade head --sql` 生成 DDL 交 DBA 审阅。
2. **`market/skills/` 必须挂载** —— 内置技能是目录型资产（`{name}/SKILL.md`），
   compose 已只读挂载并设 `CHIRON_BUILTIN_SKILLS_PATH`；**源缺失会告警，且技能列表不含内置技能**。
3. **网关读 `REDIS_ADDR`，不是 `REDIS_URL`** —— 混用会起不来。
4. **`/health` 与 `/ready` 都要 200** —— 前者是进程活着，后者是依赖就绪。

## Kubernetes

`deploy/k8s/README.md` 记录了「Chiron 能否无缝扩展」的本地验证 —— **先读它再改编排**，
里面有实测结论与限制。

## 延伸阅读

- [多实例](multi-instance.md) · [可观测性](observability.md) · [配置参考](../reference/config.md)
