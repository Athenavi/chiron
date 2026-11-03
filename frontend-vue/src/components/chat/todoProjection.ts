/**
 * Todo 清单的**投影层**（纯函数，无 Vue 依赖）。
 *
 * 三条设计约束，都来自"面板不能污染会话缓存"这个前提：
 *
 * 1. **面板状态不进 `items`**。`items` 是会话运行态（`runtimes[].items`），它会被
 *    切会话、重放历史、`/clear` 反复重建。把清单写进去，等于让一个全局性的任务板
 *    跟着某一个会话的 UI 状态漂移 —— 用户切回旧会话时，会看到上一轮任务留下的面板，
 *    而模型那边早已没有那份清单。所以清单单独按 session 存放（见 `useTodoPanel`）。
 *
 * 2. **界面从事件投影，不从 tool_result 正文解析**。引擎已经发 `todo_updated`
 *    事件（结构化 JSON）。去 `tool_result.content` 里正则抠 JSON 会在工具输出被
 *    截断（`outputTruncated`）时静默失效，而那时面板会停在上一版、看起来"卡住了"。
 *
 * 3. **revision 单调**。SSE 事件可能乱序到达（重连补发、多副本）。没有版本号时
 *    旧事件覆盖新事件，表现是面板"倒退"一格，然后永远差一格。
 */

/** 清单条目的状态枚举（与后端 `VALID_STATUSES` 一致）。 */
export type TodoStatus = 'pending' | 'in_progress' | 'completed'

/** 引擎 `todo_updated` 事件里的单条。字段与后端 `plan.to_todos` 投影对齐。 */
export interface TodoEntry {
  id?: string
  content?: string
  status?: string
  /** 认领者（host 赋值：`main` 或子 agent 的 run id） */
  owner?: string
  /** 前序步骤 id，全部 completed 才可开工 */
  depends_on?: string[]
  files?: string[]
  verified?: string[]
}

/** 投影后的一条：状态已归一化，并算出"是否被前序阻塞"。 */
export interface TodoRow {
  /** 面板按编号渲染与更新的唯一键（host 赋予，形如 t1/t2） */
  id: string
  /** 展示编号：从 id 里取数字，取不到则用序号 */
  index: number
  content: string
  status: TodoStatus
  owner: string
  dependsOn: string[]
  /**
   * 被前序阻塞：还有依赖项未完成，且自己尚未完成。
   *
   * 已完成的不算阻塞 —— 否则一项做完之后仍挂着"等待前序"的标签，看起来像没收尾。
   */
  blocked: boolean
  /** 阻塞它的那些 id（面板可直接列出"还在等 t2,t3"） */
  blockedBy: string[]
  fileCount: number
  verifiedCount: number
}

export interface TodoSummary {
  total: number
  pending: number
  inProgress: number
  completed: number
  /** 0–100，用于折叠标题上的进度条宽度 */
  percent: number
}

export interface TodoBoard {
  rows: TodoRow[]
  summary: TodoSummary
  revision: number
  /** 认领者清单（多 agent 时用来分组/标注；单 agent 时通常只有 `main`） */
  owners: string[]
}

/** 状态归一化：只认三种规范值，其余一律当作 `pending`（不抛错，模板必须总能渲染）。 */
export function normalizeStatus(value: unknown): TodoStatus {
  const text = String(value ?? '').trim()
  if (text === 'in_progress' || text === 'completed') return text
  return 'pending'
}

/**
 * 把 `id` 里的序号取出来当展示编号。
 *
 * host 赋予的 id 形如 `t1`、`t2`。取不到数字时回落到列表下标 + 1 —— 关键是
 * **编号必须稳定且唯一**，因为用户和模型都靠它指代"第几项"。
 */
function indexOf(id: string, fallback: number): number {
  const match = /(\d+)\s*$/.exec(id)
  if (match?.[1]) {
    const n = Number.parseInt(match[1], 10)
    if (Number.isFinite(n) && n > 0) return n
  }
  return fallback + 1
}

/**
 * 把事件里的原始条目投影成面板可渲染的板子。
 *
 * 依赖判定需要**全量条目先就位**（`depends_on` 指向的项可能排在后面），所以分两趟：
 * 先建 id → status 索引，再逐条算 blocked。
 */
export function projectTodoBoard(raw: unknown, revision = 0): TodoBoard {
  const entries: TodoEntry[] = Array.isArray(raw)
    ? raw.filter((item): item is TodoEntry => !!item && typeof item === 'object')
    : []

  const statusById = new Map<string, TodoStatus>()
  entries.forEach((entry, i) => {
    const id = String(entry.id ?? `t${i + 1}`)
    statusById.set(id, normalizeStatus(entry.status))
  })

  const rows: TodoRow[] = entries.map((entry, i) => {
    const id = String(entry.id ?? `t${i + 1}`)
    const status = normalizeStatus(entry.status)
    const dependsOn = Array.isArray(entry.depends_on)
      ? entry.depends_on.map((d) => String(d)).filter(Boolean)
      : []
    // 只有"还没完成"的项才谈得上被阻塞：已完成项挂着等待标签会误导。
    const blockedBy =
      status === 'completed'
        ? []
        : dependsOn.filter((dep) => statusById.get(dep) !== 'completed')
    return {
      id,
      index: indexOf(id, i),
      content: String(entry.content ?? '').trim(),
      status,
      owner: String(entry.owner ?? '').trim(),
      dependsOn,
      blocked: blockedBy.length > 0,
      blockedBy,
      fileCount: Array.isArray(entry.files) ? entry.files.length : 0,
      verifiedCount: Array.isArray(entry.verified) ? entry.verified.length : 0,
    }
  })

  const summary = summarize(rows)
  const owners = [...new Set(rows.map((r) => r.owner).filter(Boolean))]
  return { rows, summary, revision, owners }
}

export function summarize(rows: readonly TodoRow[]): TodoSummary {
  const total = rows.length
  let pending = 0
  let inProgress = 0
  let completed = 0
  for (const row of rows) {
    if (row.status === 'completed') completed += 1
    else if (row.status === 'in_progress') inProgress += 1
    else pending += 1
  }
  // 分母为 0 时返回 0 而不是 NaN —— 空板不该让进度条宽度变成 `NaN%`。
  const percent = total === 0 ? 0 : Math.round((completed / total) * 100)
  return { total, pending, inProgress, completed, percent }
}

/**
 * 合并一次到期的更新，返回**是否应该采用**。
 *
 * 乱序防护：`revision` 不大于当前值的旧事件一律丢弃。缺 `revision` 的历史事件
 * （旧引擎/旧数据）按"总是采用"处理 —— 否则升级后老会话的面板永远空白。
 */
export function shouldApply(
  incoming: { revision?: unknown },
  currentRevision: number,
): boolean {
  const rev = Number(incoming.revision)
  if (!Number.isFinite(rev)) return true
  return rev > currentRevision
}

/** 空板（新会话/新回合的初始值）。 */
export const EMPTY_BOARD: TodoBoard = {
  rows: [],
  summary: { total: 0, pending: 0, inProgress: 0, completed: 0, percent: 0 },
  revision: 0,
  owners: [],
}

/** `todo_write` 的工具名（与后端 `WRITE_TODOS_TOOL` 一致；仅用于历史投影） */
export const TODO_WRITE_TOOL = 'todo_write'

/** 历史工具调用里够用的字段（来自 `HistoryToolCall`，只取投影需要的部分）。 */
export interface HistoricalToolCall {
  tool_name?: string
  output?: string
  created_at?: string
}

/**
 * 从**历史工具调用**里恢复最后一次清单。
 *
 * 为什么需要它：`todo_updated` 是**实时事件**，刷新页面后不会重放。没有这条路，
 * 用户刷新一下就看不到自己在跑的任务清单了 —— 而长任务恰恰是最需要"刷新后还在"
 * 的场景。模型那边不受影响（它从 tool 消息里读到同一份清单），所以这里只影响界面。
 *
 * 取**最后一条**可解析的 `todo_write`：全量覆写语义下，最新的那份就是当前真相。
 * 解析失败一律跳过而不是抛错 —— 历史里存着被截断的 output（`output_truncated`）是常态，
 * 一条坏记录不该让整个面板空掉。
 */
export function boardFromHistory(
  toolCalls: readonly HistoricalToolCall[] | null | undefined,
): TodoBoard {
  if (!Array.isArray(toolCalls) || toolCalls.length === 0) return EMPTY_BOARD

  const writes = toolCalls.filter((call) => call?.tool_name === TODO_WRITE_TOOL)
  for (let i = writes.length - 1; i >= 0; i -= 1) {
    const raw = writes[i]?.output
    if (typeof raw !== 'string' || !raw.trim()) continue
    let parsed: unknown
    try {
      parsed = JSON.parse(raw)
    } catch {
      // 预览被截断的 JSON 走这里：跳过，继续找更早的那条完整记录。
      continue
    }
    const todos = (parsed as { todos?: unknown })?.todos
    if (!Array.isArray(todos)) continue
    return projectTodoBoard(todos, 0)
  }
  return EMPTY_BOARD
}
