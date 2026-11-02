# 上下文投影：主流框架调研与权威方案

> 本文解决 `docs/context-and-delegation-design.md` §2.4 第 3 步的悬置问题：
> `session_folds` 已经能写（区间 + `digest` + `body_suffix`），但**没有读回路径**。
> 折叠记录目前只服务于排查，未参与上下文组装。本文给出读回路径的设计依据与落地规格。
>
> 调研时间：2026-11-02。结论基于 7 个框架的源码 / 官方文档，逐条标注可信度。

## 1. 调研结论（先给答案）

**共识：摘要不是消息历史，而是「带区间引用的独立记录」；重建时按「覆盖集合」过滤，
而不是按「上次压缩到第几条」过滤。**

7 个框架里有 5 个把摘要存为**追加事件或独立字段**，原文一律保留；唯一例外是
LlamaIndex（见 §1.2）。你的 `session_folds` 与 OpenHands 的 `Condensation` 事件、
LangMem 的 `RunningSummary` 属于同一种做法——结构是对的，缺的只是读回路径。

**推荐：投影做成「受预算限制的回落层」，不替换全量历史这一档。** 理由见 §3。

## 2. 逐框架要点

维度：**A** 存储 / **B** 拼接结构 / **C** 水位线 / **D** 幂等 / **E** 触发 / **F** 生成 / **G** 可回溯引用

| 框架 | A 存储 | B 拼接 | C 水位线 | D 幂等 | E 触发 | F 生成 | G 引用 |
|---|---|---|---|---|---|---|---|
| OpenHands | 追加 `Condensation` 事件，原事件不删 | `[前 keep_first=4][摘要][后 ~56]`，目标 `max_size//2` | **事件 id 集合** `forgotten_event_ids` + `summary_offset` | 靠 agent 单线程 step 顺序 | 条数阈值（默认 120）+ 手动请求 | LLM（可用便宜模型） | 是 |
| LangMem / LangGraph | 摘要存独立 state key | `[summary_message] + remaining` | `RunningSummary` 内部 covered 索引 | 靠传入上轮 summary + checkpointer | `max_tokens_before_summary` | LLM，`max_summary_tokens` 限长 | 弱（摘要 + 尾部消息） |
| LangChain `trim_messages` | **无摘要产物**，纯裁剪 | `strategy="last"` + `start_on` 对齐 | 无 | 等价于丢弃 | 调用方给预算 | 提取式（即丢弃） | 否 |
| LlamaIndex `ChatSummaryMemoryBuffer` | **原地覆盖**：`get()` 内 `reset()` + `set()` | `[一条 SYSTEM 摘要][token_limit 内最近 N 条]` | 无，纯 token 计数 | **不幂等**，反复调用产生「摘要的摘要」 | `context_window*0.75` | LLM；`llm is None` 降级为丢弃 | 否 |
| Aider | `ChatSummary` 独立类，不持久化 | `[LLM 摘要(role=user)][tail]`，递归 3 层 | 无区间，按 token 二分的 `split_index` | 不幂等（每次重算 head） | 超 `max_tokens`(1024) | LLM，失败换下一模型（有降级链） | 否 |
| Codex CLI | 整体替换新历史 + `CompactedHistoryMetadata` | `[prefix][初始上下文插到最后一条真实 user 之前][summary]`；保留全部 user，丢 assistant/tool | **`window_number` 单调窗口 + `window_ids`** | 显式 `advance_auto_compact_window`；`compaction_model_hash` 判重放 | 自动 + 手动 `/compact` | LLM，prompt 可配 | 部分（`input_goal_ids`） |
| Claude Code | 生成新上下文窗口 | `[摘要][最近 5 个访问过的文件]` | 未查到 | 未查到 | 接近窗口上限 | LLM，保留架构决策/未解 bug，丢冗余工具输出 | 「轻量标识符 + 按时取回」 |

**未查到**：Letta（仓库已重构为 letta-code，`services/summarizer/` 路径 404）、
Mem0（docs 仅描述 MemFS 与 "dreaming 在 compact 时触发"）、Continue.dev、SWE-agent。
不猜。

### 2.1 共识

- **没有一家用裸计数做水位线。** 要么 id 集合（可精确去重），要么单调窗口号。
  你用 `messages.seq` 半开区间 —— 表达力强于 id 集合（区间可判定「首尾相接」）。
- **原文保留**是压倒性选择。摘要是有损派生数据，站在事实来源位置风险高。
- **头部是否冻结有分歧**：OpenHands 保 system（`keep_first=4`），Codex 保留全部
  user 而丢 assistant/tool，Claude Code 只留最近 5 个文件。你的
  `[冻结体][活动摘要][原样尾部]` 是 OpenHands 三段的等价物，多出的「冻结体」
  是迭代摘要的两代结构，属加分。

### 2.2 唯一反面样本：LlamaIndex

`ChatSummaryMemoryBuffer.get()` 里 `reset()` + `set(updated_history)` 是**原地覆盖**。
代价两条，都可验证：丢失可回溯性（没法按 id 取回原文），以及不幂等（每次 `get()`
把旧摘要与新消息合成新摘要，反复调用产生「摘要的摘要」）。**不要照搬这一点。**

## 3. 权威方案：投影作为受预算限制的回落层

### 3.1 为什么不是「替换全量历史」档

三条理由，按重要性：

1. **摘要是有损且可能失败的派生数据，不能站进事实来源的位置。** 当前 `digest` 是
   提取式的，`digest_model="extractive"`（`python-engine/app/agent/compaction.py:436`）。
   把这个质量的产物放到「全量历史」档，等于让一次降级永久影响所有副本。
2. **必须保留「全量可达」，否则验收 #3 无法实现。** `docs/context-and-delegation-design.md`
   §11 第 3 条要求「清空 Redis 后投影字节不变」。只有投影是 `messages + session_folds`
   的**纯函数**时，「逐字节相等」才有意义；把它做成事实档就变成「摘要必须逐字节复现」，
   不可验证。
3. **但也不能只当缓存。** 长会话单请求本就装不下全量，所以「预算」要放在回落层
   **内部**做条件判断，而不是放在「进不进 fold 层」上。

### 3.2 读路径规格

落地位置：`python-engine/app/agent/runtime.py:1583-1592` 的历史装配链。

```
Redis 投影缓存（cp:*，可丢）
  └─ miss → 从 PG 重建：
       (1) 取 [min_seq, max_seq) 全量
       (2) token ≤ 预算 → 直接返回全量              ← 全量这一档保留、仍可达
       (3) 超预算 → 取最新 fold，把 [covered_from, covered_to) 换成 digest，
           沿 covered_to 递减递归下钻，直到进预算或只剩一代
```

现状是 `session_cache > PG 全量 > Go 8 条`（`runtime.py:1583-1592`）。投影插入位置：
**在 PG 全量取回之后、token 超预算时**。即第 2 档内部增加一个降级分支，而不是新增
第 4 档优先级——这样不动 `_durable_history_msgs` 的「不在这里做回落」契约
（`runtime.py:2856` 的注释说明了该契约为何必须保持）。

### 3.3 必须补的两条正确性规则（当前设计缺）

**规则一：投影完备性前置校验（「不漏」的唯一可验证保证）**

使用 fold 层前校验三条，任一不成立 → **fail-open 回全量**：

1. 每条非 system 消息都有 `seq` —— 没有坐标算不出折叠边界
2. 相邻 fold 首尾相接：`from[i] == to[i-1]` —— 断档意味着那段消息既没进摘要
   也没留在尾部
3. 尾部条数不少于最后一次折叠记录的 `body_suffix` —— 少于它说明数据比记录时还少

> ⚠ **不要用算术等式校验**。设计初稿曾写
> `max_seq - body_suffix == covered_to`，实测**不成立**：`messages.seq` 是整表
> `GENERATED ALWAYS AS IDENTITY`，多会话并发写入会让同一 session 内的 seq
> **不连续**（被别的会话的行隔开）。必须按排序后的**位置关系**校验。
> 实现见 `_apply_folds`（`python-engine/app/agent/compaction.py`）。

另有一条软校验：投影的 token 估算若不小于全量，则返回 `None` —— 摘要质量差到
反而变长时，投影毫无意义。

`body_suffix` 当前语义（「折叠发生后仍留在上下文里的尾部条数」，`compaction.py:430`）
只在校验 3 成立时才有意义。这也是它长期没有读取方的根本原因 —— 没有校验，
读它就等于信任一个未验证的数字。

**规则二：同区间冲突的读取策略**

`ON CONFLICT (session_id, covered_from, covered_to) DO NOTHING`
（`history_writer.py` 的 `persist_folds`）配合唯一索引 `ux_session_folds_range`
（`0019_session_folds.py:75`）已能吸收重复插入。但**摘要内容不会二次校验**：
跨副本用不同模型摘要同一区间时，先到者胜、后到者被静默丢弃。

**已落地（2026-11-02）**，与初稿有一处差异，按实现为准：

- 初稿说「读取时按 `created_at DESC, covered_to DESC` 只取一行」。实际不需要：
  唯一索引保证同区间至多一行，`load_folds` 直接按 `covered_from ASC` 全取即可，
  读侧不存在「多行择一」的问题。
- 冲突**在写入侧观测**更直接 —— 只有写的时候才知道这一行有没有真的进去。
  实现按 asyncpg 的 `INSERT 0 n` 状态串解析（`_rowcount`），`n == 0` 时记
  `fold conflict` warning 并**不计入**返回值。此前是无条件 `written += 1`，
  冲突被谎报成写入，调用方会以为摘要更新了、实际读到的还是旧那份。
- `fold_conflict` 用日志而非指标打点，与 `history_writer` 既有的容错风格一致；
  若日后要接 Prometheus，`_rowcount(status) == 0` 处就是唯一埋点。

Codex 用 `compaction_model_hash` 解决同一问题，这里的对应物是 `digest_model`——
读方据此判断摘要可信度。

## 4. 与验收清单的对应

| 验收条目 | 本方案如何满足 |
| --- | --- |
| #3 清空 Redis 后投影字节不变 | 投影是 `messages + session_folds` 的纯函数，缓存层可丢 |
| #4 折叠区间不重叠、不倒退 | 唯一索引 + §3.3 规则一的首尾相接校验 |
| #1 任何角色原文可按 ID 读回 | `read_archive` 走 `messages` 持久表，不被投影影响 |

## 5. 实施顺序

**2026-11-02 已全部落地**（下面保留原始步骤以便对照）：

1. `history_writer.load_folds(session_id, tenant_id)` —— 取该会话全部 fold，按 `covered_from ASC`
2. `compaction._apply_folds(messages, folds, model=...)` —— 纯函数，四条校验 + 一条软校验，不满足返回 `None`
3. `runtime._project_over_budget(...)` —— 仅在「PG 全量超预算」时尝试投影，任何失败回退全量
4. `tests/test_context_projection.py` —— 10 项，其中 6 项专测**拒绝路径**

> **不在本方案内**：把 `digest` 换成 LLM 生成。那是独立一步——`_prune_messages` 是
> 同步纯函数且被当作 `on_overflow` 同步回调传入，接不了 await，需在 async 层另建
> 摘要入口。届时 `digest_model` 从 `"extractive"` 变为真实模型名，本方案的读路径不变。

### 5.1 落地时修正的两处设计错误

设计初稿有两处经实测证明不成立，实现已修正，此处留痕以免被再次写回：

1. **校验等式 `max_seq - body_suffix == covered_to` 不成立。** `messages.seq` 是整表
   `GENERATED ALWAYS AS IDENTITY`，多会话并发写入会让同一 session 内 seq **不连续**
   （被别的会话的行隔开）。改为按排序后的**位置关系**校验。
2. **「折叠区间外漏消息」的初版判断是恒等式。** 曾写成
   `covered == len(ordered) - len(tail)`，两边恒等，等于没校验。改为逐条判断
   `covered_from <= seq < covered_to`，落在任何区间之外即拒绝。

## 6. 来源

- OpenHands：`openhands/memory/condenser/`、`View.from_events()`（源码）
- LangMem：`langmem/short_term/summarization.py` 的 `RunningSummary`（源码）
- LangChain `trim_messages`：官方 API 文档（文档）
- LlamaIndex：`llama_index/core/memory/chat_summary_memory_buffer.py`（源码，已核 `reset()`+`set()`）
- Aider：`aider/history.py` 的 `ChatSummary`（源码，已核递归结构与 `step=3`）
- Codex CLI：`CompactedHistoryMetadata`、`advance_auto_compact_window`（源码）
- Claude Code：Anthropic 工程博客关于 context management 的公开描述（文档）
