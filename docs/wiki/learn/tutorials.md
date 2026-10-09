# 学习：教程索引

> 这里**不新写教程**，只把仓库里已经存在、且**本机可复现**的路径串起来。

## 三条推荐路径

### ① 跑通一次对话（最短路径）

[安装](../getting-started/install.md) → [快速入门](../getting-started/quickstart.md)
—— 五条命令，注册的第一个账号自动成为管理员。

### ② 看懂一条请求怎么走

[架构](../core/architecture.md) → [会话](../core/sessions.md) → [transcript](../core/transcript.md)
—— 从网关的路由，到事件流，再到前端怎么把它渲染成消息列表。

### ③ 把整套测试跑起来（含真实栈）

[贡献与验收](../contributing/overview.md) → [测试与评测](../production/testing.md)
—— 五个 CI job + 八个根守卫；`integration` 用例需要真 PostgreSQL + Redis，**还要把网关起起来**。

## 按主题找入口

| 我想…… | 从这页开始 |
|---|---|
| 加一个工具 | [工具与审批](../core/tools.md) —— 注意策略两侧必须同构 |
| 改消息列表的渲染 | [transcript](../core/transcript.md) —— 先读契约，`scrollTop` 有单写者门禁 |
| 做知识库问答 | [知识库（RAG）](../core/knowledge.md) |
| 让 Agent 委派子任务 | [子 Agent](../core/subagents.md) · [多智能体](../advanced/multi-agent.md) |
| 上线 / 扩容 | [部署](../production/deploy.md) · [多实例](../production/multi-instance.md) |
| 查一个错误码 | [错误码](../production/error-codes.md) |
| 搞清某个术语 | [概念速查](concepts.md) |

## 评测套件（E1 / E2 / E3）

`python-engine/evals/README.md` 是评测套件的权威说明 —— 想衡量"改完之后是变好还是变坏"，
从那里开始，而不是自己临时写脚本。

## 延伸阅读

- [概述](../overview.md) · [设计理念](../getting-started/philosophy.md)
