import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import DOMPurify from 'dompurify'
import MessageItem from '../MessageItem.vue'
import { setMermaidAdapter, type MermaidAdapter } from '../mermaidRenderer'
import { setHighlightAdapter, type HighlightAdapter } from '../codeHighlighter'
import { closeImageViewer, useImageViewer } from '../../common/imageViewerState'
import type { ChatItem, TextItem } from '../chat-types'

const assistant = (content: string): ChatItem => ({ kind: 'text', role: 'assistant', content, id: 'm1' })

const mountText = (content: string) => mount(MessageItem, { props: { item: assistant(content) } })

/**
 * 流式期不做 markdown + sanitize（§4.3 的性能项，2026-10-09）。
 *
 * 模板在 `item.streaming` 时显示的是**纯文本**（`{{ displayContent }}`），`renderedHtml`
 * 只在回合结束、`streaming` 变 false 之后才被 `v-html` 用上 ⇒ 流式期每个 delta 都跑
 * `md.render` + `DOMPurify.sanitize` 是**纯浪费**。验收口径就是 §4.3 写的那句：
 * **N 次 delta 只渲染一次**。
 *
 * 这里盯 `DOMPurify.sanitize`（**模块级**）而不是组件里的 `renderMarkdown`（局部函数、spy 不到）；
 * `vi.spyOn` 默认保留原实现，所以断言渲染结果的那半仍然是真的 sanitize 输出。
 */
describe('流式期的渲染让位（§4.3）', () => {
  it('★ 流式期不跑 markdown/sanitize；回合结束才渲染一次（且真的走 v-html 分支）', async () => {
    const sanitize = vi.spyOn(DOMPurify, 'sanitize')
    // 用 `TextItem` 而不是 `ChatItem`：后者的联合类型在展开（`{ ...streaming, content }`）后
    // 会丢掉判别字段的收窄，`content`/`streaming` 在别的成员上不存在 ⇒ tsc 报错。
    const streaming: TextItem = { kind: 'text', role: 'assistant', content: 'a', id: 's1', streaming: true }
    const wrapper = mount(MessageItem, { props: { item: streaming as ChatItem } })
    await settleAsync()
    // 初始挂载时就是流式中 ⇒ 不该渲染
    expect(sanitize.mock.calls.length).toBe(0)

    // 模拟 N 次流式增量
    for (const text of ['ab', 'abc', 'abcd', 'abcde']) {
      await wrapper.setProps({ item: { ...streaming, content: text } as ChatItem })
      await settleAsync(2)
    }
    expect(sanitize.mock.calls.length).toBe(0)

    // 回合结束：streaming → false。**这是最容易漏的回归点** ——
    // `displayContent` 不依赖 `streaming`，所以 watcher 必须同时监听它，
    // 否则消息会一直停在纯文本。
    await wrapper.setProps({ item: { ...streaming, content: 'abcde', streaming: false } })
    await settleAsync()
    expect(sanitize.mock.calls.length).toBe(1)
    expect(wrapper.html()).toContain('<p>abcde</p>')
    sanitize.mockRestore()
  })
})

/** 生成 n 行代码块，用于验证长代码折叠阈值 */
const longCode = (lines: number) =>
  `\`\`\`js\n${Array.from({ length: lines }, (_, i) => `const a${i} = ${i}`).join('\n')}\n\`\`\``

/** 渲染链路是 onMounted → 异步渲染，需要多轮微任务收敛 */
async function settleAsync(times = 6) {
  for (let i = 0; i < times; i++) {
    await nextTick()
    await Promise.resolve()
  }
}

// 真实 mermaid 在 jsdom 下不可靠，这里注入替身、只验证渲染契约
let rendered: { id: string; code: string }[] = []
let initialized: Record<string, unknown>[] = []
let failRender = false

const fakeAdapter: MermaidAdapter = {
  initialize(options) { initialized.push(options) },
  async render(id, code) {
    rendered.push({ id, code })
    if (failRender) throw new Error('bad diagram')
    return { svg: `<svg data-mmd-id="${id}"></svg>` }
  },
}

const fakeHighlighter: HighlightAdapter = {
  getLanguage: () => ({}),
  highlight: (code: string) => ({ value: `<span class="k">${code}</span>` }),
  highlightAuto: (code: string) => ({ value: code }),
}

beforeEach(() => {
  rendered = []
  initialized = []
  failRender = false
  setMermaidAdapter(fakeAdapter)
  setHighlightAdapter(fakeHighlighter)
})

afterAll(() => { setMermaidAdapter(null); setHighlightAdapter(null) })

describe('MessageItem（正文链接处理）', () => {
  it('外部链接改为新窗口打开，并带上 noopener', () => {
    const wrapper = mountText('见 [文档](https://example.com/docs)')
    const link = wrapper.find('.msg-text a')
    expect(link.attributes('href')).toBe('https://example.com/docs')
    expect(link.attributes('target')).toBe('_blank')
    expect(link.attributes('rel')).toContain('noopener')
  })

  it('站内相对链接保持原行为（不加 target，交给应用自身导航）', () => {
    const wrapper = mountText('见 [文件](/v1/media/abc/download)')
    const link = wrapper.find('.msg-text a')
    expect(link.attributes('href')).toBe('/v1/media/abc/download')
    expect(link.attributes('target')).toBeUndefined()
  })

  it('裸链接（linkify）同样走外开规则', () => {
    const wrapper = mountText('参考 https://example.org/guide 这一节')
    expect(wrapper.find('.msg-text a').attributes('target')).toBe('_blank')
  })
})

describe('MessageItem（消息操作）', () => {
  it('可以把整条消息引用到输入框', async () => {
    const wrapper = mountText('一段较长的回复正文')
    await wrapper.find('[title="引用到输入框"]').trigger('click')
    expect(wrapper.emitted('quote')?.[0]?.[0]).toBe('一段较长的回复正文')
  })

  it('流式中的消息不提供引用（内容还在变）', () => {
    const wrapper = mount(MessageItem, {
      props: { item: { kind: 'text', role: 'assistant', content: '半截', id: 'm2', streaming: true } },
    })
    expect(wrapper.find('[title="引用到输入框"]').exists()).toBe(false)
  })
})

describe('MessageItem（mermaid 图表）', () => {
  it('渲染前展示源码可读，渲染成功后替换为 SVG', async () => {
    const wrapper = mountText('```mermaid\ngraph TD;\nA-->B;\n```')
    const slot = wrapper.find('.mermaid')
    expect(slot.exists()).toBe(true)
    expect(slot.text()).toContain('graph TD;')          // 未渲染时源码可见
    expect(slot.attributes('data-code')).toContain('graph')

    await settleAsync()
    expect(rendered).toHaveLength(1)
    expect(rendered[0]!.code).toContain('A-->B;')       // data-code 解码后交给 mermaid
    expect(wrapper.find('.mermaid-diagram svg').exists()).toBe(true)
  })

  it('按当前主题初始化（暗色走 dark）', async () => {
    document.documentElement.classList.add('dark')
    mountText('```mermaid\ngraph TD;\n```')
    await settleAsync()
    expect(initialized[0]).toEqual(expect.objectContaining({ theme: 'dark', securityLevel: 'strict' }))
    document.documentElement.classList.remove('dark')
  })

  it('语法有误时保留源码并标注失败，不留空白', async () => {
    failRender = true
    const wrapper = mountText('```mermaid\nnot a diagram\n```')

    await settleAsync()
    const slot = wrapper.find('.mermaid-error')
    expect(slot.exists()).toBe(true)
    expect(slot.attributes('data-error')).toContain('语法有误')
    expect(slot.text()).toContain('not a diagram')
  })

  it('流式中的消息不触发渲染（内容还在变）', async () => {
    mount(MessageItem, {
      props: { item: { kind: 'text', role: 'assistant', content: '```mermaid\ngraph TD;\n```', id: 'm3', streaming: true } },
    })
    await settleAsync()
    expect(rendered).toHaveLength(0)
    expect(initialized).toHaveLength(0)
  })

  it('没有图表块时完全不初始化 mermaid（懒加载的意义）', async () => {
    mountText('普通正文，没有图表')
    await settleAsync()
    expect(rendered).toHaveLength(0)
    expect(initialized).toHaveLength(0)
  })
})

describe('MessageItem（代码块）', () => {
  it('先输出可读源码与语言标记，挂载后由增强器高亮', async () => {
    const wrapper = mountText('```python\nprint(1)\n```')
    const code = wrapper.find('.code-block-wrapper code')
    expect(code.attributes('data-lang')).toBe('python')
    expect(code.text()).toContain('print(1)')

    await settleAsync()
    expect(wrapper.find('.code-block-wrapper code').classes()).toContain('hljs')
    expect(wrapper.find('.code-block-wrapper code').html()).toContain('class="k"')
  })

  it('复制按钮仍带完整源码（高亮不影响复制）', () => {
    const wrapper = mountText('```js\nconst a = 1\n```')
    const encoded = wrapper.find('.code-copy-btn').attributes('data-code')
    expect(decodeURIComponent(encoded!)).toBe('const a = 1\n')
  })

  it('短代码不折叠、无展开按钮，但显示行数', () => {
    const wrapper = mountText('```js\nconst a = 1\n```')
    expect(wrapper.find('.code-block-wrapper').attributes('data-collapsed')).toBeUndefined()
    expect(wrapper.find('.code-expand-btn').exists()).toBe(false)
    expect(wrapper.find('.code-lines').text()).toBe('1 行')
  })

  it('超过阈值的长代码默认折叠，按钮写明总行数', () => {
    const wrapper = mountText(longCode(60))
    expect(wrapper.find('.code-block-wrapper').attributes('data-collapsed')).toBe('1')
    expect(wrapper.find('.code-expand-btn').text()).toBe('展开全部（共 60 行）')
    expect(wrapper.find('.code-lines').text()).toBe('60 行')
  })

  it('点展开后切换为收起，再点恢复折叠', async () => {
    const wrapper = mountText(longCode(60))
    await wrapper.find('.code-expand-btn').trigger('click')
    expect(wrapper.find('.code-block-wrapper').attributes('data-collapsed')).toBe('0')
    expect(wrapper.find('.code-expand-btn').text()).toBe('收起（共 60 行）')

    await wrapper.find('.code-expand-btn').trigger('click')
    expect(wrapper.find('.code-block-wrapper').attributes('data-collapsed')).toBe('1')
    expect(wrapper.find('.code-expand-btn').text()).toBe('展开全部（共 60 行）')
  })
})

describe('MessageItem（图片查看）', () => {
  afterEach(() => { closeImageViewer() })

  it('点击正文图片唤起查看器（markdown 图片挂不上 Vue 事件，走点击委托）', async () => {
    const wrapper = mountText('![示意图](/v1/media/a.png)')
    await wrapper.find('.msg-text img').trigger('click')
    expect(useImageViewer().current.value).toEqual({
      src: expect.stringContaining('/v1/media/a.png'),
      alt: '示意图',
    })
  })
})

describe('MessageItem（超长正文让位渲染）', () => {
  /** 捕获 requestIdleCallback，手动触发才能确定性地验证"让位" */
  let idleQueue: (() => void)[] = []
  const originalIdle = (window as any).requestIdleCallback

  beforeEach(() => {
    idleQueue = []
    ;(window as any).requestIdleCallback = (cb: () => void) => { idleQueue.push(cb); return idleQueue.length }
  })

  afterEach(() => { (window as any).requestIdleCallback = originalIdle })

  /** 超过 LARGE_MESSAGE_CHARS（30000）才进让位；助手长文默认折叠，需先展开 */
  const hugeBody = (chars: number) => '正文段落内容。'.repeat(Math.ceil(chars / 7)).slice(0, chars)

  it('超长正文展开后先让位（纯文本），idle 回调才渲染 markdown', async () => {
    const wrapper = mountText(hugeBody(40000))
    await settleAsync()

    await wrapper.find('.collapse-toggle').trigger('click')   // 展开全文
    await settleAsync()

    expect(idleQueue).toHaveLength(1)                          // 进了让位，没有同步渲染
    expect(wrapper.find('.msg-text.streaming-text').exists()).toBe(true)
    expect(wrapper.find('.msg-text').html()).not.toContain('<p>')   // 让位期间还不是 markdown

    idleQueue[0]!()
    await settleAsync()
    expect(wrapper.find('.msg-text').html()).toContain('<p>')  // 渲染完成，markdown 接管
  })

  it('普通短正文同步渲染，不进入让位', async () => {
    mountText('普通短正文')
    await settleAsync()
    expect(idleQueue).toHaveLength(0)
  })

  it('让位期间内容又变，旧回调作废（不会用过期内容覆盖）', async () => {
    const wrapper = mountText(hugeBody(40000))
    await settleAsync()
    await wrapper.find('.collapse-toggle').trigger('click')
    await settleAsync()
    expect(idleQueue).toHaveLength(1)

    await wrapper.find('.collapse-toggle').trigger('click')     // 又收起
    await settleAsync()
    idleQueue[0]!()                                             // 执行过期的让位回调
    await settleAsync()

    expect(wrapper.html()).toContain('已折叠')                  // 仍是折叠预览，没被全文覆盖
  })
})

describe('MessageItem（长内容渲染让位）', () => {
  it('普通长度同步渲染 markdown（不出现纯文本过渡）', () => {
    const wrapper = mountText('普通**加粗**文本')
    expect(wrapper.find('.msg-text').classes()).not.toContain('streaming-text')
    expect(wrapper.find('.msg-text').html()).toContain('<strong>')
  })

  it('展开超长正文时先给纯文本，空闲后再渲染 markdown', async () => {
    const long = '段落内容测试文本\n\n'.repeat(4000)   // ~40000 字符：超过折叠阈值与让位阈值
    const wrapper = mountText(long)

    // 默认折叠成预览：短内容同步渲染，不出现过渡
    expect(wrapper.find('.msg-text').classes()).not.toContain('streaming-text')

    await wrapper.find('.collapse-toggle').trigger('click')
    await nextTick()
    expect(wrapper.find('.msg-text').classes()).toContain('streaming-text')   // 让位中：显示纯文本

    await new Promise(resolve => setTimeout(resolve, 20))
    await settleAsync()
    expect(wrapper.find('.msg-text').classes()).not.toContain('streaming-text')
    expect(wrapper.find('.msg-text').html()).toContain('<p>')
  })
})
