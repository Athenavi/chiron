# 核心组件：Transcript（消息列表契约）

> **权威来源**：[transcript 契约](../../transcript-contract.md)（**唯一权威**，含 9 组不变量与 7 个纯几何模块）。
> 本页只给「为什么需要它、模块在哪、门禁守什么」。

## 为什么要有这份契约

消息列表是**最容易悄悄写坏**的地方：滚动位置、窗口化、异步测量、流式增量……
每一项都能被"顺手改一行"破坏，而且**坏得不明显**（只是偶尔跳一下）。
所以把它做成契约：**几何逻辑抽成纯模块、写者唯一、并由门禁守着**。

## 七个纯几何模块（`frontend-vue/src/components/chat/`）

| 模块 | 职责 |
|---|---|
| `transcriptViewport.ts` | **唯一**允许写消息列表 `scrollTop` 的地方 |
| `transcriptAnchor.ts` | prepend 时的锚点保持（纯几何） |
| `transcriptWindow.ts` | 窗口化的几何与渲染区间 |
| `transcriptMeasurementLedger.ts` | DOM 实测尺寸的**暂存 → 原子发布** |
| `transcriptProjection.ts` | chat item 的投影（含工具家族分类与折叠） |
| `transcriptFolds.ts` | 折叠状态 |
| `transcriptSearch.ts` | 列表内搜索 |

这些模块**不碰 DOM**（除了 viewport 那一处）⇒ 可以用**事件序列做确定性回归**，
不需要浏览器就能测。

## 门禁：单写者

`frontend-vue/scripts/check-transcript-scroll.mjs`（已进 `check:ui`）枚举 `src/**` 下**所有**
`scrollTop` 赋值，要求每一处都被**显式归类**：要么是契约内的唯一写者，要么标明它是别的容器。
**新增一处未归类的赋值 ⇒ 门禁红。**

历史上这条契约被违反过 3 次（`ChatView.vue` 里直接写 `scrollTop`）—— 其中 2 处属 transcript，
**已修并落地该门禁**；第 3 处写的是另一个容器（`.unified-list`），**保留为已文档化的差异**。

## 前端怎么消费它

- `components/chat/MessageList.vue` —— 列表容器，负责把窗口化与锚定接起来
- `components/chat/MessageItem.vue` —— 单条渲染（含工具卡、思考块、通知行）
- `transcriptProjection.ts` 的 `toolGroupKind` 同时被**单条**与**组头**复用 ⇒ 两边分类必然一致

## 延伸阅读

- [前端](frontend.md) —— 8 道棘轮
- [生产：测试](../production/testing.md) —— 这块怎么测（jsdom + 事件序列）
