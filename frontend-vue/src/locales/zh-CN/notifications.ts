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

  // ── 正文模板 ──
  //
  // 站内信按**当前界面语言**实时渲染，所以历史通知也会跟着语言切换 ——
  // 这正是服务端只存 kind + payload 而不存渲染结果的原因（见 0011 取舍 6）。
  // 这些模板与后端 `internal/notify/messages.go` 的同名模板必须同义：
  // 站内信与邮件是同一条文案的两个出口，措辞漂移会让用户以为收到了两件事。
  body: {
    payment_succeeded: '充值 {n} credits 已到账。',
    payment_succeeded_package: '套餐「{name}」已生效，{n} credits 已到账。权益有效期至 {date}。',
    entitlement_expiring: '套餐「{name}」将于 {date} 到期，到期后相关模型权益将失效。如需继续使用请及时续购。',
    quota_changed: '租户配额{action}：{resource} 为 {n}（周期 {period}）。',
    quota_action_created: '已创建',
    quota_action_updated: '已调整',
    member_invited: '你已通过邀请加入团队「{name}」。你的额度与可用模型由该团队统一管理。',
  },

  // ── 标题模板 ──
  title_payment_succeeded: '支付成功',
  title_package_payment: '套餐购买成功',
  title_entitlement_expiring: '套餐将在 {n} 天内到期',
  title_quota_changed: '配额已变更',
  title_member_invited: '已加入团队',

  // ── 邮件偏好 ──
  prefs_title: '邮件通知设置',
  prefs_jump: '邮件设置',
  prefs_note: '站内通知始终开启，这里只控制是否同时发送邮件。',
  prefs_kind_email: '邮件提醒',
  prefs_saved: '设置已保存',
  prefs_save_failed: '设置保存失败',
  prefs_load_failed: '设置加载失败',
}
