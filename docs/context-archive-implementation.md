# 上下文归档与加载：落地实施方案

> 本文是 `docs/context-and-delegation-design.md` 的**实施配套**。设计文档回答"承诺是什么"，
> 本文回答"具体怎么改、改哪些文件、先改哪个、怎么验"。
>
> 目标：压缩后原文可召回、工具结果必然落盘、长会话加载不拖垮前端与网关。

## 0. 已核实的现状

| 事实 | 证据 | 影响 |
| --- | --- | --- |
| 前端一次性拉全量消息 | `frontend-vue/src/api/index.ts:336` `getChatSessionMessages` 无分页参数 | 长会话首屏等待与内存双爆 |
| 代理端点无分页 | `internal/api/routes_proxy.go:188` 直通代理，未接 `GetMessagesPage` | 前端无分页可用 |
| 分页实现已存在 | `internal/session/manager_queries.go:28` `GetMessagesPage`（`limit`/`before` 复合游标/`HasMore`） | **可直接复用，无需重写** |
| 会话详情已暴露分页 | `internal/api/conversation.go:153-159` | 说明参数约定已定 |
| 无 HTTP 条件请求 | 全仓 `ETag` / `If-None-Match` 零命中 | 每次刷新全量重传 |
| `messages` 无 `seq` | `migrations/sql/init.sql:923-935` | 折叠区间缺少稳定 ID |
| 小结果不落盘 | `runtime.py:3747` `INLINE_LIMIT` 短路 | 断点 B（**第 2 步后已缓解**，见下） |
| 非工具消息不进 `dropped` | `compaction.py:216-230` | 断点 A |
| 归档介质 2h TTL | `result_store.py:18` | 断点 C |
| `tool_calls` 无会话索引 | `init.sql:625` 只有 `(turn_id)` | 按会话查工具调用走全表扫描 |
| 工具游标串用 | `conversation.go:178` 复用 messages 的 `before` | 翻页错位，工具调用漏取 |
| 工具查询错误被吞 | `conversation.go:178` `toolCalls, _ :=` | 记录消失且无日志 |
| `output` 写入无上限 | `tool_handler.go:119-135` `fmt.Sprintf("%v", out)` | 单条可达 MB 级，列表查询拖垮 |
| 前端翻页无竞态守卫 | `ChatView.vue:2179` 无 `switchSeq` 校验 | 会话 A 的历史拼进会话 B |
| 前端错误当"没有更多" | `ChatView.vue:2196-2197` `catch { hasMore=false }` | 一次 5xx 后永久停止加载 |
| 前端翻页不去重 | `ChatView.vue:2190` 无条件拼接 | 同一条目可能渲染两次 |

## 1. 数据模型

### 1.1 迁移 0016：`messages` 补列

```sql
-- seq：稳定单调序号。created_at 是 timestamp（非 tz），跨副本同毫秒会重复
ALTER TABLE messages ADD COLUMN IF NOT EXISTS seq bigint GENERATED ALWAYS AS IDENTITY;
CREATE UNIQUE INDEX IF NOT EXISTS ix_messages_session_seq ON messages (session_id, seq);
```

**迁移 0017 已被 `messages.tool_call_id` 占用**（第 2 步"全角色入库"的前置：
没有这一列，`tool` 角色消息读回来无法与 `assistant.tool_calls` 配对，
`_ensure_valid_tool_sequence` 会把它们当孤儿删掉 —— 存进去等于没存）。

`blob_ref` 列留到**迁移 0018**（`message_archives` 一起加）：它只有在对象存储就位后
才有意义，单独加一列会让代码在「列存在但无表」的中间态里摇摆。

**不加 `tenant_id`**：`session_id → sessions.tenant_id` 是 `NOT NULL` 的可靠链，
冗余一列只会制造两处不一致。

**回填**：存量行 `seq` 由 `GENERATED ALWAYS` 自动按物理插入顺序赋值。旧数据的最新
`seq` 与 `created_at` 顺序可能不一致，但**只要不跨行比较就不会错**——排序一律用
`ORDER BY seq`，与 `created_at` 混用是缺陷。

### 1.2 迁移 0019：`session_folds`

> 小节顺序按**依赖**排列（`session_folds` 只用 `messages.seq`；`message_archives`
> 依赖对象存储），迁移编号按分配顺序，两者不要求一致。

```sql
create table if not exists session_folds
(
    id            uuid primary key,
    session_id    varchar(36) not null references sessions on delete cascade,
    covered_from  bigint not null,   -- messages.seq，半开区间
    covered_to    bigint not null,
    digest        text not null,
    digest_model  varchar(128) not null,
    digest_prompt varchar(32)  not null,
    body_suffix   bigint not null,
    created_at    timestamptz not null default now()
);
create index if not exists ix_session_folds_session
    on session_folds (session_id, covered_to desc);
```

### 1.3 迁移 0018：`message_archives`

只存 > 4MB 的全文，其余留在 `messages.content`：

```sql
create table if not exists message_archives
(
    ref          varchar(64) primary key,
    session_id   varchar(36) not null references sessions on delete cascade,
    byte_size    bigint not null,
    storage_key  text not null,        -- 对象存储路径（local: 相对 STORAGE_ROOT）
    sha256       char(64) not null,    -- 完整性校验
    created_at   timestamptz not null default now()
);
create index if not exists ix_message_archives_session
    on message_archives (session_id);
```

**为什么不复用媒体资产表**：媒体资产是用户可下载的产物，归档是内部原文。
混用会让"清理媒体"误删归档，而且两者的保留策略相反。

### 1.4 迁移 0015：`tool_calls` 补索引

**`tool_calls` 在 `session_id` 上完全没有索引。** `init.sql:625` 只有：

```sql
create index if not exists ix_tool_calls_turn_id on tool_calls (turn_id);
```

而 `GetToolCallsPage`（`manager_messages.go:329`）的查询是
`WHERE session_id = $1 ORDER BY created_at DESC, id DESC LIMIT n`——**全表扫描 + 排序**。

对比：`messages` 有 `ix_messages_session_created`。`tool_calls` 是唯一一个按会话查
却没有会话索引的表。

```sql
-- 覆盖 WHERE session_id + ORDER BY created_at DESC, id DESC
-- 前导 session_id 服务谓词，后两列服务排序，可走 index-only scan
CREATE INDEX IF NOT EXISTS ix_tool_calls_session_created
    ON tool_calls (session_id, created_at DESC, id DESC);

-- output 的大字段不进索引；但需要能判断"是否需要单独取全文"
ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS output_bytes integer;
UPDATE tool_calls SET output_bytes = octet_length(output) WHERE output_bytes IS NULL;
```

`output_bytes` 回填必须放在迁移里，否则存量行无法判断该不该提示"展开查看"。

**行值比较改写**：现有游标条件

```sql
AND (created_at < $2 OR (created_at = $2 AND id < $3))
```

在 `(session_id, created_at DESC, id DESC)` 复合索引上**用不满索引**（`OR` 让优化器
可能退化成两次扫）。改成行值比较才能完整利用索引：

```sql
AND (created_at, id) < ($2, $3)
```

行值比较与 `ORDER BY created_at DESC, id DESC` 的方向一致，PG 可以直接用索引
保序扫描并提前终止。**这是本方案里唯一必须改 SQL 形状的地方**，其余都是加索引。

## 2. 引擎侧

### 2.1 写入：全部角色入库

新增 `app/agent/history_writer.py`：

```python
async def persist_turn(session_id: str, tenant_id: str, messages: list[dict]) -> None
```

三条硬约束：

1. **写前校验归属**：`SELECT 1 FROM sessions WHERE id=$1 AND tenant_id=$2`。
   不接受"调用方已保证"——引擎直连 PG 绕过了 Go 的 `ensureSessionOwned`。
2. **只追加**：只 `INSERT`，任何 `UPDATE` / `DELETE` 都是缺陷。
3. **失败不阻断**：写库失败只告警，本轮对话继续（与 `result_store.store` 同一取向）。

`tenant_id` 由 `app/tools/context.py:40` 的 `get_tenant_id()` 提供。

### 2.2 Redis 回写：降级为回落层，**不是**删除

原方案要求删除 `runtime.py` 的 `_session_store.append(...)`。**实施时推翻了这一判断。**

理由：`session_store` 是 PG 不可用时的**唯一**回落。`TestContextContinuity`
（`tests/test_runtime.py`）构造的场景——第一轮中断退出、第二轮继续——在 PG 不可用时
完全依赖缓存。删掉写入后该测试连续失败三次，症状是"第一轮用户消息丢失"。

正确做法是**改变它的角色**，而非删除：

| 层 | 角色 | 失效后果 |
| --- | --- | --- |
| PG `messages` | **事实来源**（只追加、含 tool 角色、`seq` 稳定排序） | 不可召回原文 |
| Redis `session_cache` | **回落层**（PG 不可用 / 隐私模式） | 仅影响可用性，不影响可召回性 |
| Go `history`（8 条） | 最后兜底 | 短会话可续，长会话断档 |

关键：**缓存里是压缩版已无害**，因为原文在 PG 里还在。这正是"压缩后原文可召回"
成立的前提——不再要求任何一份缓存保存完整历史。

边界：`no_retention` 隐私模式下两者都不写（`history_writer.persist_messages` 与
`session_store.append` 各自检查 `is_no_retention()`）。

### 2.3 分级归档

| 大小 | `content` | `blob_ref` | 上下文里放什么 |
| --- | --- | --- | --- |
| ≤ 64KB | 全文 | NULL | 全文 |
| 64KB ~ 4MB | 全文（TOAST 自动压缩） | NULL | 结构摘要 + `read_archive` 提示 |
| > 4MB | 前 64KB + 标记 | `arc_<12hex>` | 结构摘要 + 两段引用 |

**`INLINE_LIMIT` 短路（`runtime.py:3747`）：第 2 步后已不再是缺陷，保留。**

原判断是"< 16KB 的结果不进任何持久层 ⇒ 压缩后无法召回"。第 2 步落地后这个前提消失了，
小结果现在有**两条**落盘路径：

| 路径 | 位置 | 上限 |
| --- | --- | --- |
| `messages`（`role=tool`） | `history_writer.persist_messages(roles={"tool"})` | `MESSAGE_CONTENT_MAX_CHARS` = 256k |
| `tool_calls.output` | `internal/api/tool_handler.go:113 recordToolCall` | `toolOutputMaxBytes` = 256KB |

所以短路只影响"**是否生成摘要**"，不影响"**是否落盘**"。强行去掉它会让 16KB 以下的
正常结果也走一遍摘要 —— 那是净损失（小结果本就适合原样进上下文）。
"是否落盘"与"上下文里放什么"必须解耦，但解耦靠的是**第 2 步的写入路径**，不是删掉这个判断。

**真正的缺口是"召回通道的寿命"而非"是否落盘"**：`result_store` 走 Redis，TTL 仅
2 小时（`result_store.py:18`）—— 隔天就取不回。这才是第 3 步要补的东西：
`read_archive` 按 `messages.id` 读持久表，与 TTL 无关（见 §2.5）。

**已知未完成**：`read_archive` 需要 `messages.id`，而当前没有任何通知把 message id
交给模型（压缩通知里给的是 `result_store` 的 `result_ref`）。要让该工具真正可用，
还需在裁剪/折叠通知中携带 message id —— 这属于第 11 步（`session_folds`）一并做。

### 2.4 压缩改为投影

`_compact_messages` 的返回值只用于组装请求，**不再回写任何存储**。折叠时：

1. 选区间 `[covered_from, covered_to)`
2. 生成摘要 → `INSERT session_folds`
3. 投影 = `[冻结体][活动摘要][原样尾部]`

`messages` 一个字节都不动。

### 2.5 `read_archive` 工具

```python
async def read_archive(ref: str, offset: int = 0, limit: int = 8000) -> dict
```

- `ref`：`messages.id` 完整 UUID
- `limit` 上限 64KB（防止一次把 4MB 拉进上下文）
- 权限：`messages.session_id` 必须属于当前 session 且租户匹配

**命名避开 `recall`**：它已被记忆检索占用（`app/tools/memory.py:353`）。

`read_tool_result` 退化为别名，内部转调 `read_archive`，兼容旧会话。

> **实施时未按此合并（偏离已记录）。** 两者的 `ref` 来自**不同存储**：
> `read_tool_result` 的 `result_ref`（`tr_xxx` / `sn_xxx`）指向 Redis `result_store`，
> `read_archive` 的 `ref` 是 `messages.id`。把前者转调后者，等于让所有旧会话里
> 已经写在上下文中的 `result_ref` 立即失效（那些 ref 在 `messages.id` 里查不到）。
> 兼容旧会话的正确做法是**保留两个工具**：旧的继续按 Redis 查，新的按 PG 查。
> 等旧 ref 全部过期（2h TTL 之上再留一个发布周期）之后，再考虑合并。

## 3. Go 网关

### 3.1 消息分页端点

**分页逻辑已存在，不要重写。** `internal/session/manager_queries.go:28` 的
`GetMessagesPage` 已实现 `limit` + `before` 复合游标 + `HasMore`：

```go
// MessagePage / GetMessagesPage / manager_queries.go:17-92
page.Cursor = first.CreatedAt.UTC().Format(time.RFC3339Nano) + "|" + first.ID
```

真实缺口只有三处，改动都很小：

| 缺口 | 现状 | 改动 |
| --- | --- | --- |
| 代理端点未接分页 | `routes_proxy.go:188` 直通引擎 | 改为调用 `GetMessagesPage`，透传 `limit`/`before` |
| 游标键是 `created_at\|id` | 同毫秒并发会重复 | 换成 `seq`（迁移 0016 已加） |
| 无 `ETag` | 每次全量重传 | 加条件请求（§3.2） |

改游标而不是加新函数，理由：已有 `HasMore` / 正序翻转 / 多取一条判边界这些
边界处理是对的，重写只会重新踩一遍。

**为什么在网关侧查、而不是继续代理给引擎**：消息是纯读，引擎不参与；走引擎会
多一次 HTTP 跳转，且引擎不持有分页语义。

**响应形状不变**（`MessagePage` 的 JSON），只增加 `etag` 字段。保持形状不变是为了
前端能渐进切换——先接分页、后续再加缓存，两步可以分开上线。

### 3.2 条件请求

**既有端点**必须补上 `ETag`（这是当前全仓的空白——只有 `media_sign.go:200` 与
`events.go:39` 设了 `Cache-Control`，没有任何条件请求支持）：

```
ETag: W/"<session_id>-<max_seq>-<count>"
```

`max_seq` 取自 `SELECT max(seq)`。客户端带 `If-None-Match` 且匹配时返回 **304**，
不带响应体。

`Cache-Control: private, no-cache`（可缓存但必须校验）。
**不能用 `public`**：消息含用户内容，中间代理不得缓存。

这是当前全仓的空白——只有 `media_sign.go:200` 与 `events.go:39` 设了 `Cache-Control`，
没有任何条件请求支持。

### 3.3 归档内容读取

```
GET /v1/chat/sessions/{id}/archives/{ref}?offset=0&limit=8000
```

一次校验三层：`auth` → `session` 归属 → `tenant` 匹配。返回 `text/plain` 分段。

**不可枚举不等于安全**：`ref` 是 UUID，但每次仍要校验归属。

### 3.4 `tool_calls` 必须用独立游标

代码现状（`internal/api/conversation.go:176-178`）：

```go
p, e := h.sessionMgr.GetMessagesPage(r.Context(), id, limit, before)
...
toolCalls, _ := h.sessionMgr.GetToolCallsPage(r.Context(), id, limit, before)
```

**同一个 `before` 被传给了两张表**，而它的值来自 `page.Cursor`——那是
**messages 表**最早一条的 `created_at|id`。

用 messages 的游标去筛 `tool_calls` 是错的：两表 `created_at` 分布不同（工具调用发生在
两条消息之间）。`WHERE created_at < <messages游标>` 会把**本页该显示的工具调用一并排除**，
或者翻页时漏取。这不是慢，是**数据错位**。

改为双游标：

```json
{ "cursor": "<messages 游标>", "tool_cursor": "<tool_calls 游标>", "has_more": true }
```

前端上滚时两个都带。`cursor` 缺失只影响消息，`tool_cursor` 缺失只影响工具——
两者独立，一个坏不会拖垮另一个。

### 3.5 `tool_calls` 查询错误不得静默吞

`conversation.go:178` 的 `toolCalls, _ :=` 把错误丢了。`GetToolCallsPage` 出错时返回
`nil, err`，于是：

**工具调用从界面上凭空消失，日志里什么都没有。**

用户看到"我的工具调用记录没了"，而我们无从排查。必须：

```go
toolCalls, tcErr := h.sessionMgr.GetToolCallsPage(...)
if tcErr != nil {
    slog.Warn("tool calls page failed", "session", id, "error", tcErr)
}
```

并在响应里带 `tool_calls_partial: true`，前端据此提示"部分工具记录加载失败"，
而不是让人以为记录真的不存在。

### 3.6 `output` 写入必须有上限

`internal/api/tool_handler.go:119-135`：

```go
if out, ok := output["output"]; ok {
    outputStr = fmt.Sprintf("%v", out)
}
```

**没有任何大小上限。** 一个 `read_file` 返回 10MB，就写 10MB 进
`tool_calls.output`（`text` 类型）。

这直接打击加载性能：`GetToolCallsPage` 的 `SELECT id, session_id, tool_name, input,
output, ...` 会把大字段一起拉出来，一页 50 条可能几百 MB。

两条措施：

**(a) 写入侧截断。** > 256KB 时截断并置 `output_bytes`（迁移 0015 已加列），
全文进 `message_archives`，返回 `output_ref`。

**(b) 列表查询不返回 `output`。** 只返回 `output_bytes` 是否为 0 与 `output_ref`：

```sql
SELECT id, session_id, tool_name, input, is_error, duration_ms, created_at,
       COALESCE(output_bytes, octet_length(output)) AS output_bytes
  FROM tool_calls ...
```

点开工具卡时才按需取全文。**这是"加载性能"最直接的一招：列表不带大字段。**

代价是工具卡默认只显示"结果 128KB，点击展开"，多一次点击。这是刻意的取舍：
一页 50 条工具调用无论如何塞不下全文。

## 4. 前端

### 4.1 加载策略

现状 `getChatSessionMessages` 一次性拉全量，长会话（数千条）首屏会卡。

改为三段：

| 阶段 | 内容 | 时机 |
| --- | --- | --- |
| 首屏 | 最近 50 条 | 打开会话 |
| 上滚 | 每次 50 条（游标向前） | 触达顶部 |
| 全文 | 不主动加载 | 仅在搜索/导出时按需 |

`transcriptViewport.ts` 已有"prepend 时锚点保持"的契约（`docs/transcript-contract.md:1.2`），
分页加载直接复用，**不需要新几何逻辑**。

### 4.2 稳定性：`loadEarlier` 的三个缺陷

这是本节的重点。前端加载"能用"和"稳定"是两回事，下面三处都会**静默出错**。

#### 缺陷 1：网络错误被当成"没有更多了"

`ChatView.vue:2196-2197`：

```js
} catch {
  hasMore.value = false
}
```

任何错误——超时、502、断网、服务端 panic——都会被解释成"历史到此为止"。
用户看到的是"翻到顶了"，实际还有几百条没加载，而且**本次会话再也不会重试**
（`hasMore` 永久为 false）。

正确做法是区分三类：

| 情况 | 处理 |
| --- | --- |
| 真的没有更多（服务端 `has_more: false`） | 置 `hasMore = false` |
| 可重试错误（超时 / 5xx / 网络） | **保留 `hasMore = true`**，显示"加载失败，点击重试" |
| 鉴权失效（401） | 触发重新登录，不清 `hasMore` |

判据用 HTTP 状态码，不去匹配错误文案——文案会变，状态码不会。

#### 缺陷 2：没有会话切换守卫

`switchSession`（`:2090`）有 `mySeq` 守卫：

```js
const mySeq = ++switchSeq.value
...
if (mySeq !== switchSeq.value) return
```

但 `loadEarlier`（`:2179`）**完全没有**。场景：

1. 在会话 A 触顶，`loadEarlier` 发出请求
2. 请求未回，用户切到会话 B
3. A 的响应到达，执行 `items.value = [...earlier, ...items.value]`

**结果：会话 A 的历史被拼进会话 B 的消息列表。** 这不是闪烁，是数据错位——
用户会看到不属于这个会话的内容，刷新后消失，无从解释。

修法：`loadEarlier` 开头取 `const mySeq = switchSeq.value`，写回前校验
`mySeq === switchSeq.value`，否则丢弃。

#### 缺陷 3：`cursor` 为空时死锁

```js
earliestCursor.value = data.cursor || ''
...
if (loadingEarlier.value || !hasMore.value || ... || !earliestCursor.value) return
```

若服务端返回 `has_more: true` 但 `cursor` 为空（游标生成失败、字段遗漏），
`hasMore` 为 true、`earliestCursor` 为空，守卫直接早退——**永远无法继续加载，
界面上也没有任何提示**。

修法：`cursor` 为空时把 `hasMore` 一并置 false 并记 warn 日志，
或者用本页最早一条的 `id` 兜底构造游标。**不能让"有更多但取不到"这种状态存在。**

### 4.3 去重与边界

`items.value = [...earlier, ...items.value]` 是无条件拼接。

游标是 `created_at|id` 复合值，同毫秒同 id 不会重复，但**服务端与前端时钟/时区
处理不一致时会重复**。翻页重复会让同一个工具卡出现两次，用户会以为工具执行了两次。

按 `item.id` 去重（`ChatItem` 的 `id` 字段已保证唯一，见 `chat-history.ts` 的构造：

```js
const seen = new Set(items.value.map(i => i.id))
const fresh = earlier.filter(i => !seen.has(i.id))
items.value = [...fresh, ...items.value]
```

去重在**合并前**做，不能合并后再过滤——否则视口锚点会因节点被删而跳动。

### 4.4 渲染稳定性

分页与虚拟滚动叠加时，两件事必须成立：

| 约束 | 原因 |
| --- | --- |
| 窗口化只影响渲染，不影响数据 | 已加载的历史必须留在内存，否则滚回去要重新请求 |
| 未落库的乐观态消息要有独立标记 | `mergeHistory` 只处理服务端数据；本地刚发出的消息不能被分页结果覆盖 |

`transcriptWindow.ts` 的窗口是渲染区间。分页新增的数据**不得**因为落在窗口外
而被丢弃——那是数据层，不是渲染层。

### 4.5 大 `output` 的按需展开

配合 §3.6：列表接口不再返回 `output` 全文，只返回 `output_bytes` 与 `output_ref`。

工具卡默认显示：

```
read_file  src/auth/token.go  128KB   [展开]
```

点击后调 `/v1/chat/sessions/{id}/archives/{ref}` 取前 8KB 渲染，再点"继续加载"
按 `offset` 递增。

**不要预取**：一页 50 条工具调用预取全文，等于把刚省下的带宽又花回去。

### 4.6 缓存

- 会话消息按 `session_id` 缓存于 `tallyCache.ts`（已有机制）
- `ETag` 变化时整段失效，不做行级 diff——行级 diff 的复杂度换不来收益
- 切会话不销毁缓存，但限制总条数（建议 500 条/会话，超出按最旧淘汰）
- **缓存的键必须含 `tenant_id`**：同一浏览器可能切换账号，跨租户复用缓存是数据泄漏

### 4.7 归档查看

`read_archive` 的返回在 UI 里呈现为**可展开的原文块**，而不是新开面板。
理由：模型读到的是它，用户看到的也应该是同一份，否则排查时会两处对不上。

## 5. 安全

### 5.1 三条必守

| 风险 | 措施 |
| --- | --- |
| 跨租户读归档 | 每次读校验 `session.tenant_id`，失败返回 404（不是 403，避免探测存在性） |
| 归档路径穿越 | `storage_key` 由服务端生成，不接受客户端传入；拼接后必须 `realpath` 校验前缀 |
| 分段读取放大 | `limit` 服务端强制上限 64KB，忽略客户端更大值 |

### 5.2 不做的事

- **不在 URL 里放 token**：归档走正常 `Authorization` 头
- **不返回 `storage_key`**：客户端只见 `ref`，不见物理路径
- **不信任 `session_id` 来自 body**：一律从 path + auth claims 推导

### 5.3 审计

归档读取记一条结构化日志：`tenant_id` / `session_id` / `ref` / `offset` / `limit` / 结果大小。
不记内容——记内容等于把原文又抄了一份到日志系统。

## 6. 加载性能目标

| 指标 | 目标 | 测法 |
| --- | --- | --- |
| 首屏消息 | ≤ 300ms（P95，50 条） | 1000 条会话，冷缓存 |
| 上滚一页 | ≤ 150ms | 连续上滚 10 次 |
| 首屏工具调用 | ≤ 300ms（P95，50 条，**不含 `output`**） | 同上，断言响应体 < 2MB |
| 工具调用单条 | 响应体 ≤ 300KB | 构造 `output`=10MB 的工具，断言被截断 |
| 重复打开 | ≤ 50ms | 304 命中 |
| 网关内存 | 不随会话长度增长 | 同一会话翻 20 页，RSS 波动 < 10% |
| 翻页失败可重试 | 一次 5xx 后仍能继续 | 注入 502，断言 `hasMore` 保持 true |
| 翻页不串会话 | 竞态下无跨会话数据 | 慢响应 + 中途切会话，断言无混入 |

**必须避免的反模式**：

- 在网关把整段历史读进内存再切片。分页必须在 SQL 层完成（`LIMIT` + 游标）。
- 列表查询带 `output` 大字段。一页 50 条工具调用携带全文，等于把分页的意义抹掉。
- 用 `catch { hasMore = false }` 处理所有错误。它把"网络抖了一下"变成"历史没有了"。

## 7. 实施顺序

每步独立可上线，前 3 步完成后"可召回 + 必然落盘"即成立。

| 步 | 内容 | 依赖 | 风险 |
| --- | --- | --- | --- |
| 1 | 迁移 0016（`messages.seq`） | 无 | 低（只加列+索引） |
| 2 | `history_writer` 全角色入库（含 `tool` 角色）+ Redis 回写降级为回落层 | 1 | 中（写入路径变更） |
| 3 | 去掉 `INLINE_LIMIT` 短路 + `read_archive` + 权限 | 1,2 | 中（新增读面） |
| 4 | **迁移 0015（`tool_calls` 索引 + `output_bytes`）** | 无 | 低（只加索引/列） |
| 5 | **`output` 写入截断 + 列表查询去掉 `output`** | 4 | 中（响应形状变更） |
| 6 | 代理端点接上 `GetMessagesPage` + 游标换 `seq` + `ETag` | 1 | 低（复用既有实现） |
| 7 | **`tool_calls` 独立游标 + 错误不再吞** | 6 | 中（响应加字段） |
| 8 | **前端：竞态守卫 + 错误分类 + 去重** | 6,7 | 中（交互回归） |
| 9 | **前端：大 `output` 按需展开** | 5,8 | 中 |
| 10 | 迁移 0018（`message_archives`）+ 对象存储 | 3,5 | 中 |
| 11 | `session_folds` + 摘要式折叠 | 2,3 | 高（LLM 调用） |
| 12 | 分布式子 agent 配额 | 无 | 低 |

**第 4、5 步可以最先做**：它们与"可召回"无关，但却是加载性能最直接的改善，
且完全不碰写入语义。如果想先止血，从这两步开始。

**第 8 步不可省**：竞态守卫与错误分类是正确性问题，不是体验优化。漏了它们，
前面所有的分页改动都会在慢响应下出错。

### 7.1 第 2 步的迁移难题

现有 Redis 是**压缩后**的历史，无完整原文可回灌。两个选项：

- **(a) 强制旧会话开新档**——一次可见断档
- **(b) 从 PG 的 user/assistant 重建**——工具历史缺失

**推荐 (a)**：(b) 会造出"看起来完整、实际缺工具调用"的假 Durable，比断档更难排查。

## 8. 验收清单

| # | 承诺 | 验证方式 |
| --- | --- | --- |
| 1 | 压缩后 user/assistant/tool 原文都可按 ID 读回 | 三方会话触发折叠，逐个读回比对字节 |
| 2 | 3KB 小结果也被归档 | 小结果工具 → 折叠 → 读回，断言相等 |
| 3 | 清空 Redis 后消息不丢 | 删 `session_cache:*` → 重开会话 → 断言消息数不变 |
| 4 | 分页不重叠不遗漏 | 翻完所有页，断言 ID 集合 = 全集且无重复 |
| 5 | 304 生效 | 二次请求带 `If-None-Match`，断言 304 且空体 |
| 6 | 跨租户读归档被拒 | 用 A 租户 ref 在 B 租户调用，断言 404 |
| 7 | 分页在 SQL 层 | 抓慢查询日志，断言无 `SELECT *` 全量 |
| 8 | 摘要失败不丢历史 | 注入失败，断言消息数不减 |
| 9 | 子 agent 并发受配额约束 | 并发超限，断言拒绝数 = 超限数 |
| 10 | `tool_calls` 按会话查询走索引 | `EXPLAIN` 断言使用 `ix_tool_calls_session_created`，无 `Seq Scan` |
| 11 | 工具游标独立于消息游标 | 构造两表时间错位的会话，翻页后断言工具调用一条不漏 |
| 12 | 工具查询失败可见 | 注入查询错误，断言响应带 `tool_calls_partial: true` 且有 warn 日志 |
| 13 | `output` 写入被截断 | 构造 10MB 输出，断言库中 `output` ≤ 256KB 且 `output_ref` 非空 |
| 14 | 列表接口不返回 `output` 全文 | 断言 50 条工具调用的响应体 < 2MB |
| 15 | 翻页竞态不串会话 | 慢响应 + 中途切会话，断言 B 的列表不含 A 的条目 |
| 16 | 一次 5xx 后仍可重试 | 注入 502，断言 `hasMore` 保持 true 且重试后成功 |
| 17 | 翻页去重 | 构造重叠页，断言同一 `item.id` 只渲染一次 |
| 18 | 游标为空不死锁 | 服务端返回 `has_more:true` 但 `cursor:''`，断言状态自洽（不卡死） |

## 9. 与既有契约的关系

- **不改** `docs/transcript-contract.md` 的滚动契约（单写者 / 输入租约 / 锚点绑行）。
  分页只是给 `transcriptViewport` 提供更多"prepend 意图"，几何逻辑不变。
- **不改** `session_store.py` 的接口，但**改变其角色**：从"事实来源"降级为"投影缓存"。
  注释必须同步更新，否则后来者会继续把它当权威。
- **复用** `result_compactor.py` 的结构摘要（`json` / `lines` / `paths` / `head_tail`），
  它已经按内容形态选择策略，不需要重写。
