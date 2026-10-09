# 参考：命令行

> **权威来源**：`cmd/`（两个 Go 命令）· `run.py` · `scripts/cli/` · `clients/python/`。

## 两个 Go 命令

```
cmd/chiron        网关本体（跑服务的那个）
cmd/chiron-cli    管理本地启动的进程（开发/运维用）
```

`chiron-cli` 的子命令（文件名即命令名）：

| 子命令 | 作用 |
|---|---|
| `start` / `stop` / `status` | 起停与状态 |
| `run` | 前台运行 |
| `logs` | 看日志 |
| `config` | 配置检查 |
| `db` | 数据库相关 |
| `health` | 健康检查 |
| `instance` | 实例信息（多副本相关） |
| `state` | 本地状态 |

## 一键脚本（本地开发）

```bash
python scripts/init.py      # 初始化：生成 APP_SECRET、写 .env、自检依赖
python run.py setup         # 安装 Python / 前端依赖
python run.py start         # 起网关 + 引擎 + 前端
```

## 迁移与模型生成（`scripts/cli/`）

| 命令 | 作用 |
|---|---|
| `migrate` | Alembic 迁移入口（**Alembic 是唯一迁移入口**，见 [决策记录](../../db-migration-entry.md)） |
| `generate_models` | 由 `configs/orm/V1/models.yaml` 生成 ORM 模型到 `shared/models/` |
| `shell` | 交互式 shell |
| `jinja_filters` | 模板过滤器（生成器内部用） |

## 直接跑（不用 CLI）

```bash
go build -mod=mod ./...                   # 记得 -mod=mod（排除 vendor/ 参考源码）
go run ./cmd/chiron                       # 起网关（读 REDIS_ADDR）
python -m alembic upgrade head            # 迁移
python -m alembic heads                   # 检查：必须恰好 1 个 head
```

## 第一方客户端

`clients/python/` 是一个**示例客户端**（用 SDK 的方式调 Chiron），
也能当"怎么从外部调"的参考实现。

## 延伸阅读

- [配置](config.md) · [部署](../production/deploy.md) · [安装](../getting-started/install.md)
