import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import {
  DEVICE_MODE_STORAGE_KEY,
  currentDeviceMode,
  setActiveMode,
  useDeviceStore,
} from '../device'

/**
 * 设备级运行模式 store 的契约测试。
 *
 * 重点钉住三件容易回归的事：
 *   1) Web 端（无 `window.chironDesktop`）**永远**是 cloud —— 本地模式是桌面独有能力，
 *      一旦这里退化成 local，Web 端会开始跳过登录守卫（安全回归）；
 *   2) preload 暴露的 `mode` 是真相，本地镜像只是兜底；
 *   3) 外壳不支持 `setMode` 时**必须抛错**，不能假装切换成功（重启后会悄悄回到原模式）。
 */
type FakeHost = Record<string, unknown>

function installHost(host?: FakeHost) {
  if (host) (globalThis as { chironDesktop?: FakeHost }).chironDesktop = host
  else delete (globalThis as { chironDesktop?: FakeHost }).chironDesktop
}

describe('device store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    setActiveMode(null)
    localStorage.clear()
    installHost(undefined)
  })

  it('Web 端恒为 cloud，且不可切换', () => {
    expect(currentDeviceMode()).toBe('cloud')
    const store = useDeviceStore()
    expect(store.mode).toBe('cloud')
    expect(store.isCloud()).toBe(true)
    expect(store.isLocal()).toBe(false)
    expect(store.isDesktop).toBe(false)
  })

  it('preload 的 mode 是真相，并写入 localStorage 镜像', async () => {
    installHost({ isDesktop: true, apiUrl: 'http://127.0.0.1:8080', mode: 'local' })
    const store = useDeviceStore()
    expect(store.mode).toBe('local')
    expect(store.isLocal()).toBe(true)
    expect(store.isDesktop).toBe(true)
    await store.ensureReady()
    expect(localStorage.getItem(DEVICE_MODE_STORAGE_KEY)).toBe('local')
  })

  it('ensureReady 读取 getDeviceSettings 并采用其中的 mode', async () => {
    const getDeviceSettings = vi.fn().mockResolvedValue({
      mode: 'cloud',
      apiUrl: 'https://cloud.example.test',
      local: { dataDir: '', lastStartedAt: '' },
      ui: { locale: 'zh-CN', theme: 'system' },
      migration: { legacyCloudDetected: true, modeChosenAt: '' },
    })
    installHost({ isDesktop: true, mode: 'local', getDeviceSettings })
    const store = useDeviceStore()
    expect(store.mode).toBe('local')
    await store.ensureReady()
    expect(getDeviceSettings).toHaveBeenCalled()
    expect(store.mode).toBe('cloud')
    expect(currentDeviceMode()).toBe('cloud')
    expect(localStorage.getItem(DEVICE_MODE_STORAGE_KEY)).toBe('cloud')
  })

  it('setMode 委托 preload（含 deleteLocalData）并同步模式与镜像', async () => {
    const setMode = vi.fn().mockResolvedValue(undefined)
    const restart = vi.fn().mockResolvedValue(undefined)
    installHost({ isDesktop: true, mode: 'local', setMode, restart })
    const store = useDeviceStore()
    await store.setMode('cloud', { deleteLocalData: true })
    expect(setMode).toHaveBeenCalledWith('cloud', { deleteLocalData: true })
    expect(store.mode).toBe('cloud')
    expect(currentDeviceMode()).toBe('cloud')
    expect(localStorage.getItem(DEVICE_MODE_STORAGE_KEY)).toBe('cloud')
    await store.restart()
    expect(restart).toHaveBeenCalled()
  })

  it('默认不删除本地数据：未传 deleteLocalData 时按 false 透传', async () => {
    const setMode = vi.fn().mockResolvedValue(undefined)
    installHost({ isDesktop: true, mode: 'cloud', setMode })
    const store = useDeviceStore()
    await store.setMode('local')
    expect(setMode).toHaveBeenCalledWith('local', { deleteLocalData: false })
  })

  it('外壳不支持 setMode 时抛错，且不改变当前模式', async () => {
    installHost({ isDesktop: true, mode: 'cloud' })
    const store = useDeviceStore()
    await expect(store.setMode('local')).rejects.toThrow()
    expect(store.mode).toBe('cloud')
  })

  it('旧外壳（无 mode 字段）：有服务地址按 cloud，全新安装按 local', () => {
    installHost({ isDesktop: true, apiUrl: 'http://example.test' })
    expect(currentDeviceMode()).toBe('cloud')
    installHost({ isDesktop: true, apiUrl: '' })
    expect(currentDeviceMode()).toBe('local')
  })
})
