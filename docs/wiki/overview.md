# 概述

**Chiron 是一个多租户 SaaS 形态的 AI Agent 平台。** 它把「一个 agent 循环」做成可被多个租户共享、
且能被审计与配额约束的服务：Go 网关负责鉴权/路由/SSE/计费，Python 引擎负责 agent 循环、
工具执行、RAG 与工作流，Vue 3 前端负责会话界面。

## 架构一眼

```
浏览器 ─► 前端(Nginx/Vue 3, :3000) ─► Go 网关(cmd/chiron, :8080)
                                        │
                                        ▼
                          Python 引擎(python-engine, :8000)
                                        │
              PostgreSQL(pgvector) · Redis ◄──┴──► Milvus · MinIO/S3
```

- **网关薄、引擎厚**：`internal/` 里大量是路由与胶水；能力主要长在 `python-engine/`。
- **对外入口**：本地开发直连 `:5173`；容器形态走 frontend 容器的 nginx（`:3000`）。

> 权威来源：`README.md` §架构 / §目录结构 · [架构](core/architecture.md)

## 平台面

多租户、配额、计费、多副本协调、身份/会话隔离是**平台层**的能力，
与"单机 agent 运行时"不在同一层，因此不放在同一张对照表里比较。
它们的实现方式与验证方法见 [多实例部署指南](../deployment-multi-instance.md)。

## 从哪里继续

| 我想…… | 去哪 |
|---|---|
| 把它跑起来 | [安装](getting-started/install.md) → [快速入门](getting-started/quickstart.md) |
| 看懂一条请求怎么走 | [架构](core/architecture.md) |
| 知道哪些事**明确不做** | [设计理念](getting-started/philosophy.md) |
| 提交代码 | [贡献与验收](contributing/overview.md) |
