import { describe, it, expect, afterEach } from 'vitest'
import { i18n, setLocale } from '../../i18n'
import { DEFAULT_LOCALE } from '../../i18n/languages'
import { buildAccountMenuItems, accountMenuRoute } from '../accountMenu'

/**
 * `components/accountMenu.ts` 的测试（T3）。
 *
 * 这个模块存在的理由就是"两份菜单已经漂移"：`AppLayout` 与 `chat/ChatSidePanel`
 * 各写一份，聊天页那份缺 `api-keys` 与**通知入口**，而聊天页隐藏了顶栏 ——
 * 聊天中的用户完全点不到通知中心。所以这里钉三件事：
 *
 *  1. **云端模式必须有 notifications**（顶栏与聊天侧栏都得有）；
 *  2. **本地模式整组账号入口都不展示**（没有账号体系，通知表也无处落地）；
 *  3. 每个 label 在三种语言下都**真的翻译过**（缺键时 vue-i18n 会原样返回键名，
 *     界面上直接显示 `notifications.title`）—— 用真实 i18n 实例而不是 stub t。
 */

afterEach(() => setLocale(DEFAULT_LOCALE))

const t = (key: string) => i18n.global.t(key) as string
const keysOf = (isLocal: boolean, includeApiKeys?: boolean) =>
  buildAccountMenuItems(t, { isLocal, includeApiKeys }).map((i) => i.key)

describe('buildAccountMenuItems · 云端模式', () => {
  it('含通知中心（T3 的核心）', () => {
    expect(keysOf(false)).toContain('notifications')
  })

  it('顶栏菜单含 api-keys，聊天页菜单不含（显式参数差异，非漂移）', () => {
    expect(keysOf(false, true)).toContain('api-keys')
    expect(keysOf(false, false)).not.toContain('api-keys')
  })

  it('两项都保留 settings / profile / logout', () => {
    for (const includeApiKeys of [true, false]) {
      const keys = keysOf(false, includeApiKeys)
      expect(keys).toContain('settings')
      expect(keys).toContain('profile')
      expect(keys).toContain('logout')
    }
  })

  it('图标是渲染函数（ant-design-vue Menu 的 item 契约）', () => {
    for (const item of buildAccountMenuItems(t, { isLocal: false })) {
      expect(typeof item.icon).toBe('function')
    }
  })
})

describe('buildAccountMenuItems · 本地模式', () => {
  it('不展示通知、api-keys 与登出（本地没有账号体系）', () => {
    const keys = keysOf(true)
    expect(keys).not.toContain('notifications')
    expect(keys).not.toContain('api-keys')
    expect(keys).not.toContain('logout')
  })

  it('仍保留 settings / profile（本地也有界面偏好与个人信息）', () => {
    const keys = keysOf(true)
    expect(keys).toContain('settings')
    expect(keys).toContain('profile')
  })

  it('即便显式传 includeApiKeys=true，本地模式也不展示', () => {
    // 本地模式没有用户级 API Key 的落点，参数不该覆盖这条硬规则。
    expect(keysOf(true, true)).not.toContain('api-keys')
  })
})

describe('label 三语齐备（缺键会原样返回键名）', () => {
  const locales = ['zh-CN', 'en-US', 'ar']

  it('账号菜单每一项在三种语言下都有译文', () => {
    for (const loc of locales) {
      setLocale(loc, { persist: false })
      for (const item of buildAccountMenuItems(t, { isLocal: false, includeApiKeys: true })) {
        // 缺键时 vue-i18n 返回的就是 key 本身（形如 `notifications.title`）。
        expect(item.label, `${loc} 下 ${item.key} 的 label 未翻译`).not.toContain('.')
        expect(item.label.length).toBeGreaterThan(0)
      }
    }
  })

  it('通知中心的三语文案互不相同（说明真的切换了语言）', () => {
    const labels = locales.map((loc) => {
      setLocale(loc, { persist: false })
      const item = buildAccountMenuItems(t, { isLocal: false }).find((i) => i.key === 'notifications')
      return item?.label ?? ''
    })
    expect(new Set(labels).size).toBe(locales.length)
    expect(labels.every((l) => l.length > 0)).toBe(true)
  })
})

describe('accountMenuRoute · 纯跳转映射', () => {
  it('profile / notifications / api-keys 有对应路径', () => {
    expect(accountMenuRoute('profile')).toBe('/profile')
    expect(accountMenuRoute('notifications')).toBe('/notifications')
    expect(accountMenuRoute('api-keys')).toBe('/api-keys')
  })

  it('settings 与 logout 返回 null（它们有副作用，由组件自己处理）', () => {
    expect(accountMenuRoute('settings')).toBeNull()
    expect(accountMenuRoute('logout')).toBeNull()
  })

  it('未知 key 返回 null，不会误跳', () => {
    expect(accountMenuRoute('made-up')).toBeNull()
  })
})
