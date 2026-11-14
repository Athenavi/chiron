<div align="center">
  <img src="assets/chiron-logo-detail.svg" width="140" alt="Chiron">

  <h1>Chiron</h1>

  <p><b>多租户 AI Agent 平台</b><br/>
  对话 · Agent · 技能 · 知识库 · 多层记忆，一套能自己部署、能跑第二个副本的系统</p>

  <p>
    <a href="https://github.com/Athenavi/chiron/actions/workflows/ci.yml"><img src="https://github.com/Athenavi/chiron/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
    <img src="https://img.shields.io/badge/Go-1.26-00ADD8?logo=go&logoColor=white" alt="Go 1.26">
    <img src="https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white" alt="Python 3.14">
    <img src="https://img.shields.io/badge/Vue-3-4FC08D?logo=vue.js&logoColor=white" alt="Vue 3">
  </p>

  <p>
    <a href="#快速开始">快速开始</a> ·
    <a href="docs/architecture.md">架构</a> ·
    <a href="docs/deployment-multi-instance.md">部署</a> ·
    <a href="CONTRIBUTING.md">贡献</a> ·
    <a href="#english">English</a>
  </p>
</div>

---

## 为什么是 Chiron

大多数 Agent 框架在单进程 demo 里都很好用。真正难的是第二件事：**当你要把它交付给一个组织时**。

Chiron 从第一天就按"多人、多租户、能自己部署"来设计：

| 你会遇到的问题 | Chiron 的处理 |
|---|---|
| 一个实例要服务全公司，成员各建自己的 Agent | 租户 / 用户双层隔离，连数据库层都有租户上下文 |
| 扩容到第二个副本后会话锁、事件流全乱 | 跨实例会话锁、SSE 跨副本广播与断线重放、run 归属路由 |
| 计费重试导致重复扣费 | `turn_id` 唯一索引 + 幂等闸门 |
| 工具执行把主进程拖垮，或越权访问宿主机 | 执行下沉到独立沙箱容器；沙箱不可用时**拒绝启动**而非偷偷降级 |
| 出问题后不知道 Agent 到底做了什么 | 思考过程与工具调用逐步可见，轨迹可回放 |

> **它不是什么**：不是低代码可视化搭建平台，也不是模型评测 / 微调平台。

## 快速开始

一条命令拉起完整栈（PostgreSQL、Redis、迁移、网关、引擎、前端全自动）：

```bash
python scripts/demo_up.py
# 打开 http://localhost:3000 —— 注册的第一个账号自动成为系统管理员
```

密钥随机生成，不需要先填任何配置。要真的对话，给一个模型 key 即可
（也可以在登录后的「系统设置 → 服务提供商」里填）：

```bash
ANTHROPIC_API_KEY=sk-... python scripts/demo_up.py
```

> 这套体验栈有意放弃生产属性（单副本、进程内沙箱、本地存储），只为让你尽快看到产品。
> 真正部署请看 [多实例部署指南](docs/deployment-multi-instance.md)。

<details>
<summary><b>本地开发（非容器）</b></summary>

```bash
python scripts/init.py           # 交互式初始化：生成 APP_SECRET、写 .env、自检依赖
python run.py setup              # 安装 Python / 前端依赖
python -m alembic upgrade head   # 数据库迁移
python run.py start              # 网关 :8080 + 引擎 :8000 + 前端 :5173
```

需要 Go 1.26+、Python 3.14、Node 22、pnpm 10、PostgreSQL、Redis。

> 引擎默认 `SANDBOX_BACKEND=service`（工具执行下沉到独立容器），本机没有该容器时
> 启动探测会失败并**拒绝启动**。本地开发请在 `.env` 设 `SANDBOX_BACKEND=local`，
> 或设 `DEGRADED_MODE=true` 允许回退进程内执行。
</details>

<details>
<summary><b>容器部署（多副本）</b></summary>

```bash
cp .env.example .env                            # 填写 compose 必填项
python -m pip install -r requirements-migrate.txt
DATABASE_DSN='postgresql://user:pwd@your-pg:5432/dbname' \
  python -m alembic -c alembic.ini upgrade head  # 迁移由发布流程执行，应用只做只读校验
docker compose up -d --scale gateway=2 --scale python-engine=2
# 入口是 frontend 容器的 nginx：http://localhost:3000
```

PostgreSQL 由云厂商或 DBA 维护，不在 compose 内。完整说明见
[多实例部署指南](docs/deployment-multi-instance.md)。
</details>

## 架构

```
浏览器 ──► 前端 (Vue 3 + nginx) ──► Go 网关 (:8080) ──► Python 引擎 (:8000)
                                        │                    │
                              PostgreSQL · Redis ◄────────────┘
                                        │
                              S3 / RustFS · 沙箱服务
```

| 组件 | 目录 | 职责 |
|---|---|---|
| **Go 网关** | `cmd/chiron`、`internal/` | 认证、路由、限流、计费、管理后台、SSE 汇聚与转发 |
| **Python 引擎** | `python-engine/` | 对话、Agent 运行时、知识库、技能、记忆、MCP |
| **前端** | `frontend-vue/` | Vue 3 + TypeScript + Vite；nginx 同源反代 |
| **数据库** | `migrations/` | PostgreSQL，Alembic 迁移（手写 SQL，无 ORM 生成） |
| **缓存 / 队列** | — | Redis：会话、限流、后台任务、SSE 事件缓冲、run 归属 |

一次对话请求从浏览器到引擎再回到浏览器的完整链路（每步附代码位置与自查命令）见
[架构与请求链路](docs/architecture.md)。

## 特性

- **Agent 运行时** — 工具授权链、人在环审批（可跨进程答复）、检查点续跑、子 Agent 委派
- **多层记忆** — 会话摘要、长期记忆检索、上下文压缩与预算控制
- **知识库** — 文档索引与检索，附件按需引用化（不随历史驻留）
- **技能系统** — 目录型 `SKILL.md` 与扁平 `.skill.json` 双载体，支持用户覆盖内置
- **27 家模型提供商** — 内建目录，按模型名路由，密钥集中管理
- **管理后台** — 租户配额、计费、审计日志、邀请码与套餐、三语言界面（含 RTL）

## 关键配置

| 变量 | 说明 |
|---|---|
| `APP_SECRET` | **必填**。部署级主密钥（≥32 字符），派生 `JWT_SECRET` / `INTERNAL_TOKEN` 并加密后台敏感配置 |
| `POSTGRES_DSN` / `REDIS_URL`（网关用 `REDIS_ADDR`） | 基础设施连接串 |
| `DEGRADED_MODE` | 默认 `false`：Redis 不可用时**拒绝启动**；仅单机开发设 `true` |
| `SANDBOX_BACKEND` | 默认 `service`：执行下沉到独立沙箱容器；`local` 回退进程内 |
| `ALLOW_SCHEMA_DRIFT` | 默认 `false`：启动时校验迁移版本，不一致**拒绝启动** |
| `MCP_POOL_ENABLED` | 默认 `false`：每副本为活跃用户持有 MCP 连接，需要时显式开启 |

完整变量表见 [多实例部署指南](docs/deployment-multi-instance.md#3-关键变量)。

## 数据库迁移

应用**不自行迁移**，启动时只做只读的 schema 版本校验。迁移是发布流程的一次性步骤：

```bash
python -m pip install -r requirements-migrate.txt
DATABASE_DSN='postgresql://user:pwd@host:5432/db' \
  python -m alembic -c alembic.ini upgrade head
```

`migrations/versions/` 以单一权威基线 + 增量迁移组织，迁移链必须只有一个 head。
细节与禁止项见 [迁移入口](docs/db-migration-entry.md)。

## 常用命令

```bash
go build -mod=mod ./...                    # Go 编译
go test -mod=mod ./...                     # Go 测试
python -m pytest python-engine/tests -q    # 引擎测试
make check                                 # 提交前快检（Go + 文档守卫，几十秒）
make check-all                             # 全量（再加 Python 与前端）
```

> `-mod=mod` 是因为仓库本地若有 `vendor/` 参考源码目录，Go 会按 vendor 模式做一致性检查。
> 干净检出的仓库不需要该参数。

## 贡献

欢迎参与。提交前请在本地跑过 [CONTRIBUTING](CONTRIBUTING.md) 中的检查，
CI 的五个 job 与本地命令逐条对应。

从 `good first issue` 开始最省力 —— 它们范围明确，且有机械可判定的验收方式。

## 安全

漏洞请通过 [SECURITY.md](SECURITY.md) 的私有渠道报告，**不要**开公开 issue。

## 许可

[MIT](LICENSE)

---

## English

**Chiron** is a multi-tenant AI agent platform built for horizontal scale: a Go gateway
(auth, rate limiting, billing, SSE), a Python engine (agent runtime, RAG, memory, MCP),
and a Vue 3 frontend.

What separates it from demo-grade agent frameworks is what happens with a **second
replica**: cross-instance session locks, SSE replay between replicas, run-affinity
routing for approvals, idempotent billing, crash takeover of queued work, and tool
execution isolated in a separate sandbox container — fail-closed, the engine refuses to
start if the sandbox is unreachable.

### Try it in one command

```bash
python scripts/demo_up.py   # then open http://localhost:3000
```

Brings up PostgreSQL, Redis, runs migrations, and starts all three tiers with randomly
generated secrets — no configuration needed. The first account you register becomes the
system owner. To actually chat, pass a model key:
`ANTHROPIC_API_KEY=sk-... python scripts/demo_up.py`.

This stack is evaluation-only (single replica, in-process sandbox, local storage).
For a real deployment see [docs/deployment-multi-instance.md](docs/deployment-multi-instance.md).

- Docs: [docs/architecture.md](docs/architecture.md) (primary language is Chinese)
- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md)
- Security: [SECURITY.md](SECURITY.md) — private channel only
- License: MIT
