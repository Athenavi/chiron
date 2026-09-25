/**
 * 可用工具列表的解析与合并。
 *
 * 后端 `GET /v1/tools` 返回 `{ tools: [{ name, description }] }`（Go 网关把 python 的
 * `function.name/description` 扁平化过）。这里做两件事：
 * 1. **宽松解析**：不同层级的容器都认，拿不到 name 的条目丢弃 —— 工具列表里有 MCP 插件
 *    注入的代理工具，来源杂，个别脏数据不该让整个选择器空掉；
 * 2. **合并手写与勾选**：Agent 的 `tools_text` 一直是手写的，这里保留手写能力，
 *    选择器只做补充，不强迫用户改用选择器。
 */

export interface ToolInfo {
  name: string
  description: string
  /** 完整 JSON schema（来自 /v1/tools：Go 会把 python 的 function.parameters 带出来） */
  parameters?: unknown
  /** 来源：'mcp' = MCP/插件注入；缺失或其它值一律视为内置工具 */
  source?: string
}

/** 工具的书写形态不止一种：纯名字符串，或 {name}，或 OpenAI 的 {function:{name}} */
function normalizeTool(raw: unknown): ToolInfo | null {
  if (typeof raw === 'string') {
    const name = raw.trim()
    return name ? { name, description: '' } : null
  }
  const record = raw as Record<string, unknown> | null
  const fn = record?.function as Record<string, unknown> | undefined
  const rawName = typeof record?.name === 'string' ? record.name : typeof fn?.name === 'string' ? fn.name : ''
  const name = rawName.trim()
  if (!name) return null
  const description = typeof record?.description === 'string'
    ? record.description
    : typeof fn?.description === 'string' ? fn.description : ''
  // parameters 内层优先（OpenAI 形态），缺失时退回外层字段
  const parameters = fn?.parameters ?? record?.parameters
  // source 被 Go 网关扁平化到外层；同时兼容仍藏在 function 里的形态
  const rawSource = typeof record?.source === 'string'
    ? record.source
    : typeof fn?.source === 'string' ? fn.source : ''
  const source = rawSource.trim()
  const tool: ToolInfo = { name, description }
  if (parameters !== undefined) tool.parameters = parameters
  if (source) tool.source = source
  return tool
}

/** 宽松解析工具列表：接受数组本身，或 {tools: []} / {data: []} / {data: {tools: []}} */
export function toToolList(raw: unknown): ToolInfo[] {
  // 四种形状用一组小助手收敛，避免在 `unknown` 上做链式访问（那正是原来用 `any` 的原因）。
  const asArray = (v: unknown): unknown[] | null => (Array.isArray(v) ? v : null)
  const asObject = (v: unknown): Record<string, unknown> | null =>
    v !== null && typeof v === 'object' ? (v as Record<string, unknown>) : null

  const container = asObject(raw)
  const data = asObject(container?.data)
  const list = asArray(raw)
    ?? asArray(container?.tools)
    ?? asArray(data?.tools)
    ?? asArray(container?.data)
    ?? []

  const out: ToolInfo[] = []
  for (const item of list) {
    const tool = normalizeTool(item)
    if (tool && !out.some(existing => existing.name === tool.name)) out.push(tool)
  }
  return out
}

/** 名字分隔符：换行、逗号（中英）、顿号、空白都算 */
const NAME_SEPARATORS = /[\n,，、\s]+/

/** 解析手写文本里的工具名（用于把既有配置回填成勾选态） */
export function parseToolNames(text: string): string[] {
  return (text || '')
    .split(NAME_SEPARATORS)
    .map(name => name.trim())
    .filter(Boolean)
}

/** 合并手写文本与勾选的工具名：去重，手写在前（保持用户原有顺序） */
export function mergeToolNames(handwritten: string, picked: readonly string[]): string {
  const seen = new Set<string>()
  const names: string[] = []
  for (const name of [...parseToolNames(handwritten), ...picked]) {
    const trimmed = (name || '').trim()
    if (!trimmed || seen.has(trimmed)) continue
    seen.add(trimmed)
    names.push(trimmed)
  }
  return names.join('\n')
}
