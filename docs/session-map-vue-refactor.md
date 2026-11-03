# 会话地图 Vue 重构方案

> 只写代码能证明的内容；与代码冲突时以代码为准。
> 引用方：`frontend-vue/public/sessionmap/`、`frontend-vue/src/views/ChatView.vue`、
> `frontend-vue/src/components/chat/sessionmapBridge.ts`、`internal/api/sessionmap.go`。

## 1. 现状

会话地图目前是一套**独立静态页**，通过 iframe 挂在 ChatView 上：

| 文件 | 行数 / 体积 | 角色 |
| --- | --- | --- |
| `public/sessionmap/app.js` | 2201 行 | 全部 UI 逻辑（画布、连线、inspector、详情、草稿、导入、统计） |
| `public/sessionmap/adapter.js` | 795 行 | 数据面缝合：劫持 `window.fetch`，把 `/synapse/api/*` 映射到 `/v1/session-map/*` |
| `public/sessionmap/styles.css` | 54 KB | 上游 dsh-synapse 样式，原样照抄 |
| `public/sessionmap/index.html` | 51 行 | 载体 + boot-error overlay |

宿主侧：

- `ChatView.vue:3400-3413`：`<iframe :src="synapseFrameSrc">`，`synapseFrameSrc`
  （`:1437`）把 `api` 与 `token` 作为 **query 参数**下发。
- `ChatView.vue:1484-1490`：宿主 → 地图的 `postMessage`。
- `ChatView.vue:1634-1652`：地图 → 宿主的消息泵，交给 `sessionmapBridge.ts`。
- `sessionmapBridge.ts`：4 种 RPC（create / send-message / fork / update-session），
  人工配对 `requestId`，失败回 `synapse:bridge-error`。

后端已经完整（`internal/api/sessionmap.go:5-9`）：
`GET/POST /v1/session-map/workspaces`、`GET/PUT/DELETE .../{id}`、`POST .../{id}/fork-node`。
**布局数据早已在后端**，前端不需要自己发明持久化。

## 2. 生产环境问题（按严重度）

### P0 —— 会出事，或已经出过事

1. **token 放进 URL**（`ChatView.vue:1437`）。桌面端 `file://` 场景把 `token` 写进
   `synapseFrameSrc` 的 query。URL 会进浏览器历史、Referer、反代访问日志。
   这是凭据泄露面，不是性能问题。
2. **全局劫持 `window.fetch`**（`adapter.js:449`）。把 `/synapse/api/*` 改写为
   `/v1/session-map/*`。一旦 CSP 收紧（`connect-src`）或引入 service worker，
   这条路径静默失效，表现为“地图空白、console 无错”——`index.html:27-43` 的
   boot-error overlay 就是为这类问题打的补丁，说明它已经发生过。
3. **postMessage 双向 RPC + 20 秒超时**。`sessionmapBridge.ts:14-16` 记录的根因是
   宿主漏实现三条回执，导致“新建会话/追问/分叉全部空等 20 秒”。协议靠字符串
   约定（`synapse:created-session` 与 `synapse:create-session` **名字不同**，
   `sessionmapBridge.ts:115-118` 专门注释），任何一侧改名都是静默超时。
4. **`app.innerHTML = ...` 全量重渲染**（`app.js:1424`，`render()` 在 `:1389`）。
   每次交互重建整棵 DOM：输入框焦点、文本选区、滚动位置全部丢失；卡片数量上来
   后每次按键都触发整页重排。这是“越用越卡”的直接来源。

### P1 —— 质量与安全面

5. **`innerHTML` 模板拼接**。`app.js` 有 59 处含标签的模板字符串、40 处
   `escapeHtml` 调用。手写转义是**逐处**的正确性，漏一处就是 XSS。上游含 markdown
   渲染（`app.js:589` `renderMarkdown`），输入直接来自用户与模型输出。
6. **无 i18n**。全部文案硬编码简体中文（`app.js` 里“新建会话”“过程记录”“已选”
   等）。仓库是 zh-CN / en-US / ar 三语，`check-i18n.mjs` 只扫 `src/`（`:99`），
   所以 `public/` 下的硬编码从来没被拦下。
7. **RTL 破坏**。`styles.css` 有 30 处 `left/right/margin-left/padding-right`
   物理属性。阿拉伯语界面下画布、面板、徽标会整体错位。
8. **无类型、无单测**。2201 行纯 JS，`state` 是一个 20 多个字段的对象字面量
   （`app.js:132-145`）。唯一的测试 `sessionmapFollowupCards.spec.ts:32` 是
   **读源码做正则匹配**（`readFileSync(... 'adapter.js')`），重构即失效。

### P2 —— 可维护性

9. **事件协议是 `data-action` 字符串**（`app.js` 里 49 处），靠
   `closest('[data-action="..."]')` 委托分发。拼写错误不会编译失败。
10. **两套状态并存**。宿主与 iframe 各有一份会话状态，靠 postMessage 同步
    （`synapse:current-session` / `synapse:live-reply`）。

## 3. 目标架构

### 3.1 结论

删除 iframe 与 `public/sessionmap/`，改成**同一 SPA 内的一个 Vue 视图**：

```
src/views/sessionmap/
  SessionMapView.vue          # 承载：顶栏 + 画布/详情切换 + 错误条
  components/
    MapCanvas.vue             # Vue Flow 画布（节点、连线、平移缩放、minimap）
    SessionCard.vue           # 单张卡片：标题、徽标、折叠按钮、追问/分支/归档
    CardInspector.vue         # 卡片详情侧栏
    ThreadDetail.vue          # 详情页（统计 + 消息流）
    DraftComposer.vue         # 草稿输入 + 快捷词
    ImportPicker.vue          # 导入会话弹窗
    WorkspaceSwitcher.vue     # 画布下拉
  composables/
    useSessionMapGraph.ts     # 卡片/连线投影（纯函数为主）
    useMapLayout.ts           # 位置、折叠、相机、localStorage
    useMapActions.ts          # 新建/追问/分叉/归档/元数据
  api.ts                      # /v1/session-map/* + /v1/conversations/*
  types.ts
```

### 3.2 三个关键取舍

**(a) 用 `@vue-flow/core`，不保留手写 SVG 画布。**

依赖已在 `package.json`，`WorkflowView` 已在用（`useWorkflowCanvas.ts:15`）。
现有手写实现包含相机、连线路由、碰撞检测、卡片挂载（`app.js:616-710`
`connectorPath*` / `placeConversationCards` / `layoutConversationGraph`）——
这些正是库已经解决的，且库自带 a11y 与键盘可达。

保留手写的代价是：重构后仍然要维护 400+ 行几何代码，且**没有任何一行有测试**。

**(b) 删除 `sessionmapBridge.ts` 与 postMessage，直接调 store/composable。**

这是本方案最大收益。地图与会话不再分处两个 JS Realm：

- 没有 `requestId` 配对、没有 20 秒超时、没有 `synapse:bridge-error`。
- 新建/追问/分叉直接 await 宿主已有的 API，错误就是普通 `try/catch`。
- 「当前会话」「运行中」「流式文本」由 Pinia 直接订阅，不再需要
  `synapse:current-session` / `synapse:live-reply` 推送。

**(c) token 不再进 URL。**

既然同源同 SPA，axios 实例（`src/api/index.ts`）已经带鉴权头。`synapseFrameSrc`
与 `desktopDeviceToken()` 在 URL 上的那一段整体删除。

### 3.3 数据流

```
SessionMapView
  └─ useSessionMapStore (Pinia)
       ├─ workspaces      ← GET /v1/session-map/workspaces
       ├─ activeWorkspace ← GET /v1/session-map/workspaces/{id}
       ├─ sessions        ← GET /v1/conversations（宿主已有）
       └─ layout (x/y/collapsed) → 防抖 800ms → PUT .../{id}（整体保存，天然幂等）
```

后端注释（`sessionmap.go:12-16`）已说明「整体保存 + 前端防抖」是既定策略，保持不变。

卡片位置仍可写 localStorage 做**首帧**占位（避免网络往返时空画布跳一下），
但**权威来源是后端 workspace**。现有键 `chiron-map:card-positions:v3` 保留读取，
避免用户升级后布局“归零”。

### 3.4 需要保留的兼容点

- **追问标记**：历史消息里已有 `【请简短回答问题】:({text})` 前缀
  （`sessionmapBridge.ts:89-90`）。协议消失后，这个**文本格式**仍要被解析，
  否则老会话里的追问会被显示成普通用户消息。
- **分支血缘**：`parent_session_id` / `branch_from_seq` 的展示
  （`app.js:34-48` `chironCardBadges`）依赖 `docs/session-map-branch-design.md`
  的列语义，保留。
- **localStorage 键名**：见 §3.3。

## 4. 功能清单（对等验收基线）

重构必须逐项对等。以下从现行 `data-action` 与渲染函数归纳：

| 区域 | 功能 |
| --- | --- |
| 画布 | 拖动卡片、平移、缩放（含百分比显示）、整理布局、定位当前会话、折叠子树 |
| 卡片 | 标题/别名/标签徽标、分支来源徽标、运行中脉冲、流式文本、追问/分支/详情/归档 |
| 详情页 | 消息流、过程记录（工具调用折叠）、会话用量统计 |
| 侧栏 inspector | 轮次、工具数、错误态、继续追问、创建分支、重命名/别名/标签 |
| 草稿 | 新建会话草稿、发送、取消、选区追问 |
| 快捷词 | 增删改、插入、上限校验 |
| 画布管理 | 多画布切换与新建、强制刷新、导入会话、删除画布 |
| 其他 | 亮/暗主题、错误提示条、空态、20 秒超时（**删除**，见 §3.2b） |

## 5. 实施阶段

每阶段独立可验证、可回退。

### 阶段 0：护栏先行（0.5 天）

- `check-i18n.mjs` / `check-rtl-logical.mjs` 的扫描范围加入
  `src/views/sessionmap/`（它们已扫 `src/`，新目录自动覆盖，无需改脚本，
  但要确认 `check-a11y.mjs:154` 的规则对画布元素不误报）。
- 建立 `src/views/sessionmap/__tests__/`，先写**投影纯函数**的用例。

**验证**：`npm run check:ui && npx vitest run src/views/sessionmap`。

### 阶段 1：数据层与类型（1 天）

- `api.ts`：6 个端点 + 会话列表/详情/fork/branch 的薄封装。
- `types.ts`：`SessionMapWorkspace` / `SessionMapNode`（对齐
  `sessionmap.go:46-80` 的 JSON tag，**不新增字段**）。
- `useSessionMapGraph.ts`：`threads[] → cards[] + edges[]` 的纯函数投影
  （对应 `app.js:747-975`）。这一层是重构的核心价值：它可以从旧代码逐条对照。

**验证**：投影函数单测（构造 threads，断言卡片数、父子连线、折叠后可见集合）。

### 阶段 2：画布与卡片（2-3 天）

- `MapCanvas.vue`（Vue Flow）+ `SessionCard.vue`。
- 布局持久化：防抖 PUT；首帧读 localStorage。
- 键盘可达与 `aria-label`（对齐现有 `data-action` 上的中文 aria-label）。

**验证**：`vue-tsc`；组件测试覆盖拖拽落点、折叠、选中。

### 阶段 3：详情与操作（2 天）

- `CardInspector.vue` / `ThreadDetail.vue` / `DraftComposer.vue` / `ImportPicker.vue`。
- `useMapActions.ts`：新建、追问、分叉、归档、元数据更新。
  **直接调用**宿主 API，删除 `sessionmapBridge.ts`。

**验证**：动作层单测（mock api），断言：追问带上 `FOLLOWUP_TEMPLATE`、
分叉传 `atSeq`、失败向上抛而不是静默吞。

### 阶段 4：切换与删除（1 天）

- `ChatView.vue`：`synapseMapOpen` → 新组件；删除
  `synapseFrame` / `synapseFrameSrc` / `MAP_SOURCE` / 消息泵 / `sessionmapBridge.ts`。
- 删除 `frontend-vue/public/sessionmap/` 整个目录（6 个文件）。
- 删除/改写 `sessionmapFollowupCards.spec.ts`（它读源码，必然失效）。

**验证**：`npm run build`（含 `check:ui`）、`npx vitest run`、`vue-tsc -b`。

### 阶段 5：i18n 与 RTL（1 天）

- 全部文案进 `locales/{zh-CN,en-US,ar}/chat.ts` 的 `sessionmap` 段。
- 样式用逻辑属性（`inset-inline-start` 等），`check-rtl-logical.mjs` 必须通过。

**验证**：`node scripts/check-i18n-keys.mjs && node scripts/check-rtl-logical.mjs`。

合计约 **7-8 个工作日**。

## 6. 风险

| 风险 | 处置 |
| --- | --- |
| 功能回归（2201 行，功能面广） | §4 清单逐项打勾；阶段 2-3 保留旧地图可切回，阶段 4 才删 |
| 画布交互手感变化 | 用 Vue Flow 前，先在阶段 2 用真实会话数据（≥50 卡片）对比拖动/缩放 |
| 后端契约漂移 | `api.ts` 的字段名以 `sessionmap.go` 的 JSON tag 为准，两侧同步改动 |
| 桌面端 `file://` 路径问题（`ChatView.vue:1428-1431` 有真机踩坑记录） | 不再有 iframe，该问题自然消失；但需回归一次桌面端打包 |

## 7. 验收

1. §4 功能清单逐项通过。
2. `npm run check:ui`、`vue-tsc -b`、`npx vitest run` 全绿。
3. `public/sessionmap/` 不存在，仓库内无 `synapse/api`、`synapse:` 协议残留
   （`grep -rn 'synapse:' src/` 为空）。
4. token 不出现在任何 URL / query 中。
5. 50 卡片画布上拖动、缩放、输入草稿无整页重渲染卡顿。
