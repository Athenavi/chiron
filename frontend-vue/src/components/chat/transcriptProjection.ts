/**
 * 转录投影：把扁平的 chat item 序列投影为「行序列 + 回合/工具组结构」。
 *
 * 单独成层的理由：窗口化几何（transcriptWindow）、锚点（transcriptAnchor）与单写者
 * （transcriptViewport）的契约都是「行的线性序列 + 稳定 key」。本层保持这一点不变，
 * 只在中间注入折叠头、剔除被折叠的行，三个契约因此都无需改动。
 *
 * flat 模式与 items 一一对应（不注入头行、不隐藏行），用于先把本层接进渲染路径而
 * 不产生任何几何变化；grouped 模式才产生折叠头与隐藏行。
 *
 * 两条硬约束：
 * - 行 key 必须与 `itemKey` 逐字符一致（锚点与窗口都按 key 认身份，改名等于换行）。
 * - 用户消息永不隐藏：折叠只能收起思考/工具/助手正文，不能把提问藏起来。
 */

import type { ChatItem, ToolStatus } from './chat-types'

import { t } from '../../i18n'
export type ToolGroupKind = 'explore' | 'modify' | 'delegate' | 'shell'
export type ProjectionMode = 'flat' | 'grouped'
/** 折叠身份：回合为 `t:<turnId>`，工具组为 `t:<turnId>:g<n>` */
export type FoldKey = string

export interface ProjectedHeader {
  scope: 'turn' | 'tool_group'
  group?: ToolGroupKind
  fold: FoldKey
  title: string
  summary: string
  open: boolean
  status: ToolStatus
}

export interface ProjectedRow {
  /** 与 items 下标派生的 itemKey 一致（折叠头行例外，见 headerKey） */
  key: string
  /** 供 estimateRowSize 选择估计值的行型 */
  kind: string
  /** items 原始下标；折叠头行取该回合首个非用户行的下标 */
  index: number
  item?: ChatItem
  turnKey: string
  groupKey?: string
  header?: ProjectedHeader
  /** 尾部常驻：不参与窗口卸载（按回合判定，折叠不改变它） */
  resident: boolean
}

export interface TurnSlice {
  key: string
  /** 该回合在 items 中的下标区间 [from, to) */
  from: number
  to: number
  resident: boolean
}

export interface ProjectionInput {
  items: readonly ChatItem[]
  folds?: ReadonlyMap<FoldKey, boolean>
  mode?: ProjectionMode
  /** 尾部常驻的回合数（含活跃回合） */
  residentTailTurns?: number
}

export interface ProjectionResult {
  rows: ProjectedRow[]
  keys: string[]
  turns: TurnSlice[]
  /** 本帧生效的折叠态（含投影补出的默认值），供渲染层画 chevron */
  folds: ReadonlyMap<FoldKey, boolean>
}

const EMPTY_FOLDS: ReadonlyMap<FoldKey, boolean> = new Map()
export const DEFAULT_RESIDENT_TAIL_TURNS = 2

/** 无回合身份的行（实时流 / 旧数据）共用的回合 key：这类行不参与按回合常驻 */
export const RUNTIME_TURN_KEY = 't:-'

/** 折叠头行的 key 前缀：不能与 itemKey（`t:...:`）撞车 */
const HEAD_PREFIX = 'h:'

/** 稳定行身份 = 回合身份 + kind + id。同一 assistant 消息拆成 reasoning/text/tool_* 时
 * id 可能相同，故加 kind 前缀；无 turnId（实时流/旧数据）记 `t:-`。 */
export function itemKey(item: ChatItem, index: number): string {
  const scope = turnKeyOf(item)
  const body = item.id ? `${item.kind}:${item.id}` : `${item.kind}:idx${index}`
  return `${scope}:${body}`
}

export function turnKeyOf(item: ChatItem): string {
  return item.turnId ? `t:${item.turnId}` : RUNTIME_TURN_KEY
}

export function headerKey(fold: FoldKey): string {
  return `${HEAD_PREFIX}${fold}`
}

export function isUserText(item: ChatItem | undefined): boolean {
  return !!item && item.kind === 'text' && item.role === 'user'
}

// ── 工具语义分组 ─────────────────────────────────────────────
// 名单取自 python-engine/app/tools 的注册表（registry.register 调用点）。Chiron 的
// ToolDef 没有 readOnly 字段（tools/registry.py），所以未识别工具（MCP / 插件工具、
// 后端新增工具）按保守口径归入 modify，并在组头统计里计入「其它」——绝不把它们算成
// 「编辑 N 处」，否则摘要会说谎。

const EXPLORE_TOOLS = new Set(`read_file read_image grep_files search_files glob_files
web_search web_fetch kb_list kb_search rag_query recall memory_search
git_status git_diff git_log git_branch file_analyzer vision_analyze speech_to_text
graph_templates workflow_status agent_list agent_session_list skill_list skill_discover
mode_list browser_read browser_get_state browser_tab_list`.split(/\s+/))

const MODIFY_TOOLS = new Set(`write_file edit_file git_commit remember forget
skill_install skill_generate mode_edit`.split(/\s+/))

const DELEGATE_TOOLS = new Set(`agent_dispatch code_agent agent_session_create subagent
skill_run graph_create graph_run workflow_run
prd_generate tech_design task_decompose requirement_validate
image_generate media_create text_to_speech`.split(/\s+/))

const SHELL_TOOLS = new Set(`shell_exec execute_python run_code persistent_shell
run_in_background job_output job_kill
browser_navigate browser_click browser_type browser_screenshot browser_scroll
browser_tab_create browser_tab_switch browser_tab_close`.split(/\s+/))

const READ_TOOLS = new Set(`read_file read_image file_analyzer vision_analyze speech_to_text`.split(/\s+/))
const SEARCH_TOOLS = new Set(`grep_files search_files glob_files web_search web_fetch
kb_list kb_search rag_query recall memory_search`.split(/\s+/))
const WRITE_TOOLS = new Set(`write_file git_commit`.split(/\s+/))
const EDIT_TOOLS = new Set(`edit_file skill_install skill_generate mode_edit remember forget`.split(/\s+/))
const JOB_TOOLS = new Set(`run_in_background job_output job_kill`.split(/\s+/))

/**
 * 分组标题：写成**函数**而不是常量表 —— 常量表在模块加载时求值一次，语言切换后
 * 折叠标题会一直停在旧语言（本模块的 `t` 来自非组件入口，本身不具响应式）。
 */
function groupTitle(kind: ToolGroupKind): string {
  switch (kind) {
    case 'explore': return t('读取与检索')
    case 'modify': return t('修改')
    case 'delegate': return t('委派')
    case 'shell': return t('命令与浏览器')
  }
}

export function toolGroupKind(name: string): ToolGroupKind {
  if (SHELL_TOOLS.has(name)) return 'shell'
  if (EXPLORE_TOOLS.has(name)) return 'explore'
  if (MODIFY_TOOLS.has(name)) return 'modify'
  if (DELEGATE_TOOLS.has(name)) return 'delegate'
  return 'modify'
}

interface ToolGroup {
  key: FoldKey
  kind: ToolGroupKind
  /** items 下标区间 [from, to)，保证连续 */
  from: number
  to: number
  names: string[]
}

function countIn(names: readonly string[], set: Set<string>): number {
  let n = 0
  for (const name of names) if (set.has(name)) n++
  return n
}

/** 组头摘要：按工具语义分档计数，未识别的进「其它」 */
export function toolGroupSummary(kind: ToolGroupKind, names: readonly string[]): string {
  const parts: string[] = []
  let labelled = 0
  if (kind === 'explore') {
    const read = countIn(names, READ_TOOLS)
    const search = countIn(names, SEARCH_TOOLS)
    if (read) parts.push(t('读取 {n}', { n: read }))
    if (search) parts.push(t('搜索 {n}', { n: search }))
    labelled = read + search
  } else if (kind === 'modify') {
    const write = countIn(names, WRITE_TOOLS)
    const edit = countIn(names, EDIT_TOOLS)
    if (write) parts.push(t('写入 {n}', { n: write }))
    if (edit) parts.push(t('编辑 {n}', { n: edit }))
    labelled = write + edit
  } else if (kind === 'delegate') {
    const delegated = countIn(names, DELEGATE_TOOLS)
    if (delegated) parts.push(t('委派 {n}', { n: delegated }))
    labelled = delegated
  } else {
    const jobs = countIn(names, JOB_TOOLS)
    const command = countIn(names, SHELL_TOOLS) - jobs
    if (command) parts.push(t('命令 {n}', { n: command }))
    if (jobs) parts.push(t('作业 {n}', { n: jobs }))
    labelled = command + jobs
  }
  const other = names.length - labelled
  if (other > 0) parts.push(t('其它 {n}', { n: other }))
  return parts.join(' · ')
}

function turnSummary(items: readonly ChatItem[], from: number, to: number): string {
  let thinking = 0
  let tools = 0
  let text = 0
  for (let i = from; i < to; i++) {
    const item = items[i]
    if (!item) continue
    if (item.kind === 'reasoning') thinking++
    else if (item.kind === 'tool_call') tools++
    else if (item.kind === 'text' && item.role === 'assistant') text++
  }
  const parts: string[] = []
  if (thinking) parts.push(t('思考 ×{n}', { n: thinking }))
  if (tools) parts.push(t('工具 ×{n}', { n: tools }))
  if (text) parts.push(t('正文'))
  return parts.join(' · ')
}

function sliceTurns(items: readonly ChatItem[]): TurnSlice[] {
  const turns: TurnSlice[] = []
  for (let i = 0; i < items.length; i++) {
    const item = items[i]
    if (!item) continue
    const key = turnKeyOf(item)
    const last = turns[turns.length - 1]
    if (last && last.key === key) last.to = i + 1
    else turns.push({ key, from: i, to: i + 1, resident: false })
  }
  return turns
}

/** 只合并**相邻**的工具行：跨过 reasoning/正文的 tool_result 另开一组，
 * 否则折叠一个组会连带藏掉中间那段思考。 */
function sliceToolGroups(items: readonly ChatItem[], from: number, to: number, turnKey: string): ToolGroup[] {
  const groups: ToolGroup[] = []
  const groupByCallId = new Map<string, ToolGroup>()
  let current: ToolGroup | null = null

  const openGroup = (kind: ToolGroupKind, at: number, name: string): ToolGroup => {
    const group: ToolGroup = { key: `${turnKey}:g${groups.length}`, kind, from: at, to: at + 1, names: [] }
    if (name) group.names.push(name)
    groups.push(group)
    return group
  }

  for (let i = from; i < to; i++) {
    const item = items[i]
    if (!item) continue
    if (item.kind === 'tool_call') {
      const kind = toolGroupKind(item.name)
      if (current && current.kind === kind && current.to === i) {
        current.to = i + 1
        current.names.push(item.name)
      } else {
        current = openGroup(kind, i, item.name)
      }
      groupByCallId.set(item.id, current)
    } else if (item.kind === 'tool_result') {
      if (current && current.to === i) {
        current.to = i + 1
      } else {
        const owner = groupByCallId.get(item.toolCallId)
        if (owner && owner.to === i) owner.to = i + 1
        else current = openGroup('modify', i, '')
      }
    } else {
      current = null
    }
  }
  return groups
}

function residentFlagsOf(items: readonly ChatItem[], turns: readonly TurnSlice[]): boolean[] {
  const flags = new Array<boolean>(items.length).fill(false)
  for (const turn of turns) {
    if (!turn.resident) continue
    for (let i = turn.from; i < turn.to; i++) flags[i] = true
  }
  return flags
}

function resolveOpen(folds: ReadonlyMap<FoldKey, boolean>, key: FoldKey, fallback: boolean): boolean {
  const value = folds.get(key)
  return value === undefined ? fallback : value
}

function hasFoldableSegment(items: readonly ChatItem[], from: number, to: number): boolean {
  for (let i = from; i < to; i++) {
    const kind = items[i]?.kind
    if (kind === 'reasoning' || kind === 'tool_call') return true
  }
  return false
}

function firstNonUserIndex(items: readonly ChatItem[], from: number, to: number): number {
  for (let i = from; i < to; i++) {
    if (!isUserText(items[i])) return i
  }
  return from
}

export function projectTranscript(input: ProjectionInput): ProjectionResult {
  const items = input.items
  const mode = input.mode ?? 'flat'
  const tailTurns = Math.max(1, input.residentTailTurns ?? DEFAULT_RESIDENT_TAIL_TURNS)
  const turns = sliceTurns(items)
  for (let i = 0; i < turns.length; i++) {
    // 无回合身份的行（实时流 / 旧数据）不按回合常驻：它们没有回合边界，若并进
    // "最后一个回合"就会把整个会话算成常驻，窗口化随之失效。这类行由按行兜底覆盖。
    turns[i].resident = turns[i].key !== RUNTIME_TURN_KEY && i >= turns.length - tailTurns
  }
  const resident = residentFlagsOf(items, turns)

  if (mode === 'flat') {
    const rows: ProjectedRow[] = []
    for (let i = 0; i < items.length; i++) {
      const item = items[i]
      if (!item) continue
      rows.push({
        key: itemKey(item, i),
        kind: item.kind,
        index: i,
        item,
        turnKey: turnKeyOf(item),
        resident: resident[i],
      })
    }
    return { rows, keys: rows.map(r => r.key), turns, folds: input.folds ?? EMPTY_FOLDS }
  }

  const folds = new Map<FoldKey, boolean>(input.folds ?? EMPTY_FOLDS)
  const rows: ProjectedRow[] = []

  for (const turn of turns) {
    const active = turn.to === items.length
    const foldable = hasFoldableSegment(items, turn.from, turn.to)
    // 可折叠的回合默认展开；不可折叠的回合不生成头行（折起来也看得见全部内容）
    const turnOpen = !foldable || resolveOpen(folds, turn.key, true)
    const headAt = firstNonUserIndex(items, turn.from, turn.to)
    const groups = sliceToolGroups(items, turn.from, turn.to, turn.key)
    const groupByIndex = new Map<number, ToolGroup>()
    for (const group of groups) {
      for (let i = group.from; i < group.to; i++) groupByIndex.set(i, group)
    }
    const groupOpen = new Map<FoldKey, boolean>()

    for (let i = turn.from; i < turn.to; i++) {
      const item = items[i]
      if (!item) continue

      if (foldable && i === headAt) {
        folds.set(turn.key, turnOpen)
        rows.push({
          key: headerKey(turn.key),
          kind: 'turn_header',
          index: turn.from,
          turnKey: turn.key,
          resident: turn.resident,
          header: {
            scope: 'turn',
            fold: turn.key,
            title: t('回合'),
            summary: turnSummary(items, turn.from, turn.to),
            open: turnOpen,
            status: active ? 'running' : 'done',
          },
        })
      }

      // 用户消息永不隐藏
      if (!isUserText(item) && !turnOpen) continue

      const group = groupByIndex.get(i)
      if (group) {
        if (!groupOpen.has(group.key)) {
          // 完成的回合里工具组默认收起（保留摘要），活跃回合默认展开
          const open = resolveOpen(folds, group.key, active)
          folds.set(group.key, open)
          groupOpen.set(group.key, open)
          rows.push({
            key: headerKey(group.key),
            kind: 'tool_group_header',
            index: group.from,
            turnKey: turn.key,
            groupKey: group.key,
            resident: turn.resident,
            header: {
              scope: 'tool_group',
              group: group.kind,
              fold: group.key,
              title: groupTitle(group.kind),
              summary: toolGroupSummary(group.kind, group.names),
              open,
              status: active ? 'running' : 'done',
            },
          })
        }
        if (groupOpen.get(group.key) === false) continue
      }

      rows.push({
        key: itemKey(item, i),
        kind: item.kind,
        index: i,
        item,
        turnKey: turn.key,
        groupKey: group?.key,
        resident: turn.resident,
      })
    }
  }

  return { rows, keys: rows.map(r => r.key), turns, folds }
}
