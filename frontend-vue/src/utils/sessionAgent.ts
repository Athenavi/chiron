import { t } from '../i18n'

/**
 * 把一次对话沉淀成一个 Agent（人格）。
 *
 * 与 `sessionGraph`（对话 → 工作流）的区别：Agent 的字段是**固定的**
 * （`name` / `description` / `system_prompt`），没有 graph_json 那样的自由结构。
 * 所以这里只把会话正文当人格描述，**名字交给用户确认** —— 凭空造一个叫
 * "对话记录 2026/9/12" 的 Agent 只会积出一堆无意义的条目。
 */

import type { ChatItem, TextItem } from '../components/chat/chat-types'
import { sessionToMarkdown } from './sessionMarkdown'

export interface AgentDraft {
  name: string
  description: string
  system_prompt: string
}

/** Agent 的三类工作台绑定（与 api 的 Agent 接口同形） */
export interface AgentBindingFields {
  kb_id?: string
  skills?: string[]
  plugins?: string[]
}

/** 收敛成去空、去重、保序的字符串列表（容忍单值与非字符串项） */
function stringList(raw: unknown): string[] {
  const out: string[] = []
  const push = (value: unknown) => {
    if (typeof value !== 'string') return
    const trimmed = value.trim()
    if (trimmed && !out.includes(trimmed)) out.push(trimmed)
  }
  if (Array.isArray(raw)) raw.forEach(push)
  else push(raw)
  return out
}

/**
 * 会话里挂着的跨工作台资源 → Agent 的绑定列。
 *
 * 两边字段名不同，映射只做这一次：
 * - 会话上下文（`contextChips.ts` 的 `buildWorkbenchContext`，引擎侧见
 *   `workbench_context.py`）用 `kb_id` / `skill_names` / `plugin_names`；
 * - `agents` 表用 `kb_id` / `skills` / `plugins`。
 *
 * 此前「存为 Agent」只沉淀人格（`system_prompt`），对话里挂的知识库/技能/插件
 * 全部丢掉 —— 用户存完还得回各工作台重新装配一遍。
 *
 * 知识库取首个：`agents.kb_id` 是单值（"默认知识库"），而对话侧允许多选。
 * 多值优先、单值回退，与引擎的 `context_ids` 同口径。
 */
export function agentBindingsFromContext(
  context: Record<string, unknown> | null | undefined,
): AgentBindingFields {
  const out: AgentBindingFields = {}
  if (!context) return out

  const kbIds = stringList(context.kb_ids)
  const singleKb = typeof context.kb_id === 'string' ? context.kb_id.trim() : ''
  const kbId = kbIds[0] || singleKb
  if (kbId) out.kb_id = kbId

  const skills = stringList(context.skill_names)
  if (skills.length) out.skills = skills

  const plugins = stringList(context.plugin_names)
  if (plugins.length) out.plugins = plugins

  return out
}

/** 名字上限与 AgentsView 的输入框一致（`:maxlength="60"`），避免存进去再被截 */
const NAME_MAX = 60

/** 从会话标题推一个默认名字；没有标题时用中性兜底（而不是空字符串） */
function defaultName(title?: string): string {
  const base = (title || '').trim().split('\n')[0]!.trim()
  if (!base) return t('agent.new_agent')
  return base.length > NAME_MAX ? base.slice(0, NAME_MAX) : base
}

/**
 * 会话 → Agent 草稿。正文为空时返回 null，调用方据此禁用入口
 * （与 `sessionToMarkdown` / `sessionToGraph` 同一约定）。
 */
export function sessionToAgent(items: readonly ChatItem[], title?: string): AgentDraft | null {
  // 不能只看 sessionToMarkdown 的返回值：带标题时它即使没有正文也会返回 `# 标题`
  const hasBody = items.some(
    item => item.kind === 'text' && String((item as TextItem).content ?? '').trim().length > 0,
  )
  if (!hasBody) return null

  return {
    name: defaultName(title),
    description: '',
    system_prompt: sessionToMarkdown(items, title).trim(),
  }
}
