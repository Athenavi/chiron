/**
 * 地图投影：`session_map_nodes` + 会话元数据 → 画布上的卡片与连线。
 *
 * 这一层是纯函数，不碰网络、不碰 DOM、不读 localStorage —— 它是整个重构里唯一
 * 能逐条对照旧实现的部件，也是唯一值得单独写单测的部件。
 *
 * ## 与旧实现（`public/sessionmap/app.js::conversationCards`）的关键差异
 *
 * 旧地图的**一张卡片是一个回合**（`turn`），因为它复刻的 dsh-synapse 后端没有布局存储，
 * 位置、车道、父子关系全都得前端从消息流里现推（`layoutConversationGraph`）。
 * Chiron 的数据面反过来：`session_map_nodes` 已经存了 `session_id` / `parent_node_id` /
 * `x,y`（见 `internal/api/sessionmap.go`），**节点就是会话级引用**。
 *
 * 所以这里不做消息推导：卡片 = 会话，回合细节交给详情页（`ThreadDetail.vue`）。
 * 好处是投影变成确定性映射，节点数上限 2000（`smMaxNodes`）时仍是线性复杂度。
 */
import type { SessionMapNode } from './types'

/** 画布卡片要展示的会话元数据（来自 `/v1/conversations` 的元素）。 */
export interface MapSessionMeta {
  id: string
  title?: string
  alias?: string
  tag?: string
  pinned?: boolean
  parent_session_id?: string
  parent_title?: string
  branch_from_seq?: number
}

/** 一张卡片 = 一个会话在地图上的表示。 */
export interface MapCard {
  /** Vue Flow 的节点 id —— 用地图节点 id，而不是会话 id：同一会话可以出现在多张画布上 */
  id: string
  sessionId: string
  /** 展示名：别名优先，其次节点标题，最后会话标题 */
  title: string
  alias: string
  tag: string
  color: string
  /** 分叉点（保留到源会话第几条消息）；0 表示不是分叉 */
  branchFromSeq: number
  /** 父会话展示名；父会话已删则回落到后端给的 `parent_title` */
  parentTitle: string
  /** 直接子节点数（决定是否显示折叠按钮） */
  childCount: number
  /** 全部后代数（折叠时用来提示「还有 N 个」） */
  descendantCount: number
  /** 子树已折叠（自己仍可见） */
  collapsed: boolean
  /** 该会话有一轮正在跑（脉冲动画） */
  running: boolean
  /** 没有 `session_id` 的占位卡：还没绑定会话，不能发消息 */
  hasSession: boolean
  x: number
  y: number
  /** 后端持久化的问题标记，用于置顶排序 */
  pinned: boolean
}

/** 一条连线。`kind` 直接透传后端 `edge_kind`（`'branch'` / `'manual'`）。 */
export interface MapEdge {
  id: string
  source: string
  target: string
  kind: string
}

export interface MapGraph {
  cards: MapCard[]
  edges: MapEdge[]
}

export interface BuildMapGraphOptions {
  /** 会话元数据索引；缺元数据的节点仍会渲染（标题回落到节点标题） */
  sessions?: Map<string, MapSessionMeta>
  /**
   * 折叠根集合。调用方应把**节点持久化的 `collapsed`** 与运行时的临时覆盖合并后传进来，
   * 本函数不去读 `node.collapsed`，避免出现两种真相。
   */
  collapsed?: ReadonlySet<string>
  /** 正在运行的会话 id（来自子 Agent / 主流程的实时事件） */
  running?: ReadonlySet<string>
  /** 只保留这些节点（用于搜索过滤）；省略则不筛 */
  visibleOnly?: ReadonlySet<string>
}

/**
 * 把父子关系整理成「只认存在的父节点」的形式。
 *
 * 脏数据有两种：父节点被删（`ON DELETE CASCADE` 之下理论上不会发生，但热层与 PG
 * 之间的快照可能短暂不一致）、以及成环（人工编辑或并发保存导致）。
 * 前者降级为根节点，后者靠深度访问路径上的 `visited` 截断 —— 都不会抛错或死循环。
 */
function collectChildren(nodes: SessionMapNode[]): Map<string, string[]> {
  const ids = new Set(nodes.map(n => n.id))
  const children = new Map<string, string[]>()
  for (const node of nodes) {
    const parent = node.parent_node_id
    if (!parent || parent === node.id || !ids.has(parent)) continue
    const list = children.get(parent)
    if (list) list.push(node.id)
    else children.set(parent, [node.id])
  }
  return children
}

/**
 * 统计每个节点的全部后代数。
 *
 * 用带 `inStack` 保护的递归：成环时**不再深入**（返回 0），而不是尝试算出
 * 「环友节点互相算作后代」的精确值。理由：环是脏数据，此时 UI 只需要一个
 * 有限的数字来渲染折叠提示，精确计数没有用户价值，而死循环会让整个页面卡死。
 */
function countDescendants(children: Map<string, string[]>, nodes: SessionMapNode[]): Map<string, number> {
  const counts = new Map<string, number>()
  const inStack = new Set<string>()
  const visit = (id: string): number => {
    const known = counts.get(id)
    if (known !== undefined) return known
    if (inStack.has(id)) return 0
    inStack.add(id)
    let total = 0
    for (const child of children.get(id) ?? []) total += 1 + visit(child)
    inStack.delete(id)
    counts.set(id, total)
    return total
  }
  for (const node of nodes) visit(node.id)
  return counts
}

/** 折叠根的**后代**集合（不含折叠根自己：它必须留在画布上，否则无法再展开）。 */
function hiddenByCollapse(children: Map<string, string[]>, collapsed: ReadonlySet<string>): Set<string> {
  const hidden = new Set<string>()
  const walk = (id: string, seen: Set<string>) => {
    for (const child of children.get(id) ?? []) {
      if (seen.has(child)) continue
      seen.add(child)
      hidden.add(child)
      walk(child, seen)
    }
  }
  for (const root of collapsed) walk(root, new Set([root]))
  // 折叠根自己永远可见 —— 否则用户点折叠后连「展开」的入口都没了。
  // 这一步也兜住脏数据里的环：若 A、B 互相折叠，各自都会被救回来。
  for (const root of collapsed) hidden.delete(root)
  return hidden
}

/**
 * 构建画布图。
 *
 * 顺序：先算可见集合（折叠 + 过滤），再输出卡片，最后**只连两端都可见的边** ——
 * 悬空边会让 Vue Flow 画出连到虚空的线。
 */
export function buildMapGraph(nodes: SessionMapNode[], options: BuildMapGraphOptions = {}): MapGraph {
  const { sessions, collapsed, running, visibleOnly } = options
  const children = collectChildren(nodes)
  const collapseRoots = collapsed ?? new Set<string>()
  const hidden = hiddenByCollapse(children, collapseRoots)
  const descendants = countDescendants(children, nodes)

  const cards: MapCard[] = []
  for (const node of nodes) {
    // `hidden` 是后端的「永久隐藏」（用户在画布上移除但保留数据），
    // 与折叠产生的临时隐藏是两回事，两者都不可见。
    if (node.hidden) continue
    if (hidden.has(node.id)) continue
    if (visibleOnly && !visibleOnly.has(node.id)) continue

    const meta = node.session_id ? sessions?.get(node.session_id) : undefined
    const alias = (meta?.alias ?? '').trim()
    const sessionTitle = (meta?.title ?? '').trim()
    const nodeTitle = (node.title ?? '').trim()

    cards.push({
      id: node.id,
      sessionId: node.session_id ?? '',
      title: alias || nodeTitle || sessionTitle || (node.session_id ?? ''),
      alias,
      tag: (meta?.tag ?? '').trim(),
      color: (node.color ?? '').trim(),
      branchFromSeq: node.branch_from_seq ?? meta?.branch_from_seq ?? 0,
      parentTitle: (meta?.parent_title ?? '').trim(),
      childCount: (children.get(node.id) ?? []).length,
      descendantCount: descendants.get(node.id) ?? 0,
      collapsed: collapseRoots.has(node.id),
      running: !!node.session_id && !!running?.has(node.session_id),
      hasSession: !!node.session_id,
      x: Number.isFinite(node.x) ? node.x : 0,
      y: Number.isFinite(node.y) ? node.y : 0,
      pinned: !!node.pinned,
    })
  }

  const visible = new Set(cards.map(c => c.id))
  const edges: MapEdge[] = []
  for (const node of nodes) {
    const parent = node.parent_node_id
    if (!parent || !visible.has(parent) || !visible.has(node.id)) continue
    const kind = (node.edge_kind ?? '').trim() || 'manual'
    edges.push({ id: `${parent}->${node.id}`, source: parent, target: node.id, kind })
  }

  return { cards, edges }
}

/**
 * 从卡片里挑出「当前会话」所在的那张，没有就返回 null。
 *
 * 定位当前会话时还要把它所有的折叠祖先展开 —— 否则"定位"到一个看不见的卡片。
 */
export function findCardBySession(cards: MapCard[], sessionId: string): MapCard | null {
  if (!sessionId) return null
  return cards.find(c => c.sessionId === sessionId) ?? null
}

/**
 * 求出为了让某张卡片可见，需要展开哪些折叠根。
 *
 * 用法：`for (const id of ancestorsToExpand(...)) collapsed.delete(id)`。
 * 返回顺序是从最近的祖先到最远，便于调用方按需提前退出。
 */
export function ancestorsToExpand(nodes: SessionMapNode[], targetNodeId: string): string[] {
  if (!targetNodeId) return []
  const byId = new Map(nodes.map(n => [n.id, n]))
  const out: string[] = []
  const seen = new Set<string>([targetNodeId])
  let current = byId.get(targetNodeId)?.parent_node_id ?? ''
  while (current && !seen.has(current)) {
    seen.add(current)
    out.push(current)
    current = byId.get(current)?.parent_node_id ?? ''
  }
  return out
}
