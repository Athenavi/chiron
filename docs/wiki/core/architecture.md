# 架构

> **权威来源**：`docs/architecture.md`（架构与请求链路）。本页是导读与**契约索引**。

## 三层

```
浏览器 ─► 前端(Nginx/Vue 3, :3000) ─► Go 网关(cmd/chiron, :8080)
                                        │
                                        ▼
                          Python 引擎(python-engine, :8000)
                                        │
              PostgreSQL(pgvector) · Redis ◄──┴──► Milvus · MinIO/S3
```

| 层 | 职责 | 代码位置 |
|---|---|---|
| **前端** | 会话界面；nginx 反代 `/v1`、`/events`、`/ws`、`/submit`、`/cancel`、`/media` 到网关 | `frontend-vue/` |
| **网关** | 鉴权、路由、SSE 广播、会话运行锁与归属路由、计费、媒体与存储 | `cmd/` · `internal/` |
| **引擎** | agent 循环、工具执行、子 Agent、RAG、工作流、记忆 | `python-engine/` |
| **存储** | 业务库 + 事件流 + 向量 + 对象 | `migrations/` · `configs/orm/` |

**网关薄、引擎厚**：`internal/` 里大量是路由与胶水（这也是为什么单看 Go 的代码量会低估它）。

## 目录结构（关键几处）

```
cmd/               网关与 CLI（chiron 网关；chiron-cli 管理本地启动的进程）
internal/          Go 网关实现（api、auth、billing、broadcast、db、engine、session、storage…）
python-engine/     引擎实现（app/agent、queue、api、tools、workflow、memory、rag…）
frontend-vue/      Vue 3 前端
migrations/        Alembic 迁移；ORM 模型由 configs/orm/V1/models.yaml 生成到 shared/models/
market/skills/     内置技能资产（{name}/SKILL.md 目录型技能）—— 容器部署必须挂载
```

## 关键契约索引（改行为前先读这些）

| 主题 | 契约 |
|---|---|
| 会话运行时状态与遥测 | [session-runtime-spec.md](../../session-runtime-spec.md) |
| 会话地图与分支 | [session-map-branch-design.md](../../session-map-branch-design.md) |
| transcript（消息列表几何 / 窗口化 / 单写者） | [transcript-contract.md](../../transcript-contract.md) |
| 错误码（Go `Code*` ↔ 三语言 `errors.ts`） | [error-codes.md](../../error-codes.md) |
| 子 Agent | [subagent-design.md](../../subagent-design.md) |
| 钩子协议 | [hook-protocol-design.md](../../hook-protocol-design.md) |
| run checkpoint 续跑 | [run-checkpoint-design.md](../../run-checkpoint-design.md) |
| 安全与可靠性（威胁模型） | [agent-safety-and-reliability.md](../../agent-safety-and-reliability.md) |
| 多实例部署 | [deployment-multi-instance.md](../../deployment-multi-instance.md) |
| 模型服务提供商目录 | [service-providers.md](../../service-providers.md) |

> 其中几条**不只是文档**：`transcript` / `error-codes` / `service-providers` / 工具策略都有
> **CI 门禁**在守（见 [贡献与验收](../contributing/overview.md)）。契约一旦写成门禁，改代码就会先撞门禁。

## 下一步

- [贡献与验收](../contributing/overview.md) —— 门禁清单与自查流程
- [设计理念](../getting-started/philosophy.md) —— 为什么这些契约必须机械化
