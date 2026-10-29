import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createRouter, createMemoryHistory, type Router } from 'vue-router'
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
/**
 * `notice` 行的**渲染**（2026-10-09）。
 *
 * 第 113–115 轮加的三种留痕（护栏拦截 / 上下文压缩 / 中断）此前只在**投影层**有测试
 * （`chatHistory.test.ts` 断言"产生了 notice 条目"），而 `MessageItem` 里那条
 * `v-else-if="item.kind === 'notice'"` **从没被渲染过** ✗ —— 少个分支、`role` 掉了、
 * tone class 拼错，测试都不会红。这里把**渲染那一半**补上（DOM 级）。
 */
describe('notice 行的渲染', () => {
  it('★ warning 通知行：文案 + role=status + tone class', async () => {
    const wrapper = mount(MessageItem, {
      props: { item: { kind: 'notice', tone: 'warning', content: '输入包含不允许的指令', id: 'n1' } as ChatItem },
    })
    await settleAsync()
    const row = wrapper.find('.notice-row')
    expect(row.exists()).toBe(true)
    expect(row.classes()).toContain('warning')
    expect(row.attributes('role')).toBe('status')   // 读屏要能播报
    expect(row.text()).toContain('输入包含不允许的指令')
  })

  it('info 通知行：tone class 不同（压缩留痕走这条）', async () => {
    const wrapper = mount(MessageItem, {
      props: { item: { kind: 'notice', tone: 'info', content: '压缩前 42.0k → 后 18.0k', id: 'n2' } as ChatItem },
    })
    await settleAsync()
    const row = wrapper.find('.notice-row')
    expect(row.classes()).toContain('info')
    expect(row.classes()).not.toContain('warning')
    expect(row.text()).toContain('42.0k')
  })
})

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

  /** 让位是**按字符数**触发的（> LARGE_MESSAGE_CHARS=30000），与折叠无关：
   *  折叠态现在渲染全文（只由 CSS 裁切），所以超长正文在挂载时就已经进入让位。 */
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

  it('让位期间正文又变，旧回调作废（不会用过期内容覆盖）', async () => {
    const wrapper = mountText(hugeBody(40000))
    await settleAsync()
    expect(idleQueue).toHaveLength(1)                          // 超长正文在挂载时已进入让位
    const stale = idleQueue[0]!

    // 正文在让位期间变了：短内容同步渲染，所以此刻界面已是新正文
    await wrapper.setProps({ item: assistant('新正文段落。'.repeat(100)) })
    await settleAsync()
    expect(wrapper.find('.msg-text').html()).toContain('新正文段落。')

    stale()                                                     // 执行**过期**的让位回调
    await settleAsync()
    // token 不匹配 ⇒ 旧长文被丢弃，界面仍是新正文
    expect(wrapper.find('.msg-text').html()).toContain('新正文段落。')
    expect(wrapper.find('.msg-text').html()).not.toContain('正文段落内容。')
  })
})

describe('MessageItem（长内容渲染让位）', () => {
  it('普通长度同步渲染 markdown（不出现纯文本过渡）', () => {
    const wrapper = mountText('普通**加粗**文本')
    expect(wrapper.find('.msg-text').classes()).not.toContain('streaming-text')
    expect(wrapper.find('.msg-text').html()).toContain('<strong>')
  })

  it('折叠态渲染的是全文（CSS 裁切），超长时先纯文本、空闲后再换 markdown', async () => {
    const long = '段落内容测试文本\n\n'.repeat(4000)   // ~40000 字符：同时超过折叠阈值与让位阈值
    const wrapper = mountText(long)

    // 折叠不再伪造截断：正文里没有"已折叠"提示，折叠由 class 表达（max-height + 羽化蒙版）
    expect(wrapper.html()).not.toContain('已折叠')
    expect(wrapper.find('.msg-text').classes()).toContain('collapsed')
    // 超长正文即使折叠也走让位：先纯文本（同样带 collapsed，所以不会铺满一屏）
    expect(wrapper.find('.msg-text').classes()).toContain('streaming-text')

    await new Promise(resolve => setTimeout(resolve, 20))
    await settleAsync()
    expect(wrapper.find('.msg-text').classes()).not.toContain('streaming-text')
    expect(wrapper.find('.msg-text').html()).toContain('<p>')
    expect(wrapper.find('.msg-text').classes()).toContain('collapsed')

    // 展开只是去掉裁切类，内容一直是同一份
    await wrapper.find('.collapse-toggle').trigger('click')
    await settleAsync()
    expect(wrapper.find('.msg-text').classes()).not.toContain('collapsed')
  })
})

// ── 三类失败的针对性动作（P0-2）─────────────────────────────────────────
//
// 三类失败的正确动作**互不相同**：余额不足 → 去充值（用户自己能解决）；
// 配额耗尽 → 找租户管理员（等多久都没用）；速率超限 → 等 N 秒（重试有效）。
// 只给一个"重试"按钮等于把三类失败当成同一件事，用户会在必然失败的循环里打转。
describe('失败消息的可执行动作', () => {
  const makeRouter = () => createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/chat', component: { template: '<div />' } },
      { path: '/billing', component: { template: '<div />' } },
    ],
  })

  const mountFailed = (over: Partial<TextItem>, router: Router) => mount(MessageItem, {
    props: {
      item: { kind: 'text', role: 'assistant', content: '', id: 'e1', error: true, errorMsg: '失败', ...over } as ChatItem,
    },
    global: { plugins: [router] },
  })

  it('★ 余额不足给「去充值」入口，点击后落到 /billing', async () => {
    const router = makeRouter()
    await router.push('/chat')
    await router.isReady()

    const wrapper = mountFailed({ errorCode: 'insufficient_credits' }, router)
    await settleAsync()

    const action = wrapper.findAll('.retry-btn').find(b => b.text().includes('去充值'))
    expect(action, '余额不足必须有一个直达充值页的按钮').toBeTruthy()

    await action!.trigger('click')
    // 导航是异步的（router.push 返回 promise）：只 await nextTick 不够，
    // 必须 flush 微任务队列才能看到 currentRoute 变化。
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/billing')
  })

  it('配额耗尽不给「去充值」：要的是找管理员，不是花钱', async () => {
    const wrapper = mountFailed({ errorCode: 'quota_exceeded' }, makeRouter())
    await settleAsync()

    expect(wrapper.findAll('.retry-btn').some(b => b.text().includes('去充值'))).toBe(false)
    expect(wrapper.find('.msg-error-banner').exists()).toBe(true)
  })

  it('配额耗尽不显示倒计时：周期配额等下也不会开', async () => {
    const wrapper = mountFailed({ errorCode: 'quota_exceeded', errorRetryAfterSec: 60 }, makeRouter())
    await settleAsync()

    expect(wrapper.find('.repair-hint').exists()).toBe(false)
  })

  it('session_busy 仍走「修复会话 + 倒计时」，未被新分支挤掉', async () => {
    const wrapper = mountFailed({ errorCode: 'session_busy', errorRetryAfterSec: 120 }, makeRouter())
    await settleAsync()

    expect(wrapper.find('.repair-hint').exists()).toBe(true)
    expect(wrapper.findAll('.retry-btn').some(b => b.text().includes('去充值'))).toBe(false)
  })

  it('★ 配额耗尽给「查看用量」入口，点击后落到 /billing', async () => {
    const router = makeRouter()
    await router.push('/chat')
    await router.isReady()

    const wrapper = mountFailed({ errorCode: 'quota_exceeded' }, router)
    await settleAsync()

    const action = wrapper.findAll('.retry-btn').find(b => b.text().includes('查看用量'))
    expect(action, '配额耗尽必须给一个能看到实际用量的入口').toBeTruthy()

    await action!.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/billing')
  })

  it('★ retryable=false 时不渲染「重试」按钮', async () => {
    // 后端说"重试必然再失败"（配额按周期重置），此时给重试按钮就是把用户
    // 推进一个必然失败的循环 —— 这比不报错更伤，因为他会以为是自己点得不对。
    const wrapper = mountFailed(
      { errorCode: 'quota_exceeded', errorRetryable: false },
      makeRouter(),
    )
    await settleAsync()

    expect(wrapper.findAll('.retry-btn').some(b => b.text().includes('重试'))).toBe(false)
    // 但错误横幅本身还在：不能因为不给重试就把失败悄无声息地咽下去
    expect(wrapper.find('.msg-error-banner').exists()).toBe(true)
  })

  it('retryable 缺省（服务端无结论）时仍给「重试」', async () => {
    // 这是绝大多数错误（网络抖动 / 500）的形状：不因新分支而失去重试能力。
    const wrapper = mountFailed({ errorCode: 'internal_error' }, makeRouter())
    await settleAsync()

    expect(wrapper.findAll('.retry-btn').some(b => b.text().includes('重试'))).toBe(true)
  })

  // ── 模型越界（P2-2b）────────────────────────────────────────────────
  //
  // 同一个 403 有两种归因，动作**相反**：套餐不覆盖 → 买了就能用；
  // 租户策略不允许 → 买套餐也没用。给错按钮的代价是让用户白花钱。
  it('★ 套餐不覆盖 → 给「去购买套餐」，点击后落到套餐页签', async () => {
    const router = makeRouter()
    await router.push('/chat')
    await router.isReady()

    const wrapper = mountFailed(
      { errorCode: 'model_not_allowed', errorReason: 'entitlement' },
      router,
    )
    await settleAsync()

    const action = wrapper.findAll('.retry-btn').find(b => b.text().includes('去购买套餐'))
    expect(action, '套餐不覆盖时必须给一个直达套餐页签的入口').toBeTruthy()

    await action!.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/billing')
    // 落到套餐页签而不是充值页：买套餐和充值是两件事
    expect(router.currentRoute.value.query.tab).toBe('packages')
  })

  it('租户策略不允许 → 不给「去购买套餐」（买了也没用）', async () => {
    const wrapper = mountFailed(
      { errorCode: 'model_not_allowed', errorReason: 'policy' },
      makeRouter(),
    )
    await settleAsync()

    expect(wrapper.findAll('.retry-btn').some(b => b.text().includes('去购买套餐'))).toBe(false)
    // 但错误横幅仍在：必须让用户看到"用不了"以及原因，不能静默
    expect(wrapper.find('.msg-error-banner').exists()).toBe(true)
  })

  it('模型越界且 retryable=false → 不渲染「重试」', async () => {
    const wrapper = mountFailed(
      { errorCode: 'model_not_allowed', errorReason: 'entitlement', errorRetryable: false },
      makeRouter(),
    )
    await settleAsync()

    expect(wrapper.findAll('.retry-btn').some(b => b.text() === '重试')).toBe(false)
  })
})
