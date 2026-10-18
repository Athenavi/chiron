/**
 * 人工介入待办 API（网关 internal/api/interactions.go → 引擎 app/api/interactions.py）。
 *
 * 对位的是"**进程没了也看得见、答得上**"这条保证：某一轮跑到需要用户确认/回答的那一步时
 * 会以 `awaiting_input` 收尾（不是空等、也不是直接拒绝），现场写进 `pending_interactions`。
 * 这里的列表就是那张表的读视图，答复就是把票写成 `answered`。
 *
 * 答复**不会自己把那一轮跑起来**：接着跑靠下一条提交（见 `respond` 的调用方）。
 * 所以"答完没反应"不是 bug —— 会话下一次提交时会自动从暂停处接手；
 * 但如果那一行停在 `answered`，列表里就一直看得到它，用户知道还欠一次继续。
 */
import { api } from './index'

export type InteractionKind = 'approval' | 'question' | 'budget'
export type InteractionStatus = 'pending' | 'answered'

export interface PendingInteraction {
  id: string
  kind: InteractionKind
  status: InteractionStatus
  tool_name?: string
  tool_call_id?: string
  /** approval: `{question, level, risk, arguments}`；question: `{question, options}`；
   *  budget: `{question, root_run_id, axis, cap_tokens, settled_tokens, suggested_cap_tokens}` */
  request?: Record<string, unknown>
  /** 仅 `answered`：已收到的答复（列表里显示"你答过了"，避免重复作答的困惑） */
  decision?: Record<string, unknown>
  reason?: string
  created_at?: string
  expires_at?: string
}

/** 该会话仍"等着人"的介入；无归属/无待办时为空数组（后端按来电者过滤，不按会话猜测）。 */
export async function listPendingInteractions(sessionId: string, limit = 20): Promise<PendingInteraction[]> {
  if (!sessionId) return []
  const { data } = await api.get(`/v1/agent/sessions/${encodeURIComponent(sessionId)}/interactions`, {
    params: { limit },
  })
  const items = data?.data?.items
  return Array.isArray(items) ? (items as PendingInteraction[]) : []
}

/**
 * 答复一张票。`response` 的形状由那一行的 `kind` 决定（后端按库里的 kind 校验，不按客户端声称）：
 *
 * * approval：`{decision: 'approve'|'reject'|'edit', arguments?, reason?}`
 *   —— `edit` 的 `arguments` 是**编辑后的完整参数对象**（JSON 字符串），与实时审批卡同一契约；
 * * question：`{answer}` 或 `{declined: true, reason?}`；
 * * budget：`{cap_tokens: <整数>}`。它**不是一张等着被取走的授权票**：答复即刻改写那条任务线
 *   的上限并解除暂停，那一行同时关掉（`consumed`），所以它不会像前两类那样停在 `answered`。
 *   也没有"拒绝"这一项 —— 不答复就等于不提额，线继续停着，做一个关掉票却不改变任何事的按钮
 *   只会造出"我处理过了"的错觉。
 *
 * 业务失败是 HTTP 状态码（404 不存在 / 409 已有下文 / 400 形状不对），由调用方呈现；
 * 返回 false 表示"没答上"，调用方**不该**随后再去提交唤醒。
 */
export async function resolveInteraction(params: {
  interactionId: string
  sessionId: string
  response: Record<string, unknown>
}): Promise<boolean> {
  const { data } = await api.post(
    `/v1/agent/interactions/${encodeURIComponent(params.interactionId)}/resolve`,
    { session_id: params.sessionId, response: params.response },
  )
  return !!(data?.success && data?.data)
}
