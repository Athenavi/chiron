<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { CaretRightOutlined, ExclamationCircleOutlined } from '@ant-design/icons-vue'
import type { ToolResultItem } from './chat-types'
import { looksLikeDiff, parseUnifiedDiff, type ParsedDiff } from './diffParse'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

const props = defineProps<{ item: ToolResultItem }>()
// 失败结果默认展开：错误信息藏在折叠后面等于没报错
const expanded = ref(props.item.isError)
watch(() => props.item.isError, isError => { if (isError) expanded.value = true })

interface ParsedResult {
  kind: 'read' | 'terminal' | 'search' | 'diff' | 'json' | 'text'
  read?: any
  terminal?: any
  search?: any
  diff?: ParsedDiff
  path?: string
  text: string
}

// S 修复：仅放行安全光栅格式的 data: URI，拒绝 svg+xml 等可携带外部资源/脚本语义的类型
const isImageData = computed(() => {
  const c = props.item.content
  return /^data:image\/(png|jpe?g|gif|webp);base64,/.test(c)
})

// 分型解析：改动 diff / read_file / 终端 / 搜索 / JSON / 文本
const parsed = computed<ParsedResult>(() => {
  const c = props.item.content
  if (isImageData.value) return { kind: 'text', text: '' }
  let obj: any = null
  try { obj = JSON.parse(c) } catch { /* not json */ }

  // edit_file 返回 { path, success, diff }（python-engine/app/tools/edit_file.py）
  if (obj && typeof obj === 'object' && typeof obj.diff === 'string' && obj.diff) {
    return {
      kind: 'diff',
      diff: parseUnifiedDiff(obj.diff),
      path: typeof obj.path === 'string' ? obj.path : '',
      text: '',
    }
  }
  if (obj && typeof obj === 'object' && 'path' in obj && 'content' in obj && obj.content !== undefined) {
    return { kind: 'read', read: obj, text: '' }
  }
  if (obj && typeof obj === 'object' && ('stdout' in obj || 'exit_code' in obj)) {
    return { kind: 'terminal', terminal: obj, text: '' }
  }
  if (obj && typeof obj === 'object' && Array.isArray(obj.matches)) {
    return { kind: 'search', search: obj, text: '' }
  }
  if (obj && typeof obj === 'object') {
    return { kind: 'json', text: JSON.stringify(obj, null, 2) }
  }
  // 纯文本里携带的 patch（例如 run_code 直接打印 diff）同样按 diff 渲染
  if (looksLikeDiff(c)) return { kind: 'diff', diff: parseUnifiedDiff(c), text: '' }
  const text = c.length <= 6000 ? c : `${c.slice(0, 3000)}\n...(truncated ${c.length - 6000} chars)...\n${c.slice(-3000)}`
  return { kind: 'text', text }
})

// read 分型的行号行
const readLines = computed(() => {
  const read = parsed.value.read
  if (!read) return []
  const content = typeof read.content === 'string' ? read.content : String(read.content || '')
  const offset = Number(read.offset || 0)
  return content.split('\n').map((line: string, i: number) => ({ n: offset + i + 1, line }))
})

// 搜索分型：按文件分组（deepseek SearchBlock）
const searchFiles = computed(() => {
  const s = parsed.value.search
  if (!s) return []
  const groups = new Map<string, { path: string; matches: { line: number; text: string }[] }>()
  for (const m of s.matches || []) {
    const path = m.path || ''
    if (!groups.has(path)) groups.set(path, { path, matches: [] })
    groups.get(path)!.matches.push({ line: Number(m.line) || 0, text: String(m.text ?? '') })
  }
  return Array.from(groups.values())
})

/** 终端输出：stdout/stderr 合成一段（stderr 显式标注，避免混进 stdout 里看不出） */
const terminalText = computed(() => {
  const t = parsed.value.terminal
  if (!t) return ''
  const out = t.stdout || t.output || ''
  return t.stderr ? `${out}\n[stderr] ${t.stderr}` : out
})
const terminalLines = computed(() => (terminalText.value ? terminalText.value.split('\n').length : 0))
const textLines = computed(() => (parsed.value.text ? parsed.value.text.split('\n').length : 0))

// 大 diff 先给一段可读的预览，展开按钮写明总行数（滚动容器仍然兜底）
const DIFF_PREVIEW_LINES = 60
const diffExpanded = ref(false)
const visibleDiffLines = computed(() => {
  const lines = parsed.value.diff?.lines ?? []
  return diffExpanded.value ? lines : lines.slice(0, DIFF_PREVIEW_LINES)
})
const diffHidden = computed(() => Math.max(0, (parsed.value.diff?.lines.length ?? 0) - DIFF_PREVIEW_LINES))
const diffTitle = computed(() => {
  const files = parsed.value.diff?.files ?? []
  if (files.length > 1) return t('{n} 个文件', { n: files.length })
  return files[0] || parsed.value.path || 'diff'
})
</script>

<template>
  <div
    class="tool-result"
    :class="{ error: item.isError }"
  >
    <div
      v-if="item.isError"
      class="result-error-bar"
    >
      <ExclamationCircleOutlined />
      <span>{{ $t('工具执行失败') }}</span>
    </div>

    <img
      v-if="isImageData"
      :src="item.content"
      class="result-image"
      alt="tool result"
      loading="lazy"
      decoding="async"
    >

    <!-- 改动 diff：banner（文件 + 增删统计）+ 行级着色 -->
    <div
      v-else-if="parsed.kind === 'diff'"
      class="diff-block"
    >
      <div class="diff-banner">
        <span class="diff-files">{{ diffTitle }}</span>
        <span class="diff-stat">
          <span class="diff-add">+{{ parsed.diff!.additions }}</span>
          <span class="diff-del">−{{ parsed.diff!.deletions }}</span>
        </span>
      </div>
      <div class="diff-body">
        <div
          v-for="(line, i) in visibleDiffLines"
          :key="i"
          class="diff-line"
          :class="line.kind"
        >{{ line.text || ' ' }}</div>
      </div>
      <button
        v-if="diffHidden > 0"
        class="diff-more"
        type="button"
        @click="diffExpanded = true"
      >
        {{ $t('展开全部（还有 {n} 行）', { n: diffHidden }) }}
      </button>
    </div>

    <!-- read 卡片：banner + 行号 gutter（deepseek ReadBlock） -->
    <div
      v-else-if="parsed.kind === 'read'"
      class="read-block"
    >
      <div class="read-banner">
        <span class="read-path">{{ parsed.read.path }}</span>
        <span class="read-count">{{ $t('{n} 行', { n: parsed.read.total_lines }) }}</span>
      </div>
      <div class="read-body">
        <div
          v-for="row in readLines"
          :key="row.n"
          class="read-line"
        >
          <span class="line-no">{{ row.n }}</span>
          <span class="line-text">{{ row.line || ' ' }}</span>
        </div>
      </div>
    </div>

    <!-- 终端卡片（deepseek TerminalBlock 语义） -->
    <div
      v-else-if="parsed.kind === 'terminal'"
      class="terminal-block"
    >
      <div class="terminal-banner">
        <span class="terminal-label">{{ $t('终端输出') }}</span>
        <span
          v-if="terminalLines"
          class="terminal-lines"
        >{{ $t('{n} 行', { n: terminalLines }) }}</span>
        <span
          class="terminal-exit"
          :class="{ nonzero: parsed.terminal.exit_code }"
        >exit {{ parsed.terminal.exit_code ?? '?' }}</span>
      </div>
      <pre class="terminal-body">{{ terminalText }}</pre>
    </div>

    <!-- 搜索卡片（deepseek SearchBlock：banner + 行号 + pre 水平滚动） -->
    <div
      v-else-if="parsed.kind === 'search'"
      class="search-block"
    >
      <div class="search-header">
        <span class="search-summary">{{ $t('{matched} 个匹配 · {files} 个文件', { matched: parsed.search.count ?? searchFiles.length, files: searchFiles.length }) }}</span>
      </div>
      <div class="search-body">
        <template
          v-for="file in searchFiles"
          :key="file.path"
        >
          <div class="search-file">
            {{ file.path }}
          </div>
          <div
            v-for="m in file.matches"
            :key="m.line"
            class="search-line"
          >
            <span class="search-line-no">{{ m.line }}</span>
            <span class="search-line-text">{{ m.text }}</span>
          </div>
        </template>
      </div>
    </div>

    <!-- JSON / 代码 / 文本：可折叠 -->
    <template v-else>
      <button
        class="result-head"
        type="button"
        :aria-expanded="expanded"
        @click="expanded = !expanded"
      >
        <CaretRightOutlined
          class="chat-chevron"
          :class="{ open: expanded }"
        />
        <span class="result-label">{{ item.isError ? $t('结果（失败）') : $t('结果') }}</span>
        <span
          v-if="textLines > 1"
          class="result-lines"
        >{{ $t('{n} 行', { n: textLines }) }}</span>
      </button>
      <template v-if="expanded">
        <pre
          v-if="parsed.kind === 'json'"
          class="result-code"
        >{{ parsed.text }}</pre>
        <pre
          v-else-if="parsed.kind === 'text' && parsed.text.includes('\n')"
          class="result-code"
        >{{ parsed.text }}</pre>
        <div
          v-else
          class="result-text"
        >
          {{ parsed.text }}
        </div>
      </template>
    </template>
  </div>
</template>

<style scoped>
.tool-result { max-width: min(var(--chat-content-width), 92%); margin: 2px auto 8px; }
.result-error-bar { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; padding: 5px 10px; border-radius: var(--sig-radius-button); background: var(--error-bg); color: var(--error); font-size: 12px; }
.result-head { display: flex; align-items: center; gap: 8px; width: 100%; padding: 4px 0; border: none; background: none; color: var(--text-tertiary); cursor: pointer; font-size: 12px; }
.result-head:hover { color: var(--primary); }
.result-lines { flex: none; font-size: 11px; color: var(--text-tertiary); }
.tool-result.error .result-label { color: var(--error); }
.result-code { margin: 0; padding: 12px; background: var(--bg-code); border-radius: var(--sig-radius-code); font-family: var(--font-mono); font-size: 12px; line-height: 1.6; color: var(--text-code); white-space: pre-wrap; word-break: break-all; }
.result-text { padding: 10px 12px; background: var(--bg-secondary); border-radius: var(--sig-radius-button); font-size: 13px; color: var(--text-secondary); white-space: pre-wrap; word-break: break-all; }
.result-image { max-width: min(320px, 80vw); max-height: 240px; border-radius: var(--sig-radius-card); border: 1px solid var(--border-card); display: block; margin-top: 4px; }

/* 失败结果：卡片 banner 统一转错误色，扫一眼就能定位是哪一步断了 */
.tool-result.error .read-banner,
.tool-result.error .terminal-banner,
.tool-result.error .search-header,
.tool-result.error .diff-banner { background: var(--error-bg); }

/* diff 卡片：banner（文件 + 增删统计）+ 行级着色 */
.diff-block { margin: 8px 0; background: var(--bg-code); border-radius: var(--sig-radius-code); overflow: hidden; }
.diff-banner { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 9px 14px; background: var(--bg-secondary); }
.diff-files { font-family: var(--font-mono); font-size: 12px; color: var(--text-primary); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.diff-stat { display: flex; gap: 8px; flex: none; font-family: var(--font-mono); font-size: 12px; font-variant-numeric: tabular-nums; }
.diff-add { color: var(--success); }
.diff-del { color: var(--error); }
.diff-body { max-height: 420px; overflow: auto; font-family: var(--font-mono); font-size: 12px; line-height: 20px; }
.diff-line { padding: 0 14px; white-space: pre; color: var(--text-primary); }
.diff-line.add { background: color-mix(in srgb, var(--success) 14%, transparent); }
.diff-line.del { background: color-mix(in srgb, var(--error) 14%, transparent); }
.diff-line.hunk { color: var(--accent); background: var(--bg-secondary); }
.diff-line.file { color: var(--text-tertiary); background: var(--bg-secondary); }
.diff-line.context { color: var(--text-secondary); }
.diff-more { width: 100%; padding: 6px; border: none; border-top: 1px solid var(--border-card); background: var(--bg-secondary); color: var(--text-secondary); font-size: 12px; cursor: pointer; }
.diff-more:hover { color: var(--primary); }
.diff-more:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }

/* read 卡片：12px 圆角 + banner + 48px 行号 gutter + 22px 行高（deepseek ReadBlock） */
.read-block { margin: 8px 0; background: var(--bg-code); border-radius: var(--sig-radius-code); overflow: hidden; }
.read-banner { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 9px 14px; background: var(--bg-secondary); }
.read-path { font-family: var(--font-mono); font-size: 12px; line-height: 18px; color: var(--text-primary); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.read-count { font-size: 12px; color: var(--text-tertiary); flex-shrink: 0; }
.read-body { max-height: 420px; overflow-y: auto; }
.read-line { display: flex; line-height: 22px; font-family: var(--font-mono); font-size: 12px; }
.line-no { flex: 0 0 48px; padding-right: 12px; text-align: right; color: var(--text-tertiary); user-select: none; }
.line-text { flex: 1; padding-right: 14px; white-space: pre; overflow-x: auto; color: var(--text-primary); }

/* 终端卡片（deepseek TerminalBlock 语义） */
.terminal-block { margin: 8px 0; background: var(--terminal-bg); border-radius: var(--sig-radius-code); overflow: hidden; }
.terminal-banner { display: flex; align-items: center; gap: 12px; padding: 9px 14px; background: var(--terminal-header-bg); }
.terminal-label { font-family: var(--font-mono); font-size: 12px; color: var(--terminal-text); }
.terminal-lines { flex: 1; font-size: 11px; color: var(--text-tertiary); font-variant-numeric: tabular-nums; }
.terminal-exit { font-family: var(--font-mono); font-size: 12px; color: var(--success); }
.terminal-exit.nonzero { color: var(--error); }
.terminal-body { margin: 0; padding: 14px; font-family: var(--font-mono); font-size: 12px; line-height: 1.6; color: var(--terminal-text); white-space: pre-wrap; word-break: break-all; max-height: 360px; overflow-y: auto; }

/* 搜索卡片（deepseek SearchBlock：12px 圆角 + banner + 22px 行 + pre 不折行） */
.search-block { margin: 8px 0; background: var(--bg-code); border-radius: var(--sig-radius-code); overflow: hidden; }
.search-header { display: flex; align-items: center; gap: 12px; padding: 9px 14px; background: var(--bg-secondary); }
.search-summary { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; color: var(--text-secondary); }
.search-body { padding: 8px 14px 12px 0; overflow-x: auto; font-family: var(--font-mono); font-size: 12px; }
.search-file { padding: 6px 0 2px 14px; color: var(--primary); font-weight: 600; white-space: pre; }
.search-line { min-height: 22px; padding-left: 14px; white-space: pre; color: var(--text-primary); }
.search-line-no { display: inline-block; width: 40px; color: var(--text-tertiary); user-select: none; }
.search-line-text { color: var(--text-primary); }
/* ── 移动端：结果卡片内边距/字号压缩 ── */
@media (max-width: 768px) {
  .result-code { padding: 10px; font-size: 11px; }
  .result-text { padding: 8px 10px; }
  .read-banner, .terminal-banner { padding: 8px 10px; }
  .read-line { line-height: 20px; font-size: 11px; }
  .diff-body { font-size: 11px; line-height: 18px; }
  .diff-line { padding: 0 10px; }
  .search-body { font-size: 11px; }
}
</style>
