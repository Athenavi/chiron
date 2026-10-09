# 核心组件：Vue 前端

> **权威来源**：`docs/transcript-contract.md` · `frontend-vue/src/style.md`（CSS 知识索引）·
> `frontend-vue/src/i18n/README.md`（i18n 约定）。本页只给「代码在哪、门禁守什么」。

## 代码在哪

```
frontend-vue/src/
  views/          页面（ChatView 是最大的一处：会话界面）
  components/     组件；components/chat/ 是 transcript 的渲染端
  stores/         Pinia（theme.ts 是主题与「双源」的另一半）
  composables/    组合式函数
  api/            HTTP 客户端封装
  utils/          工具（datetime / uploader / requestOptimizer / toolFamily …）
  i18n/ locales/  多语言（zh-CN / en-US / ar，含 RTL）
  router/         路由与进度条
  types/          共享类型
  style.css       全局样式与 .chat-* 原语（为什么这么写见 style.md）
frontend-vue/scripts/   check:ui 的 8 道棘轮（check-*.mjs）
```

## 门禁：`check:ui` 八道棘轮

`pnpm run build` 会依次跑 **`check:ui`（8 道棘轮）→ `vue-tsc -b` → `vite build`**：

| 棘轮 | 守什么 |
|---|---|
| `check-z-index-tokens` | 层级只能来自 token |
| `check-theme-token-contract` / `check-theme-token-parity` | 主题 token 契约；8 个 `[data-theme]` 块与 8 个 `*Tokens` 对象各共享一套键集 |
| `check-motion-tokens` | 动效时长只能来自 token |
| `check-i18n` / `check-i18n-keys` | 文案不许硬编码；三语言键集一致 |
| `check-a11y` | 机械 a11y 规则 |
| `check-transcript-scroll` | **`scrollTop` 单写者**（源码级；见 [transcript](transcript.md)） |

**基线全部为空、新增即失败** —— 遇到红先判断是"新增"还是"基线该缩小"，**不要调大基线**。

## 三条容易踩的约定

1. **主题是双源**：CSS 变量（`style.css` 的 `[data-theme]` 块）与 TS token 对象（`stores/theme.ts`）
   必须各自**内部齐整**；`cssThemeId` 拼成 `<theme>-<light|dark>`，**拼错主题块就匹配不上**。
2. **i18n 的日期**必须走 `utils/datetime.ts`（统一入口，跟随界面语言）——
   历史上 20+ 处写死 `toLocaleString('zh-CN')`，切语言后日期仍是中文格式。
3. **本仓没开 eslint 的 `argsIgnorePattern`** ⇒ 形参写出来就要显式 `void`。

## 延伸阅读

- [transcript](transcript.md) —— 前端最核心的契约
- [架构](architecture.md) · [生产：测试](../production/testing.md)
- `frontend-vue/src/style.md` —— 三层 CSS 与四个守卫的**实际**范围
