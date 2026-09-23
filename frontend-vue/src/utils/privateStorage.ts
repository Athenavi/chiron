/**
 * 私有数据的本地存储治理。
 *
 * `localStorage` 是**浏览器级**的，与账号无关 —— 凡是"能反推出用户做过什么"的内容落在
 * localStorage，就会在同一浏览器换账号的场景下**泄漏给下一个账号**。
 * 已确认的实例：输入框历史（`↑` 召回）在换账号后召回了上一个账号发过的内容。
 *
 * 因此约定（新增私有数据时必须遵守）：
 *   1. 键必须经 :func:`privateKey` 加账号命名空间；
 *   2. 键前缀必须登记进 :data:`PRIVATE_KEY_PREFIXES`（登出时按前缀整体清理）；
 *   3. UI 偏好（主题 / 字号 / 视图模式）不属于私有数据，可不隔离。
 */

/** 私有键前缀清单 —— 登出时按前缀清理，避免"换账号后仍残留上一账号的数据"。 */
export const PRIVATE_KEY_PREFIXES: readonly string[] = [
  'chiron:composer-history', // 输入框历史（↑ 召回）
  'chat_sessions',           // 会话列表缓存
]

/** 构造带账号命名空间的私有键；无账号时退化为 `:anonymous`（登出态本就不应写入）。 */
export function privateKey(base: string, userId?: string | null): string {
  return `${base}:${userId || 'anonymous'}`
}

/**
 * 清理全部私有键（登出 / 切换账号时调用）。
 *
 * 按**前缀**扫描而不是按已知的完整键名删除：这样也能清掉历史遗留的、没有账号命名空间的
 * 数据（那正是越权的来源）。
 */
export function clearPrivateStorage(): void {
  try {
    const doomed: string[] = []
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (!key) continue
      if (PRIVATE_KEY_PREFIXES.some(prefix => key === prefix || key.startsWith(`${prefix}:`))) {
        doomed.push(key)
      }
    }
    for (const key of doomed) localStorage.removeItem(key)
  } catch {
    /* 隐私模式 / 存储被禁用：清理失败不应影响登出流程 */
  }
}
