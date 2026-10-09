# Chiron Wiki

**Chiron** 是一个**多租户 SaaS 形态的 AI Agent 平台**：Go 网关 + Python 引擎 + Vue 3 前端，
后端依赖 PostgreSQL(pgvector) / Redis / Milvus / MinIO。

## 这套 wiki 怎么用

> **单一事实源声明**：本 wiki 只做**导航 + 精炼导语**，**不复制正文**。
> 每一页的「权威来源」都指向 `docs/` 或仓库里的对应文件 —— 改事实请改那里，不要改这里。
> 这样 wiki 与代码不会各自漂移（这是本仓库一直在防的事）。

- 想**跑起来** → [开始使用](getting-started/install.md)
- 想知道**它长什么样、怎么走通一条请求** → [架构](core/architecture.md)
- 想**贡献代码** → [贡献与验收](contributing/overview.md)
- 想**知道它和同类比在哪** → [概述](overview.md) 里给了可比性口径与差距分析入口

## 目录

| 页 | 内容 | 权威来源 |
|---|---|---|
| [概述](overview.md) | 定位、架构图、可比性口径 | `README.md` · `docs/dsh-gap-analysis.md` |
| [安装](getting-started/install.md) | 工具链、依赖、本地 / 容器两条路 | `README.md` · `docs/contributing.md` |
| [快速入门](getting-started/quickstart.md) | 五条命令跑通一次对话 | `README.md` |
| [设计理念](getting-started/philosophy.md) | 北极星、判据、明确不做的事 | `vendor/规划.md` §6 |
| [架构](core/architecture.md) | 三层与请求链路、目录结构、契约索引 | `docs/architecture.md` |
| [贡献与验收](contributing/overview.md) | CI 五个 job、八个根守卫、自查清单 | `docs/contributing.md` |

## 还没写的页（按需补）

核心组件（网关 / 引擎 / 前端 / 会话 / transcript / 工具与审批 / 知识库 / 子 Agent）、
高级用法（钩子协议 / checkpoint / 多智能体 / 安全护栏 / 记忆）、生产使用（部署 / 多实例 / 可观测性 / 错误码 / 测试）、
参考（API / 配置 / CLI）。**在写出来之前，这些主题直接看 `docs/` 下同名文档。**

## 同步到 GitHub Wiki

仓库内是**单一事实源**；GitHub Wiki 是**派生镜像**。同步用：

```bash
python scripts/sync_github_wiki.py            # 预演：生成到临时目录并打印，不推送
python scripts/sync_github_wiki.py --push     # 真正推送到 chiron.wiki.git
```
