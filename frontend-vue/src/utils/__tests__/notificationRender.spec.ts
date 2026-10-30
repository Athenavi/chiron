import { describe, it, expect, afterEach } from 'vitest'
import { setLocale } from '../../i18n'
import { i18n } from '../../i18n'
import { DEFAULT_LOCALE } from '../../i18n/languages'
import {
  renderNotificationBody,
  renderNotificationTitle,
  numOf,
  dateOf,
} from '../notificationRender'
import type { NotificationItem } from '../../api/notifications'

/**
 * `utils/notificationRender.ts` 的测试（T1）。
 *
 * 为什么用**真实 i18n 实例**而不是 stub 的 `t`：这个模块的全部意义就是"按当前
 * 界面语言渲染"，用假 t 只能证明"调了 t"，证明不了三语模板真的存在且键没写错。
 * 后者恰恰是最容易出错的环节 —— 缺键时 vue-i18n 会**原样返回键名**，界面上直接
 * 显示 `notifications.body.member_invited`。这与 `datetime.spec.ts` 用真实
 * `setLocale()` 是同一个理由。
 *
 * 服务的这份模板与后端 `internal/notify/messages.go` 是同一条文案的两个出口，
 * 所以这里也顺带钉住"三类语言都能渲染出来"，而不是只测中文。
 */

afterEach(() => setLocale(DEFAULT_LOCALE))

const t = (key: string, named?: Record<string, unknown>) =>
  (i18n.global.t as unknown as (k: string, n?: Record<string, unknown>) => string)(key, named)

const formatDate = (iso: string): string => {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  // 只取日期部分，与视图里的 dayjs 行为一致（避免时区差异引入脆弱断言）。
  return iso.slice(0, 10)
}

function note(partial: Partial<NotificationItem>): NotificationItem {
  return {
    id: 'n-1',
    kind: 'pay' + 'ment_succeeded',
    title: '服务端兜底标题',
    body: '服务端兜底正文',
    link: '',
    read: false,
    read_at: null,
    created_at: '2026-10-30T00:00:00Z',
    ...partial,
  }
}

describe('numOf（jsonb 数字两种形态都要认）', () => {
  it('接受 number 与数字字符串', () => {
    expect(numOf(1000)).toBe(1000)
    expect(numOf('1000')).toBe(1000)
    // jsonb 数字读回来是 float64，整数额度会以 1000（不是 "1000"）到达。
    expect(numOf(1000.0)).toBe(1000)
  })

  it('无法解析时返回 0，而不是 NaN', () => {
    expect(numOf(undefined)).toBe(0)
    expect(numOf(null)).toBe(0)
    expect(numOf('abc')).toBe(0)
    expect(numOf({})).toBe(0)
    expect(Number.isNaN(numOf('abc'))).toBe(false)
  })
})

describe('dateOf（解析失败原样返回，不吐空）', () => {
  it('有效时间裁成 YYYY-MM-DD', () => {
    expect(dateOf('2026-12-30T10:00:00Z', formatDate)).toBe('2026-12-30')
  })

  it('缺失返回空串', () => {
    expect(dateOf(undefined, formatDate)).toBe('')
    expect(dateOf('', formatDate)).toBe('')
  })

  it('解析失败时原样返回，避免「将于  到期」这种残缺句', () => {
    expect(dateOf('not-a-date', formatDate)).toBe('not-a-date')
  })
})

describe('payment_succeeded（纯充值）', () => {
  it('中文：标题与正文都渲染出额度，且不含键名', () => {
    setLocale('zh-CN')
    const item = note({ payload: { credits: 1000 } })
    expect(renderNotificationTitle(item, t)).toBe('支付成功')
    const body = renderNotificationBody(item, t, formatDate)
    expect(body).toContain('1000')
    expect(body).not.toContain('notifications.')
  })

  it('英文：不残留中文', () => {
    setLocale('en-US')
    const item = note({ payload: { credits: 1000 } })
    expect(renderNotificationTitle(item, t)).toBe('Payment succeeded')
    const body = renderNotificationBody(item, t, formatDate)
    expect(body).toContain('1000')
    expect(body).toContain('credits')
    // 中文正文不该出现在英文界面。
    expect(body).not.toMatch(/[\u4e00-\u9fa5]/)
  })

  it('阿语：用阿语标题', () => {
    setLocale('ar')
    const item = note({ payload: { credits: 1000 } })
    expect(renderNotificationTitle(item, t)).toBe('تم الدفع بنجاح')
    expect(renderNotificationBody(item, t, formatDate)).toContain('1000')
  })
})

describe('payment_succeeded（套餐订单）', () => {
  const item = () =>
    note({
      payload: {
        credits: 5000,
        package_code: 'starter',
        package_name: '入门包',
        expires_at: '2026-12-30T10:00:00Z',
      },
    })

  it('中文：标题区分套餐、正文含套餐名与到期日', () => {
    setLocale('zh-CN')
    expect(renderNotificationTitle(item(), t)).toBe('套餐购买成功')
    const body = renderNotificationBody(item(), t, formatDate)
    expect(body).toContain('入门包')
    expect(body).toContain('2026-12-30')
    expect(body).toContain('5000')
  })

  it('英文：标题与正文均为英文，日期保留', () => {
    setLocale('en-US')
    expect(renderNotificationTitle(item(), t)).toBe('Package purchased')
    const body = renderNotificationBody(item(), t, formatDate)
    expect(body).toContain('2026-12-30')
    // 只断言**模板文案**是英文：`package_name` 是运营填的业务数据（这里是
    // 「入门包」），英文界面照样原样展示，不做翻译 —— 因此不能断言全文无中文。
    expect(body).not.toContain('已生效')
    expect(body).not.toContain('权益有效期')
  })

  it('缺 package_name 时用 package_code 兜底，不渲染空引号', () => {
    setLocale('en-US')
    const noName = note({
      payload: { credits: 1, package_code: 'starter', expires_at: '2026-12-30T10:00:00Z' },
    })
    expect(renderNotificationBody(noName, t, formatDate)).toContain('starter')
  })

  it('缺到期日时退化为准确的「充值到账」文案，而不是残缺句', () => {
    setLocale('zh-CN')
    const noExpiry = note({ payload: { credits: 5000, package_code: 'starter' } })
    const body = renderNotificationBody(noExpiry, t, formatDate)
    expect(body).toContain('5000')
    expect(body).not.toContain('有效期至')
  })
})

describe('entitlement_expiring', () => {
  const item = () =>
    note({
      kind: 'entitlement_expiring',
      payload: { package_name: '入门包', expires_at: '2026-11-02T00:00:00Z', days_before: 3 },
    })

  it('中文：标题含天数，正文含套餐名与日期', () => {
    setLocale('zh-CN')
    expect(renderNotificationTitle(item(), t)).toContain('3')
    const body = renderNotificationBody(item(), t, formatDate)
    expect(body).toContain('入门包')
    expect(body).toContain('2026-11-02')
  })

  it('英文：标题含天数且正文模板为英文', () => {
    setLocale('en-US')
    expect(renderNotificationTitle(item(), t)).toContain('3')
    const body = renderNotificationBody(item(), t, formatDate)
    // 同样只断言模板文案：`package_name` 是运营填的业务数据，不做翻译。
    expect(body).not.toContain('将于')
    expect(body).not.toContain('如需继续使用')
  })

  it('缺 days_before 时回落服务端标题', () => {
    const noDays = note({
      kind: 'entitlement_expiring',
      payload: { package_name: '入门包', expires_at: '2026-11-02T00:00:00Z' },
    })
    expect(renderNotificationTitle(noDays, t)).toBe('服务端兜底标题')
  })

  it('缺套餐名/日期时回落服务端正文', () => {
    const bare = note({ kind: 'entitlement_expiring', payload: {} })
    expect(renderNotificationBody(bare, t, formatDate)).toBe('服务端兜底正文')
  })
})

describe('quota_changed', () => {
  const item = (action: string) =>
    note({
      kind: 'quota_changed',
      payload: {
        resource_type: 'token',
        total_amount: 100000,
        period: 'monthly',
        action,
      },
    })

  it('中文 created / updated 用不同动词', () => {
    setLocale('zh-CN')
    expect(renderNotificationBody(item('created'), t, formatDate)).toContain('已创建')
    expect(renderNotificationBody(item('updated'), t, formatDate)).toContain('已调整')
  })

  it('英文 created / updated 同样区分，且含配额数值', () => {
    setLocale('en-US')
    const created = renderNotificationBody(item('created'), t, formatDate)
    expect(created).toContain('created')
    expect(created).toContain('100000')
    expect(created).not.toMatch(/[\u4e00-\u9fa5]/)
    expect(renderNotificationBody(item('updated'), t, formatDate)).toContain('updated')
  })

  it('缺 resource_type 或 period 时回落服务端正文', () => {
    const broken = note({ kind: 'quota_changed', payload: { total_amount: 1 } })
    expect(renderNotificationBody(broken, t, formatDate)).toBe('服务端兜底正文')
  })
})

describe('member_invited', () => {
  it('三语都含团队名', () => {
    for (const loc of ['zh-CN', 'en-US', 'ar']) {
      setLocale(loc)
      const item = note({ kind: 'member_invited', payload: { tenant_name: 'Acme' } })
      expect(renderNotificationBody(item, t, formatDate)).toContain('Acme')
      expect(renderNotificationTitle(item, t)).not.toContain('notifications.')
    }
  })

  it('缺团队名时回落服务端正文', () => {
    setLocale('en-US')
    const bare = note({ kind: 'member_invited', payload: {} })
    expect(renderNotificationBody(bare, t, formatDate)).toBe('服务端兜底正文')
  })
})

describe('未知 kind（后端新增事件而前端未跟上）', () => {
  it('标题与正文都用服务端文案，不显示键名也不显示空', () => {
    for (const loc of ['zh-CN', 'en-US', 'ar']) {
      setLocale(loc)
      const item = note({ kind: 'some_future_kind', payload: {} })
      expect(renderNotificationTitle(item, t)).toBe('服务端兜底标题')
      expect(renderNotificationBody(item, t, formatDate)).toBe('服务端兜底正文')
    }
  })
})

describe('历史通知跟随语言切换（T1 的核心验收）', () => {
  it('同一条通知在三种语言下渲染出三种语言', () => {
    const item = note({ kind: 'member_invited', payload: { tenant_name: 'Acme' } })

    setLocale('zh-CN')
    const zh = renderNotificationBody(item, t, formatDate)
    setLocale('en-US')
    const en = renderNotificationBody(item, t, formatDate)
    setLocale('ar')
    const ar = renderNotificationBody(item, t, formatDate)

    expect(zh).not.toBe(en)
    expect(en).not.toBe(ar)
    expect(zh).not.toBe(ar)
    // 三者都必须含同一个业务事实（团队名），只是语言不同。
    for (const body of [zh, en, ar]) expect(body).toContain('Acme')
  })
})
