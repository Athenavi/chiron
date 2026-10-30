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

  // ── Body templates ──
  //
  // Rendered client-side against the *current* UI language, so historical
  // notifications follow a language switch too — that is why the server stores
  // only kind + payload instead of rendered text (see 0011 note 6).
  body: {
    payment_succeeded: '{n} credits have been added to your balance.',
    payment_succeeded_package: 'Package "{name}" is now active; {n} credits have been added. Entitlements are valid until {date}.',
    entitlement_expiring: 'Package "{name}" expires on {date}; the related model entitlements will stop working after that. Buy a new package to keep using them.',
    quota_changed: 'Tenant quota {action}: {resource} is now {n} (period {period}).',
    quota_action_created: 'created',
    quota_action_updated: 'updated',
    member_invited: 'You joined the team "{name}" via an invitation. Your quota and available models are managed by that team.',
  },

  // ── Title templates ──
  title_payment_succeeded: 'Payment succeeded',
  title_package_payment: 'Package purchased',
  title_entitlement_expiring: 'Package expires within {n} day(s)',
  title_quota_changed: 'Quota changed',
  title_member_invited: 'Joined a team',

  // ── Email preferences ──
  prefs_title: 'Email notification settings',
  prefs_jump: 'Email settings',
  prefs_note: 'In-app notifications are always on; this only controls whether an email is sent as well.',
  prefs_kind_email: 'Email alerts',
  prefs_saved: 'Settings saved',
  prefs_save_failed: 'Failed to save settings',
  prefs_load_failed: 'Failed to load settings',
}
