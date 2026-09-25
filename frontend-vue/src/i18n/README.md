# 前端 i18n 约定

源语言是 **zh-CN**；当前支持 `zh-CN` / `en-US` / `ar`（RTL）。完整规划见仓库
`docs/i18n-plan.md`（后端错误码契约见 `internal/api/error_codes.go`）。

## 目录与单一来源

```
src/i18n/index.ts        实例、语言持久化、antd/dayjs/<html lang,dir> 联动、t() 入口
src/i18n/languages.ts    语言清单（唯一来源：code / 母语名 / dir / antd 包 / dayjs key）
src/locales/<lang>/      译文（zh-CN 为源语言，其余语言缺失的键自动回退到 zh-CN）
scripts/check-i18n.mjs   棘轮检查：禁止新增硬编码中文 UI 文案
scripts/i18n-baseline.json  存量账本（只允许下调）
```

## 写文案

组件内用 `useI18n()`；非组件模块（axios 拦截器、utils、store）用 `src/i18n/index.ts`
导出的 `t()` —— 后者不随语言切换重渲染，仅适合一次性字符串。

```vue
<script setup lang="ts">
import { useI18n } from 'vue-i18n'
const { t } = useI18n()
</script>
<template>
  <a-button>{{ t('common.save') }}</a-button>
</template>
```

key 规范：`<域>.<语义>`（`chat.input.send`、`errors.quota_exceeded`）。禁止把整句话当 key，
禁止在模板里拼接语义片段（语序在不同语言里不同，拼接必然出错）。

带参数用命名插值：`t('errors.http_status', { status })`，译文写 `请求失败（HTTP {status}）`。

## 新增语言的步骤

1. `src/i18n/languages.ts` 增加一项（顺带确认 antd 有对应语言包、dayjs 有 locale）；
2. 新建 `src/locales/<code>/`（先复制 zh-CN 结构，再逐个替换）；
3. `src/i18n/index.ts` 里 `import 'dayjs/locale/<key>'` 并加入 `messages`。

## RTL

`<html dir="rtl">` 由 `applyDocumentLocale()` 写入（`main.ts` 在 mount 前调用，避免首屏
跳变）。**新增样式请用逻辑属性**：`margin-inline-*` / `padding-inline-*` / `inset-inline-*` /
`text-align: start|end`，不要写 `left/right`、`margin-left`。纯数值宽度（`min-width`）在 RTL
下也应按行内方向表达为 `min-inline-size`。

## 后端错误文案

后端在失败响应里带稳定 `code`（如 `quota_exceeded`），前端按当前语言渲染
（`src/locales/<lang>/errors.ts`），`error` 字段仅作为兜底。优先级与实现见
`src/utils/apiError.ts`。**新增后端错误码时，必须同时补三个语言的 `errors.ts`。**

## 迁移流程（存量账本见 `scripts/i18n-baseline.json`，勿在此处写死数字）

1. **模板文案走 codemod**：`node scripts/i18n-codemod.mjs`（默认 dry-run，`--apply` 写入，
   `--only <file>` 单文件）。把 Vue **模板**里的中文静态文案换成 `$t('原文')`。
2. **`<script setup>` 内的 UI 文案走第二个 codemod**：`node scripts/i18n-codemod-script.mjs`
   （同样 dry-run / `--apply` / `--only`）。它按**行级白名单**替换并自动注入 `useI18n`：
   - 只处理命中 `label:` / `title:` / `placeholder:` / `description:` / `hint:` / `tooltip:` /
     `okText:` / `cancelText:` / `errorText:` / `emptyText:` / `text:` / `content:` 或
     `message.success|error|warning|info(` 的行；
   - **整行跳过**含 `===` `!==` `case` `switch` `.includes(` `.startsWith(` `.endsWith(` /
     `indexOf(` 的行 —— 那些中文是**参与比较的数据值**，i18n 化会造成永久失配；
   - 只处理单引号字面量（反引号有插值、双引号在 HTML 属性里更常见）；
   - 注入点按 import 语句的**结束位置**计算（见下方踩坑 1）。
   两个 codemod 的原文都收进 `locales/zh-CN/legacy.ts`；`en-US|ar/legacy.ts` 留空即
   "待翻译"（vue-i18n 回退到 zh-CN）。
3. `node scripts/check-i18n.mjs --write-baseline` 下调基线，锁定成果。
4. `node scripts/i18n-inventory.mjs` 产出逐文件清单与**高频短语**（≥3 次），
   高频短语优先收敛到 `common` 域，能显著减少后续 key 数量。
5. 跑 `pnpm exec vue-tsc --noEmit` + `pnpm exec vitest run` 验证（顺序很重要，见踩坑 1）。

`pnpm run build` 已把 `check-i18n.mjs` 挂进 `check:ui`：新增**裸露**中文会让构建失败
（`$t('…')` / `t('…')` 里的中文不算硬编码 —— 那是迁移目标）。

### codemod 的已知局限（需人工收尾）

- **跨标签/跨行文本会被切开**（如 `让 AI Agent` + 后缀分处两个标签）→ 译文语序无法维护，
  需人工合并为一条 key；
- 含 `&nbsp;` 等 HTML 实体、含 `{{ }}`/`${}` 插值、含 `(` `)` `:` 等结构字符的文案被
  **跳过**（保守），仍在基线里；
- 模板属性只处理白名单（`label/title/placeholder/description/hint/ok-text/cancel-text/empty-text/alt`）；
- **普通 `<script>`（模块作用域）与独立 `.ts`**：由 `i18n-codemod-ts.mjs` 处理，注入
  `import { t } from '<相对路径>/i18n'`。注意它是**非响应式**的 —— 模块级常量在求值时翻译一次，
  语言切换后不会重新求值；模块级表格列定义之类若需跟随语言，应把定义移进组件的 `computed`
  （那属于 `i18n-codemod-script.mjs` 的范围）；
- 生成的 key 是**原文**（gettext 风格），语义化 key 改造是独立任务（只改 key，不动文案）。

### codemod 踩到的坑（都很贵，写在最前面）

1. **注入点必须按 import 语句的结束位置算，不能按 `^import` 行**：多行 import
   （`import {\n  a,\n} from 'x'`）的**首行**同样匹配 `/^import .*$/`，声明会被插进花括号里 →
   `vue/compiler-sfc` 报 `Unexpected keyword 'import'`，**整块 setup 编译失败、测试套件级挂掉**
   （不是断言失败，是文件根本没跑）。现在用三个正则（单行 import / `} from 'x'` 收尾行 /
   副作用 import）取"结束位置"的最大值。
2. **生成的键含换行会撑破字符串**：模板里换行书写的长句，抽取后写进 `legacy.ts` 会出现
   `Unterminated string` → **整个应用与全部测试一起挂**（`test-setup.ts` 全局加载 i18n）。
   现在 codemod 直接跳过跨行文本，`esc()` 也补了 `\r\n\t` 转义。

两条共同教训：**生成语言包后必须先跑 `vue-tsc` 再跑测试** —— 语法错误会一次性打挂全部测试，
从测试输出里很难看出是"某个语言包文件坏了"。出问题时的回滚路径：
`git checkout -- $(git diff --name-only -- 'frontend-vue/**/*.vue')` 后重跑三个 codemod（幂等）。

## 首批迁移（chat 相对时间）踩到的四个坑

以 `formatRelativeTime`（`src/components/chat/chat-types.ts`）+ `ChatSidePanel.vue` 为样板，
实际踩到并已处理：

1. **模板变量遮蔽**：`ChatSidePanel.vue` 模板里有 `v-for` 循环变量就叫 `t`，setup 里再
   `const { t } = useI18n()` 会被模板作用域遮蔽（`vue/no-template-shadow`）——循环内调用
   `t()` 会串到循环变量上。改用 `const { t: tr } = useI18n()`。
2. **响应式 vs 一次性**：utils 里的纯函数不要把 `t` 直接 import 进来用到底，应把翻译函数
   作为**参数注入**（`translate` 形参，默认回退全局 `t`），组件内传 `useI18n()` 的 `t`；
   否则语言切换后该处文案不会立即更新。
3. **测试需要 i18n 实例**：组件一旦调用 `useI18n()`，未提供实例的 spec 会在 setup 阶段直接
   抛错（整个文件全挂）。已在 `src/test-setup.ts` 全局注册
   `config.global.plugins = [i18n]`，新 spec 不必各自 mount 传 plugins。
4. **日期不要硬编码 locale**：`toLocaleDateString('zh-CN', …)` → `dayjs(d).format('MMM D')`，
   dayjs 的 locale 由 `setLocale()` 同步，自动跟随界面语言。日期格式化统一入口见
   `src/utils/datetime.ts`（`formatDateTime/formatDate/formatTime/formatMonthDay/formatSmart`）。

### 批量迁移踩到的三个坑（codemod 相关）

1. **跨行原文会撑破生成的字符串**：模板里换行书写的长句，抽取出的键含 `\n` →
   `locales/zh-CN/legacy.ts` 出现 `Unterminated string` → **整个应用与全部测试一起挂**
   （因为 test-setup 全局加载 i18n）。现在 codemod 直接跳过跨行文本，`esc()` 也补了
   `\r\n\t` 转义。教训：生成语言包后必须跑一次 `vue-tsc`，语法错误会一次性打挂所有测试。
2. **护栏口径必须排除 `$t('中文')`**，否则正确的迁移会被判成"新增硬编码"，基线失去公信力。
3. **`locales/` 目录必须从扫描中排除** —— 那里的中文就是译文产物。

护栏自身的两处假阳性也已修（`scripts/check-i18n.mjs`）：多行注释（HTML/块注释）必须整体
剥离后再统计；`src/locales/` 必须从扫描中排除 —— 那里的中文就是译文产物。

## 收尾阶段（存量归零后）踩到的三个坑

都属"tsc 不报、只有 lint/运行时才炸"的类型：

1. **`t` 未定义要靠 ESLint 兜底**：批量替换时容易在 `<script setup>` 里直接写 `t(...)`，
   而该文件并没有 `useI18n()`。`vue-tsc` **不会**报（Vue 宏的类型较宽松），
   `eslint` 的 `no-undef` 才是那道网 —— 所以 i18n 改动必须跑 lint，不能只看 tsc。
2. **三元表达式不能当语句**：`v ? message.success(...) : message.success(...)` 能跑，
   但 ESLint 的 `no-unused-expressions` 会报错；应写成 `if (v) … else …`。
3. **HTML 属性里的 `&#10;` 会被解码成真换行**：`:placeholder="$t('如 a&#10;b')"` 解码后
   内联 JS 字符串被换行截断（`Unterminated string literal`）。要换行请用 `\n` 转义。

## 常量表：语言切换后"冻住"的典型来源

`const XXX = { label: t('…') }` 这类**模块级**常量在模块加载时求值一次，切换语言后不会更新。
独立 `.ts` 里 `import { t }` 拿到的函数同样是非响应式的。处理方式：

| 形态 | 改法 |
|---|---|
| 组件内的映射表 | 改成 `computed(() => ({ … t('…') }))`，使用处加 `.value` |
| store / 工具模块里的常量表 | 改成函数（每次调用求值），或 store 的 computed |
| 值本身就是 i18n key 的表（如 `EFFORT_LABEL`） | 表保持原文，**取值时**再 `t()` |
| 跨语言必须稳定的分组键 | 键用英文标识（`today`/`yesterday`…），只翻译显示标签 |

`check-i18n.mjs` 还会用**真实 vue-i18n 编译器**逐个编译 legacy 的键，并拒绝
"插值之外出现 `|` / `@`"的消息（`|` 是复数分隔符，会静默只返回一个分支）；
`<style>` 块整块不计入（CSS 的 `content` 没有 `t()` 可用）。
