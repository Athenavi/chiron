# 子 Agent 的「继续 / 分叉」设计（R5(b)）

> **状态：✅ 已落地（2026-10-09）** —— 迁移 `0008_subagent_resume_link`（两列）·
> `app/subagent/access.py`（访问谓词）· `app/subagent/resume.py`（步骤 → 起始上下文）·
> `app/tools/subagent_resume.py`（`resume_subagent` 工具）· 用例
> `python-engine/tests/test_subagent_resume.py`。本文保留为**设计与取舍**的记录；
> **实现与设计的对齐与偏差见 §0.1**。
>
> 依据：`vendor/规划.md` §3.5 的 R5 —— 该处已把"从某个子 agent 继续 / 分叉"**拆成两个方向**：
> **(a) parent → child 播种**已落地（`app/subagent/inherit.py`），**(b) child → 继续 / 分叉**是缺的那个。
> 本文即对 (b) 的**数据模型 · 续跑语义 · 可达性 · 未决问题**的回答。
>
> **取证强度**：【枚举】读源码（2026-10-09）；§0 的现状表每条都带文件路径。

## 0. 先修口径：缺口比"没做"窄得多

立项前的第一件事是**核现状**——否则会把已有能力再实现一遍（`vendor/规划.md` §4 的同族教训）。
实测（【枚举】，2026-10-09）：

| 已有的 | 在哪 | 它解决的是什么 |
|---|---|---|
| `subagent(rerun_of=…)` 参数 | `app/tools/subagent.py` | 落血缘：新 run 知道自己"从哪次重跑而来" |
| **`rerun_subagent(run_id, task_override="")` 工具**（已实现、已接线、已在两侧策略表） | `app/tools/subagent_rerun.py` · `app/agent/tool_policy.py` · `internal/api/tool_policy.go` | **按原任务再派一次**（中断/失败/lost 后的补救）。起始上下文仍是**父会话 seed / Profile**，**不带被重跑 run 的步骤** |
| `subagent_run_steps`（每步一行） | `app/subagent/store.py` | **复查**子 agent 做了什么（`read_subagent_result`） |
| parent → child **一次性播种** | `app/subagent/inherit.py`（R5(a)，默认关） | 父的**已完成回合**作为子 agent 的起始上下文 |
| 后台 run 结束 → 唤起父会话新一轮 | `app/subagent/followup.py`（幂等 + 限速 + 禁级联） | 上行通道（子 agent 的结论回到主对话） |
| 结构化结局（含能否重试） | `app/subagent/outcome.py` + 迁移 `0007_subagent_error_code`（R3） | 失败/取消/超时**可区分** |

**⇒ 真正的缺口只有一件**：**带着被续跑 run 的已有步骤**继续跑，或从某一步**截断**另走一条路。

> ⚠ **不要把 (b) 塞进 `rerun_subagent`**：那会让**同一个入参**有两种起始上下文（"原任务" vs
> "沿步骤继续"），是"一个工具两种语义"的经典来源。**新工具**（`resume_subagent`）与
> `rerun_subagent` **并列**，血缘也分开记录。

**与 DSH 的关系**：DSH 的 `dsh-subagent-fork-in-process` 只做 (a) 那层的**一次性 seed**
⇒ (b) 属**超出对标**，不是"追平项"。这也是它可以慢慢评审的原因。

### 0.1 实现与设计的对齐与偏差（2026-10-09 落地时记录）

| 设计里写的 | 落地的样子 | 为什么 |
|---|---|---|
| §2「重建成 **messages**」 | 重建成**前置文本**，前缀到子 Agent 的 `content` | 构造真正的 messages 要改子 agent 的消息组装；而 R5(a) 的 `inherit.py` 已经确立了"渲染成文本 + 前置"这条**被验证过**的通道 —— 两条 sibling 通道用同一种交付形态，比"更像 messages"更重要（也避免"同一件事两种注入方式"） |
| §5② 「分叉点的粒度 = 步骤」 | `at_step` 直接进 **SQL**（`seq < $2`），不在宿主侧裁剪 | 少一次全量读；且"取哪一段"是**查询语义**，写在查询里才不会与调用方各自漂移 |
| §2「默认关」 | `settings.subagent_resume_enabled=false`，在 **handler** 处判 | 与 R1（`SUBAGENT_HOST_RECEIPTS`）/ R2（`SUBAGENT_WRITE_ARBITRATION`）**同一手法**：工具始终注册、开关是运行时的；关闭时对任何调用返回**明确的"未启用"**并点出开关名（不静默失败） |
| §5① 「是否暴露给模型」 | **注册，但不在 `CORE_TOOL_NAMES`** | 与 `rerun_subagent` / `read_subagent_result` **完全对称**：靠 `tool_search` 按需激活 + `app/subagent/reporting.py` 的报告提示引导（已补一句"需要接着做用 `resume_subagent(run_id, instruction)`"） |
| §5④ 「配额」 | **已落地（M15a–M15c，2026-10-18）**：挂在**任务线**上，不是挂在会话或单个 run 上 | 立项时记的**已知风险**"同一 run 可被反复续跑（每次都派新 `run_id` 并消耗 token）"**已被机械化**：迁移 `0021` 把线根物化成 `subagent_runs.root_run_id`（**优先沿 `resumed_from`、其次 `parent_run_id`** —— 本表 §2 的血缘正是线账的账本单位；`rerun_of` 属"用户主动重复劳动"，**不入线**），`0022` + `app/subagent/line_ledger.py` 按线求和已发生量并加上在途预留与上限比，越界 ⇒ **暂停该线 + 挂出「提额」待办**（复用 M7 的 `pending_interactions` 与「待你处理」收件箱），有人提额后续跑。**为什么挂在 run 而不是会话**：会话是人的边界（一个会话里可以派很多条互不相干的任务线，按会话设上限会让一条重活挤掉别人的额度），而"这条任务一共花了多少"恰好等于血缘闭包的和。**仍知的缺口**（都写在论文 §5.2 那条里）：崩溃/接管那一段的用量**从不进租户额度池**（网关只按终态事件计费，接管又绕过网关），线账只能按已落库的行求和 ⇒ 偏**少算** |

## 1. 数据模型（推荐：加两列，不建新表）

沿用本仓既有手法（`vendor/规划.md` §1.2 第 4 条：**新列单独写**，让未迁移的库**只丢那一列**，
而不是让整条收尾 SQL 失败 —— `rerun_of` / `inherited_messages` / `LIFECYCLE_SQL` 都是这么做的）：

```sql
ALTER TABLE subagent_runs ADD COLUMN IF NOT EXISTS resumed_from character varying(64);
ALTER TABLE subagent_runs ADD COLUMN IF NOT EXISTS resume_at_step integer;
```

| 列 | 语义 |
|---|---|
| `resumed_from` | 被续跑 / 被分叉的那个 run id（**血缘**，与 `rerun_of` 并列但不同源） |
| `resume_at_step` | 分叉点：只继承 `seq < resume_at_step` 的步骤；`NULL` = **续到底**（继续） |

* **关系是 1:N**（一个 run 可被多次续跑/分叉）⇒ 用一个列 + 索引即可查
  （`WHERE resumed_from = $1`）；建新表只在需要"多种关系统一表示"时才划算，而当前只有两种
  （`rerun_of` 已经在列上）。
* 写入用**单独的 UPDATE**（与 `RERUN_OF_SQL` 同款），不进 `RUN_INSERT_SQL`。
* **迁移单 head**：当前 head 是 `0007_subagent_error_code` ⇒ 新迁移的 `down_revision` 指向它。

**备选（更重，不推荐先用）**：新表 `subagent_run_links(from_run_id, to_run_id, kind, at_step)`。
它更好看，但要多一条 join 与一套生命周期（何时删、如何防环），而当前需求用两列就能表达。

## 2. 续跑语义

新增工具 `resume_subagent(run_id, instruction, at_step=0) -> dict`：

1. **只允许终态 run**：复用 `app/tools/subagent_rerun.py` 的 `RERUNNABLE`
   （`completed` / `failed` / `cancelled` / `lost`）。`running` 的不许续跑 —— 会得到两份并发作业。
2. **起始历史 = 被续跑 run 的步骤**：取 `subagent_run_steps` 里 `seq < at_step`（`at_step=0` ⇒ 全部），
   把 `role` / `content` / `tool_name` / `tool_call_id` 重建成 messages。**只读**，原 run 的行**一个字不改**
   —— 它是证据。
3. **带上限定与信任声明**：重建受**既有**上限约束（`STEP_MAX_CHARS` = 32K 的逐步截断；
   条数/总长上限与 R5(a) 同一套：**取先到、超限保近处**），并在最前面加**数据不是指令**声明
   （与 `inherit.py` 的 `<inherited-context>` 同姿态 —— 步骤里可能夹着模型自己写过的"指令"）。
4. **工具面与权限重新计算**：新 run 按自己的 Profile / mode 决定工具面，**不继承**旧 run 的；
   `read_only` 沿用被续跑 run 的值（**只能收紧、不能放宽** —— 与 `rerun_subagent` 的"重跑不是提权通道"同一条纪律）。
5. **审计与遥测**：新 run 走**同一个** `subagent` 派发路径（同一套预算 / 栅栏 / 落库 / 收据），
   只多写 `resumed_from` / `resume_at_step`；生命周期起点标为 resumes（`PHASE_RESUME` 已存在）。
6. **默认关**：新增 `settings.subagent_resume_enabled`（默认 `False`）。关闭时：不注册工具、
   不写新列 ⇒ **零行为变化**（与 `SUBAGENT_WRITE_ARBITRATION`、`fs_require_observation` 同约定）。

**"继续"与"分叉"是同一个实现的两种取值**（`at_step` 全取 vs 截断），因此**一个工具**即可，
不需要第二个入口 —— 区别只在 `at_step`。

## 3. 可达性（多租户边界）

**只允许同一对话会话内**：查询条件与 `rerun_subagent` 的 `RUN_SQL` **同源**
（`tenant_id` + `user_id`（或 `IS NULL`）+ `root_session_id`），跨租户/跨会话一律拒并留痕。

为什么重用那一套判定（而不是另写一份）：同一件事写两遍必然漂移，而**漂移方向通常是放宽**。

## 4. 明确不做（本设计范围内）

* **不修改被续跑 run 的任何行**（不追加步骤、不改状态）—— 续跑产生**新 run**，旧 run 保持终态；
* **不做跨实例的"现场迁移"**：续跑是"用已落库的步骤**重建**上下文"，不是搬运行中的进程内存；
* **不给续跑开新的可执行面**：工具面按新 run 自己的 Profile 算，不继承旧 run 的加宽；
* **不做"无限续跑"**：见 §5 的配额问题；
* **不引新依赖**：重建只用既有 store 与 `inherit.py` 的上限工具函数。

## 5. 待评审要拍的四个问题

| # | 问题 | 备选与代价 |
|---|---|---|
| ① | **`resume_subagent` 是否暴露给模型？** | 暴露 = 能力面更完整（`rerun_subagent` 已在策略表里同类）；不暴露 = 只留内部 API（工具数量不膨胀）。**现状倾向**：与 `rerun_subagent` 同级登记在 `write` 档（它会再派作业、消耗 token） |
| ② | **分叉点的粒度**：`seq`（步骤）还是"回合"？ | 步骤级更精确但对外更难解释；回合级更好懂但需要"回合→seq 区间"的映射（当前没有这个概念） |
| ③ | **产物关系**：分叉出的新 run 与旧 run 的 `artifacts` / `result_ref` 是否共享？ | 共享 = 省空间但"哪次产生的"变模糊；不共享 = 各存各的，证据链更清楚（推荐后者） |
| ④ | **配额**：同一 run 最多被续跑几次？ | `followup.py` 已有"每会话每小时上限 + 禁级联"的先例；**建议照抄**（默认 3/小时、续跑出来的 run 再续跑需显式允许） |

## 6. 验收（对外可验证的行为；**已满足** —— 用例见 `python-engine/tests/test_subagent_resume.py`）

> 实现于 2026-10-09，本节每条都有一条用例打在它上面；§0.1 记录了与设计的三处对齐、
> 一处**未实现**（配额）。

1. **默认关 ⇒ 零行为变化**：关闭时无新工具、无新列写入、`pytest -m "not integration"` 全绿；
2. **血缘可查**：续跑后的新 run 在 `subagent_runs` 里能看到 `resumed_from` = 原 run、`resume_at_step` = 传入值；
3. **历史真的带上了**：新 run 的起始消息里能找到原 run 的步骤文本，且**不超过** `at_step`（分叉）；
4. **原 run 不被改动**：续跑前后，原 run 的 `status` / 步骤行**逐字不变**；
5. **边界**：跨租户 / 跨会话续跑被拒并留痕；`running` 的 run 不可续跑；`read_only` 不会被放宽；
6. **超限有界**：步骤重建遵守既有限额，超限时**保近处并显式标注**（不静默截断）；
7. **迁移单 head**：`python -m alembic -c alembic.ini heads` 只输出 1 个 head。

## 7. 相关文档与代码

* `vendor/规划.md` §3.5（R1–R5 的判据与进度）· §1.2 第 4 条（迁移约定）· §4（"先核现状再立项"的教训）
* [子 Agent 设计](subagent-design.md)（§3.1 Profile 定义层 · §3.2 运行记录落库 · §8 已知边界）
* [运行现场 checkpoint 续跑设计](run-checkpoint-design.md)（**主对话**的续跑，与本文同族但不同层：
  它续的是 run 的**现场**，本文是"用已落库的步骤**重建**子 agent 上下文"）
* 代码：`app/tools/subagent_rerun.py` · `app/subagent/{inherit,store,outcome,reporting}.py` ·
  `app/agent/subagent_runner.py` · `migrations/versions/`
