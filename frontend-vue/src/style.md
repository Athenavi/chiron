# `style.css` 的「为什么」

> 权威在 `style.css` 的分节注释里。本文件只回答「改之前该知道什么」，**不复制取值**
> —— 取值会变，复制必失真（本文曾抄写行数、token 数、`!important` 处数，后来全部对不上）。

## 1. 分层：改之前先认清改哪一层

| 层 | 位置 | 什么时候变 |
|---|---|---|
| **① 与主题无关的固定量** | `:root` | 极少变：层级（z-index）、界面字号缩放入口、动画周期、饱和色底上的前景色、全屏查看器压暗底、投影、工作流类目色、macOS 红黄绿灯、搜索命中高亮 |
| **② 主题色板** | 8 个 `[data-theme='<id>-<light\|dark>']` 块 | 换主题 / 改色板时（notion / paper / terminal / aurora × light / dark） |
| **③ 兼容别名层** | 与主题无关 | 只在迁移期存在；值是 `var(--x)` 间接引用，主题切换时随之变化 |

**判断法则**：值**随主题变** ⇒ ②；**不随主题变** ⇒ ①。例如 z-index 定义在 `:root` 而不是每个主题块里，
否则 8 份重复且必然漂移。

**字号只有一个入口**：`:root` 里的缩放量。不要给单个组件写死字号，否则「整套等比缩放」会破。
反过来，**几何量（高度、圆角）不跟随字号缩放**，保持固定像素 —— 按钮阶梯即按此处理（见 `style.css` 按钮分节）。

## 2. 不变量（有守卫，别绕过）

四个守卫都在 `npm run check:ui`，都是**棘轮**（存量记在基线里容忍、**只许下调**）：

| 守卫 | 管什么 |
|---|---|
| `scripts/check-theme-token-parity.mjs` | 8 个 `[data-theme]` 块共享同一套 token 名；`stores/theme.ts` 的 8 个 `*Tokens` 对象共享同一套键 |
| `scripts/check-theme-token-contract.mjs` | 组件里**硬编码色值**（`#fff`、`rgba(0,0,0,.04)` …）⇒ 换主题时不跟着变 |
| `scripts/check-z-index-tokens.mjs` | 组件里**裸 `z-index` 数字** ⇒ 层叠关系不可推理 |
| `scripts/check-motion-tokens.mjs` | 组件里**硬编码动效时长**（`transition: opacity 0.2s`）⇒ 节奏无法统一调 |

**违反的后果**：前两条 ⇒ 某主题下组件拿到未定义变量 / 回退色，界面上只是"某个角落颜色不对"；
后两条 ⇒ 想统一调层级或节奏时得逐处找。

> **没有守卫的（如实写明，别以为门禁绿就万事大吉）**：
> ① `prefers-reduced-motion` 的降级 —— `check-motion-tokens` 只管**时长字面量**，不管有没有写这条 media query；
> ② **对比度**；③ **`!important` 的数量**。这三项**只靠评审**。

## 3. 相邻知识在哪

| 想知道 | 去哪 |
|---|---|
| transcript 的几何 / 滚动法 | `docs/transcript-contract.md` |
| 主题 token 与 antd 的关系 | `frontend-vue/src/stores/theme.ts` 头部注释 |
| i18n / RTL 与书写方向 | `frontend-vue/src/i18n/README.md` |
