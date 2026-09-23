import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../../api', () => ({
  uploadFile: vi.fn(),
  listModels: vi.fn(async () => []),
}))

import ChatInput from '../ChatInput.vue'
import { useAuthStore } from '../../../stores/auth'
import { privateKey } from '../../../utils/privateStorage'

/** 输入历史按账号隔离（见 utils/privateStorage），测试里也要用带账号的键 */
const TEST_USER_ID = 'u1'
const HISTORY_KEY = privateKey('chiron:composer-history:v1', TEST_USER_ID)

function mountInput(props: Record<string, unknown> = {}) {
  return mount(ChatInput, {
    props: { loading: false, mode: 'normal', modeOptions: [], sessionId: 's1', ...props } as any,
  })
}

const valueOf = (wrapper: ReturnType<typeof mountInput>) =>
  (wrapper.find('textarea').element as HTMLTextAreaElement).value

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
  // 组件用 auth.user.id 拼历史键；不注入 user 会退化成 :anonymous，就测不到真实路径
  useAuthStore().user = { id: TEST_USER_ID, email: '', name: '', role: 'user', tenant_id: 't1' }
})

describe('ChatInput（输入区交互）', () => {
  it('↑ 召回最近发送的内容，↓ 越过最近一条后回到自己的草稿', async () => {
    const wrapper = mountInput()
    const ta = wrapper.find('textarea')

    await ta.setValue('第一个问题')
    await ta.trigger('keydown', { key: 'Enter' })
    expect(wrapper.emitted('send')?.[0]?.[0]).toBe('第一个问题')

    await ta.setValue('正在写一半的草稿')
    await ta.trigger('keydown', { key: 'ArrowUp' })
    expect(valueOf(wrapper)).toBe('第一个问题')

    await ta.trigger('keydown', { key: 'ArrowDown' })
    expect(valueOf(wrapper)).toBe('正在写一半的草稿')
  })

  it('Esc 放弃召回并恢复草稿', async () => {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(['历史内容']))
    const wrapper = mountInput()
    const ta = wrapper.find('textarea')

    await ta.setValue('我的草稿')
    await ta.trigger('keydown', { key: 'ArrowUp' })
    expect(valueOf(wrapper)).toBe('历史内容')

    await ta.trigger('keydown', { key: 'Escape' })
    expect(valueOf(wrapper)).toBe('我的草稿')
  })

  it('光标不在首行时 ↑ 不召回（留给光标移动）', async () => {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(['历史内容']))
    const wrapper = mountInput()
    const ta = wrapper.find('textarea')

    await ta.setValue('第一行\n第二行')
    await ta.trigger('keydown', { key: 'ArrowUp' })
    expect(valueOf(wrapper)).toBe('第一行\n第二行')
  })

  it('输入法组合期按 Enter 不发送（选词确认不该提交）', async () => {
    const wrapper = mountInput()
    const ta = wrapper.find('textarea')

    await ta.setValue('中文候选')
    await ta.trigger('keydown', { key: 'Enter', isComposing: true })
    expect(wrapper.emitted('send')).toBeUndefined()
  })

  it('发送后写入历史，重复内容只保留最近一条', async () => {
    const wrapper = mountInput()
    const ta = wrapper.find('textarea')

    for (const text of ['a', 'b', 'a']) {
      await ta.setValue(text)
      await ta.trigger('keydown', { key: 'Enter' })
    }
    expect(JSON.parse(localStorage.getItem(HISTORY_KEY) as string)).toEqual(['b', 'a'])
  })

  it('草稿按会话持久化并在切回时恢复', async () => {
    const wrapper = mountInput()
    const ta = wrapper.find('textarea')

    await ta.setValue('未发完的草稿')
    await new Promise(resolve => setTimeout(resolve, 350))   // 草稿写入有 300ms 防抖
    expect(localStorage.getItem('chiron:draft:s1')).toBe('未发完的草稿')

    await wrapper.setProps({ sessionId: 's2' })
    expect(valueOf(wrapper)).toBe('')

    await wrapper.setProps({ sessionId: 's1' })
    expect(valueOf(wrapper)).toBe('未发完的草稿')
  })
})
