# 会话地图与分支设计

> **溯源（2026-10-08）**：本文件**原先缺失** —— 仓库里 6 处引用它（`internal/session/manager_branch.go` ×3、
> `internal/api/gateway_router.go`、`internal/session/branch_test.go`、`python-engine/app/context/branch_condense.py`），
> 但文件不在仓库中。现按**代码与注释**重建，**只写代码能证明的内容**；与代码冲突时以代码为准。
> 门禁：`python scripts/check_doc_links.py` 会拦住"引用了不存在的文档"这类断链。

## 1. 为什么需要分支

长会话里，用户常想"从某个位置岔开一条新线"，但又**不想把整段历史重跑一遍**（重复计费 + 重复副作用）。
分支要做的是：在**用户指定的分叉点**裁出一段上下文，落到一个**带血缘标记**的新会话上。

与"整轮重跑"的区别：新会话只带"压缩后的核心上下文 + 尾部若干条原文"，因此既省 token，又保住最近的语气与细节。

参照实现：`vendor/dsh-synapse` 按 `session.header.parentSession` 画边、用 `seedLength` 记分叉点。
在本设计落地前，Chiron 只有"复制前 N 条"的 fork（没有父子关系、没有裁剪语义）。

## 2. 语义

### 2.1 血缘与状态（列）

`sessions` 表上的列（基线见 `migrations/sql/init.sql`，索引 `ix_sessions_parent_session_id`）：

| 列 | 含义 |
|---|---|
| `parent_session_id` | 源会话（血缘） |
| `branch_from_seq` | 分叉点：保留到源会话的第几条消息（**含**该条，1 基，必须 > 0） |
| `branch_mode` | `truncate` / `condense` |
| `branch_state` | `NULL`（truncate）· `pending`（压缩中）· `ready` · `failed` |
| `branch_keep_tail` | condense 下保留的**原文**条数（审计/复现用；truncate 写 NULL） |

### 2.2 两种模式

- **`truncate`**：逐字复制前 N 条原文，不带压缩状态（`branch_state` 留 `NULL`）。这是**旧行为**，
  `/fork` 即它的别名，保留是为了不破坏既有前端与会话地图连线。
- **`condense`**（默认）：只复制**尾部 K 条原文**，其余保留区交给引擎压成"核心上下文摘要"。
  摘要是**异步**写入的，因此新建时先把 `branch_state` 置为 `pending`，引擎写完后推进到 `ready`
  （失败写 `failed`，可重置回 `pending` 重试）。

`planBranch` 里的两个阈值：
- `defaultBranchKeepTail = 4` —— 默认保留最近 4 条原文（与引擎侧 `ContextManager` 的口径对齐）；
  用户传 `keep_tail` 时以用户为准，且**被夹到不超过 `from_index`**。
- `minCondenseMessages = 3` —— 压缩区至少这么多条才值得调一次模型；
  否则**整段复制**、状态直接 `ready`（前端显示"无需压缩"），不做无意义的模型调用。

### 2.3 窗口语义（与"不得记得未来"不变量）

```text
源会话  m1 ... m(N-K) | m(N-K+1) ... mN | m(N+1) ...
        └─ 压缩区 ──┘ └─ 原文保留 ──┘ └─ 被裁掉（丢弃）─┘
新会话  [ 模型压缩摘要 ] + m(N-K+1) ... mN
```

- `from_index = N`：新会话**只看到分叉点及之前**的内容。**不得把 `m(N+1)...` 带进来** ——
  否则新会话会"记得未来"，语义混乱（引擎侧同一约束见 `app/context/branch_condense.py` 的注释）。
  这也是为什么 `include_future` 字段在请求里**只预留、当前忽略**：它一旦实现，必须与这条不变量一起重新定义。
- 复制窗口：`OFFSET compressible LIMIT keep`（truncate 为 `OFFSET 0 LIMIT from_index`）。
- **时间戳重排**：复制进新会话的消息按**源会话里的绝对序号**重排 —— `NOW() - ((N - rn) ms)`，
  窗口最后一条正好是 `NOW()`。这样既保持与源会话一致的相对间距，又保证新会话**之后**产生的真实消息
  时间必然更大（否则历史列表与会话地图的排序会乱）。

## 3. 契约

```text
POST /v1/conversations/{id}/branch
  body: {from_index, keep_tail?, mode?, title?}          # mode 默认 condense
  201:  {session_id, parent_session_id, branch_from_seq, branch_mode,
         branch_state, copied, condensed}
POST /v1/conversations/{id}/fork                         # 旧语义 = truncate 别名
```

响应直接带 `branch_mode` / `branch_state`：前端拿到就要立刻把"压缩中 / 已压缩 / 无需压缩"画在列表与
地图卡片上，不该让它再发一次请求。

## 4. 实现落点

| 环节 | 位置 |
|---|---|
| 计划（纯函数，可单测） | `internal/session/manager_branch.go::planBranch` |
| 建会话 + 复制窗口（**一个事务**） | `internal/session/manager_branch.go::BranchSession`（`ForkSession` = truncate 别名） |
| 状态推进（CAS：只改 `pending` 的行） | `internal/session/branch_summary.go`（`MarkBranchState` / `ResetBranchStateForRetry`） |
| HTTP | `internal/api/session_branch.go`（`BranchConversationHandler`）、`internal/api/session_fork.go` |
| 引擎侧压缩 | `python-engine/app/context/branch_condense.py` |
| 前端 | `ChatSidePanel.vue` + `components/chat/transcriptProjection.ts`（地图连线） |

**为什么建会话与复制消息必须同一个事务**：否则地图会出现"空卡"或"有消息却没有会话"的中间态。

## 5. 验证

- `go test ./internal/session/... -run TestPlanBranch`（窗口与阈值语义）
- `go test ./internal/api/... -run 'Branch'`（参数校验、JSON 契约）
- 端到端：`POST /v1/conversations/{id}/branch` 后，新会话应带 `parent_session_id` + `branch_from_seq`，
  消息条数 = `keep`（condense）或 `from_index`（truncate），且不含分叉点之后的消息。
