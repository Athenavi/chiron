/**
 * 注释剥离 —— 前端源码守卫的**公共判据**。
 *
 * **为什么要共享**：这几个 token 棘轮都是"在源码里找违规写法"，而**注释也能满足字面量匹配**。
 * 2026-10-09 实测：只往一个 .vue 里加**纯注释**（`/* 备注：z-index: 9999 *\/`、
 * `/* 备注：transition: opacity 0.2s *\/`、`/* 备注：color: #fff *\/`），
 * `check-z-index-tokens` / `check-motion-tokens` / `check-theme-token-contract` **三个全部误报**（exit 1）。
 *
 * 误报的代价不是"麻烦"：基线一旦被假阳性顶红，人就会去**上调基线**，护栏随之失效 ——
 * 这正是 `check-a11y.mjs` 早就写下的那条理由（"假阳性会让基线失去公信力"）。
 * 所以判据要建立在**可执行代码**上，和 Python 侧 `tests/source_scan.py` 是同一条纪律。
 *
 * 剥离口径：HTML/Vue 模板注释、块注释、行注释（行注释避开 `http://` 这类字符串）。
 * 注释内容用空格替换但**保留其中的换行** —— 行号必须与原文件一致，否则失败信息里的
 * 位置无法定位（跨行注释整体塌缩会让后面所有行号前移）。
 */
export function stripComments(source) {
  const blank = text => text.replace(/[^\n]/g, ' ')
  let out = source.replace(/<!--[\s\S]*?-->/g, blank)
  out = out.replace(/\/\*[\s\S]*?\*\//g, blank)
  out = out
    .split(/\r?\n/)
    .map(line => line.replace(/(^|[^:'"`\\])\/\/.*$/, '$1'))
    .join('\n')
  return out
}
