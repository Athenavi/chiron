/**
 * 会话地图的数据面。
 *
 * 全部端点见 `internal/api/sessionmap.go` 的文件头：布局是**整体读写**（不是逐字段
 * PATCH），因为写入热点是拖节点 —— 逐字段会把一次拖动拆成几十个请求。
 *
 * 这一层只做两件事：拆 `{success, data}` 包装、把后端可能给 `null` 的集合字段兜成
 * 数组/对象。业务语义一律留给 composable，避免"同一个兜底写两遍、两处不一致"。
 */
import { api } from '../../api'

import type {
  BranchRequest,
  BranchResult,
  ForkNodeHint,
  ForkPoint,
  ForkPointsResult,
  ForkResult,
  SaveWorkspaceResult,
  SessionMapNode,
  SessionMapWorkspace,
  WorkspaceSummary,
} from './types'

/**
 * 后端把 `nodes` / `viewport` 直接 JSON 编出来，空值就是字面 `null`
 * （`smLoadFromPG` 在没有任何节点时不会初始化切片）。前端在边界处一次兜住，
 * 后面的代码就不必到处写 `?? []`。
 */
function normalizeWorkspace(raw: unknown): SessionMapWorkspace {
  const ws = (raw ?? {}) as Partial<SessionMapWorkspace>
  return {
    id: typeof ws.id === 'string' ? ws.id : '',
    name: typeof ws.name === 'string' ? ws.name : '',
    viewport: ws.viewport && typeof ws.viewport === 'object' ? ws.viewport : {},
    nodes: Array.isArray(ws.nodes) ? (ws.nodes as SessionMapNode[]) : [],
    created_at: ws.created_at,
    updated_at: ws.updated_at,
  }
}

/** 列出当前用户的画布（只有 id 与名字，不含节点）。 */
export async function listWorkspaces(): Promise<WorkspaceSummary[]> {
  const { data } = await api.get('/v1/session-map/workspaces')
  const list = data?.data?.workspaces
  if (!Array.isArray(list)) return []
  return list
    .map((it: Partial<WorkspaceSummary>) => ({ id: String(it?.id ?? ''), name: String(it?.name ?? '') }))
    .filter((it: WorkspaceSummary) => it.id !== '')
}

/** 新建一张空画布。 */
export async function createWorkspace(name: string): Promise<SessionMapWorkspace> {
  const { data } = await api.post('/v1/session-map/workspaces', { name })
  return normalizeWorkspace(data?.data)
}

/** 读取一张画布（viewport + 全部节点）。 */
export async function getWorkspace(id: string): Promise<SessionMapWorkspace> {
  const { data } = await api.get(`/v1/session-map/workspaces/${encodeURIComponent(id)}`)
  return normalizeWorkspace(data?.data)
}

/**
 * 整体保存一张画布。
 *
 * 幂等：重复提交同一份快照结果相同，所以调用方可以放心用防抖重试，
 * 不必自己去重（`sessionmap.go:11-16`）。
 */
export async function saveWorkspace(
  id: string,
  workspace: Pick<SessionMapWorkspace, 'name' | 'viewport' | 'nodes'>,
): Promise<SaveWorkspaceResult> {
  const { data } = await api.put(`/v1/session-map/workspaces/${encodeURIComponent(id)}`, workspace)
  const result = (data?.data ?? {}) as SaveWorkspaceResult
  return { saved: !!result.saved, nodes: result.nodes, degraded: !!result.degraded }
}

/** 删除一张画布。 */
export async function deleteWorkspace(id: string): Promise<void> {
  await api.delete(`/v1/session-map/workspaces/${encodeURIComponent(id)}`)
}

/**
 * 询问后端"从某个节点分叉"该怎么调。
 *
 * **它本身不产生新会话** —— 后端只回一份调用提示（`fork_endpoint` / `from_index` /
 * `parent_node_id`），真正的复制由 `forkConversation` 完成，之后还要把新节点接上父节点
 * 再 `saveWorkspace`。这是后端刻意的单一职责划分（`sessionmap.go:765-771`），
 * 前端不要试图用一次调用绕过它。
 */
export async function requestForkNode(
  workspaceId: string,
  params: { node_id: string; from_index: number; title?: string },
): Promise<ForkNodeHint> {
  const { data } = await api.post(
    `/v1/session-map/workspaces/${encodeURIComponent(workspaceId)}/fork-node`,
    params,
  )
  return data?.data as ForkNodeHint
}

/** 真正分叉会话（`internal/api/session_fork.go`）。`from_index` 必须 > 0。 */
export async function forkConversation(
  sessionId: string,
  params: { from_index: number; title?: string },
): Promise<ForkResult> {
  const { data } = await api.post(`/v1/conversations/${encodeURIComponent(sessionId)}/fork`, params)
  return data?.data as ForkResult
}

/**
 * 列出可选的智能分叉点（`GET /v1/conversations/{id}/fork-points`）。
 *
 * 只用它做「自定义分叉」的选点，**不要**用会话详情里的消息数组下标 ——
 * 两者排序口径不同（见 `types.ts::ForkPoint` 的说明）。
 */
export async function getForkPoints(sessionId: string): Promise<ForkPointsResult> {
  const { data } = await api.get(`/v1/conversations/${encodeURIComponent(sessionId)}/fork-points`)
  const raw = (data?.data ?? {}) as Partial<ForkPointsResult>
  const points = Array.isArray(raw.points) ? (raw.points as ForkPoint[]) : []
  return {
    points: points.filter(p => Number.isInteger(p?.index) && p.index > 0),
    total: typeof raw.total === 'number' ? raw.total : points.length,
  }
}

/**
 * 创建分支会话（`POST /v1/conversations/{id}/branch`）。
 *
 * 三档分叉都走这一个端点，只差参数：
 *   - 全量：`mode='truncate'`，`from_index` = 总会话条数；
 *   - 压缩：`mode='condense'`，`keep_tail: 0`（一条原文不留，只剩摘要）；
 *   - 自定义：由用户选 `from_index` / `keep_tail`。
 *
 * 压缩档返回 `branch_state='pending'`：摘要由后端异步生成，`condensed=true` 时
 * 前端应显示"压缩中"而不是当成失败。
 */
export async function branchConversation(
  sessionId: string,
  body: BranchRequest,
): Promise<BranchResult> {
  const { data } = await api.post(
    `/v1/conversations/${encodeURIComponent(sessionId)}/branch`,
    body,
  )
  return data?.data as BranchResult
}

/**
 * 重试一次失败的分支压缩（`POST /v1/conversations/{id}/branch/retry`）。
 *
 * 只对 `branch_mode='condense'` 的分支有效；`branch_state='pending'` 时后端返回 409
 * （已有一次压缩在跑，重复触发会写下两份摘要）。
 */
export async function retryBranchCondense(sessionId: string): Promise<void> {
  await api.post(`/v1/conversations/${encodeURIComponent(sessionId)}/branch/retry`)
}
