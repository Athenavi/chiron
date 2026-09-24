/**
 * **单条**工具调用的摘要（从 JSON 参数里摘出一句话）。
 *
 * ## 分工（重要，避免重复实现）
 *
 * - **组的划分与组摘要**属于投影层：`transcriptProjection.ts` 的
 *   `toolGroupKind()` / `toolGroupSummary()`，且**已经默认启用**
 *   （`MessageList` 传 `mode: 'grouped'`）。别再在这里重做一套家族表。
 * - 本文件只负责**单条**：折叠态的 `ToolCallCard` 该显示哪个字段。
 *
 * ## 为什么不能"取前两个字段"
 *
 * `read_file {path, offset, limit}` 会摘成 `src/a.ts · 0`（第二个是无意义的 offset）；
 * `shell_exec {command, timeout}` 会摘成 `npm test · 30000`；
 * `run_code {code, language}` 更是**把整段代码塞进摘要**。
 *
 * 所以按工具名给一份**显式字段表** —— **绝不用正则猜类型**（ZCode 的踩坑记录：
 * "继续用正则扫 kind/title 的话，会把 `TodoWrite` 里的 `Write` 当成文件写入"）。
 * 未登记的工具走通用兜底（前两个短字段），**与既有行为完全一致 ⇒ 零退化**。
 */

import { t } from '../i18n'

/** 工具名 → 摘要优先取的参数字段（按顺序找第一个非空） */
const SUMMARY_FIELDS: Record<string, string[]> = {
  // 文件
  read_file: ['path', 'file_path', 'file'],
  list_dir: ['path', 'dir', 'directory'],
  edit_file: ['path', 'file_path', 'file'],
  write_file: ['path', 'file_path', 'file'],
  // 搜索与网页
  grep_files: ['pattern', 'query', 'regex'],
  web_search: ['query', 'q'],
  web_fetch: ['url'],
  // 执行
  shell_exec: ['command', 'cmd'],
  git_status: ['path', 'cwd'],
  run_code: ['language'],
  execute_python: [],
  // 子 Agent
  subagent: ['task', 'prompt', 'expert'],
  read_subagent_result: ['run_id'],
  // 记忆
  recall: ['query', 'q'],
  remember: ['content', 'text', 'fact'],
  // 技能
  skill_run: ['name', 'skill'],
  skill_install: ['name', 'source'],
}

/**
 * 参数里装着"正文/代码"的工具：摘要只报**规模**。
 * 否则摘要会变成一大段代码或正文 —— 那正是"信息密度低"的来源。
 */
const BULK_FIELDS: Record<string, { field: string; unit: string }> = {
  run_code: { field: 'code', unit: t('chat.toolUnit.codeLines') },
  execute_python: { field: 'code', unit: t('chat.toolUnit.codeLines') },
  write_file: { field: 'content', unit: t('chat.toolUnit.lines') },
  edit_file: { field: 'new_string', unit: t('chat.toolUnit.lines') },
}

function clip(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text
}

export function toolSummary(toolName: string, rawArgs: string, maxLen = 60): string {
  const fields = SUMMARY_FIELDS[toolName]
  const bulk = BULK_FIELDS[toolName]

  let parsed: Record<string, unknown> | null = null
  try {
    const value = JSON.parse(rawArgs || '{}')
    parsed = value && typeof value === 'object' ? (value as Record<string, unknown>) : null
  } catch {
    parsed = null
  }

  if (parsed) {
    for (const key of fields ?? []) {
      const value = parsed[key]
      if (typeof value === 'string' && value.trim()) return clip(value.trim(), maxLen)
      if (typeof value === 'number') return String(value)
    }
    if (bulk) {
      const text = typeof parsed[bulk.field] === 'string' ? (parsed[bulk.field] as string) : ''
      if (text) return `${text.split('\n').length} ${bulk.unit}`
    }
    // 通用兜底：前两个短字段（与既有行为一致，未登记工具不退化）
    const keys = Object.keys(parsed)
    if (keys.length) {
      return keys
        .slice(0, 2)
        .map((k) => {
          const v = parsed[k]
          return clip(typeof v === 'string' ? v : JSON.stringify(v), 40)
        })
        .join(' · ')
    }
  }

  return clip(rawArgs || '', maxLen)
}
