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

## 1. 逐层对照（只列"同一层"）

| 维度 | DSH | Chiron | 方向 |
|---|---|---|---|
| Agent 循环 | `dsh-agent-loop` 插件 + Cordis seam | `app/agent/runtime.py`、`loop.py`、`loop_guard.py`、`task_budget.py` | 相当 |
| 事件契约 | session 事件（`user/message` `assistant/message` `tool/call` `turn/end`…），**内部 TS 词汇表**，无对外稳定承诺 | SSE ~13 种；**思考已有独立 `thinking` 事件**（`app/agent/runtime.py` 的 A1 分支，契约见 [聊天记录契约](transcript-contract.md) §"两个来源各走各的"），但**用量只在 `done` 里给一次累计值**（无每回合 usage/计价），**无 `Phase`/`TurnStarted`/`Message` 通道** | DSH 的**边界更清晰**；Chiron **思考已对齐**，缺**阶段**与**用量**两条通道 |
| Fork / 分支 | 原生 durable fork：`parentSession` + `seedLength`/`firstLiveSeq`，可 `atSeq` | `internal/session/branch_summary.go`、`manager.go`（分支语义待核实） | **DSH 更明确** |
| 子 agent | `dsh-subagent*` + 实验性 `dsh-experimental-agent-team`（roster / durable mailbox / shared task DAG） | `app/subagent/`（R1 receipts / R2 写路径仲裁 / R3 结构化结局 / R4 可注入时钟已落地；R5 fork 待定） | 相当（Chiron 有**可核实行为**判据） |
| 多租户 / 计费 | ⛔ 未发现任何租户/配额/账务机制 | JWT + RBAC + 402 计费预检 + 429 配额 + 租户并发池 | **Chiron** |
| 多副本横向扩展 | 文档明确"只跑一个实例" | 跨实例会话运行锁 + Redis Stream 断线重放 + run 亲和 | **Chiron** |
| MCP | `dsh-mcp-client` 单用户直连 | 连接池 + owner lease + `MCP_MAX_*` 预算 + 拒绝指标 | **Chiron** |
| 执行隔离 | OS 级本机限制（bwrap/landlock/Seatbelt/Windows ACL），威胁模型=**保护用户本机** | 白名单 + RLIMIT + AST 守卫 + 审计，威胁模型=**保护平台与其它租户**；`chiron-sandbox/` 为空 | **不可比**（威胁模型相反） |
| 插件 / 技能生态 | Cordis profile patch + 插件面板 + HMR，已跑起 `dsh-synapse`/`dsh-im` | **技能侧已对齐**：`market/skills/` 6 个 SKILL.md 由 `SkillStore` 以 `scope=builtin` 读取（2026-10-08 复核并修好容器缺席问题）；**插件生态**（profile/面板/HMR/第三方插件真跑）仍明显落后 | **DSH 领先（插件侧）** |
| 会话持久化 / 检索 | JSONL(zstd) + 格式 v0→v4 迁移链 + SQLite FTS5 | PG 单一持久层 + Alembic 单 head；会话检索走 PG | 相当 |
| CLI / SDK | `dsh` CLI + **Python SDK(PyPI)** + ACP/JSON-RPC | `cmd/chiron-cli` 有；**无对外 SDK** | **DSH** |
| 可观测性 | OTel + 产品分析（+ 日志默认上传） | OTel 类埋点 + Prometheus/Alertmanager + **不默认上传** | 相当（Chiron 隐私更优） |
| 国际化 | 客户端 locale 包（中文覆盖度未取证） | 三语种 + 缺键护栏 | **Chiron** |

## 2. 可"超越"的切口（按代价/可核实性排序）

| # | 切口 | 为什么 DSH 做不了/不这么做 | Chiron 已有基础 | 代价 |
|---|---|---|---|---|
| 1 | **多副本一致性语义的对外承诺** | 架构上是单进程本地；多写者冲突是写进文档的限制 | 每会话 Redis Stream + `Last-Event-ID` 补发 + 会话运行锁心跳 + 跨实例取消（**广播前校验会话属主**）+ **session→实例归属路由**（`engine:run:*`）。**对外保证已成文**：[多实例部署指南 §9](deployment-multi-instance.md) 新增「多副本语义的对外保证」表（每条附取证与**边界**）；真实 Redis 覆盖见 `internal/broadcast/hub_live_test.go`、`internal/api/session_coord_live_test.go`、`internal/api/session_cancel_test.go`、`internal/engine/run_affinity_test.go` | **低**（缺"两实例端到端"HTTP 级演练） |
| 2 | **MCP 多租户连接预算与 owner lease** | 单用户直连即可，无连接数经济学 | `MCP_POOL_ENABLED`/`MCP_MAX_*`/`MCP_OWNER_LEASE_ENABLED` + `mcp_pool_rejected_total`；**已有实测证据**（`tests/test_mcp_owner_lease.py`，真实 Redis：互斥/CAS/TTL/清单往返/桥往返·报错·超时），对外口径见 [多实例部署指南](deployment-multi-instance.md) §8 | **已闭环**（补测时连带修掉两处 pub/sub 连接生命周期缺陷） |
| 3 | **阶段（Phase）与用量（Usage）独立通道**（思考 ✅ 已落地，见 §1） | session 事件是内部词汇表，无对外契约承诺 | 思考通道已按 A1 落地并有契约（[聊天记录契约](transcript-contract.md)）；**用量只在 `done` 里给整轮累计**，**阶段完全缺失** | **低-中**（动手前先决定："新增事件类型"是否落入 `vendor/规划.md` §6「不改 SSE 协议」的边界） |
| 4 | **租户级全链路审计与可回放证据链** | 只有本机 session log，且默认上传；无"操作者/租户"维度 | 三条 JSONL 流水带 tenant/user/session：`approval_audit.py`（本轮补测并修掉**密钥明文落盘**）、`exec_audit.py`、`hooks/audit.py`；`ts` 已统一为 UTC+毫秒 | **中低**（缺统一 schema 与租户级导出） |
| 5 | **插件生态的可运行资产化**（技能侧已对齐，见 §1） | DSH 已领先且不会替 Chiron 做 | `plugin_runner.py`、前端 Plugins/Skills 视图；技能资产已接线（6 个 `SKILL.md`，容器侧本轮已修） | **中**（是其余切口的**前置**） |
| 6 | **服务端沙箱：多租户不可信代码隔离** | 其沙箱目标是保护用户本机，不面对"租户 A 攻击平台" | `tools/sandbox.py`/`code_guard.py`/`fs_guard.py`/`ssrf.py`/`exec_audit.py` | **高**（但 DSH 结构上不可比 ⇒ 做到即"不同物种"） |

**判据纪律**：与 `vendor/规划.md` §3.5 一致 —— "超越"= **对外可核实的行为**，不是"有对应模块"。
每条切口落地时必须给出：可复现命令 + 期望输出 + 反例（未做时会怎样）。

## 3. 不确定 / 未验证（不要当成事实引用）

1. npm 元数据不可取（403）；GitHub 仓库未直接读取（策略拦截），monorepo 结构仅有 `repository` 字段为证。
2. app.asar **只做了目录索引 + 定向读 `package.json`**，未解包、未读源码 ⇒ 能力判定基于包名与官方 description。
3. **从未运行 DSH**：沙箱是否真 fail-closed、fork `atSeq` 语义、HMR、插件热卸载均未实测。
4. 多租户/计费属"**未发现证据**"，不等于官方声明不支持（可能在未读源码或 `harness.deepseek.com` 后端）。
5. KV-cache 前缀复用、browser-trust 围栏均只有 `dsh-synapse` **插件侧声明**，未在 DSH 侧验证。
6. `$DSH_HOME` 下第三方插件目录当前为空（安装中），仅有 `package.json`/lockfile 声明。
7. **原文更正（2026-10-08）**：本文件早期版本照抄了 README 的"`market/skills/` 未被运行时读取"，
    实测为**错**（技能已接线，只是容器里缺席）。教训与 `vendor/规划.md` §4"配置里有 ≠ 运行期生效"
    同类：**文档里的话不是事实**，引用前必须跑一遍。
8. **原文更正（2026-10-08，方向相反的那种）**：本文件早期把「思考通道内联在 `text`」列为差距 ——
   实测 **A1 早已落地**：`app/agent/runtime.py` 直接 `yield AgentEvent(type="thinking")`，
   前端有 `ReasoningBlock` 与 `onThinkingChunk`，契约写在 [聊天记录契约](transcript-contract.md)。
   ⇒ 切口 #3 收窄为**阶段 + 用量**两条。**"差距分析写下的差距" ≠ "今天还存在的差距"** ——
   分析文档不会自己更新，**引用差距前同样必须跑一遍现状**（否则会把已有能力再实现一遍）。

## 4. 复现取证的方法（只读）

```powershell
# DSH 安装形态
Get-Content "$env:ProgramFiles(x86)\deepseek-harness\version"
Get-Content "$env:ProgramFiles(x86)\deepseek-harness\resources\runtime\cli\bin\dsh.cmd"

# app.asar 目录索引（只读；不要解包）：UInt32LE@12 = JSON 长度，dataStart = 8 + UInt32LE@4
# 解析后用 @deepseek-ai/* 的 name@version + description 做能力盘点

# DSH 状态根（不要输出来自 .credentials.yaml 的值）
Get-ChildItem "$env:DSH_HOME" -Force
Get-Content "$env:DSH_HOME\storages\workspace.json"
```
