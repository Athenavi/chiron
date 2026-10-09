# Chiron Wiki

**Chiron** 是一个**多租户 SaaS 形态的 AI Agent 平台**：Go 网关 + Python 引擎 + Vue 3 前端，
后端依赖 PostgreSQL(pgvector) / Redis / Milvus / MinIO。

## 这套 wiki 怎么用

> **单一事实源声明**：本 wiki 只做**导航 + 精炼导语**，**不复制正文**。
> 每一页的「权威来源」都指向 `docs/` 或仓库里的对应文件 —— 改事实请改那里，不要改这里。
> 这样 wiki 与代码不会各自漂移（这是本仓库一直在防的事）。

## 目录

### 入门

| 页 | 内容 |
|---|---|
| [概述](overview.md) | 定位、架构图、**可比性口径**、三份差距分析入口 |
| [安装](getting-started/install.md) | 先决条件、本地 / 容器两条路、三个最容易踩的坑 |
| [快速入门](getting-started/quickstart.md) | 五条命令跑通一次对话 + 探针验证 |
| [设计理念](getting-started/philosophy.md) | 北极星、判据、**明确不做的事**、证据阶梯 |

### 核心组件

| 页 | 内容 |
|---|---|
| [架构](core/architecture.md) | 三层与请求链路、目录结构、**契约索引** |
| [Go 网关](core/gateway.md) | 鉴权 / 路由 / SSE / 运行锁 / 计费；`internal/` 14 个子域 |
| [Python 引擎](core/engine.md) | agent 循环 / 工具 / 子 Agent / RAG / 工作流 / 记忆 |
| [Vue 前端](core/frontend.md) | 代码分布 + **`check:ui` 八道棘轮** |
| [会话与运行时状态](core/sessions.md) | 历史 / 事件流 / 运行状态 / checkpoint 四样东西 |
| [Transcript](core/transcript.md) | 消息列表契约、7 个纯几何模块、**单写者门禁** |
| [工具与审批](core/tools.md) | 工具面、**两侧策略同构**、审批与 `toolsMode` |
| [知识库（RAG）](core/knowledge.md) | 上传 → 解析 → 向量 → 检索 → 注入；三个存储各司其职 |
| [子 Agent](core/subagents.md) | 委派、账本、**边界**（继承是显式的） |

### 高级用法

| 页 | 内容 |
|---|---|
| [钩子协议](advanced/hooks.md) | 挂载点与取舍；**批次 1/2a 已落地，2b/3/4 待拍板** |
| [checkpoint 与续跑](advanced/checkpoints.md) | 回合末落盘；恢复**不是"取最新"** |
| [多智能体编排](advanced/multi-agent.md) | 委派 / 协作 / 调度天花板；**什么时候不该用** |
| [安全与护栏](advanced/guardrails.md) | 威胁模型、**四层隔离**、审批、`toolsMode` 放宽什么 |
| [记忆](advanced/memory.md) | 分层、冲突处理、**与 RAG 的区别** |

### 生产使用

| 页 | 内容 |
|---|---|
| [部署](production/deploy.md) | 三种形态、上线四步、**四个必须检查的点** |
| [多实例](production/multi-instance.md) | 靠什么做到水平扩展、上线前该验的三件事 |
| [可观测性](production/observability.md) | 指标 / 链路 / **审计流水**（审计不是日志） |
| [错误码](production/error-codes.md) | 跨语言契约 + **行号复核工具** |
| [测试与评测](production/testing.md) | 四层测试、真实栈怎么跑、**测试自身的纪律** |

### 学习与参考

| 页 | 内容 |
|---|---|
| [概念速查](learn/concepts.md) | 每个术语一句话 + 去哪读 |
| [教程索引](learn/tutorials.md) | 三条推荐路径 + 按主题找入口 |
| [HTTP 面](reference/api.md) | 入口/代理/探针/内部面；怎么找具体接口 |
| [配置](reference/config.md) | 两个来源一处权威 + 容易配错的变量 |
| [命令行](reference/cli.md) | 两个 Go 命令、一键脚本、迁移与模型生成 |

### 贡献

| 页 | 内容 |
|---|---|
| [贡献与验收](contributing/overview.md) | CI 五个 job、**八个根守卫**、自查清单 |

## 同步到 GitHub Wiki

仓库内是**单一事实源**；GitHub Wiki 是**派生镜像**。同步用：

```bash
python scripts/sync_github_wiki.py            # 预演：生成到临时目录并打印，不推送
python scripts/sync_github_wiki.py --push     # 真正推送到 chiron.wiki.git
```

GitHub Wiki 的页面是**扁平**的（没有子目录），所以脚本把目录树编码进页名
（`core/architecture.md` → `Core-Architecture.md`、本页 → `Home.md`），并生成 `_Sidebar.md` 导航。
