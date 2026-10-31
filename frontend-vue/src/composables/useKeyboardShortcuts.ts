/**
 * 快捷键注册表：把散落在各处的 `if (e.key === ...)` 收敛成可查、可改、可测的声明。
 *
 * 边界：只管"按下什么键 → 调哪个 handler"，不碰业务状态，也不渲染任何 UI。
 * 文案由调用方经 i18n 提供，本模块不 import vue-i18n。
 *
 * 三条约定，都是被真实问题逼出来的：
 *
 *  1) **逐层消费**。同一个键可以登记多个绑定（Esc 就是典型：停生成 > 关浮窗 > 关侧栏）。
 *     按声明顺序匹配，`when()` 返回 false 的跳过，第一个命中的消费掉并阻止默认行为。
 *     "逐层关闭"的语义因此从 if 链搬到了**声明顺序**上 —— 顺序即优先级，改顺序就是改优先级。
 *
 *  2) **精确匹配**。`mod+k` 要求 shift/alt 都未按下，否则 `mod+shift+k` 会误触发它。
 *     修饰键一律按"必须完全相等"比较，不做子集判断。
 *
 *  3) **改键可回退**。用户改过的组合落在 localStorage（经 :func:`privateKey` 加账号命名空间），
 *     没改过的继续跟随 `defaultKeys` —— 这样将来调整默认键，没动过手的用户能自动获得新键。
 */
import { onMounted, onUnmounted, ref, type Ref } from 'vue'

import { privateKey } from '../utils/privateStorage'

/** 一条快捷键的静态定义（用于展示与改键，不含 handler）。 */
export interface ShortcutDef {
  /** 稳定 id，也是持久化的键名；改名等于让用户的自定义失效 */
  id: string
  /** 默认组合，如 `mod+k`、`escape` */
  defaultKeys: string
  /** 是否允许用户改键（默认 true） */
  customizable?: boolean
}

/** 运行期绑定：定义 + 处理函数。 */
export interface ShortcutBinding extends ShortcutDef {
  handler: (e: KeyboardEvent) => void
  /**
   * 附加条件；返回 false 时跳过本条绑定。
   * 注意它与声明顺序是**两套**机制：`when` 表达"此刻是否适用"，顺序表达"谁优先"。
   */
  when?: () => boolean
  /**
   * 命中后是否阻止默认行为，默认 true。
   *
   * 个别绑定（如"Esc 关侧栏"）在原实现里就不拦 —— 那时浮层已经关了，没有需要
   * 抢占的默认行为，拦住反而可能干扰浏览器/输入法自己的取消语义。
   */
  preventDefault?: boolean
}

/** 解析后的组合键。`mod` 已按平台展开成 ctrl / meta。 */
interface ParsedCombo {
  key: string
  ctrl: boolean
  meta: boolean
  shift: boolean
  alt: boolean
}

/** 浏览器在 `e.key` 里对这些键的写法与直觉不同，统一成组合键里能写的名字。 */
const KEY_ALIASES: Record<string, string> = {
  ' ': 'space',
  arrowup: 'up',
  arrowdown: 'down',
  arrowleft: 'left',
  arrowright: 'right',
  esc: 'escape',
  del: 'delete',
}

function normalizeKey(raw: string): string {
  const lower = raw.toLowerCase()
  return KEY_ALIASES[lower] ?? lower
}

/**
 * 把 `mod+shift+k` 解析成各修饰键的布尔值。
 *
 * `mod` 是唯一需要按平台解释的修饰键：macOS 用 ⌘，其它平台用 Ctrl。
 * 其余修饰键（ctrl / meta / shift / alt）都按字面意思，便于在个别场景写死某一侧。
 */
export function parseCombo(combo: string, isMac: boolean): ParsedCombo {
  const parts = combo
    .toLowerCase()
    .split('+')
    .map(s => s.trim())
    .filter(Boolean)
  const key = normalizeKey(parts[parts.length - 1] ?? '')
  const has = (m: string) => parts.slice(0, -1).includes(m)
  return {
    key,
    ctrl: has('ctrl') || (has('mod') && !isMac),
    meta: has('meta') || (has('mod') && isMac),
    shift: has('shift'),
    alt: has('alt'),
  }
}

/** 事件是否精确命中该组合键。 */
export function matchesCombo(e: KeyboardEvent, combo: ParsedCombo): boolean {
  return (
    normalizeKey(e.key) === combo.key &&
    e.ctrlKey === combo.ctrl &&
    e.metaKey === combo.meta &&
    e.shiftKey === combo.shift &&
    e.altKey === combo.alt
  )
}

/**
 * 把事件转成组合键字符串（用于"按下即录制"的改键交互）。
 *
 * 只按下修饰键时返回空串 —— 那种时刻用户还没按完，不该被记成一个组合。
 */
export function eventToCombo(e: KeyboardEvent, isMac: boolean): string {
  const key = normalizeKey(e.key)
  if (['control', 'meta', 'shift', 'alt'].includes(key)) return ''
  const parts: string[] = []
  // mod 优先：mac 上 ⌘ / 其它平台 Ctrl 都记成 mod，跨平台改键才有意义
  const isMod = isMac ? e.metaKey : e.ctrlKey
  if (isMod) parts.push('mod')
  if (!isMac && e.metaKey) parts.push('meta')
  if (isMac && e.ctrlKey) parts.push('ctrl')
  if (e.altKey) parts.push('alt')
  if (e.shiftKey) parts.push('shift')
  parts.push(key)
  return parts.join('+')
}

/** 人类可读的按键标签（用于设置页与速查表）。 */
export function comboLabel(combo: string, isMac: boolean): string {
  return combo
    .split('+')
    .map(p => {
      const t = p.trim().toLowerCase()
      if (t === 'mod') return isMac ? '⌘' : 'Ctrl'
      if (t === 'ctrl') return isMac ? '⌃' : 'Ctrl'
      if (t === 'meta') return isMac ? '⌘' : 'Meta'
      if (t === 'alt') return isMac ? '⌥' : 'Alt'
      if (t === 'shift') return isMac ? '⇧' : 'Shift'
      if (t === 'escape') return 'Esc'
      if (t === 'space') return 'Space'
      return t.toUpperCase()
    })
    .join(isMac ? '' : '+')
}

export interface UseKeyboardShortcutsOptions {
  bindings: ShortcutBinding[]
  /** 持久化基名（会经 privateKey 加账号命名空间），默认 `chiron:shortcuts` */
  storageBase?: string
  /** 当前账号 id，用于账号隔离 */
  userId?: string | null
}

export interface KeyboardShortcutsApi {
  /** 生效中的组合键（用户改过的优先） */
  keysOf: (id: string) => string
  /** 改键；与其它绑定冲突时返回冲突方的 id，且**不写入** */
  setKeys: (id: string, keys: string) => { ok: true } | { ok: false; conflictWith: string }
  /** 恢复单条默认 */
  resetKeys: (id: string) => void
  /** 恢复全部默认 */
  resetAll: () => void
  /** 查某组合键当前被谁占用（改键时做实时提示） */
  conflictOf: (id: string, keys: string) => string | null
  /** 供设置页/速查表渲染的只读快照 */
  definitions: Ref<ShortcutDef[]>
}

/**
 * 覆盖值的**模块级共享**仓库：`storageKey → 响应式覆盖表`。
 *
 * 这是必需的，不是优化：ChatView 与设置页是两个组件实例，各自 `useKeyboardShortcuts()`
 * 会拿到各自的 ref。用户在设置页改键、写进 localStorage，ChatView 持有的那份 ref 却不会变，
 * 于是"设置页显示已改、实际还是旧键"。共享同一份 ref 后，改键立刻反映到真正分发按键的那一侧。
 */
const overrideStores = new Map<string, Ref<Record<string, string>>>()

function overrideStoreOf(storageKey: string): Ref<Record<string, string>> {
  const existing = overrideStores.get(storageKey)
  if (existing) {
    /*
     * 缓存要与 localStorage 对齐。
     *
     * 共享缓存解决的是"设置页改键、ChatView 立刻生效"，但如果持久值被外部清掉
     * （登出时 clearPrivateStorage 按前缀删除、或在另一个窗口清空），内存里那份
     * 旧覆盖值就会继续生效 —— 表现为"换了个账号，键位却还是上一个人的"。
     * 磁盘上没有这条记录了，就以磁盘为准。
     */
    if (readStored(storageKey) === null && Object.keys(existing.value).length > 0) {
      existing.value = {}
    }
    return existing
  }

  const store = ref<Record<string, string>>(readStored(storageKey) ?? {})
  overrideStores.set(storageKey, store)
  return store
}

/** 读出磁盘上的覆盖表；无记录 / 脏数据 / 存储不可用时返回 null（调用方按"没改过"处理）。 */
function readStored(storageKey: string): Record<string, string> | null {
  try {
    const raw = localStorage.getItem(storageKey)
    if (!raw) return null
    const parsed = JSON.parse(raw)
    // 脏数据（被手改、版本不符）当作没改过，继续用默认键工作
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, string>) : null
  } catch {
    /* 隐私模式 / 存储被禁用 / JSON 损坏：用默认键 */
    return null
  }
}

/**
 * 改键状态（不改监听）。
 *
 * 设置页只需要读写组合键，不该顺手再注册一个 keydown —— 那会让快捷键在设置页里
 * 也去触发会话页的 handler。真正分发按键的只有 `useKeyboardShortcuts`。
 */
export function useShortcutPreferences(opts: {
  specs: readonly ShortcutDef[]
  storageBase?: string
  userId?: string | null
}): KeyboardShortcutsApi {
  const { specs, storageBase = 'chiron:shortcuts', userId = null } = opts
  const store = overrideStoreOf(privateKey(storageBase, userId))

  function persist() {
    try {
      localStorage.setItem(privateKey(storageBase, userId), JSON.stringify(store.value))
    } catch {
      /* 配额满 / 存储被禁用：本次会话内仍生效，只是下次打开回到默认 */
    }
  }

  function keysOf(id: string): string {
    const override = store.value[id]
    if (override) return override
    return specs.find(s => s.id === id)?.defaultKeys ?? ''
  }

  function conflictOf(id: string, keys: string): string | null {
    const normalized = keys.toLowerCase()
    for (const s of specs) {
      if (s.id === id) continue
      if (keysOf(s.id).toLowerCase() === normalized) return s.id
    }
    return null
  }

  function setKeys(id: string, keys: string): { ok: true } | { ok: false; conflictWith: string } {
    const conflict = conflictOf(id, keys)
    if (conflict) return { ok: false, conflictWith: conflict }
    store.value = { ...store.value, [id]: keys }
    persist()
    return { ok: true }
  }

  function resetKeys(id: string) {
    const next = { ...store.value }
    delete next[id]
    store.value = next
    persist()
  }

  function resetAll() {
    store.value = {}
    persist()
  }

  const definitions = ref<ShortcutDef[]>(
    specs.map(s => ({ id: s.id, defaultKeys: s.defaultKeys, customizable: s.customizable })),
  )

  return { keysOf, setKeys, resetKeys, resetAll, conflictOf, definitions }
}

export function useKeyboardShortcuts(opts: UseKeyboardShortcutsOptions): KeyboardShortcutsApi {
  const { bindings, storageBase = 'chiron:shortcuts', userId = null } = opts
  const isMac = typeof navigator !== 'undefined' && /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent)
  const api = useShortcutPreferences({ specs: bindings, storageBase, userId })

  /**
   * 焦点是否在可编辑区域。
   *
   * 用于挡住"纯字符键"的全局绑定：`shift+?` 是打开速查表，但用户也可能只是在输入框里
   * 打一个问号。带 Ctrl/Cmd/Alt 的组合不受影响（那些在输入框里本来也没有输入语义），
   * Esc 这类非字符键（`e.key.length > 1`）同样照常生效 —— 否则输入框里按 Esc 就关不掉浮层。
   */
  function isTypingTarget(e: KeyboardEvent): boolean {
    const target = e.target as HTMLElement | null
    if (!target) return false
    const tag = target.tagName
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable
  }

  /**
   * 全局 keydown。
   *
   * 不用 capture：让输入框、弹窗等先按自己的语义处理，避免在输入法组合过程中被抢走。
   * 顺序即优先级 —— 见文件头约定 1。
   */
  function onKeydown(e: KeyboardEvent) {
    // 输入法组合期间不响应：此时 e.key 是中间态，按下去会被当成真实按键
    if (e.isComposing) return
    // 输入框里打字符时，不要用纯字符快捷键抢走这次输入
    if (isTypingTarget(e) && !e.ctrlKey && !e.metaKey && !e.altKey && e.key.length === 1) return

    for (const b of bindings) {
      if (b.when && !b.when()) continue
      const combo = parseCombo(api.keysOf(b.id), isMac)
      if (!matchesCombo(e, combo)) continue
      if (b.preventDefault !== false) e.preventDefault()
      b.handler(e)
      // 第一个命中的消费掉：这正是 Esc"逐层关闭"的实现方式
      return
    }
  }

  onMounted(() => window.addEventListener('keydown', onKeydown))
  onUnmounted(() => window.removeEventListener('keydown', onKeydown))

  return api
}
