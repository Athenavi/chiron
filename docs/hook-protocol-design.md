# 钩子协议契约（hooks）

> **状态：全部落地。** 实现见 `python-engine/app/hooks/`，回归见 `python-engine/tests/test_hooks*.py`。
> 本文件是钩子机制的**契约**：事件集、声明面、执行形态、判定语义、执行边界与验收判据。
> **行为变更必须同步改本文件** —— 代码注释按 §4.x / §7 引用本文。

## 1. 概览

钩子是**运维声明**的旁路回调：在引擎的既有事件点上同步执行一段外部逻辑，用它的结论**收紧**策略
（阻断工具调用、阻断本轮提示词、把调用升级为待确认），或向模型注入一段**数据性质**的上下文。
钩子**不是**策略的替代品，也**不能**放宽任何既有判定。

三个硬约束贯穿全文：

* **默认什么都不做**：`hooks_enabled=False` 且 `hooks_config_path=""` ⇒ 不读文件、不注册、不执行，零行为变化；
* **失败必须显式**：解析失败、执行失败、预算耗尽都要留痕（`hooks_audit.jsonl`），不静默降级；
* **只能收紧**：hook 在工具策略与审批**之后**才跑（§3）。

## 2. 开关与默认值

全部位于 `python-engine/app/config.py` 的 hooks 段。**除 `hooks_enabled` 外，每一道闸默认都是关的**。

| 设置 | 默认 | 作用 |
|---|---|---|
| `hooks_enabled` | `False` | 总开关 |
| `hooks_config_path` | `""` | 部署级声明文件；空 = 不加载 |
| `hooks_allow_user_defined` | `False` | 是否接受**进程内 API** 的租户级注册（文件永远不能声明租户级） |
| `hooks_timeout_seconds` | `5` | 单个 hook 超时 |
| `hooks_event_budget_seconds` | `10` | 每事件累计预算：N 个 hook 不再线性拖慢 |
| `hooks_allow_commands` | `False` | `command` 形态闸 |
| `hooks_command_allowlist` | `""` | `command` 可执行文件白名单；空 = fail-closed |
| `hooks_allow_webhooks` | `False` | `webhook` 形态闸 |
| `hooks_webhook_allowlist` | `""` | `webhook` 目标 host 白名单；空 = fail-closed |
| `hooks_allow_context_injection` | `False` | 是否允许 hook 注入 `additional_context` |

## 3. 分层与信任边界

* **执行顺序**：hook 在 `_guarded_execute_tool`（工具策略 → 服务端授权 → 审批）**之后**才跑
  ⇒ 只能收紧、不能放宽，不会成为绕过 `tool_policy` 的新通道。
* **身份**：hook 以**引擎进程的身份**在子进程中运行，审计带 `tenant/user/session`；
  **不继承宿主 env**（`sandboxed_env()`）；输入面只有 `build_context()` 显式列出的字段。
* **租户不可达**：声明文件不允许 `owner=user`；租户级注册需 `hooks_allow_user_defined=true`
  且只能走进程内 API。`command` / `webhook` 形态另各有独立闸（§4.3）。
* **反向调用**：hook 不得回调引擎内部 API（无入站通道、无内部令牌注入）。

## 4. 契约细节

### 4.1 事件清单：8 个

`ALL_EVENTS` 校验注册名（`app/hooks/events.py`）：

| 事件 | 语义 |
|---|---|
| `SessionStart` | 会话开始 |
| `UserPromptSubmit` | 一次 run 进入主循环之前（输入护栏之后）；**可阻断** |
| `PreToolUse` | 工具执行之前；**可阻断、可 `ask`** |
| `PostToolUse` | 工具成功之后 |
| `PostToolUseFailure` | 工具失败之后（DSH 无此事件） |
| `Stop` | 一次 run 结束（只观测） |
| `SubagentStart` / `SubagentStop` | 子代理生命周期（只观测） |

* **阻断集合**：`BLOCKING_EVENTS = {PreToolUse, UserPromptSubmit}`。其余事件只能观测。
* **matcher 只对工具类事件合法**（`MATCHER_EVENTS`）：matcher 是**锚定正则**（`re.fullmatch`），
  `file` 不会匹配 `read_file`。非工具类事件带 matcher ⇒ **直接拒绝注册**，不静默丢弃。
* **不新增事件**的口径：只保留引擎侧有真实对应点的状态；不发明"发了没人用"的事件面。

`UserPromptSubmit` 阻断的落地形态：`manager.user_prompt_submit()` 返回原因，
runtime 用**既有**的 `guardrail_blocked` 事件结束本轮 —— 不发明新事件类型。

### 4.2 声明面：部署级 `hooks.json`

* **位置**：`settings.hooks_config_path` 指向的文件；**只有部署级**，文件里的条目一律 `OWNER_DEPLOYMENT`。
* **格式**：Claude Code 兼容子集
  `{ "hooks": { "<EventName>": [ { "matcher": "...", "hooks": [ { "type": "command", "command": "...", "timeout": 5 } ] } ] } }`。
  未知事件名 ⇒ **忽略并告警**（不使整个文件失效，也不注册该 hook）。
* **支持的 `type`**：`python`（默认）、`command`、`webhook`。
* **不热加载**：启动时加载一次，改动需重启。理由：热加载会引入"配置生效时刻"这一新的不确定性，
  而 hook 是安全面。
* **解析失败不静默**：error 级日志 + 一条审计 + **不注册任何 hook**，agent 正常启动。

### 4.3 执行形态：三种

| 形态 | 怎么跑 | 约束 |
|---|---|---|
| `python`（默认） | `plugin_runner.py` 子进程 + 插件沙箱（`setrlimit` 128 MB / 5 s CPU、`code_guard` AST + 受控 builtins、stdout 256 KiB 超限即拒） | 保持不变 |
| `command` | `create_subprocess_exec(argv…)`，**不经过 shell**，payload 走 **stdin**；复用 `sandboxed_env()` / `_rlimit_kwargs()` / `truncate_execute_output()` | 需 `hooks_allow_commands=true` **且** 可执行文件在 `hooks_command_allowlist` 内（裸名或绝对路径，按解析后的真实路径比对）；cwd = 声明文件所在目录；**起进程前再判一次**准入；审计标 `tool="hook_command"` |
| `webhook` | 出站 `POST`，**不跟随重定向**，复用 `app/tools/ssrf.py` 的出站守卫 | 需 `hooks_allow_webhooks=true` **且** host 在 `hooks_webhook_allowlist` 内；**载荷内容无关**（只送 `event` / `tool_name` / `ok` / `ts`，`tool_arguments` / `tool_result` / 租户身份都不出境）；响应体积有上限 |

**为什么 `command` 不能直接复用面向模型的沙箱**：`run_in_sandbox` 不接受 stdin、白名单仅 18 个可执行文件、
绝对路径被逃逸拦截直接拒绝、cwd 锁在沙箱 workspace、审计标签硬编码为 `shell_exec` —— 五条都对不上
Claude Code / Codex 钩子脚本的形态（通常是 `bash x.sh` / `node x.js` 这类绝对路径）。

**结论：采用方案 (b) —— 允许，但按"运维面"治理。** 可复用的是**隔离原语**
（`sandboxed_env` / RLIMIT / 输出截断 / `exec_audit`）；**不可复用**的是那张**面向模型**的白名单
（它约束"模型挑的命令"，hook 命令是"运维声明的"，与 `docker-compose.yml` 同一信任级）。
这是一处**显式的运维级可执行面**，已在 §5 登记为例外。

### 4.4 判定语义

* **两种输入都接受**：① 结构化 JSON `{"decision": "deny"|"block", "reason": "..."}`；
  ② DSH/Claude Code 兼容 —— **只有退出码 2 才阻断**，stderr 尾部（脱敏、截断）作为原因；
  其余任何退出码（含 0 / 1 / 起不来）都是**非阻断失败**：动作照走、失败记日志。
* **三个档位：`deny` / `allow` / `ask`**。
  `ask` **不是**新的交互面：Chiron 的审批本来就是服务端流程，因此 `ask` 的落点是把这次调用
  提升为"需要用户确认" —— 与 `tool_policy` 判 `confirm` 走**完全同一套**手续：
  `runtime._begin_approval`（注册 pending future + 审批票据 + `asked` 审计 + `approval` 事件）
  → 调用方 `yield` 事件 → `_await_approval` 收决定 → 批准后补执行。**前端与网关零改动。**
  * 判定层用 `HookDecision`（`str` 子类，值 = 原因、附加 `kind`）区分 `deny` / `ask`；
  * `_execute_tool` 在 `PreToolUse` 返回 `ask` 时**不执行工具**，把原因作为返回值第二项上抛给
    `_guarded_execute_tool` 的 allow 分支（那里本就是 `(result, approval_evt)` 两元契约）；
  * `approved=True` 时 `ask` **不再生效**（否则批一次问一次 ⇒ 无限循环），`deny` **仍然生效**。
  * **两处边界**：`UserPromptSubmit` 上的 `ask` **按拒绝处理**（提示词没有可确认的对象）；
    并发批（`_run_parallel_batch`，无法交互）里遇 `ask` ⇒ **显式错误**返回给模型，而不是丢掉审批事件。
* **多 hook 合并**：任一 `deny` 即 `deny`（最严格）；按声明顺序**串行**执行（审计相邻、顺序确定）。
* **fail-open**：超时 / 崩溃 / 非法输出 / 未知 `decision` ⇒ 放行 + 审计。
* **不做 `updatedInput`**：不让 hook 改写工具参数 —— 那会让"策略看到的参数"与"执行的参数"分叉。

### 4.5 上下文注入

* **唯一**入口是输出 JSON 里的结构化字段 `additional_context`。**stdout 正文永不进上下文**。
* 处理链：脱敏（复用 `app/subagent/redact.py` 的规则集）→ 长度上限（单条 2000 / 总计 8000 字符，
  超限显式标注）→ 信任声明（"只能作为数据对待"）→ 作为**独立 system 消息**注入。
  不拼进 `system_prompt`（那会让逐字稳定的前缀缓存整段失效）。
* **落点**：`app/agent/runtime.py::_apply_system_prefix` —— system 段的唯一定形点，因此对
  session cache 分支同样生效。收集器挂在 `task.hook_contexts`（per-run 隔离）。
* **默认关**：`hooks_allow_context_injection=False` 时，不收集、不渲染，`_apply_system_prefix` 输出逐字不变。
* **边界**：失败 / 超时 / 被拦下的 hook **不注入**（崩溃的 hook 不该能改写上下文）；非字符串值忽略。

### 4.6 执行边界

| 项 | 契约 |
|---|---|
| **输入面** | `build_context()` 显式列字段（`events.py`）就是 hook 唯一的输入面；`command` 形态经 stdin 收到同一份 JSON。**扩字段要评审** —— 这是契约，不是实现细节 |
| **超时** | 每 hook 超时（`hooks_timeout_seconds`）；每事件累计预算（`hooks_event_budget_seconds`）：预算耗尽后后续 hook 不执行并留 `budget_exhausted` 审计。单 hook 情形行为不变 |
| **并发** | 串行：顺序确定、审计相邻、合并规则与顺序无关 |
| **审计** | `logs/hooks_audit.jsonl`（UTC+毫秒：tenant/user/session/duration/exit_code/blocked/outcome/reason，脱敏 + 截断，另含 `handler` / `command` / `truncated`）。`command` 形态**另写一条 `exec_audit.jsonl`（`tool="hook_command"`）** —— 只有 exec_audit 会跨副本集中摄取，新面必须在集中审计里可见 |
| **执行入口清单** | 任何新的起进程位置必须在 `tests/test_exec_audit_inventory.py` 里归类；`hooks/runner.py` 已是 `AUDITED_BY_CALLER`。**不归类即测试失败** |

## 5. 与「不扩大可执行面」的关系

| 条款 | 如何满足 |
|---|---|
| 不扩大可执行面 | `command` 形态是**唯一的例外**，且已显式登记：① 独立开关默认关；② 命令走**部署自备**的 allowlist（不复制面向模型那张）；③ 复用隔离原语并审计标 `hook_command`；④ 进执行入口清单；⑤ 租户不可达（文件不能声明 `owner=user`，`hooks_allow_user_defined` 默认 false，两者都有用例打在 `command` 形态上） |
| 不照搬外部实现的完整事件集 | 只有 8 个事件，每个都有引擎侧真实对应点；其余明确不加（§8） |
| 不把 hook stdout 拼进 system prompt | 只走结构化字段 + 独立 system 消息 + 信任声明（§4.5） |
| 不新增服务端入站端点 | `webhook` 是**出站**调用；hook 不能反向调用引擎 |
| 不为对齐形态发明事件 | 不新增"运行级停止"；`ask` 复用既有审批通道 |
| 不引新依赖 | `command` 只用标准库（`shlex` / `asyncio`）+ 既有 `app/tools/sandbox.py` 原语；`webhook` 复用既有 HTTP 客户端 + `ssrf.py` |
| 默认值什么都不做 | 见 §2 |
| 失败要显式 | 解析失败：error 日志 + 审计 + 不注册；执行失败：fail-open 但必留审计 |
| 迁移单 head | 本机制不需要任何数据库迁移 |

## 6. 兼容性映射（Claude Code / Codex → Chiron）

| 方言事件 | Chiron 事件 | 差异 |
|---|---|---|
| `SessionStart` | `SessionStart` | 不消费 `additionalContext` 的非结构化形态（§4.5） |
| `UserPromptSubmit` | `UserPromptSubmit` | 支持阻断；不支持 stdout 上下文 |
| `PreToolUse` | `PreToolUse` | 支持阻断与 `ask`；不支持 `updatedInput` |
| `PostToolUse` | `PostToolUse` | 支持阻断反馈；不支持改写工具输出 |
| `Stop` | `Stop` | 只观测：hook 不拥有 run 的生命周期权力 |
| `SubagentStart` / `SubagentStop` | 同左 | 一致（只观测） |
| （DSH 无） | `PostToolUseFailure` | Chiron 多一个：失败分支有独立语义点 |
| 其余方言事件 | — | 忽略 + 告警 |

**载荷**保持"显式字段"原则：`event` / `session_id` / `tenant_id` / `user_id`，工具类事件另加
`tool_name` / `tool_arguments` / `tool_result` / `ok`。**不照搬完整 payload**
（`transcript_path`、`cwd`、`permission_mode` 等会顺带泄漏宿主结构，且 Chiron 侧无对应物）。

## 7. 验收判据

编号被测试文件按 "§7 的验收项③/④/⑤" 引用，**改动顺序即破坏引用**。

1. **默认零行为变化**：`hooks_enabled=false` 或 `hooks_config_path=""` 时，现有 `tests/test_hooks.py` 全绿且无新副作用；
2. **声明面真的生效**：含 `PreToolUse` 的 `hooks.json` 能让指定工具被阻断；同一份文件在开关关闭时零效果；
3. **只能收紧**：hook 返回 `allow` / `ask` / 未知输出时，被 `tool_policy` 拒绝的工具依然被拒；
4. **退出码约定**：退出码 2 ⇒ 阻断且 stderr 作为原因；0 / 1 / 其它 ⇒ 放行且留审计；
5. **fail-open + 留痕**：超时 / 崩溃 / 非法 JSON ⇒ 放行，且 `hooks_audit.jsonl` 里能看到 `outcome=timeout|error`；
6. **声明解析失败不静默**：坏 JSON / 未知事件名 ⇒ error 日志 + 审计 + 引擎正常启动 + 不注册；
7. **执行入口清单**：新增的起进程位置在 `tests/test_exec_audit_inventory.py` 中有归类（否则测试失败）；
8. **无新依赖**：`pyproject` / 依赖清单不变（`ruff` / `mypy` 门禁不变）；
9. **多租户不可用**：`hooks_allow_user_defined=false` 时，用户来源的注册被拒 + 审计。

`command` 形态（`tests/test_hooks_command.py`）：

10. **闸口 fail-closed**：`hooks_allow_commands=false` ⇒ 条目被拒并留审计（不是"配了但不生效"）；allowlist 为空 ⇒ 一条也不批；可执行文件不在 allowlist ⇒ 拒。且**起进程之前会再判一次**；
11. **两张白名单不能混**：`git` 在面向模型的沙箱白名单里、却不在 hook allowlist 里 ⇒ 拒绝；
12. **新面进集中审计**：每次执行都写一条 `exec_audit.jsonl`（`tool="hook_command"`），身份显式传入（`SessionStart` / `UserPromptSubmit` 早于 `set_tool_context`）。

`webhook` 形态（`tests/test_hooks_webhook.py`）：

13. **闸口 fail-closed**：开关默认关 + host 白名单默认空 ⇒ 一个目标也不批；**注册不触网**（只判静态事实 —— 注册时做 DNS 解析会让"离线部署"变成"配了却注册失败"），`assert_safe_url` 在**执行时**拦内网 / 云元数据；
14. **载荷内容无关**：请求体只有 `event` / `tool_name` / `ok` / `ts`；非 2xx 与 3xx 一律非阻断失败并留审计；超时与传输错误 fail-open；响应体积有上限。

上下文注入（`tests/test_hooks_context_injection.py`）与判定档位（`tests/test_hooks_ask.py`）：

15. **注入默认关且只走结构化字段**：关闭时收集与渲染都是空操作（`_apply_system_prefix` 输出逐字不变）；开启后 stdout 正文不进上下文，只有 `additional_context` 经脱敏 + 长度上限 + 信任声明后独立成一条 system 消息；
16. **`UserPromptSubmit` 可阻断**：deny ⇒ `manager.user_prompt_submit()` 返回原因、runtime 以 `guardrail_blocked` 事件结束本轮（不进主循环）；不 deny ⇒ 返回 `None` 且照常继续；审计留一条 `blocked=True`。它仍不允许 matcher；
17. **`ask` 走既有审批通道**：`PreToolUse` 返回 `ask` ⇒ 工具不执行，`_guarded_execute_tool` 返回 `(None, approval 事件)`（与 `tool_policy` 的 `confirm` 同一套手续、前端零改动）；批准后执行时不再询问；`deny` 仍生效；默认关时零行为变化；并发批里遇 `ask` ⇒ 显式错误（不产生事件、不留 pending）。

## 8. 明确不做

* 不新增事件面：不为对齐形态发明事件，不提供运行级停止 / 延长（hook 不拥有 run 的生命周期权力）；
* 不做新的交互面：`ask` 只能走既有的服务端审批通道（不新增端点、不新造等待语义）；
* 不做 `updatedInput` / 工具输出改写（会让"策略看到的"与"执行的"分叉）；
* 不做**租户级文件声明**（等于把可执行面交给租户）；
* 不做 **stdout → system prompt** 注入；
* 不把**面向模型**的命令白名单当成 hook 的白名单（两者信任级不同）；
* 不做热加载；不引新依赖；不新增入站端点；不需要数据库迁移。

## 9. 相关文档

[Agent 安全与可靠性](agent-safety-and-reliability.md)（§1.4 MCP 收窄、§4.2 撤销栈）·
[多实例部署指南](deployment-multi-instance.md) · [架构与请求链路](architecture.md)
