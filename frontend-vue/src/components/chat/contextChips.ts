/**
 * 工作台上下文的解析与组装（纯逻辑，无 DOM）。
 *
 * 对话是六大工作台的枢纽：知识库 / Agent / 技能 / 工作流都可以"带进对话"。
 * 这里定两件事：
 * 1. **URL 约定**：同名参数可重复（`?kb=a&kb=b&skill=x`），同名即同一类，可多选；
 *    单值写法（`?kb=a`）保持兼容。
 * 2. **发给引擎的形态**：单值字段取首个（老后端只认它），同时给出 `*_ids` 数组
 *    供支持多值的新后端使用 —— 两边都能工作。
 */

export type ContextChipType = 'kb' | 'agent' | 'skill' | 'workflow' | 'plugin' | 'memory'

export interface ContextChip {
  type: ContextChipType
  label: string
  value: string
}

/** 参数名 → chip 类型与展示名。展示名只是占位，真实名称由调用方按 id 补全。 */
const PARAMS: { param: string; type: ContextChipType; label: (value: string) => string }[] = [
  { param: 'kb', type: 'kb', label: value => `知识库 #${value.slice(0, 8)}` },
  { param: 'agent', type: 'agent', label: value => `Agent #${value.slice(0, 8)}` },
  { param: 'skill', type: 'skill', label: value => `技能 ${value}` },
  { param: 'workflow', type: 'workflow', label: value => `工作流 ${value}` },
  // 插件（MCP server）：带进对话后限定本次只放它提供的工具
  { param: 'plugin', type: 'plugin', label: value => `插件 ${value}` },
  // 长期记忆分类：带进对话后按这些分类注入（服务端口径见
  // python-engine/app/agent/workbench_context.py 的 selected_memory_slots）
  { param: 'memory', type: 'memory', label: value => `记忆 ${value}` },
]

/**
 * 全部上下文参数名。清空 URL 上下文时用它遍历，而不是在调用方硬编码一份列表 ——
 * 硬编码正是"新增 chip 类型后清空时漏掉它"的根源（plugin 就差点漏掉）。
 */
export const CONTEXT_QUERY_KEYS: readonly string[] = PARAMS.map(p => p.param)

/** query 值既可能是字符串（单值），也可能是字符串数组（重复参数） */
function toList(raw: unknown): string[] {
  if (typeof raw === 'string') return raw.trim() ? [raw.trim()] : []
  if (Array.isArray(raw)) {
    return raw
      .filter((item): item is string => typeof item === 'string')
      .map(item => item.trim())
      .filter(Boolean)
  }
  return []
}

/** 从路由 query 解析上下文 chips；同类参数出现多次时全部保留 */
export function parseContextQuery(query: Record<string, unknown> | null | undefined): ContextChip[] {
  if (!query) return []
  const chips: ContextChip[] = []
  for (const { param, type, label } of PARAMS) {
    for (const value of toList(query[param])) chips.push({ type, label: label(value), value })
  }
  return chips
}

function valuesOf(chips: readonly ContextChip[], type: ContextChipType): string[] {
  const out: string[] = []
  for (const chip of chips) {
    if (chip.type === type && chip.value && !out.includes(chip.value)) out.push(chip.value)
  }
  return out
}

/**
 * 组装发给引擎的 context。
 *
 * 单值字段（`kb_id`/`agent_id`/`workflow_id`）取首个：老后端只认这三个标量，
 * 保留它们才不会因为前端升级而失效；对应的 `*_ids` 数组供读多值的后端使用
 * （多值语义见 python-engine/app/agent/workbench_context.py）。
 */
export function buildWorkbenchContext(chips: readonly ContextChip[]): Record<string, unknown> | undefined {
  const ctx: Record<string, unknown> = {}

  const kbIds = valuesOf(chips, 'kb')
  if (kbIds.length) {
    ctx.kb_id = kbIds[0]
    ctx.kb_ids = kbIds
  }

  const agentIds = valuesOf(chips, 'agent')
  if (agentIds.length) {
    ctx.agent_id = agentIds[0]
    ctx.agent_ids = agentIds
  }

  const workflowIds = valuesOf(chips, 'workflow')
  if (workflowIds.length) {
    ctx.workflow_id = workflowIds[0]
    ctx.workflow_ids = workflowIds
  }

  // 技能与插件本来就是多选语义，各只有一个字段：
  // skill_names = 只启用这些技能；plugin_names = 本次对话只放这些插件（MCP server）
  // 提供的工具（内置工具不受影响，见 python-engine/app/agent/runtime.py 的筛选）。
  const skillNames = valuesOf(chips, 'skill')
  if (skillNames.length) ctx.skill_names = skillNames

  const pluginNames = valuesOf(chips, 'plugin')
  if (pluginNames.length) ctx.plugin_names = pluginNames

  // 记忆分类：未指定 = 服务端注入全部（不收窄，不改变既有行为）。
  // memory=all 与未指定等价 —— 服务端只保留「全量」与「按分类」两态。
  const memorySlots = valuesOf(chips, 'memory')
  if (memorySlots.length) ctx.memory_slots = memorySlots

  return Object.keys(ctx).length ? ctx : undefined
}

/**
 * chips → 路由 query，是 `parseContextQuery` 的逆运算：同类多个值序列化成重复参数。
 * 首页与各工作台"带着能力跳到对话"用它与对话侧读 URL 共用同一套约定。
 */
export function buildContextQuery(chips: readonly ContextChip[]): Record<string, string[]> {
  const query: Record<string, string[]> = {}
  for (const { param, type } of PARAMS) {
    const values = valuesOf(chips, type)
    if (values.length) query[param] = values
  }
  return query
}

/**
 * `buildWorkbenchContext()` 的**逆运算**：把工作台上下文还原成 chips。
 *
 * 用途（P1，docs/session-runtime-spec.md）：会话运行时状态 `runtime.context` 是
 * "已激活能力"的**单一事实源**。切换会话/刷新页面时据此还原侧栏展示，
 * 而不是只依赖 URL query —— 否则用户"带进对话"的能力在刷新后就从界面上消失了。
 */
export function chipsFromWorkbenchContext(
  ctx: Record<string, unknown> | null | undefined,
): ContextChip[] {
  if (!ctx) return []
  const LABELS: Record<ContextChipType, (value: string) => string> = {
    kb: value => `知识库 #${value.slice(0, 8)}`,
    agent: value => `Agent #${value.slice(0, 8)}`,
    skill: value => `技能 ${value}`,
    workflow: value => `工作流 ${value}`,
    plugin: value => `插件 ${value}`,
    memory: value => `记忆 ${value}`,
  }
  const chips: ContextChip[] = []
  const push = (type: ContextChipType, raw: unknown) => {
    if (!Array.isArray(raw)) return
    for (const item of raw) {
      if (typeof item === 'string' && item.trim()) {
        chips.push({ type, label: LABELS[type](item.trim()), value: item.trim() })
      }
    }
  }
  // 多值字段优先，缺省回退单值兼容字段（与 buildWorkbenchContext 的写法对应）
  push('kb', ctx.kb_ids ?? (ctx.kb_id ? [ctx.kb_id] : []))
  push('agent', ctx.agent_ids ?? (ctx.agent_id ? [ctx.agent_id] : []))
  push('skill', ctx.skill_names)
  push('workflow', ctx.workflow_ids ?? (ctx.workflow_id ? [ctx.workflow_id] : []))
  push('plugin', ctx.plugin_names)
  push('memory', ctx.memory_slots)
  return chips
}
