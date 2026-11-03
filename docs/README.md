# docs/

面向**使用与维护**的文档。内部设计讨论、历史方案与路线图不进入本仓库。

| 文档 | 内容 |
|---|---|
| [architecture.md](architecture.md) | 组件拓扑、一次对话请求的完整链路、事件回传与断线重放、凭证与信任边界 |
| [deployment-multi-instance.md](deployment-multi-instance.md) | 多副本部署、关键变量、伸缩注意项、连接预算、已知边界 |
| [db-migration-entry.md](db-migration-entry.md) | 数据库迁移入口与禁止项（Alembic 是唯一入口） |
| [error-codes.md](error-codes.md) | 稳定错误码契约、`retryable` 语义、三语言同步清单 |
| [session-runtime-spec.md](session-runtime-spec.md) | 会话运行时状态（模式 / 模型 / 授权）的存放与保鲜规则 |
| [transcript-contract.md](transcript-contract.md) | 转录渲染契约（单一写者、滚动、分组） |
| [service-providers.md](service-providers.md) | LLM provider 接入、密钥集中管理与故障注入 |
| [mail.md](mail.md) | 邮件通道配置与模板 |

新增文档前先确认：它回答的是"怎么用 / 怎么维护"，而不是"当初为什么这么设计"。
后者写在代码注释或 PR 里。
