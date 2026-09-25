import { t } from '../i18n'

/**
 * 把 Agent 的一次运行结果导出成 Markdown —— 用于「运行结果 → 知识库」的沉淀。
 *
 * 与 `sessionMarkdown.ts`（对话沉淀）分开：那边吃 `ChatItem`，这边吃 `AgentSession`
 * 的 `result` JSON 字符串，两者形状没有交集，合并只会让两边都变模糊。
 */

import type { AgentSession } from '../api'

export interface ParsedAgentResult {
  output?: unknown
  error?: unknown
  duration?: unknown
  tool_calls?: unknown
  token_usage?: unknown
}

/** 可沉淀的最小字段集合：只要求渲染需要的那几个，测试就不用造完整 AgentSession */
export type AgentResultSource = Pick<AgentSession, 'agent_name' | 'task' | 'result'>

/**
 * 宽松解析 `session.result`。
 *
 * 后端把它存成 JSON 字符串，但解析失败时退回把原文当 output —— 一次脏数据
 * 不该让整条运行结果无法展示或沉淀。只有普通对象才当字段容器：数组与标量
 * （后端历史数据里出现过）整体退化为空，调用方按「无输出」处理 —— 这与
 * 改动前 `JSON.parse` 的页面效果一致，但类型不再对调用方撒谎。
 */
export function parseAgentResult(result: string | undefined): ParsedAgentResult {
  if (!result) return {}
  try {
    const parsed: unknown = JSON.parse(result)
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    return parsed as ParsedAgentResult
  } catch {
    return { output: result }
  }
}

function asText(value: unknown): string {
  if (typeof value === 'string') return value.trim()
  if (value === null || value === undefined) return ''
  return String(value).trim()
}

/** 展示与沉淀的取文优先级：error 优先于 output（与页面既有展示一致） */
export function resultText(parsed: ParsedAgentResult): string {
  return asText(parsed.error) || asText(parsed.output)
}

/** 元信息行：只在真有数据时出现，不产出空壳 */
function metaLine(parsed: ParsedAgentResult): string {
  const parts: string[] = []
  if (typeof parsed.duration === 'number' && Number.isFinite(parsed.duration)) {
    parts.push(t('耗时 {n}s', { n: parsed.duration.toFixed(1) }))
  }
  if (Array.isArray(parsed.tool_calls) && parsed.tool_calls.length > 0) {
    parts.push(t('工具调用 {n} 次', { n: parsed.tool_calls.length }))
  }
  return parts.join(' · ')
}

/**
 * 生成可上传的知识库文档。
 *
 * 任务与结果都为空时返回空串，调用方据此禁用上传（与 `sessionToMarkdown` 同一约定）。
 */
export function agentResultToMarkdown(session: AgentResultSource, title?: string): string {
  const parsed = parseAgentResult(session.result)
  const task = asText(session.task)
  const output = resultText(parsed)
  if (!task && !output) return ''

  const heading = asText(title) || t('运行结果 · {name}', { name: asText(session.agent_name) || 'Agent' })
  const blocks: string[] = [`# ${heading}`]
  if (task) blocks.push(t('## 任务'), task)
  if (output) {
    // 只有错误、没有正常输出时单独标注：检索时「错误」比「输出」更有信息量
    const isErrorOnly = !!asText(parsed.error) && !asText(parsed.output)
    blocks.push(isErrorOnly ? t('## 错误') : t('## 输出'), output)
  }
  const meta = metaLine(parsed)
  if (meta) blocks.push('---', meta)
  return `${blocks.join('\n\n')}\n`
}
