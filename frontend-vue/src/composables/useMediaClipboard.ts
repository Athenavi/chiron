/**
 * 媒体页剪贴板的**响应式封装**（sessionStorage + 账号隔离）。
 *
 * 纯逻辑在 `clipboard.ts`（可穷举测试），这里只做三件事：
 * 读写 `sessionStorage`、把结果放进 `ref`、在账号切换时换键。
 *
 * 为什么用 `sessionStorage` 而不是内存变量：刷新页面后剪贴板仍在，与资源管理器一致 ——
 * 用户复制后切去别的标签页再回来，不该发现剪贴板空了。
 *
 * 为什么键里带 tenant + user：同一浏览器换账号时，前一个账号的剪贴板不该被后一个看到。
 * 键不同 ⇒ 读不到旧数据，隔离是**结构性**的，不依赖登出清理是否被调用。
 * （注意：`clearPrivateStorage` 只扫 `localStorage`，不覆盖这里 —— 见下方 `clear()` 的说明。）
 */

import { computed, ref } from 'vue'

import {
  clipboardStorageKey,
  EMPTY_CLIPBOARD,
  parseClipboard,
  pruneClipboard,
  serializeClipboard,
  type ClipboardPayload,
} from '../views/media/clipboard'

/** 当前账号的剪贴板键。账号变化时由 `syncKey` 重新计算。 */
function makeKey(tenantId: string, userId: string): string {
  return clipboardStorageKey(tenantId, userId)
}

export function useMediaClipboard() {
  const payload = ref<ClipboardPayload>(EMPTY_CLIPBOARD)
  let currentKey = ''

  /** 读取某个账号空间下的剪贴板（切换账号 / 首次挂载时调用）。 */
  function syncKey(tenantId: string, userId: string): void {
    const key = makeKey(tenantId, userId)
    if (key === currentKey) return
    currentKey = key
    try {
      payload.value = parseClipboard(sessionStorage.getItem(key))
    } catch {
      // 隐私模式 / 存储被禁用：退化为内存剪贴板，而不是让整个页面报错
      payload.value = EMPTY_CLIPBOARD
    }
  }

  /** 写入剪贴板（复制 / 剪切时调用）。 */
  function write(next: ClipboardPayload): void {
    payload.value = next
    if (!currentKey) return
    try {
      if (next.ids.length === 0) sessionStorage.removeItem(currentKey)
      else sessionStorage.setItem(currentKey, serializeClipboard(next))
    } catch {
      // 存不进去不影响本次会话：内存里那份仍然可用
    }
  }

  /**
   * 清空剪贴板。
   *
   * 登出不调用它也不会泄漏：键里有 userId，下个账号读到的是另一个键。
   * 但**同一账号**登出再登入时，旧剪贴板会回来 —— 这与资源管理器"剪贴板跨重启仍在"
   * 的行为一致，不是缺陷。真正需要清空的场景是"剪切后粘贴成功"，由调用方显式清。
   */
  function clear(): void {
    write(EMPTY_CLIPBOARD)
  }

  /** 粘贴前剔除已删除的源项（返回剔除后的载荷，供调用方直接使用）。 */
  function prune(aliveIds: ReadonlySet<string>): ClipboardPayload {
    const pruned = pruneClipboard(payload.value, aliveIds)
    if (pruned !== payload.value) write(pruned)
    return pruned
  }

  /** 剪贴板是否有内容（决定"粘贴"是否可用）。 */
  const hasContent = computed(() => payload.value.ids.length > 0)

  /** 是否处于"剪切"态（UI 可据此在源项上显示虚化/半透明）。 */
  const isCutting = computed(() => payload.value.mode === 'cut' && payload.value.ids.length > 0)

  return { payload, hasContent, isCutting, syncKey, write, clear, prune }
}
