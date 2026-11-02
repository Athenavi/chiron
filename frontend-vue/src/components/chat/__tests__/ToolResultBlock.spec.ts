import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

/** 分段读取必须打桩：真实请求在单测里不存在，而这条路径的错正是没有测试才活到现在 */
const mocked = vi.hoisted(() => ({ getToolCallOutput: vi.fn() }))
// 组件内是 `from '../../api'`（相对 components/chat/）；测试在 components/chat/__tests__/，
// 同一个模块要多退一层，写少一层 mock 就静默不生效 —— 调用会走真实 axios 并在 catch 里消失。
vi.mock('../../../api', () => ({ getToolCallOutput: mocked.getToolCallOutput }))

import ToolResultBlock from '../ToolResultBlock.vue'
import type { ToolResultItem } from '../chat-types'

const item = (content: string, isError = false): ToolResultItem =>
  ({ kind: 'tool_result', toolCallId: 'c1', content, isError })

/** 与 edit_file 的 difflib 输出形态一致（python-engine/app/tools/edit_file.py） */
const DIFF = [
  '--- a/app/main.py',
  '+++ b/app/main.py',
  '@@ -1,2 +1,2 @@',
  ' context',
  '-old',
  '+new',
  '',
].join('\n')

describe('ToolResultBlock（工具结果富渲染）', () => {
  it('edit_file 的 diff 结果按 diff 渲染，并给出增删统计与文件路径', () => {
    const wrapper = mount(ToolResultBlock, {
      props: { item: item(JSON.stringify({ path: '/w/app/main.py', success: true, diff: DIFF })) },
    })
    expect(wrapper.findAll('.diff-line').length).toBe(6)
    expect(wrapper.find('.diff-add').text()).toBe('+1')
    expect(wrapper.find('.diff-del').text()).toBe('−1')
    expect(wrapper.text()).toContain('app/main.py')
    expect(wrapper.findAll('.diff-line.add').length).toBe(1)
    expect(wrapper.findAll('.diff-line.del').length).toBe(1)
    expect(wrapper.findAll('.diff-line.hunk').length).toBe(1)
  })

  it('纯文本里携带的 patch 同样按 diff 渲染', () => {
    const wrapper = mount(ToolResultBlock, { props: { item: item(DIFF) } })
    expect(wrapper.findAll('.diff-line').length).toBe(6)
  })

  it('超长 diff 先给预览，展开后显示全部', async () => {
    const big = ['--- a/x.ts', '+++ b/x.ts', '@@ -1 +1 @@']
      .concat(Array.from({ length: 100 }, (_, i) => `+line${i}`))
      .join('\n')
    const wrapper = mount(ToolResultBlock, { props: { item: item(big) } })
    expect(wrapper.findAll('.diff-line').length).toBe(60)
    expect(wrapper.find('.diff-more').text()).toContain('还有 43 行')
    await wrapper.find('.diff-more').trigger('click')
    expect(wrapper.findAll('.diff-line').length).toBe(103)
  })

  it('失败结果显式标注错误并默认展开内容', () => {
    const wrapper = mount(ToolResultBlock, { props: { item: item('{"error":"boom"}', true) } })
    expect(wrapper.find('.result-error-bar').exists()).toBe(true)
    expect(wrapper.find('.result-code').exists()).toBe(true)
    expect(wrapper.text()).toContain('结果（失败）')
  })

  it('成功结果的文本默认收起', () => {
    const wrapper = mount(ToolResultBlock, { props: { item: item('{"a":1}') } })
    expect(wrapper.find('.result-error-bar').exists()).toBe(false)
    expect(wrapper.find('.result-code').exists()).toBe(false)
  })

  it('终端输出显示退出码与行数，非零退出码标红', () => {
    const wrapper = mount(ToolResultBlock, {
      props: { item: item(JSON.stringify({ stdout: 'a\nb\n', stderr: '', exit_code: 1 })) },
    })
    expect(wrapper.text()).toContain('exit 1')
    expect(wrapper.find('.terminal-exit').classes()).toContain('nonzero')
    expect(wrapper.find('.terminal-lines').text()).toBe('3 行')
  })
})

/**
 * 大 output 的分段读取。
 *
 * 这一组针对的是一个真实缺陷：offset 的口径。服务端 `substring(... for n)` 数的是
 * **Unicode 码点**，而 JavaScript 的 `string.length` 数 **UTF-16 码元** ——
 * 遇到 emoji 这类增补平面字符时，两者相差一倍。前端曾用 `已收内容的 .length`
 * 当下一段起点，于是每跨过一个 emoji 就**静默跳过**若干字符。
 *
 * 修法是把「下一段从哪开始」交给服务端：它才知道自己切到哪了。这组断言锁定的
 * 就是「前端不再自行推算 offset」这条契约。
 */
describe('ToolResultBlock（大 output 分段读取）', () => {
  beforeEach(() => {
    mocked.getToolCallOutput.mockReset()
  })

  /** 已被列表接口截短的预览：`outputTruncated` 为真才允许展开 */
  const truncated = (content: string): ToolResultItem => ({
    kind: 'tool_result',
    toolCallId: 'c1',
    content,
    isError: false,
    outputTruncated: true,
    outputBytes: 20_000,
  })

  /** 展开后点「加载完整结果」；已展开则直接点按钮（再点 head 会折叠面板） */
  async function clickLoad(wrapper: ReturnType<typeof mount>) {
    if (!wrapper.find('.result-load-btn').exists()) {
      await wrapper.find('.result-head').trigger('click')
    }
    await wrapper.find('.result-load-btn').trigger('click')
    await flushPromises()
  }

  it('首段起点按码点数预览，不是 UTF-16 长度', async () => {
    // 'a😀😀'：3 个码点，5 个 UTF-16 码元。用后者当 offset 会跳过 2 个字符。
    const preview = 'a😀😀'
    mocked.getToolCallOutput.mockResolvedValue({
      content: 'TAIL', total_bytes: 40, total_chars: 7,
      offset: 3, limit: 8192, next_offset: 7,
    })

    const wrapper = mount(ToolResultBlock, { props: { item: truncated(preview), sessionId: 's1' } })
    await clickLoad(wrapper)

    expect(mocked.getToolCallOutput).toHaveBeenCalledWith('s1', 'c1', 3, 8192)
  })

  it('第二段沿用服务端给的 next_offset，不重算已拼接内容的长度', async () => {
    // 首段返回的内容自带一个 emoji：其 UTF-16 长度与码点数不同，
    // 若前端拿 `fullText.length` 推进，第二次请求的 offset 就会偏大。
    mocked.getToolCallOutput
      .mockResolvedValueOnce({
        content: '😀', total_bytes: 40, total_chars: 9,
        offset: 3, limit: 8192, next_offset: 4,
      })
      .mockResolvedValueOnce({
        content: 'Z', total_bytes: 40, total_chars: 9,
        offset: 4, limit: 8192, next_offset: 9,
      })

    const wrapper = mount(ToolResultBlock, { props: { item: truncated('abc'), sessionId: 's1' } })
    await clickLoad(wrapper)
    await clickLoad(wrapper)

    expect(mocked.getToolCallOutput).toHaveBeenNthCalledWith(2, 's1', 'c1', 4, 8192)
  })

  it('next_offset 到达 total_chars 时判定读完，按钮换成「已全部加载」', async () => {
    mocked.getToolCallOutput.mockResolvedValue({
      content: 'TAIL', total_bytes: 40, total_chars: 7,
      offset: 3, limit: 8192, next_offset: 7,
    })

    const wrapper = mount(ToolResultBlock, { props: { item: truncated('abc'), sessionId: 's1' } })
    await clickLoad(wrapper)

    expect(wrapper.find('.result-load-btn').exists()).toBe(false)
    expect(wrapper.find('.result-load-done').exists()).toBe(true)
  })

  it('未读完时保留按钮，可继续加载下一段', async () => {
    mocked.getToolCallOutput.mockResolvedValue({
      content: 'MORE', total_bytes: 900, total_chars: 500,
      offset: 3, limit: 8192, next_offset: 400,
    })

    const wrapper = mount(ToolResultBlock, { props: { item: truncated('abc'), sessionId: 's1' } })
    await clickLoad(wrapper)

    expect(wrapper.find('.result-load-btn').exists()).toBe(true)
  })
})
