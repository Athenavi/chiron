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
  useAuthStore().user = { id: TEST_USER_ID, email: '', name: '', role: 'user', tenant_id: 0 }
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

describe('ChatInput（运行中补充指令 steer）', () => {
  it('只在"正在生成 + 手上有草稿"时出现', async () => {
    const wrapper = mountInput()
    // 空闲：没有草稿、也没在跑
    expect(wrapper.find('.steer-btn').exists()).toBe(false)

    await wrapper.setProps({ loading: true })
    expect(wrapper.find('.steer-btn').exists()).toBe(false)      // 在跑但没内容可注入

    await wrapper.find('textarea').setValue('先别删那个表')
    expect(wrapper.find('.steer-btn').exists()).toBe(true)       // 两条都满足才出现

    // 空闲但有草稿：此时普通发送就是正确动作，不该出现 steer
    await wrapper.setProps({ loading: false })
    expect(wrapper.find('.steer-btn').exists()).toBe(false)
  })

  it('发出 steer 且清空输入，但不打断、不写提问历史', async () => {
    const wrapper = mountInput({ loading: true })
    await wrapper.find('textarea').setValue('先别删那个表')
    await wrapper.find('.steer-btn').trigger('click')

    expect(wrapper.emitted('steer')?.[0]?.[0]).toBe('先别删那个表')
    // 不打断：stop 一次都不该发（那是另一条语义）
    expect(wrapper.emitted('stop')).toBeUndefined()
    expect(wrapper.emitted('send')).toBeUndefined()
    expect(valueOf(wrapper)).toBe('')
    // 补充指令不是"我问过的问题"，不该进 ↑ 召回历史
    expect(localStorage.getItem(HISTORY_KEY)).toBeNull()
  })

  it('setDraft 把失败的原文还回输入框（不发送、不写历史）', async () => {
    const wrapper = mountInput()
    ;(wrapper.vm as any).setDraft('没送到的那句话')
    await new Promise(resolve => setTimeout(resolve, 0))   // 等 v-model 回写 DOM
    expect(valueOf(wrapper)).toBe('没送到的那句话')
    expect(wrapper.emitted('send')).toBeUndefined()
    expect(localStorage.getItem(HISTORY_KEY)).toBeNull()
  })
})
