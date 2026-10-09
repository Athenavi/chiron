# 归档：reasonix-ui-ux-gap-analysis.md

> 全文归档（2026-10-09）。`docs/reasonix-ui-ux-gap-analysis.md` 现为**结论+行动项摘要**，正文在此。
> 移动原因：`docs/` 只保留绝对核心且当前有效的文档；这类对照/评估的完整过程留档备查。

---

# Chiron 与 Reasonix 2.x 的 UI/UX 差距分析

> **这份文档是 [Chiron 与 Reasonix 的差距分析](../reasonix-gap-analysis.md) §8 的展开**，对照目标同为
> `vendor/DeepSeek-Reasonix` 的 `studio` 分支（Reasonix 2.x，HEAD `c47bdfd84`，2026-10-08）。
>
> **取证强度**：【枚举】逐文件/逐符号读出 · 【文档】读契约原文 · **【实测】** 本次真的跑过。
> 本次**实测**了两项：Chiron 的 6 个 UI 门禁（全 exit 0、基线全空）与前端测试
> （**57 文件 / 480 用例通过，exit 0**）。
> 其余为**静态对照**：**没有**在浏览器里跑过、**没有**做性能剖析 —— "代码里这么写"不等于"手感如此"。
>
> **口径先声明**：本文比较的是 **Vue 3 + Ant Design Vue 的 Web SaaS** 与 **Electron 桌面 + React 19 SPA**。
> 三条**不可比**（依 `vendor/规划.md` §6 的"不做"清单）：桌面 OS 集成（托盘/自更新/包签名）、
> 主题包的 ZIP 发行形态、TUI。**UI/UX 的差距不等于要重写渲染** —— 本篇的每条建议都是
> **文档化 / 机械化 / 消除重复**，不涉及换框架。

## 0. 结论摘要

| # | 结论 | 强度 |
|---|---|---|
| 1 | **Chiron 的 UI 静态纪律不落后，甚至更干净**：6 个门禁（z-index/主题/动效/i18n/i18n-keys/a11y）基线**全部为空**、`build` 阻断、实测 exit 0 | 【实测】 |
| 2 | **transcript 的"法"两边都有，只是放的地方不同**：Chiron 在 `docs/transcript-contract.md`（**11,671 B / 165 行**，2026-10-09 再测）+ **7 个纯几何模块**（合计 **37,880 B**，见 §4.4 那张表；两者**不要混为一个数**）；2.x 在 **`styles/LAYOUT.md`** 的 §Virtualisation / §The locator rail。1.x 的 `TRANSCRIPT_*` 文档族确已退役，但**不是没写，是搬到了 CSS 旁边** | 【文档】【枚举】 |
| 3 | **真正的差距是"验证"**：Chiron **零浏览器/e2e 测试**，测试/源码字节比 **0.11×**（57 文件 / 244 KB）；2.x SPA **0.77×**（366 文件 / 1.98 MB），含 **179 个 interaction test** + `MockPort` 内核模拟 + **16 条 census 不变量** + Electron live 测试 | 【枚举】 |
| 4 | **契约与实现已经不一致**：`docs/transcript-contract.md` §1.1 规定"唯一写 `scrollTop` 的模块"，而 `ChatView.vue` 有 **3 处**直接写 —— **其中 2 处写的是 transcript 本身（2026-10-09 已修，并落地源码级门禁）**，第 3 处写的是 `.unified-list`（另一个容器）；统一任务模式还绕开投影/窗口化/折叠，成为**第二条渲染路径**（未做） | 【枚举】 |
| 5 | **最大的可搬运机制是"接缝"，不是工具**：2.x 用一个 `AgentPort` 契约（35,620 B）+ `MockPort`（30,959 B）把内核换成可脚本化的假实现 ⇒ 才可能有 179 个 DOM 级交互测试。**补它不需要新依赖**（Chiron 已有 jsdom + @vue/test-utils） | 【枚举】 |
| 6 | **2.x 的文档分层值得学**：`app.css`（320 KB）**不带注释**，理由全部写在 `styles/LAYOUT.md`（**90,649 B / 1,439 行 / 56 节**）+ `styles/TOKENS.md`；而 Chiron 的知识散在 `style.css` 的 60 行注释与组件注释里 | 【枚举】 |

---

## 1. 可比性与口径

| | Chiron | Reasonix 2.x |
|---|---|---|
| 形态 | Web SPA（浏览器 / nginx 反代） | Electron 桌面壳 + 本地 Go 内核（回环 HTTP/SSE） |
| 框架 | Vue 3.5 + **Ant Design Vue 4.2** | **React 19** + TS 7 + Vite 8 |
| 用户面 | 多租户 SaaS：20 条路由 + 30 屏管理台 | 单机单用户：图标栏 + 多窗格 + 设置页 |
| UI 状态来源 | 网关 SSE（`EventSource`）+ 会话内 Map | Go 内核回环 SSE；**渲染进程不持有任何状态副本** |
| 主题 | 4 套包 × 明暗 = 8 块 CSS + antd token 注册表 | 主题包（Go 侧校验）+ `tokens.css` + `app.css` |

**口径警告**：2.x 的"组件数"不能直接比 —— 它的 `src/ui/` 有 571 文件，其中 **255 是测试**，
**173 个非测试 `.tsx`**；Chiron 的 `components/` 有 100 文件（含 34 测试）。

---

## 2. 量表（本次实测）

| 维度 | Chiron | Reasonix 2.x SPA | 说明 |
|---|---|---|---|
| 前端源码 | `frontend-vue/src` **266 文件 / 2.52 MB** | `desktop/frontend-next/src` ts/tsx **768 文件 / 4.56 MB** | 含测试；非测试 402 文件 / 2.58 MB |
| 逻辑落点 | **`views/` 943 KB > `components/` 769 KB** | `ui/` 571 文件，最大组件 44 KB | Chiron 的逻辑长在视图里 |
| 最大单文件 | `ChatView.vue` **148,405 B / 3,429 行** | `Settings.tsx` **44,005 B** | Chiron 的最大文件是 2.x 的 **3.4×** |
| 测试 | **57 文件 / 244,075 B**（6,271 行） | **366 文件 / 1,979,839 B** | |
| 测试/源码（字节） | **0.11×** | **0.77×**（ts/tsx 口径） | 同口径对比 |
| 交互级测试 | **0**（无浏览器测试） | **179 个 `.interaction.test.tsx`**（1,189,279 B，占测试字节 60%） | 2.x 的关键差异 |
| 样式层 | 单文件 `style.css` 51,192 B / 1,532 行 | `styles/` **26 文件 / 709,342 B**（`app.css` 320,220 + `studio.css` 201,356 + 说明文档 90,649 + 17 个测试文件） | |
| 语言 | 3（zh-CN / en-US / **ar，RTL**）；`src/` 内硬编码中文 0 | SPA **2**（zh + en，**中文原文即 key**，缺译文回退中文）；内核/CLI 另有一套 3 语言 Go 目录 | Chiron 多一个 **RTL** 面 |

---

## 3. 先立事实：Chiron 的强项（避免"越比越差"的错觉）

1. **transcript 契约是资产，不是负债**。【文档】`docs/transcript-contract.md`（**157 行**，2026-10-09 复测；原记 142 行）给出 9 组不变量
   （§1.1 单写者 + 输入租约 + provenance、§1.2 锚点绑行不绑总高度、§1.3 两段并集 + 覆盖优先 + 身份稳定、
   §1.4 key 一致 + 用户消息永不隐藏、§1.5 测量代际不混用、§1.6 折叠按会话分键 + 静默降级 + 有限、
   §1.7 只搜正文与思考、§1.8 两条思考通道不合并）+ 9 行验收清单 + 单向依赖图 + 明确的"不做"清单。
   **2.x 的对应物在 `src/styles/LAYOUT.md`**（§Virtualisation、§The locator rail）—— 1.x 的契约族退了役，
   渲染法被写进 CSS 旁边的"布局法"（见 §4.10）。顺带一条**他们自己的漂移**：`docs/SPEC.md:646` 引用了
   "(§ rendering)"，而 SPEC.md 里**没有**渲染章节。
2. **7 个纯几何模块**（合计 **37,880 B**，2026-10-09 再测；此前记 36,842 B；最大 `transcriptProjection.ts` **16,865 B**，此前记 15,817 B）把 DOM 隔离在
   唯一一处（`transcriptViewport.ts:86`）。【枚举】这是"可用事件序列做确定性回归"的前提。
3. **6 个 UI 门禁全部零存量、且阻断构建**（2026-10-09 增为 **7** 个：新增 transcript 滚动单写者守卫）。【实测】`package.json` 的
   `build = npm run check:ui && vue-tsc -b && vite build`；本次 7 个脚本逐条 exit 0，
   输出均为"存量 0 处 / 0 文件"。门禁覆盖：裸 `z-index`、硬编码颜色、硬编码时长、
   `src/` 内 CJK 字面量（含 vue-i18n 消息编译探针 + CJK 字面量 key 守卫）、
   键集 1:1 对齐、a11y 两条规则。**2.x 的 `repolint` 基线反而带 471 个文件的存量预算。**
4. **i18n 面更宽也更严**：3 语言 × 14 文件，`ar` 是 RTL，`languages.ts` 是
   `<html lang/dir>` + antd + dayjs 的单一来源（避免 RTL 闪动），源语言 zh-CN 且键集强制 1:1，
   `src/` 内硬编码中文 **0 行**（本机独立复算确认）。【枚举】
5. **流式渲染的取舍是对的**：流式期渲染**纯文本 + 光标**（`MessageItem.vue:438-443`），
   结束后才 `v-html` styled markdown（`:451-456`）；`html:false` + DOMPurify 白名单 + `mdCache`（上限 300）
   + 超大消息（>30,000 字符）走 `requestIdleCallback` 并降级纯文本。**2.x 的 markdown 处理没有更高级**。

---

## 4. 差距清单（每条都带证据与动作）

### 4.1 契约与实现不一致（**transcript 侧已于 2026-10-09 修复并加门禁**；剩余一条按 §6 保留为已文档化差异）

* **单写者被破坏 3 处 → 已修 2 处（2026-10-09）**：`docs/transcript-contract.md` §1.1 声明"分页 prepend、流式追加、折叠、跳转
  都只提交意图，由 `transcriptViewport` 写滚动位置；别处直接写 `scrollTop` 一律视为缺陷"。
  实测 `ChatView.vue:1045`、`:1150`、`:1850` 各有 `scrollTop = scrollHeight` 直写。
  **逐处复核后**：`:1045` / `:1850` 写的是 **`.message-list`（就是 transcript 本身）** ⇒ 真违规，
  **已改为** `messageListRef.value?.scrollToBottom()` —— `MessageList.vue` 新增 `defineExpose`，
  内部走 `vp.release()` + `vp.follow(el)`（"用户显式要求回底：租约作废，跟随立即生效"）；
  `:1150` 写的是 **`.unified-list`**，**是另一个容器**（统一任务模式的列表，不经 `transcriptViewport`）
  ⇒ **不属本契约违规，保留**，但它正是下面那条"第二条渲染路径"的一部分。【枚举】
* **第二条渲染路径**（未变）：统一任务模式在 `ChatView.vue:2691-2718` 用普通 `v-for` 直接渲染 `MessageItem`
  —— **没有投影、没有窗口化、没有折叠**，而常规路径走 `MessageList.vue:540-579`；
  它也因此自带一套"写 `.unified-list` 的 `scrollTop`"。【枚举】
* **动作**：① 两处 transcript 直写**已按"提交意图"改完**（见上）；② 统一任务模式改为复用
  `MessageList`（`SessionPreviewPane.vue` 已证明可复用 `mergeHistory` + `MessageList`）—— **本文不再作为行动项**：
  `vendor/规划.md` §6 明确*"不把 UI/UX 差距读成'要重写渲染'（那是**文档化**，不是重构）"*，并已把本条列为
  **实例**。理由：这是对 3,429 行 `ChatView.vue` 渲染路径的重构，而**本环境无法做视觉验证**（无浏览器），
  风险与收益不成比例；"两条渲染路径并存"作为**已文档化的事实**保留，滚动侧另有源码级门禁守住。
  真要合并属**产品决定**（收益 = 统一投影/窗口化/折叠；代价 = 大重构 + 需要真机视觉回归）。
  **门禁已落地**：`frontend-vue/scripts/check-transcript-scroll.mjs`（已接进 `check:ui`）—— 枚举 `src/**` 下
  所有 `scrollTop` **赋值**并逐条归类（单写者 / 写的是**别的**容器+理由 / 测试豁免），**未归类即失败**。
  刻意**不**采用本文原提法"`ChatView.vue` 内不得出现 `scrollTop =`"：那会对 `.unified-list` 那处**假阳性**
  —— 契约管的是**消息列表**，不是所有列表；**口径不精确的门禁比没有门禁更糟**（会被上调基线而失效）。

### 4.2 验证面：这是最大的系统性差距

* Chiron：**没有 Playwright/Cypress/Puppeteer**，没有浏览器测试；`docs/transcript-contract.md` §3 的
  9 条验收里 **5 条标"手测"**（上滚加载、历史中段窗口化、锚点下方异步变化、切会话、图片加载），
  这些恰恰是"只能靠肉眼"的部分。【枚举】
  **⚠ 2026-10-09 更正：这句的两个数字都错了** ✗ —— §3 的表里其实只有 **4 行**带"手测"，而且
  **这 4 行现在全部有测试**（`transcriptAnchor.spec.ts:21/28/46` · `MessageList.windowing.spec.ts:40/63` ·
  `transcriptMeasurementLedger.spec.ts:56`）⇒ 按 §3 自己的口径它们已是"自动"。**结论要相应减弱**：
  "没有浏览器级端到端测试"**仍然成立** ✓，但"**9 条验收里 5 条只能靠肉眼**"**不成立** ✗ ——
  真正的差距是**验证阶梯的层次**（缺 DOM 级 interaction test 与真浏览器 smoke），不是"没有自动化"。
  **2026-10-09 补一处已闭合的具体缺口（顺手量的）**：第 113–115 轮新加的三种留痕
  （护栏拦截 / 上下文压缩 / 中断）当时**只在投影层有测试**（`chatHistory.test.ts` 断言"产生了 `notice` 条目"），
  而 `MessageItem.vue` 里那条 `v-else-if="item.kind === 'notice'"` **从没被渲染过** ✗ ——
  少个分支、`role` 掉了、tone class 拼错，测试都不会红。已在 `MessageItem.spec.ts` 补两条 **DOM 级**渲染用例
  （warning/info 的文案 + `role="status"` + tone class）⇒ **投影与渲染两半都有测试** ✓。
  **教训**：**新增一个 UI 分支时，要测"分支本身"，而不是只测喂给它的数据** ——
  投影测试证明"条目产生了"，只有渲染测试才证明"那一行真的出现了"。
  **2026-10-09 再进一步：补了第一条真正的「交互级」测试（仍然零新依赖）** ——
  `src/views/__tests__/ChatView.interaction.spec.ts`（**3 条**）。**能做的原因**：`createSSEConnection`
  的契约是**回调式**（`(sessionId, onMessage, onError, opts)`，`api/index.ts:310`）⇒ 替身只要**把 `onMessage`
  存下来**，测试就能直接喂帧 ⇒ **不需要 Playwright/Cypress，也不需要真的 EventSource** ✓。
  覆盖的链：**输入 → 提交 → 建流 → 收到帧 → 落成 transcript 条目**（冒烟网把重子组件全打桩，
  **看不到任何 SSE 分支**）。三条断言：① 提交真的建了流 + 用户消息进列表；② `guardrail_blocked` 帧 ⇒
  **warning 通知行**；③ `compaction` 帧 ⇒ **info 通知行** ⇒ 第 113–115 轮那三条留痕从此**端到端**有测试 ✓。
  **两个契约细节我第一版都写错了**（都已修）：`createSSEConnection` **内部已经 `JSON.parse`**
  （`api/index.ts:335`）⇒ `onMessage` 收到的是**对象**不是字符串；帧形状是 `{ type, data }` 而非平铺 ✓。
  **且一次挂载会建多次流**（会话映射 / 子代理流）⇒ 只存**最后一个** `onMessage` 会**喂错流** ✗ ⇒ 改为存**全部** ✓。
  **顺带浮出一个被掩盖的形状错误** ✓✓：把 `ChatInput` 解桩后立刻炸
  `TypeError: models.value.map is not a function` —— 因为 `ChatInput.vue:160` 是
  `models.value = await listModels()`，它要的是**数组本身**，不是 axios 的 `{ data: { data: [] } }` 外壳；
  **冒烟网从来没见过它，因为那里把 `ChatInput` 打桩了** ✓。修正替身后运行**无 unhandled error** ✓。
* 2.x 的验证阶梯（四层，全部有对应文件）：
  1. **`MockPort`**：`src/port/mock.ts`（30,959 B / 791 行）实现 `AgentPort`（`port.ts` 35,620 B / 582 行），
     **忠实模拟内核语义** —— 遇到 `approval_request`/`ask_request` 会像真内核一样阻塞
     （注释：*"the script pauses on approval_request/ask_request the same way the real run blocks on
     Approve()/AnswerQuestion; nothing advances until answered"*），并按内核规则给帧编号（`seq`）。【枚举】
  2. **179 个 interaction test**（DOM 级，`@testing-library/react` + jsdom）；
     代表性一例 `pane-startup-reads.interaction.test.tsx` 断言"启动期每个资源**恰好读一次**、
     恢复（`gap()`）后**不得重读 models**" —— 这是**读调度**这种玄学问题的机器化。【枚举】
  3. **`ui-census`**：33 个模块的**静态源码分析**（不是截图、不是快照），
     `gate.mjs` 定义 **16 条必须为 0 的不变量**（`MUTATION_WITHOUT_WITNESS`、`UNDECLARED_MUTATION`、
     `CAPABILITY_SILENTLY_LOST`…）；两条"已知未闭合"的口子**只报不拦**
     （注释：*"a gate that fails on known-open debt is a gate someone turns off"*）。【文档】【枚举】
  4. **Electron live 测试**：`test/smoke.js`（17,789 B）把**托盘的显示值对照内核的新读**，
     `browser_live.js`（11,700 B）与 `computer_live.js`（7,874 B）驱动真实内核；
     另有 `unit.mjs`（79,801 B）。【枚举】
* **动作（不引浏览器依赖的路径）**：Chiron 已有 jsdom + `@vue/test-utils`。缺的是**接缝**：
  在 `frontend-vue/src` 里为"网关 + SSE"定义一层端口契约（`GatewayPort`），
  提供 `MockGateway`（可脚本化发事件、可暂停在审批/提问、可按 `seq` 编号），
  再把 §3 的 5 条手测逐条写成组件级交互测试。**这不需要新依赖**；
  是否需要真浏览器（Playwright 等）仍是 `docs/development-roadmap.md` §3 记录的**产品决定**。

### 4.3 每 delta 的重复工作（可测、可省）

* **流式期每个 delta 都做全量 markdown + sanitize**：`MessageItem.vue:311-343` 的 `scheduleRender`
  **没有 `streaming` 判断**，每次增量都对**累计缓冲区**跑 `md.render` + `DOMPurify.sanitize`（`:328`/`:280`），
  并往 `mdCache` 插一条（上限 300）；而流式期显示的是纯文本，**这份结果到回合结束才用得上**。
  同时 `throttleRaf`（`chat-types.ts:294-309`）**零调用点** —— 帧批处理是现成的却没接。【枚举】
* **窗口化模式下每次更新都全量测量**：`MessageList.vue:290-294` 的 `onUpdated` 触发
  `measureMountedRows()`（`querySelectorAll` + 逐行 `offsetHeight`）+ `recomputeWindow()`；
  只有滚动触发的重算被 rAF 节流（`:271-282`），**流式驱动的更新没有**。【枚举】
* **动作**：给 `scheduleRender` 加 `streaming` 早退（流式期不产出 styled 结果、不进缓存），
  回合结束再算一次；把 `onUpdated` 的测量改为 rAF/微任务合并（`throttleRaf` 已在仓库里）。
  验收：组件测试断言"流式 N 次 delta 只调用 1 次 `md.render`"。
  **✅ 前半已落地（2026-10-09）**：`MessageItem.vue` 的 `scheduleRender` 加了 `isStreaming` 早退 ——
  流式期**不跑** `md.render` + `DOMPurify.sanitize`、也**不进** `mdCache`。**⚠ 这里有一个最容易漏的回归点**：
  `displayContent` **不依赖 `streaming`**（它只看 `content` 与折叠状态）⇒ watcher 必须**同时监听
  `isStreaming`**，否则"流式结束"那一刻不会重算、消息会**一直停在纯文本**。用例 `MessageItem.spec.ts`
  的「流式期不跑 markdown/sanitize；回合结束才渲染一次」正是按 §4.3 的验收写的（盯**模块级**
  `DOMPurify.sanitize`，因为 `renderMarkdown` 是局部函数、spy 不到），并额外断言结束那刻**真的走了
  `v-html` 分支**（`<p>abcde</p>`）⇒ vitest **57 文件 / 487 用例全绿**。
  **后半 ✅ 也已落地（2026-10-09）**：`MessageList.vue` 的更新路径改用仓库里**现成的 `throttleRaf`**
  （此前**零调用点**）合并「测量 + 窗口重算」—— 同一帧内多次 `onUpdated` 只测一遍；
  `syncActiveQuestion()` 仍**同步**执行（它轻，且提问导航条随内容即时更新是可见行为）。
  顺手给 `throttleRaf` 补了 `setTimeout` 退路（与 `scheduleWindowRecompute` 同款，免得在无 rAF 的环境"一接进来就崩"）。
  **⚠ 这条**不适合**用组件级测试钉**：真 jsdom 的 rAF 在 `await nextTick()` 之间就会落地 ⇒
  "同一帧内合并"在那里**观察不到**（我第一版就是这么写的，实测 4 次更新各测了一遍 ✗）⇒
  改成**直接单测 `throttleRaf`**（用**受控的 rAF 替身**控制帧边界）✓ —— 这也顺带说明
  **可观察性取决于运行环境的帧模型**，这类"按帧合并"的判据要么控制帧、要么别用组件测。
  另注：测量本身会写响应式状态（`measuredSizes`）⇒ 触发下一轮 `onUpdated`，是个**收敛过程**
  （实测 4 次更新在几帧内共跑 7 遍），所以判据只能是"**同一帧内不重复**"，不能是"总共只一遍"。

### 4.4 架构：逻辑长在视图里

* `views/` **943 KB > `components/` 769 KB**；`ChatView.vue` **3,429 行**同时持有 SSE 编排、
  会话 Map 桥接（`:1291-1370`）、统一任务执行器（`:1037-1151`、`:2691-2723`）、分享/导出、
  搜索、历史分页。**2.x 的最大组件是 44 KB（`Settings.tsx`）**。【枚举】
* 2.x 把一屏拆成**卡片组件族**：`ui/cards/` 里 `ToolCard` 18,465 · `ApprovalCard` 15,361 ·
  `AskCard` 14,196 · `SayCard` 9,709 · `CompactionCard` 8,608 · `GuardianCard`/`ReceiptCard`/`ReadsCard`/
  `UserCard`/`NoticeCard`/`RememberCard`/`ExtensionCard`；回合组合抽到 `turnrows.ts`/`blocks.ts`，
  折叠偏好抽到 `state/foldpref.ts`。【枚举】
* **动作**：沿用既有纪律（`docs/split-assessment.md` §2.5 的"注入面"度量）：`ChatView.vue` 已补 3 条冒烟网，
  下一簇是**会话加载/切换 + SSE 生命周期**（`loadSessions`/`switchSession`/`activeSSE`/`onUnmounted`）。
  **抽 composable，不动模板契约、零行为变更。**
  **⏸ 2026-10-09 量完：这一簇不抽，且"SSE 生命周期"不是一簇** ✗ —— 按同一"注入面"口径实测
  （`ChatView.vue` 现 **3,458 行 / 218 顶层声明**）：**整簇 27 个**外部符号（持久化簇在 **14** 时已判暂缓 ⇒ 27 更不该抽）·
  `switchSession` 单独就有 **23**；而所谓"SSE 生命周期"**散在 9 处**（`activeSSE` `L463` … `sendMessage` `L2298` ·
  `stopGeneration` `L2448`），**横跨 1,100+ 行** ⇒ 抽它等于抽掉**半个视图** ✗。
  唯一可抽的是 `loadSessions`+`persistSessions`+`sortSessions`（**~25 行 / 注入面 3**），但太小且无组件测试兜底 ⇒ **不做** ✓。
  **⇒ 正确拆法是"按能力"而不是"按生命周期阶段"**（承重的是 `sendMessage`/`switchSession`/`onSSEMessage`）；
  **下一簇应另选**。详细测量见 `docs/split-assessment.md` §2.5。

### 4.5 a11y：只有两条机械规则

* `check-a11y.mjs` 只拦两件事：非交互元素上绑 `@click` 却无 `role`、`<img>` 缺 `alt`。
  **没有** axe-core 类扫描、**没有**焦点陷阱/焦点归还检查（弹窗类 UI 是高危区）、**没有**对比度检查、
  **没有** live region（`aria-live`）审计 —— 而 Chiron 是流式界面，中断通知是否被读屏播报无人验证。【枚举】
* 讽刺点：门禁注释把 `chat/ChatEmptyHero.vue` 树为参考实现，而**那段示例代码本身是注释掉的**
  （`ChatEmptyHero.vue:35-40`）。【枚举】
* **公允地说，这一项不是"2.x 有、Chiron 没有"**：2.x 也没发现自动化 a11y 扫描（无 axe-core 类依赖），
  只在 `panels/AgentTranscript.tsx` 里有一个**手工** Tab 焦点陷阱。两边都弱；
  Chiron 至少已有 2 条**会失败**的规则，起点更好。【枚举】
* **动作**：a11y 门禁是可加规则的（棘轮已空，加规则成本最低）：先加
  R3「`aria-live` 区域必须存在且流式状态进入其中」与 R4「模态类组件必须有焦点陷阱/归还」，
  基线允许先用存量白名单收敛。

> **2026-10-09 落地与实测（R3 加、R4 不加）**：
> **R3 ✅ 已加**（`frontend-vue/scripts/check-a11y.mjs`）—— 但它**不是**一条扫描规则，而是
> **必需不变量**：`aria-live` 该不该有取决于语义，从模板上无法机械推断（硬扫既漏又误报）。
> 而全仓只有**一处**承载"逐字追加的助手回答"：`components/chat/MessageItem.vue`
> （`:aria-live="item.streaming ? 'polite' : 'off'"`）⇒ 守卫钉住**这一条具体不变量**：
> 该文件必须保留 `aria-live`。它**不进基线、也不因 `--write-baseline` / `--list` 而跳过**
> （缺了就是缺陷，没有"存量"可言）。**变异验证**：拿掉该属性 ⇒ **exit 1**（`--list` 模式下同样红）；
> 字节级还原 + SHA256 一致 ⇒ exit 0 ✓。
> **R4 ❌ 刻意不加**：实测全仓 **62 处**模态走 antd `Modal`（**自带焦点陷阱**），
> 自研浮层只有 `FloatingPanel.vue` 且**已做焦点归还**（`lastFocused?.focus?.()`）⇒
> 想用文本规则表达"必须有陷阱/归还"，只能退化成"文件里出现过 `.focus(`" —— 那是**伪判据**
> （既拦不住真问题、又会因无关改动误报）。按该脚本开头那条原则：**宁可少收** ✓。
> **仍缺的**（如实保留）：axe-core 类扫描、对比度检查、中断通知的读屏播报验证 —— 都没有。

### 4.6 i18n 盲区

* `src/` 内 0 处硬编码中文，**但 `index.html:2` 的 `lang="zh-CN"` 与 `:13` 的
  `<title>Chiron AI Agent 工作平台</title>` 在门禁扫描范围（`src/`）之外**。【枚举】
  **2026-10-09 更正（原文把两处并列，其实只有一处是真缺口）**：
  **`lang` / `dir` 运行时是接了的** —— `src/i18n/index.ts` 的 `applyDocumentLocale()` 会写
  `<html lang/dir>`（首屏 mount 前调用一次、每次 `setLocale()` 再写一次），`index.html` 里那句
  `lang="zh-CN"` 只是 **JS 起来之前的静态默认值**（**必须有**，否则 RTL 首屏会先按 LTR 渲染再跳变）。
  **`<title>` 才是真缺口**：此前**没有任何地方在运行时更新它** ⇒ 切到 `en-US` / `ar` 后标签页仍是中文。
* **动作（已落地 2026-10-09）**：在 `applyDocumentLocale()` 里补 `document.title = t('common.appTitle')`
  —— 它同时覆盖「首次挂载」与「每次 `setLocale()`」两条路径；三个语言各加 `common.appTitle`
  （zh-CN `Chiron AI Agent 工作平台` · en-US `Chiron AI Agent Platform` · ar `منصة Chiron AI Agent`）；
  用例 `LanguageSwitcher.spec.ts` 的「**setLocale 同步浏览器标签页标题**」逐语言断言
  （vitest **57 文件 / 481 用例全绿** · `check:ui` / `vue-tsc -b` / `eslint` 0 error）。
  **原提议的"把门禁扫描根扩到 `index.html`"没有采用**：那两行是**刻意的静态默认值**，
  让文本门禁去要求它们"i18n 化"会**惩罚正确的代码**（与第 85/91 轮同一类判断）；
  该钉的是"**运行时确实同步了**"—— 那是**行为**，用**用例**钉比用文本门禁钉准确。

### 4.7 生命周期正确性（一个可见的 bug 温床）

* `transcriptProjection.ts:336` 的"活跃轮"判据是**位置**（`turn.to === items.length`），
  不是生命周期。而 `done` 事件只在**带 token 时**才额外压入 `turn_stats` 行
  （`ChatView.vue:2220-2227`）⇒ **一次不带 token 的 `done` 会让最后一轮永远停在 `status:'running'`**，
  其工具组默认展开。【枚举】
* **动作**：把活跃判据改为绑 `turnId` 的生命周期标记（`done`/`cancelled`/`error` 都终结该轮），
  补一条"无 token 的 done 也终结轮次"的组件测试。
  **✅ 已落地（2026-10-09）**：`ProjectionInput` 新增 `running?: boolean`，活跃判据改为
  **`Boolean(input.running) && turn.to === items.length`**；`MessageList` 传自己的 **`loading`** 作为
  生命周期信号（`done` / `cancelled` / `error` / `guardrail_blocked` 都会把 `loading` 置 false ⇒ 该轮终结 ✓），
  **缺省 `false`** ⇒ 历史里没有任何回合是"活跃"的 ✓。用例 `transcriptProjection.spec.ts` 的
  「**没有 token 的 done 也终结轮次**」正是原行要求的验收（并带一条对照断言：仍在运行时**必须**展开，
  免得"修好了 bug 但把展开也修没了"）✓。
  **⚠ 一处连带影响（已处理）**：`MessageList.spec.ts` 三个用例原本用 `loading: false` 却期望
  **全部 item 都渲染** —— 那是**依赖旧的位置语义**（最后一轮恒为活跃 ⇒ 工具组恒展开）✗；
  按新语义 `loading: false` = 该轮**已完成** ⇒ 工具组按设计**默认收起**，工具行被折叠头剔掉 ⇒
  数量对不上。这三个用例测的是**按 kind 的分发**，故改为 `loading: true`（并写明原因）✓。

### 4.8 状态呈现的缺口（用户看得见的）

| 场景 | Chiron 现状 | 2.x | 建议 |
|---|---|---|---|
| 运行被中断/接管 | **原本无 UI**：`ChatItem` 没有 `interrupted` 类型，`frontend-vue/src` 里 `interrupted` 只命中文案。**实测更精确的现状**：用户**主动停止**有标记（`TextItem.stopped` + 「继续生成」按钮，`ChatView.vue` 的停止分支），但那是**纯前端字段、不入库 ⇒ 刷新即消失**；而**断线 / 会话取消 / 超时**这三种中断，用户侧**一点痕迹都没有**（网关只把它落进 `turns.status='cancelled'`，`submit_handler.go:588-598`）。**接管（另一实例续跑）另说**：引擎侧有完整语义（`resume.py`：热 1h / 冷 24h / 超窗 `abandoned` **可见**），但它**不发 `AgentEvent`** ⇒ 前端无从得知 | 有 `abandoned`/恢复语义与事件 | **✅ 留痕 + 呈现已补（2026-10-09）**：网关在回合以 `cancelled` 收尾时落一条 `tool_name='interrupted'` 的记录（`reason` = `timeout`（`ctx.Err()==DeadlineExceeded`）或 `cancelled`），投影渲染成 **warning 通知行**（`common.turnInterrupted` / `common.turnTimeout`，超时与取消**分开说**）⇒ **刷新后仍可追溯**。**仍开**：① **重试/继续入口**（原行建议的"重试入口"未做）；② **接管（`abandoned`）的呈现** —— 那需要**引擎发事件**（`resume.py` 目前只写 DB + 指标） |
| 护栏拦截 | **实时原本只有 `message.warning` toast**（`ChatView.vue:2251-2256`）；留痕**有但是有损的** —— 网关把 `guardrail_blocked` 落成一条 `tool_name='guardrail'` 的 tool_call 并立即冲刷（`submit_handler.go`），但 id 写成 `"guard_"+evt.ID` 而**引擎的 `AgentEvent` 没有 `id` 字段** ⇒ `evt.ID` 恒空 ⇒ id 是**常量** `"guard_"`，而 `SaveToolCall` 走 `ON CONFLICT (id) DO UPDATE` ⇒ **同会话多次拦截只剩最后一条** ✗ | 有 `Notice` 级事件与卡片 | **✅ 呈现已补齐 + 有损留痕已修（2026-10-09）**：新增 `kind: 'notice'`（`chat-types.ts` 的 `NoticeItem`，`tone: 'warning' \| 'info'`）· **投影**里把 `tool_name === 'guardrail'` 映射成它（`chat-history.ts`，文案取 `input` 的 `{"reason"}`，**解析不出就回退通用文案、绝不把 JSON 原样丢给用户**）· **实时路径**插**同一种条目** ⇒ 实时与刷新后观感一致 · 渲染在 `MessageItem.vue`（`role="status"`）· **id 改用 `spillRecordID()`**（纳秒时间戳）⇒ 多次拦截不再互相覆盖。用例 `chatHistory.test.ts` ⇒ vitest **57 文件 / 485 用例全绿** |
| 压缩 | **只有事后**（`saved_tokens` 非零才显示状态栏文字），**无"进行中"态** | `CompactionStarted/Progress/Done` 三段式 + 可展开卡片 | **事后那一半 ✅ 已补（2026-10-09）**：网关把引擎的 `compaction` 事件落成 `tool_name='compaction'` 的记录（**此前网关侧 0 处理 ⇒ 刷新即丢**）+ 投影渲染成 **info 通知行**（文案复用状态栏那个键与 k 单位 ⇒ 两处读数不矛盾）+ 实时路径插同款条目。**"进行中"态仍缺，且需要引擎改动**：`runtime.py:884-919` **只在真的压缩之后**才发事件（没压就返回 None）⇒ 上游**不存在**"开始压缩"这个事件，补它要动引擎。 |
| 会话内搜索 | 只高亮整行 + `n / N`；**无结果列表、无词级高亮**，`matchExcerpt` 是死代码 | 有查找落点（`findland.ts`）+ 定位栏 | 复用 `matchExcerpt` 做结果列表与词级高亮（纯前端） |
| 会话内搜索 | 只高亮整行 + `n / N`；**无结果列表、无词级高亮**，`matchExcerpt` 是死代码 | 有查找落点（`findland.ts`）+ 定位栏 | 复用 `matchExcerpt` 做结果列表与词级高亮（纯前端） |

### 4.9 死代码与未用依赖（廉价清理）

**2026-10-09 逐条复核**（原文四条里有**三条已过期** ✗ —— 一条已修、一条修了一半、一条是错的）：

| 原文断言 | 当前状态 |
|---|---|
| `@tanstack/vue-virtual` 在 `package.json` 里但**从未 import** | **✅ 已修（第 101 轮）**：已从 `package.json` 移除（−1 行）与 lockfile（−18 行）✓ |
| `composables/useVirtualList.ts` 与 `chat-types.ts` 的 `throttleRaf` **零调用点** | **半修**：`throttleRaf` **已有调用点**（第 117 轮接进 `MessageList.vue` 的更新路径 ✓）；**`useVirtualList.ts`（137 行）仍然零调用点** ✓ |
| `MessageItem.vue:666` 的注释仍说虚拟列表项是 `absolute` | **仍成立**，但**行号已过期**：现在是 `:697`（文件变长了）✓ |
| **没有 `content-visibility`** | **✗ 这条是错的**：`style.css` 里**有 3 处** ✓（2.x 是 5 文件 / 10 处 ⇒ 差距是**覆盖面**，不是"有没有" ✓） |

**2026-10-09 复核新增的死代码**（同一套"先量有没有被用"的顺序，见 `docs/development-roadmap.md`）：
`composables/useResponsive.ts` **159 行** · `composables/useVirtualList.ts` **137 行** ·
`composables/useIdleLoader.ts` **101 行** ⇒ **三个 composable 共 397 行零引用** ✓；
加上第 141 轮的两个组件（`AgentCollabPanel.vue` 525 · `KBSearchResults.vue` 412）⇒
**删除候选合计 1,334 行**，证据均已备齐（全 `src` 搜名字 / kebab 变体 / 动态引用皆无命中 ✓）。

---

### 4.10 渲染技术、状态层与主题：三处"可对照但不等价"

**渲染技术：两条不同的取舍，不是谁落后。**

| | Chiron | Reasonix 2.x |
|---|---|---|
| 技术 | **两段并集窗口化**（主窗口 + 常驻尾部），几何为纯函数 | **块粒度卸载 + 实测高度占位**：`ui/blocks.ts` 把稳定 item 切成 ≤48 的块（**不切断回合**、保持数组标识以便 memo）；`Transcript.tsx:501-557` 用一个共享 `IntersectionObserver`（`rootMargin: 200%`）判远近，远则渲染高度=上次 `offsetHeight` 的空 `.chunk` |
| 块内 | 行级窗口 | CSS `content-visibility:auto` + `contain-intrinsic-size:auto 216px`（`app.css:918-919`）；**流式中的最后一张卡排除在外** |
| 跟随滚动 | `vp.follow`（租约期间被拒） | `useLayoutEffect` 里**绘制前**写 `scrollTop = scrollHeight`；刻意不用 `scrollIntoView`（注释：它占 13% self time，还与揭示时钟打架） |
| 底部判定 | `isAtBottom`（阈值 4） | 观察 `.flow-end` 标记（`rootMargin: 0 0 48px 0`），**不测量** |
| 跳转 | `jumpToRow` + 高亮 2 s | 定位栏是**鱼眼索引**（`ui/Rail.tsx`：STEP 9 / PAD 26 / 余弦衰减 REACH 5），落到块后再重排至**连续两帧一致** |
| 虚拟化库 | `@tanstack/vue-virtual` 声明却**从未 import** | `useVirtualizer` **0 命中** |

⇒ 2.x 省的是**测量成本**（只测块），Chiron 强的是**表达力**（"活跃轮常驻"是显式不变量）。

**状态层：影响可测试性。** 2.x **不引状态库**（依赖里无 redux/zustand/jotai），
每窗格一个 `useReducer`（`state/session.ts` 38,230 B / 796 行）+ `useSyncExternalStore` 读偏好。
关键手法是 **`revision` 计数器** —— 除 `text`/`reasoning` 增量外都自增，让 memo 以它而非 items 数组为键
（`session.ts:159-169`）：这正是 Chiron"投影不读 `content` 所以只有流式那条失效"的同一性质，
但 2.x **把它写成了显式声明**。Chiron 侧是 Pinia（5 store / 30,553 B）+ 视图内状态，
`ChatView.vue` 实际承担了 reducer 的角色。

**主题：这一项 2.x 明显更强，且强在原则不在数量。**
* **状态色不可被主题覆写**：`ui/theme.ts:61` 把 `--ok --warn --err --net --deleg --add --del --focus`
  列为 reserved —— *"a theme that could recolour them would let a failure render as success"*；
* **读者偏好压过主题包**：对比度以 OKLCH 相对亮度偏移的方式，**在包自己的规则内**施加（`:75-81`）；
* **写入走 CSSOM**：生成的包规则经 CSSOM 写入，值被解析器拒绝时**只丢那一条**，绝不碰根内联样式（`:83-110`）；
* **跨语言词表有测试**：Go 侧 `internal/ext/theme/tokens_test.go` 读 `src/ui/theme.ts`，把内核词表与前端映射钉住。

**Chiron 的缺口**：`style.css` 的 8 个主题块（96 token/块）与 `stores/theme.ts` 的 antd 注册表是
**双源**，代码注释**自己承认**（`stores/theme.ts:22-28`），但**没有任何测试把两半钉住**，
也没有"状态色不可覆写"这类约束。**动作**：仿照既有的 `scripts/check_tool_policy_parity.py`
（Go↔Python 分级表同构）加一条"CSS token ↔ antd token 键集同构"守卫 —— **零依赖、与现有门禁同构**。

> **2026-10-09 实测后修正了判据（重要）**：**"两侧键集同构"这个提法不成立** ✗ —— 两侧词表**结构不同**：
> CSS 是**语义**词表（每块 **96** 个：`--primary` / `--brand-500` / `--success` …），
> antd 是**组件面**词表（每对象 **13** 个：`colorPrimary` / `colorBgLayout` …）；
> 按 `camelCase → kebab` 约定去对，**0/13 命中** ⇒ 两侧是「**投影**」关系（antd 取色板的一个子集），
> **不是同名同构**。硬写"键集相等"会得到一个**永远红或永远假绿**的护栏。
> **已落地的守卫改钉「每一侧内部必须齐整」**（`frontend-vue/scripts/check-theme-token-parity.mjs`，
> 已接进 `check:ui` ⇒ **8 道棘轮**）：
> ① `style.css` 的 **8** 个 `[data-theme='<id>-<light|dark>']` 块必须共享**同一套 token 名**；
> ② `stores/theme.ts` 的 **8** 个 `*Tokens` 对象必须共享**同一套键**。
> 这是"双源漂移"里**真正会出事**的那种 —— 改主题时漏写一个 token ⇒ 该主题下某组件拿到
> **未定义变量/回退色**，而界面上只是"某个角落颜色不对"，没人会立刻发现。
> **跨侧映射刻意不查**：那是**语义映射**，需要人给映射表（0/13 可由约定推导 ⇒ 现在写只会是伪判据）。
> **验收**：`check:ui` **exit 0** · 变异验证**两个方向**（从 `aurora-dark` 删 `--brand-50` ⇒ exit 1 ·
> 从第 2 个 `*Tokens` **对象体内**删 `colorPrimary` ⇒ exit 1；字节级还原 + SHA256 一致 ⇒ exit 0）·
> 守卫含**防失效断言**（找不到主题块/`*Tokens` 对象即失败）。

**审批/提问的呈现**：2.x 把它们做成 **transcript 里的卡片**（不是模态），结算后**留在原位可回读**
（`cards/ApprovalCard.tsx` 15,361 B / `AskCard.tsx` 14,196 B），按钮**只画内核声明的授权**
（`allowsSession`/`allowsPersist`），原因码经 5 条文案表映射并有旧内核回退。
**这正是 §4.8「护栏只有 toast」的正解**：可追溯的留痕优于一次性弹层。

**运行时通知**：2.x 的通知**不进 transcript**，集中在 composer 上方的 `RuntimeBar`
（有自己的高度上限、逐条关闭，`StallBar` 提供 停止/继续/静音/关闭），内核码→句子经 `i18n/notices.ts`。
这对 Chiron 是可选形态，但**"状态与历史分开"这条分界本身**值得抄。

---

## 5. 2.x 侧**机制**上可搬运的部分（形态不搬）

| 机制 | 证据 | 为什么值得搬 | 成本 |
|---|---|---|---|
| **端口契约 + Mock 实现** | `src/port/port.ts` 35,620 B；`mock.ts` 30,959 B（忠实模拟阻塞语义） | **是 179 个交互测试的前提**；Chiron 缺的正是这层接缝 | 中 |
| **状态层的 `revision` 键** | `state/session.ts:159-169` | 除 `text`/`reasoning` 增量外自增，memo 以计数器为键 —— 把 Chiron 已有的隐式性质变成显式声明 | 低 |
| **保留状态色 + 读者偏好优先** | `ui/theme.ts:61,75-81,83-110` | 主题不能把失败画成成功；对比度以读者为准；坏值只丢一条 | 低 |
| **跨语言词表 parity 测试** | Go `internal/ext/theme/tokens_test.go` 读 TS `ui/theme.ts` | 与 Chiron 既有的 Go↔Python parity 习惯同构，可复制到 **CSS ↔ antd token** | 低 |
| **留痕式审批卡片** | `cards/ApprovalCard.tsx` / `AskCard.tsx` / `ui/gates.ts` | 结算后留在 transcript、只画内核声明的授权 ⇒ 可追溯（对位 §4.8） | 中 |
| **破坏性对照的测试写法** | `src/state/trace_parity.test.ts` 主动丢掉 `source`/`issuer`，**要求比较必须失败** | 证明"这条断言真的在测东西"；Chiron 已有"变异验证"习惯，可制度化 | 低 |
| **静态不变量门禁** | `tools/ui-census/gate.mjs` 16 条（11,626 B）+ golden 10 文件 134,794 B | 把"渲染纪律"变成可失败的检查；且**口径诚实**（未闭合的口子只报不拦） | 中高 |
| **棘轮式 lint** | `tools/repolint`：28 条规则、`Finding.Weight`（换更长的违规也照样失败）、`-update` 拒绝放宽 | 比"基线白名单"更能防漂移；Chiron 的 6 个门禁已是同类，可补 `file-size`/`function-size` 规则 | 中 |
| **文档分层** | `styles/LAYOUT.md` 90,649 B / 1,439 行 / 56 节；`styles/TOKENS.md`；`app.css` **零注释** | 让"为什么这样写 CSS"集中可检索，而不是埋在 `style.css`（**1,532 行 / 100 行注释 / 51 条分节注释**）里 —— **✅ 已落地（2026-10-09）**：[`frontend-vue/src/style.md`](../frontend-vue/src/style.md)（62 行：三层结构 + 四个守卫的**实际**口径 + **没有守卫的三项**如实写明 + 相邻知识索引）。**权威仍在代码里**，该文档只做索引与不变量、不复制取值 | 低 |
| **状态分类法** | `docs/STUDIO_SHELL_BOUNDARIES.md` §6：durable intent / projection / replayable event | *"Promoting a projection to an event makes a rendering detail into a fact the stream has to guarantee"* —— 与 `vendor/规划.md` §6「不为对齐形态发明事件」**是同一条原则** | 低 |
| **跨语言镜像校验** | `repolint` 的 `wire-parity`（TS 镜像 ↔ Go wire 类型）、`frontend-parity`（ACP/serve ↔ Controller） | Chiron 已有 Go↔Python 的工具策略 parity，**前端↔网关的 payload 契约还没有** | 中 |
| **对齐台账** | `docs/STUDIO_PARITY.md` 49 行（Have 15 / Have differently 14 / Unverified 10 / Missing 9 / Removed 1） | 把"哪些行为是承诺、哪些没验过"写成可评审清单；它还**自陈"无门禁，靠评审"** | 低 |

**不搬**：Electron 壳与 Wails→Electron 迁移史、主题包 ZIP/`theme-pack.schema.json`（该 schema **从未被编译执行**，
真正生效的是 Go 侧 `internal/ext/theme/tokens.go` + 一个端到端生命周期测试）、TUI 与 `termrender`、
`bestof` 类多 worktree 形态、任何新框架或新依赖。

---

## 6. 建议顺序（性价比优先；全部不引新依赖）

| 序 | 项 | 依据 | 验收 |
|---|---|---|---|
| 1 | ~~修 3 处 `scrollTop` 直写~~ → **修 2 处 transcript 直写（已完成 2026-10-09）**；统一任务模式复用 `MessageList` **降级为产品决定（§6 约束：UI/UX 差距是文档化，不是重构）** | §4.1 | 源码级门禁**已落地**：`frontend-vue/scripts/check-transcript-scroll.mjs`（枚举 `scrollTop` 赋值并逐条归类，未归类即失败；已接进 `check:ui` ⇒ 7 道棘轮）· `vue-tsc -b` 通过 · `eslint` 0 error · 现有 **480 用例全绿** |
| 2 | ~~`thinking` 落库缺口（见 [差距分析](../reasonix-gap-analysis.md) §12.1）~~ → **✅ 已完成（2026-10-09）** | 逐 delta 的重复渲染之外，这是**用户可见的数据丢失** | **已满足**：`internal/api/submit_handler.go` 把独立 `thinking` 事件按既有 `[thinking]…[/thinking]` 格式累加落库（此前只认 `evt.Type == "text"` ⇒ 实时看得到、刷新就没了）；用例 `internal/api/submit_thinking_persist_live_test.go`（假引擎发 thinking/text/done + **真 PG**，把消息**读回来**断言）；前端 `splitThinking(loose)` 据此还原。**变异验证**：去掉该分支 ⇒ 用例红并报「思考没有落库（刷新后会丢）」 |
| 3 | 流式期跳过 styled 渲染 + 合帧测量 | §4.3 | **前半 ✅**：`scheduleRender` 加 `isStreaming` 早退（流式期不跑 `md.render`/`sanitize`、不进 `mdCache`）；watcher 必须同时监听 `isStreaming`（`displayContent` 不依赖它，否则结束那刻不重算、消息一直停在纯文本）；用例按 §4.3 的验收写 ⇒ vitest 全绿。**后半 ✅**：`MessageList.vue` 的更新路径用现成的 `throttleRaf` 合并「测量 + 窗口重算」（`syncActiveQuestion` 仍同步）· 给 `throttleRaf` 补 `setTimeout` 退路。**⚠ 该判据不适合组件级测试**（jsdom 的 rAF 在 `await nextTick()` 之间就落地 ⇒ 观察不到合并）⇒ 改为**用受控 rAF 替身单测 `throttleRaf`**；且测量会写响应式状态 ⇒ 是**收敛过程**，判据只能是"同一帧内不重复"。验收：vitest **57 文件 / 488 用例全绿** · `vue-tsc -b` / `check:ui` / `eslint` 全 exit 0 |
| 4 | 补 `interrupted` / `guardrail_blocked` / 压缩进行中 的呈现 | §4.8 | **`guardrail_blocked` ✅**（`kind: 'notice'` + 投影 + 实时 + `role="status"`；并修掉一处**有损留痕**：id 恒为常量 ⇒ 多次拦截互相覆盖）· **压缩（事后那一半）✅**（网关落 `tool_name='compaction'` + 投影成 info 通知行；此前网关 0 处理 ⇒ 刷新即丢）· **`interrupted`（留痕 + 呈现）✅**（网关在 `cancelled` 收尾时落 `tool_name='interrupted'`，投影成 warning 通知行，超时/取消分开说；此前**断线 / 取消 / 超时在界面上毫无痕迹**）。**仍开**：① 压缩的**"进行中"态**（需**引擎**发"开始压缩"事件）· ② `interrupted` 的**重试/继续入口** · ③ **接管（`abandoned`）的呈现**（同样需引擎发事件 —— `resume.py` 目前只写 DB + 指标）。验收：vitest **57 文件 / 486 用例全绿** · `vue-tsc -b` / `check:ui` / `eslint` 全 exit 0 · `go build` / `go vet` / `go test ./internal/api/` 全 exit 0 |
| 5 | 前端↔网关的**端口接缝 + Mock**，把 §3 的 5 条手测自动化 | §4.2 | **⚠ 前提已更正（2026-10-09）**：§3 里其实只有 **4 行**带"手测"，且**这 4 行现在全部有测试**（`transcriptAnchor.spec.ts:21/28/46` · `MessageList.windowing.spec.ts:40/63` · `transcriptMeasurementLedger.spec.ts:56`）⇒ **"5 条手测"这个立项前提不成立** ✗。**仍成立的真差距**：缺 **DOM 级 interaction test**（2.x 有 179 个）与**真浏览器 smoke**（Chiron 无 Playwright/Cypress）⇒ 本项应重述为"**补一层集成/浏览器验证**"，而不是"把已有的手测自动化"。**已做的一小块（不引新依赖）**：给第 113–115 轮的 `notice` 行补了 **DOM 级渲染用例**（此前只有投影层测试）· **再进一步（2026-10-09）补了第一条真正的「交互级」测试** —— `src/views/__tests__/ChatView.interaction.spec.ts`（**3 条**：提交→建流→用户消息进列表 · `guardrail_blocked` 帧⇒warning 通知行 · `compaction` 帧⇒info 通知行）。**可行性来自契约形状**：`createSSEConnection` 是**回调式**（`onMessage` 回调）⇒ 替身存下它即可喂帧，**无需浏览器**。**顺带发现一处被冒烟网掩盖的形状错误**：解桩 `ChatInput` 后 `models.value.map is not a function`（`ChatInput.vue:160` 要的是**数组本身**，不是 axios 外壳）⇒ 已修替身。⇒ vitest **58 文件 / 494 用例全绿** |
| 6 | **a11y 加 R3/R4 规则** → **R3 ✅ 已加（2026-10-09）· R4 实测后刻意不加**；i18n 那一半见 §4.6（标题已接 i18n） | §4.5 / §4.6 | **R3** 是**必需不变量**而非扫描规则：`MessageItem.vue` 必须保留 `aria-live`（全仓唯一承载流式回答处），不进基线、任何模式都先校验；**变异验证**（拿掉属性 ⇒ exit 1，含 `--list`）✓。**R4 不加**：62 处模态走 antd `Modal`（自带焦点陷阱）· `FloatingPanel` 已做焦点归还 ⇒ 文本规则只能是伪判据。仍缺 axe-core / 对比度 / 读屏播报验证（如实保留） |
| 7 | CSS 知识分层（`styles/` + 说明文档），清理死代码/未用依赖 | §4.4 / §4.9 | **未用依赖已清（2026-10-09）**：`@tanstack/vue-virtual` **声明了但全仓零 import**（`git grep` 只命中 `package.json` 与 lockfile）⇒ 已移除 —— `package.json` **−1 行** · `pnpm-lock.yaml` **−18 行**（diff **只含该包与其传递依赖** `@tanstack/virtual-core`，无无关改动）· `pnpm install --lockfile-only` exit 0（离线）· 验收 `check:ui` **exit 0** · `vue-tsc -b` **exit 0** · vitest 全绿。**CSS 知识分层 ✅ 已落地（2026-10-09）**：[`frontend-vue/src/style.md`](../frontend-vue/src/style.md) —— 把"为什么这样写 CSS"从 `style.css`（**实测 1,532 行 / 100 行注释 / 51 条分节注释**，原文写的"60 行注释"已过期）里**索引**出来：三层结构（`:root` 主题无关量 / 8 个主题块 / 兼容别名层）+ 四个守卫的**实际**口径 + **没有守卫的三项**（`prefers-reduced-motion` 降级 · 对比度 · `!important` 数量）如实写明。**未复制取值**（权威仍在代码里）。**§4.4 的抽 composable 仍未做**（那是重构，不是文档）—— **且 2026-10-09 量完判定"不该抽这一簇"** ⏸：按 §2.5 的"注入面"口径，原提的"会话加载/切换 + SSE 生命周期"簇是 **27** 个外部符号（持久化簇在 **14** 时已判暂缓），而"SSE 生命周期"**散在 9 处、横跨 1,100+ 行、含 `sendMessage`/`stopGeneration`** ⇒ 抽它等于抽半个视图 ✗；唯一可抽的 `loadSessions`+`persistSessions`+`sortSessions`（~25 行 / 面 3）太小且无测试兜底 ⇒ 不做 ✓。**正确拆法是"按能力"不是"按生命周期阶段"**，下一簇应另选（详见 `docs/split-assessment.md` §2.5） |
| 8 | ~~加"CSS token ↔ antd token 键集同构"守卫~~ → **✅ 已落地（2026-10-09，判据经实测修正）** | §4.10 | **判据修正**：两侧词表**结构不同**（CSS 语义 96 个 vs antd 组件面 13 个，`camelCase→kebab` **0/13** 命中）⇒ 不是"键集同构"，硬写会永远红或假绿。改为钉「**每一侧内部必须齐整**」：`frontend-vue/scripts/check-theme-token-parity.mjs`（8 个 `[data-theme]` 块共享同一套 token 名 · 8 个 `*Tokens` 对象共享同一套键），已接进 `check:ui` ⇒ **8 道棘轮**。**验收**：`check:ui` exit 0 · **变异验证两方向**（删 `--brand-50` / 删 `colorPrimary` ⇒ 均 exit 1，字节级还原 ⇒ exit 0）· 含防失效断言 |
| 9 | ~~修 §4.7 的活跃回合判据~~ → **✅ 已落地（2026-10-09）** | §4.7 | 活跃判据由**位置**改为**生命周期**（`ProjectionInput.running` ← `MessageList` 的 `loading`）⇒ **不带 token 的 `done` 也终结轮次**（此前最后一轮会永远 `running` 且工具组默认展开 ✗）。用例：`transcriptProjection.spec.ts` 的「没有 token 的 done 也终结轮次」（带"仍在运行时必须展开"的对照断言）· 连带修正 `MessageList.spec.ts` 三个依赖旧语义的用例（`loading: false` → `true`）。验收：vitest **57 文件 / 491 用例全绿** · `vue-tsc -b` / `check:ui` / `eslint` 全 exit 0 |

---

## 7. 复现方式与证据索引

### 7.1 本机实测部分

```bash
# 1) 6 个 UI 门禁（本次全部 exit 0，且每条都报"存量 0 处"）
cd frontend-vue && npm run --silent check:ui

# 2) 前端测试（本次 57 文件 / 480 用例通过，exit 0；约 39s）
npx vitest run --reporter=dot; echo "exit=$LASTEXITCODE"

# 3) 量测试厚度（用 git ls-files，避免 Get-ChildItem -Recurse 被 junction 带出仓库）
git ls-files "frontend-vue/src/*" | ...
```

### 7.2 Chiron 侧证据

| 路径 | 关键数据 |
|---|---|
| `docs/transcript-contract.md` | **157 行**（2026-10-09 复测；原记 142 行）；9 组不变量 + 9 行验收（5 条标"手测"） |
| `frontend-vue/src/components/chat/transcript*.ts` | **7 文件 / 37,880 B**（2026-10-09 再测；此前记 36,832 B 与 36,842 B **两个不同的数**，都过期）；`transcriptViewport.ts:86` 是唯一在契约内的写者 |
| `frontend-vue/src/views/ChatView.vue` | **148,618 B / 3,435 行**（2026-10-09 复测；原记 148,405 B / 3,429 行）；原 `:1045`/`:1150`/`:1850` 直写 `scrollTop` —— **前两处写 transcript，已于 2026-10-09 改为 `messageListRef.scrollToBottom()`**；第三处写 `.unified-list`（另一容器）；`:2691-2718` 第二条渲染路径 |
| `frontend-vue/src/components/chat/MessageItem.vue` | `:311-343` 每 delta 全量 markdown+sanitize；`:438-443` 流式纯文本 |
| `frontend-vue/src/components/chat/MessageList.vue` | `:290-294` 每次更新全量测量；`:83` 窗口化阈值 150 行 |
| `frontend-vue/scripts/check-*.mjs` | **7** 个门禁，基线全为 `{}`（2026-10-09 新增 `check-transcript-scroll.mjs`） |
| `frontend-vue/src/style.css` | 51,192 B / 1,532 行，约 60 行注释 |
| `frontend-vue/src/index.html` | `:2` `lang` 与 `:13` `title` 硬编码，在门禁范围外 |

### 7.3 2.x 侧证据（`vendor/DeepSeek-Reasonix/`）

| 路径 | 字节 | 用途 |
|---|---|---|
| `desktop/frontend-next/src/port/port.ts` / `mock.ts` / `fixture.ts` | 35,620 / 30,959 / 20,446 | 端口契约 + 忠实 Mock + 脚本夹具 |
| `desktop/frontend-next/src/ui/blocks.ts` / `state/session.ts` | 2,012 / 38,230 | 块粒度渲染；会话 reducer（`revision` 键） |
| `desktop/frontend-next/src/ui/theme.ts` / `styles/tokens.css` / `ui/paint.ts` | 8,535 / 12,172 / 5,317 | 包→变量映射；108 个 token；唯一写 `dataset` 处 |
| `desktop/frontend-next/src/i18n/index.ts` / `kernel.ts` | 4,478 / 39,662 | **中文原文即 key**；内核只给码，SPA 给句子 |
| `desktop/frontend-next/src/ui/cards/ApprovalCard.tsx` / `AskCard.tsx` | 15,361 / 14,196 | 审批/提问是 transcript 卡片（留痕） |
| `desktop/frontend-next/src/ui/MeterRail.tsx` / `RuntimeBar.tsx` | 5,461 / 2,292 | 运行仪表；运行时通知**不进 transcript** |
| `desktop/frontend-next/src/styles/LAYOUT.md` / `TOKENS.md` | 90,649（1,439 行）/ 15,568 | "布局法"：`app.css` 不带注释的原因 |
| `desktop/frontend-next/src/styles/app.css` / `studio.css` | 320,220 / 201,356 | |
| `desktop/frontend-next/tools/ui-census/`（`gate.mjs`/`report.mjs`/`golden/`） | 11,626 / 42,146 / 134,794 | 16 条静态不变量 + 冻结报告 |
| `desktop/electron/test/{unit.mjs,smoke.js,browser_live.js,computer_live.js}` | 79,801 / 17,789 / 11,700 / 7,874 | 真壳/真内核 live 测试 |
| `desktop/frontend-next/src/ui/Transcript.tsx` + `cards/` | 33,184 + 各卡片 | 一事件一卡片；`rank`/`turnrows`/`blocks` 拆回合组合 |
| `docs/STUDIO_SHELL_BOUNDARIES.md` | 7,479 | 渲染进程职责 + **状态三分类** |
| `docs/STUDIO_PARITY.md` | 6,322（49 行） | 对齐台账；自陈"无门禁" |
| `docs/TOOL_APPROVAL_MODES.md` | 12,467 | 审批卡片键位（`←/→`、`Enter`、`1`–`4`）与计划卡三选 |
| `internal/frontend/tui/transcript.go` | 16,716（598 行） | TUI 与桌面**折叠同一份事件流**（但无跨面测试） |
| `tools/repolint/`（`baseline.json` 38,788） | 28 条规则 / 471 文件带预算 | 含 `wire-parity`、`frontend-parity`、`css-size` |

---

## 8. 一句话总结

Chiron 在**静态 UI 纪律**（6 个门禁、零存量、阻断构建）与 **transcript 契约**上并不落后：
2.x 的渲染法写在 `styles/LAYOUT.md` 里、Chiron 的写在 `docs/transcript-contract.md` 里，两边都算"有法"。
差距集中在两处：
**（a）契约没被实现完全遵守**（3 处直写 `scrollTop`，其中 2 处属 transcript —— **已修并加门禁**；另一条绕开投影的渲染路径**保留为已文档化的差异**，按 §6 不为此重构），
**（b）契约的验证没有机械化**（0 浏览器测试、5 条验收靠手测、测试/源码 0.11× vs 0.77×）。
最划算的不是换框架或重写渲染，而是**补一层端口接缝把内核变成可脚本化的假实现**——
2.x 用 `AgentPort` + `MockPort` 换来了 179 个交互测试，而这件事**不需要任何新依赖**。
