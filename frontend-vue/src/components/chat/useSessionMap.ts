/**
 * 会话地图（Session Map）—— 地图层状态。P0。
 *
 * ## 关键语义：地图是**独立的记忆层**，不是会话列表的另一种排版
 *
 * 见 docs/session-map-plan.md §九。两层的关系是"引用"，不是"同一份数据"：
 *
 * ```
 * 会话列表（真实会话：有消息、有运行态）     地图层（节点：位置、名字、分组）
 *         │  导入 = 建立引用                ← 独立生命周期
 *         └───────────────────────────────►
 * ```
 *
 * 四条推论（这个模块逐条落实）：
 * 1. `MapNode` 有**自己的 id** 和**自己的 title**（可覆盖会话标题）；
 * 2. `sessionId` **可空** ⇒ 支持纯便签节点；
 * 3. `removeNode` **只删节点，绝不删会话**；
 * 4. `groupNodes` 只做地图内的分组，不动会话本身。
 *
 * ## 铁律（见同文档 §〇）
 *
 * **位置一旦定下，系统永不自动改。** 空间记忆生效的前提是位置稳定 —— 位置一漂移，
 * 用户脑中那张图就作废，而且比列表更糟（用户会信任它）。所以：
 * - `importSessions` **只为新节点找空位，绝不移动任何既有节点**；
 * - `moveNode` 是唯一的"移动"入口，且只由用户拖动触发。
 *
 * 有测试专门守着这条：`sessionMap.test.ts` 的"导入不得改变任何既有节点坐标"。
 *
 * ## 存储
 *
 * 目前落 `localStorage`，但**收敛在 `readState` / `writeState`** ——
 * 将来换服务端（跨设备一致）只改这两个函数。
 */

import { computed, ref } from 'vue'

/** 网格步长：吸附到它，让位置"可复述"（"左上角第三行"这种描述才成立） */
export const GRID = 260

export const MIN_SCALE = 0.15
export const MAX_SCALE = 2.5

/** 一个地图节点：**独立实体** */
export interface MapNode {
  /** 节点自己的 id（不是 session_id —— 同一会话可以在地图上被引用多次） */
  id: string
  /** 引用的会话；**可空 ⇒ 便签节点**（画布上直接写的想法） */
  sessionId?: string
  x: number
  y: number
  /** 节点自己的名字，优先于会话标题 */
  title?: string
  /** 隐藏 ≠ 删除：画布上不显示，数据保留，随时恢复 */
  hidden?: boolean
  /** 所属组（组合/拼接的产物） */
  groupId?: string
  /** 用户手动放置过：此后连"引力落位"也不再生效 */
  pinned?: boolean
}

/** 组：组合/拼接的产物 */
export interface MapGroup {
  id: string
  name: string
  collapsed?: boolean
}

export interface Camera {
  scale: number
  tx: number
  ty: number
}

interface MapState {
  nodes: MapNode[]
  groups: MapGroup[]
  camera: Camera
}

export interface ImportResult {
  /** 新建的节点 id（UI 可据此高亮或飞过去） */
  created: string[]
  /** 因为已在地图里而跳过的会话 id */
  skipped: string[]
}

const STORAGE_KEY = 'chiron.sessionMap.v2'

const DEFAULT_CAMERA: Camera = { scale: 1, tx: 0, ty: 0 }

/** 吸附到网格（用 round 而非 floor，避免整体偏移半格） */
export function snap(value: number): number {
  return Math.round(value / GRID) * GRID
}

export function clampScale(scale: number): number {
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale))
}

let seq = 0
/** 节点 id：时间戳 + 递增，避免同一毫秒内多次导入撞号 */
export function newNodeId(): string {
  seq += 1
  return `n_${Date.now().toString(36)}_${seq.toString(36)}`
}

/* ── 存储层：只有这两个函数知道数据落哪（换服务端时只改这里） ── */

function readState(): MapState {
  const empty = (): MapState => ({ nodes: [], groups: [], camera: { ...DEFAULT_CAMERA } })
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return empty()
    const parsed = JSON.parse(raw) as Partial<MapState>
    return {
      nodes: Array.isArray(parsed?.nodes) ? (parsed.nodes as MapNode[]) : [],
      groups: Array.isArray(parsed?.groups) ? (parsed.groups as MapGroup[]) : [],
      camera: { ...DEFAULT_CAMERA, ...(parsed?.camera || {}) },
    }
  } catch {
    // 隐私模式 / 数据损坏：退回空地图，绝不抛错打断会话列表
    return empty()
  }
}

function writeState(state: MapState): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
  } catch {
    /* 隐私模式等：静默降级 */
  }
}

/* ── 首次落位 ── */

/**
 * 找一个**离原点最近且未被占用**的空格位（方形螺旋由内向外扫）。
 *
 * 螺旋保证新节点总是落在最近的空位上 ⇒ "新节点会出现在哪儿"是**可预测**的，
 * 而不是被随机丢到远处。
 *
 * **只读 `occupied`，绝不写入或移动任何既有节点。**
 */
export function nextFreeSlot(occupied: Set<string>): { x: number; y: number } {
  if (!occupied.has('0,0')) return { x: 0, y: 0 }
  for (let radius = 1; radius < 200; radius++) {
    for (let i = -radius; i <= radius; i++) {
      const top = `${i},${-radius}`
      if (!occupied.has(top)) return { x: i * GRID, y: -radius * GRID }
      const bottom = `${i},${radius}`
      if (!occupied.has(bottom)) return { x: i * GRID, y: radius * GRID }
    }
    for (let j = -radius + 1; j <= radius - 1; j++) {
      const left = `${-radius},${j}`
      if (!occupied.has(left)) return { x: -radius * GRID, y: j * GRID }
      const right = `${radius},${j}`
      if (!occupied.has(right)) return { x: radius * GRID, y: j * GRID }
    }
  }
  return { x: 0, y: 0 }
}

/** 已占用的格位集合（用于找空位）—— 只读 */
export function occupiedSlots(nodes: MapNode[]): Set<string> {
  const set = new Set<string>()
  for (const n of nodes) set.add(`${Math.round(n.x / GRID)},${Math.round(n.y / GRID)}`)
  return set
}

/* ── 面向组件的状态 ── */

export function useSessionMap() {
  const initial = readState()
  /** 节点列表：**响应式**（退化成普通数组就会出现"拖动不跟手"） */
  const nodes = ref<MapNode[]>(initial.nodes)
  const groups = ref<MapGroup[]>(initial.groups)
  const camera = ref<Camera>(initial.camera)

  /* ── 撤销 / 重做 ──
   *
   * 用**快照式**而不是命令式：地图状态很小（几十个节点），快照简单得多，也不会漏掉某条路径。
   *
   * 关键判断：**相机（平移/缩放）不进历史**。
   * 否则每拖一次画布、每滚一次滚轮都会压栈，撤销键按下去变成"撤销视角" —— 非常烦人。
   * 所以这里用"**内容指纹**"判定：只有 nodes/groups 真的变了才记一步。
   * 好处是所有既有 mutation（import/addNote/removeNode/moveNode/...）**一行都不用改**。
   *
   * 另外：拖动只在 `pointerup` 落盘一次 ⇒ 一次拖动天然就是一步。
   */
  const HISTORY_MAX = 50
  const past = ref<string[]>([])
  const future = ref<string[]>([])
  const canUndo = computed(() => past.value.length > 0)
  const canRedo = computed(() => future.value.length > 0)

  function contentFingerprint(): string {
    return JSON.stringify({ nodes: nodes.value, groups: groups.value })
  }

  let lastContent = contentFingerprint()

  function writeCurrent(): void {
    writeState({ nodes: nodes.value, groups: groups.value, camera: camera.value })
  }

  function persist(): void {
    const next = contentFingerprint()
    if (lastContent && next !== lastContent) {
      past.value = [...past.value, lastContent].slice(-HISTORY_MAX)
      future.value = [] // 新操作让"重做"失效
    }
    lastContent = next
    writeCurrent()
  }

  function restore(fingerprint: string): void {
    const state = JSON.parse(fingerprint) as { nodes: MapNode[]; groups: MapGroup[] }
    nodes.value = state.nodes
    groups.value = state.groups
    lastContent = fingerprint
    writeCurrent()
  }

  /** 撤销：返回是否真的退了（供 UI 决定要不要提示） */
  function undo(): boolean {
    const prev = past.value[past.value.length - 1]
    if (prev === undefined) return false
    future.value = [...future.value, contentFingerprint()]
    past.value = past.value.slice(0, -1)
    restore(prev)
    return true
  }

  function redo(): boolean {
    const next = future.value[future.value.length - 1]
    if (next === undefined) return false
    past.value = [...past.value, contentFingerprint()].slice(-HISTORY_MAX)
    future.value = future.value.slice(0, -1)
    restore(next)
    return true
  }

  /**
   * **从会话列表导入**：为每个会话建一个地图节点。
   *
   * - 已经在地图里的会话**跳过**（不重复建节点，也不移动既有节点）；
   * - 新节点按螺旋找最近空位 —— **绝不推开任何既有节点**（铁律）。
   */
  function importSessions(
    entries: { sessionId: string; title?: string }[],
    opts?: { skipExisting?: boolean },
  ): ImportResult {
    const skipExisting = opts?.skipExisting !== false
    const occupied = occupiedSlots(nodes.value)
    const present = new Set(
      nodes.value.map(n => n.sessionId).filter((v): v is string => !!v),
    )
    const created: string[] = []
    const skipped: string[] = []

    for (const entry of entries) {
      if (skipExisting && present.has(entry.sessionId)) {
        skipped.push(entry.sessionId)
        continue
      }
      const slot = nextFreeSlot(occupied)
      occupied.add(`${Math.round(slot.x / GRID)},${Math.round(slot.y / GRID)}`)
      const node: MapNode = {
        id: newNodeId(),
        sessionId: entry.sessionId,
        title: entry.title,
        x: slot.x,
        y: slot.y,
      }
      nodes.value.push(node)
      present.add(entry.sessionId)
      created.push(node.id)
    }
    if (created.length) persist()
    return { created, skipped }
  }

  /** 双击空白新建**便签节点**（无 sessionId）—— 独立记忆层最直接的体现 */
  function addNote(x: number, y: number, title = ''): string {
    const node: MapNode = { id: newNodeId(), x: snap(x), y: snap(y), title, pinned: true }
    nodes.value.push(node)
    persist()
    return node.id
  }

  /**
   * **只从地图移除节点 —— 绝不删除会话。**
   * （UI 文案必须写清楚，否则这是最容易引发数据恐慌的操作。）
   */
  function removeNode(id: string): void {
    const i = nodes.value.findIndex(n => n.id === id)
    if (i < 0) return
    nodes.value.splice(i, 1)
    persist()
  }

  /** 隐藏/恢复：隐藏 ≠ 删除 */
  function setHidden(id: string, hidden: boolean): void {
    const n = nodes.value.find(x => x.id === id)
    if (!n) return
    n.hidden = hidden
    persist()
  }

  /** 用户拖动落点：吸附网格并标记 pinned（此后引力也不再生效） */
  function moveNode(id: string, x: number, y: number): void {
    const n = nodes.value.find(v => v.id === id)
    if (!n) return
    n.x = snap(x)
    n.y = snap(y)
    n.pinned = true
    persist()
  }

  function renameNode(id: string, title: string): void {
    const n = nodes.value.find(v => v.id === id)
    if (!n) return
    n.title = title
    persist()
  }

  /** 组合/拼接：把这些节点归入一个组（**不改动会话本身**） */
  function groupNodes(ids: string[], name = ''): string {
    const gid = `g_${Date.now().toString(36)}`
    groups.value.push({ id: gid, name })
    for (const n of nodes.value) {
      if (ids.includes(n.id)) n.groupId = gid
    }
    persist()
    return gid
  }

  function ungroup(groupId: string): void {
    for (const n of nodes.value) {
      if (n.groupId === groupId) delete n.groupId
    }
    groups.value = groups.value.filter(g => g.id !== groupId)
    persist()
  }

  /** 相机变化（缩放/平移）—— 只写盘，不触发落位 */
  function setCamera(next: Partial<Camera>): void {
    camera.value = {
      scale: next.scale !== undefined ? clampScale(next.scale) : camera.value.scale,
      tx: next.tx !== undefined ? next.tx : camera.value.tx,
      ty: next.ty !== undefined ? next.ty : camera.value.ty,
    }
    persist()
  }

  /** 仅供测试/调试：清空地图（**同样不碰会话**） */
  function reset(): void {
    nodes.value = []
    groups.value = []
    camera.value = { ...DEFAULT_CAMERA }
    persist()
  }

  return {
    nodes,
    groups,
    camera,
    persist,
    importSessions,
    addNote,
    removeNode,
    setHidden,
    moveNode,
    renameNode,
    groupNodes,
    ungroup,
    setCamera,
    reset,
    undo,
    redo,
    canUndo,
    canRedo,
  }
}
