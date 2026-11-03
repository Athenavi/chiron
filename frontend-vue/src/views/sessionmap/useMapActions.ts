/**
 * 地图的动作层：新建、追问、分叉、改元数据、增删节点。
 *
 * ## 为什么这一层存在
 *
 * 旧形态里，地图（iframe 里的 `app.js`）发起会话操作要经过
 * `postMessage → 宿主 → 回执` 这条 RPC。任一侧改名或漏实现
 * 一条回执，用户就要空等 20 秒才看到"会话服务未在规定时间内响应" —— 这不是假设，
 * 历史上三条回执就**从未实现过**。
 *
 * 现在地图与宿主在同一个 SPA 里，所以动作就是普通的 `await`：错误照常冒泡到调用方，
 * 由组件里的 `try/catch` 展示，不再有 requestId 配对、不再有超时。
 *
 * ## 与宿主的分工
 *
 * 会话的主人是宿主（`ChatView`），地图只是它的另一种视图。因此：
 *   - 建会话、发消息、分叉、改元数据**一律走宿主已有的 REST**，不另开通道；
 *   - `llm_config` 由宿主注入（`buildLlmConfig`）—— 地图不该复制一份模型/工具授权
 *     等偏好状态，否则两处默认值迟早不一致。
 */
import { api, updateConversation } from '../../api'
import { t } from '../../i18n'

import { forkConversation } from './api'
import type { SessionMapNode } from './types'

/**
 * 追问的文本前缀。
 *
 * 这是**协议**而不是文案：历史会话里已经存着 `【请简短回答问题】:({text})` 形式的
 * 用户消息，服务端与引擎侧按它识别"一次性简短回复"。所以它刻意不随界面语言变化
 * （三套 locale 的这个键值完全相同），改它会让老会话里的追问变成普通消息。
 */
export function followupContent(text: string): string {
  return t('chat.sessionmap.followup', { text })
}

/** 宿主可注入的能力；全部可选，缺省时按"没有该偏好"处理。 */
export interface MapActionDeps {
  /**
   * 宿主当前的请求级配置（模型 / 工具授权 / 输出风格…）。
   *
   * 由宿主注入而不是在这里重建：那套默认值依赖 UI 上的实时选择，
   * 在地图层复制一份必然 drift（drift 的表现是"地图里发的消息用了别的模型"）。
   */
  buildLlmConfig?: () => Record<string, unknown>
}

export interface CreatedSession {
  id: string
  title: string
}

function llmConfigWith(deps: MapActionDeps, extra: Record<string, unknown> = {}): Record<string, unknown> {
  const base = deps.buildLlmConfig?.() ?? {}
  return { ...base, ...extra }
}

/**
 * 新建会话。
 *
 * 标题为空时用本地化的"新对话"：服务端不会替我们兜底，
 * 空标题会让会话列表里出现一条无法辨认的空行。
 */
export async function createSession(
  title: string,
  deps: MapActionDeps = {},
  defaultTitle = '',
): Promise<CreatedSession> {
  const name = title.trim() || defaultTitle || t('chat.sessionmap.newSession')
  const { data } = await api.post('/v1/conversations', {
    title: name,
    llm_config: llmConfigWith(deps),
  })
  const payload = (data?.data ?? data) as { id?: string; title?: string } | null
  if (!payload?.id) {
    // 不静默返回一个假 id：调用方会拿它去建画布节点，最终指向一个不存在的会话。
    throw new Error(t('errors.failed_to_create_session'))
  }
  return { id: payload.id, title: payload.title || name }
}

/**
 * 在指定会话上发一条消息。
 *
 * `followup = true` 时加上协议前缀（一次性简短回复）；否则按普通消息发送。
 */
export async function sendMessage(
  sessionId: string,
  text: string,
  options: { followup?: boolean; deps?: MapActionDeps } = {},
): Promise<void> {
  const { followup = false, deps = {} } = options
  const content = followup ? followupContent(text) : text
  await api.post('/submit', {
    content,
    session_id: sessionId,
    // 发送侧幂等：与对话页同一条约定（服务端 5 分钟去重窗口）。
    // 少了它，用户在弱网下连点两次就会产生两轮对话。
    llm_config: llmConfigWith(deps, { client_msg_id: crypto.randomUUID() }),
  })
}

/**
 * 从源会话的第 `atSeq` 条消息处分叉。
 *
 * `atSeq` 必须 > 0（`session_fork.go:44-47` 直接 400 拒绝），这里提前拦下：
 * 让错误在 UI 就近显示，而不是变成一个笼统的 400。
 */
export async function forkSession(
  sessionId: string,
  atSeq: number,
  title: string,
): Promise<CreatedSession> {
  if (!Number.isInteger(atSeq) || atSeq <= 0) {
    throw new Error(t('chat.sessionmap.invalidForkPoint'))
  }
  const result = await forkConversation(sessionId, { from_index: atSeq, title })
  if (!result?.session_id) {
    throw new Error(t('errors.fork_failed'))
  }
  return { id: result.session_id, title }
}

/** 改会话元数据：标题 / 别名 / 标签。空串表示清除该字段。 */
export async function updateSessionMeta(
  sessionId: string,
  patch: { title?: string; tag?: string; alias?: string },
): Promise<void> {
  await updateConversation(sessionId, patch)
}

/**
 * 生成画布节点的 id。
 *
 * 由**前端**生成而不是后端：`smPersist` 直接把 `n.ID` 插进 `session_map_nodes`
 * （`ON CONFLICT (id) DO UPDATE`），后端不补 id、也不做去重 ——
 * 若前端交空 id，整张画布会在同一个冲突目标上互相覆盖。
 */
export function newNodeId(): string {
  return `n-${crypto.randomUUID()}`
}

/** 新节点相对父节点的横向偏移（与旧实现的卡片间距一致，避免升级后布局观感突变）。 */
const NODE_OFFSET_X = 260
const NODE_OFFSET_Y = 0

export interface NewNodeInput {
  sessionId: string
  title?: string
  /** 从哪个节点分出去的；省略 = 根节点 */
  parentNodeId?: string
  /** 分叉点（消息序号） */
  branchFromSeq?: number
}

/**
 * 往画布上追加一个节点（不可变，返回新数组）。
 *
 * 位置放在父节点右侧一列；没有父节点则落在原点附近。用户随后拖动会覆盖它 ——
 * 这里只是给一个"不会叠在别人身上"的初始值。
 */
export function appendNode(nodes: readonly SessionMapNode[], input: NewNodeInput): SessionMapNode[] {
  const parent = input.parentNodeId ? nodes.find(n => n.id === input.parentNodeId) : undefined
  const node: SessionMapNode = {
    id: newNodeId(),
    session_id: input.sessionId,
    ...(input.parentNodeId ? { parent_node_id: input.parentNodeId, edge_kind: 'branch' } : {}),
    x: parent ? parent.x + NODE_OFFSET_X : 80,
    y: parent ? parent.y + NODE_OFFSET_Y : 80,
    ...(input.title ? { title: input.title } : {}),
    ...(input.branchFromSeq && input.branchFromSeq > 0 ? { branch_from_seq: input.branchFromSeq } : {}),
  }
  return [...nodes, node]
}

/** 从画布移除节点，并同步删掉连到它下面的子节点（避免留下悬空父引用）。 */
export function removeNode(nodes: readonly SessionMapNode[], nodeId: string): SessionMapNode[] {
  const doomed = new Set([nodeId])
  let grew = true
  while (grew) {
    grew = false
    for (const node of nodes) {
      const parent = node.parent_node_id
      if (parent && doomed.has(parent) && !doomed.has(node.id)) {
        doomed.add(node.id)
        grew = true
      }
    }
  }
  return nodes.filter(n => !doomed.has(n.id))
}

/** 改单个节点的坐标（拖动落点）。 */
export function moveNode(
  nodes: readonly SessionMapNode[],
  nodeId: string,
  position: { x: number; y: number },
): SessionMapNode[] {
  return nodes.map(n => (n.id === nodeId ? { ...n, x: Math.round(position.x), y: Math.round(position.y) } : n))
}

/** 切换节点的折叠标记（持久化到后端，刷新后仍保持）。 */
export function toggleNodeCollapsed(nodes: readonly SessionMapNode[], nodeId: string): SessionMapNode[] {
  return nodes.map(n => (n.id === nodeId ? { ...n, collapsed: !n.collapsed } : n))
}
