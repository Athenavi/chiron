# 钩子协议设计（追平 DSH 的钩子协议）

> **状态：设计 + 批次 1 / 2a / 2b / 3 / 4 全部落地（2026-10-09）。**
> **批次 1（声明面）** 已实现并验证：`app/hooks/config.py` + `settings.hooks_config_path` +
> 逐 hook `matcher`（**锚定**正则）+ 启动接线；**默认空 = 不读文件 / 不注册 / 不执行**。
> **批次 2a（事件 + 预算）**：`UserPromptSubmit`（**只观测**）+ **每事件累计预算**
> （`hooks_event_budget_seconds`，默认 10 s ⇒ N 个 hook 不再线性拖慢）。
> **批次 2b（`command` 形态；拍板 ① = 选项 (b)）**：运维声明的本地命令 —— 独立开关
> `hooks_allow_commands`（默认关）+ 部署自备 `hooks_command_allowlist`（默认空 = fail-closed、
> 允许绝对路径）+ **退出码 2 约定** + `exec_audit(tool="hook_command")`；例外已在 §6 登记。
> **批次 3（`webhook` 形态；拍板 ⑤ = 载荷只送内容无关字段）**：出站 POST —— 独立开关
> `hooks_allow_webhooks`（默认关）+ `hooks_webhook_allowlist`（host，默认空）+ 复用
> `app/tools/ssrf.py` 的出站守卫 + **不跟随重定向** + 响应体积上限。**这一条 DSH 没有**（超出对标）。
> **批次 4（上下文注入；拍板 ③ = 做）**：hook 输出里的 **`additional_context`**（**结构化字段，
> 不是 stdout**）经脱敏 + 长度上限 + 信任声明后作为**独立 system 消息**注入；独立开关
> `hooks_allow_context_injection`（默认关 ⇒ 零行为变化）。
> **批次 5（2026-10-09 拍板"两项都做"）—— 两项均已落地**：**④ `UserPromptSubmit` 可阻断**
> （进入 `BLOCKING_EVENTS`；`manager.user_prompt_submit()` 返回原因，runtime 以**既有**的
> `guardrail_blocked` 事件结束本轮）· **② `ask` 判定**（判定层用 `HookDecision.kind` 区分
> `deny`/`ask`；`ask` ⇒ `_guarded_execute_tool` 走**既有**的服务端审批通道 —— 与 `tool_policy`
> 的 `confirm` **共用** `_begin_approval`，因此前端与网关**零改动**；批准后 `ask` 不再生效、
> `deny` 仍生效）。
>
> **依据**：`vendor/规划.md` §3.2 曾有「追平 DSH：**钩子协议**」一条（用户 2026-10-09 指示：未来规划追平）。
> **该条目现已完成、并按规划 §0 的约定从待做清单移除** ⇒ 本文是它的**唯一载体**（实现见 git
> 历史与 `python-engine/app/hooks/`、`python-engine/tests/test_hooks*.py`）。本文要求
> **落地前先出设计**，并指定三件必答：**事件清单 · 执行边界 · 与 §6「不扩大可执行面」的关系**。
> 本文即对该三项的回答，外加声明面、执行形态、判定语义与实现批次。
>
> **约束**（`vendor/规划.md`）：§1.1 不引新依赖 · §1.3 新增机制默认值必须"什么都不做"、失败要显式 ·
> §6 不照搬 deepagents 的 12 个事件、不把 hooks 的 stdout 拼进 system prompt、不为对齐形态发明事件、
> 不扩大可执行面（批次 2b 是**已在 §6 显式登记**的例外，见 §5）。
>
> **取证基线**：Chiron @ `master`（2026-10-09）；DSH = 本机安装的
> `@deepseek-ai/dsh-hook-protocol` / `dsh-hooks-claude-code` / `dsh-hooks-codex` / `dsh-webhook`
> （从 `app.asar` 枚举并导出原文，见 §10.1）。
> **取证强度**：【枚举】读源码 · 【文档】读契约原文 · **【实测】** 本次跑过。**未跑过** DSH 侧的 hook 运行时。

## 0. 结论摘要（需要拍板的四件事）

| # | 结论 |
|---|---|
| 1 | **"钩子协议落后"这个判断要修口径**：Chiron 的钩子机制**已经落地并接线**（沙箱执行 / 审计 / 默认关 / 有测试），**事件集现为 8 个** —— 与 DSH **实际支持的 7 个**相比，并**多一个** DSH 不支持的 `PostToolUseFailure`（`UserPromptSubmit` 已由批次 2a 补齐） |
| 2 | **声明面已补**（批次 1）：`settings.hooks_config_path`（**默认空 = 不读文件、不注册、不执行 = 零行为变化**）+ `load_hooks_config()` 已在 `app/main.py` 接线。**`command` 形态亦已落地**（批次 2b）：它才是"能搬动 DSH 既有钩子脚本"的那一环 |
| 3 | **五件事的拍板结果（2026-10-09 两轮拍板）**：① = **(b)**（运维声明的命令钩子按"运维面"治理 + 在 §6 显式登记例外）**已落地**；② `ask` = **要做** ⇒ **已落地**（复用既有审批通道，与 `confirm` 共用 `_begin_approval`）；③ 上下文注入 = **做**（只走结构化字段，已落地）；④ `UserPromptSubmit` = **可阻断**（已落地）；⑤ `webhook` = **做**（载荷只送内容无关字段，已落地） |
| 4 | **DSH 侧对同样四件事的答案**（读 `dsh-hook-protocol` README，实现源码级）：① **只跑 `command` 钩子**（`http`/`mcp_tool`/`prompt`/`agent` **跳过并告警**）⇒ 命令钩子是对标物**唯一**形态，与本设计一致；② **有 `ask`**（仅 Claude Code 桥；Codex 桥不提供）⇒ **有意偏离**；③ **有上下文注入** ⇒ **有意偏离**；④ **`UserPromptSubmit` 可阻断**（退出码 2）⇒ **有意偏离**。⑤ DSH **根本没有** http/webhook 形态 ⇒ webhook 属**超出对标** |

> **另两条关键语义（照抄进实现）**：**只有退出码 2 才阻断**，其它退出码一律**非阻断失败**
> （动作照走、失败记日志），**连"起不来的钩子"也同等对待**；`{"continue": false}` 只记录、无运行级效果。
> **另核**：`UserPromptSubmit` / `Stop` 在 DSH 里**没有 matcher 主体**（*matchers are discarded*）——
> Chiron 的 `MATCHER_EVENTS` 同样只含工具类事件（且更严：带 matcher 的非工具事件**直接拒绝**）。

---

## 1. 现状：已落地什么（用证据，不靠"模块存在"）

| 维度 | 现状 | 证据 |
|---|---|---|
| 事件集 | **8 个**：`SessionStart` · **`UserPromptSubmit`**（批次 2a，**只观测**）· `PreToolUse`（**唯一可阻断**）· `PostToolUse` · `PostToolUseFailure` · `Stop` · `SubagentStart` · `SubagentStop` —— **2026-10-09 复测**（原写 7 个） | `app/hooks/events.py`（**3,962 B / 100 行**），`ALL_EVENTS` 校验注册名 |
| 执行隔离 | **三种形态**：`python`（默认）独立子进程跑 `plugin_runner.py`（插件沙箱：`setrlimit` 128 MB / 5 s CPU、`code_guard` AST + 受控 builtins、stdout 256 KiB 超限即拒）；**`command`（批次 2b）** 直接 `create_subprocess_exec(argv…)`（**不经过 shell**）+ 同一套 `sandboxed_env()` / `_rlimit_kwargs()` / `truncate_execute_output()`，payload 走 **stdin**；**`webhook`（批次 3）** 出站 `POST`（不跟随重定向）+ `ssrf.py` 守卫 + 内容无关载荷 | `app/hooks/runner.py`（`run_hook` / `run_command_hook` / `run_webhook_hook`） |
| 失败姿态 | **永不抛给调用方**（`HookOutcome`）；超时/崩溃/非法输出**一律放行**（fail-open），但**必留审计**；`command` 形态另按 DSH 约定：**只有退出码 2 阻断**，其余非 0 = 非阻断失败 | `app/hooks/runner.py`、`app/hooks/manager.py` |
| 编排 | 门面单例 `hooks`；**按 `Hook.handler` 分派形态**；按事件串行执行；`PreToolUse` 阻断以"工具错误"回灌给模型；每事件有累计预算（`min(单 hook 超时, 剩余预算)`，默认 10 s ⇒ N 个 hook 不再线性拖慢） | `app/hooks/manager.py` |
| 接线 | 启动告警 + **声明面加载**（`app/main.py`）；`app/agent/runtime.py`：`session_start` · `user_prompt_submit`（输入护栏之后、主循环之前）· `stop` · `before_tool_use` · `subagent_start` / `subagent_stop` · `after_tool_use` | `app/main.py`、`app/agent/runtime.py` |
| **分层顺序** | hook 在 `_guarded_execute_tool`（工具策略 / 服务端授权 / 审批）**之后**才跑 ⇒ **只能收紧、不能放宽**，不会成为绕过 `tool_policy` 的新通道 | `runtime.py::_execute_with_hooks` 的注释 + 调用点 |
| 审计 | `logs/hooks_audit.jsonl`（UTC+毫秒：tenant/user/session/duration/exit_code/**blocked**/outcome/reason/脱敏+截断），批次 2b 增 **`handler`** / **`command`** / **`truncated`** 三个字段；**`command` 形态另写一条 `exec_audit.jsonl`（`tool="hook_command"`）** —— 它是唯一会跨副本集中摄取（N4）的那条流水 | `app/hooks/audit.py`、`app/hooks/manager.py::_record_command_exec_audit` |
| 默认值 | `hooks_enabled=False` · `hooks_allow_user_defined=False` · `hooks_timeout_seconds=5` · `hooks_event_budget_seconds=10` · `hooks_config_path=""` · `hooks_allow_commands=False` · `hooks_command_allowlist=""` · **`hooks_allow_webhooks=False`** · **`hooks_webhook_allowlist=""`** · **`hooks_allow_context_injection=False`**（后三者为批次 3/4） | `app/config.py` 的 hooks 段 |
| 执行入口清单 | `hooks/runner.py` **已被机械归类**为 `AUDITED_BY_CALLER`（"manager 在每次执行后调用 `audit.record_hook`；`command` 形态另写一条 exec_audit"）—— 新增起进程位置不归类即测试失败 | `tests/test_exec_audit_inventory.py` |
| 测试 | `tests/test_hooks.py`（注册校验、阻断、超时放行、沙箱逃逸、用户 hook 拒绝、审计、启动告警、**两端 UTF-8 编码契约**、**`UserPromptSubmit` 可阻断**）· `tests/test_hooks_config.py`（声明面加载 / **锚定 matcher** / 校验 / 预算 / 未知事件忽略）· `tests/test_hooks_command.py`（批次 2b）· `tests/test_hooks_webhook.py`（批次 3）· `tests/test_hooks_context_injection.py`（批次 4）· **`tests/test_hooks_ask.py`（批次 5：`ask` 走审批 / 防重复询问 / 并发边界）** | 见各文件 |

### 1.1 本批新增的可执行面（原"没有声明面"缺口已补）

* 批次 1 + 2b 之后，一个部署**可以**只靠配置文件启用 hook：`hooks_config_path` 指向 `hooks.json`，
  `hooks_enabled=true` 打开机制；`python` 形态到此即可用。
* **`command` 形态另需两道闸**（否则条目在注册处就被拒并留审计）：
  `hooks_allow_commands=true` **且** 可执行文件落在 `hooks_command_allowlist` 内。
* ⇒ 这就是批次 2b 的实质：把 `run_in_sandbox` **不可直接复用**的地方（无 stdin · 白名单仅 18 个 ·
  绝对路径被逃逸拦截挡下 · cwd 锁沙箱 · 审计标签硬编码，见 §4.3）**逐个正面解决**，并把它登记成
  §6 的一处**显式例外**（运维级可执行面、租户不可达）。

---

## 2. 对标：DSH 的协议到底是什么（精确）

DSH 侧是**两个桥接 + 一份共享协议**（`@deepseek-ai/dsh-hook-protocol`），用户不直接选协议包：

* **只跑 `command` 形态**：`{type:'command', command, timeout?}`；`http` / `mcp_tool` / `prompt` / `agent`
  四类 handler **解析后跳过并告警**（【文档】protocol README §已知限制）；
* **判定语义**：**退出码 2 = 阻塞**，stderr 作为原因展示；**除 2 以外任何退出码都是非阻塞失败**（继续 + 记录）；
  完全起不来的 hook 同样处理；
* **合并规则**：最严格合并 `deny > ask > allow`；首个 `{"continue": false}` 是**粘性**的；上下文按 hook 顺序累积；
* **上下文注入**：`additionalContext`（含 `SessionStart`）会**让模型在下一次请求看到**，带来源归因；
* **matcher**：按名字或 pattern 选事件；缺失/空/`'*'` = 该类全部事件；**两个方言唯一的差异轴是 `mode`**
  （claude-code：字面量备选或正则；codex：始终未锚定正则）；
* **绝不向循环抛异常**：格式错误 JSON、非法正则、执行器拒绝都降级为受控结果或不匹配；
* **可观测**：`hook/invoked` + `hook/result` 是**仅日志的会话事件**（与 `compaction/*` 同类，不是 surface 事件），
  按 `handlerId` 配对，且必须落在**尚未结束的轮次**内；
* **进程控制交给执行器**（`dsh-shell`：已清理但可覆盖的 env、进程组取消、超时）。

**DSH 的 Claude Code 桥接支持 7 个事件**（Claude Code 官方 30 个中的 7 个）：
`SessionStart` · `UserPromptSubmit` · `PreToolUse` · `PostToolUse` · `Stop` · `SubagentStart` · `SubagentStop`。
未支持的 23 个（配置在解析前被忽略，既不使配置失效也不注册 hook）：
`Setup` `InstructionsLoaded` `UserPromptExpansion` `MessageDisplay` `PermissionRequest` `PostToolUseFailure`
`PostToolBatch` `PermissionDenied` `Notification` `TaskCreated` `TaskCompleted` `StopFailure` `TeammateIdle`
`ConfigChange` `CwdChanged` `FileChanged` `WorktreeCreate` `WorktreeRemove` `PreCompact` `PostCompact`
`SessionEnd` `Elicitation` `ElicitationResult`。

DSH 自陈的三条已知限制：`updatedInput` **解析但不应用**；折叠出的 `continue:false` **没有运行级效果**；
**只有 command 形态会跑**。

---

## 3. 差距表（这张表就是设计范围）

| 维度 | Chiron 现状 | DSH | 差距性质 |
|---|---|---|---|
| 事件集 | **8 个**（批次 2a 已补 `UserPromptSubmit`） | 7 个 | **Chiron 多 1 个**（`PostToolUseFailure`；`UserPromptSubmit` 已对齐）—— **2026-10-09 更正**：此行原写「7 个 / 只差 1 个事件」，与 §8 的批次表矛盾 |
| **声明面** | **`hooks.json` 已落地**（批次 1：`settings.hooks_config_path` + **锚定 matcher** + 校验 + 预算 + 启动告警 + 未知事件忽略；**默认空 = 不读文件/不注册/不执行 = 零行为变化**） | `hooks.json`（Claude Code / Codex 方言） | **已追平**（批次 2b 补上 `command` 形态后，**handler 形态也已对齐**：`python` / `command`；`webhook` 属超出对标） |
| 执行形态 | **两种**：Python `code`（沙箱子进程）+ **`command`（批次 2b：运维声明的本地命令）** | `command`（本地命令，走 `dsh-shell` 执行器） | **已对齐**（`command` 是 DSH 唯一形态；Chiron 的 `command` 不经过 shell，要 shell 特性须显式 `bash -c`） |
| 判定语义 | 结构化 JSON `{decision, reason}` **与退出码 2 约定都支持**（后者批次 2b 落地）；失败 fail-open | **退出码 2 = 阻塞** + stderr 原因；其余非阻塞 | **已对齐** |
| 判定档位 | `deny` / `allow` / **`ask`** | `deny > ask > allow` | **已对齐**：`ask` ⇒ **复用既有的服务端审批通道**（与 `tool_policy` 的 `confirm` 共用 `_begin_approval`），不新增交互面（§4.4） |
| matcher | **已有**（**锚定**正则 `re.fullmatch`；**只对工具类事件合法** —— 其余事件带 matcher **直接拒绝**而不是静默丢弃） | 按名字 / pattern 选择 | **已追平且更严**（DSH 对 `UserPromptSubmit`/`Stop` 的 matcher 是 *discarded*）—— **2026-10-09 更正**：此行原写「无（注册到事件即全跑）」，是批次 1 之前的状态 |
| 上下文注入 | **无**（§6 禁止 stdout 拼接） | `additionalContext` 模型可见 | **刻意不同** |
| 运行级停止 | 无（也不该有） | `continue:false`（**且无实际效果**） | 不需要 |
| 审计 | 独立 JSONL（`hooks_audit.jsonl`） | 会话内 `hook/*` 仅日志事件 | 形态不同，各有优劣 |
| 失败姿态 | fail-open + 审计 | 非 2 退出码非阻塞 + 记录 | **已一致** |
| 不抛异常 | `HookOutcome` 永不抛 | 每步降级 | **已一致** |

**结论**：**声明面 + 本地命令形态 + 退出码约定**三者已全部落地（批次 1 / 2b）；事件集与 DSH 对齐且多 1 个。
**剩余**只有 `webhook`（批次 3，**超出对标**、待拍板 ⑤）与上下文注入（批次 4，**有意偏离**）。

---

## 4. 设计

### 4.1 事件清单：**7 → 8**，只加 `UserPromptSubmit`；**2026-10-09 起它可阻断**

**保留现有 7 个**（每个都有引擎侧真实对应点，见 §1）。**新增 1 个**：

| 新事件 | 引擎侧对应点 | 为什么它不是"发明出来的面" |
|---|---|---|
| `UserPromptSubmit` | `app/agent/runtime.py`（输入护栏校验之后、`hooks.session_start()` 紧邻处） | 用户提示词在那一刻**已经在手**（`task`），且这是"一次 run 进入主循环之前"的唯一入口；DSH 支持它，且它有真实语义（可阻断 / 可附加上下文） |

> **判定档位：2026-10-09 拍板** —— `UserPromptSubmit` **进入 `BLOCKING_EVENTS`**（此前只观测）。
> 与 `PreToolUse` 不冲突：它拦的是**用户自己的输入**（拒绝本轮），**不放宽**任何策略；
> 输入护栏仍然**先跑**（两者叠加）。落地形态：`manager.user_prompt_submit()` 返回阻断原因，
> runtime 用**既有**的 `guardrail_blocked` 事件把原因回给前端并结束本轮 —— **不发明新事件类型**
> （`vendor/规划.md` §6：没有真实状态就不发事件，这里的状态是真实的）。
> **它仍不允许 `matcher`**（`MATCHER_EVENTS` 只含工具类事件，判定主体是工具名）。

**明确不加**（依 §6「不为对齐形态发明事件」+ `events.py` 既有纪律"只保留引擎侧有真实对应点的"）：

* Claude Code 那 23 个未支持事件：`PreCompact` / `SessionEnd` / `Notification` / `PermissionRequest` 等
  —— **DSH 自己也不支持**，且 Chiron 侧要么无对应进程、要么没有可阻断的语义点；
* `PreCompact` 虽然 Chiron 有压缩，但**压缩不是回合边界**：`_force_compact` 是溢出回调
  （`runtime.py:896` 定义、`:1506` 作为 `on_overflow` 挂上），前端只把它当**状态栏更新**
  （`ChatView.vue:2195-2206` 解析 `compaction` → `lastCompaction`），没有"边界"语义
  ⇒ 加它只会变成"发了没人用"的死面；
* **不加运行级停止**：hook **不应有**停止/延长一次 run 的权力（Chiron 的取消语义由用户与网关拥有）。

### 4.2 声明面：部署级 `hooks.json`（默认不存在 = 零行为变化）

* **位置与开关**：`settings.hooks_config_path`（默认 `""` ⇒ 不加载任何文件 ⇒ **零行为变化**）。
  路径由部署显式给出；**不引入 `DEPLOYMENT_MODE`**（§6 明确不做，且部署形态判定不可靠）。
* **层级**：**只有部署级**。文件里的条目一律是 `OWNER_DEPLOYMENT`；**文件不允许声明 `owner=user`**
  （多租户下"租户可写的文件"等于把可执行面交给租户，与 §6 直接冲突）。租户级注册仍只保留
  进程内 API + `hooks_allow_user_defined` 开关（现状）。
* **格式**：Claude Code 兼容子集 —— `{ "hooks": { "<EventName>": [ { "matcher": "...", "hooks": [ { "type": "command", "command": "...", "timeout": 5 } ] } ] } }`。
  未知事件名 ⇒ **忽略并告警**（与 DSH 一致：不支持的事件既不让配置失效，也不注册 hook）。
  **已支持 `type: "python"` 与 `type: "command"`**（批次 2b）；`webhook` 目前**跳过并告警**（待 §4.3 的 ⑤ 拍板）。
* **matcher**：**锚定正则**，`mode` 作为唯一的方言差异轴（照搬 DSH 的收拢手法）。
  ⚠ **必须锚定**：Reasonix 2.x 的教训写在它自己的文档里 —— *"Matchers are **anchored** regexes:
  `file` does not match `read_file`"*；未锚定会让"只想匹配 `file`"的钩子意外匹配一堆工具。
* **热加载**：**不做**（声明文件在启动时加载一次；改动需重启）。理由：热加载会引入"配置生效时刻"
  这一新的不确定性，而 hook 是安全面；§1.3 要求失败显式、默认什么都不做。
* **解析失败**：**不静默** —— 记录 error 级日志 + 一条审计，且**不注册任何 hook**（agent 正常启动）。
  与 DSH 同款姿态（"记录警告且不运行任何 hook；agent 仍会启动"）。

### 4.3 执行形态：三种，分两批 —— ⚠ `command` 的"直接复用"**已被实测证伪**

| 形态 | 批次 | 怎么跑 | 关键约束 |
|---|---|---|---|
| `python`（现有） | 已落地 | `plugin_runner.py` 子进程 + 既有沙箱 | 保持不变 |
| `command`（新） | **✅ 批次 2b 已落地** | `create_subprocess_exec(argv…)`（**不走 shell**）+ 复用隔离原语 | 独立开关 `hooks_allow_commands`（**默认关**）+ 部署自备 `hooks_command_allowlist`（**默认空 = fail-closed**，**允许绝对路径**）+ 审计标 `tool="hook_command"`；起进程前**再判一次**准入 |
| `webhook`（新） | **✅ 批次 3 已落地** | 出站 `POST`（**不跟随重定向**：POST 本不该被重定向，跟随会多一条绕过出口检查的路径）+ 复用 **`app/tools/ssrf.py`** 的出站守卫 | 独立开关 `hooks_allow_webhooks`（**默认关**）+ `hooks_webhook_allowlist`（**host 白名单，默认空 = fail-closed**）+ **载荷内容无关**（拍板 ⑤：只送 `event` / `tool_name` / `ok` / `ts` —— **`tool_arguments` / `tool_result` 与租户身份都不出境**）+ 响应体积上限 + **不新增任何入站端点**（§6） |

**实测结论（2026-10-09 逐行核对 `app/tools/sandbox.py`）**：`run_in_sandbox(command, timeout)`
**不是**可直接承载 hook 命令的执行器 —— 五条对不上：

| hook 需要 | `run_in_sandbox` 现状 | 证据 |
|---|---|---|
| payload 走 **stdin** 的 JSON（DSH / Claude Code 约定） | **无 stdin 入参**：`communicate()` 不带 input，stdin 是空管道 | `sandbox.py:376` 签名；`:476` |
| 能跑**部署自己的**可执行文件 | **白名单仅 18 个**：`python`/`python3`/`git` + `echo/ls/dir/cat/type/head/tail/wc/find/grep/sort/uniq/cut/tr/tee` | `sandbox.py:123-145` |
| 用**绝对路径**指向钩子脚本 | **绝对路径被逃逸拦截直接拒绝**（盘符、UNC、Unix `/xxx`、`~`、`$HOME`） | `sandbox.py:109-120` |
| 工作目录 = 部署目录 | cwd 锁定为**沙箱 workspace** | `sandbox.py:469` |
| 审计能区分"这是 hook" | `record_execution(tool="shell_exec", …)` **硬编码** | `sandbox.py:412-419` |

⇒ **"复用同一套沙箱基线"必须拆成两件事**：

* **可复用**的隔离原语：`sandboxed_env()`（清 env + 重定向 HOME/TEMP）· `_rlimit_kwargs()`（内存/CPU）·
  `truncate_execute_output()`（输出上限 + 显式截断标注）· `exec_audit`（**每个出口都记**）；
* **不可复用**的是那张**面向模型**的白名单 —— 它约束"**模型**挑的命令"（不可信输入），
  而 hook 命令是"**运维**声明的"（与 `docker-compose.yml`、沙箱白名单本身同一信任级）。
  混为一谈的后果：`command` 形态只剩"跑 python/git/coreutils"，而 **python 形态已经存在 ⇒ 增量极小**；
  同时"让已有 Claude Code/Codex 脚本直接复用"的目标**落空**（那些脚本通常是 `bash x.sh` / `node x.js`，
  既不在白名单里、又多半是绝对路径）。

**于是 §6 的冲突浮出来**（这就是待拍板 ① 的实质）：让运维声明的命令跑起来**就是新增一个可执行面**，
而 §6 写着「**不扩大可执行面**」。三条出路，需 §6 的所有者拍板：

| 选项 | 做法 | 代价 / 收益 |
|---|---|---|
| **(a) 不做 `command` 形态**（§6 从严解释） | 只保留 `python` 形态；"追平"落在**声明面 + 事件 + 判定语义**上 | 零新增可执行面；**放弃** Claude Code/Codex 脚本的直接复用 |
| **(b) 允许，但按"运维面"治理** —— **✅ 2026-10-09 已拍板并落地** | 独立开关（默认关）+ **只允许部署级声明** + 复用隔离原语 + 审计标 `tool="hook_command"` + 命令走**部署自备的 allowlist**（允许绝对路径）+ 进执行入口清单 | 新增一个**运维级**可执行面 ⇒ **已在 `vendor/规划.md` §6 显式登记为例外**并写明"租户不可达" |
| **(c) 只允许白名单内可执行文件** | 严格复用 `_ALLOWED_EXECUTABLES` | 可执行面零变化；但兼容性目标**基本落空**（只多出 `git`/coreutils 钩子） |

**拍板结果 (b)，落地落在五处**：`settings.hooks_allow_commands`（默认关）· `settings.hooks_command_allowlist`
（默认空 = fail-closed，**不复制**面向模型那张白名单）· `runner.split_command` / `command_policy_error`
（不走 shell；**起进程前**再判一次）· `manager`（注册处设闸 + 分派 + `exec_audit(tool=hook_command)`）·
`config.SUPPORTED_TYPES`（文件入口认 `type: "command"`）。**租户不可达**由两道既有闸保证：
声明文件不允许 `owner: user`，且 `hooks_allow_user_defined` 默认 false —— 两者都有用例打在 `command` 形态上。

### 4.4 判定语义：结构化 JSON + **退出码 2**，档位只有两档

* **两种输入都接受**（**已落地**）：① 既有结构化 JSON `{"decision": "deny"|"block", "reason": "..."}`；
  ② **DSH/Claude Code 兼容**：**退出码 2 = 阻断**，stderr（尾部、脱敏、截断）作为原因。
  ⇒ 第二条让"已有的 Claude Code / Codex 钩子脚本"能直接复用；`run_command_hook` 把退出码 2 折成
  `{"decision": "deny", ...}`，于是 `_blocking_decision` **只有一套**，两种形态与两种输入都在此合流。
* **三个档位：`deny` / `allow` / `ask`（✅ 已落地，2026-10-09）**。
  `ask` **不是**新增交互面：Chiron 的审批本来就是**服务端**流程，因此 `ask` 的落点是**把这次
  调用提升为"需要用户确认"** —— 与 `tool_policy` 判 `confirm` 时**完全同一套**手续：
  `runtime._begin_approval`（注册 pending future + 审批票据 + `asked` 审计 + `approval` 事件）
  → 调用方 `yield` 事件 → `_await_approval` 收决定 → 批准后补执行。**前端与网关零改动。**
  落地形态（三处）：① 判定层用 `HookDecision`（**`str` 子类**，值 = 原因、附加 `kind`）区分
  `deny` / `ask` —— 继承 `str` 让既有"原因串"断言（`is None` / `in` / `startswith`）全部照旧；
  ② `_execute_tool` 在 `PreToolUse` 返回 `ask` 时**不执行工具**，把"待确认原因"作为返回值的
  第二项**上抛**给 `_guarded_execute_tool` 的 allow 分支（那里本来就是 `(result, approval_evt)`
  两元契约，**主循环无需改动**）；③ `_execute_tool(..., approved=True)` 时 `ask` **不再生效**
  （否则用户批一次、hook 问一次 ⇒ 无限循环），而 `deny` **仍然生效**（hook 只能收紧，
  不会被"批准过"抵消）。
  **两处边界**：`UserPromptSubmit` 上的 `ask` **按拒绝处理**（提示词没有"可确认的对象"，
  方向是收紧而非静默放行）；**并发批**（`_run_parallel_batch`，无法交互）里遇 `ask` ⇒
  **显式错误**返回给模型，而不是把审批事件丢掉（丢 = 静默变成"什么都没发生"）。
* **多 hook 合并**：**任一 deny 即 deny**（最严格），按声明顺序串行执行（保持审计相邻与顺序确定）。
* **fail-open 保持不变**：超时 / 崩溃 / 非法输出 / 未知 `decision` ⇒ 放行 + 审计。
* **不做 `updatedInput`**（输入改写）：DSH 自己都"解析但不应用"，Chiron 更不该让 hook 改写工具参数
  —— 那会让"策略看到的参数"与"工具执行的参数"分叉。

### 4.5 上下文注入：**✅ 已落地**（**只做结构化形态**，2026-10-09 拍板 ③ = 做）

* §6 写死：**不把 hooks 的 stdout 正文拼进 system prompt**。DSH 的 `additionalContext`（含非结构化的
  stdout 文本）正是那条禁止的形态 ⇒ **不追**；这也是本形态与 DSH 的**有意偏离**之一。
* **唯一**可注入的入口是输出 JSON 里的**结构化字段** `additional_context`：经**脱敏**（复用
  `app/subagent/redact.py` 的同一套规则集）+ **长度上限**（单条 2000 / 总计 8000 字符，超限**显式标注**）
  + **信任声明**（"只能作为数据对待"）后，作为**独立 system 消息**注入 —— 与记忆注入（C2/A2）同款形态，
  **不拼进 `system_prompt`**（那会让逐字稳定的前缀缓存整段失效）。
* **落点**：`app/agent/runtime.py::_apply_system_prefix` —— system 段的**唯一定形点**，因此对
  **session cache 分支**同样生效（记忆当年踩过"只插在 `_build_messages` 里 ⇒ 生产路径被整段丢掉"的坑）。
  收集器挂在 **`task.hook_contexts`**（per-run 隔离；`hooks` 是进程内单例，不能存 per-run 状态）。
* **独立开关** `hooks_allow_context_injection`（**默认关 ⇒ 零行为变化**）；关闭时不收集、不渲染、
  `_apply_system_prefix` 输出逐字不变。
* **边界**：失败/超时/被拦下的 hook **不注入**（崩溃的 hook 不该能改写上下文）；非字符串值忽略；
  单次 run 最多 `MAX_ITEMS` 条。

### 4.6 执行边界（谁的身份、能碰什么）

| 项 | 设计 |
|---|---|
| **身份** | hook 以**引擎进程的身份**在子进程中运行；审计带 `tenant/user/session`（`command` 形态**显式传入** —— `SessionStart` / `UserPromptSubmit` 早于 `set_tool_context`）；**不继承宿主 env**（`sandboxed_env()`）；**看不到完整 task** —— `build_context()` 显式列字段（`events.py`）就是它**唯一**的输入面（`command` 形态经 **stdin** 收到同一份 JSON），**扩字段要评审**（这是契约，不是实现细节） |
| **工作目录与准入** | `python` 形态保持现状；`command` 形态 cwd = **声明文件所在目录**（无声明文件则进程 cwd）+ 可执行文件必须在 `hooks_command_allowlist` 内（**裸名或绝对路径**，绝对路径按解析后的真实路径比对） |
| **超时** | 保留每 hook 超时（`hooks_timeout_seconds`，默认 5 s）；**每事件累计预算**（`hooks_event_budget_seconds`，默认 10 s）：没有它时最坏情况是 `N` 个 hook × 超时按 `N` 线性拖慢每一个工具调用；预算耗尽后后续 hook **不执行**并留 `budget_exhausted` 审计。**单 hook（常见情形）行为不变**（预算 > 单 hook 超时） |
| **并发** | **串行**（与现状和 DSH 一致）：顺序确定、审计相邻、合并规则与顺序无关 |
| **谁能注册** | 部署级（文件或进程内 API）；租户级仅进程内 + `hooks_allow_user_defined=true`（现状保留）；**文件不能声明租户级**。`command` 形态另需 `hooks_allow_commands=true` + allowlist 命中 —— **两道闸都在 `manager.register` 一处**，文件入口与进程内入口共用 |
| **审计** | 复用 `hooks_audit.jsonl`（**已加** `handler` / `command` 摘要（脱敏+截断）/ `truncated`）；`command` 形态**另写一条 `exec_audit.jsonl`（`tool="hook_command"`）**，因为**只有 exec_audit 会跨副本集中摄取**（N4）—— 新面必须在集中审计里可见；沿用 UTC+毫秒与统一脱敏 |
| **清单** | 任何**新的起进程位置**必须在 `tests/test_exec_audit_inventory.py` 里归类（`hooks/runner.py` 已是 `AUDITED_BY_CALLER`，理由已更新为"hooks 审计 + `command` 形态另写 exec 审计"；**不归类即测试失败**） |
| **反向调用** | hook **不得**回调引擎内部 API（无入站通道、无内部令牌注入） |

---

## 5. 与 §6「不扩大可执行面」的关系（逐条对齐）

| §6 条款 | 本设计如何满足 |
|---|---|
| 不扩大可执行面 | **本批次的核心，不能靠"复用既有基线"解释掉**（§4.3 实测：直接复用不可行）。**2026-10-09 拍板 = (b)，且五条已全部落地**：① 在 §6 **显式登记为例外**并写明"租户不可达"；② 独立开关默认关；③ 命令走**部署自备的 allowlist**（**不复制**面向模型的那张）；④ 复用隔离原语（`sandboxed_env` / RLIMIT / 截断 / 审计）且审计标 `hook_command`；⑤ 进执行入口清单。**租户不可达**：`hooks_allow_user_defined` 默认 false，且文件不允许 `owner=user`（两者都有用例打在 `command` 形态上） |
| 不照搬 deepagents 的 12 个事件 | 只加 **1** 个（`UserPromptSubmit`），且要求"引擎侧有真实对应点"（§4.1 给出点位）；其余 23 个 Claude Code 事件明确不加 |
| 不把 hooks 的 stdout 拼进 system prompt | **不做 stdout 注入**；`command` 形态 exit 0 时也**只**把 stdout 当**结构化 JSON** 解析（正文绝不进上下文）；若做上下文注入（批次 4），只走结构化字段 + 独立 system 消息 + 信任声明 |
| 不新增服务端同步端点 | `webhook` 是**出站**调用；不新增任何入站端点；hook 不能反向调用引擎 |
| 不为对齐形态发明事件 | 不加 `PreCompact` / `SessionEnd` / 运行级停止；`ask` **复用既有审批通道**（不发明新的交互面） |
| §1.1 不引新依赖 | `command` 只用标准库（`shlex` / `asyncio`）+ **既有** `app/tools/sandbox.py` 的隔离原语；`webhook` 复用既有 HTTP 客户端 + `ssrf.py`；声明文件用标准库 JSON |
| §1.3 默认值必须"什么都不做" | `hooks_enabled=False` **且** `hooks_config_path=""` 时：不读文件、不注册、不执行 —— **零行为变化**（现有回归用例即护栏） |
| §1.3 失败要显式 | 解析失败：error 日志 + 审计 + 不注册（不静默降级）；执行失败：fail-open 但**必留审计**；`ask`/未知输出：告警 + 审计 |
| §1.2 迁移单 head | **本设计不需要任何数据库迁移**（声明文件 + settings 开关），因此不引入单 head 风险 |

---

## 6. 兼容性映射（Claude Code / Codex → Chiron）

| 方言事件 | Chiron 事件 | 差异 |
|---|---|---|
| `SessionStart` | `SessionStart` | Chiron 不消费 `additionalContext`（§4.5） |
| `UserPromptSubmit` | **`UserPromptSubmit`（新增）** | 支持阻断；不支持 stdout 上下文 |
| `PreToolUse` | `PreToolUse` | 支持阻断；**不支持 `ask`**；不支持 `updatedInput` |
| `PostToolUse` | `PostToolUse` | 支持阻断反馈；不支持改写工具输出 |
| `Stop` | `Stop` | **只观测**：不强制"再执行一步"（§4.1 明确不给 hook 延长 run 的权力） |
| `SubagentStart` / `SubagentStop` | `SubagentStart` / `SubagentStop` | 一致（只观测） |
| （DSH 无） | `PostToolUseFailure` | **Chiron 多一个**：失败分支有独立语义点，保留 |
| 其余 23 个 | — | 忽略 + 告警 |

**载荷**：保持"显式字段"原则 —— `event` / `session_id` / `tenant_id` / `user_id` + 工具类事件的
`tool_name` / `tool_arguments` / `tool_result` / `ok`。**不照搬 Claude Code 的完整 payload**
（`transcript_path`、`cwd`、`permission_mode` 等会顺带泄漏宿主结构，且 Chiron 侧无对应物）。

---

## 7. 验收（对外可验证的行为；沿用 §3.5 的判据风格）

1. **默认零行为变化**：`hooks_enabled=false` 或 `hooks_config_path=""` 时，现有 `tests/test_hooks.py` 全绿且**无任何新副作用**；
2. **声明面真的生效**：一个含 `PreToolUse` 的 `hooks.json` 能让指定工具被阻断；**同一份文件在开关关闭时零效果**；
3. **只能收紧**：hook 返回 `allow`/`ask`/未知输出时，**被 `tool_policy` 拒绝的工具依然被拒**（hook 不能放宽）；
4. **退出码约定**：退出码 2 ⇒ 阻断且 stderr 作为原因；退出码 0/1/其它 ⇒ 放行且留审计；
5. **fail-open + 留痕**：超时 / 崩溃 / 非法 JSON ⇒ 放行，且 `hooks_audit.jsonl` 里能看到 `outcome=timeout|error`；
6. **声明解析失败不静默**：坏 JSON / 未知事件名 ⇒ error 日志 + 审计 + 引擎正常启动 + 不注册；
7. **执行入口清单**：新增的起进程位置在 `tests/test_exec_audit_inventory.py` 中有归类（否则测试失败）；
8. **无新依赖**：`pyproject`/依赖清单不变（`ruff` / `mypy` 门禁不变）；
9. **多租户不可用**：`hooks_allow_user_defined=false` 时，用户来源的注册被拒 + 审计（现有用例已覆盖）。

**批次 2b 另加三条**（对 `command` 形态；用例落在 `tests/test_hooks_command.py`）：

10. **闸口是 fail-closed 的**：`hooks_allow_commands=false` ⇒ 条目**被拒并留审计**（不是"配了但不生效"）；
    allowlist 为空 ⇒ **一条也不批**；可执行文件不在 allowlist ⇒ 拒。且**起进程之前会再判一次**。
11. **两张白名单不能混**：`git` 在**面向模型**的沙箱白名单里、却不在 hook allowlist 里 ⇒ **拒绝**
    （这是把 §4.3 的实测结论变成断言，而不是留在注释里）。
12. **新面进集中审计**：`command` 形态每次执行都写一条 `exec_audit.jsonl`（`tool="hook_command"`），
    身份显式传入（`SessionStart` / `UserPromptSubmit` 早于 `set_tool_context`）。

**批次 3 / 4 另加三条**：

13. **`webhook` 闸口 fail-closed**：开关默认关 + host 白名单默认空 ⇒ 一个目标也不批；**注册不触网**
    （只判静态事实 —— 注册时做 DNS 解析会让"离线部署"变成"配了却注册失败"），而 `assert_safe_url`
    在**执行时**拦内网 / 云元数据；
14. **`webhook` 载荷内容无关**：请求体只有 `event` / `tool_name` / `ok` / `ts`，**不含** `tool_arguments` /
    `tool_result` / 租户身份；非 2xx 与 3xx 一律**非阻断失败**并留审计；超时与传输错误 fail-open；
    响应体积有上限（流式计数）；
15. **上下文注入默认关且只走结构化字段**：关闭时收集与渲染都是空操作（`_apply_system_prefix` 输出
    逐字不变）；开启后 hook **打印的 stdout 正文不进上下文**，只有 `additional_context` 经脱敏 +
    长度上限 + 信任声明后**独立成一条 system 消息**；
16. **`UserPromptSubmit` 可阻断**：deny ⇒ `manager.user_prompt_submit()` 返回原因、runtime 以
    `guardrail_blocked` 事件结束本轮（**不进主循环**）；不 deny ⇒ 返回 `None` 且照常继续；
    审计留一条 `blocked=True`。**它仍不允许 matcher**；
17. **`ask` 走既有审批通道**：`PreToolUse` 返回 `ask` ⇒ **工具不执行**，`_guarded_execute_tool`
    返回 `(None, approval 事件)`（与 `tool_policy` 的 `confirm` **同一套**手续、前端零改动）；
    批准后由 `_await_approval` 执行时 **不再询问**；`deny` 仍生效；默认关时**零行为变化**；
    并发批里遇 `ask` ⇒ 显式错误（不产生事件、不留 pending）。

---

## 8. 实现批次（每批独立可验收）

| 批次 | 内容 | 风险 | 验收 |
|---|---|---|---|
| **1** ✅ **已落地（2026-10-09）** | **声明面**：`hooks_config_path` + `hooks.json` 加载 + matcher（锚定正则）+ 校验 + 预算 + 启动告警 + 未知事件忽略 | 低（无新执行面） | **§7-1/2/6 已满足**（2026-10-09 逐条复验：用例名与判据一一对应 —— `test_default_off_zero_behavior_change` / `test_empty_path_is_a_noop` · `test_declared_hook_blocks_matching_tool` / `test_loaded_hooks_do_not_run_while_disabled` · `test_bad_json_registers_nothing` / `test_missing_file_is_a_failure_not_a_crash` / `test_structurally_wrong_documents_are_refused` / `test_oversized_file_is_refused`）；**§7-8 / §7-9 也满足**（`app/hooks/*.py` 顶层 import **只有标准库 + `app`** ⇒ 零新依赖 · 用户来源注册被拒有用例）· `tests/test_hooks_config.py` **21 条**全绿 · 引擎全量 `pytest -m "not integration"` **1957 passed** · `mypy app/ acp_adapter/` **245 文件 0 错** · `ruff check .` 干净 |
| **2a** ✅ **已落地（2026-10-09）** | §4.1 的**事件新增**（`UserPromptSubmit`，**只观测**）+ 接线与用例；**每事件累计预算**（`hooks_event_budget_seconds`，默认 10 s：单 hook 情形行为不变，`N` 个 hook 不再线性拖慢） | 低 | 新事件有落点与用例；预算耗尽留 `budget_exhausted` 审计且**不静默跳过** |
| **2b** ✅ **已落地（2026-10-09，拍板 ① = (b)）** | **`command` 形态**：`create_subprocess_exec`（不走 shell）+ **隔离原语复用**（`sandboxed_env` / `_rlimit_kwargs` / `truncate_execute_output`）+ **部署自备 allowlist**（`hooks_command_allowlist`，**允许绝对路径**）+ 独立开关 `hooks_allow_commands`（默认关）+ 退出码 2 约定 + stderr 摘要 + 审计 `handler`/`command`/`truncated` + `exec_audit(tool=hook_command)` + 进执行入口清单 | 中（新增**运维级**可执行面 ⇒ 已在 §6 登记例外） | **§7-3/4/5/7 + 10/11/12**（`tests/test_hooks_command.py` **19 条**，含真的起子进程；已做变异验证：把开关判定改成恒放行 ⇒ 2 条用例红） |
| **3** ✅ **已落地（2026-10-09，拍板 ⑤ = 载荷只送内容无关字段）** | **`webhook` 形态**：出站 `POST`（不跟随重定向）+ `ssrf.py` 守卫 + host 白名单 + 内容无关载荷 + 响应体积上限；`app/hooks/runner.py::run_webhook_hook`（新） | 中（**数据出境面** ⇒ 载荷与身份默认都不出境） | **§7-13/14**（`tests/test_hooks_webhook.py`：SSRF 用**真的** `assert_safe_url`、判定语义用假客户端；已做变异验证：移除 SSRF 调用 ⇒ 2 条用例红） |
| **4** ✅ **已落地（2026-10-09，拍板 ③ = 做）** | **上下文注入**：结构化字段 `additional_context` + 脱敏 + 长度上限 + 信任声明 + **独立 system 消息**；`app/hooks/inject.py`（新）+ `_apply_system_prefix` 一处接线 | 中高（**改变"什么能影响模型输入"** ⇒ 独立开关默认关） | **§7-15**（`tests/test_hooks_context_injection.py`） |

| **5** ✅ **已落地（2026-10-09 拍板：两项都做）** | **`UserPromptSubmit` 可阻断**（进入 `BLOCKING_EVENTS`；`manager.user_prompt_submit()` 返回原因，runtime 以**既有**的 `guardrail_blocked` 事件结束本轮 —— 不发明新事件）· **`ask` 判定**（`HookDecision.kind` 分 `deny`/`ask`；`ask` ⇒ 与 `tool_policy` 的 `confirm` **共用** `runtime._begin_approval` ⇒ 前端/网关零改动；`_execute_tool(approved=True)` 防重复询问，`deny` 仍生效；并发批与 `UserPromptSubmit` 两处边界显式处理） | 低（阻断**用户自己的**输入 / 只是把调用提升为需确认） | **§7-16/17**（`tests/test_hooks_ask.py` **8 条** + `test_hooks.py`；已做变异验证：把 `ask` 判定改成永不命中 ⇒ **3 条用例红**） |

**批次 2a 落地清单**：`events.USER_PROMPT_SUBMIT`（加入 `ALL_EVENTS`；**2026-10-09 起也进 `BLOCKING_EVENTS`** —— 见批次 5）·
`manager.user_prompt_submit()` · `manager._trigger` 的预算封顶（`min(单 hook 超时, 剩余预算)`）·
`settings.hooks_event_budget_seconds` · `runtime.py` 在输入护栏后接线 · `tests/test_hooks.py` 新增 4 条。

**批次 2b 落地清单**（供评审对照）：`settings.hooks_allow_commands` + `settings.hooks_command_allowlist`
（`app/config.py`）· `runner.split_command` / `runner.command_policy_error` / `runner._exe_identity` /
`runner.run_command_hook` / `runner.BLOCK_EXIT_CODE`（新）· `manager.HANDLER_*` + `Hook.handler` /
`Hook.command` + `manager.register` 的闸口 + `manager._trigger` 的分派 + `manager._record_command_exec_audit`（新）·
`audit.record_hook` 的 `handler` / `command` / `truncated` 字段 · `config.SUPPORTED_TYPES` 纳入 `command` ·
`exec_audit.record_execution` 的**显式身份覆盖**（`tenant`/`user`/`session`，默认仍取 contextvars）·
`tests/test_hooks_command.py`（新，19 条）+ `test_hooks_config.py`（原"`command` 被跳过"的用例改用 `webhook`）·
`tests/test_exec_audit_inventory.py` 的归类理由。

**批次 1 落地清单**（供评审对照）：`python-engine/app/hooks/config.py`（新，加载 / 校验 / 预算 / 审计）·
`manager.Hook.matcher` + `manager._trigger` 锚定筛选 · `events.MATCHER_EVENTS` + `matcher_applies()` ·
`settings.hooks_config_path` · `app/main.py` 启动接线 · `tests/test_hooks_config.py`（新）。

> 批次 1 单独就有价值：它把"机制已就绪但不可用"变成"可用"，且**不新增任何可执行面**。

---

## 9. 明确不做

* 不照搬 Claude Code 那 23 个未支持事件（DSH 自己也不支持）；
* 不做**新的**交互面：`ask` 只能走**既有的服务端审批通道**（不新增端点、不新造一套等待语义）；
* 不做 `updatedInput` / 工具输出改写（会让"策略看到的"与"执行的"分叉；DSH 自己也没应用）；
* 不做运行级停止 / 延长（hook 不应拥有 run 的生命周期权力）；
* 不做**租户级文件声明**（等于把可执行面交给租户）；
* 不做 **stdout → system prompt** 注入（§6 明令）；
* 不把**面向模型**的命令白名单当成 hook 的白名单：两者信任级不同（前者约束"模型挑的命令"，
  后者是"运维声明"）；若采纳 §4.3 的 (b)，hook 用**部署自备**的 allowlist，而不是复制那一张；
* 不做热加载（引入"配置生效时刻"的不确定性）；
* 不引新依赖；不新增入站端点；**不需要数据库迁移**。

---

## 10. 复现方式与证据索引

### 10.1 DSH 侧（从 `app.asar` 枚举，非猜测）

```bash
# asar 头部：offset 0=4 / 4=总长 / 8=payload 长 / 12=JSON 长度 / 16 起为 JSON 目录
# 本机路径：D:\Program Files (x86)\deepseek-harness\resources\app.asar
# 命中条目：@deepseek-ai/dsh-hook-protocol（README.zh.md 10,467 B）
#           @deepseek-ai/dsh-hooks-claude-code（15,557 B）
#           @deepseek-ai/dsh-hooks-codex（13,231 B）
#           @deepseek-ai/dsh-webhook（4,915 B）+ dsh-webhook-github（3,843 B）
```

### 10.2 Chiron 侧

| 路径 | 关键数据 |
|---|---|
| `python-engine/app/hooks/events.py` | **100 行；8 事件**；`BLOCKING_EVENTS={PreToolUse}`；`build_context` 显式字段（2026-10-09 复测；原记 77 行 / 7 事件） |
| `python-engine/app/hooks/manager.py` | **648 行**；默认关 / owner 模型 / fail-open / 启动告警 / **锚定 matcher** / **每事件累计预算** / **按 `Hook.handler` 分派三种形态** / `command` 与 `webhook` 各自的注册闸口 / `command` 的 exec 审计 / 上下文收集 / **三态判定**（`HookDecision` + `_decision_of`）（2026-10-09 批次 5 复测；原记 416 行） |
| `python-engine/app/hooks/runner.py` | **577 行**；三种形态：`run_hook`（`plugin_runner` 沙箱子进程）+ `run_command_hook`（批次 2b）+ **`run_webhook_hook`（批次 3：`webhook_target_error` / `webhook_payload` / 不跟随重定向 / 响应体积上限）**；永不抛（原记 143 行） |
| `python-engine/app/hooks/inject.py` | **144 行（新，批次 4）**；`additional_context` 的**提取 / 脱敏 / 上限 / 信任声明渲染**；`collect()` 挂在 `task.hook_contexts`（per-run 隔离） |
| `python-engine/app/hooks/audit.py` | **146 行**；`hooks_audit.jsonl`；脱敏 + 截断；批次 2b/3 加 `handler` / `command`（目标摘要：命令文本或 URL）/ `truncated` |
| `python-engine/app/agent/runtime.py` | `_begin_approval`（审批的**唯一**手续点：`tool_policy` 的 `confirm` 与 hook 的 `ask` 共用）· `_execute_tool(..., approved=)` 返回 `(结果, 待确认原因)` · `_guarded_execute_tool(..., allow_approval=)`。hook 调用点：`session_start` · `user_prompt_submit`（**可阻断**）· `stop` · `before_tool_use` · `subagent_start` / `subagent_stop` · `after_tool_use`；`_apply_system_prefix`（批次 4 注入落点）—— 行号 2026-10-09 起多次变动，**按符号定位** |
| `python-engine/app/config.py` | `hooks_enabled` / `hooks_allow_user_defined` / `hooks_timeout_seconds` / `hooks_event_budget_seconds` / `hooks_config_path` / `hooks_allow_commands` / `hooks_command_allowlist` / **`hooks_allow_webhooks`** / **`hooks_webhook_allowlist`** / **`hooks_allow_context_injection`**（后三者为批次 3/4） |
| `python-engine/plugin_runner.py` | **139 行**；stdin/stdout **显式 UTF-8**（批次 3 顺手修的既有缺陷：`print` / `sys.stdin.read()` 走 locale 编码，Windows 上会让含非 ASCII 的 hook 源码/结果损坏） |
| `python-engine/tests/test_hooks.py` | **357 行**；注册 / 阻断 / 超时 / 逃逸 / 用户拒绝 / 审计 + **两端 UTF-8 编码契约** |
| `python-engine/tests/test_hooks_config.py` | **308 行**（批次 1）：声明面加载 / 锚定 matcher / 校验 / 预算 / 未知事件忽略 |
| `python-engine/tests/test_hooks_command.py` | **399 行（批次 2b）**：闸口 fail-closed / 两张白名单不混 / 绝对路径 / 不走 shell / 退出码 2 与其余非 0 / stdout JSON / stdin 载荷 / 超时 / 输出截断 / 声明面端到端 |
| `python-engine/tests/test_hooks_webhook.py` | **413 行（新，批次 3）**：闸口 / **SSRF（用真的 `assert_safe_url`）** / 判定语义（假 httpx 客户端）/ 不跟随重定向 / 超时与传输错误 / 体积上限 / **载荷内容无关** / 声明面端到端 |
| `python-engine/tests/test_hooks_context_injection.py` | **237 行（批次 4）**：默认关零行为变化 / 只走结构化字段 / 失败与阻断的 hook 不注入 / 独立 system 消息 / 脱敏 / 单条与总量上限 |
| `python-engine/tests/test_hooks_ask.py` | **205 行（批次 5）**：`ask` 是独立档位 / **走既有审批通道**（含 `asked` 审计与 pending future）/ 批准后不再询问（含对照）/ `deny` 不被"批准过"抵消 / 默认关零行为变化 / 并发批显式拒绝 |
| `python-engine/tests/test_exec_audit_inventory.py` | `hooks/runner.py` = `AUDITED_BY_CALLER`（理由已含 `command` 形态的 exec 审计） |
| `python-engine/app/tools/sandbox.py` | `sandboxed_env()` / `_rlimit_kwargs()` / `truncate_execute_output()` / `_normalize_exe`（`command` 形态**复用的隔离原语**；白名单**不**复用） |
| `python-engine/app/tools/exec_audit.py` | `record_execution()`：批次 2b 加**显式身份覆盖**（默认仍取 contextvars —— hook 的早期事件早于 `set_tool_context`） |
| `python-engine/app/tools/ssrf.py` | 8,936 B（webhook 形态要复用的出站守卫） |

### 10.3 相关文档

[与 DSH 的差距分析](dsh-gap-analysis.md)（§插件生态一行：钩子协议已列未来规划）·
[Agent 安全与可靠性](agent-safety-and-reliability.md)（§1.4 MCP 收窄、§4.2 撤销栈）·
[运行现场 checkpoint 续跑设计](run-checkpoint-design.md)（同类"设计先行"文档的格式先例）·
[多实例部署指南](deployment-multi-instance.md) · [架构与请求链路](architecture.md) ·
[开发路线图](development-roadmap.md)
