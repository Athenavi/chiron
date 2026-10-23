# 界面精修设计稿（水墨 / 明度阶 / 双族动效）

> 状态：**设计稿，未实施**。目标读者：实现者与验收者。
> 范围：`frontend-vue/src` 的 token 底座 + 4 个组件 + 1 个提示封装。不改信息架构、不引入新依赖。
> 前置约束（有守卫，违反即红）：组件里**不许写色值字面量**（`check-theme-token-contract`）、
> **不许写动效时长字面量**（`check-motion-tokens`）、**8 个主题块的 token 名必须齐整**
> （`check-theme-token-parity`，且 `stores/theme.ts` 的 8 个 `*Tokens` 对象要同步）。

---

## 0. 一句话方向

**去掉"画框"，改用"光线"**：层次由相邻明度承担，交互由阴影的深浅与回弹表达；
暗色模式不靠加亮硬撑，而是**降字重、降字号**换取更少的光晕；
动效分两族 —— **拖拽过阻尼（不回头）**、**点赞欠阻尼（会回弹）**。

---

## 1. 视觉底座：明度阶替代 1px 边框

### 实现方式：别名层 —— **8 个主题块一行都不用改**

`style.md` 的分层规则里，②主题色板（8 块）只放"随主题变的值"，③兼容别名层放"对其它
token 的间接引用"。`--elev-*` 完全属于第三层：它只是 `--bg-*` 的语义别名。

```css
/* :root，放在 8 个主题块之后 */
--elev-0: var(--bg-base);
--elev-1: var(--bg-surface);
--elev-2: var(--bg-surface-hover);
--elev-3: var(--bg-elevated);
--hairline: var(--border-subtle);
--ring-inner: var(--border-strong);
--ring-outer: color-mix(in srgb, var(--primary) 12%, transparent);
```

主题切换时 `--bg-*` / `--primary` 变，别名自动跟随。**这是本文档最初设计里最容易做错的一处**
（原方案想在 8 个块里各写一份明度值，那会立刻被 parity 守卫判为漂移，且换主题必失真）。

- `color-mix()` 需要 Chrome 111+ / Safari 16.2+；不支持时 `--ring-outer` 解析失败，
  用 `var(--ring-outer, var(--border-subtle))` 兜底（外环退化为一条淡线，不影响可用性）。
- `--sig-*` 是**各主题的性格**（notion 圆角 3px、无阴影；aurora 玻璃感）。
  新样式**复用** `--sig-input-ring`（当前 2px）作为内环宽度，而不是覆盖它。

### 使用规则

- **卡片 / 输入框 / 按钮**：`background: var(--elev-1)`；**不写 `border`**。
- **唯一保留的线**：`--hairline`（列表分隔、表格行、表单分区）。它仍占 1px，但透明度低到
  "看起来是呼吸，不是画框"。
- **`--border-strong` 降级**：只用于输入框 focus 的**内环**，不再用于大面积描边。
- 暗色的层次方向与亮色相反：越靠前**越亮**（`elev-0` 最暗），否则暗色下"卡片会陷进背景"。

---

## 2. 暗色模式：字重 −40、字号降一档

### 字重

`--fw-*` 目前定义在 `:root`（400/500/600/700）。改为**可在暗色块内覆盖**：

| token | light | dark |
|---|---|---|
| `--fw-regular` | 400 | **360** |
| `--fw-medium` | 500 | **460** |
| `--fw-semibold` | 600 | **560** |
| `--fw-bold` | 700 | **660** |

- 前提：`--font-sans` 是 **Geist Variable**（可变字体，支持任意字重）✓。
- 若某主题回退到非可变字体，降级到最近整数（360→400）；在 `theme.ts` 注释里写明。
- **代价要认**：小字号 + 360 字重在低对比度背景下会"发虚"。补偿手段是 §1 的 `--elev-*`
  把背景压得更暗（对比度反而升），而不是把颜色调亮 —— 调亮就回到了"光晕"问题。

### 字号

- 实际基准是 **14px**（不是 15），暗色下 → **13px**。
- **小字号档要同步收窄偏移**：`--fs-xs` / `--fs-sm` 原本是 `-3px` / `-2px`，暗色下改为 `-2px` / `-1px`，
  否则 11px 会掉到 10px —— 暗色 + 360 字重下几乎不可读。
- 用 **`html.dark`**（特异性 0,1,1）一次性覆盖，而不是改 4 个暗色主题块：
  后者与 `[data-theme='x-dark']`（0,1,0）同级，会依赖源码先后；前者恒定胜出。
- 这仍然满足"字号只有一个入口"的既有纪律（`style.md` §3）：只改 token，组件里不写死字号。

---

## 3. 水墨流式打字

### 现状与限制

`MessageItem.vue:458-463` 流式期是**整条 `{{ item.content }}` 插值**，结束后才走 markdown。
所以逐字动画只能在"流式期的纯文本"上做 —— 这反而没有回归风险（本来就不解析 markdown）。

### 实现

流式期渲染为逐字 span（**只为新增字符**启动动画）：

```html
<span
  v-for="(ch, i) in chars"
  :key="i"
  class="ink"
  :style="{ '--i': i }"
>{{ ch }}</span>
```

```css
.ink {
  display: inline;
  animation: ink-in 140ms var(--ease-out) both;
  animation-delay: calc(var(--i) * 28ms);
}
@keyframes ink-in {
  from { filter: blur(6px); opacity: .35; transform: translateY(1px); }
  to   { filter: blur(0);    opacity: 1;   transform: none; }
}
```

- **错开 28ms**（"几十毫秒"取中值）；140ms 完成对焦。
- `--i` 用**本批新增字符的序号**，不是全局下标 —— 否则第 500 个字要等 14 秒才出现。
  实现上：流式追加时记录 `batchStart`，`--i = index - batchStart`。
- **行内图标下移 1/8 字高**：`.ink svg, .ink img { vertical-align: -0.125em }`。
- 尾光标保留，但改为"墨滴"（6px 圆点 + 1.2s 呼吸），不再是竖条。

### 性能护栏

- **逐字上限 2000 字**：`content.length > 2000` 时退回整条插值（不做 span）。
- 动画只在 `streaming` 为真时存在；结束切 markdown 后 span 全部卸载。
- `prefers-reduced-motion: reduce` → 关闭 blur/位移，仅 120ms opacity 淡入。

---

## 4. 数字：tabular-nums 锁死

- 新增工具类 `.tnum { font-variant-numeric: tabular-nums; font-feature-settings: 'tnum' }`。
- 必须应用的 5 处：思考时长秒数、`ChatStatusBar` 的 `durationSec`、`ChatInput` 的 `elapsed`、
  用量/成本/计费数字、消息时间戳。
- **思考时长加 `min-width: 3ch`**：`9s → 10s → 100s` 时容器宽度不跳（"避免乱晃"）。
- 不全局设 `body { font-variant-numeric }`：中文标点与全角数字的排版不受影响，但没必要
  让所有正文承担这个属性；按"显示数字的地方"精确加。

---

## 5. 思考面板：网格撑开 + 时长锁宽

### 展开动画（替换 `v-if`）

`ReasoningBlock.vue:114-137` 目前是 `<Transition>` + `v-if`（挂载/卸载，高度是跳变的）。
改为**常驻容器 + `grid-template-rows: 0fr → 1fr`**：

```html
<div class="think-grid" :class="{ open: expanded }">
  <div class="think-inner">
    <div class="think-body">…</div>
  </div>
</div>
```

```css
.think-grid {
  display: grid;
  grid-template-rows: 0fr;
  transition: grid-template-rows var(--dur-normal) var(--ease-out);
}
.think-grid.open { grid-template-rows: 1fr; }
.think-inner { min-height: 0; overflow: hidden; }
```

- 内部 `.think-body { max-height: 40vh; overflow: auto }` **保留**（长思考仍内部滚动）。
- 为什么是 `0fr→1fr` 而不是 `max-height`：后者要么写死上限（撑不满）、要么用 9999px
  （前 80% 的时间在"空跑"），网格是浏览器自己算高度，**丝滑且不需要知道内容多高**。
- 时长秒数：`.tnum` + `min-width: 3ch`。

---

## 6. 动效两族：拖拽过阻尼、点赞欠阻尼

### 新增 token（`:root`）

| token | 值 | 用途 |
|---|---|---|
| `--dur-instant` | `60ms` | 拖拽跟手、按下反馈 |
| `--dur-snap` | `120ms` | 吸附归位 |
| `--dur-spring` | `420ms` | 点赞回弹 |
| `--ease-snap` | `cubic-bezier(0.2, 0, 0, 1)` | **过阻尼**：快出、无回弹 |
| `--ease-spring` | `cubic-bezier(0.34, 1.56, 0.64, 1)` | **欠阻尼**：回弹一下再停 |

### 拖拽（雷厉风行）

- 拖动中：`transform` 直跟指针，`transition: none`；只在"松手归位"时用
  `transform var(--dur-snap) var(--ease-snap)`。
- `will-change: transform` **只在拖动期间加**，结束移除（长期挂着会吃显存）。

### 点赞 / 点踩（欢快 Q 弹）

- 按下：`scale(.86)`，`var(--dur-instant) var(--ease-snap)`。
- 松开：`scale(1)` + 图标 `pop`（`1 → 1.18 → 1`），`var(--dur-spring) var(--ease-spring)`。
- 选中态换色走 `--dur-fast`，**不参与回弹**（颜色回弹会显得脏）。
- 点踩同理但幅度减半（`1.09`），避免"负面操作也很欢快"的语义错位。

---

## 7. 输入框：三行起、八行止、双环咬合

### 高度

`ChatInput.vue:1023-1028` 现在 `rows=1` + `auto-size{minRows:1, maxRows:8}` → 改：

```html
<Input.TextArea :rows="3" :auto-size="{ minRows: 3, maxRows: 8 }" />
```

（编辑态 `MessageItem.vue:431-434` 保持 `1..8`，编辑短消息不该占三行。）

### 聚焦：双环咬合，不用亮线

```css
.input-card {
  background: var(--elev-1);
  border: 0;                       /* 扔掉描边 */
  box-shadow: var(--shadow-md);
  transition: box-shadow var(--dur-normal) var(--ease-out),
              background-color var(--dur-normal) var(--ease-out);
}
.input-card:focus-within {
  background: var(--elev-2);
  box-shadow:
    inset 0 0 0 1px var(--ring-inner),   /* 内环：同色系加深 */
    0 0 0 4px var(--ring-outer),         /* 外环：柔光 */
    0 6px 20px -8px var(--ring-outer);   /* 投影：把卡片"托起来" */
}
```

- **内环用 `inset`**：它替代原来的 1px 边框，但颜色来自主题、强度可控；
  视觉上像"边框被吸进去了"，而不是"外面套了一圈亮线"。
- 不写 `outline`（键盘可达性靠 `:focus-visible` 另行保留，见 §9）。

---

## 8. 提示：2.5s 消失

- 新增 `src/utils/notify.ts`，统一封装 antd `message`：
  `notify.success(msg)` → `message.success({ content: msg, duration: 2.5 })`。
- 替换范围：`MessageItem.vue`（复制/引用/重新生成/反馈）、`ChatView.vue`、`ChatInput.vue`
  里的 `message.*` 调用（约 20–30 处）。
- 错误提示同样 2.5s（antd 默认 3s），但如果同一秒内连续报错，交给 antd 的堆叠，
  不做去重（去重会吞掉真正不同的错误）。

---

## 9. 排版保护与可访问性

- **行末单字**：正文 `text-wrap: pretty`（Chrome 117+ / Safari 17.5+，不支持则无副作用）；
  标题与短句 `text-wrap: balance`。
- **长 URL / 英文**：`overflow-wrap: anywhere`（仅在 `.msg-text` 的正文容器，
  不作用于代码块 —— 代码块已有自己的横向滚动）。
- **键盘焦点**：去掉 `border` 后，`:focus-within` 的内环同时承担焦点提示；
  非输入类的可聚焦元素保留 `:focus-visible { outline: 2px solid var(--ring-inner) }`。
- **对比度**：暗色 360 字重 + 14px 是本次最需要复测的组合。
  验收要求仍按 4.5:1（正文）/ 3:1（大字），**不因为"看起来更柔和"就放宽**。
- **`prefers-reduced-motion`**：本项目**没有守卫**（`style.md` §2 已如实写明），
  这次必须手工覆盖三处：水墨逐字、思考面板撑开、点赞回弹。

---

## 10. 改动清单（按文件）

| 文件 | 改什么 |
|---|---|
| `src/style.css` | `:root` 加 `--dur-instant/--dur-snap/--dur-spring`、`--ease-snap/--ease-spring`；8 个主题块各加 `--elev-0..3`、`--hairline`、`--ring-inner/--ring-outer`，暗色块内覆盖 `--fw-*` 与 `--ui-font-size`；新增 `.tnum` 与 `.ink` 关键帧 |
| `src/stores/theme.ts` | **不需要加新键**：parity 守卫检查的是 8 个 antd token 对象**彼此**的键集一致，与 style.css 的 CSS 变量无关。只有想让 antd 控件的背景也跟随色阶时，才改 `colorBgContainer` 的**值**（不是加键） |
| `src/components/chat/MessageItem.vue` | 流式期逐字 span（≤2000 字）+ 批内 `--i`；`.msg-action` 的点赞/点踩弹簧；`message.*` → `notify.*`；正文 `text-wrap: pretty` |
| `src/components/chat/ReasoningBlock.vue` | `v-if` → 网格撑开；时长 `.tnum` + `min-width: 3ch` |
| `src/components/chat/ChatInput.vue` | `rows=3 / minRows=3`；`.input-card` 去 border、双环 focus；`message.*` → `notify.*`；elapsed 加 `.tnum` |
| `src/components/chat/ChatStatusBar.vue` | `durationSec` 加 `.tnum` |
| `src/utils/notify.ts`（新） | 2.5s 的统一提示封装 |
| `src/App.vue` | 全局 `text-wrap` 兜底；`prefers-reduced-motion` 的全局降级块 |

**不新增依赖**：弹簧用 `cubic-bezier` 近似，不引 `motion`/`gsap`。

---

## 11. 验收

1. `npm run check:ui` 八项守卫全绿（重点：parity 的键集、motion 的时长字面量、theme 的色值）。
2. `vue-tsc -b` 0；`vitest run` 通过；新增 2 个用例：`notify` 的 duration、`.ink` 的批内延迟计算。
3. 视觉对照：**8 张截图**（4 主题 × light/dark），检查"无边框后层次是否仍成立"。
4. 性能：2000 字逐字流式时无掉帧（DevTools Performance 长任务 < 50ms）；
   输入框从 3 行撑到 8 行时无跳动。
5. 对比度：暗色下正文 ≥ 4.5:1（360 字重是重点）。
6. reduced-motion 下三处动画都停（手工验，没有守卫）。

---

## 12. 分阶段落地

| 阶段 | 内容 | 风险 |
|---|---|---|
| P0 | token 底座 + 输入框（去边框/双环/三行）+ 提示 2.5s | 低：纯 token 与局部样式 |
| P1 | 思考面板网格撑开 + 数字锁死 + 排版保护 | 低 |
| P2 | 水墨逐字流式 | 中：逐字 DOM 的性能与 markdown 切换边界 |
| P3 | 点赞/拖拽弹簧 + 暗色字重/字号 | 中：字重降档需要逐主题复测对比度 |

---

## 13. 已定参数（2026-10-23 确认）

1. **字重**：暗色下 400/500/600/700 → **360/460/560/660**（−10%）。
2. **逐字上限**：**2000 字**；超过则退回整条插值（长回答不带水墨效果）。
3. **主题范围**：**全部 8 个**（4 主题 × light/dark），token 与暗色字重/字号一起铺。
4. **提示时长**：**成功类 2.5s / 错误类 4s**（错误需要读完再消失）。

> 实施顺序按 §12：P0（token 底座 + 输入框 + 提示）→ P1（思考面板 + 数字 + 排版）
> → P2（水墨逐字）→ P3（弹簧 + 暗色字重/字号）。

---

## 14. 后续修订：desktop-agent-ui 规约落地（2026-10-24）

参考 [Ludi-Herry/desktop-agent-ui](https://github.com/Ludi-Herry/desktop-agent-ui) 的桌面 Agent UI 规范后，
遵循其「**已有系统不折腾**」原则，只借用具体机制（不重构组件库、不换设计体系），落地了四项。
**其中两项覆盖了本文档前面的决策** —— 在此登记，原文保留作为当时的记录：

| 项 | 本文档原决策 | 现行 | 说明 |
|---|---|---|---|
| **最小字号** | §2「字号」：`--fs-xs` 在暗色下收窄到 **11px**（并解释"否则会掉到 10px"） | **12px 硬门禁** | 铁律 #6 严禁 < 12px。`--fs-xs` 在 `:root` 与 `html.dark` 都改为 **12px**；全仓 122 处 9/10/11px 硬编码字号统一改用 `var(--fs-xs)` |
| **中点分隔符** | §1/§5/§7 的若干提示文案用「·」连接（如"生成中 · 12s"） | **一律移除** | 铁律 #1 禁用中点，改用布局留白（flex 容器内由 `gap` 承担）。含代码 14 处 + 三语言文案 24 个文件 |
| **边框宽度** | §1「用明度阶替代 1px 边框」（**保留**） | 追加 **0.5px** | 铁律 #5：线条 / 描边 / 卡框统一 `var(--hairline-width)`（= 0.5px）。明度阶本体不动，只把"剩下的那条线"变细 |
| **同心圆圆角** | 未涉及 | **新增** | 铁律 #4：嵌套容器内层 = `max(0, 外层 − padding)`。约定写在 `style.css` 的 `--radius-*` 之后；外层用主题 token 时以 `calc` 跟随主题，**不写死像素** |

**同时新增的"防错机制"**（与上表无冲突、纯增益，出自同一份规范）：

- **CJK 输入法伴生 Enter 守卫**：`compositionend` 之后 500ms 内的 Enter 判定为"选词确认"的幽灵键，
  拦截并复位时间戳（`frontend-vue/src/components/chat/ime.ts`）。此前只判 `e.isComposing`，
  在某些输入法引擎或高负载下该事件会在 `isComposing` 转 false 之后到达，导致"只想选词却发出消息"。
- **分栏拖拽 Escape 取消闭环**：拖拽中途按 Escape 复位状态机、并把宽度还回拖拽前基准
  （`SessionPreviewPane.vue`），防止手柄 DOM 丢失后无法再展开。
- **composer 局部滚动条收敛**：输入框内隐藏原生滚动条外观，保留滚轮 / 触控 / 选中 / 键盘导航；
  **仅限输入框，不全局隐藏**。

**未做**：铁律 #6 的另一半「字号阶梯收敛到 ≤4 种」—— 现存 12/13/14/15/16/17/18/20px 多级，
收敛属于大范围视觉重构，不在本次范围内。

**验收**：`npm run build`（八项守卫 + `vue-tsc -b` + `vite build`）通过 · `npm test` 84 files / 639 tests。
`--hairline-width` 与 `--fs-xs` 都集中为 token：任一项若在真机上观感不对，改一处即可整体回退。
