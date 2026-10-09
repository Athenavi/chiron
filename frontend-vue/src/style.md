# `style.css` 的「为什么」

> 本文件是 `style.css`（**1,532 行** / **100 行注释**）的**检索入口**：把"为什么这样写"集中到一处，
> 而不是埋在 1,532 行里的 51 条分节注释中。**权威仍在代码里** —— 这里只做**索引与不变量**，
> 不复制具体取值（取值会变，复制必失真）。
>
> 为什么要有它：Reasonix 2.x 的 `app.css`（320 KB）**不带注释**，理由全部写在
> `styles/LAYOUT.md`（90,649 B / 1,439 行 / 56 节）+ `styles/TOKENS.md`；
> Chiron 的知识**分散在** `style.css` 的分节注释与组件注释里 —— 两边都"写了"，差别是**能不能检索**。
> （见 `docs/reasonix-ui-ux-gap-analysis.md` §4.10 的「文档分层」行。）

## 1. 分层：`style.css` 里的三种东西

改样式前先认清自己在改**哪一层** —— 三层的生命周期完全不同：

| 层 | 位置 | 什么时候变 | 说明 |
|---|---|---|---|
| **① 与主题无关的固定量** | `:root` | 极少变 | 层级（z-index）、界面字号缩放入口、动画周期、饱和色底上的前景色、全屏查看器的压暗底、投影、工作流类目色、macOS 红黄绿灯、搜索命中高亮 |
| **② 主题色板** | 8 个 `[data-theme='<id>-<light\|dark>']` 块 | 换主题/改色板时 | notion / paper / terminal / aurora × light / dark ⇒ **每块 96 个 token** |
| **③ 兼容别名层** | 与主题无关 | 只在迁移期存在 | 值是**对其它 token 的间接引用**（`var(--x)`），主题切换时随之变化 |

**判断法则**：值**随主题变**⇒②；**不随主题变**⇒①（例如 z-index —— 堆叠高低与配色无关，
所以它定义在 `:root` 而不是每个主题块里，否则 8 份重复且必然漂移）。

## 2. 不变量（有守卫，别绕过）

四个守卫都是**棘轮**（存量记在 `BASELINE` 里容忍、**只许下调**）—— 口径是
**「组件里不许写字面量，要写 token」** + **「8 个主题块必须齐整」**：

| 守卫（都在 `npm run check:ui`） | 实际管什么 |
|---|---|
| `scripts/check-theme-token-parity.mjs` | **8 个 `[data-theme]` 块共享同一套 token 名** · `stores/theme.ts` 的 8 个 `*Tokens` 对象共享同一套键 |
| `scripts/check-theme-token-contract.mjs` | 组件里**硬编码色值**（`#fff`、`rgba(0,0,0,.04)` …）⇒ 换主题时不跟着变 |
| `scripts/check-z-index-tokens.mjs` | 组件里**裸 `z-index` 数字** ⇒ 层叠关系不可推理（历史上有 `9999`） |
| `scripts/check-motion-tokens.mjs` | 组件里**硬编码动效时长**（`transition: opacity 0.2s`）⇒ 节奏无法统一调 |

**违反会怎样**：前两条 ⇒ 某主题下组件拿到**未定义变量/回退色**，界面上只是"某个角落颜色不对"；
后两条 ⇒ 想统一调层级/节奏时得逐处找。

> **没有守卫的（如实写明，别以为门禁绿就万事大吉）**：
> ① **`prefers-reduced-motion` 的降级** —— `check-motion-tokens` 只管**时长字面量**，不管
> 有没有写 `@media (prefers-reduced-motion)`；② **对比度**；③ **`!important` 的数量**。
> 这三项目前**只靠评审**。


## 3. 已知的写法与代价

* **`!important` 有 93 处** —— 绝大多数是为了压过 **antd** 的组件内联/高优先级样式
  （antd 的 `:where()` 化程度随版本变化，覆盖点因此脆弱）。**新增 `!important` 前先问**：
  能不能用 token 或 `ConfigProvider` 解决？覆盖 antd 时**写明压的是哪条规则**。
* **`contain` / `content-visibility` / `will-change`** 是渲染性能手段，出现在 6 / 3 / 4 处 ——
  它们**改变布局与合成行为**，加之前先确认没有破坏窗口化几何（见 `docs/transcript-contract.md`）。
* **界面字号只有一个入口**（`:root` 里的缩放量）—— 不要给单个组件写死字号，否则"整套等比缩放"会破。

## 4. 相邻知识在哪

| 想知道 | 去哪 |
|---|---|
| transcript 的几何/滚动法 | `docs/transcript-contract.md`（9 组不变量 + 单写者门禁） |
| 主题 token 与 antd 的关系 | `frontend-vue/src/stores/theme.ts`（头部注释）+ `docs/reasonix-ui-ux-gap-analysis.md` §4.10 |
| i18n / RTL 与书写方向 | `frontend-vue/src/i18n/README.md` |
| 拆分与"注入面"度量 | `docs/split-assessment.md` |
