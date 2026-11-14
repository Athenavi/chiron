# 前端 i18n 约定

源语言 **zh-CN**，当前支持 `zh-CN` / `en-US` / `ar`（RTL）。后端错误码契约见
`internal/api/error_codes.go`。

> 本文件**就是** i18n 的完整规划：语言清单、key 规范、迁移流程、RTL 口径都在这里。
> 不再维护第二份重叠文档。

## 目录与单一来源

```
src/i18n/index.ts          实例、语言持久化、antd/dayjs/<html lang,dir> 联动、t() 入口
src/i18n/languages.ts      语言清单（唯一来源：code / 母语名 / dir / antd 包 / dayjs key）
src/locales/<lang>/        译文（zh-CN 为源语言，缺失键自动回退）
scripts/check-i18n.mjs     棘轮：禁止新增硬编码中文 UI 文案
scripts/i18n-baseline.json 存量账本（只允许下调）
```

## 写文案

组件内用 `useI18n()`；非组件模块（axios 拦截器、utils、store）用 `index.ts` 导出的 `t()`
—— 后者**非响应式**，仅适合一次性字符串。

```vue
<script setup lang="ts">
import { useI18n } from 'vue-i18n'
const { t } = useI18n()
</script>
<template>
  <a-button>{{ t('common.save') }}</a-button>
</template>
```

key 规范 `<域>.<语义>`（`chat.input.send`、`errors.quota_exceeded`）。禁止把整句话当 key，
禁止在模板里拼接语义片段（语序因语言而异，拼接必然出错）。带参数用命名插值：
`t('errors.http_status', { status })`，译文写 `请求失败（HTTP {status}）`。

## 新增语言

1. `src/i18n/languages.ts` 增加一项（确认 antd 有语言包、dayjs 有 locale）；
2. 新建 `src/locales/<code>/`（复制 zh-CN 结构再逐条替换）；
3. `index.ts` 里 `import 'dayjs/locale/<key>'` 并加入 `messages`。

## RTL

`<html dir="rtl">` 由 `applyDocumentLocale()` 在 mount 前写入（避免首屏跳变）。
**新增样式一律用逻辑属性**：`margin-inline-*` / `padding-inline-*` / `inset-inline-*` /
`text-align: start|end`，不写 `left/right`、`margin-left`；纯数值宽度用 `min-inline-size`。

## 后端错误文案

后端失败响应带稳定 `code`（如 `quota_exceeded`），前端按当前语言渲染
（`src/locales/<lang>/errors.ts`），`error` 字段仅兜底。优先级与实现见 `src/utils/apiError.ts`。
**新增错误码必须同时补三个语言的 `errors.ts`。**

## 迁移存量（账本见 `scripts/i18n-baseline.json`，勿在文档里写死数字）

1. 模板文案：`node scripts/i18n-codemod.mjs`（默认 dry-run，`--apply` 写入，`--only <file>`）
2. `<script setup>` 内 UI 文案：`node scripts/i18n-codemod-script.mjs`，按行级白名单替换并注入
   `useI18n`；模块作用域与独立 `.ts` 由 `i18n-codemod-ts.mjs` 处理
3. `node scripts/check-i18n.mjs --write-baseline` 下调基线锁定成果
4. `node scripts/i18n-inventory.mjs` 产出清单与高频短语（≥3 次），高频短语优先收敛到 `common`

**顺序固定：先 `vue-tsc` 再测试。** 生成的语法错误会一次性打挂全部测试（`test-setup.ts`
全局加载 i18n），从测试输出看不出是语言包坏了。codemod 幂等，回滚用
`git checkout -- $(git diff --name-only -- 'frontend-vue/**/*.vue')` 后重跑。

### codemod 不做的事（需人工收尾）

- 跨标签/跨行文本会被切开 → 译文语序无法维护，需人工合并为一条 key；
- 含 HTML 实体、`{{ }}`/`${}` 插值、结构字符的文案**跳过**（保守），仍在基线里；
- 模板属性只处理白名单（`label/title/placeholder/description/hint/ok-text/cancel-text/empty-text/alt`）；
- 生成的 key 是**原文**（gettext 风格），语义化 key 改造是独立任务。

## 常量表：语言切换后"冻住"的来源

`const XXX = { label: t('…') }` 这类模块级常量在加载时求值一次，切换语言不更新；
独立 `.ts` 里 `import { t }` 同样非响应式。

| 形态 | 改法 |
|---|---|
| 组件内的映射表 | 改成 `computed(() => ({ … t('…') }))` |
| store / 工具模块里的常量表 | 改成函数，或 store 的 computed |
| 值本身就是 i18n key 的表（如 `EFFORT_LABEL`） | 表保持原文，**取值时**再 `t()` |
| 跨语言必须稳定的分组键 | 键用英文标识（`today`/`yesterday`），只翻译显示标签 |

`check-i18n.mjs` 会用真实 vue-i18n 编译器逐个编译 legacy 键，并拒绝"插值之外出现 `|` / `@`"
的消息（`|` 是复数分隔符，会静默只返回一个分支）；`<style>` 块整块不计入。
