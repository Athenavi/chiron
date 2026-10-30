/**
 * 通知中心（P2-3）。
 *
 * 约定：站内通知**不可关闭**，这里只描述"是否同时发邮件"的开关。所以文案里
 * 反复出现"邮件"而不是"通知"—— 说成"关闭通知"会让用户以为站内信也没了。
 */
export default {
  title: '通知中心',
  unread_badge: '未读通知',
  empty: '暂无通知',
  empty_unread: '没有未读通知',
  filter_all: '全部',
  filter_unread: '仅未读',
  mark_all_read: '全部标为已读',
  mark_read: '标为已读',
  marked_read: '已标为 {n} 条已读',
  nothing_to_mark: '没有需要标记的通知',
  load_failed: '通知加载失败',
  view_detail: '查看',

  // 事件类型：既是分类标签，也是偏好开关的标题。
  // 与后端 internal/notify 的 AllKinds 一一对应。
  kind: {
    payment_succeeded: '支付成功',
    entitlement_expiring: '套餐即将到期',
    quota_changed: '配额变更',
    member_invited: '已加入团队',
  },

  // ── 邮件偏好 ──
  prefs_title: '邮件通知设置',
  prefs_note: '站内通知始终开启，这里只控制是否同时发送邮件。',
  prefs_kind_email: '邮件提醒',
  prefs_saved: '设置已保存',
  prefs_save_failed: '设置保存失败',
  prefs_load_failed: '设置加载失败',
}
