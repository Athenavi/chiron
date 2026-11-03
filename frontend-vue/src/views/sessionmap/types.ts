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

/**
 * `GET /v1/conversations/{id}/fork-points` 的元素。
 *
 * `index` 是 **1 基**序号，与后端复制窗口的 `ROW_NUMBER() OVER (ORDER BY created_at, id)`
 * 完全同口径（`manager_queries.go::ListForkPoints`）。**不能**拿
 * `GET /v1/conversations/{id}` 的消息数组下标来推 —— 那条路径按 `seq` 排序，
 * 而 0016 迁移自述存量行的 `seq` 与 `created_at` 顺序可能不一致，
 * 混用会让"从第 10 条分叉"实际复制另外 10 条，且**没有任何报错**。
 */
export interface ForkPoint {
  index: number
  role: string
  preview: string
  created_at: string
}

/** `GET /v1/conversations/{id}/fork-points` 的响应。 */
export interface ForkPointsResult {
  points: ForkPoint[]
  /** 总会话条数；0 表示空会话（此时任何 from_index 都不合法） */
  total: number
}

/** 分叉方式：逐字复制前 N 条，或压成摘要。与后端 `mode` 字段一一对应。 */
export type BranchMode = 'truncate' | 'condense'

/** `POST /v1/conversations/{id}/branch` 的请求体。 */
export interface BranchRequest {
  /** 分叉点：保留到源会话的第几条消息（含该条），必须 > 0 */
  from_index: number
  /**
   * 压缩式分叉下保留多少条**原文**。
   *
   * 省略 = 用后端默认值 4；显式 `0` = 一条原文都不留，整段进压缩区
   * （新会话里只剩一条摘要）—— 「压缩分叉」档位就是靠 `0` 表达的。
   * 后端为此把该字段改成指针语义（`manager_branch.go::planBranch`）。
   */
  keep_tail?: number
  mode: BranchMode
  title?: string
}

/**
 * `POST /v1/conversations/{id}/branch` 的响应（`session_branch.go`）。
 *
 * `condensed=true` 且 `branch_state='pending'` 表示摘要还在后台生成：
 * 新会话**已经可用**（它带着尾部原文），摘要几秒后作为一条 `role=system`、
 * `source=branch_summary` 的消息出现。压缩失败则置 `failed`，
 * 由 `POST /v1/conversations/{id}/branch/retry` 重试。
 */
export interface BranchResult {
  session_id: string
  parent_session_id: string
  branch_from_seq: number
  branch_mode: BranchMode
  branch_state: 'pending' | 'ready' | 'failed' | ''
  copied: number
  condensed: boolean
}
