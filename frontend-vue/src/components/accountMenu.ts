import { h, type VNode } from 'vue'
import {
  BellOutlined,
  KeyOutlined,
  LogoutOutlined,
  SettingOutlined,
  UserSwitchOutlined,
} from '@ant-design/icons-vue'

/**
 * 账号菜单（头像下拉 / 聊天侧栏底部菜单）的**唯一**定义处。
 *
 * 为什么抽出来：此前 `AppLayout.vue` 与 `chat/ChatSidePanel.vue` **各写一份**，
 * 已经漂移了 —— 聊天页那份没有 `api-keys`，也没有通知入口。而聊天页恰恰
 * **隐藏了顶栏**（`AppLayout` 的 `isChatPage`），所以聊天过程中的用户点不到
 * 任何账号入口。通知最常见的场景正是"对话时收到支付成功 / 套餐到期提醒"，
 * 缺这个入口等于 T3 没做完。
 *
 * 抽成纯数据构造函数而不是共享组件：两处的触发器与弹出方向不同（顶栏向下、
 * 侧栏向上），只有"菜单项列表"这一份内容需要统一。
 */

/** 与 ant-design-vue `Menu` 的 items 形状兼容（icon 用渲染函数，不预建 VNode）。 */
export interface AccountMenuItem {
  key: string
  label: string
  icon: () => VNode
}

export interface AccountMenuOptions {
  /**
   * 本地模式：没有账号体系（本地用户由本地后端固定下发），
   * 通知表与用户级 API Key 都无处落地，故整组不展示。
   */
  isLocal: boolean
  /**
   * 是否展示「用户级 API Key」。
   *
   * 聊天页暂不展示：那里是对话工作区，凭证管理与它的心智距离最远。
   * 参数化而不是硬编码，是为了让这个差异**显式**，而不是靠两份实现各自漂移。
   */
  includeApiKeys?: boolean
}

/**
 * 构造账号菜单项。
 *
 * `tr` 由调用方传入（组件里是 `useI18n()` 的 `t`）：直接用 i18n 全局 t 不会
 * 建立响应式依赖，切语言后菜单不会重渲染。
 */
export function buildAccountMenuItems(
  tr: (key: string) => string,
  options: AccountMenuOptions,
): AccountMenuItem[] {
  const { isLocal, includeApiKeys = true } = options

  const items: AccountMenuItem[] = [
    { key: 'settings', label: tr('settings.settings'), icon: () => h(SettingOutlined) },
    { key: 'profile', label: tr('settings.profile'), icon: () => h(UserSwitchOutlined) },
  ]

  // 账号级入口：本地模式没有账号体系，全部跳过。
  if (!isLocal) {
    items.push({
      key: 'notifications',
      label: tr('notifications.title'),
      icon: () => h(BellOutlined),
    })
    if (includeApiKeys) {
      items.push({
        key: 'api-keys',
        label: tr('settings.user_api_key_title'),
        icon: () => h(KeyOutlined),
      })
    }
  }

  // 本地模式没有账号可登出（本地用户由本地后端固定下发）。
  if (!isLocal) {
    items.push({ key: 'logout', label: tr('auth.logout'), icon: () => h(LogoutOutlined) })
  }

  return items
}

/**
 * 账号菜单的跳转目标（不需要额外动作的项）。
 *
 * `settings`（弹窗）与 `logout`（清会话）有副作用，由各组件自己处理 ——
 * 把它们也塞进来会逼这个模块知道 auth store 与弹窗状态。
 */
export function accountMenuRoute(key: string): string | null {
  switch (key) {
    case 'profile':
      return '/profile'
    case 'notifications':
      return '/notifications'
    case 'api-keys':
      return '/api-keys'
    default:
      return null
  }
}
