import { api } from '../api'
import { hasExplicitLocale, setLocale, setLocaleSyncHook } from '../i18n'
import { currentDeviceMode } from '../stores/device'

/**
 * 把界面语言同步到 `users.settings.locale`（P2-3 / T1）。
 *
 * 为什么需要它：站内通知由前端按当前语言渲染，但**邮件是服务端拼的**，只能读
 * `users.settings.locale` 决定语言。此前语言只存 `localStorage['chiron.locale']`，
 * 服务端永远不知道用户选了什么 —— 于是非中文用户收到的邮件正文仍是中文。
 *
 * 为什么不用 `i18n/index.ts` 直接调 api：`api/index.ts` 已 import 了 i18n 的 `t`
 * （拦截器翻译错误消息），反向依赖会形成循环，ESM 下拿到未初始化绑定。
 * 所以 i18n 只暴露一个 hook，由本模块在 `main.ts` 里注册。
 *
 * 为什么不 import `stores/auth`：auth store 会在 `fetchProfile` 之后反过来调用
 * 本模块的 `applyServerLocale`，互相 import 同样成环。登录态判定改由调用方注入。
 */

/** 登录态判定（由 main.ts 注入，避免本模块依赖 pinia store）。 */
type AuthProbe = () => boolean
let authProbe: AuthProbe = () => false

/**
 * 安装语言同步（在 `main.ts` 里、`app.use(pinia)` 之后调用一次）。
 *
 * probe 应返回"当前是否有已登录用户"：匿名用户没有 users 行，写了也没意义。
 */
export function installLocaleSync(probe: AuthProbe): void {
  authProbe = probe
  setLocaleSyncHook((code) => {
    // 不 await：切换语言是同步操作，网络写入失败不该阻塞或回滚界面。
    void persistLocale(code)
  })
}

/**
 * 写入服务端语言偏好。**失败一律静默**。
 *
 * 语言在本地已经生效，写服务端失败只意味着"以后的邮件语言可能不对"，
 * 对用户当下没有任何可操作的动作，弹错只会造成噪音。
 */
export async function persistLocale(code: string): Promise<void> {
  // 本地模式没有账号体系（也没有 /v1/auth/profile），偏好无处可写。
  if (currentDeviceMode() === 'local') return
  if (!authProbe()) return

  try {
    // 后端 UpdateProfile 对 settings 做 `||` 局部合并，所以只传 locale
    // 不会冲掉 tts / theme 等其它设置。
    await api.put('/v1/auth/profile', { settings: { locale: code } })
  } catch {
    /* 见上：静默 */
  }
}

/**
 * 登录后按服务端偏好对齐界面语言。
 *
 * 只在**本机没有显式选择过**语言时生效：否则用户在登录页刚选的语言会被
 * 服务端旧值立刻覆盖，表现为"选择没生效"。
 *
 * `persist: false`：这个方向是"服务端 → 本地"，再回写会让两个方向互相触发。
 */
export function applyServerLocale(serverSettings: Record<string, unknown> | undefined): void {
  if (!serverSettings) return
  const locale = serverSettings.locale
  if (typeof locale !== 'string' || !locale) return
  if (hasExplicitLocale()) return
  setLocale(locale, { persist: false })
}
