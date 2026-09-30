# Transcript 契约

> 本文把 `frontend-vue/src/components/chat/` 下 10 个模块**已经在代码注释里写对的不变量**固化为
> **契约 + 验收清单**。形式参照 Reasonix 的 `docs/TRANSCRIPT_SCROLL_CONTRACT.md`。
>
> **为什么值得固化**：transcript 是"只能靠肉眼与手感验证"的领域。契约的作用不是规定实现，而是让人
> 知道**哪些行为是承诺**——改动时知道什么不能破，以及**怎么验证没破**。这些不变量原本散在代码注释里，
> 每个人改这块都得重新读一遍才能避免踩坑。

## 0. 模块地图

| 模块 | 职责 | 触碰 DOM |
|---|---|---|
| `transcriptViewport.ts` | **唯一**允许写消息列表 `scrollTop` 的地方 | 是（唯一） |
| `transcriptAnchor.ts` | prepend 时的锚点保持（纯几何） | 否 |
| `transcriptWindow.ts` | 窗口化的几何与渲染区间 | 否 |
| `transcriptMeasurementLedger.ts` | DOM 实测尺寸的**暂存 → 原子发布** | 否 |
| `transcriptProjection.ts` | chat item 序列 → 行序列 + 回合/工具组结构 | 否 |
| `transcriptFolds.ts` | 折叠状态持久化（按会话分键） | 否 |
| `transcriptSearch.ts` | 会话内文本检索 | 否 |
| `chat-types.ts` | item 类型 + `splitThinking` 等纯解析 | 否 |
| `chat-history.ts` | 落库历史 → item 序列 | 否 |
| `MessageList.vue` / `MessageItem.vue` | 渲染（消费上述契约） | 是 |

**分层原则**：几何与状态计算**全部**是纯逻辑（无 DOM、可确定性测试），只有 `transcriptViewport` 与两个
Vue 组件碰 DOM。这条是能用"事件序列回归测试"验证滚动行为的前提。

## 1. 不变量（承诺）

### 1.1 滚动：单写者 + 输入租约 + provenance（`transcriptViewport`）

1. **单写者**：分页 prepend、流式追加、折叠、跳转**都只提交"意图"**，由本模块写滚动位置。
   别处直接写 `scrollTop` 一律视为缺陷。
2. **输入租约**：用户滚动期间持有租约，**此期间程序写入被拒绝**。违反表现：用户正读历史时被
   "自动跟随底部"抢走位置——这是本项目历史上"上滚后内容跳动"的根因之一。
3. **writer provenance**：程序写入登记为 `pending`，直到匹配的原生 `scroll` 事件被消费；**落点不符
   则判定为用户滚动**并立即进入租约。这是区分"我写的"与"用户滚的"的唯一依据。
4. **时间可注入**：便于用事件序列做确定性回归。

### 1.2 锚点：绑行，不绑总高度（`transcriptAnchor`）

prepend 后保持视口位置时，**视口绑在锚点行自身**，而不是 `scrollTop += scrollHeight 增量`。

* 为什么：高度差法把视口位置绑在**总高度**上，只要锚点**下方**内容异步变化（图片加载、markdown
  增高、折叠展开），补偿值就错了 —— 表现为上滚加载后跳动。绑行只依赖该行位置，对其它行免疫。

### 1.3 窗口：两段并集 + 覆盖优先 + 身份稳定（`transcriptWindow`）

1. **渲染区间是两段的并集**：
   * 主窗口 `[start, end)`：视口 + 上下预渲染，随滚动移动；
   * 常驻尾部 `[tailStart, tailEnd)`：活跃轮与最近若干轮，**永远挂载**。
   * 违反表现：当成一个连续区间 ⇒ 滚到历史中段会把中间所有行一并渲染（**窗口化形同失效**）；
     只渲染主窗口 ⇒ 活跃轮在上滚时被卸载。
2. **覆盖优先**：主窗口必须覆盖视口，否则调用方**必须退回全量渲染**（`coversViewport`）。
3. **身份稳定**：区间用**下标**表达、由 key 集合重新计算，**不就地改写已挂载的行**。
4. 行尺寸优先取**实测值**；未测量时用 kind 估计值 —— 估计只影响窗口边界**裕度**，不影响正确性。

### 1.4 投影：key 一致 + 用户消息永不隐藏（`transcriptProjection`）

1. **行 key 必须与 `itemKey` 逐字符一致**（锚点与窗口都按 key 认身份，**改名等于换行**）。
2. **用户消息永不隐藏**：折叠只能收起思考/工具/助手正文，**不能把提问藏起来**。
3. `flat` 与 `grouped` 双模式：`flat` 与 items 一一对应（不注入头行、不隐藏行），用于安全接入渲染
   路径**而不产生任何几何变化**；`grouped` 才产生折叠头与隐藏行。

### 1.5 测量：代际不混用（`transcriptMeasurementLedger`）

DOM 实测尺寸**先进暂存区，再一次性发布为不可变快照**：

* 为什么不能边测边改：前缀偏移（`transcriptWindow.buildGeometry` 的输入）是所有窗口与锚点计算的
  **共同输入**，逐条写入会让同一帧内不同计算读到"**半更新**"的几何（**测量代际混用**），表现为滚动中
  偶发跳动或空白；
* 因此 `stage()` 只暂存、`publish()` 原子提交；**无实际变更时返回 null**，让调用方跳过该次重渲染；
* 切换会话/整批作废时用 `discard()`。

### 1.6 折叠：按会话分键 + 静默降级 + 有界（`transcriptFolds`）

1. **按会话分键**：折叠是"我在这个会话里读到哪儿"的阅读状态；跨会话沿用会让新会话继承上一会话的
   展开态，看起来像随机展开。
2. **静默降级**：读写失败（隐私模式、配额满、Safari 下访问 `localStorage` 本身抛异常）降级为内存态
   —— 折叠态不值得为它打断渲染。
3. **有界**：单会话最多记 `MAX_FOLDS_PER_SESSION`（100）条，防止长会话撑满 `localStorage`。

### 1.7 检索：只搜正文与思考（`transcriptSearch`）

只搜**正文与思考**；工具参数/结果是机器输出，体量大且形态各异，掺进结果会让"我刚说过什么"被噪声
淹没 —— 需要查工具输出时用全局搜索（`/v1/search`）。

### 1.8 思考通道：两条通道不合并（`chat-types` + `ChatView`）

`[thinking]` 这个形态有**两个来源**，契约要求它们各走各的：

| 来源 | 通道 | 处理 |
|---|---|---|
| **native reasoning**（引擎的 `chunk.reasoning_content`） | SSE 事件 `type=thinking` | 直接进 `reasoning` 条目（`onThinkingChunk`），**绝不进 `streamBuf`** |
| **模型自产**（prompt 教模型输出的标记） | 就在正文 `text` 里 | 由 `splitThinking` 解析；**绝不吞内容**（正文里提到 `[thinking]` 字样必须完整保留） |

* 违反表现：把 `thinking` 增量塞进 `streamBuf` ⇒ `splitThinking` 把它当正文，刚分开的两条通道又合回去；
* 这是追赶批次的 A1（见 `vendor/规划.md` §3.3）：此前引擎**把 native reasoning 包装成与模型标记相同的形态**混进 `text`，消费方
  只能猜"谁包的"。

## 2. 依赖方向

```text
chat-types / chat-history（纯解析）
        ↓
transcriptProjection（item → 行）
        ↓
transcriptWindow（几何） ← transcriptMeasurementLedger（实测尺寸）
        ↓
transcriptAnchor（锚点） / transcriptSearch / transcriptFolds（旁路）
        ↓
transcriptViewport（唯一写 scrollTop）
        ↓
MessageList.vue / MessageItem.vue（渲染）
```

**规则**：箭头单向。下层不得反向依赖上层（例如 `transcriptWindow` 不得 import `transcriptViewport`）。

## 3. 验收清单

改这块时按下表验证；表中"自动"指已有测试覆盖（`src/components/chat/__tests__/`）。

| 场景 | 期望 | 验证 |
|---|---|---|
| 上滚加载历史后位置不动 | 视口停在原锚点行 | 自动（锚点纯几何测试）+ 手测 |
| 流式追加时用户正在上读 | 位置**不被抢走** | 自动（viewport 事件序列） |
| 滚到历史中段 | 只渲染两段并集（DOM 行数受限） | 手测 + 行数断言 |
| 图片/折叠在锚点下方异步变化 | 无跳动 | 手测 |
| 同一帧内多次测量 | 计算只读到已发布快照 | 自动（ledger 测试） |
| 折叠状态跨会话 | **不**互串 | 自动（folds 测试） |
| 长会话折叠 | 不超过 100 条/会话 | 自动 |
| 正文含 `[thinking]` 字样 | 完整保留，不吞内容 | 自动（`chat-types.spec.ts`） |
| 切会话 | 测量作废、不带着上一会话几何 | 手测 + `discard()` 断言 |

## 4. 已知取舍与不做的事

* **不做**：把窗口化换成"全部渲染 + CSS 虚拟化"——当前两段并集已能表达"活跃轮常驻"，换法会丢掉
  覆盖优先与身份稳定这两条不变量；
* **不做**：把折叠态迁到服务端 —— 它是纯阅读状态，不值得一次网络往返；
* **不做**：给 `transcriptSearch` 加工具输出 —— 那正是全局搜索的职责；
* 估计尺寸（未实测时的 kind 估计）**只影响裕度**，若将来发现边界抖动，应先补实测而不是调估计。
