/**
 * 会话地图的类型定义。
 *
 * 字段严格对齐后端 `internal/api/sessionmap.go` 的 JSON tag，**不新增、不改名** ——
 * 这份数据面的读写两端各有一份实现（Go 网关 + desktop-local/app/api/session_map.py），
 * 前端擅自加字段只会在另一个后端上变成静默的 undefined。
 */

/**
 * 地图上的一个节点：对**会话的引用** + 布局。
 *
 * 关键区别：节点不是消息容器。消息永远走 `/v1/conversations/{id}`，
 * 这里只存"它在画布的哪个位置、和谁连着"。
 */
export interface SessionMapNode {
  id: string
  /** 关联的会话；为空表示这是一个还没绑定会话的占位卡 */
  session_id?: string
  /** 父节点（血缘连线）；为空表示根节点 */
  parent_node_id?: string
  /**
   * 连线语义。后端从 PG 读出时的默认值是 `'manual'`（`sessionmap.go:441` 的
   * `COALESCE(n.edge_kind,'manual')`），分支产生的边是 `'branch'`。
   */
  edge_kind?: string
  x: number
  y: number
  /** 节点自己的标题；为空时后端已回填成会话标题（`COALESCE(NULLIF(n.title,''), s.title, '')`） */
  title?: string
  color?: string
  collapsed?: boolean
  hidden?: boolean
  pinned?: boolean
  /** 分叉点：保留到源会话的第几条消息（1 基，含该条） */
  branch_from_seq?: number
  created_at?: string
  updated_at?: string
}

/**
 * 画布视口。后端存的是 `map[string]interface{}` 原样 JSON，**不做校验** ——
 * 所以这里给的是"期望形状"而非"保证形状"，读取时要逐字段兜底。
 */
export interface MapViewport {
  x?: number
  y?: number
  zoom?: number
}

/** 一张画布：视口 + 全部节点。整体读取、整体保存（见 `sessionmap.go:11-13` 的说明）。 */
export interface SessionMapWorkspace {
  id: string
  name: string
  viewport: MapViewport
  nodes: SessionMapNode[]
  created_at?: string
  updated_at?: string
}

/** `GET /v1/session-map/workspaces` 的元素：只有 id 与名字，不含节点。 */
export interface WorkspaceSummary {
  id: string
  name: string
}

/**
 * `PUT /v1/session-map/workspaces/{id}` 的响应。
 *
 * `degraded=true` 表示 Redis 热层不可用、本次是**直写 PG**（`sessionmap.go:688-695`）：
 * 数据没丢，但后续读会走慢路径。UI 不必打扰用户，日志里区分即可。
 */
export interface SaveWorkspaceResult {
  saved: boolean
  nodes?: number
  degraded?: boolean
}

/**
 * `POST /v1/session-map/workspaces/{id}/fork-node` 的响应。
 *
 * **注意它不执行分叉**：后端只解析出源会话与分叉点，把真正的复制交还给
 * `/v1/conversations/{id}/fork`（`sessionmap.go:765-771`，理由是"单一职责，
 * 避免两处各写一份复制逻辑"）。
 *
 * 因此前端必须走两步：先调 `forkNode` 拿提示 → 再用 `forkConversation` 真分叉 →
 * 最后把新节点接上 `parent_node_id` 并 `saveWorkspace`。少一步都只是"看起来分叉了"。
 */
export interface ForkNodeHint {
  fork_endpoint: string
  from_index: number
  parent_node_id: string
  hint?: string
}

/** `POST /v1/conversations/{id}/fork` 的响应（`session_fork.go:61-69`）。 */
export interface ForkResult {
  session_id: string
  parent_session_id: string
  /** 保留到源会话的第几条消息 */
  branch_from_seq: number
  /** 实际复制了多少条 */
  copied: number
}
