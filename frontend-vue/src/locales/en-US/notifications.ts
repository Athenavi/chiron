/**
 * Notification center (P2-3).
 *
 * In-app notifications cannot be turned off; these strings only describe the
 * "also send an email" switch. That is why they talk about *email* rather than
 * *notifications* — calling it "turn off notifications" would read as if the
 * in-app entry disappeared too.
 */
export default {
  title: 'Notifications',
  unread_badge: 'Unread notifications',
  empty: 'No notifications yet',
  empty_unread: 'No unread notifications',
  filter_all: 'All',
  filter_unread: 'Unread only',
  mark_all_read: 'Mark all as read',
  mark_read: 'Mark as read',
  marked_read: '{n} marked as read',
  nothing_to_mark: 'Nothing to mark as read',
  load_failed: 'Failed to load notifications',
  view_detail: 'View',

  // Event types: used both as category labels and as preference switch titles.
  // One-to-one with AllKinds in internal/notify.
  kind: {
    payment_succeeded: 'Payment succeeded',
    entitlement_expiring: 'Package expiring soon',
    quota_changed: 'Quota changed',
    member_invited: 'Joined a team',
  },

  // ── Email preferences ──
  prefs_title: 'Email notification settings',
  prefs_note: 'In-app notifications are always on; this only controls whether an email is sent as well.',
  prefs_kind_email: 'Email alerts',
  prefs_saved: 'Settings saved',
  prefs_save_failed: 'Failed to save settings',
  prefs_load_failed: 'Failed to load settings',
}
