import { describe, it, expect, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useThemeStore } from '../theme'

/**
 * `stores/theme.ts` 的测试（2026-10-09）—— **这个 store 此前零测试** ✗。
 *
 * 为什么现在补：第 139–143 轮在补 `src/components/**` 的人口，本轮顺手量了一眼 `src/stores/`
 * —— 只有 `auth.spec.ts` / `typography.spec.ts`，**`theme.ts`（298 行）没有任何测试** ✗，
 * 而它是 `style.css` 主题色板的**另一半双源**（第 110 轮的守卫只钉了**键集齐整** ✓，
 * 钉不了这里的**逻辑** ✓）。
 *
 * **钉五条不变量**：
 * ① **`cssThemeId` 必须正好是 `[data-theme]` 认的形状** `<theme>-<light|dark>`（`:201`）——
 *    这是 **store → CSS 的契约**：拼错了主题块就匹配不上，界面直接退回无主题 ✓；
 * ② **`applyTheme` 写 `data-theme` 与 `dark` class**（`:203-211`）—— 两处都要写，
 *    只写一个会出现"CSS 变量对了但 antd 还是亮的"这类半生效 ✓；
 * ③ **`system` 跟随系统**（`:181-186`）—— 用户选"跟随系统"时不能写死 ✓；
 * ④ **持久化往返 + 脏值清理**（`:213-223` / `:247-255`）：保存的值要能读回来，
 *    而**已下线主题 id 的脏值要被拒并清掉**（旧版本写过 linear/supabase/futuristic）✓；
 * ⑤ **`setAccent` 校验 `#RRGGBB`**（`:232-239` / `:262-265`）—— 非法值**不写入** ✓。
 */

/** 可切换的系统深色偏好 */
let systemDark = false
function installMatchMedia() {
  window.matchMedia = ((query: string) => ({
    matches: query.includes('prefers-color-scheme: dark') ? systemDark : false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

describe('stores/theme', () => {
  beforeEach(() => {
    localStorage.clear()
    systemDark = false
    installMatchMedia()
    setActivePinia(createPinia())
  })

  it('★ cssThemeId 正好是 [data-theme] 的形状，且 applyTheme 同时写 data-theme 与 dark class', () => {
    const store = useThemeStore()
    store.setTheme('paper')
    store.setMode('light')

    expect(store.cssThemeId).toBe('paper-light')
    expect(document.documentElement.getAttribute('data-theme')).toBe('paper-light')
    expect(document.documentElement.classList.contains('dark')).toBe(false)

    store.setMode('dark')
    expect(store.cssThemeId).toBe('paper-dark')
    expect(document.documentElement.getAttribute('data-theme')).toBe('paper-dark')
    expect(document.documentElement.classList.contains('dark')).toBe(true)
  })

  it('★ system 模式跟随系统深色偏好（不写死）', () => {
    const store = useThemeStore()
    store.setMode('system')

    systemDark = false
    expect(store.resolvedMode).toBe('light')
    expect(store.isDark).toBe(false)

    systemDark = true
    // 重新取一次：`matchMedia` 的 matches 是读取时求值的
    setActivePinia(createPinia())
    const store2 = useThemeStore()
    store2.setMode('system')
    expect(store2.resolvedMode).toBe('dark')
    expect(store2.isDark).toBe(true)
  })

  it('★ 持久化往返：保存的值能被 init() 读回来', () => {
    const store = useThemeStore()
    store.setTheme('terminal')
    store.setMode('dark')
    expect(localStorage.getItem('chiron-theme')).toBe('terminal')
    expect(localStorage.getItem('chiron-theme-pref')).toBe('dark')

    // 新开一个 pinia 模拟"下次启动"
    setActivePinia(createPinia())
    const fresh = useThemeStore()
    fresh.init()
    expect(fresh.activeThemeId).toBe('terminal')
    expect(fresh.preference).toBe('dark')
    expect(document.documentElement.getAttribute('data-theme')).toBe('terminal-dark')
  })

  it('★ 已下线主题的脏值：拒绝并清掉（否则每次启动都走这条分支）', () => {
    localStorage.setItem('chiron-theme', 'linear')   // 旧版本写过、现已下线

    const store = useThemeStore()
    store.init()

    expect(store.activeThemeId).toBe('notion')                       // 回退默认
    expect(localStorage.getItem('chiron-theme')).toBeNull()          // 脏值被清
  })

  it('★ setAccent 校验 #RRGGBB：非法值不写入', () => {
    const store = useThemeStore()

    store.setAccent('#1a2b3c')
    expect(store.accent).toBe('#1a2b3c')
    expect(localStorage.getItem('chiron-accent')).toBe('#1a2b3c')

    store.setAccent('red')            // 非法
    expect(store.accent).toBe('#1a2b3c')                     // 保持不变
    expect(localStorage.getItem('chiron-accent')).toBe('#1a2b3c')

    store.setAccent(null)             // 清除
    expect(store.accent).toBeNull()
    expect(localStorage.getItem('chiron-accent')).toBeNull()
  })
})
