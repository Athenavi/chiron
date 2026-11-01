import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

/**
 * 「运行模式」面板里**服务地址**那一行的网。
 *
 * 为什么要单独钉这一处：桌面壳里"改服务地址"此前只有一个隐藏菜单入口（`Ctrl+,`，
 * 而菜单栏 `autoHideMenuBar`），而 `setup.html` 的文案一直写着"填写后可以随时在设置里修改"
 * —— 承诺是假的。云端地址不可达时更死：登不了录就进不了设置页。
 * 现在这一行是承诺兑现的地方，另一个入口在登录页。
 *
 * 面板不内嵌第二套编辑器：它只负责把用户送到主进程那一份配置页（校验/探测/落盘只有一套口径），
 * 所以这里钉的是**可达性**，不是输入框行为。
 */
type FakeStore = Record<string, unknown>

const deviceStoreMock = vi.hoisted(() => ({
  isDesktop: true,
  canOpenSetup: true,
  mode: 'cloud' as 'cloud' | 'local',
  settings: { apiUrl: 'http://127.0.0.1:8080' } as null | { apiUrl: string },
  isLocal: () => deviceStoreMock.mode === 'local',
  isCloud: () => deviceStoreMock.mode === 'cloud',
  ensureReady: vi.fn(async () => {}),
  openConnectionSetup: vi.fn(async () => true),
}))

vi.mock('../../../stores/device', () => ({
  useDeviceStore: () => deviceStoreMock as unknown as FakeStore,
}))

import RunModePanel from '../RunModePanel.vue'

function mountPanel() {
  return mount(RunModePanel, {
    global: {
      stubs: {
        'a-alert': true,
        'a-modal': true,
        'a-tag': true,
        'a-checkbox': true,
        'a-input': true,
        // 不写 @click：父组件的监听器本来就会随 attrs 落到 stub 的根 <button> 上，
        // 自己再 emit 一次会变成两次调用（第一次跑就撞上了）。
        'a-button': { template: '<button type="button"><slot /></button>' },
      },
    },
  })
}

beforeEach(() => {
  deviceStoreMock.isDesktop = true
  deviceStoreMock.canOpenSetup = true
  deviceStoreMock.mode = 'cloud'
  deviceStoreMock.settings = { apiUrl: 'http://127.0.0.1:8080' }
  deviceStoreMock.openConnectionSetup.mockClear()
})

describe('RunModePanel 的服务地址行', () => {
  it('云端桌面壳：显示当前地址，点「修改…」把用户送去主进程的配置页', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.text()).toContain('服务地址')
    expect(wrapper.text()).toContain('http://127.0.0.1:8080')

    const edit = wrapper
      .findAll('button')
      .find(button => button.text().includes('修改…'))
    expect(edit, '没有「修改…」按钮：改地址的入口又只剩隐藏菜单').toBeTruthy()
    await edit!.trigger('click')
    expect(deviceStoreMock.openConnectionSetup).toHaveBeenCalledTimes(1)
  })

  it('本地模式不显示这一行：本地没有"云服务地址"可言', async () => {
    deviceStoreMock.mode = 'local'
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.text()).not.toContain('服务地址')
  })

  it('地址还没配过时显示占位，而不是一个空白值', async () => {
    deviceStoreMock.settings = null
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.text()).toContain('尚未配置')
  })

  it('老外壳没有 openSetup：按钮禁用，不给一个点了没反应的选择', async () => {
    deviceStoreMock.canOpenSetup = false
    const wrapper = mountPanel()
    await flushPromises()

    const edit = wrapper
      .findAll('button')
      .find(button => button.text().includes('修改…'))
    expect(edit, '这一行只有按钮没有意义，外壳不支持时应当明确禁用').toBeTruthy()
    expect(edit!.attributes('disabled')).toBeDefined()
    await edit!.trigger('click')
    expect(deviceStoreMock.openConnectionSetup).not.toHaveBeenCalled()
  })
})
