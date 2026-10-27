/**
 * 设备级运行模式（`local` / `cloud`）store。
 *
 * 契约（docs/desktop-local/contracts.md §1–§2）：
 *   · 模式的**真相在主进程** `userData/config.json`，经 preload 的 `window.chironDesktop`
 *     暴露（`mode` 属性 + `getDeviceSettings()` / `setMode()` / `restart()`）；
 *   · 渲染进程 `localStorage` 只做**镜像**（`chiron.device.mode`）—— 用于 preload 尚未
 *     注入完成、或主进程读取失败时的一次性兜底，不反向写回主进程；
 *   · 模式是**设备级**设置，与登录无关；切换后必须重启应用（见 §1），因此界面不能
 *     假设"切换后同会话内一切立即就位"。
 *
 * 为什么把 `currentDeviceMode()` 做成**模块级函数**（而不是只暴露 pinia store）：
 * `src/api/index.ts`（axios 响应拦截器）与 `src/router/guard.ts`（路由守卫）都要在
 * 非组件上下文判断模式，其中守卫在单测里**没有 pinia**（`router/__tests__/guard.spec.ts`
 * 只 mock 了 auth store）。让它们依赖模块级函数，既避开 "no active pinia" 崩溃，
 * 也避免 api ↔ store 的循环依赖（本文件**不**导入 `../api`）。
 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

/** 模式取值：`local` = 数据与模型都在本机；`cloud` = 连接云端/团队服务 */
export type ChironMode = 'local' | 'cloud'

/** preload 契约 `DeviceSettings`（字段顺序与含义见 contracts.md §2） */
export interface DeviceSettings {
  mode: ChironMode
  apiUrl: string
  local: { dataDir: string; lastStartedAt: string }
  ui: { locale: string; theme: string }
  migration: { legacyCloudDetected: boolean; modeChosenAt: string }
}

/**
 * `window.chironDesktop` 的**可选**视图。
 *
 * 全部字段可选：W1（Electron 主进程）与我们并行开发，preload 可能还没暴露模式相关
 * 成员。这里的每个调用点都必须能在"外壳不支持"时优雅降级 —— 不能因为桌面壳版本旧
 * 就把 Web 端也能用的界面打挂。
 */
export interface ChironDesktopHost {
  isDesktop?: boolean
  platform?: string
  apiUrl?: string
  mode?: ChironMode
  getDeviceSettings?: () => Promise<DeviceSettings>
  setMode?: (mode: ChironMode, opts?: { deleteLocalData?: boolean }) => Promise<void>
  restart?: () => Promise<void>
}

/** localStorage 镜像键（真相在主进程 config.json，这里只镜像） */
export const DEVICE_MODE_STORAGE_KEY = 'chiron.device.mode'

export function isChironMode(value: unknown): value is ChironMode {
  return value === 'local' || value === 'cloud'
}

/** 桌面外壳对象；Web 端（含 jsdom 测试环境）为 undefined */
export function desktopHost(): ChironDesktopHost | undefined {
  return (globalThis as { chironDesktop?: ChironDesktopHost }).chironDesktop
}

export function isDesktopHost(): boolean {
  return Boolean(desktopHost()?.isDesktop)
}

/** 读镜像；损坏或非法值一律当作"没有镜像"（不抛错，存储可能被禁用） */
export function readMirroredMode(): ChironMode | null {
  try {
    const raw = globalThis.localStorage?.getItem(DEVICE_MODE_STORAGE_KEY) ?? null
    return isChironMode(raw) ? raw : null
  } catch {
    return null
  }
}

function writeMirroredMode(mode: ChironMode): void {
  try {
    globalThis.localStorage?.setItem(DEVICE_MODE_STORAGE_KEY, mode)
  } catch {
    /* 存储不可用（隐私模式）时只保留内存态；模式真相本来就在主进程 */
  }
}

/** `setMode` 成功后的模块级缓存：重启前的窗口期内，各模块读到的是**新**模式 */
let activeMode: ChironMode | null = null

/** 仅供 store / 单测使用：显式设置（或 `null` 清除）模块级模式缓存 */
export function setActiveMode(mode: ChironMode | null): void {
  activeMode = mode
}

/**
 * 解析当前生效模式（同步，无副作用）：
 *   1. `setMode` 后的模块级缓存；
 *   2. Web 端没有桌面外壳 ⇒ 一律 `cloud`（本地模式是桌面独有能力，Web 构建不含本地后端）；
 *   3. preload 暴露的 `mode`（真相）；
 *   4. localStorage 镜像；
 *   5. 兼容降级：外壳尚无 `mode` 字段时，已配置服务地址按 `cloud`（保持既有行为），
 *      全新安装（无地址）按契约默认 `local`。
 */
export function resolveDeviceMode(): ChironMode {
  if (activeMode) return activeMode
  if (!isDesktopHost()) return 'cloud'
  const host = desktopHost()
  // 显式先判 host：可选链上的类型谓词无法把 host 本身缩窄成非空
  if (host && isChironMode(host.mode)) return host.mode
  const mirrored = readMirroredMode()
  if (mirrored) return mirrored
  return host?.apiUrl ? 'cloud' : 'local'
}

/** 非组件上下文的模式读取入口（axios 拦截器 / 路由守卫） */
export function currentDeviceMode(): ChironMode {
  return resolveDeviceMode()
}

export const useDeviceStore = defineStore('device', () => {
  /** 当前模式（响应式；切换成功后由 `setMode` 更新） */
  const mode = ref<ChironMode>(resolveDeviceMode())
  /** preload 下发的设备设置快照（拿不到时为 null） */
  const settings = ref<DeviceSettings | null>(null)
  const loading = ref(false)
  const switching = ref(false)

  /** 是否运行在桌面外壳里（Web 端无法切换模式） */
  const isDesktop = computed(() => isDesktopHost())

  /** 本地模式：不走登录、不连云端接口 */
  function isLocal(): boolean {
    return mode.value === 'local'
  }

  /** 云端模式：保持既有登录 + cookie/Bearer 行为 */
  function isCloud(): boolean {
    return mode.value === 'cloud'
  }

  /** 应用一个模式：同步 store、模块级缓存与 localStorage 镜像 */
  function applyMode(next: ChironMode): void {
    mode.value = next
    setActiveMode(next)
    writeMirroredMode(next)
  }

  /**
   * 从 preload 拉一次设备设置（幂等；失败不抛错）。
   * 首次挂载/进入设置页时调用：主进程的 `config.json` 才是真相，镜像只是兜底。
   */
  async function loadSettings(): Promise<void> {
    const host = desktopHost()
    // 旧外壳没有 getDeviceSettings：保持解析出的推断值
    if (!host || !host.getDeviceSettings) return
    loading.value = true
    try {
      const next = await host.getDeviceSettings()
      if (!next || typeof next !== 'object') return
      settings.value = next
      if (isChironMode(next.mode)) applyMode(next.mode)
    } catch {
      /* 主进程不可读时保留推断值：模式判断不能拖垮启动 */
    } finally {
      loading.value = false
    }
  }

  let ready: Promise<void> | null = null

  /** 启动时同步一次设备设置（多次调用共享同一次读取） */
  async function ensureReady(): Promise<void> {
    if (!ready) ready = loadSettings()
    await ready
  }

  /** 强制重新读取（设置页可见时刷新用） */
  async function refresh(): Promise<void> {
    ready = loadSettings()
    await ready
  }

  /**
   * 切换模式。
   *
   * 只把"要不要同时删除本地数据"这一个选择透传给主进程 —— 清理哪些内容（cookie /
   * localStorage / IndexedDB / CacheStorage / Service Worker / HTTP 缓存 / token）
   * 由主进程按契约执行，渲染进程**不**自行删除用户数据（误删 `userData/local/**`
   * 是不可逆的，见 contracts.md §8）。
   *
   * 抛错语义：外壳不支持、主进程拒绝时直接抛给调用方（界面据此提示），
   * 不静默吞掉 —— 否则用户以为切了，重启后又回到原模式。
   */
  async function setMode(next: ChironMode, opts: { deleteLocalData?: boolean } = {}): Promise<void> {
    if (!isChironMode(next)) throw new Error(`invalid device mode: ${String(next)}`)
    const host = desktopHost()
    if (!host || !host.setMode) throw new Error('desktop shell does not expose setMode')
    switching.value = true
    try {
      await host.setMode(next, { deleteLocalData: opts.deleteLocalData === true })
      applyMode(next)
      // 主进程已写入 config.json；把本地快照也对齐，避免设置页显示旧值
      if (settings.value) settings.value = { ...settings.value, mode: next }
    } finally {
      switching.value = false
    }
  }

  /**
   * 重启应用（契约：切换后必须重启，避免两个后端并存）。
   * 若主进程的 `setMode` 自己已经 `app.relaunch()`，这里多半来不及执行 —— 无副作用。
   */
  async function restart(): Promise<void> {
    const host = desktopHost()
    if (!host || !host.restart) return
    await host.restart()
  }

  // 首启把解析值写进镜像：下次 preload 读失败时仍有兜底依据
  writeMirroredMode(mode.value)

  return {
    mode,
    settings,
    loading,
    switching,
    isDesktop,
    isLocal,
    isCloud,
    ensureReady,
    refresh,
    setMode,
    restart,
  }
})
