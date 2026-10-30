import api from './index'

/**
 * 站内通知中心（P2-3）的接口封装。
 *
 * 后端约定（`internal/api/notification_handler.go`）：
 *   - 所有接口只操作**当前登录用户自己的**通知，`user_id` 取自 JWT claims，
 *     前端不传也传不了别人的 user_id；
 *   - `GET /v1/notifications` 同时返回 `unread_count`，渲染列表时几乎总要画角标，
 *     不必为此多打一次请求；
 *   - `POST .../read` 返回的是「本次真正改变的行数」，重复标记同一批返回 `marked: 0`
 *     而不是错误 —— 调用方按「目标状态已达成」处理即可。
 */

/**
 * 事件类型。与后端 `internal/notify/notify.go` 的 `AllKinds` **必须一致**。
 *
 * 后端会把完整列表放在 `GET /v1/notifications/prefs` 的 `kinds` 里，页面按它渲染
 * 开关，所以这里用联合类型只为编译期拼写检查，不作为唯一真相源。
 */
export type NotificationKind =
  | 'payment_succeeded'
  | 'entitlement_expiring'
  | 'quota_changed'
  | 'member_invited'

export interface NotificationItem {
  id: string
  kind: NotificationKind | string
  title: string
  body: string
  /** 站内跳转路径（如 `/billing`）；空串表示没有可跳转的地方。 */
  link: string
  /** 事件附加细节（payment_id、expires_at 等）。结构随 kind 变化，页面不要依赖具体键。 */
  payload?: Record<string, unknown>
  read: boolean
  read_at: string | null
  created_at: string
}

export interface NotificationList {
  items: NotificationItem[]
  unread_count: number
}

export interface NotificationPrefsResponse {
  /** 各事件类型的邮件开关。**未设置过的由后端补成 true**，前端直接当作实际生效值。 */
  email_enabled: Record<string, boolean>
  /** 后端支持的全部事件类型，用于渲染开关列表。 */
  kinds: string[]
  note?: string
}

/** 拉取通知列表。`limit` 由后端夹逼到上限，传大值不会报错，只会截断。 */
export async function listNotifications(
  options: { limit?: number; unreadOnly?: boolean } = {},
): Promise<NotificationList> {
  const { data } = await api.get('/v1/notifications', {
    params: {
      ...(options.limit ? { limit: options.limit } : {}),
      ...(options.unreadOnly ? { unread_only: 'true' } : {}),
    },
  })
  return data?.data
}

/** 只取未读数，供顶栏角标轮询（比拉整个列表轻得多）。 */
export async function getUnreadCount(): Promise<number> {
  const { data } = await api.get('/v1/notifications/unread_count')
  return data?.data?.unread_count ?? 0
}

/** 把指定通知标为已读，返回本次实际改变的行数。 */
export async function markNotificationsRead(ids: string[]): Promise<number> {
  const { data } = await api.post('/v1/notifications/read', { ids })
  return data?.data?.marked ?? 0
}

/** 把全部未读标为已读，返回本次实际改变的行数。 */
export async function markAllNotificationsRead(): Promise<number> {
  const { data } = await api.post('/v1/notifications/read_all')
  return data?.data?.marked ?? 0
}

/** 读取实际生效的邮件偏好（站内通知不可关闭，这里只控制是否同时发邮件）。 */
export async function getNotificationPrefs(): Promise<NotificationPrefsResponse> {
  const { data } = await api.get('/v1/notifications/prefs')
  return data?.data
}

/**
 * 更新某类事件的邮件开关。
 *
 * 单条更新而非整表覆盖：整表覆盖要求先 GET 再 PUT，中间若有别处改过偏好
 * （比如用户在邮件里点了退订），会被静默改回去。
 */
export async function updateNotificationPref(
  kind: string,
  emailEnabled: boolean,
): Promise<NotificationPrefsResponse> {
  const { data } = await api.put('/v1/notifications/prefs', {
    kind,
    email_enabled: emailEnabled,
  })
  return data?.data
}
