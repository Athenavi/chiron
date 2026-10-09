# 钩子协议设计（追平 DSH 的钩子协议）

> **状态：设计，待评审；批次 1 与 2a 已落地。**
> **批次 1（声明面）** 已实现并验证（2026-10-09）：`app/hooks/config.py` + `hooks_config_path` +
> 逐 hook `matcher`（锚定）+ 启动接线；`tests/test_hooks_config.py` **21 条用例**。
> **批次 2a（事件 + 预算）** 已实现并验证（2026-10-09）：`UserPromptSubmit`（只观测）+ 每事件累计预算；
> `tests/test_hooks.py` 新增 4 条。
> 批次 2b（`command` 形态）**待拍板 ①**；批次 3 / 4 **未实现**（③ 待拍板，见 §0）。
>
> **依据**：`vendor/规划.md` §3.2「追平 DSH：**钩子协议**」（用户 2026-10-09 指示）要求
> **落地前先出设计**，并指定三件必答：**事件清单 · 执行边界 · 与 §6「不扩大可执行面」的关系**。
> 本文即对该三项的回答，外加声明面、执行形态、判定语义与实现批次。
>
> **约束**（`vendor/规划.md`）：§1.1 不引新依赖 · §1.3 新增机制默认值必须"什么都不做"、失败要显式 ·
> §6 不照搬 deepagents 的 12 个事件、不把 hooks 的 stdout 拼进 system prompt、不为对齐形态发明事件、
> 不扩大可执行面。
>
> **取证基线**：Chiron @ `master` `d7a9d501`（2026-10-09）；DSH = 本机安装的
> `@deepseek-ai/dsh-hook-protocol` / `dsh-hooks-claude-code` / `dsh-hooks-codex` / `dsh-webhook`
> （从 `app.asar` 枚举并导出原文，见 §10.1）。
> **取证强度**：【枚举】读源码 · 【文档】读契约原文 · **【实测】** 本次跑过。**未跑过**任何 hook 运行时。

## 0. 结论摘要（需要拍板的四件事）

| # | 结论 |
|---|---|
| 1 | **"钩子协议落后"这个判断要修口径**：Chiron 的钩子机制**已经落地并接线**（沙箱执行 / 审计 / 默认关 / 有测试），**事件集现为 8 个** —— 与 DSH **实际支持的 7 个**相比，`UserPromptSubmit` **已由批次 2a 补齐**（只观测），并**多一个** DSH 不支持的 `PostToolUseFailure`（**2026-10-09 更正**：本条此前写"缺 `UserPromptSubmit`"，与 §8 的批次表自相矛盾） |
| 2 | ~~**真正的缺口是"没有声明面"**~~ —— **✅ 该缺口已补（批次 1，2026-10-09）**：`settings.hooks_config_path`（`app/config.py:148`，**默认空 = 不读文件、不注册、不执行 = 零行为变化**）+ `app/hooks/config.py` 的 `load_hooks_config()` 已在 **`app/main.py:571-575` 接线**，用例 `tests/test_hooks_config.py` **21 条**。**2026-10-09 更正**：本条此前写"`hooks.register()` 在生产代码里零调用者、也没有 `hooks.json` 一类的声明文件"，那是**批次 1 之前**的状态（同样与 §8 自相矛盾） |
| 3 | **要拍板的四件事**：① **§6「不扩大可执行面」是否允许运维声明的本地命令钩子** —— 实测 `run_in_sandbox` **直接复用不可行**（无 stdin · 白名单仅 18 个可执行文件 · 绝对路径被逃逸拦截挡下 · cwd 锁沙箱 · 审计标签硬编码），三选项与推荐见 §4.3（**建议 (b)：允许但按运维面治理，并在 §6 显式登记为例外**）；② 是否支持 DSH 的 `ask` 判定（建议**不支持**，会新增交互面）；③ 上下文注入是否做（建议**Phase 1 不做**，§6 已禁止 stdout 注入）；④ `UserPromptSubmit` 是否允许阻断（现为**只观测**，建议维持） |

> **2026-10-09 补：DSH 侧对同样四件事的答案（读 `dsh-hook-protocol` 的 README，**实现源码级**）** ——
> ① **只跑 `command` 钩子**（*"Only command hooks run; `http`, `mcp_tool`, `prompt`, and `agent` handlers are **skipped with a warning**"*）
> ⇒ 命令钩子就是对标物的**唯一**形态；② **有 `ask`**（*"a Claude Code hook can **request confirmation** instead of blocking outright;
> **the Codex bridge does not surface this option**"*）；③ **有上下文注入**（*"a hook can return extra text that the model sees in the next request"*）；
> ④ **`UserPromptSubmit` 可阻断**（*"a hook that exits with code 2 **stops the prompt or tool call**, and its error output is shown as the reason"*）。
> **另外两条关键语义**：**只有退出码 2 才阻断**，其它退出码一律**非阻断失败**（动作照走、失败记日志），
> **连"起不来的钩子"也同等对待** ✓；钩子还能返回 `{"continue": false}` 请求停止运行，但**只记录、无运行级效果**
> （它自己标为 Known Limitations ✓）。**另核**：`UserPromptSubmit` / `Stop` 在 DSH 里**没有 matcher 主体**（*matchers are discarded*）
> —— Chiron 的 `MATCHER_EVENTS` **同样只含工具类事件**（且更严：带 matcher 的非工具事件**直接拒绝**而不是静默丢弃）✓。
> **⇒ 结论**：②③④ 都是**有意的偏离**（Chiron 分别以「不新增交互面 / §6 禁止 stdout 注入 / 只观测」为由），
> **不是"不知道对标物怎么做"**；① 与"追平 DSH"一致。**四条都可据此拍板。**

---

## 1. 现状：已落地什么（用证据，不靠"模块存在"）

| 维度 | 现状 | 证据 |
|---|---|---|
| 事件集 | **8 个**：`SessionStart` · **`UserPromptSubmit`**（批次 2a，**只观测**）· `PreToolUse`（**唯一可阻断**）· `PostToolUse` · `PostToolUseFailure` · `Stop` · `SubagentStart` · `SubagentStop` —— **2026-10-09 复测**（原写 7 个） | `app/hooks/events.py`（**3,962 B / 100 行**），`ALL_EVENTS` 校验注册名 |
| 执行隔离 | 独立子进程跑 `plugin_runner.py`，复用插件沙箱：`setrlimit` 128 MB / 5 s CPU、`code_guard` 静态 AST + 受控 builtins、`sandboxed_env()` 清空宿主 env；stdin 传 payload / stdout 收结果，**stdout 上限 256 KiB** | `app/hooks/runner.py`（4,959 B / 143 行） |
| 失败姿态 | **永不抛给调用方**（`HookOutcome`）；超时/崩溃/非法输出**一律放行**（fail-open），但**必留审计** | `runner.py:91-134`、`manager.py:86-101` |
| 编排 | 门面单例 `hooks`；按事件串行执行；`PreToolUse` 阻断以"工具错误"回灌给模型；**批次 2a 起每事件有累计预算**（`min(单 hook 超时, 剩余预算)`，默认 10 s ⇒ N 个 hook 不再线性拖慢） | `app/hooks/manager.py`（**17,461 B / 416 行**，2026-10-09 复测；原记 12,815 B / 317 行） |
| 接线 | 启动告警 + **声明面加载** `main.py:566-575`；`runtime.py`：`1160`（SessionStart）· **`1166`（UserPromptSubmit，批次 2a）** · `2145`（Stop）· **`3105`（PreToolUse）· `3111/3116`（Subagent\*）· `3117`（PostToolUse）** | 【枚举】**2026-10-09 复测**：原记的 `main.py:567-569` 与 `runtime.py:1134/2113/3068/3074/3079/3080` **全部过期**（代码长大 + 批次 2a 新增一处） |
| **分层顺序** | hook 在 `_guarded_execute_tool`（工具策略 / 服务端授权 / 审批）**之后**才跑 ⇒ **只能收紧、不能放宽**，不会成为绕过 `tool_policy` 的新通道 | **`runtime.py:3092`** 的注释 + 调用点（2026-10-09 复测；原记 `3050-3056`） |
| 审计 | `logs/hooks_audit.jsonl`，UTC+毫秒，字段含 tenant/user/session/duration/exit_code/**blocked**/outcome/reason（脱敏 + 截断 200 字符） | `app/hooks/audit.py`（4,028 B / 112 行） |
| 默认值 | `hooks_enabled=False` · `hooks_allow_user_defined=False` · `hooks_timeout_seconds=5` · **`hooks_event_budget_seconds=10`**（批次 2a：每事件累计预算）· **`hooks_config_path=""`**（批次 1：**默认空 = 不读文件 / 不注册 / 不执行 = 零行为变化**） | `app/config.py:135-148`（2026-10-09 复测；原记 `:133-140` 且只有前三个） |
| 执行入口清单 | `hooks/runner.py` **已被机械归类**为 `AUDITED_BY_CALLER`（"manager 在执行前后调用 `audit.record_hook`"） | `tests/test_exec_audit_inventory.py:57` |
| 测试 | `tests/test_hooks.py`（**14,655 B / 327 行**）：注册校验、阻断、超时放行、沙箱逃逸（`import os` 被拒）、用户 hook 拒绝、审计、启动告警 · **`tests/test_hooks_config.py`（13,064 B / 308 行，21 条，批次 1）**：声明面加载 / **锚定 matcher** / 校验 / 预算 / 未知事件忽略 | 【枚举】**2026-10-09 复测**（原记 `test_hooks.py` 11,331 B 且未提 `test_hooks_config.py`） |

### 1.1 关键缺口：**没有声明面**

* `hooks.register()` 的调用者**只有测试**（`tests/test_hooks.py`）——生产代码零调用；
* 仓库里**没有** `hooks.json` 一类的声明文件，`configs/`、`config/`、`.env.example`、`docker-compose.yml`
  里**没有任何 hook 配置**；`app/config.py` 只有上述 3 个开关。【枚举】
* ⇒ **今天没有任何办法让一个部署真正启用一个 hook**（除非改 Python 源码）。所以"落后"的实质不是机制缺失，
  而是**缺少把机制接上产品的最后一环**。

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
| **声明面** | **`hooks.json` 已落地**（批次 1：`settings.hooks_config_path` + **锚定 matcher** + 校验 + 预算 + 启动告警 + 未知事件忽略；**默认空 = 不读文件/不注册/不执行 = 零行为变化**） | `hooks.json`（Claude Code / Codex 方言） | **已追平**（形态仍是子集：Chiron 只接受 `type: python`；`command` / `webhook` 见批次 2b / 3）—— **2026-10-09 更正**：此行原写「**无**（只能进程内 `register`）」，是批次 1 之前的状态 |
| 执行形态 | Python `code`（沙箱子进程） | `command`（本地命令，走 `dsh-shell` 执行器） | 形态不同 |
| 判定语义 | 结构化 JSON `{decision, reason}`；失败 fail-open | **退出码 2 = 阻塞** + stderr 原因；其余非阻塞 | **缺退出码约定** |
| 判定档位 | 只有"阻断/放行" | `deny > ask > allow` | Chiron 无 `ask` |
| matcher | **已有**（**锚定**正则 `re.fullmatch`；**只对工具类事件合法** —— 其余事件带 matcher **直接拒绝**而不是静默丢弃） | 按名字 / pattern 选择 | **已追平且更严**（DSH 对 `UserPromptSubmit`/`Stop` 的 matcher 是 *discarded*）—— **2026-10-09 更正**：此行原写「无（注册到事件即全跑）」，是批次 1 之前的状态 |
| 上下文注入 | **无**（§6 禁止 stdout 拼接） | `additionalContext` 模型可见 | **刻意不同** |
| 运行级停止 | 无（也不该有） | `continue:false`（**且无实际效果**） | 不需要 |
| 审计 | 独立 JSONL（`hooks_audit.jsonl`） | 会话内 `hook/*` 仅日志事件 | 形态不同，各有优劣 |
| 失败姿态 | fail-open + 审计 | 非 2 退出码非阻塞 + 记录 | **已一致** |
| 不抛异常 | `HookOutcome` 永不抛 | 每步降级 | **已一致** |

**结论**：真正要补的是 **声明面 + 本地命令形态 + 退出码约定**；事件集只差 1 个。

---

## 4. 设计

### 4.1 事件清单：**7 → 8**，只加 `UserPromptSubmit`

**保留现有 7 个**（每个都有引擎侧真实对应点，见 §1）。**新增 1 个**：

| 新事件 | 引擎侧对应点 | 为什么它不是"发明出来的面" |
|---|---|---|
| `UserPromptSubmit` | `app/agent/runtime.py:1160-1166` —— 输入护栏校验之后、`hooks.session_start()` 紧邻处（**2026-10-09 复测：批次 2a 已落地，实际接线在 `:1166`；原记 `1131-1134`**；`session_start` 已在同一个 `try` 块顶部） | 用户提示词在那一刻**已经在手**（`task`），且这是"一次 run 进入主循环之前"的唯一入口；DSH 支持它，且它有真实语义（可阻断/可附加上下文） |

> ⚠ **该事件的判定档位是刻意收窄的**：Chiron 首版取**只观测**（**不加入 `BLOCKING_EVENTS`**）。
> 理由：阻断**用户自己的**提示词是**对用户**的控制点，与 `PreToolUse`"只收紧工具策略"的性质不同；
> 且拒绝输入已由网关/引擎的输入护栏承担（`runtime.py:1118-1121`）。是否允许阻断留待 §3.3 产品决定。

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
  ⚠ **批次 1 只注册 `type: "python"`**；`command` / `webhook` 目前**跳过并告警**（待 §4.3 的 ① 拍板）。
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
| `command`（新） | **Phase 1** | 本地命令 | ⚠ **不能直接复用 `run_in_sandbox()`**，见下；**待拍板 ①** |
| `webhook`（新） | **Phase 2** | 出站 `POST`，复用既有 HTTP 客户端 + **`app/tools/ssrf.py` 的 SSRF 守卫**（内网/云元数据拒绝 + 体积上限 + 流式越界即断） | 默认关 + 独立开关；**不新增任何入站端点**（§6）；⚠ **待拍板 ⑤：数据出境** —— 工具事件的载荷含 `tool_arguments` / `tool_result`（可能是租户内容），把它 POST 到运维配置的 URL 是一次**数据出境**决定（§6 排除远程 sandbox provider 的理由正是"数据出境 + 计费归属"）；建议默认只送**内容无关**字段（事件名 / 工具名 / 结果成败 / 耗时），要送载荷需单独开关 |

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
| **(b) 允许，但按"运维面"治理**（本文推荐） | 独立开关（默认关）+ **只允许部署级声明** + 复用隔离原语 + 审计标 `tool="hook_command"` + 命令走**部署自备的 allowlist**（允许绝对路径）+ 进执行入口清单 | 新增一个**运维级**可执行面 ⇒ **必须在 §6 显式登记为例外**并写明"租户不可达" |
| **(c) 只允许白名单内可执行文件** | 严格复用 `_ALLOWED_EXECUTABLES` | 可执行面零变化；但兼容性目标**基本落空**（只多出 `git`/coreutils 钩子） |

**本文推荐 (b)** —— 它是**唯一**能真正"追平 DSH 钩子协议"的选项，且风险面被三件事收住
（默认关 · 只有运维能声明 · 租户不可达）。**但它需要 §6 的所有者同意**：这是一次**显式登记的可执行面扩大**，
不是"复用既有基线"就能解释掉的。若选 (a) 或 (c)，本设计仍然自洽（声明面 + 事件 + 判定语义可单独交付），
只是要接受"DSH 的 `command` 钩子搬不过来"。

### 4.4 判定语义：结构化 JSON + **退出码 2**，档位只有两档

* **两种输入都接受**：① 既有结构化 JSON `{"decision": "deny"|"block", "reason": "..."}`；
  ② **DSH/Claude Code 兼容**：**退出码 2 = 阻断**，stderr（尾部、脱敏、截断）作为原因。
  ⇒ 这条让"已有的 Claude Code / Codex 钩子脚本"能直接复用，是"追平"的最低成本落点
  —— **但能否复用取决于待拍板 ①**（§4.3 已实测：`run_in_sandbox` 的白名单/绝对路径限制会让它落空）。
* **档位只有 deny / allow**：**不支持 `ask`**。理由：Chiron 的审批是**服务端**流程（审批票据 + 网关路由），
  让 hook 触发一次交互式审批等于新增一个交互面与一条等待路径，与 §6「不新增服务端同步端点 / 不扩大可执行面」
  相抵触；DSH 的 `ask` 只在其 Claude Code 桥接里有，且它自己也把 `allow` 记为"不会预审批"。
  hook 返回 `ask` ⇒ **按未知输出处理：告警 + 审计 + 放行**（fail-open，且显式可见）。
* **多 hook 合并**：**任一 deny 即 deny**（最严格），按声明顺序串行执行（保持审计相邻与顺序确定）。
* **fail-open 保持不变**：超时 / 崩溃 / 非法输出 / 未知 `decision` ⇒ 放行 + 审计。
* **不做 `updatedInput`**（输入改写）：DSH 自己都"解析但不应用"，Chiron 更不该让 hook 改写工具参数
  —— 那会让"策略看到的参数"与"工具执行的参数"分叉。

### 4.5 上下文注入：**Phase 1 不做**；Phase 2 只做结构化形态（产品决定）

* §6 已经写死：**不把 hooks 的 stdout 直接拼进 system prompt**。DSH 的 `additionalContext`（含纯 stdout 上下文）
  正是这条禁止的形态 ⇒ **刻意不追**。
* 若将来要做，唯一可接受的形态是**结构化字段**（`additional_context`，不是 stdout）经
  **脱敏 + 长度上限 + 信任声明**后作为**独立 system 消息**注入 —— 复用 §5.1 已经确立的"记忆注入"形态。
* 这**改变"什么能影响模型输入"**，属**产品决定**，应进 `vendor/规划.md` §3.3，并配独立开关
  （`hooks_allow_context_injection`，默认关）。

### 4.6 执行边界（谁的身份、能碰什么）

| 项 | 设计 |
|---|---|
| **身份** | hook 以**引擎进程的身份**在子进程中运行；审计带 `tenant/user/session`；**不继承宿主 env**（`sandboxed_env()`）；**看不到完整 task** —— `build_context()` 显式列字段（`events.py:61-77`）就是它**唯一**的输入面，**扩字段要评审**（这是契约，不是实现细节） |
| **超时** | 保留每 hook 超时（`hooks_timeout_seconds`，默认 5 s）；**每事件累计预算已落地**（`hooks_event_budget_seconds`，默认 10 s）：没有它时最坏情况是 `N` 个 hook × 超时按 `N` 线性拖慢每一个工具调用；预算耗尽后后续 hook 不执行并留 `budget_exhausted` 审计。**单 hook（常见情形）行为不变**（预算 > 单 hook 超时） |
| **并发** | **串行**（与现状和 DSH 一致）：顺序确定、审计相邻、合并规则与顺序无关 |
| **谁能注册** | 部署级（文件或进程内 API）；租户级仅进程内 + `hooks_allow_user_defined=true`（现状保留）；**文件不能声明租户级** |
| **审计** | 复用 `hooks_audit.jsonl`；新增字段：`handler`（`python`/`command`/`webhook`）、`command` 摘要（脱敏）、`truncated`；沿用 UTC+毫秒与统一脱敏 |
| **清单** | 任何**新的起进程位置**必须在 `tests/test_exec_audit_inventory.py` 里归类（`hooks/runner.py` 已是 `AUDITED_BY_CALLER`；`command` 形态若新增执行点，**不归类即测试失败**） |
| **反向调用** | hook **不得**回调引擎内部 API（无入站通道、无内部令牌注入） |

---

## 5. 与 §6「不扩大可执行面」的关系（逐条对齐）

| §6 条款 | 本设计如何满足 |
|---|---|
| 不扩大可执行面 | **这一条正是待拍板 ① 的核心，不能靠"复用既有基线"解释掉**（§4.3 实测：直接复用不可行）。若选 **(b)**：`command` 形态**新增一个运维级可执行面** ⇒ ① 在 §6 **显式登记为例外**并写明"租户不可达"；② 独立开关**默认关**；③ 命令走**部署自备的 allowlist**（**不复制**面向模型的那张）；④ 复用隔离原语（`sandboxed_env` / RLIMIT / 截断 / 审计）且审计标 `hook_command`；⑤ 进执行入口清单。若选 **(a)/(c)**：本形态不做、或只允许白名单内可执行文件 ⇒ 可执行面零变化。**任何选项下租户都不可达**（`hooks_allow_user_defined` 默认 false，且文件不允许 `owner=user`） |
| 不照搬 deepagents 的 12 个事件 | 只加 **1** 个（`UserPromptSubmit`），且要求"引擎侧有真实对应点"（§4.1 给出点位）；其余 23 个 Claude Code 事件明确不加 |
| 不把 hooks 的 stdout 拼进 system prompt | **不做 stdout 注入**；Phase 2 若做上下文注入，只走结构化字段 + 独立 system 消息 + 信任声明 |
| 不新增服务端同步端点 | `webhook` 是**出站**调用；不新增任何入站端点；hook 不能反向调用引擎 |
| 不为对齐形态发明事件 | 不加 `PreCompact`/`SessionEnd`/运行级停止；不加 `ask` |
| §1.1 不引新依赖 | `command` 复用 `run_in_sandbox`；`webhook` 复用既有 HTTP 客户端 + `ssrf.py`；声明文件用标准库 JSON |
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

---

## 8. 实现批次（每批独立可验收）

| 批次 | 内容 | 风险 | 验收 |
|---|---|---|---|
| **1** ✅ **已落地（2026-10-09）** | **声明面**：`hooks_config_path` + `hooks.json` 加载 + matcher（锚定正则）+ 校验 + 预算 + 启动告警 + 未知事件忽略 | 低（无新执行面） | **§7-1/2/6 已满足**（2026-10-09 逐条复验：用例名与判据一一对应 —— `test_default_off_zero_behavior_change` / `test_empty_path_is_a_noop` · `test_declared_hook_blocks_matching_tool` / `test_loaded_hooks_do_not_run_while_disabled` · `test_bad_json_registers_nothing` / `test_missing_file_is_a_failure_not_a_crash` / `test_structurally_wrong_documents_are_refused` / `test_oversized_file_is_refused`）；**§7-8 / §7-9 也满足**（`app/hooks/*.py` 顶层 import **只有标准库 + `app`** ⇒ 零新依赖 · 用户来源注册被拒有用例）· `tests/test_hooks_config.py` **21 条**全绿 · 引擎全量 `pytest -m "not integration"` **1957 passed** · `mypy app/ acp_adapter/` **245 文件 0 错** · `ruff check .` 干净 |
| **2a** ✅ **已落地（2026-10-09）** | §4.1 的**事件新增**（`UserPromptSubmit`，**只观测**）+ 接线与用例；**每事件累计预算**（`hooks_event_budget_seconds`，默认 10 s：单 hook 情形行为不变，`N` 个 hook 不再线性拖慢） | 低 | 新事件有落点与用例；预算耗尽留 `budget_exhausted` 审计且**不静默跳过** |
| **2b** | **`command` 形态**：复用**隔离原语**（env/RLIMIT/截断/审计）+ 部署自备 allowlist；退出码 2 约定；stderr 摘要；审计标 `hook_command`；进执行入口清单。⚠ **待拍板 ①**（§6 是否允许运维声明的本地命令钩子；实测 `run_in_sandbox` 直接复用不可行，见 §4.3） | 中 | §7-3/4/5/7 |
| **3** | **`webhook` 形态**：复用 HTTP 客户端 + `ssrf.py`；独立开关；默认关 | 中 | §7-5 + SSRF 用例 |
| **4**（可选） | **上下文注入**（结构化字段 + 独立 system 消息 + 信任声明）—— **需产品决定** | 中高 | 需先更新 §3.3 与 §6 |

**批次 2a 落地清单**：`events.USER_PROMPT_SUBMIT`（加入 `ALL_EVENTS`，**不进** `BLOCKING_EVENTS`）·
`manager.user_prompt_submit()` · `manager._trigger` 的预算封顶（`min(单 hook 超时, 剩余预算)`）·
`settings.hooks_event_budget_seconds` · `runtime.py` 在输入护栏后接线 · `tests/test_hooks.py` 新增 4 条。

**批次 1 落地清单**（供评审对照）：`python-engine/app/hooks/config.py`（新，加载 / 校验 / 预算 / 审计）·
`manager.Hook.matcher` + `manager._trigger` 锚定筛选 · `events.MATCHER_EVENTS` + `matcher_applies()` ·
`settings.hooks_config_path` · `app/main.py` 启动接线 · `tests/test_hooks_config.py`（新）。

> 批次 1 单独就有价值：它把"机制已就绪但不可用"变成"可用"，且**不新增任何可执行面**。

---

## 9. 明确不做

* 不照搬 Claude Code 那 23 个未支持事件（DSH 自己也不支持）；
* 不做 `ask` 判定（会新增交互面与等待路径）；
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
| `python-engine/app/hooks/manager.py` | **416 行**；默认关 / owner 模型 / fail-open / 启动告警 / **锚定 matcher** / **每事件累计预算**（原记 317 行） |
| `python-engine/app/hooks/runner.py` | 143 行；沙箱子进程；256 KiB 上限；永不抛 |
| `python-engine/app/hooks/audit.py` | 112 行；`hooks_audit.jsonl`；脱敏 + 截断 |
| `python-engine/app/agent/runtime.py` | **`1160`（SessionStart）· `1166`（UserPromptSubmit）** · `2145`（Stop）· **`3105`（PreToolUse）· `3111/3116`（Subagent\*）· `3117`（PostToolUse）** · **`3092`**（分层顺序：hook 在审批之后）—— 2026-10-09 复测（原记 `1131-1134` / `3050-3056` / `3068-3080`） |
| `python-engine/app/config.py` | **`135-148`**：`hooks_enabled` / `hooks_allow_user_defined` / `hooks_timeout_seconds` / **`hooks_event_budget_seconds`** / **`hooks_config_path`**（原记 `133-140` 且只有前三个） |
| `python-engine/tests/test_hooks.py` | **14,655 B / 327 行**；注册 / 阻断 / 超时 / 逃逸 / 用户拒绝 / 审计（原记 11,331 B） |
| `python-engine/tests/test_hooks_config.py` | **13,064 B / 308 行 / 21 条**（批次 1）：声明面加载 / 锚定 matcher / 校验 / 预算 / 未知事件忽略 |
| `python-engine/tests/test_exec_audit_inventory.py` | `hooks/runner.py` = `AUDITED_BY_CALLER` |
| `python-engine/app/tools/sandbox.py` | `run_in_sandbox()` / `sandboxed_env()`（command 形态要复用的基线） |
| `python-engine/app/tools/ssrf.py` | 8,936 B（webhook 形态要复用的出站守卫） |

### 10.3 相关文档

[与 DSH 的差距分析](dsh-gap-analysis.md)（§插件生态一行：钩子协议已列未来规划）·
[Agent 安全与可靠性](agent-safety-and-reliability.md)（§1.4 MCP 收窄、§4.2 撤销栈）·
[运行现场 checkpoint 续跑设计](run-checkpoint-design.md)（同类"设计先行"文档的格式先例）·
[多实例部署指南](deployment-multi-instance.md) · [架构与请求链路](architecture.md) ·
[开发路线图](development-roadmap.md)
