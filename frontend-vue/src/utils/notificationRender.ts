import type { NotificationItem } from '../api/notifications'

/**
 * 通知标题/正文的本地化渲染（P2-3 / T1）。
 *
 * 为什么要抽成纯模块：服务端只存 `kind` + `payload`（见迁移 0011 取舍 6），
 * 渲染推迟到读取时 —— 于是**历史通知也会跟随语言切换**。这段逻辑需要单测覆盖
 * 「四类事件 × 三语 + 参数缺失回落」，放在 .vue 的 setup 里测不到。
 *
 * `t` 由调用方传入（组件里是 `useI18n()` 的 t）：直接用 i18n 的全局 `t`
 * 不会建立响应式依赖，切换语言后列表不会重渲染。
 */

/** vue-i18n 的 t 签名子集（只要这两种用法）。 */
export type Translate = (key: string, named?: Record<string, unknown>) => string

function strOf(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

/**
 * 从 payload 取整数。
 *
 * jsonb 里的数字可能是 number，也可能是数字字符串（不同驱动/序列化路径），
 * 两种都要认，否则额度会显示成 0。
 */
export function numOf(v: unknown): number {
  if (typeof v === 'number' && Number.isFinite(v)) return v
  if (typeof v === 'string') {
    const n = Number(v)
    return Number.isFinite(n) ? n : 0
  }
  return 0
}

/**
 * 把 RFC3339 时间裁成 YYYY-MM-DD。
 *
 * 解析失败时**原样返回**而不是空串：空串会让文案出现「将于  到期」，
 * 原样返回至少让用户看到实际值。
 */
export function dateOf(v: unknown, format: (iso: string) => string): string {
  const s = strOf(v)
  if (!s) return ''
  const out = format(s)
  return out || s
}

/**
 * 按当前界面语言渲染通知标题。
 *
 * 任何一步拿不到参数就回落服务端给的 `item.title` —— 语言可能不对，但内容完整，
 * 好过空白。
 */
export function renderNotificationTitle(item: NotificationItem, t: Translate): string {
  const p = item.payload ?? {}
  switch (item.kind) {
    case 'payment_succeeded':
      return strOf(p.package_code)
        ? t('notifications.title_package_payment')
        : t('notifications.title_payment_succeeded')
    case 'entitlement_expiring': {
      const days = numOf(p.days_before)
      return days > 0
        ? t('notifications.title_entitlement_expiring', { n: days })
        : item.title
    }
    case 'quota_changed':
      return t('notifications.title_quota_changed')
    case 'member_invited':
      return t('notifications.title_member_invited')
    default:
      // 未知 kind：后端新增了事件类型但前端还没跟上，用服务端文案。
      return item.title
  }
}

/** 按当前界面语言渲染通知正文；参数不全时回落 `item.body`。 */
export function renderNotificationBody(
  item: NotificationItem,
  t: Translate,
  formatDate: (iso: string) => string,
): string {
  const p = item.payload ?? {}
  switch (item.kind) {
    case 'payment_succeeded': {
      const credits = numOf(p.credits)
      if (!strOf(p.package_code)) {
        return t('notifications.body.payment_succeeded', { n: credits })
      }
      const name = strOf(p.package_name) || strOf(p.package_code)
      const expires = dateOf(p.expires_at, formatDate)
      // 到期日缺失时退化为「充值到账」那条准确定义的文案，
      // 而不是渲染出「权益有效期至 。」这种残缺句。
      if (!expires) {
        return t('notifications.body.payment_succeeded', { n: credits })
      }
      return t('notifications.body.payment_succeeded_package', {
        name,
        n: credits,
        date: expires,
      })
    }
    case 'entitlement_expiring': {
      const name = strOf(p.package_name)
      const expires = dateOf(p.expires_at, formatDate)
      if (!name || !expires) return item.body
      return t('notifications.body.entitlement_expiring', { name, date: expires })
    }
    case 'quota_changed': {
      const resource = strOf(p.resource_type)
      const period = strOf(p.period)
      if (!resource || !period) return item.body
      const actionKey =
        strOf(p.action) === 'created'
          ? 'notifications.body.quota_action_created'
          : 'notifications.body.quota_action_updated'
      return t('notifications.body.quota_changed', {
        action: t(actionKey),
        resource,
        n: numOf(p.total_amount),
        period,
      })
    }
    case 'member_invited': {
      const name = strOf(p.tenant_name)
      if (!name) return item.body
      return t('notifications.body.member_invited', { name })
    }
    default:
      return item.body
  }
}
