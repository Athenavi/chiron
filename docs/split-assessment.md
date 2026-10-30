# 巨型文件拆分评估

**口径**：这是一份**边界与契约**清单，不是工时估算。凡是没有实测支撑的数字都标「待实测」；凡是"拆了会改变行为"的地方都写清为什么暂不动。

**为什么这份文档曾经不存在**：提交 `beb7bbb`（2026-09-28）的信息写着「新增 `docs/split-assessment.md`」，但该提交 `--stat` 只改了 `docs/development-roadmap.md` 一个文件，`git log --all -- docs/split-assessment.md` 返回 0 条 —— 换句话说，路线图、wiki 与两处源码注释共 7 处引用都指向一个**从未被提交过**的文件。本文档按当前代码实测重写，以保证那些引用（含 §2.3、§2.5 两个具体节号）确实有落点。

## 1. 扫描口径

**排除**（它们会霸占体积榜且拆分无收益）：

- `vendor/` —— 第三方代码；
- `frontend-vue/src/locales/` —— 文案数据，按域切分已足够；
- `migrations/versions/0001_authoritative_baseline.py` —— **已发布迁移不可变**（仓库硬约束：只能追加 revision）；
- `shared/models/`、`**/__pycache__/`、构建产物与生成物。

**统计口径**：行数用 `wc -l`；Go 顶层符号为 `^func |^type ` 的行数；Python 为 `^def |^class |^async def ` 的行数；Vue 只统计整文件行数。**以下数字实测于 2026-10-30**，复算命令见 §6。

## 2. 逐文件边界与契约

### 2.1 已拆出的产出（现状复核）

历史批次声称的行数与本次实测对照如下。**旧数字普遍偏小**，因此任何"接着上一批继续"的计划都必须先重新实测：

| 文件（拆分前） | 文档旧记 | 实测现状 | 产出 |
|---|---|---|---|
| `internal/api/gateway_router.go` | 1254 → 442 | **591** 行 | 12 个 `routes_*.go`（不含测试），其中 `routes_agent.go` 354 行为最大 |
| `internal/api/mail_handler.go` | 1488 → 695 | **695** 行 | `mail_handlers_config.go` 303 · `mail_handlers_auth.go` 282 · `mail_handlers_reset.go` 214 |
| `python-engine/app/main.py` | 2170 → 2061 | **2452** 行 | `app/deps.py` **234** 行；启动步骤拆成 **20 个** `_start_*` / `_bind_*` 函数 |
| `python-engine/app/agent/runtime.py` | 3370 → 3198 | **4498** 行 | `runtime_types.py` **252** 行 + `app/agent/compaction.py` **418** 行 |

### 2.2 Go 侧候选（`internal/api` 与相邻包）

按行数排序，附顶层符号数。符号多而分散的文件更适合按"职责簇"切；符号少而单个巨大的（如 `submit_handler.go` 17 个符号 / 935 行）往往是少数几个超长函数，**切函数体比切文件风险更高**。

| 文件 | 行数 | 顶层符号 | 建议边界 |
|---|---|---|---|
| `internal/api/sso_handler.go` | 1395 | 35 | 按 `provider` 切：OIDC 发现/回调、绑定/解绑、以及各提供方的差异分支 |
| `internal/api/ent_identity_handler.go` | 1168 | 55 | 按资源切：用户 / 角色 / 群组 / 租户 —— 语义上已经是四个子域 |
| `internal/api/admin_ops.go` | 1145 | 45 | 按运维动作切，注意其中的跨表写要留在同一事务 |
| `internal/api/ent_costcenter_handler.go` | 1135 | 45 | `quota_pools` / `allocations` / `usage` 三簇（已有 `pgEntCostStore` 可作天然分界） |
| `internal/api/billing_handler.go` | 1015 | 29 | 订单 / 余额 / 用量 / 套餐与权益 —— 路由已按此分组 |
| `internal/api/submit_handler.go` | 935 | 17 | **符号极少但很长**，多为一次 SSE 回合的主流程；拆分即改动时序，暂不动 |
| `internal/auth/mail.go` | 904 | 46 | 按"发送器实现"与"模板/配置"分；`MailSender` 接口是天然契约 |

**通用验收**：同包纯移动，`go build`/`go vet` 零错误，全量 `go test` 绿，且**函数体逐字未变**（可用 `git diff -M --color-moved` 辅助确认）。

### 2.3 Python：`main.py` 的 `lifespan` 与 `locals()` 陷阱

**现状（实测）**：`lifespan` 定义在 `app/main.py:124`，启动步骤已拆成 20 个独立函数（`_start_file_backend` 292、`_start_queue_worker` 380、`_start_memory_service` 407、`_start_gateway` 499、`_start_redis` 753、`_start_postgres` 804、`_start_mcp_pool` 826、`_start_subagent_governance` 845、`_start_engine_registry` 956、`_start_instance_liveness` 983、`_start_run_reconciler` 1011 等）。

**为什么这一节值得单独立项**：关闭段曾经靠 `'x' in locals()` 探测"某个句柄有没有被赋值过"，从而决定要不要清理。这种写法把**清理是否执行**绑在**词法作用域**上 —— 任何把启动段抽成函数的纯移动重构，都会让探测恒为 False，于是清理被静默跳过：进程退出时连接不关、任务不取消，而且**没有任何日志**。

**当前约束（机械护栏）**：`python-engine/tests/test_shutdown_lifespan.py::test_lifespan_does_not_probe_locals_anymore` 用 AST 解析断言 `lifespan` 函数体内**不得出现** `locals()` 调用。句柄必须显式初始化（`app/main.py:127-130` 的 `_metrics_task` / `_retention_task` 就是范例）。

**续拆时的硬要求**：

1. 句柄一律在 `lifespan` 顶部**显式初始化为 `None`**，不靠分支赋值 + `locals()` 探测；
2. 每个新 `_start_*` 函数通过**返回值**或**显式参数**交出句柄，不写模块级全局；
3. 抽函数后必须核对**异常语义**：原实现里"某步失败继续/中止"的差异不能因为搬进函数而改变；
4. 每批跑 `python-engine` 的关闭路径测试（`test_shutdown_lifespan.py`），而不只是启动路径。

### 2.4 Python：其余大类

| 文件 | 行数 | 顶层符号 | 结论 |
|---|---|---|---|
| `app/agent/runtime.py` | 4498 | 27 | `AgentRuntime` 本体高度内聚在"一次 run 的状态机"上。**拆它必然触碰行为**（取消传播、事件顺序、token 记账都跨方法）⇒ **暂不动**；纯函数与数据类型已分别拆到 `compaction.py` / `runtime_types.py`。若将来要拆，先补 run 级别的事件序列快照测试 |
| `app/main.py` | 2452 | 49 | 见 §2.3；剩余的启动/装配代码可继续按"基础设施 \| 业务域"聚拢 |
| `app/memory/service.py` | 1742 | 11 | **符号少、单类巨大**（同 `runtime.py` 一类问题）。拆类内部等于改行为，暂不动 |
| `app/queue/worker.py` | 1100 | 4 | 同上，单类 4 个符号撑起 1100 行；拆解前需要先有 worker 生命周期测试 |

**共同判据**：文件大本身不是问题，"一个类装了多个可独立演进的状态机"才是。上表后三行都属于**行为高风险**，不列入可执行的拆分批次。

### 2.5 前端：`ChatView.vue` 与"没有测试网就不许动"的前置条件

**现状（实测）**：`src/views/ChatView.vue` **3966 行**，其中 `<script setup>` 占 1–2923、`<template>` 2925–3647、`<style scoped>` 3649 行起 —— **脚本部分占了四分之三**，这才是它难拆的真正原因（模板只是长，脚本是逻辑）。

| 文件 | 行数 |
|---|---|
| `src/views/ChatView.vue` | 3966 |
| `src/views/MediaView.vue` | 1842 |
| `src/components/chat/ChatInput.vue` | 1628 |
| `src/views/WorkflowView.vue` | 1364 |
| `src/components/chat/ChatSidePanel.vue` | 1228 |
| `src/components/AppLayout.vue` | 1002 |

**前置条件（这就是 `ChatView.spec.ts` 头注释引用的 §2.5）**：`ChatView.vue` 此前**零测试**，在全仓最大文件上做重构等于裸奔。因此先补一层**冒烟网**——它不覆盖业务，只钉住"能挂载 / 初始加载会跑 / 卸载不炸"，重子组件全部打桩、`../../api` 用永远返回空列表的替身（不联网）。

现规模：`ChatView.spec.ts` **3 项** + `ChatView.interaction.spec.ts` **17 项** = 20 项。**这层网存在之后**，才允许按"会话流 / 输入编排 / 附件与媒体 / 分享与分支"抽 composable。

**抽取顺序建议**：先抽**纯计算**（消息合并、锚点换算、围栏解析），它们不碰组件生命周期；再抽**带副作用的 composable**（SSE 连接、断线重连）。反过来做会把时序问题混进重构里，出问题时分不清是拆错还是原本就错。

**已完成的同类参考**：`useWorkflowExecution` / `useWorkflowTemplates` / `useChatShare` / `useWorkflowCanvas` / `useWorkflowPersistence`。

## 3. 明确不做

| 不做的事 | 理由 |
|---|---|
| 改 `migrations/versions/0001_authoritative_baseline.py` | 已发布迁移不可变，只能追加 revision；改它会破坏存量库的可重放性 |
| 拆 `locales/` 下的文案文件 | 那是数据不是逻辑，按域切分已经够用 |
| 拆 `AgentRuntime` / `MemoryService` / `QueueWorker` 的**类内部** | 见 §2.4：拆即改行为，需要独立的 run 级快照测试作为前置 |
| 为凑行数而机械切文件 | 拆分的目标是降低阅读与修改成本，不是让体积榜变好看 |

## 4. 批次与验收

| 批次 | 内容 | 验收 |
|---|---|---|
| A | `internal/api` 的 Go 候选（§2.2），从符号多、边界清晰的两个开始（`ent_identity_handler.go`、`ent_costcenter_handler.go`） | 同包纯移动；`go build`/`vet` 零错误；全量 `go test` 绿；函数体逐字未变 |
| B | `python-engine` 的 `main.py` 续拆（§2.3） | 句柄显式传递、不出现 `locals()` 探测；`test_shutdown_lifespan.py` 必须过；启动顺序测试不变 |
| C | 前端纯计算部分抽 composable（§2.5） | `vue-tsc -b` 零错误；`vitest` 全绿；ChatView 冒烟网 20 项不受影响 |
| D | 前端带副作用 composable（SSE / 重连） | 同上，且**单独一批**，不与 C 混提 |

**每批的通用验收**：编码检查（`scripts/check_source_encoding.py`）通过；提交信息说明本批的边界与"为什么这不算行为变更"。

### 4.1 批次 A 实际进度（2026-10-30）

已完成 6 个文件、**2043 行逐字未变外移**（每块都用「按函数签名 + 大括号平衡提取，与 `git show HEAD` 逐行比对」校验）：

| 原文件 | 拆前 → 拆后 | 外移行数 | 新文件（内容） |
|---|---|---|---|
| `internal/api/ent_costcenter_handler.go` | 1135 → 725 | 406 | `ent_costcenter_store_quota.go`（配额池/分配/用量查询）· `ent_costcenter_store_cost.go`（CostSummary/GroupCost） |
| `internal/api/ent_identity_handler.go` | 1168 → 538 | 551 | `ent_identity_roles.go`（角色 CRUD）· `ent_identity_groups.go`（群组 CRUD + 成员/角色授予） |
| `internal/api/sso_handler.go` | 1395 → 888 | 482 | `sso_providers_admin.go`（管理端 IdP 配置 + 配置指引） |
| `internal/api/billing_handler.go` | 1015 → 815 | 185 | `billing_paypal.go`（PayPal 渠道全链路） |
| `internal/api/admin_ops.go` | 1145 → 950 | 167 | `admin_ops_redis.go`（Redis 慢日志归一化 + FLUSHALL） |
| `internal/auth/mail.go` | 904 → 614 | 252 | `mail_mime.go`（MIME 报文组装，全为纯函数） |
| `internal/engine/python_client.go` | 817 → 631 | 183 | `python_client_addr.go`（地址路由与故障转移：`normalizeURLs`/`snapshot`/`SetAddresses`/`markFailure`/`markSuccess`/`pickAddress`/`addressFor`/`sortUnique`） |
| `internal/api/plugin_handler.go` | 837 → 693 | 143 | `plugin_validation.go`（命令与参数校验，全为纯函数：`allowedPluginCommands`/`pluginCommandPathAllowed`/`isASCIILetter`/`validatePluginCommandShape`/`checkOperatorAllowlist`/`checkPluginExecutionAllowed`/`checkPluginArgsAllowed`/`maskSensitiveEnv`） |
| `internal/api/agents.go` | 922 → 670 | 250 | `agent_context.go`（Agent 配置解析与上下文装配：`jsonOrEmptyArray`/`agentSkillNames`/`agentContextPayload`/`resolveAgentContext`/`applyAgentBindings`/`loadExpertPayloads`/`llmString` 等 18 个纯逻辑符号） |
| `internal/api/rpa_ws.go` | 860 → 708 | 152 | `rpa_registry.go`（Redis 注册中心 + 跨实例桥接：`rpaCmdEnvelope`/`rpaResEnvelope`/`crossInstanceLoop`/`handleRemoteCmd`/`handleRemoteRes`/`putRegistry`/`refreshRegistry`/`registryOwner`/`dropRegistry`/`registryUser`/`registryIDFromKey`） |
| `internal/api/sso_handler.go` | 888 → **559** | **326** | `sso_identity.go`（三方身份 ↔ 本地账号的绑定闭环 + 用户自助：`handleBindCallback`/`ListIdentities`/`DeleteIdentity`/`setPasswordRequest`/`SetPassword`/`findIdentityUser`/`provisionAndBind`/`resolveRole`） |
| `internal/api/sms_handler.go` | 739 → **525** | **214** | `sms_config.go`（短信通道配置读取/回显/更新：`smsConfigRow`/`GetConfig`/`configResponse`/`updateSmsConfigRequest`/`UpdateConfig`；`loadConfig` 被发码链路共用，按依赖判据留在原文件） |
| `internal/api/mail_handler.go` | 695 → **569** | **126** | `mail_config_rules.go`（邮件配置行的边界校验与补丁应用，纯函数：`validateMailConfigBounds`/`applyMailConfigPatch`/`isHTTPURL`/`derefString`/`ttlMinutes`/`resetTTLSeconds`） |
| `internal/api/billing_handler.go`（第二个文件） | 815 → **632** | **182** | `billing_usage.go`（用量分摊查询：`usageGroupColumn`/`parseUsageParams`/`GetUsage`/`getBreakdownUsage` —— 含 P0-1 的分组维度与时间区间解析） |
| `internal/api/ent_costcenter_handler.go`（第二个文件） | 725 → **487** | **238** | `ent_costcenter_quota_crud.go`（配额池的增删改查 HTTP 入口：`ListQuotas`/`CreateQuota`/`GetQuota`/`UpdateQuota`/`DeleteQuota`） |

**一条可复用的拆分判据**（本轮验证有效）：优先按「**谁依赖谁**」而不是按「行数均分」切 —— 例如 `normalizeProviderInput`/`defaultStr` 只被管理端配置调用，就随配置走；而 `getProvider`/`exchangerFor` 被登录链路调用，必须留在原文件。按依赖切永远不会出现「拆完两边都缺一半」。

**踩过的坑（下轮直接照做，别再试错）**：
1. **不要用硬编码行号做校验** —— `HEAD` 版本与工作区的行号会漂移，脚本会 ABORT。改按符号提取。
2. **import 必须按实测计数双向修** —— 每一块都出现过：新文件预置 import 漏了某个包（`enterprise`、`slog`、`net/mail` 等），同时原文件因外移多出失效导入（`pgx`、`sort`、`strconv`、`context`/`json`/`io`…）。做法是先 `rg -c "<pkg>\." <file>` 统计两边，再改。
3. **用脚本按符号边界切片，不要手工复制** —— 几百行手工搬运必然出错；脚本还能顺带做边界断言（节注释 + 大括号平衡）。

**剩余候选**（§2.2 已列，未动）：`submit_handler.go`（935 行 / 仅 17 符号 ⇒ **建议跳过**，拆函数体会改时序）。本轮已完成十个：`python_client.go`（817 → 631）、`plugin_handler.go`（837 → 693）、`agents.go`（922 → 670）、`rpa_ws.go`（860 → 708）、`sso_handler.go`（888 → 559）、`sms_handler.go`（739 → 524）、`mail_handler.go`（695 → 568）、`billing_handler.go`（815 → 632）、`ent_costcenter_handler.go`（725 → 487），后两个指其第二个文件。

## 5. 受外部条件阻塞（不列入批次）

| 项 | 缺什么 | 实测结论 |
|---|---|---|
| L4-3 时间列 `timestamp` → `timestamptz` | 生产时区语义抽样 + 可达 PG | `0001` 基线仍有 **98 处** `timestamp without time zone`。风险不是"哪一侧对"，而是**同一列可能混着两种语义**（`NOW()` 跟数据库会话时区、`time.Now()` 跟宿主时区）⇒ 任何单条 `ALTER ... USING AT TIME ZONE 'X'` 都会把一半行移错时刻。必须先在生产跑 `internal/db/tz_diagnostic.go` 对应的只读诊断（已挂在 `cmd/chiron/main.go:428` 的 `warnOnTimezoneMismatch`） |
| S5-(e) 沙箱部署形态 | 独立容器（无 docker socket、仅内网）+ 与引擎共享的工作区卷 | 服务应用已落地（`sandbox-service/service.py` + `app/backends/remote_exec.py` + `SANDBOX_BACKEND` 分流）。**真实部署验证不能靠单测替代** —— 本次会话实测 `docker info` 失败（npipe 不存在），本机无法验证 |

## 6. 数字时效性

**这份文档里的所有行号与行数都会过期。** 复算命令：

```bash
# Go 侧：行数 + 顶层符号数
for f in internal/api/*.go internal/auth/mail.go; do
  case "$f" in *_test.go) continue;; esac
  printf '%s\t%s\t%s\n' "$(wc -l < "$f")" "$(rg -c '^func |^type ' "$f")" "$f"
done | sort -rn | head -15

# Python 侧
for f in python-engine/app/agent/runtime.py python-engine/app/main.py \
         python-engine/app/memory/service.py python-engine/app/queue/worker.py; do
  printf '%s\t%s\t%s\n' "$(wc -l < "$f")" "$(rg -c '^def |^class |^async def ' "$f")" "$f"
done

# 前端（只数行数；Vue 的段内行号用 rg -n '^<script setup|^</script>|^<template>|^</template>|^<style' 取）
wc -l frontend-vue/src/views/ChatView.vue frontend-vue/src/views/MediaView.vue
```

**因此**：任何一批开工前**先重跑上面的命令**，不要照搬本文档的数字 —— 上一版就是被这么拖到错的（见文首说明）。
