import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'

/**
 * `ChatView` 的**交互级**测试（§6 #5 想要的那一层，**零新依赖**）。
 *
 * 与 `ChatView.spec.ts`（冒烟网：能挂载 / 初始加载 / 卸载不炸）的区别：这里**真的走一遍**
 * 「输入 → 提交 → 建 SSE → 收到帧 → 落成 transcript 条目」这条链 —— 冒烟网把重子组件全打桩，
 * 因此**看不到**任何一条 SSE 分支。
 *
 * 为什么现在能做：`createSSEConnection` 的契约是**回调式**（`(sessionId, onMessage, onError, opts)`），
 * 不是 `addEventListener` ⇒ 替身只要**把 `onMessage` 存下来**，测试就能直接喂一帧 ✓
 * （不需要引 Playwright，也不需要真的 EventSource）。
 *
 * 覆盖第 113–115 轮加的三条留痕在**实时路径**上的行为（此前只有投影层与渲染层测试）：
 * 护栏拦截 / 上下文压缩 / 中断。
 */

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

/**
 * jsdom 没有 `crypto.randomUUID`，而 `sendMessage` 用它生成 `client_msg_id`
 * （`ChatView.vue:2338`）⇒ 不打桩的话，提交会在**建流之后**抛错、走 `catch` 把 `loading` 复位
 * （`:2350`）—— 那样测到的就不是"提交成功"这条路径了（第一版就这么被骗过一次：
 * 用户消息照样出现，于是"看起来跑通了"，其实已经进了 catch）。
 */
if (typeof globalThis.crypto?.randomUUID !== 'function') {
  Object.defineProperty(globalThis.crypto ?? (globalThis.crypto = {} as Crypto), 'randomUUID', {
    value: () => '00000000-0000-4000-8000-000000000000',
    configurable: true,
  })
}

/** 各次建流拿到的 `onMessage` —— 测试用它喂帧。 */
const stream = vi.hoisted(() => ({
  /** 数组而不是单个：`sendMessage` 之外还有 `mapSessionStreams` / `ensureSubagentStream` 也会建流，
   *  只留最后一个会喂错流（第一版就栽在这）。 */
  onMessages: [] as Array<(data: unknown) => void>,
  onError: null as null | (() => void),
  closed: 0,
}))

const apiMocks = vi.hoisted(() => {
  const emptyList = (url?: string) => {
    void url
    return Promise.resolve({ data: { data: [] } })
  }
  return {
    emptyList,
    api: {
      get: vi.fn(emptyList),
      // 形参写出来（并显式 void）：否则 `mock.calls` 被推断成 `[url?]` 一元组，
      // 取 `calls[i][1]`（请求体）会报 TS2493；返回类型也要给成 `unknown`，
      // 否则用例里改成别的响应形状会被判成类型不匹配。
      post: vi.fn((url?: string, body?: unknown): Promise<unknown> => {
        void url; void body
        return Promise.resolve({ data: { data: [] } })
      }),
      put: vi.fn(emptyList),
      delete: vi.fn(emptyList),
    },
    createSSEConnection: vi.fn((_sessionId: string, onMessage: (data: unknown) => void, onError: () => void) => {
      stream.onMessages.push(onMessage)
      stream.onError = onError
      return {
        close: vi.fn(() => { stream.closed += 1 }),
        addEventListener: vi.fn(),
        readyState: 1,
      }
    }),
    submitApproval: vi.fn(emptyList),
    submitAnswer: vi.fn(emptyList),
    updateConversation: vi.fn(emptyList),
    createShare: vi.fn(emptyList),
    getActiveShare: vi.fn(emptyList),
    revokeShare: vi.fn(emptyList),
    getChatSessionMessages: vi.fn(emptyList),
    resolveMediaUrl: vi.fn((u: string) => u),
    // ⚠ `ChatInput.vue:160` 是 `models.value = await listModels()` —— 它要的是**数组本身**，
    // 不是 axios 的 `{ data: { data: [] } }` 外壳。冒烟网没暴露这点，是因为那里把 ChatInput
    // 打桩了；本文件把输入区解桩后，形状不对会以 `models.value.map is not a function` 浮出来。
    // ⚠ 这里**必须给一个模型**：`ChatView.sendMessage` 现在会在发送前确保有模型
    //    （空 model ⇒ 后端走默认路由 ⇒ 线上那条 `provider opencode-go stream failed: 401`）。
    //    此前这里返回**空数组**，于是整套交互测试都在"没有模型"的状态下跑 ——
    //    它们断言的其实是**会 401 的那条路径**，等于把线上 bug 当成了正常路径
    //    （2026-10-09 修此问题时暴露：13 个用例里 11 个依赖这个前提）。
    listModels: vi.fn(() => Promise.resolve([
      { provider: 'test-provider', name: 'test-model', display_name: 'Test Model', context_window: 8192 },
    ])),
    createAgent: vi.fn(emptyList),
    createGraph: vi.fn(emptyList),
  }
})

vi.mock('../../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api')>()
  return { ...actual, ...apiMocks }
})

vi.mock('../../api/sessionRuntime', () => ({
  getSessionRuntime: vi.fn(() => Promise.resolve(null)),
  putSessionRuntime: vi.fn(() => Promise.resolve()),
}))

vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({ user: { id: 'u-1', name: 'Tester', tenant_id: 't-1' }, token: 'tk' }),
}))
vi.mock('../../stores/theme', () => ({
  useThemeStore: () => ({ isDark: false, toggle: vi.fn() }),
}))

import ChatView from '../ChatView.vue'
import ChatInput from '../../components/chat/ChatInput.vue'
import { message } from 'ant-design-vue'
import { TOUR_ANCHORS } from '../../composables/useOnboardingTour'

/** 除 ChatInput 外全部打桩：输入区是真组件（提交路径要真的走）。 */
const STUBS = {
  MessageList: false,        // 要看到 notice 行真的渲染出来
  MessageItem: false,
  ChatInput: false,
  ChatSidePanel: true,
  SubAgentPanel: true,
  SessionPreviewPane: true,
  SessionStatsPanel: true,
  CallChainTimeline: true,
  FloatingPanel: true,
  SaveToKnowledgeDialog: true,
  SaveToMemoryDialog: true,
  ChatDisplaySettings: true,
  ChatStatusBar: true,
  AskCard: true,
  // 未配置引导的浮层打桩：本文件测的是「SSE 帧 → 提示」这条链，
  // 引导的**决策**在 `composables/__tests__/useOnboardingTour.spec.ts` 里测 ✓
  // （打桩后 `offer` 不在，`offerSetupTour` 会落回 toast —— 这条兜底路径因此也被覆盖 ✓）
  ChatSetupTour: true,
}

const router = createRouter({
  history: createMemoryHistory(),
  routes: [
    { path: '/', component: { template: '<div />' } },
    { path: '/chat', component: { template: '<div />' } },
  ],
})

/**
 * 喂一帧。
 *
 * 两个契约细节（都踩过）：
 * ① `createSSEConnection` 内部**已经 `JSON.parse`** 过（`api/index.ts:335`）⇒ `onMessage` 收到的是
 *    **对象**，不是字符串；
 * ② 帧形状是 `{ type, data }` —— `onSSEMessage` 取 `evt.data`（`ChatView.vue:2157`）。
 */
function feed(type: string, data: Record<string, unknown> = {}) {
  for (const cb of stream.onMessages) cb({ type, data })
}

describe('ChatView 交互级：提交 → SSE 帧 → transcript', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    stream.onMessages = []
    stream.onError = null
    stream.closed = 0
    apiMocks.api.get.mockImplementation(apiMocks.emptyList)
    apiMocks.getChatSessionMessages.mockImplementation(apiMocks.emptyList)
    // ⚠ 这里必须**重新装回**默认模型列表：`vi.clearAllMocks()` 只清调用记录，
    // **不重置 mock 实现** —— 某个用例把它改成 `[]` 之后会**泄漏**给后面的用例，
    // 于是后面的发送会被 `ensureModel()` 拦下、`stream.onMessages` 变成空数组，
    // 表现为"单独跑通过、全文件跑失败"（本轮就踩了这个，排查了一轮）。
    apiMocks.listModels.mockImplementation(() => Promise.resolve([
      { provider: 'test-provider', name: 'test-model', display_name: 'Test Model', context_window: 8192 },
    ]))
  })

  async function mountAndSend(text = '你好') {
    const wrapper = mount(ChatView, { global: { plugins: [router], stubs: STUBS } })
    await flushPromises()
    wrapper.findComponent(ChatInput).vm.$emit('send', text, [])
    await flushPromises()
    return wrapper
  }

  it('提交后真的建了流，且用户消息进了列表', async () => {
    const wrapper = await mountAndSend('你好')
    // ≥1：除提交外，会话映射 / 子代理流也会建连接
    expect(apiMocks.createSSEConnection.mock.calls.length).toBeGreaterThanOrEqual(1)
    expect(stream.onMessages.length).toBeGreaterThanOrEqual(1)
    expect(wrapper.text()).toContain('你好')
  })

  it('★ 护栏拦截帧 ⇒ transcript 里出现 warning 通知行（实时路径，不刷新）', async () => {
    const wrapper = await mountAndSend()
    feed('guardrail_blocked', { content: '输入包含不允许的指令' })
    await flushPromises()

    const row = wrapper.find('.notice-row')
    expect(row.exists()).toBe(true)
    expect(row.classes()).toContain('warning')
    expect(row.text()).toContain('输入包含不允许的指令')
  })

  it('★ 压缩帧 ⇒ 出现 info 通知行（且不是 warning）', async () => {
    const wrapper = await mountAndSend()
    feed('compaction', {
      content: JSON.stringify({ before_tokens: 42000, after_tokens: 18000, saved_tokens: 24000 }),
    })
    await flushPromises()

    const row = wrapper.find('.notice-row')
    expect(row.exists()).toBe(true)
    expect(row.classes()).toContain('info')
    expect(row.text()).toContain('42.0k')
  })

  /**
   * §4.7 的**接线级**验证（2026-10-09）。
   *
   * 第 120 轮把活跃判据从「位置」改成「生命周期」（`ProjectionInput.running` ← `MessageList` 的 `loading`），
   * 但那条修复只在**投影层**被测过。这里从交互层钉住**接线**：提交后 `loading` 真的为真、
   * 不带 token 的 `done` 真的把它置回 false。
   *
   * **两个必须知道的坑（第 124/125 轮各踩一个）**：
   * ① **不能读 `props('loading')`** —— Vue 把 prop 传播批到下一次渲染，emit 后同步读必然是旧值 ✗；
   *    读父组件的 setup 状态（`vm.loading`，`<script setup>` 在 dev 模式暴露 ref）才是真值 ✓。
   * ② **必须先有一个活跃会话**：`items` / `loading` 是**按会话切片**的可写 computed（`ChatView.vue:367-382`），
   *    而 `sendMessage` 在 `activeSessionId` 为空时会**新生成一个 id**（`:2310`）并写回（`:2347`）⇒
   *    代理切到**新会话**那个空切片 ⇒ 看起来像"提交把视图复位了" ✗。
   *    真实使用里用户是**往已有会话里发**，所以这里也让会话列表非空 ✓。
   */
  /** 会话感知的挂载：先给一个**已有会话**，避免撞上切片切换（见上面那条用例的注释 ②）。 */
  async function mountWithSession() {
    apiMocks.api.get.mockImplementation((url?: string) => {
      if (url === '/v1/conversations') {
        return Promise.resolve({
          data: { data: [{ id: 's-1', title: '会话 1', updated_at: '2026-10-08T00:00:00Z' }] },
        }) as never
      }
      return apiMocks.emptyList() as never
    })
    const wrapper = mount(ChatView, { global: { plugins: [router], stubs: STUBS } })
    await flushPromises()
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      loading: boolean
      items: Array<Record<string, unknown>>
      pendingApprovals: Array<Record<string, unknown>>
      pendingQuestions: Array<Record<string, unknown>>
    }
    wrapper.findComponent(ChatInput).vm.$emit('send', '你好', [])
    return { wrapper, vm }
  }

  it('★ error 帧 ⇒ 回合结束：loading 复位 + 流被关闭（清理路径真的走到）', async () => {
    const { wrapper, vm } = await mountWithSession()
    expect(vm.loading).toBe(true)
    const closedBefore = stream.closed

    feed('error', { content: '引擎错误' })
    await flushPromises()

    expect(vm.loading).toBe(false)
    // 引擎侧错误是**回合级**的，不该把"用户那条消息"标成失败（那是 sendMessage 的 catch 才做的事）
    expect(vm.items.some(i => i.kind === 'text' && i.error)).toBe(false)
    // 但必须**关掉流**：否则后续帧还会往这条已结束的回合里写
    expect(stream.closed).toBeGreaterThan(closedBefore)
    expect(wrapper.exists()).toBe(true)
  })

  it('★ 用户点停止 ⇒ loading 复位，且助手消息带 stopped 标记（可"继续生成"）', async () => {
    const { wrapper, vm } = await mountWithSession()

    // 先流出一段助手正文，再让用户停
    feed('text', { content: '正在回答' })
    await flushPromises()
    expect(vm.loading).toBe(true)

    wrapper.findComponent(ChatInput).vm.$emit('stop')
    await flushPromises()

    expect(vm.loading).toBe(false)
    const stopped = vm.items.filter(i => i.kind === 'text' && i.stopped)
    expect(stopped.length).toBeGreaterThanOrEqual(1)
  })

  it('★ 提交后 loading 为真；不带 token 的 done 把它置回 false（§4.7 接线级）', async () => {
    const { vm } = await mountWithSession()
    // 真值读法：`vm.loading`（不是 props —— 见上面那条注释的 ①）
    expect(vm.loading).toBe(true)

    // 真实引擎会发不带 token 的 done —— 修好之前，最后一轮会永远停在 running
    feed('done', {})
    await flushPromises()

    expect(vm.loading).toBe(false)
  })

  /**
   * 审批 / 提问帧（2026-10-09）。
   *
   * 这两条是**安全相关**的：审批卡片要如实显示"要执行什么、参数是什么"，
   * 用户据此决定批不批 —— 数据接错了（漏 id、参数为空、选项丢失）比崩溃更危险，
   * 因为它会**静默地**让人批准一件他没看清的事。这里钉住**数据接线**。
   */
  it('★ approval 帧 ⇒ 待审批条目带 id / 工具名 / 参数，且有到期时间', async () => {
    const { vm } = await mountWithSession()

    feed('approval', { id: 'call_1', name: 'run_command', arguments: '{"cmd":"rm -rf /tmp/x"}' })
    await flushPromises()

    expect(vm.pendingApprovals).toHaveLength(1)
    const p = vm.pendingApprovals[0]!
    expect(p.id).toBe('call_1')
    expect(p.toolName).toBe('run_command')
    // 参数必须**原样**到达卡片（否则用户看不到自己要批的是什么）
    expect(String(p.arguments)).toContain('rm -rf /tmp/x')
    expect(Number(p.expiresAt)).toBeGreaterThan(Date.now() - 1000)
  })

  it('★ ask 帧 ⇒ 提问条目带问题与选项（且缺字段时有兜底）', async () => {
    const { vm } = await mountWithSession()

    feed('ask', { id: 'q_1', question: '要部署到哪个环境？', options: ['staging', 'prod'] })
    await flushPromises()

    expect(vm.pendingQuestions).toHaveLength(1)
    const q = vm.pendingQuestions[0]!
    expect(q.question).toBe('要部署到哪个环境？')
    expect(q.options).toEqual(['staging', 'prod'])
    expect(q.allowFreeText).toBe(true)
  })

  /**
   * 工具调用 ↔ 结果的**配对**（2026-10-09）。
   *
   * `tool_result` 是按 `tool_call_id` 回填到对应的 `tool_call` 上（`ChatView.vue:2171-2182`）——
   * 配错就表现为"结果挂到别的工具上"或"工具永远转圈"，都是**静默**的数据错。
   * 另外 `isError` 必须如实传递：**失败的工具绝不能显示成成功**（用户据此判断要不要重试）。
   */
  it('★ tool_call → tool_result 正确配对：状态置 done，且 error 如实传递', async () => {
    const { vm } = await mountWithSession()

    feed('tool_call', { id: 'c1', name: 'read_file', arguments: '{"path":"a.txt"}' })
    await flushPromises()
    const call = () => vm.items.find(i => i.kind === 'tool_call' && i.id === 'c1')!
    expect(call().status).toBe('running')
    expect(call().name).toBe('read_file')

    feed('tool_result', { tool_call_id: 'c1', content: 'file body', error: true })
    await flushPromises()

    expect(call().status).toBe('done')
    const result = vm.items.find(i => i.kind === 'tool_result' && i.toolCallId === 'c1')!
    expect(result.content).toBe('file body')
    expect(result.isError).toBe(true)   // 失败必须看得出来
  })

  it('★ 结果 id 不认识时**不污染**别的工具（也不把它标成 done）', async () => {
    const { vm } = await mountWithSession()

    feed('tool_call', { id: 'c1', name: 'read_file', arguments: '{}' })
    await flushPromises()
    const call = () => vm.items.find(i => i.kind === 'tool_call' && i.id === 'c1')!

    feed('tool_result', { tool_call_id: 'c-unknown', content: 'orphan' })
    await flushPromises()

    // c1 仍在跑（不该被一条无关结果结束）
    expect(call().status).toBe('running')
    // 孤儿结果照旧落行（它带着自己的 id），只是没有对应的 call
    const orphan = vm.items.find(i => i.kind === 'tool_result' && i.toolCallId === 'c-unknown')!
    expect(orphan.content).toBe('orphan')
  })

  /**
   * 用量的**累加**语义（2026-10-09）。
   *
   * `ChatView.vue:2184-2199` 的注释自己写明了这条不变量：引擎在**每次 LLM 调用**结束后发一条
   * `usage`（**增量**），而一次提交会跨多步 ⇒ **必须累加进同一条 `turn_stats`**，
   * "逐条 push 会堆出 N 行" ✓。`mergeTurnStats` 本身在 `chat-types.spec.ts` 有单测，
   * 但**接线**（handler 真的用它、`streamStatsId` 真的串起来）此前没人测 ✗ —— 这里补上。
   *
   * 为什么值得钉：界面上 N 行"tokens: …"会被读成 N 个回合；而 Go 侧 `usageTotals`
   * 是同口径的（`internal/api/usage_accounting.go`）—— 前端堆行会让**两边数字对不上** ✓。
   */
  it('★ 多条 usage 帧累加进同一条 turn_stats（不是逐条堆 N 行）', async () => {
    const { vm } = await mountWithSession()

    feed('usage', { input_tokens: 10, output_tokens: 5 })
    feed('usage', { input_tokens: 20, output_tokens: 7 })
    await flushPromises()

    const stats = vm.items.filter(i => i.kind === 'turn_stats')
    expect(stats).toHaveLength(1)          // 累加，不堆行
    expect(stats[0]?.inputTokens).toBe(30)
    expect(stats[0]?.outputTokens).toBe(12)
  })

  it('★ 零用量的 usage 帧不产生空行（避免无意义的"0 in / 0 out"）', async () => {
    const { vm } = await mountWithSession()

    feed('usage', { input_tokens: 0, output_tokens: 0 })
    await flushPromises()

    expect(vm.items.filter(i => i.kind === 'turn_stats')).toHaveLength(0)
  })

  /**
   * ⚠ **记录当前行为，不是背书**（2026-10-09）—— 两条注释互相矛盾，已列为待决项。
   *
   * `ChatView.vue:675` 写：默认模式在"会话没记（或记了个不认识的值）"时回落 ——
   * **绝不能沿用上一个会话的模式** ✓；
   * 而 `:1797-1798` 又写：模式"是前端全局实时状态，切换会话**既不改变它们**，也不从会话状态读回" ✓。
   *
   * **实测代码跟随后者**：`mode` / `toolsMode` 只有两个赋值点（`:660` / `:812`），**都在用户驱动的 setter 里**，
   * 切会话时**没有任何复位** ⇒ 它们会**跨会话沿用** ✓。
   *
   * **为什么这条要单独标出来**：`toolsMode` 是**工具授权模式**，`yolo` 的文案就是
   * "工具确认被跳过"（`:662`）—— 在一个会话里开了 `yolo`，切到另一个会话**仍然自动批准** ✗，
   * 而用户在切换时**不会预期**这一点。这是**安全相关**的行为差异，不是外观问题。
   *
   * 本用例把**现状**钉住（免得它被无声改动），**不代表它是对的** —— 待决项见
   * `docs/development-roadmap.md`（第 131 轮记录）。
   */
  it('⚠ 现状：toolsMode 跨会话沿用（yolo 会带进下一个会话）—— 待决，不是背书', async () => {
    const { vm } = await mountWithSession()
    const v = vm as unknown as {
      toolsMode: string
      onToolsModeChange: (v: unknown) => void
      switchSession: (id: string) => Promise<void>
    }

    v.onToolsModeChange('yolo')
    expect(v.toolsMode).toBe('yolo')

    await v.switchSession('s-2')
    await flushPromises()

    // 现状：切了会话，授权模式**没有**回到默认 —— 这正是 :675 那句警告说"绝不能"的事
    expect(v.toolsMode).toBe('yolo')
  })

  /**
   * ── 2026-10-09 修复：无会话 / 无模型时的发送路径 ──
   *
   * 线上现象：用户不先「新建会话 + 选模型 + 配对话模式 + 配授权方式」就直接发消息 ⇒
   * ① 会话**永久丢失** —— `sendMessage` 自己编了个 UUID，而 `activeSessionId` 只有提交
   *    **成功**才写回（`ChatView.vue:2347` 原样），侧栏 `sessions` 里从来没有它；
   *    消息又落进"无会话"占位切片 `__none__` ⇒ 切走就再也回不来；
   * ② 大概率报 `provider opencode-go stream failed: AuthenticationError: 401 Invalid API key`
   *    —— `llm_config.model` 为空 ⇒ 后端走默认路由 ⇒ 那个 provider 的密钥无效
   *    （产地 `python-engine/app/gateway/router.py:244`）。
   */
  it('★★ 引导锚点真的挂出来了（锚点被改名/删掉时没人会知道 —— 本仓无 e2e）', async () => {
    const wrapper = await mountAndSend('你好')
    // 输入框与模型选择器这两条锚点必须真的在 DOM 里：`buildTourSteps` 用
    // `[data-tour="…"]` 找元素，找不到时 antd 会退化成"居中浮层" —— **不报错、只是指错地方** ✗
    expect(wrapper.find(`[data-tour="${TOUR_ANCHORS.input}"]`).exists()).toBe(true)
    expect(wrapper.find(`[data-tour="${TOUR_ANCHORS.modelPicker}"]`).exists()).toBe(true)
  })

  it('★★ 没有会话时发送 ⇒ **先建会话再提交**，且用服务端会话 id（消息不再是无主孤魂）', async () => {
    apiMocks.api.post.mockImplementation((url?: string) => {
      if (url === '/v1/conversations') {
        return Promise.resolve({ data: { data: { id: 'srv-1', title: '新对话' } } })
      }
      return Promise.resolve({ data: { data: [] } })
    })

    const wrapper = await mountAndSend('你好')
    await flushPromises()

    const calls = apiMocks.api.post.mock.calls.map(c => String(c[0]))
    const convAt = calls.indexOf('/v1/conversations')
    const submitAt = calls.indexOf('/submit')
    expect(convAt).toBeGreaterThanOrEqual(0)      // 真的建了会话
    expect(submitAt).toBeGreaterThan(convAt)      // 且**在建会话之后**才提交

    const body = apiMocks.api.post.mock.calls[submitAt]?.[1] as { session_id?: string } | undefined
    expect(body?.session_id).toBe('srv-1')        // 用服务端 id，而不是自己编的
    expect(wrapper.text()).toContain('你好')
  })

  it('★★ 没有可用模型时**不发请求**（不再打到后端默认路由吃 401）', async () => {
    apiMocks.listModels.mockImplementation(() => Promise.resolve([]))

    const wrapper = await mountAndSend('你好')
    await flushPromises()

    const urls = apiMocks.api.post.mock.calls.map(c => String(c[0]))
    expect(urls).not.toContain('/submit')
    // 连模型都没有时也不该先造一个空会话出来
    expect(urls).not.toContain('/v1/conversations')
    void wrapper
  })

  it('★ provider 认证错误帧 ⇒ 提示「加一句可行动的」，且**保留原文**（原文才是线索）', async () => {
    const errSpy = vi.spyOn(message, 'error').mockImplementation((() => undefined) as never)
    try {
      const wrapper = await mountAndSend('你好')
      feed('error', {
        content: "provider opencode-go stream failed: AuthenticationError: Error code: 401 "
          + "- {'error': {'message': 'Invalid API key.'}}",
      })
      await flushPromises()

      // 断言"所有调用里出现过"而不是"最后一次"：`feed` 会把帧喂给**每一个**已建立的流
      // （提交流 / 会话映射流 / 子 Agent 流），最后一次调用未必来自这条错误分支。
      const shown = errSpy.mock.calls.map(c => String(c[0])).join(' | ')
      expect(shown).toContain('API Key')          // 可行动的那句
      expect(shown).toContain('Invalid API key')  // 原文仍在
      void wrapper
    } finally {
      errSpy.mockRestore()
    }
  })
})
