# 上下文压缩与工具结果归档：设计规范

> 本文是 Chiron 的**承诺来源**。每条规则回答三件事：约束是什么、为什么必须是它、
> 破了会出什么事。实现可以变，承诺不能悄悄变。
>
> 与 `vendor/DeepSeek-Reasonix` 的关系：参考其**不变量**，不照搬其**实现**。§0 说明
> 为什么照搬会把单机假设带进多租户集群。

## 0. 约束前提

| 维度 | Reasonix | Chiron | 强制影响 |
| --- | --- | --- | --- |
| 进程模型 | 单进程 CLI | 多副本 K8s | "进程内状态即事实"失效 |
| 会话存储 | 本地文件 | PG `messages` + Redis 缓存 | 必须明确哪层是事实 |
| 租户 | 无 | 硬隔离 | 每张表都要能追溯到 `tenant_id` |
| 并发 | 单用户 | 多用户共享实例 | 配额必须在共享介质上 |
| 分片 | 无 | 会话可跨副本 | 召回不能依赖本进程内存 |

三条推论，全部设计由它们推出：

1. **投影必须可重建**：副本随时被驱逐，内存态不能作为事实来源。
2. **归档必须持久**：Redis 有 TTL，过期即失效，不能承担"唯一的原文副本"。
3. **召回必须按稳定 ID**：无单调序号、多副本并发追加时，位置寻址会漂移。

## 1. 现状：三个断点

### 1.1 断点 A：非工具消息被压缩后无法召回

`compaction.py:216-230` 的 `_prune_messages` 里，只有 `role == "tool"` 的消息会生成
`sn_xxx` 引用并收进 `dropped`：

```python
if m.get("role") == "tool" and m.get("tool_call_id"):
    ref = f"sn_{uuid.uuid4().hex[:12]}" if dropped is not None else ""
    ...
    if dropped is not None and len(content) < len(original):
        dropped.append((ref, original))
```

`user` / `assistant` 走 `else: processed.append(m)`，随后在 `:275` 的
`kept = processed[start:]` 中被整体丢弃，只留一句
`[N earlier message(s) were pruned to fit the context window]`（`:283-291`）。

**后果**：用户在会话开头说的硬约束（"这个仓库绝不要动 `vendor/`"）在会话中途
被丢弃后，模型无从查证，会重复犯同一个错。

### 1.2 断点 B：只有超大结果才落盘

`runtime.py:3662`：

```python
if len(text) <= compactor.INLINE_LIMIT:   # 16KB
    return text
```

小于 16KB 的工具结果**不经过** `result_store.store`。但 `_prune_messages` 按消息数
丢弃，与大小无关——一条 3KB 的 `read_file` 结果照样会被 prune 掉，且没有任何副本。

### 1.3 断点 C：归档介质会过期，且唯一完整副本被覆盖

```python
# result_store.py:18
RESULT_TTL_SECONDS = 2 * 60 * 60
```

```python
# session_store.py:103 —— _redis_set 是 SET，整体覆盖
await self._redis_set(session_id, messages)
```

```python
# runtime.py:2538-2540 —— 把压缩后的列表写回
await self._session_store.append(task.session_id, messages)
```

压缩结果覆盖了 Redis 里的完整副本。**2 小时后 TTL 到期，被压缩掉的原文在任何地方
都不存在。**

### 1.4 断点 D：`messages` 里的对话形态不完整

`sessions` 表有 `tenant_id`，`messages` 表（`migrations/sql/init.sql:923`）则只被
`SaveUserMessage`（`submit_handler.go:448`）与 `UpsertAssistantMessage`
（`:631`/`:759`）写入 **user + assistant 两种角色**，且 `tool_calls` 列只存 **id 列表**：

> `messages.tool_calls` 列只存 tool_call id 集合（内容在 tool_calls 表，避免重复存储）
> —— `submit_handler.go:588`

工具调用的**记录**在 PG 里是持久的（`SaveToolCall` 写 `input`、`UpdateToolCall`
回填 `output`/`is_error`、`recordToolCall` 走直连路径）。所以**不是"工具历史全丢"**。

真正缺的是**对话形态**：模型的上下文里 `tool` 角色消息（`{role:"tool", tool_call_id,
content}`）与 `assistant.tool_calls` 的结构性配对，在 PG 中只能从 `tool_calls` 表
**反推**，而不是原样存下来。后果是：

- `seq` 之外的**顺序**无法可靠重建（工具结果与 assistant 消息的先后只存在于 Redis）；
- 折叠区间（§3.1）以 `messages.seq` 为坐标，而被折叠的上下文里有一半内容不在
  `messages` 表里 —— 折叠边界的语义会与真实上下文错位。

也就是说：**Redis 那一份仍是"模型实际看到的历史"的唯一完整副本**，TTL 到期后
可以拼凑出记录，但拼不出当时的上下文。这足够支撑"压缩后原文可召回"的首批改造，
但不足以支撑"删掉 Redis 回写"（§2.2）—— 那一步必须等 `tool` 角色入库之后。

## 2. 核心决策：单一事实来源

上一版方案把"落盘"和"压缩"当两件事，于是产生了 `result_store`（Redis，2h）
和 `session_cache`（Redis，2h）两套易失介质。

**新方案的根本转变**：把 PG `messages` 树成唯一事实来源，压缩与归档都只是它的投影。

```
                    PG messages（唯一事实来源，只追加）
                     ↑                    ↓
       写入：引擎每轮结束提交完整序列   读取：投影重建
                     ↑                    ↓
       压缩：不删行，只记 session_folds  →  Redis 投影缓存（可丢弃）
                                          ↓
                                     Request（一次性）
```

由此得到三条承诺，它们是本方案的骨架：

- **压缩永不丢失原文**——因为压缩不碰 `messages` 行。
- **工具结果必然落盘**——因为工具结果就是 `messages` 的一行。
- **Redis 可随时清空**——因为它只缓存投影，不是事实。

## 3. Durable 层：补齐 `messages`

### 3.1 必须补的三件事

**(a) 补齐写入范围。** 引擎每轮结束提交**完整消息序列**（含 `tool_calls` 与
`tool` 角色），不再只写 user/assistant 纯文本。

**(b) 加单调序号。**

```sql
ALTER TABLE messages ADD COLUMN seq bigint GENERATED ALWAYS AS IDENTITY;
CREATE UNIQUE INDEX ix_messages_session_seq ON messages (session_id, seq);
```

`created_at` 是 `timestamp`（不是 `timestamptz`），跨副本同毫秒写入会重复，
**不能**作为排序键。

**(c) 不加 `tenant_id`。** 租户经 `session_id → sessions.tenant_id` 关联，
而 `sessions.tenant_id` 是 `NOT NULL`（`init.sql:876`），这条链可靠。冗余一列会
制造"两处不一致"的风险。

所有租户级查询必须显式 `JOIN sessions` 并让索引覆盖。

### 3.2 只追加是硬约束

任何 `UPDATE messages` / `DELETE FROM messages` 都视为缺陷。

例外只有两条，且都必须显式：分支操作（`manager_branch.go` 的既定语义）与
用户主动删除会话（级联）。

### 3.3 谁来写

**引擎直连 PG 写**，不绕 Go。理由：

- 引擎本来就有完整序列；绕 Go 要设计一套序列化协议，且 Go 侧不理解中立消息格式
- 引擎已持有 `tenant_id`（`app/tools/context.py:40` 的 `get_tenant_id()`）
- 少一次 HTTP 往返，减少一轮对话的尾延迟

**代价与对价**：引擎直连绕过了 Go 的 `ensureSessionOwned`。对价是引擎在写入前
必须校验 `session_id` 属于当前 `tenant_id`——这条校验放在写入函数的第一行，
不接受"调用方已保证"。

## 4. 工具结果落盘：分级存储

### 4.1 从"超大才落盘"改为"全部落盘"

每条工具结果都写一行 `messages`，**不依赖大小判断**。

这解决了断点 B。写放大的代价是一行 INSERT——相对于工具调用本身的开销可以忽略。

### 4.2 超大结果的分级

PG 的 `text` 支持 1GB，但撑大表会拖慢所有查询。设两道阈值：

| 大小 | 存哪里 | 上下文里放什么 |
| --- | --- | --- |
| ≤ 64KB | `messages.content` 原样 | 原样全文 |
| 64KB ~ 4MB | `messages.content` 原样（PG TOAST 自动压缩） | 结构摘要 + 全文引用 |
| > 4MB | `messages.content` 存截断体；全文进对象存储，`blob_ref` 指向它 | 结构摘要 + 两段引用 |

`blob_ref` 复用现有的存储抽象（`app/media/store.py` 的 `BaseStore`），但**新建一个
独立的 bucket/前缀**（`archives/`），不与媒体资产混用——两者生命周期语义不同：
媒体资产是用户可下载的产物，归档是内部可召回的原文。

### 4.3 上下文中的表达

模型看到的不是"这里被截断了"，而是"原文在哪、怎么取"：

```
[结果过大，已按结构摘要] 原文 1,234,567 字节，归档 ref="arc_7f3a1c2e"。
需要细节时用 read_archive(ref="arc_7f3a1c2e", offset=0, limit=8000) 分段取，
不要重复调用原工具去拿同样的内容。
```

## 5. 压缩：投影而非改写

### 5.1 `session_folds` 表

```sql
create table if not exists session_folds
(
    id            uuid primary key,
    session_id    varchar(36) not null references sessions on delete cascade,
    -- 覆盖区间，半开 [covered_from, covered_to)，值为 messages.seq
    covered_from  bigint not null,
    covered_to    bigint not null,
    digest        text not null,
    -- 溯源：没有这三列就无法追责
    digest_model  varchar(128) not null,
    digest_prompt varchar(32)  not null,
    body_suffix   bigint not null,
    created_at    timestamptz not null default now()
);
create index if not exists ix_session_folds_session
    on session_folds (session_id, covered_to desc);
```

**`covered_to` 单调不减**：新 fold 的 `covered_from` ≥ 上一条的 `covered_to`。
否则同一段历史被两份摘要代表，模型会看到互相矛盾的两版。

不写 `tenant_id`，理由同 §3.1(c)：`session_id` 已是隔离边界。

### 5.2 折叠不改写任何消息

压缩只做三件事：

1. 选出一段 `[covered_from, covered_to)` 区间
2. 为这段生成摘要，写一行 `session_folds`
3. 投影改为 `[冻结体][活动摘要][原样尾部]`

**`messages` 表一个字节都不动。** 这是"必要时可召回原文"的实现方式——不是
"把原文存到别处"，而是"原文从来没离开过"。

### 5.3 折叠索引

投影末尾附一段索引，让模型知道自己丢了什么、去哪取：

```
## Folded work index
- 9f3a1c2e user    这个仓库绝不要动 vendor/ (folded, read_archive)
- 7b2d44a0 tool    src/auth/token.go
- 5c81eb36 tool    go test ./internal/api/ (failed)
```

展示用 `messages.id` 前 8 位，实际调用用完整 ID。

**排序优先级**（最不可再生的先出现）：

1. 被折叠的用户轮次——模型无法重新推导的原话
2. 失败的调用——试过的死路
3. 命令
4. 普通读取——随时可重读

### 5.4 冻结体

摘要本身也占上下文。当摘要累积到一定规模，需要对**摘要**再折叠：

- 最新一代摘要 = 可增益的活动摘要
- 更早的摘要 = 冻结体，原样保留、永不再改

`body_suffix` 记录冻结体在投影中的残留起点：
`投影 = [冻结体][活动摘要][原样尾部]`。

与 Reasonix 的 `foldBoundary.BodySuffixFrom`（`fold_boundary.go:8-16`）同构，
但索引含义不同：那边是 transcript 位置，这边是 `messages.seq`。

### 5.5 触发边界

| 边界 | 计算 | 回答 |
| --- | --- | --- |
| 容量 | `context_window × compact_ratio`（默认 0.85） | 会不会撑爆 |
| 经济 | `ContextSoftLimitTokens` | 重放这么长的前缀值不值 |
| 硬顶 | `context_window - output_reserve` | 物理上限 |

**输出预算不参与触发**，只在发送时裁剪（§7）。否则配一个大 `max_output_tokens`
就会让压缩提前，用户设的 `compact_ratio` 形同虚设。

**共享窗口例外**：DeepSeek 官方端点输入输出共享窗口，硬顶的 reserve 抬到
`max(protocol_reserve, window/32)`。固定边距护不住大窗口——估算误差随 prompt 增长。

## 6. 召回

### 6.1 统一入口 `read_archive`

命名说明：`recall` 已被记忆检索占用（`app/tools/memory.py:353`），不能复用。
新增 `read_archive`，与既有 `read_tool_result` 的关系：

| 工具 | 读什么 | 保留理由 |
| --- | --- | --- |
| `read_tool_result` | 工具结果的**摘要替身**（`tr_` / `sn_`） | 兼容既有会话与前端，内部转调 `read_archive` |
| `read_archive` | 任意 `messages.id`，含 user/assistant/tool | 新增，覆盖全部角色 |

**`read_archive` 是唯一的新写入面**，`read_tool_result` 退化为别名。

### 6.2 参数

```python
async def read_archive(ref: str, offset: int = 0, limit: int = 8000) -> dict
```

- `ref`：`messages.id`（完整 UUID）
- `offset` / `limit`：字符分段，`limit` 上限 64KB（防止一次把 4MB 全拉进上下文）

### 6.3 每代回取预算

`max(2000, window × 0.10)`，**每代重置**而非每次调用重置。

理由：回取会重新占用刚释放的空间。"压缩 → 回取 → 压缩"若不设预算会无限循环。

回取内容**以普通工具结果身份**进入上下文，不作为投影的一部分——否则会改变投影
结构，破坏前缀缓存稳定性。

### 6.4 权限

`read_archive` 的 `ref` 是 UUID，不可枚举，但**不能只靠不可枚举当安全**。

每次读取必须校验：`messages.session_id` 属于调用方当前 `session_id`，
且该 session 属于当前 `tenant_id`。这是 §3.3 提到的同一条校验，复用同一函数。

## 7. 输出预算与压缩的边界

`max_tokens` 与压缩是两件事，互相不能替代：

- **压缩**决定"送来多少"（输入侧）
- **输出预算**决定"最多回多少"（输出侧）

唯一的交叉点在 §5.5 的硬顶：共享窗口的 provider，输入 + 输出不能超过窗口。

补法：

```python
available = window - estimated_prompt - reserve
if available <= 0:
    raise CompactionRequired   # 触发压缩，不发请求
max_tokens = min(configured, available)
```

关键：`available <= 0` 时**不硬发**。发出去只会换一个 HTTP 400，而 400 不带
"该压缩了"的语义，模型会以为自己参数错了。

## 8. 摘要契约

### 8.1 结构

固定七段，按序，无内容则省略：

| 段 | 要求 |
| --- | --- |
| 仍然成立的约束 | 用户说过的、仍生效的一切：名字、路径、ID、版本、"绝不做什么"，**用原话** |
| 目标 | 请求与意图 |
| 已做的决定及理由 | 避免被反复推翻 |
| 文件与代码 | 读改过的文件、签名、行位置、实际改动 |
| 命令与结果 | 通过/失败、关键错误文本 |
| 踩过的坑与修复 | 避免重复死路 |
| 未完成与下一步 | **一个**最具体的动作 |

规则：要点式而非散文；标识符/路径/数字原样保留；**不得编造**，不知道就省略。

### 8.2 溯源

每条 fold 必须记录 `digest_model` / `digest_prompt` / `covered_from` / `covered_to`。

Reasonix 不需要，因为它是单用户本地工具。Chiron 是 SaaS：必须能回答
"模型当时看到的这句'用户要求 X'是从哪条消息来的"——出问题时这是唯一追责依据。

### 8.3 摘要失败时

- **不得**静默降级成"丢消息无摘要"
- 保留原投影不变，记录失败原因，下一轮重试
- 连续失败达阈值后，回退到纯 SNIP（只截上下文里的工具结果，`messages` 不受影响）

理由：宁可上下文大一点，也不能让模型在无摘要状态下失去历史。

## 9. 子 Agent 委派（保留要点）

### 9.1 分布式配额

`asyncio.create_task`（`subagent.py:320/377`）在本副本内起协程，**默认无上限**
（`WriteScheduler` 在 `subagent_write_arbitration=False` 时返回 `None`）。

3 副本 × 模型一轮并发 10 个调用 = 30 个并发子 agent。

三层配额，全部在 Redis：

| 层 | 键 | 默认 | 超限行为 |
| --- | --- | --- | --- |
| 租户 | `subq:t:{tenant}` | 可配 | 拒绝 |
| 会话 | `subq:s:{session}` | 8 | 拒绝 |
| 全局 | `subq:g` | 可配 | 拒绝 |

**必须拒绝而非排队**：排队消耗父 agent 的 wall budget，且父模型无法区分"慢"
与"死了"。

### 9.2 上下文胶囊

子 agent 启动时记录装配清单：`workspace_root` / `system_prompt_source` + hash /
`tool_scope` + `tool_schema_hash` / `model` / `effort` / `parent_session` /
`parent_tool_call_id`。

**用途是续跑校验**：`resume_subagent` 前比对，不一致则拒绝并说明差异。用不同的
工具面或模型续跑，它此前的结论不再成立，等于伪造。

### 9.3 中途指导（steer）

多副本下子 agent 可能在**另一个 pod** 上跑：

```
父 pod → LPUSH chiron:subagent:steer:{run_id}
子 pod → 每轮开始前 LRANGE + LTRIM 取走
```

约束：只增不改；限流（每 run 每分钟 N 条、单条 ≤ M 字节）；标注来源
（`user` / `parent`）；不可投递时报错而非静默丢弃。

## 10. 非目标

- **不做 DAG/fleet 编排**。`AgentHub`（`collaboration.py:144`）除测试外无调用点，
  是死代码，应删除而非接线。
- **不做手工压缩工具**。自动压缩若按本文设计可靠，手工工具只增加误用面。
- **不做 `[[keep]]` 标记**。需要 UI 配合，摘要质量足够时价值有限。
- **不把归档塞进媒体资产表**。生命周期语义不同，混用会让"清理媒体"误删归档。

## 11. 验收清单

| # | 承诺 | 验证方式 |
| --- | --- | --- |
| 1 | 压缩后任何角色的原文都可按 ID 读回 | 构造三方会话（user/assistant/tool）触发折叠，逐个按 ID 读回比对字节 |
| 2 | 3KB 的小工具结果也被归档 | 执行小结果工具 → 压缩 → 按 ID 读回，断言字节相等 |
| 3 | 清空 Redis 后投影字节不变 | 删 `cp:*` 键 → 重建 → 断言与删前逐字节相等 |
| 4 | 折叠区间不重叠、不倒退 | 连续折叠 10 次，断言 `covered_from[i] >= covered_to[i-1]` |
| 5 | 摘要失败不丢历史 | 注入摘要失败，断言消息数不减、仅 SNIP 生效 |
| 6 | 回取有每代预算 | 循环 recall，断言第 N 次后返回"预算耗尽" |
| 7 | 跨租户读归档被拒绝 | 用 A 租户的 ref 在 B 租户调用，断言 403 |
| 8 | 子 agent 并发受租户配额约束 | 并发超限，断言拒绝数 = 超限数 |
| 9 | 装配不一致时拒绝续跑 | 改工具面后续跑，断言拒绝并给出差异 |
| 10 | 共享窗口下不发出必 400 的请求 | 构造近窗口 prompt，断言 CompactionRequired 而非 HTTP 错误 |

## 12. 实施顺序

按"正确性 → 可恢复性 → 能力"排，每步独立可上线：

1. **补齐 Durable**（§3）：`messages` 加 `seq`、引擎写入完整序列、删掉
   `runtime.py:2540` 的回写。这一步完成即止住"压缩即永久丢失"。
2. **小结果也落盘**（§4.1）：去掉 `runtime.py:3662` 的 `INLINE_LIMIT` 短路。
3. **`read_archive` + 权限校验**（§6）：有了它，断点 A/B 的后果才真正可逆。
4. **投影改为可重建**（§5.1）：Redis 键加租户与 generation，未命中从 PG 重建。
5. **发送时窗口裁剪**（§7）。
6. **折叠索引 + 摘要式折叠**（§5.2-§5.4）。
7. **分布式配额**（§9.1）。
8. **steer**（§9.3）。

**第 1 步的迁移难题**必须提前决策：现有 Redis 缓存里是**压缩后**的历史，没有完整
原文可回灌。两个选项：

- **(a) 强制旧会话开新档**——接受一次可见的断档
- **(b) 从 PG 的 user/assistant 重建**——接受工具历史缺失

**推荐 (a)**：(b) 会造出一份"看起来完整、实际缺工具调用"的假 Durable，
这种假数据比断档更难排查。但这是产品决策，取决于能否接受用户丢历史。
