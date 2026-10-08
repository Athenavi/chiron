# Chiron 与 DeepSeek Harness（DSH）的差距分析

> **依据**：本地只读取证 —— `vendor/dsh-synapse`（真实 DSH 插件）、`D:\Program Files (x86)\deepseek-harness`
> （app.asar 目录索引 + `package.json`）、`$DSH_HOME`（`C:\Users\athenavi\.dsh`）+ 官方文档站。
> **静态对照，不是跑分**；**未在本机运行过 DSH**，运行时行为一律标「未验证」。
>
> 与另两份的关系：`docs/reasonix-gap-analysis.md`（平台 vs 同类产品）、本份（平台 vs **本地 harness**）。
> DSH 是**本地单租户 harness**，Chiron 是**多租户 SaaS** —— **只有"同一层"才可比**：agent 循环 / 子 agent /
> 事件契约 / 执行隔离 / 可审计性。交付形态与 UI 不可比，不列入差距。

## 0. DSH 是什么（证据）

| 事实 | 证据 |
|---|---|
| 形态：Electron 桌面壳 + TS/Cordis 插件树；另有 Web UI / CLI / Python SDK | `app.asar` 根 `package.json` = `@deepseek-ai/dsh-desktop@0.2.0-rc.2`；`resources\runtime\cli\bin\dsh.cmd` |
| 组合机制：Cordis DI + profile + patch 分层（`cordis.patch.yml` 最后叠加） | `vendor/dsh-synapse/package.json` 的 `dsh.bundle.patch`；官方 [develop/basic](https://deepseek-harness.github.io/deepseek-harness/en/develop/basic/) |
| **单用户、单机、无租户**：`$DSH_HOME` 即边界，凭据是本地文件 | `$DSH_HOME\.credentials.yaml`（结构只读，值未读）；无 org/tenant/quota 包 |
| 默认危险权限是常态：SDK 最小 profile pin `danger-full-access` | [python-sdk 文档](https://deepseek-harness.github.io/deepseek-harness/en/guide/python-sdk) |
| 会话日志**默认外发 DeepSeek**（`dsh-session-log-deepseek`） | 同上，`maxBytes` 8 MiB |
| 明确不支持多写者 / 多实例 | `vendor/dsh-synapse/docs/architecture.md`（单文件 last-writer-wins） |
| **复核（2026-10-08）**：**287 个依赖逐条枚举**（自写 asar 读取器，方法见 §4） | 仍**无**任何 tenant/org/quota/billing/metering 包 ⇒ 切口 #2 的"⛔ 未发现"升级为**枚举后确认**；但有 `dsh-api-account-controller` / `dsh-authorization` / `dsh-deepseek-account-platform` / `dsh-anonymous-user-id` —— 那是**客户端对 DeepSeek 账号平台的集成，不等于自建多租户** |
| **复核（2026-10-09）**：把 289 个一方包的**声明文本**（`README.md` + `package.json`，595 文件）全部读出（见 §3 第 2、12 条） | **证强**：`tenant` 全仓仅 1 处，原文 *"nothing is decided"* ⇒ 无多租户。**修口径**：`dsh-client-ui-settings-account` 自述 *"Manage DeepSeek login and open **Platform billing pages**"*（余额 / 充值 / 赠送额度 / 配额通知）⇒ **有面向个人的计费面**，但**无租户级计量**；`dsh-token-meter` 自陈 *"not a billing record"* |

## 1. 逐层对照（只列"同一层"）

| 维度 | DSH | Chiron | 方向 |
|---|---|---|---|
| Agent 循环 | `dsh-agent-loop`（2026-10-09 **实现源码级**）：建新 agent 或**恢复持久会话**，逐回合驱动"模型请求 → 流式响应 → 工具执行 → 持久历史"；**`maxParallelToolCalls` 限制并发**（schema `default(10)`，`DEFAULT_MAX_PARALLEL_TOOL_CALLS = 10`），**独占调用是屏障** —— 源码 `fillPool()`：`if (nextToStart > 0 && mode === "parallel" && ctx.tools.executionMode(nextCall.exec).kind !== "parallel") break;`（首批之后一遇非 `parallel` 种类即停止填充），**结果按模型顺序提交**（*Results and contexts commit in model order*），**中止时**已启动的排空并提交、**被跳过的调用也记结果**（*records results for skipped calls*）；只有标准"调模型、跑工具、重复"生命周期不够用时才该自写 `Agent` | `app/agent/runtime.py`（主循环）+ `loop.py`（遗留，见路线图 §3）+ `loop_guard.py` + `task_budget.py`；**并发工具同样分批**：`_split_tool_batch`（`runtime.py:328`）的并发条件是**三条全满足** —— ① 动作级别是 **`read`**（写/删/外部类有副作用、顺序敏感）· ② 不是 **`ask_user`**（有交互语义，必须逐个等答案）· ③ **不与同批其它调用共享路径**（同路径并发读写在语义上无法保证顺序；借鉴 deepagents 的 `_parallel_file_mutation_error`）；上限由 `tool_concurrency_limit`（**默认 4**，设 1 即完全串行）的信号量约束（`:2312`）；**取消同样保留已流式文本**：`internal/api/submit_handler.go` 在流结束后 `flushText()` + `saveDraft(true)`（注释：*"定型：覆盖正常结束、**被取消**、断线等所有路径"*），回合终态收敛为 `cancelled`（`:560-569`） | **相当，且三处差异值得记**（2026-10-09 **双侧源码级**）：① **判定依据不同** —— DSH 按**每个工具声明的执行种类**（`executionMode(...).kind === "parallel"`），Chiron 按**策略级别 `read` + 同批路径冲突检测** ⇒ **Chiron 多一道"同路径不并发"的保守判据**；② **默认并发上限**：DSH **10** vs Chiron **4**（Chiron 更保守）；③ 中止语义 DSH 写明"**被跳过的调用也记结果**"，Chiron 侧**本行未取证 ⇒ 不据此下结论**。**共同点**：并发只对安全调用放开、独占保持顺序、结果按序提交、取消保留已交付文本（Chiron 把"被取消/断线"写进了同一处注释与状态收敛里） |
| 事件契约 | session 事件（`user/message` `assistant/message` `tool/call` `turn/end`…），**内部 TS 词汇表**，无对外稳定承诺 | SSE ~13 种；**思考**走独立 `thinking` 事件（A1，契约见 [聊天记录契约](transcript-contract.md)），**用量**自 C3 起每次 LLM 调用发独立 `usage` 事件（**本次调用增量**，`done` 仍给整轮累计）；**无 `TurnStarted`/`Message` 通道**（回合边界靠前端按 `turnId` 推断；`Phase` 已决定**不做** —— 主对话没有这个真实状态，见切口 #3） | DSH 的**边界更清晰**；Chiron 的思考与用量已对齐 |
| Fork / 分支 | 原生 durable fork：`parentSession` + `seedLength`/`firstLiveSeq`，可 `atSeq` | **已实现，非"待核实"**（2026-10-08 核对）：血缘列 `sessions.parent_session_id`（+索引 `ix_sessions_parent_session_id`）· 分叉点 `branch_from_seq` · 模式 `branch_mode`/`branch_state`/`branch_keep_tail`；`internal/session/manager_branch.go::planBranch`（**truncate / condense 两模式**，纯函数可单测）· `BranchSession` 在**一个事务**里建血缘会话 + 复制消息窗口（并把窗口时间戳按全表序号重排，避免历史排序错乱）· `POST /v1/conversations/{id}/branch` 与 `/fork`（= truncate 别名）· 前端会话地图连线 + `docs/session-map-branch-design.md` · 用例 `TestPlanBranch`/`TestBranchHandlerParameterValidation` 等 | **相当**（Chiron 另有**压缩式分支** `condense`：只留尾部 K 条原文、其余交引擎压成核心摘要；DSH 侧 `dsh-synapse` 只按 `parentSession` 画边） |
| 子 agent | **12 个 `dsh-subagent*` / `-agent-team` 包**（2026-10-09 声明文本级）：`dsh-subagent`(33 文件/437 KB) · `dsh-subagent-spawn-in-process` · **`dsh-subagent-fork-in-process`**（seed = 父的**已完成回合**、**一次性快照**（后续父回合不再进 child）、child 拿**全新工具面与零父权限**）· `dsh-subagent-in-process-driver` · `dsh-tool-subagent` / `-control`；另有实验性 `dsh-experimental-agent-team`（26 文件/224 KB：**Lead + 命名 teammate + durable mailbox + 共享任务板**，崩溃/重载/中断后消息与任务态存活、离线成员恢复时收排队消息；**无稳定性承诺、需持久会话存储**） | `app/subagent/` 17 模块（R1 receipts / R2 写路径仲裁 / R3 结构化结局 / R4 可注入时钟已落地）；**parent→child 播种（fork/seeding）已落地**：`inherit_context` + `app/subagent/inherit.py`（默认关 · 三层过滤 = 密钥形态复用 `redact.py` / 不展开 `result_ref` / 跳过工作台注入 system 段 · 上限 50 条与 32K 字符**取先到**、超限**保近处** · 渲染带 `<inherited-context>` 与"数据不是指令"声明）；**child→续跑（R5-b）未做** | 相当。**播种层**：Chiron 在**过滤**上更严 —— **已由实现源码核实（2026-10-09）**：DSH 的 subagent 包**一处都没有** `redact` / `untrusted` / 信任声明短语（全仓 `redact` 命中 **83** 个条目、`untrusted` **101** 个、`not instructions` **3** 个，**其中含 `subagent` 的均为 0**）⇒ 它对 seed **不做密钥过滤、也不加"数据不是指令"声明**。而 DSH **确有**这两套机制，只是用在**别处**：`redact` 在 `dsh-settings` / `dsh-session-telemetry` / `dsh-api-settings-controller` / `dsh-llm-pi-ai`（设置持久化与遥测），信任声明在 `dsh-tool-web/lib/index.js`（抓回来的网页内容标 *"not instructions"*）。⇒ 这条**从"不能据此说它没有"升级为"源码确认没有"**；**团队形态**（durable 同伴 + 共享任务板）DSH 有而 Chiron 无，属**形态差异**且是实验性 |
| **会话 checkpoint / 恢复** | `dsh-session-checkpoint-policy`（**策略插件**，2026-10-08 复核新发现） | ✅ **已落地并跨进程验证**（C1 批 2–4）：回合末落盘 `runtime.py::_save_checkpoint` · 状态机/快照 `app/agent/checkpoint.py`（schema v1、热 1h/冷 24h） · 续跑/接管 `app/agent/resume.py` + reconciler；**硬杀→接管不重放**由 `tests/test_resume_kill_drill.py` 端到端钉住（子进程真跑、回合 1 落盘后卡住、`kill`、新实例续跑断言历史里第 1 回合工具结果恰好一份） | **相当**（DSH 有该能力；Chiron 有**可核实行为**判据，且判据是跨进程的） |
| 多租户 / 计费 | ⛔ **287 个依赖逐条枚举后，仍无任何租户/配额/账务/metering 包**（`dsh-api-gateway` / `dsh-authorization` / `dsh-api-account-controller` 是客户端对 DeepSeek 账号平台的集成，不是自建多租户） | JWT + RBAC + 402 计费预检 + 429 配额 + 租户并发池 | **Chiron** |
| 多副本横向扩展 | 文档明确"只跑一个实例" | 跨实例会话运行锁 + Redis Stream 断线重放 + run 亲和 | **Chiron** |
| MCP | `dsh-mcp-client` 单用户直连 | 连接池 + owner lease + `MCP_MAX_*` 预算 + 拒绝指标 | **Chiron** |
| 执行隔离 | OS 级本机限制（bwrap/landlock/Seatbelt/Windows ACL），威胁模型=**保护用户本机**；**且不只是一个沙箱，而是一层策略插件**：`dsh-sandbox-policy` / `dsh-permission-presets` / `dsh-user-approval` / `dsh-fs-observation-policy` / `dsh-output-retention` / `dsh-spill-policy` / `dsh-tool-call-timeout-policy`（2026-10-08 复核） | **A 层（代码层）**：`app/tools/{code_guard,sandbox,run_code,_sandbox_worker}.py` 的静态 AST 守卫 + 运行时 import/内置函数白名单 + RLIMIT + `exec_audit`；威胁模型=**保护平台与其它租户**。`chiron-sandbox/` 是**执行工作根**（本机已跑出 `default/anonymous`，未纳入版本控制），**B 层（独立 sandbox 服务）代码已落地（2026-10-09 更新）**：`sandbox-service/service.py` 直接复用引擎那份 `run_in_sandbox` 基线；`python-engine/tests/test_sandbox_service_multiprocess.py` 把它起成**独立进程**并断言"命令真在服务进程里跑 + 服务进程写下带身份的审计"；**剩部署形态与真实验证**（§3.1） | **不可比**（威胁模型相反）；且**A 层只提高门槛、不构成隔离** —— 实测就有一处逃逸（见切口 #6） |
| **工具审批 / 权限** | `dsh-permission-presets` + `dsh-user-approval`（**channel-neutral 一次性审批 seam**；2026-10-09 **实现源码级**）：策略 **`ask`**（schema 默认，交给部署的**人类或机器 answerer**）/ **`never`**（确定性拒绝，可每会话覆盖）；**结果词表恰 4 个**：`allowed-once` / `rejected` / `cancelled` / `unavailable`；answerer 是 `approval/request` **waterfall 监听器**，**部署只组合一个终结 answerer**；**缺失或失败的 answerer ⇒ fail closed** —— 源码即 `waterfall(…, () => Promise.resolve("unavailable"))`，且**非词表返回值被归一成 `unavailable`**、answerer 抛错也是 `unavailable`；**`never` 直接 `rejected`**（不问 answerer）；**`allowed-once` 是唯一的"准"**（无会话级授权）；**审计对是"回合内"的硬要求**：`request()` 若没有未结束回合就**抛错**（理由写得很清楚 —— *a bare event between turns is **crash-tail garbage on reload***），`approval/asked` 在 `decide()` **之前**写、`approval/decided` 在**之后**写，**任一审计 append 失败即整体拒绝**（*returning an unlogged decision would violate the pair*）；模型只见工具结果与当前策略，**看不到**权限 UI 与审计事件；不变式伴生插件把 `asked`/`decided` 配在**同一未结束回合**内 | `internal/api/submit_handler.go::SubmitApproval` 把决定**路由到承载该 session run 的引擎实例**（Redis 归属映射）+ 引擎 `runtime._await_approval`：决定跨副本经 Redis 决策键（**单次消费 GET+DEL**）· **执行前二次核对票据**（`approval_req:{tool_call_id}`：`turn_id` 或**参数哈希**不符 ⇒ **拒绝执行**，fail-closed）· **超时即拒**（默认 300 s）· **参数被编辑后升级 ⇒ 拒绝执行**（escalation 守卫）· `edit` / `approve` / `deny` 三种决定（`edit` 必须带参数，否则 400）· **归属 + 调用者身份 + run token 三重校验** · 审计 `approval_audit.jsonl`（带 tenant/user/session）· **无人值守默认 fail**：CLI `--on-approval fail`（默认 ⇒ `ExitBlocked`）与 `--on-approval approve\|deny` 显式选择，用例 `TestRun_ApprovalDefaultsToFailExit2` 断言"**默认不得自动批准**" | **Chiron 略强**（多副本路由 · 票据二次核对 · 编辑升级守卫 · 三种决定 · 三重身份校验）；**DSH 强在两点**：① **可组合的 answerer seam**（机器 answerer + "服务自己从不提示人类"的通道中立）；② **`asked`/`decided` 配对不变式** —— 且**已核实**：Chiron 的 `approval_audit.jsonl` **只记"决定"不记"请求"**（`record_approval_decision` 的 5 个调用点（`runtime.py:2499/2515/2541/2578/2593`）**全在 `_await_approval` 内**，而审批事件的发出点 `runtime.py:2435` **没有审计**）⇒ **"问了但没等到决定"**（等待期间进程被杀、副本被回收、连接断开后无人再答）**在流水里看不出来**。DSH 记的是 *"every request **and** outcome"*。**已修（2026-10-09）**：在发出审批事件处补 `asked` 记录，并与 `decided` 按 `tool_call_id` **配对**（`approval_audit.py` 的 `EVENT_ASKED` / `EVENT_DECIDED` + `runtime._record_approval_request`；用例 `tests/test_approval_audit.py`、`tests/test_runtime.py`）—— 与既有四条流水同口径（UTC+毫秒、身份同源、fail-soft）。**入口清单已机械化**：`tests/test_approval_audit_inventory.py` 枚举 `app/` 下所有**发出审批请求**的位置并要求逐条归类（发起 / 转发已记录的子 Agent 审批 / 豁免+理由 / 已知缺口），未归类即失败 —— **已做变异验证**（临时加一处未归类的 `type="approval"` ⇒ 守卫红，退出码 1）。**"Chiron 是否有会话级授权"未核实**（引擎里无 `allow_session` 类关键词，前端/网关未逐处核 —— 按 §3 第 2 条口径，未核实不引用）。**形态差异（非缺陷）**：DSH 另有 `dsh-permission-presets` —— **一个产品级 Permissions 选择器把两个旋钮打包**（沙箱模式 + 审批策略）成命名预设（默认 `workspace-write` / `danger-full-access`；保留名 `custom` = 派生的"非预设"态、**可离开不可选中**；`auto` = Auto 审查集成，固定为 Full access + `ask`），`permission/preset` **仅在有效预设变化时追加**（净零选择不追加），且**选中即钉住会话**（后续设置改动不影响既有会话；恢复的会话保留其有效值）。**Chiron 没有这个产品面**：两个旋钮分别在**部署级**（`SANDBOX_BACKEND` + `sandbox_service_tenants` 白名单）与**请求级**（审批策略 / CLI `--on-approval fail\|approve\|deny`）—— 对多租户 SaaS 这是**有意的信任边界**，不是缺失 |
| **上下文 / 输出治理** | `dsh-output-retention`（**零依赖有界保留"库"**；2026-10-09 **实现源码级**：*deliberately a library, **not a cordis service or plugin** — takes no `ctx`, registers nothing, emits no events*）：它**只回答机械问题**「留了什么 / 省了什么」，**业务语义仍归工具**（分组、行号、退出码、provider 错误态、逐行预览、spill 文件、面向模型的措辞）。**`truncated` 的契约写得很准**：含义是「**保留器因预算而省掉了本来可得的内容**」，**不是**「上游不完整」—— 权限失败 / 跳过的二进制 / provider 部分失败 / 不可读候选**留在工具域字段里，绝不并进 `truncated`**。**两个保留器按资源模型分名**：`ItemRetainer` 管**有序逻辑单元**（路径 / grep 命中 / 搜索源，v1 只有 `head`，`push()` 逐单元报 `{kept,truncated}`，**调用方继续推所有观测单元 ⇒ 最终省略计数精确**，`finish()` 返回 `omitted:{kind:"exact",count}`）；`TextRetainer` 管**字节流**（bash stdout/stderr、网页正文；head / tail / headTail，**在 `finish` 处保住 UTF-8 边界** —— `trimTrailingPartialUtf8` 会回退掉不完整的多字节序列，**让前缀切点永不产生替换字符**）。状态**按实例、绝不跨调用**；`formatRetentionNotice` 统一省略措辞；`truncateWithoutSplittingSurrogatePair` 供工具侧使用· `dsh-spill-policy`（**按 token 预算把超大结果移出上下文**；2026-10-09 **实现源码级**）：`Config = z.object({ maxInlineTokens: z.number() })` —— **没有默认值**，`if (cap === void 0) return;` ⇒ **省略即关闭**（给了非法值则抛错）。流程：先按**最坏情况的 notice** 定价并从预算里**预留**（notice 单独就超预算 ⇒ 抛错），再 `retainContent` 保留**有序 head + `\n\n[...]\n\n` 间隔 + tail**（**head 与 tail 各分一半预算**；**图片绝不移动、也绝不部分保留** —— *unused space at an indivisible image stays unused*）；末尾附 notice：`( <精确省略字节数>[ Omitted N images.] Full formatted result stored at: <locator>. <retrievalHint> )` ⇒ **模型拿到"省了什么"+"全文在哪"+"怎么取回"**。落盘的全文里，每个图片位置换成**可执行读回的路径**（`[Image: "<readonlyPath>"; <mediaType>; <W>x<H>. Use read_image to view it.]`）。**边界**：只处理 `accept` 且无 `value` 的结果、**跳过 `read` 工具本身**（避免"读回又被卸载"的循环）、只对子 agent 结果或含图内容生效；图片定价用**模型专属的 image token calculator**，缺它就抛错（fail-closed）；文本切点另有 **UTF-16 代理对守卫**（`textSlice`，与保留库的 UTF-8 守卫互补）。**失败即放行**：`bound()` 的 catch 只 warn 并返回 `undefined` ⇒ **成功工具的内容照常可见**（*failures leave successful tool content visible*）· `dsh-fs-observation-policy`（**read-before-edit + 版本守卫**；2026-10-09 **实现源码级**）：它是**纯事件型**插件（不注册服务）—— 用一个 **owner/target 的 WeakMap** 记录每次"权威的在场/缺席观测"，再在三个 `fs/*` waterfall 上**派生意图**，**真正的原子检查由 provider 做**；**不装这个插件，工具就退回 provider 的"无条件变更"行为**（⇒ 该策略是**按组合生效**的）。三条判定逐字：**`writeIntent`** —— 未见或已确认缺席 ⇒ **`createIfAbsent`**（⇒「读一个不存在的路径 = **授权受保护的创建**」，且**不覆盖**）；已确认在场 ⇒ **`replaceIfVersion`（带观测到的版本）**。**`editIntent`** —— **未见直接抛 `FS_NOT_OBSERVED`**（消息即 *edit requires reading "…" first*）· 已确认缺席 ⇒ `FS_NOT_FOUND` · 在场 ⇒ 给出观测版本作 **CAS 依据**。**`observe`** 记录权威观测。**归属**：状态按 **owner（通常是当前 agent session）**分键、WeakMap 弱持有（会话被回收即释放）；**取不到 owner 时**（无 agent 的直接工具调用）—— *such calls **read freely but cannot satisfy** the write/edit prior-observation policy*。**HMR 安全**：每个 `apply()` 一个实例、注册 teardown 清空状态· `dsh-tool-call-timeout-policy`（per-tool deadline 包装，超时返回 `TOOL_TIMEOUT`；**自陈无法硬停下游工作**） | **多数已对齐**：截断 `truncate_execute_output`（head+tail、**报精确省略字节数**、附 `truncated` 布尔，且"**截断由产生它的那一层给出，不由上层猜**"）· 卸载 `result_ref` + `read_tool_result`（大工具结果移出上下文且**可回读**）· `fs_guard`（**read-before-write 版本签名 = mtime+size+首块 hash**，key 带 **tenant:user 前缀**防跨租户冲突）· 工具级超时**分散在各工具**（沙箱 120 s · 后台 job deadline · hooks 5 s） | **两处真实差异**：① **从未读过的文件** —— DSH **要求先读**（硬要求），Chiron **不限制**（`fs_guard` 自陈"兼容旧行为"）⇒ **Chiron 更松**（收紧机制**已就绪、默认关**，见 §3 第 13 条）；② **没有统一的 per-tool deadline 包装**（DSH 有，但它自陈无法硬停下游）。**Chiron 更强的一处**：观测是**租户/用户隔离**的（DSH 单用户无此维度） |
| 插件 / 技能生态 | Cordis profile patch + 插件面板 + HMR，已跑起 `dsh-synapse`/`dsh-im`；**另有 `dsh-hook-protocol` + `dsh-hooks-claude-code`/`dsh-hooks-codex`（钩子协议与 Claude Code/Codex 兼容）与 `dsh-webhook(-github)`**（2026-10-08 复核） | **技能侧已对齐**：`market/skills/` 6 个 SKILL.md 由 `SkillStore` 以 `scope=builtin` 读取（2026-10-08 复核并修好容器缺席问题）；**插件生态**（profile/面板/HMR/第三方插件真跑/**钩子协议**）仍明显落后 —— **钩子协议已列入未来规划（用户 2026-10-09 指示：追平 DSH）**，见 `vendor/规划.md` §3.2 | **DSH 领先（插件侧）** |
| 会话持久化 / 检索 | **事件溯源**（2026-10-09 **实现源码级**）：`dsh-session` 把**每一个模型可见的事实**记进 **append-only** 日志，并**从该记录派生模型历史**；消费者可 inspect / replay / fork / flush，**压缩只把被取代的条目从活动对话里隐藏、不删除**；`dsh-session-persistence` 是后端无关的持久接缝，其**共享存储语义**逐条为：events **contiguous from seq 0 且永不重写** · **残缺的物理尾部永不返回给读者**、且**由写路径在首次 append 前截断** · 读只校验**当前格式**记录、**未知词汇一律 fail-closed 拒绝**；**`append` 是 best-effort，`flush`（按句柄或服务级）才是持久性屏障**（服务级 flush「drains every active write handle's routed events and materializes its session」）；**单写者**由 `open(id, 'write')`「**claim single-writer ownership**」表达（`create` 即取得写所有权，`open(id,'read')` 只是观察）；**可见性边界写得很诚实**：进程内 `create` 一解析就可见，**别的进程要等它物化**，且「**崩溃前从未物化的会话等于从未存在**」（`handle.flush` 强制物化）。默认 JSONL(zstd) 后端 = 每会话一个压缩日志；格式 v0→v4 迁移链 + SQLite FTS5 检索 | PG 单一持久层 + Alembic 单 head；**检索是真全文检索**（2026-10-08 核对）：`internal/api/search_handler.go` 用 `to_tsvector('simple', m.content) @@ plainto_tsquery('simple', $1)` + `ts_rank` 排序（消息与会话名两路），`NewSearchHandler()` 挂在 public 端点 | 相当，但**模型不同**：DSH 是**事件溯源**（append-only 派生历史 + 压缩"隐藏不删"），Chiron 是**消息表 + checkpoint 快照**（对应 Reasonix 分析里那条"无事件级持久化"）。Chiron 侧的等价保证在**别的层**：PG 事务给出"无残缺记录"、回合终态收敛 + in-flight 标记/恢复（`app/agent/{checkpoint,resume}.py`）给出"中断后不重放"；**"一个会话一个写者"**由跨实例运行锁 + run 归属租约承担（DSH 在存储后端内表达）。**格式版本策略（2026-10-09 声明文本级，三方对照）**：DSH 对**旧格式**用**相邻迁移链**（`dsh-session-format` + `dsh-session-format-catalog` + 四个步骤包 `v0-to-v1` / `v1-to-v2` / `v2-to-v3` / `v3-to-v4`；catalog 在**模块初始化时校验整条链无缺口**，物理行**单遍**消费、不产生中间副本或冻结），对**无法忠实解释的日志一律拒绝**（*"refused with a **direction-aware** error, never misread"*；`assertVersion` 拒绝 **foreign** 版本、`validateStoredEvents` 拒绝未知词汇与已退役的预发布形态），**写入只写当前格式**。Chiron 的 checkpoint 快照**只写 v1、且拒绝更高版本**（`app/agent/resume.py`：放弃续跑、不触碰该快照）⇒ 与 DSH 在"**未知/未来版本**"上**立场一致（都拒绝）**，差别只在**旧格式**：DSH 有迁移链，Chiron 暂无历史格式可迁。**前瞻（不是当前差距）**：真出现 v2 快照时，值得照 DSH 的形态做 —— **相邻迁移 + 初始化时校验链无缺口**，而不是写一串 ad-hoc 分支 |
| CLI / SDK | `dsh` CLI + **Python SDK(PyPI)** + ACP/JSON-RPC | `cmd/chiron-cli`（运维 CLI）· **第一方 Python 客户端已落地（2026-10-09）**：`clients/python/chiron_client.py`（**零第三方依赖**：登录/注册 · 提交 · SSE 订阅含 `Last-Event-ID` 重连 · 取消）+ 契约单测 + **真实网关端到端用例**（`python-engine/tests/test_chiron_client_sdk_live.py`，本机实测通过）· 仍**未**：官方发布/PyPI、**兼容性承诺**、OpenAPI/JSON-RPC/ACP 面 | **部分追平（2026-10-09 已决定：停在示例客户端）** —— 客户端可用且**刻意不承诺兼容性**；官方发布/兼容承诺/OpenAPI-JSON-RPC 面**暂不做**（将来要发布需重开产品决定） |
| 可观测性 | OTel + 产品分析（+ 日志默认上传） | OTel 类埋点 + Prometheus/Alertmanager + **不默认上传**；2026-10-08 核对落点：`prometheus.yml` 抓 `python-engine:8000/metrics`（+ prometheus 自身）· `prometheus_alerts.yml`（`PythonEngineDown`/`HighPythonMemoryUsage`，**按 job 名写**）· `alertmanager.yml` · 网关侧 `GET /metrics`（admin） | 相当（Chiron 隐私更优） |
| 国际化 | 客户端 locale 包（中文覆盖度未取证） | 三语种 + 缺键护栏 | **Chiron** |

## 2. 可"超越"的切口（按代价/可核实性排序）

| # | 切口 | 为什么 DSH 做不了/不这么做 | Chiron 已有基础 | 代价 |
|---|---|---|---|---|
| 1 | **多副本一致性语义的对外承诺** | 架构上是单进程本地；多写者冲突是写进文档的限制 | 每会话 Redis Stream + `Last-Event-ID` 补发 + 会话运行锁心跳 + 跨实例取消（**广播前校验会话属主**）+ **session→实例归属路由**（`engine:run:*`）。**对外保证已成文**：[多实例部署指南 §9](deployment-multi-instance.md) 新增「多副本语义的对外保证」表（每条附取证与**边界**）；真实 Redis 覆盖见 `internal/broadcast/hub_live_test.go`、`internal/api/session_coord_live_test.go`、`internal/api/session_cancel_test.go`、`internal/engine/run_affinity_test.go` | **已闭环（本机 + CI）**：`python-engine/tests/drills/multi_instance_drill.py` 起**两个真实网关进程**，验跨实例扇出 / 实时不重复 / 断线重发不重复，**已接 CI 的 real-stack job**；仍缺**含引擎**的完整提交链路演练 |
| 2 | **MCP 多租户连接预算与 owner lease** | 单用户直连即可，无连接数经济学 | `MCP_POOL_ENABLED`/`MCP_MAX_*`/`MCP_OWNER_LEASE_ENABLED` + `mcp_pool_rejected_total`；**已有实测证据**（`tests/test_mcp_owner_lease.py`，真实 Redis：互斥/CAS/TTL/清单往返/桥往返·报错·超时），对外口径见 [多实例部署指南](deployment-multi-instance.md) §8 | **已闭环**（补测时连带修掉两处 pub/sub 连接生命周期缺陷） |
| 3 | ~~阶段（Phase）事件~~ **结案：不做**（**没有真实状态可上报**） | session 事件是内部词汇表，无对外稳定承诺 | **思考**：A1 独立 `thinking`；**用量**：C3 每次 LLM 调用一条 `usage`（**本次调用增量**）+ `done` 整轮累计，网关按**事件类型**分流以防重复计费，前端 `mergeTurnStats` 累加成本轮一行；**阶段**：**子 agent 侧本就有**（`subagent.status` = `queued\|running\|reasoning\|responding\|tool\|retrying\|终态`），而**主对话侧不存在"阶段"这个状态** —— 运行时是单一 ReAct 循环（`for turn in range(task.max_turns)`），`AgentMode`（normal / minimal / ptc / creative）是**工具/人设档案**，不是 planner→executor 的协调边界。⇒ **不为对齐对标物发明事件**；将来真引入协调边界时再评估 | **已结案** |
| 4 | **租户级全链路审计与可回放证据链** | 只有本机 session log，且默认上传；无"操作者/租户"维度 | 三条 JSONL 流水带 tenant/user/session：`approval_audit.py`（本轮补测并修掉**密钥明文落盘**）、`exec_audit.py`、`hooks/audit.py`；`ts` 已统一为 UTC+毫秒。**覆盖度已补**（2026-10-08）：`exec_audit` 此前只覆盖 `shell_exec` / `run_code` / `persistent_shell` 三条**前台**路径，**后台命令 `tool_job` 完全没有痕迹**（同一个沙箱、同一套逃逸拦截，却最难事后观察），且队列 worker 的 `contextvars` 为空、载荷也不带身份 ⇒ 即便补审计也只能记命令、记不下人。已在 `execute_tool_job` 的**每个终态**（含取消分支，它会在尾部审计之前向外抛）补记，并让身份**随载荷投递 + 在 worker 恢复**（回归 `tests/test_job_exec_audit.py`）。**执行入口清单已机械化**：`python-engine/tests/test_exec_audit_inventory.py` 枚举 `app/` 下所有起进程的位置并要求逐条归类（已审计 / 由调用方审计 / 显式豁免+理由 / 已知缺口），新增执行路径不归类即失败。**清单里的缺口已清零（2026-10-08）**：plugin audit 从 `api/plugins.py` 的**内联写盘**抽成可复用的 `app/plugins/audit.py::record_plugin`，运行时拉起插件进程的两处（`mcp/client.py` 的 spawn、`skill/manager.py` 的 skill-mcp spawn）已接入（**成功与失败都记**）；顺带把这条流水的 `ts` 从**本地秒级**对齐为 **UTC+毫秒** —— 四条流水本就"由同一套收集/排障流程合并读取"，口径不齐等于跨实例排序会错（回归 `tests/test_plugin_audit.py`） | **中低**（**多副本收集已落地（N4）**：引擎与沙箱服务成批送 `POST /v1/internal/audit/exec`，默认关/不阻断/有界队列带丢弃计数；仍缺统一 schema 与租户级导出） |
| 5 | **插件生态的资产化形态**（与 DSH **不同构**） | DSH 是**进程内 TS 插件树**（Cordis profile patch + 插件面板 + HMR，已跑起 `dsh-synapse` / `dsh-im`） | Chiron 走 **MCP 桥 + 多租户隔离**：Go `internal/api/plugin_handler.go`（List / Install / Uninstall / Update / Test）+ 前端 `PluginsView.vue`（`/v1/plugins`）；引擎 `app/plugins/{store,pool,owner_lease,broker_proxy,extensions}.py` —— per-user 配置隔离、连接池按**配置指纹**共享、`SOURCE_MCP` 工具注册与 owner 过滤；技能侧 6 个 `SKILL.md` 以 `scope=builtin` 接线。**生命周期已钉**：用户**显式停用** ⇒ 工具注销 + 本地连接关闭（`python-engine/tests/test_plugins.py`） | **中**（差距在**形态**而非功能：没有进程内插件的加载 / HMR / 面板扩展点。属**产品取向**，不是缺陷 —— 进程内执行与多租户隔离本身是冲突的） |
| 6 | **服务端沙箱：多租户不可信代码隔离** | 其沙箱目标是保护用户本机，不面对"租户 A 攻击平台" | A 层实现：`tools/{code_guard,sandbox,run_code,_sandbox_worker}.py` + `fs_guard.py` / `ssrf.py` / `exec_audit.py`（静态 AST + 运行时 builtins/import 白名单 + RLIMIT + 审计；技能生成与 `run_code` 共用同一守卫）。**三处实证逃逸已修（2026-10-08）**：① `asyncio` 在白名单里而它夹带 `asyncio.subprocess` ⇒ 可 `create_subprocess_exec` 起进程，根因是**两层都只比较模块名根段**，使 `DANGEROUS_MODULES` 里的点分条目（`asyncio.tasks` / `http.client`）从未生效，已改为**逐级前缀**判定（静态 + 运行时），回归 `tests/test_code_guard_submodule_escape.py`；② `ssrf.py` 只比 IPv6 黑名单，整体放行 `http://[::ffff:169.254.169.254]/`（IPv4-mapped，落点=云元数据）与 6to4/Teredo 同类写法，已改为取出**内嵌 IPv4** 按同一策略递归判定，回归 `tests/test_ssrf.py`；③ 插件命令白名单**只比 basename**，`../../tmp/npx` / `..\..\tmp\npx` / `\\evil-host\share\npx`（UNC）均命中 ⇒ 可执行攻击者放置或远程共享上的同名二进制（Python 侧 docstring 声称拦这些、代码从未实现），已两侧同步为"裸 basename 或绝对路径"，回归 `tests/test_ssrf.py` + `internal/api/plugin_command_allowlist_test.go` **B 层（独立沙箱服务）已开工（2026-10-08）**：`sandbox-service/service.py` 落地 —— 把「执行」移出引擎进程（故障/资源/审计隔离），**直接复用**引擎那份 `run_in_sandbox` 基线（不存在第二份名单可漂移），令牌 fail-closed，被拦下/超时映射为非零退出码，身份随请求带上并在服务侧恢复（否则审计只能记命令、记不下人）。**剩余在代码之外**：独立容器部署、与引擎**共享工作区卷**、真实部署下的分流断言与审计收集。**接线已可测**：`SANDBOX_BACKEND=service` 的分流逻辑抽成 `_apply_sandbox_service_branch()`，4 条分支各有用例（默认恒等 / 白名单租户才走 / **空白名单谁都不走** / 默认后端不支持执行时记 warning 不包装）。**跨进程也已验证**：`test_sandbox_service_multiprocess.py` 把服务起成独立 uvicorn 进程，引擎侧客户端交给它执行，断言命令真在服务进程里跑、服务进程写下带身份的审计、白名单外租户留在引擎进程。⚠ 仍要按"**提高门槛**"读：它解决的是隔离形态，不是内核级强隔离 | **高**（DSH 结构上不可比 ⇒ 做到即"不同物种"；B 层部署形态与真实验证仍待做） |

**判据纪律**：与 `vendor/规划.md` §3.5 一致 —— "超越"= **对外可核实的行为**，不是"有对应模块"。
每条切口落地时必须给出：可复现命令 + 期望输出 + 反例（未做时会怎样）。

## 3. 不确定 / 未验证（不要当成事实引用）

1. npm 元数据不可取（403）；GitHub 仓库未直接读取（策略拦截），monorepo 结构仅有 `repository` 字段为证。
2. app.asar：**已能解包**（自写读取器，见 §4）。**取证强度已升级（2026-10-09）**：不再只看包名 / description ——
   已把 **289 个一方包**（`@deepseek-ai/*`，3,664 个条目）的**声明文本全部读出**（`README.md` + `package.json`，
   **595 个文件**）并据此核对能力。**取证强度再升一级（2026-10-09，实现源码）**：已按需读出 `lib/*.js`
   **实现源码**（自写 asar 读取器，例：沙箱三件套 `dsh-sandbox-policy` / `dsh-fs-sandbox` / `dsh-bash-sandbox`，
   见第 3 条）。**仍未运行 DSH** ⇒ 判定强度 = **实现源码**（高于"包内声明文本"），
   但**运行时行为**（HMR、插件热卸载、真实隔离效果）仍未验证。
3. **从未运行 DSH**；其中**"沙箱是否真 fail-closed"与"fork `atSeq` 语义"均已由实现源码核实（2026-10-09）**，
   **HMR、插件热卸载仍未实测**。
   **沙箱 fail-closed —— 已核实（实现源码级，`app.asar` 内 `lib/index.js`）**：
   ① **默认模式就是最严的那档**：`SandboxPolicyService.Config.mode` 默认值 = **`read-only`**
   （`dsh-sandbox-policy/lib/index.js:97-102`）；优先级为
   `已批准的显式 mode ?? 会话日志里最后一个 sandbox/mode 事件 ?? 部署默认`（`:141-148`）；
   ② **`read-only` 拒绝每一次写**：`dsh-fs-sandbox/lib/index.js:157` 抛结构化 `FS_SANDBOX_DENIED`；
   `workspace-write` 只允许工作区根内（`:164`），且按**精确目标**校验（注释点明为避开
   check-here-write-there TOCTOU，`:143-149`）；
   ③ **沙箱跑不起来时绝不放行**：bash 侧 runner 失败即抛 `SandboxUnavailableError`
   （`dsh-bash-sandbox/lib/index.js:105`），**不会静默退回不受限执行**；
   ④ **模式串校验也是 fail-closed**：未知 mode 触发包内不变量失败（`dsh-sandbox-policy/lib/invariant.js:37`）；
   ⑤ **它自己写明了边界**（值得学的诚实）：文件沙箱"**不是内核边界** —— 那些操作是 seam 自己的
   （open / rename）……对不受信**代码**的内核级隔离是 `ctx.shell` 的职责"（`dsh-fs-sandbox/lib/index.js:78-81`）。
   **fork `atSeq` 语义 —— 已核实（实现源码级）**：
   ① **`atSeq` = 事件序列的"含端切点"**：*"Fork copies the **exact inclusive event prefix** selected by `atSeq`,
   **including a cut inside an open turn**"*（`dsh-api-session-controller/README.md:57`）；
   ② **显式 `atSeq`** ⇒ 就在那个 seq 切；**省略时** ⇒ 默认取**"最近一个已完成回合的前缀"**
   （*"Resolve the omitted-`atSeq` default to the latest completed-turn prefix, including standalone events
   before the next turn begins"*，`lib/types/commands.js:70-73`；`:199-203` 复述同一口径）；
   ③ **开放切点会被补上合成的 fork 收尾**（*"An open cut receives synthetic fork closers"*）——
   实现见 `dsh-session/lib/types/fork.js` 的 `buildForkSeed(events, boundary)`：拷 `slice(0, boundary+1)`、
   追加带 `{inherited: true}` 的 `session/end-seed`、再用 `openTurnClosers(prefix, {kind:'forked'})` **闭合未收尾的
   步骤/回合**；且**调用方必须校验该边界是"存在的连续事件 seq"**，构造时**先快照**借来的事件（fail-closed）；
   ④ **子 agent fork 后端用的正是"默认口径"**：`dsh-subagent-fork-in-process` 取
   `completedTurnPrefix()`（到最后一个 `turn/end` 为止）作为种子，并写明**为什么排除当前回合** ——
   *"the current tool-call turn is **unbalanced and cannot be replayed as a valid child session**"*；
   **没有任何已完成回合时种子为空 ⇒ 子会话从零开始**（不是错误）。
   **对照 Chiron**：本项目对应物是 `sessions.branch_from_seq` —— 但**计的不是同一种东西**：
   DSH 的 `atSeq` 是**事件序列（seq）**的含端切点，Chiron 的 `branch_from_seq` 是
   **消息序号（1 基、含该条）**的含端切点（`docs/session-map-branch-design.md:27`）⇒
   **"含端切点"这一层口径一致**，差别在**被切的单位**（事件流 vs 消息表）。
   值得在实现分叉续跑时对齐的是 DSH 那两条默认语义：**开放切点的合成收尾**与
   **省略时取"最近已完成回合"**（见 `vendor/规划.md` §3.5 R5）。
4. 多租户/计费：**口径已细化（2026-10-09，读包内声明文本后）** ——
   **① 无多租户（证强）**：全部 289 个一方包里 `tenant` / `multi-tenant` **只命中 1 处**，且原文是"**未决定**"：
   `dsh-ptc-runtime` —— *"A container-class backend would provide a hard multi-tenant boundary for both code and
   shell execution; **nothing is decided** beyond the well-known `isolation` value."* ⇒ 不是"没找到"，
   是**它自己写着还没决定**。
   **② 有面向个人的 Platform 计费面（原"未发现证据"须修口径）**：`dsh-client-ui-settings-account` 的 description
   逐字为 *"**Manage DeepSeek login and open Platform billing pages**"*；其 README 记有**登录 / 余额（充值资金与赠送额度
   分列）/ 充值 top-up / 配额通知（`ACCOUNT_QUOTA`，Cancel · Top up）/ Platform usage 页**；
   `dsh-api-account-controller` 暴露 `getProfile` / `getBalance` / `getUnnotifiedBonuses` / `ackBonusNotified` / `signOut`；
   凭据获取在 `dsh-deepseek-account-platform`（description 逐字：*"**Authorize DeepSeek accounts through browser PKCE**"*）——
   `getProfile` / `getBalance` 把 grant 发到 `platformOrigin` 的 `GET /auth-api/v0/users/current` 与
   `GET /api/v0/users/get_user_summary` ⇒ 这是**对 DeepSeek 托管平台**的集成，**不是自建账号体系**。
   **但那不是租户级计量**（无 org / seat / tenant 维度），且 `dsh-token-meter` **自陈不是计费**
   （*"Occupancy is a reference figure, **not a billing record**"*；*"reach for a provider tokenizer when a deployment
   needs exact **billing-grade** counts"*）。
   ⇒ **Chiron 的"租户级配额 / 计费"差距仍然成立**，但"**DSH 完全没有账务面**"这句话**不能再说**。
5. **browser-trust 围栏**已由实现源码核实（2026-10-09）；**KV-cache 前缀复用**仍只有声明文本级证据。
   **browser-trust —— 已核实（实现源码级）**：它**不只是** `dsh-synapse` 插件侧声明，**DSH 核心自己就实现了**：
   ① **鉴权之前先过 `api-request-trust`**：`Host` 必须**是 loopback 或命中 `trustedHosts`**（按 `host:port` 精确匹配），
   见 `dsh-client-connection/README.md` 的「Browser authentication and request trust」一节（`:36-48`）；
   ② **没有"方法级 loopback 豁免"**：*"Every Host RPC method and WebSocket stream requires one browser session;
   there is no method-specific loopback tier."*；
   ③ **拒绝升级时不把 socket 交给 `ws`**：`rejectRemoteStreamUpgrade(socket, status)` 的注释即
   *"Reject an upgrade **without transferring socket ownership to ws**"*，状态为
   **401（鉴权）或 403（browser-trust）**（`dsh-api-gateway/src/stream-server.ts:428-444`）⇒ **fail-closed**；
   ④ 每次启动**现签一个随机 launch token**，cookie 签名密钥是 owner-scoped 的
   `client-connection/browser-session` 凭据记录（落在 `$DSH_HOME`）；**每个被放行的请求代表一个 Peer（操作者）**，
   由 `ctx.connection.operator` 这个 `PeerScope` 持有连接生命周期。
   **KV-cache 前缀复用 —— 已核实（实现源码级），且必须分成两半说（2026-10-09）**：
   ① **断点设置（"复用"本身）不在 DSH 一方代码里**：`cache_control` 的 122 处命中**全部**落在
   厂商 SDK（`@anthropic-ai/sdk` 占 97 处 · `openai`）与 **`@earendil-works/pi-ai`** —— 而 pi-ai 正是
   **DSH 的 provider 层依赖**（`dsh-llm-pi-ai`）；pi-ai 的 `dist/api/anthropic-messages.js:823` 会按条件带上
   `cache_control`（`...(cacheControl ? { cache_control: cacheControl } : {})`）⇒ **DSH 通过 provider 层获得前缀复用能力，
   但自己不写断点**。
   ② **用量读取与展示是 DSH 一方包自己做的**：`dsh-token-meter/lib/types/turn-usage.js:22-35` 的 `normalizeUsage`
   解构 `cacheReadTokens` / `cacheWriteTokens`、**校验其为合法计数**（非法即返回 `undefined`，fail-closed），
   并算 `knownPrompt = inputTokens + cacheReadTokens + cacheWriteTokens` —— 即**缓存感知的 prompt 总量**
   （它知道厂商的 `inputTokens` 不含被缓存的那些）；适配在 `dsh-llm-pi-ai` / `dsh-llm-deepseek`，
   展示在 `dsh-client-ui-chat`（33 处）与 `dsh-client-ui-trajectory`（23 处）。
   ⇒ **口径修正**：不再是"声明文本里未见、所以不能说有" —— 而是
   "**一方代码不设缓存断点（交给 provider 层），但自己实现了缓存感知的用量统计与展示**"。
6. `$DSH_HOME` 下第三方插件目录当前为空（安装中），仅有 `package.json`/lockfile 声明。
7. **原文更正（2026-10-08）**：本文件早期版本照抄了 README 的"`market/skills/` 未被运行时读取"，
    实测为**错**（技能已接线，只是容器里缺席）。教训与 `vendor/规划.md` §4"配置里有 ≠ 运行期生效"
    同类：**文档里的话不是事实**，引用前必须跑一遍。
8. **原文更正（2026-10-08，方向相反的那种）**：本文件早期把「思考通道内联在 `text`」列为差距 ——
   实测 **A1 早已落地**：`app/agent/runtime.py` 直接 `yield AgentEvent(type="thinking")`，
   前端有 `ReasoningBlock` 与 `onThinkingChunk`，契约写在 [聊天记录契约](transcript-contract.md)。
   ⇒ 切口 #3 收窄为**阶段 + 用量**两条（阶段后经核查**决定不做**，见切口 #3）。**"差距分析写下的差距" ≠ "今天还存在的差距"** ——
   分析文档不会自己更新，**引用差距前同样必须跑一遍现状**（否则会把已有能力再实现一遍）。
9. **原文更正（2026-10-08）**：切口 #5 的「现有」列引用了 **`app/plugin_runner.py` —— 该文件不存在**。
   实测插件链路的真实位置见切口 #5（Go `plugin_handler.go` + 前端 `PluginsView.vue` + 引擎 `app/plugins/*`）。
   **"分析里写的文件名"也要核**：连路径都没核过的差距，其"中/高"评级同样不可信。

10. **复核（2026-10-08，v0.2.0-rc.2）**：写了一个 asar 读取器把 `app.asar` 解开索引、枚举全部 **287 个依赖**（方法见 §4）。结论分三类：
    **① 原判断被证强**：仍无 tenant/org/quota/billing/metering 包 ⇒ 多租户与计费差距成立（切口 #2）；沙箱目标是本机（`dsh-sandbox-windows-acl` / `dsh-fs-sandbox`）⇒ 切口 #6 的"威胁模型相反"成立。
    **② 原判断被证弱（需修口径）**：DSH 不是"只有沙箱"，而是一层**策略插件框架**（`dsh-permission-presets` / `dsh-user-approval` / `dsh-fs-observation-policy` / `dsh-output-retention` / `dsh-spill-policy` / `dsh-tool-call-timeout-policy`）；插件生态除 profile/HMR 外还有**钩子协议**（`dsh-hook-protocol` + Claude Code/Codex 兼容）与 `dsh-webhook`。
    **③ 新发现（会改优先级）**：**`dsh-session-checkpoint-policy`** —— DSH 已有会话 checkpoint/恢复能力，而 Chiron 的 L4-1 还卡在"待用户确认 5 问" ⇒ **L4-1 属"追平"，不能当差异化卖点**，已同步进路线图 §2 与 §1 对照表。
    ⇒ 教训：**"未发现证据"必须写清取证强度**（目录索引 / 依赖枚举 / 源码 / 运行时是四个量级）；本轮就是把 #2 从"未发现"升到"枚举后确认"，同时把两处被低估的地方补上。

11. **原文更正（2026-10-08）**：Fork / 分支 一行原写 `internal/session/branch_summary.go`、`manager.go`**（分支语义待核实）**并判"**DSH 更明确**" —— 实测 **Chiron 早已实现且有测试**：`manager_branch.go` 的 `planBranch`（truncate / condense）与 `BranchSession`（一个事务内建血缘会话 + 复制消息窗口 + 时间戳重排）· `sessions` 的血缘/分叉列（含索引）· `POST /v1/conversations/{id}/branch` 与 `/fork` · 前端地图连线 · 设计文档 [会话地图与分支设计](session-map-branch-design.md)；`TestPlanBranch`、`TestBranchHandlerParameterValidation` 通过（2026-10-08 实测）。
    ⇒ 该行已改为"**相当**"（Chiron 另有压缩式分支）。**教训**：**连自己文档里写着"待核实"的格子也必须真去核** —— 留一个未核实的判断在交付物里，它就会以"差距"的身份被引用（本文件第 8、9 条是同一类错误的另外两例）。

12. **复核（2026-10-09，声明文本级）**：把 289 个一方包的 `README.md` + `package.json` **全部读出**后按关键词核对。
    **结论**：切口 #1「单进程本地」与 #2「无多租户」**证强**（`tenant` 全仓仅 1 处，且原文写着 *"nothing is decided"*），
    但**计费口径须修**（见第 4 条：**有**面向个人的 Platform 计费面，**无**租户级计量）。
    **⚠ 方法学陷阱（本轮踩到并记下）**：**关键词命中数不是能力证据** —— `kv cache` 命中 **269 / 289 个包**，
    因为**每个包的 README 都有同款模板章节**（"KV Cache 影响"），而不是 269 个包都实现了前缀复用；
    同理 `subscription`（28）/ `seat`（35）多为**事件订阅**与 **CSS 子串**的误伤。
    ⇒ 取证必须**看命中处的上下文**，并区分**模板章节**与**实质声明**；只报命中数的盘点不可引用。

13. **读-写策略的口径差（2026-10-09，声明文本级）**：`dsh-fs-observation-policy` 把 **read-before-edit 当硬要求**
    （*"Reading a missing path authorizes guarded creation"*），而 Chiron 的 `app/tools/fs_guard.py` 自陈
    *"**从未被 read 过的文件不受限制（兼容旧行为）**"* ⇒ **从未读过的文件可以直接被 `write`/`edit` 覆盖**，
    这在 DSH 侧是被拦下的。**是否收紧属产品决定**，但**机制已就绪（2026-10-09）**：开关
    `settings.fs_require_observation`（**默认关 ⇒ 零行为变化**）；打开后"**已存在但从未读过**"的文件
    也拒绝（要求先 `read_file`），用例见 `tests/test_fs_guard.py::TestStrictMode`（含"默认关 ⇒ 行为逐字不变"）。
    **仍未实现的两处 DSH 语义**（留给产品决定）：① DSH 的 *"读一个不存在的路径 = 授权受保护的创建"*
    —— Chiron 对不存在的路径**两种模式都放行**；② DSH 的**并发创建**保护（读完缺失路径后别人建了它 ⇒ 拒绝）。
    **附带一处形态差**：DSH 有统一的 per-tool deadline 包装（超时 → `TOOL_TIMEOUT`），Chiron 的超时
    **分散在各工具**（沙箱 / 后台 job / hooks）；但 DSH 自陈该包装 *"cannot hard-stop downstream work"*，
    所以这不是"更安全"，只是**错误口径更统一**。

## 4. 复现取证的方法（只读）

```powershell
# DSH 安装形态
Get-Content "$env:ProgramFiles(x86)\deepseek-harness\version"
Get-Content "$env:ProgramFiles(x86)\deepseek-harness\resources\runtime\cli\bin\dsh.cmd"

# app.asar 解包（只读）：实测布局 = UInt32LE@0(=4) | UInt32LE@4 | UInt32LE@8 | UInt32LE@12(=JSON 长度)
#   JSON 索引从 **offset 16** 开始；文件内容 dataStart = 16 + UInt32LE@12（等价于 8 + UInt32LE@4，两者实测一致）
#   解析后遍历 JSON 索引，按 name@version 枚举依赖做能力盘点（本轮 = 287 个依赖）
#   注意：`resources\app.asar.unpacked\dsh` 只有 node_modules，源码在 asar 内，不要把它当成源码树

# DSH 状态根（不要输出来自 .credentials.yaml 的值）
Get-ChildItem "$env:DSH_HOME" -Force
Get-Content "$env:DSH_HOME\storages\workspace.json"
```
