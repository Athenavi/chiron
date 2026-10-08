# Agent 安全与可靠性（威胁模型与边界）

> **溯源（2026-10-08）**：本文件**原先缺失** —— 3 处引用它（`internal/api/tool_broker.go` 要 §1.4、
> `python-engine/app/mcp/client.py` 要 §1.4、`python-engine/app/agent/undo_stack.py` 要 §4.2）。
> 现按**代码与注释**重建，**只写代码能证明的内容**；与代码冲突时以代码为准。**小节编号沿用引用**。
> 门禁：`python scripts/check_doc_links.py` 拦住"引用了不存在的文档"这类断链。

## 1. 信任域与总原则

### 1.1 谁不可信

引擎进程**处理不可信内容**：模型输出、抓取到的网页、工具返回。因此**必须假设 prompt injection 会成功**
——它可以让模型输出"帮我执行 X"这样的话，而任何**跑在同一个进程里**的判定都可能被它影响。

### 1.2 判定与执行分处两个信任域（Tool Broker）

危险工具（`delete` / `external`）的授权判定由**服务端独立做一遍**：

```
POST /v1/internal/tool-authorize        （内部令牌；由引擎调用）
请求：{tenant_id, user_id, session_id, tool_call_id, tool_name, arguments, tools_mode}
响应：{allowed, level, requires_user_approval, requires_second_check, rollback, enforced, reason}
```

- 服务端**不执行**工具（实现仍在引擎），只回答"**允不允许执行 + 该走哪些关**"；
- 引擎侧判定若比服务端**松**，**以服务端为准** —— 这就是"判定与执行分处两个信任域"的落点；
- 审计只在**危险工具**上记（`tool.authorize`，含等级 / 模式 / 是否被收紧 / 可回滚性）；
  只读与普通写入不记 —— 审计的价值在"异常与不可逆"，不是逐条流水。

### 1.3 服务端可以**收紧**用户的模式选择

`delete` / `external` 是"不可逆 + 触达外部"。**即使会话选了 `yolo`（跳过全部确认）**，
服务端仍要求**人工确认 + 二次校验**，并在响应里用 `enforced=true` + `reason` 说明原因，
前端据此告诉用户"为什么选了全自动还要确认"。

> 理由（代码注释原话）：模式选择表达"我愿意多放手"，**不能**表达"我愿意承受不可撤销的后果"。

### 1.4 引擎**没有**直连危险工具的能力（MCP 收窄 + 出口凭据不进引擎）

**这一条才是硬约束**，§1.2 只解决"判定权威"、**不解决"引擎绕过"**。落点是 MCP：

- 引擎**只连接显式标记 `read_only: true` 的 MCP server**；**未标记的一律拒绝（fail-closed）**，
  要接就必须在配置里显式写明 `"read_only": true`；
- 需要写 / 删 / 触达外部能力的 server，应当由**网关侧**提供（经 Tool Broker），
  **不要把凭据放在 agent 能够到的地方**；
- 原因：MCP 凭据写在 `ServerDef.env` / `url` 里（由用户/管理员配置）。若引擎进程持有它们，
  被 prompt injection 影响的 agent 就能**直接**用这些凭据访问外部系统，
  **绕过网关的全部授权、审计与限流**。

另外 `assert_server_connectable(server, role=...)` 在连接前还会按**角色**再判一次。

## 2. 工具分级与关闸

四级：`read` · `write` · `delete` · `external`（`ToolLevelOf(name, args)` 判定；参数解析不了时，
命令类工具走**保守默认**）。三个关闸函数：

| 函数 | 回答的问题 |
|---|---|
| `RequiresConfirmation(level, mode)` | 这个等级 + 这个模式，要不要用户确认 |
| `RequiresSecondCheck(level)` | 要不要**二次校验**（不可逆 / 触达外部） |
| `ToolRollbackCapability(tool)` | 这个工具的**可回滚性**（进审计与前端提示） |

## 3. 出站网络（SSRF）

出站请求一律经 `app/tools/ssrf.py` 的 `fetch_url_safe` 逐跳校验（禁自动重定向），并做**语义归一**：
IPv4-mapped / 6to4 / Teredo 里内嵌的 IPv4 会按**同一套策略递归判定**（否则
`http://[::ffff:169.254.169.254]/` 这类写法会整体绕过名单）。命令白名单另要求
**裸 basename 或绝对路径**（`../`、UNC 会被拒）。细节与三处实证逃逸见
[差距分析](dsh-gap-analysis.md) 切口 #6。

## 4. 副作用与可撤销性

### 4.1 中断与去重

副作用工具"中断在中间"的判定、提交去重（`client_msg_id`）与续跑语义见
[run 现场 checkpoint 续跑设计](run-checkpoint-design.md) §5 —— 那里的规则是"**宁可少做不漏告知**"。

### 4.2 撤销栈：**诚实优先**，撤不回要说出来

`/undo` 曾经是**空壳**：只 `pop` 一个 `last_edit` 元数据并回显字符串，**文件没有任何变化**。
用户以为撤销了、其实没有 —— **这比"没有 `/undo`"更危险**。

现在的做法：**写入前**保存原内容快照（会话级、有界、带 TTL），`/undo` 据此真正恢复。两条原则：

1. **诚实优先**：快照存不下（文件太大 / 存储不可用）时，工具结果里**明确告知"本次不可撤销"**，
   而不是事后才发现撤不回；
2. **不越界**：恢复前校验目标**仍在工作区内** —— 快照存在 Redis，**不能假设它没被篡改**。

边界（代码常量）：每会话最多 **20** 条快照 · TTL **24h** · 单条内容上限 **256 KiB**（超过就不存，
但必须明确告知）。撤销是"最近几步"的需求，**不是版本控制**。

## 5. 审计

四条流水，全部带 tenant / user / session 维度：

| 流水 | 覆盖 |
|---|---|
| `exec_audit` | **每次执行出口**（含被拦下、超时、后台任务 `tool_job` 的每个终态） |
| `plugin_audit` | 插件/skill 进程拉起（成功与失败都记） |
| `approval_audit` | 人工审批 |
| `tool.authorize`（DB 审计） | **危险工具**的每次授权尝试（含是否被服务端收紧） |

"哪些执行路径要审计"是**机械化**的：`python-engine/tests/test_exec_audit_inventory.py`
枚举 `app/` 下所有起进程的位置，未归类即失败。

## 6. 实现落点

| 环节 | 位置 |
|---|---|
| 危险工具授权与审计 | `internal/api/tool_broker.go` + `internal/api/tool_policy.go` |
| MCP 收窄 | `python-engine/app/mcp/client.py`（`_connect_server` / `assert_server_connectable`） |
| 出站守卫 | `python-engine/app/tools/ssrf.py` |
| 撤销栈 | `python-engine/app/agent/undo_stack.py`（`/undo` 后端） |
| 审计流水 | `python-engine/app/tools/exec_audit.py` · `app/plugins/audit.py` · `app/agent/approval_audit.py` |

## 7. 已知边界（不要读成"已经安全了"）

- Tool Broker 解决的是**判定权威**；**引擎若已持有凭据，它拦不住**（所以 §1.4 才是硬约束）；
- A 层守卫（AST 守卫 / 白名单 / RLIMIT / SSRF）**提高门槛、不构成隔离** —— 实测有过三处逃逸
  （见差距分析切口 #6）；多租户不可信代码的**内核级隔离**是另一个方向（独立沙箱服务）；
- 审计覆盖的是**已归类**的执行路径；新增执行路径必须进清单，否则"看不见"；
- `/undo` 只覆盖**文件写入**；其它副作用（外部调用、消息发送）不可撤销，只能在授权阶段拦住。
