/**
 * legacy 域：**存量自动抽取**的文案（原文即 key）。
 *
 * 来源：scripts/i18n-codemod.mjs（模板）、scripts/i18n-codemod-script.mjs（<script setup>）、
 * scripts/i18n-codemod-ts.mjs（独立 .ts）对中文字面量的替换。
 * 约定：
 *   - gettext 风格 msgid —— 键就是 zh-CN 原文，因此本文件即源语言译文；
 *   - en-US / ar 的对应文件可留空（vue-i18n 会回退到本文件），
 *     翻译是可并行的独立任务，不阻塞代码；
 *   - 后续「语义化 key」改造只需改键与调用点，不动文案。
 */
export default {
}
