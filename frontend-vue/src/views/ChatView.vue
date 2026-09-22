<script setup lang="ts">
import { ref, computed, reactive, shallowRef, onMounted, onUnmounted, nextTick, watch, h } from 'vue'
import { Button, Input, Modal, Checkbox, Alert, message, Dropdown, Menu, MenuItem, MenuDivider } from 'ant-design-vue'
import { MenuOutlined, CopyOutlined, LinkOutlined, CloseOutlined } from '@ant-design/icons-vue'
import {
  api, createSSEConnection, submitApproval, submitAnswer,
  updateConversation, createShare, getActiveShare, revokeShare,
  getChatSessionMessages, resolveMediaUrl, getSessionMode, setSessionMode, listModels,
  createAgent, createGraph,
} from '../api'
import type { ShareInfo, LlmModel } from '../api'
import { getSessionRuntime, putSessionRuntime } from '../api/sessionRuntime'
import FloatingPanel from '../components/common/FloatingPanel.vue'
import SubAgentPanel from '../components/chat/SubAgentPanel.vue'
import SessionStatsPanel from '../components/chat/SessionStatsPanel.vue'
import SessionPreviewPane from '../components/chat/SessionPreviewPane.vue'
import { useAuthStore } from '../stores/auth'
import { useThemeStore } from '../stores/theme'
import { useRoute, useRouter } from 'vue-router'
import ChatSidePanel from '../components/chat/ChatSidePanel.vue'
import type { SubagentEvent } from '../api/subagent'
import MessageList from '../components/chat/MessageList.vue'
import MessageItem from '../components/chat/MessageItem.vue'
import ChatEmptyHero from '../components/chat/ChatEmptyHero.vue'
import ChatInput from '../components/chat/ChatInput.vue'
import SaveToKnowledgeDialog from '../components/chat/SaveToKnowledgeDialog.vue'
import SaveToMemoryDialog from '../components/chat/SaveToMemoryDialog.vue'
import { CloudUploadOutlined } from '@ant-design/icons-vue'
import { sessionToMarkdown } from '../utils/sessionMarkdown'
import { sessionToGraph } from '../utils/sessionGraph'
import { sessionToAgent, agentBindingsFromContext } from '../utils/sessionAgent'
import ChatStatusBar from '../components/chat/ChatStatusBar.vue'
import ChatDisplaySettings from '../components/chat/ChatDisplaySettings.vue'
import AskCard from '../components/chat/AskCard.vue'
import CallChainTimeline from '../components/CallChainTimeline.vue'
import { HistoryOutlined, ExportOutlined, BulbOutlined, BulbFilled, MoreOutlined, FontSizeOutlined, SearchOutlined, PartitionOutlined, RobotOutlined, DatabaseOutlined, SwapOutlined } from '@ant-design/icons-vue'
import { splitThinking, stripUserInputTag, formatClock, formatSize, countItemsAfter } from '../components/chat/chat-types'
import { mergeHistory, normalizeMeta } from '../components/chat/chat-history'
import { findMatches } from '../components/chat/transcriptSearch'
import { describeApiError } from '../utils/apiError'
import { buildWorkbenchContext, chipsFromWorkbenchContext, CONTEXT_QUERY_KEYS, parseContextQuery, type ContextChip } from '../components/chat/contextChips'
import { buildPrefillText, setChatPrefill, takeChatPrefill } from '../components/chat/chatPrefill'
import type { ChatItem, ChatSession, ChatAttachment, TurnStatsItem, TextItem } from '../components/chat/chat-types'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const authStore = useAuthStore()
const themeStore = useThemeStore()
const route = useRoute()
const router = useRouter()

// 工具条低频操作（导出/主题）收进溢出菜单：条上只留「会话/轨迹」两个高频入口。
// 菜单项在渲染时求值，故与 themeStore 的初始化顺序无关。
const toolbarMenuItems = computed(() => [
  { key: 'export', label: t('导出为 Markdown'), icon: () => h(ExportOutlined), disabled: !items.value.length },
  { key: 'save_kb', label: t('存入知识库'), icon: () => h(CloudUploadOutlined), disabled: !hasSessionContent.value },
  { key: 'save_memory', label: t('记住这条'), icon: () => h(DatabaseOutlined), disabled: !hasSessionContent.value },
  { key: 'save_workflow', label: t('另存为工作流'), icon: () => h(PartitionOutlined), disabled: !hasSessionContent.value || savingWorkflow.value },
  { key: 'save_agent', label: t('存为 Agent'), icon: () => h(RobotOutlined), disabled: !hasSessionContent.value || savingAgent.value },
  { type: 'divider' as const },
  { key: 'display', label: t('显示设置'), icon: () => h(FontSizeOutlined) },
  // 交换布局：侧边栏与分屏左右对调。注意这里引用的 layoutSwapped 定义在本文件靠后处，
  // 但本数组是 computed（getter 惰性求值），执行时它已初始化 —— 与 splitTitle 同理。
  { key: 'swap_layout', label: layoutSwapped.value ? t('布局：侧栏在右') : t('布局：侧栏在左'), icon: () => h(SwapOutlined) },
  { key: 'theme', label: themeStore.isDark ? t('切换到亮色模式') : t('切换到暗色模式'), icon: () => h(themeStore.isDark ? BulbFilled : BulbOutlined) },
])

const displaySettingsOpen = ref(false)
/** 存入知识库：把会话正文沉淀成知识库文档（弹窗里选目标知识库） */
const saveToKbOpen = ref(false)
/** 记住这条：把会话正文沉淀成长期记忆条目（后续对话会自动注入） */
const saveToMemoryOpen = ref(false)
/**
 * 是否存在可沉淀的会话正文。
 *
 * 与 `sessionToMarkdown(...) !== ''` 完全等价（见 utils/sessionMarkdown.ts：只有标题为空
 * 且没有任何非空 text 项时才返回空串），但**不构造字符串** —— 流式输出时 items 每收到一个
 * chunk 就会变，若用 computed 往返序列化整个会话，长对话下每个 token 都要做一次全量拼接，
 * 还会连锁重算 toolbarMenuItems。这里只做短路判定。
 */
const hasSessionContent = computed(
  () =>
    (activeSession.value?.title || '').trim() !== '' ||
    items.value.some(i => i.kind === 'text' && ((i as TextItem).content || '').trim() !== ''),
)
/**
 * 沉淀弹窗的正文：**按需生成** —— 只在对应弹窗打开时序列化。
 * 弹窗打开后 items 基本不再变化，因此这等价于在原位算一次，而不是随流式输出反复算。
 */
const sessionMarkdownForDialog = computed(() =>
  saveToKbOpen.value || saveToMemoryOpen.value
    ? sessionToMarkdown(items.value, activeSession.value?.title || '')
    : '',
)
/** 另存为工作流：把整段对话沉淀成一个可重复执行的工作流图（单 llm 节点） */
const savingWorkflow = ref(false)

async function saveAsWorkflow() {
  // 带上本对话挂的知识库：生成 input → knowledge → llm 链，让沉淀出的工作流
  // 在每次运行时都先检索该知识库（见 utils/sessionGraph.ts 的说明）。
  const graph = sessionToGraph(items.value, activeSession.value?.title || '', {
    kbId: agentBindingsFromContext(buildContext()).kb_id,
  })
  if (!graph) {
    message.warning(t('当前会话没有可沉淀的正文'))
    return
  }
  savingWorkflow.value = true
  try {
    const saved = await createGraph({ name: graph.name, graph_json: graph.graph_json })
    message.success(`已保存为工作流「${saved?.name || graph.name}」，可在工作流页打开调整`)
  } catch (e) {
    message.error('保存工作流失败: ' + describeApiError(e))
  } finally {
    savingWorkflow.value = false
  }
}

/**
 * 存为 Agent：把会话正文沉淀成 Agent 的人格（system_prompt）。
 *
 * 名字必须由用户确认 —— `sessionToAgent` 只给一个基于会话标题的默认值，
 * 直接落库会积出一堆叫"对话记录"的条目。
 */
const saveAgentOpen = ref(false)
const savingAgent = ref(false)
const agentDraft = ref({ name: '', description: '', system_prompt: '' })

function openSaveAsAgent() {
  const draft = sessionToAgent(items.value, activeSession.value?.title || '')
  if (!draft) {
    message.warning(t('当前会话没有可沉淀的正文'))
    return
  }
  agentDraft.value = draft
  saveAgentOpen.value = true
}

async function saveAsAgent() {
  const draft = agentDraft.value
  const name = draft.name.trim()
  if (!name) {
    message.warning(t('请填写 Agent 名称'))
    return
  }
  savingAgent.value = true
  try {
    // 连带把本对话挂的知识库/技能/插件写进新 Agent（见 utils/sessionAgent.ts 的
    // agentBindingsFromContext）：只沉淀人格会让用户存完还得回各工作台重新装配一遍。
    const bindings = agentBindingsFromContext(buildContext())
    await createAgent({
      name,
      description: draft.description,
      system_prompt: draft.system_prompt,
      enabled: true,
      ...bindings,
    })
    // 明确说出继承了哪些 —— 绑定是"沉默生效"的，不告诉用户就成幽灵行为
    const inherited = [
      bindings.kb_id ? '知识库' : '',
      bindings.skills?.length ? `${bindings.skills.length} 个技能` : '',
      bindings.plugins?.length ? `${bindings.plugins.length} 个插件` : '',
    ].filter(Boolean)
    message.success(
      `已创建 Agent「${name}」${
        inherited.length ? `，并继承本对话的 ${inherited.join('、')}` : ''
      }，可在 Agents 页继续调整`,
    )
    saveAgentOpen.value = false
  } catch (e) {
    message.error('创建 Agent 失败: ' + describeApiError(e))
  } finally {
    savingAgent.value = false
  }
}

/**
 * 布局方向：把右侧的侧边栏与左侧的分屏**左右对调**。
 *
 * 为什么是"本机记忆"而不是服务端偏好：它描述的是**这块屏幕上的阅读习惯**
 * （宽屏上有人习惯把会话列表放在左手边），不随账号走。与字号、主题同一类，
 * 所以落 localStorage，且**读取失败一律回退默认**（隐私模式不该因此崩）。
 */
const LAYOUT_SWAP_KEY = 'chiron.chat.swapLayout'
const layoutSwapped = ref(readLayoutSwap())

function readLayoutSwap(): boolean {
  try {
    return localStorage.getItem(LAYOUT_SWAP_KEY) === '1'
  } catch {
    return false
  }
}

function toggleLayout() {
  layoutSwapped.value = !layoutSwapped.value
  try {
    localStorage.setItem(LAYOUT_SWAP_KEY, layoutSwapped.value ? '1' : '0')
  } catch {
    /* 存不下也不影响本次会话内的切换 */
  }
}

function onToolbarMenu(info: { key: string | number }) {
  if (info.key === 'export') exportMarkdown()
  else if (info.key === 'save_kb') saveToKbOpen.value = true
  else if (info.key === 'save_memory') saveToMemoryOpen.value = true
  else if (info.key === 'save_workflow') void saveAsWorkflow()
  else if (info.key === 'save_agent') openSaveAsAgent()
  else if (info.key === 'display') displaySettingsOpen.value = true
  else if (info.key === 'theme') themeStore.toggleTheme()
  else if (info.key === 'swap_layout') toggleLayout()
}

// 消息区「引用到输入框」：把选中文本交给输入框（ChatInput 暴露 insertText）
const chatInputRef = ref<InstanceType<typeof ChatInput> | null>(null)

function onQuoteText(text: string) {
  chatInputRef.value?.insertText(text)
}

// ── 会话内检索（正文与思考；工具输出不进结果）────────────────────────────
const searchOpen = ref(false)
const searchQuery = ref('')
const searchCursor = ref(0)
const searchInputRef = ref<HTMLInputElement | null>(null)
const searchMatches = computed(() => findMatches(items.value, searchQuery.value))
watch(searchQuery, () => { searchCursor.value = 0 })

function openSearch() {
  searchOpen.value = true
  nextTick(() => searchInputRef.value?.focus())
}

function closeSearch() {
  searchOpen.value = false
  searchQuery.value = ''
}

/** 循环跳转：命中项用既有的 focusToken 链路（含高亮闪烁与滚动归因） */
function gotoMatch(delta: number) {
  const hits = searchMatches.value
  if (!hits.length) return
  searchCursor.value = (searchCursor.value + delta + hits.length) % hits.length
  trajectoryFocus.value = hits[searchCursor.value]!
  trajectoryToken.value++
}

// 状态栏：最近一轮用量（turn_stats 由后端在回合结束时下发）
const lastTurnStats = computed<TurnStatsItem | null>(() => {
  for (let i = items.value.length - 1; i >= 0; i--) {
    const item = items.value[i]
    if (item?.kind === 'turn_stats') return item
  }
  return null
})

// 最近一次自动压缩（问题 4：压缩早已实现，但此前前端完全不可感知 —— 用户只会
// 觉得"上下文好像丢了/回答变短了"）。引擎现在会发 compaction 事件，这里接住并展示。
const lastCompaction = ref<{ beforeTokens?: number; afterTokens?: number; savedTokens?: number } | null>(null)

/**
 * 运行时解析结果（`GET /v1/sessions/{id}/runtime` 的 `resolved`）。
 *
 * 后端已经把解析链算好：**请求显式 > 会话 runtime > 用户默认 > 全局默认 > 系统兜底**，
 * 并给出每一项的 `source`。前端只负责显示 —— 用户由此才能回答
 * "我现在到底在用哪个模式/模型？是谁定的？"（ZCode 的 `value / effectiveValue / overridden` 三件套）
 */
const runtimeResolved = ref<Record<string, { value?: string; source?: string }> | null>(null)

/**
 * 观测浮层开关。子 Agent 与统计已从侧栏移出（设计稿 docs/floating-panels-design.md）：
 * 侧栏只留「导航」（轨迹 / 会话历史），观测类信息按需浮出、看完即关。
 */
const subagentsOpen = ref(false)
const statsOpen = ref(false)

/**
 * 分屏 6a：参考栏绑定的会话 id（空 = 不显示）。
 *
 * 先做**只读参考栏** —— 真正"两栏都可写"要把 `activeSessionId` / `items` / 流式连接
 * 从单值改成**按 pane 归属**（ZCode 为此有 `paneLayoutStore` + `SessionPane` 一整套），
 * 那是一次状态归属重构。而"边跑子 Agent 边看另一个会话"这个高频需求只读就够，
 * 且**完全不触碰现有状态模型**（零回归风险）。可写双栏属 6b 之后。
 */
const splitSessionId = ref('')

/** 工具栏按钮：开/关参考栏 */
function toggleSplit() {
  if (splitSessionId.value) {
    splitSessionId.value = ''
    return
  }
  // 候选 = 会话列表里第一个**不是当前会话**的会话。
  // 这里刻意**不用 watch / 不在模块级读 activeSessionId**：`activeSessionId` 与 `sessions`
  // 在本文件里声明得比本函数更晚，而函数体只在**点击那一刻**求值 ——
  // 于是既能拿到最新值，又不会触发 TDZ（"used before its declaration"）。
  const prev = sessions.value.find(s => s.id !== activeSessionId.value)
  if (!prev) {
    message.info(t('还没有其它会话可以作为参考'))
    return
  }
  splitSessionId.value = prev.id
}

/** 参考栏标题（computed 的 getter 同样惰性求值，渲染时 sessions 已就绪） */
const splitTitle = computed(() => {
  const s = sessions.value.find(x => x.id === splitSessionId.value)
  return s?.title || splitSessionId.value.slice(0, 8)
})

/**
 * 底部预留高度（输入区 + 状态栏）—— 传给浮层当限高基准，
 * 使浮层"向上长高到输入区顶部即止"，从而**不遮挡正在写的草稿**。
 * 取近似常量而非实时测量：多留一些比少留安全，且避免 ResizeObserver 的复杂度。
 */
const floatingBottomInset = 160

/**
 * 运行中的子 Agent 数（状态栏角标）。
 *
 * 直接数实时事件：遇到 `subagent.done` 就移除，否则记为运行中 ——
 * 不依赖面板是否打开、也不等侧栏轮询结果，所以"有子 Agent 在跑"永远看得出来。
 */
const subagentActiveCount = computed(() => {
  const active = new Set<string>()
  for (const e of subagentLiveEvents.value) {
    const id = (e as { run_id?: string }).run_id
    if (!id) continue
    if ((e as { type?: string }).type === 'subagent.done') active.delete(id)
    else active.add(id)
  }
  return active.size
})

/** 解析来源 → 用户可读的词 */
const SOURCE_LABELS: Record<string, string> = {
  request: '本次请求',
  session: '本会话',
  default: '偏好默认',
  system: '系统默认',
  auto: '自动路由',
}

/** 当前模式的显示名 */
const modeLabel = computed(() => modeOptions.find(o => o.value === mode.value)?.label || '常规')

/**
 * 当前模式的**来源**（空串表示后端没给解析结果 —— 此时不显示后缀，
 * 避免界面出现"常规 · 未知"这种既占位又没信息的东西）。
 */
const modeSourceLabel = computed(() => {
  const src = runtimeResolved.value?.mode?.source
  return src ? (SOURCE_LABELS[src] || src) : ''
})

// 上下文占用环的分母：模型上限来自 /v1/models 的 context_window（拿不到就不显示比例）
const availableModels = ref<LlmModel[]>([])
const contextWindow = computed(() => {
  const name = llmModel.value
  if (!name) return null
  return availableModels.value.find(m => m.name === name)?.context_window || null
})
listModels()
  .then(models => {
    availableModels.value = models
    // 模型默认兜底：列表就绪时若还没有选中任何模型，**自动选第一个可用模型**。
    // 否则 `llmModel` 会一直是空 —— 提交时不携带 model，落到**后端默认模型**，
    // 而默认模型一旦被上游下架就会报 "Upstream request failed: Model is unavailable."。
    // 注意顺序：这必须发生在列表到达之后（切换会话时的第三级回落依赖它）。
    if (!llmModel.value && models.length) {
      llmModel.value = models[0].name
    }
  })
  .catch(() => { availableModels.value = [] })

// ── 会话状态 ──
const sessions = ref<ChatSession[]>([])
const activeSessionId = ref('')
const activeSession = computed(() => sessions.value.find(s => s.id === activeSessionId.value) || null)
/**
 * ── 多会话运行：运行时状态**按会话**存 ──
 *
 * 背景（docs/multi-session-runtime-plan.md）：此前 `loading` / `items` 是**单值**，
 * 于是切会话必须打断上一个会话（清空 items + 无条件置 loading），
 * 而后台会话的 SSE 并没有被关闭 —— 它的事件会写进**当前**会话的列表（串台）。
 *
 * 手法：**存储按会话，对外仍是 `items` / `loading` 这两个名字** ——
 * 用 Vue 的**可写 computed** 代理到当前会话的切片。于是既有读写点
 * （`items.value` 50 处 + `loading.value` 19 处）**一行都不用改**：
 *   - 读 `items.value`              → 当前会话的数组（同一引用）
 *   - 写 `items.value = [...]`      → 走 setter，落到当前会话
 *   - 改内容 `items.value.push(x)`   → 直接改那个数组（引用不变，天然正确）
 *
 * `gen`（代际号）：会话运行时被**重建**（重开同一 id、重新建流）时递增。
 * 只比 `sessionId` 不够 —— 同一个 id 可能对应**前后两次生命周期**，
 * 旧回调必须靠 gen 才知道"我已经不属于这个会话了"
 * （参考 DeepSeek-Reasonix 的 `sessionGen` 与 `submissionBindingCurrent`）。
 */
interface SessionRunState {
  /** 该会话的消息列表：切换会话时**不再清空**，后台继续追加 */
  items: ChatItem[]
  /** 该会话是否正在生成（切走不影响它继续跑） */
  loading: boolean
  /** 代际号：该会话运行时被重建时 +1（用于丢弃旧回调） */
  gen: number
  /**
   * 该会话的**子 Agent 实时事件**（有界缓冲）。
   *
   * 此前它是模块级单值 `ref`，于是会话 A 的子 Agent 进度会**串到会话 B** 的
   * 观测面板与状态栏角标上（`items` / `loading` 早已按会话隔离，唯独漏了它）。
   * 放进 run state 后，"事件属于哪个会话"由 `withRun` 作用域自动决定 ——
   * `onSSEMessage` 本就在 `withRun(runOf(sessionId), …)` 内执行。
   */
  subagentEvents: SubagentEvent[]
}

/**
 * ⚠️ 必须是**响应式** Map：`items` / `loading` 是从这里派生的 computed，
 * 而流式输出靠 `items.value.push(...)` 追加 —— 底层若不是响应式的，push 不会触发
 * 任何重渲染，表现为"流式期间界面不动、消息结束后一次性跳出全部内容"。
 */
const runtimes = reactive(new Map<string, SessionRunState>())

function runOf(sid: string): SessionRunState {
  let run = runtimes.get(sid)
  if (!run) {
    runtimes.set(sid, { items: [], loading: false, gen: 1, subagentEvents: [] })
    // 必须取回 `runtimes.get()` 的**代理**，而不是刚构造的原始对象 ——
    // 原始对象不在响应式系统里，后续 push 不会触发更新。
    run = runtimes.get(sid)!
  }
  return run
}

/** 当前视图所属会话的运行时（`activeSessionId` 为空时用占位键，保持既有"无会话"行为） */
const currentRun = computed(() => runOf(activeSessionId.value || '__none__'))

/**
 * 写入作用域：SSE 回调在**同步**执行期间，指向"这条事件所属的会话"。
 *
 * 为什么安全：JS 单线程 + Vue 的渲染是**异步**的 —— 同步块内设置、返回前复位，
 * 于是模板渲染**永远看不到**它（绝不可能渲染错会话）。
 * 换来的是：`onSSEMessage` 内部二十多处 `items.value…` 的写法**一行都不用改**，
 * 却会写进**正确的**会话 —— 这才是真正修掉串台的那一步。
 *
 * （前提：`onSSEMessage` 全程同步，内部没有 `await`。）
 */
// ⚠️ 必须是 `shallowRef`（而不是普通变量）：`items` / `loading` 是 computed，
// 而 computed **只追踪求值过程中读到的响应式数据** —— 普通变量读不到、也不会令其失效，
// 于是 SSE 写回时 computed 会命中缓存、拿回**当前会话**的数组（作用域形同失效）。
// 用 shallowRef 后它参与依赖追踪；又因为 withRun 在同一同步块内设置并复位，
// 而 Vue 的渲染是**异步**的，净变化为零 —— 不会引起多余渲染。
const writingRun = shallowRef<SessionRunState | null>(null)

function withRun<T>(run: SessionRunState, fn: () => T): T {
  const prev = writingRun.value
  writingRun.value = run
  try {
    return fn()
  } finally {
    writingRun.value = prev
  }
}

/** 读/写都优先走作用域：SSE 回写时指向事件所属会话，其余时刻就是当前视图的会话 */
const loading = computed<boolean>({
  get: () => (writingRun.value ?? currentRun.value).loading,
  set: (v) => {
    ;(writingRun.value ?? currentRun.value).loading = v
  },
})

const items = computed<ChatItem[]>({
  get: () => (writingRun.value ?? currentRun.value).items,
  set: (v) => {
    ;(writingRun.value ?? currentRun.value).items = v
  },
})
let activeSSE: EventSource | null = null
// 每个会话最后收到的 SSE 事件 id（服务端 id: 行 → event.lastEventId）。
// 跨轮重建 SSE 时回传（last_event_id），服务端从缓冲流补发上一轮断线缺口（见 api/index.ts createSSEConnection）
const sseLastIdBySession = new Map<string, string>()

// ── Trace ID (当前会话的链路追踪标识) ──
const currentTraceId = ref('')  // SSE done 事件回传的 trace_id

// 安全修复：待确认工具调用（三态栅栏"确认"态）
interface PendingApproval {
  id: string
  toolName: string
  arguments: string
  /** 审批截止时间（ms）：与后端 300s 超时对齐，用于卡片倒计时与过期清理 */
  expiresAt?: number
}
const pendingApprovals = ref<PendingApproval[]>([])
// 审批卡片倒计时：approval 事件到达时记录截止时间（后端 _await_approval 默认 300s，
// 超时按"拒绝"处理），前端展示剩余时间并在过期后移除卡片。
const approvalTick = ref(0)
const APPROVAL_TIMEOUT_MS = 300_000
let approvalTimer: ReturnType<typeof setInterval> | null = null

function approvalRemain(a: any): number {
  void approvalTick.value // 依赖 tick 触发重算
  if (!a?.expiresAt) return 0
  return Math.max(0, Math.ceil((a.expiresAt - Date.now()) / 1000))
}

function ensureApprovalTimer() {
  if (approvalTimer) return
  approvalTimer = setInterval(() => {
    approvalTick.value++
    const now = Date.now()
    pendingApprovals.value = pendingApprovals.value.filter(
      (p) => !(p as any).expiresAt || (p as any).expiresAt > now,
    )
    if (pendingApprovals.value.length === 0 && approvalTimer) {
      clearInterval(approvalTimer)
      approvalTimer = null
    }
  }, 1000)
}

async function resolveApproval(a: PendingApproval, approved: boolean) {
  try {
    await submitApproval({
      session_id: activeSessionId.value || '',
      tool_call_id: a.id,
      approved,
    })
  } catch {
    // 静默失败
  } finally {
    pendingApprovals.value = pendingApprovals.value.filter(p => p.id !== a.id)
  }
}

// ── 结构化提问（ask_user 工具）────────────────────────────────────────────
// 与审批卡的分工：审批回传布尔（允许/拒绝），提问回传**答案文本**（选项值或自由输入），
// 因此走独立的 /v1/agent/answer 通道而不是复用 approval。
interface PendingQuestion {
  id: string
  question: string
  options: string[]
  allowFreeText: boolean
}

const pendingQuestions = ref<PendingQuestion[]>([])

async function answerQuestion(question: PendingQuestion, answer: string) {
  pendingQuestions.value = pendingQuestions.value.filter(item => item.id !== question.id)
  try {
    await submitAnswer({
      session_id: activeSessionId.value || '',
      tool_call_id: question.id,
      answer,
    })
  } catch {
    message.error(t('回答提交失败，请重试'))
  }
}

// ── 工具授权模式（ask/auto/yolo）──────────────────────────────────────────
// 与下方「对话模式」(mode: normal/minimal/ptc/creative) 是两个不同维度：
// 本项控制**工具执行是否需要用户确认**，状态存后端 Redis（多副本一致），
// 实际判定在 Python 侧 guards.py（模式读取失败会 fail-safe 到最严格的 ask）。
const toolsMode = ref<'ask' | 'auto' | 'yolo'>('auto')
const toolsModeOptions = [
  { label: t('询问'), value: 'ask' },
  { label: t('自动'), value: 'auto' },
  { label: t('全自动'), value: 'yolo' },
]

async function loadToolsMode(sessionId: string) {
  if (!sessionId) {
    toolsMode.value = 'auto'
    return
  }
  try {
    const m = await getSessionMode(sessionId)
    if (m === 'ask' || m === 'auto' || m === 'yolo') toolsMode.value = m
  } catch {
    // 读取失败：界面保持 auto；判定侧会按 fail-safe 取最严格模式
  }
}

async function onToolsModeChange(v: any) {
  const m = String(v) as 'ask' | 'auto' | 'yolo'
  toolsMode.value = m
  const sid = activeSessionId.value
  if (!sid) {
    message.warning(t('请先创建或选择会话，再设置工具授权模式'))
    return
  }
  try {
    await setSessionMode(sid, m)
    message.success(
      m === 'yolo'
        ? '已切换为全自动：跳过工具确认（该操作会留审计）'
        : `工具授权模式已设为「${toolsModeOptions.find(o => o.value === m)?.label || m}」`,
    )
  } catch {
    message.error(t('工具授权模式保存失败'))
  }
}

// 模式
const modeOptions = [
  { label: t('常规'), value: 'normal' },
  { label: t('极简'), value: 'minimal' },
  { label: 'PTC', value: 'ptc' },
  { label: t('创意'), value: 'creative' },
]
/** 默认模式：会话没记（或记了个不认识的值）时回落到它 —— **绝不能沿用上一个会话的模式** */
const DEFAULT_MODE = 'normal'
const mode = ref(DEFAULT_MODE)

// ── 模型路由：会话 llm_config.model（空 = 后端默认路由） ──
const llmModel = ref('')

// ── 对话模式预设：mode → temperature/max_tokens + 用户可见的**一句话定位** ──
// desc 必须与后端 app/agent/modes.py 的四种模式定义表一一对应（工具集/上下文/压缩差异），
// 否则又会回到"切了看不出区别"的老问题（2026-09 实测暴露）。
const MODE_PRESETS: Record<string, { temperature: number; max_tokens: number; note?: string; desc: string }> = {
  normal: { temperature: 0.6, max_tokens: 4096, desc: '通用助手：12 个核心工具，注入记忆/技能/知识库上下文' },
  minimal: { temperature: 0.2, max_tokens: 1024, note: '简短回复', desc: '极简：只用 read_file/edit_file/shell_exec，不注入上下文、不压缩' },
  ptc: { temperature: 0.4, max_tokens: 4096, note: '分步思考', desc: 'PTC：多步操作写成一段程序一次执行（run_code），少往返、省 token' },
  creative: { temperature: 1.0, max_tokens: 8192, desc: '创意：可读写平台自身的模式/技能定义（mode_list/mode_edit）' },
}

/** 构建 llm_config：mode + 对应预设 temperature/max_tokens + 模型路由 model（base 已显式携带的字段优先保留） */
/**
 * 思考档位：`disabled / low / high / max`（与后端 `app/providers/effort.py` 的归一化表对齐）。
 * 空串 = 不携带 —— 是否真正发送由后端归一化决定（各家词表不一致，见该文件注释）。
 */
const effort = ref('')

/** 档位循环顺序（点击按钮依次切换 —— ZCode 的 `ThoughtLevelCycleControl` 同款交互，比下拉省空间） */
const EFFORT_ORDER = ['', 'low', 'high', 'max'] as const
const EFFORT_LABEL: Record<string, string> = { '': '关', low: '低', high: '高', max: '最高' }
// 模板不直接做两层索引：EFFORT_LABEL 与 effort 各自都可能为空，一旦编译产物出现不一致
// （排查过：HMR 增量重编译后 setup 绑定表与 render 不同步），EFFORT_LABEL[effort] 会炸成
// undefined[undefined]，整个 ChatView 被 ErrorBoundary 接管。收进 computed 后取值路径只有一条。
const effortLabel = computed(() => EFFORT_LABEL[effort.value] || '关')

/** 思考档位切换：与模型/模式同一套持久化（走 buildLlmConfig → 会话 llm_config） */
function onEffortChange(v: string) {
  effort.value = (EFFORT_ORDER as readonly string[]).includes(v) ? v : ''
  persistRuntime({})
  message.info(
    effort.value ? `思考档位：${EFFORT_LABEL[effort.value]}` : t('思考档位已重置为默认'),
  )
}

/** 点按循环：关 → 低 → 高 → 最高 → 关 */
function cycleEffort() {
  const order = EFFORT_ORDER as readonly string[]
  onEffortChange(order[(order.indexOf(effort.value) + 1) % order.length])
}

/** 构建 llm_config：mode + 对应预设 temperature/max_tokens + 模型路由 model + 思考档位 */
function buildLlmConfig(base?: Record<string, any>): Record<string, any> {
  const cfg: Record<string, any> = { mode: mode.value, ...(base || {}) }
  if (effort.value) cfg.effort = effort.value
  // 模型路由：会话选定模型写入 llm_config（空 = 不携带，走后端默认路由）
  if (llmModel.value) cfg.model = llmModel.value
  const preset = MODE_PRESETS[mode.value]
  if (preset) {
    if (cfg.temperature === undefined) cfg.temperature = preset.temperature
    if (cfg.max_tokens === undefined) cfg.max_tokens = preset.max_tokens
  }
  return cfg
}

/**
 * 把运行时状态写进**单一事实源**（P1：Redis 热 + `unified_sessions.runtime` 持久）。
 *
 * 会话已建立时同时写 `/v1/sessions/{id}/runtime`（刷新/重开会话都不丢，且提交链路
 * 按 spec §3 的解析链读取）与 `llm_config`（老客户端与其他页面仍按此口径读取）。
 */
function persistRuntime(patch: Record<string, unknown>) {
  const sid = activeSessionId.value
  if (!sid) return
  void putSessionRuntime(sid, patch).catch(() => {})
  void updateConversation(sid, { llm_config: buildLlmConfig() } as any).catch(() => {})
}

/**
 * `@` 提及选中的资源 → 加进"激活引用"的 chips，并**同步到单一事实源**（`runtime.context`）。
 *
 * 与 URL chips 走同一条链路：chips 只是 UI 呈现，`runtime.context` 才是提交时真正生效的东西
 * —— 只改 chips 不写 runtime，刷新后引用就会从界面上消失（这正是我们修过的那类问题）。
 */
const MENTION_CHIP_LABEL: Record<string, string> = {
  kb: '知识库', agent: 'Agent', skill: '技能', workflow: '工作流', plugin: '插件',
}

function onMentionAdd(p: { type: string; id: string; name: string }) {
  if (contextChips.value.some(c => c.type === p.type && c.value === p.id)) return
  contextChips.value = [
    ...contextChips.value,
    {
      type: p.type as ContextChip['type'],
      label: `${MENTION_CHIP_LABEL[p.type] || p.type} ${p.name}`,
      value: p.id,
    },
  ]
  persistRuntime({ context: buildWorkbenchContext(contextChips.value) })
}

/** 模型切换：更新 llmModel ref + 写入运行时状态（会话级持久） */
function onModelChange(m: string) {
  if (m === llmModel.value) return
  llmModel.value = m
  message.info(m ? `模型已切换：${m}（仅影响后续消息）` : t('模型已重置为默认（后端路由）'))
  persistRuntime({ model: m || null })
}

/** 模式切换：更新 mode ref + 写入运行时状态（会话级持久） */
function onModeChange(m: string) {
  if (m === mode.value) return
  mode.value = m
  const opt = modeOptions.find(o => o.value === m)
  const preset = MODE_PRESETS[m]
  message.info(`已切换到「${opt?.label || m}」模式${preset?.desc ? `：${preset.desc}` : ''}，仅影响后续消息`)
  persistRuntime({ mode: m })
}

/** 归一化后的 metadata（可能为 JSON 字符串或对象） */

// ── 互联互通：统一任务模式 + 上下文芯片（与 SSE 流式并列的新路径） ──
// 路由 query 约定（由 WorkstationNav / 各工作台入口发起）：
//   ?task=<sessionId>          统一会话（拉历史 + 继续追问）
//   ?task=&error=xxx           仅错误提示
//   ?kb=<id> / ?agent=<id> / ?skill=<name> / ?workflow=<id|name>   上下文附加
//   同名参数可重复（?kb=a&kb=b）=> 同类可多选
//   ?mode=<auto|agent|workflow> 创建时模式（WorkflowView 为 workflow）
const contextChips = ref<ContextChip[]>([])
const errorBanner = ref('')          // query.error 提示
const unifiedSessionId = ref('')     // 统一任务会话 id（query.task）
const unifiedSubmitMode = ref('auto') // 会话创建时的 mode（shared_context.mode 优先）
// P1-e：**统一任务模式（TaskRouter 自动编排）已并入常规对话链路**。
// 常规链路（SSE /submit → AgentTask）才是模式/模型/工具集/上下文全部生效的那条；
// 保留两条会让"切换模式/模型"看起来只对其中一条起作用（见 docs/session-runtime-spec.md）。
// 恒为 false 即停用分流，unifiedSessionId 等相关代码保留以便回退。
const unifiedMode = computed(() => false)
// 纯展示 flag：任务提交成功时，徽标短暂过渡到"完成"态后复位
const unifiedJustFinished = ref(false)
let unifiedDoneTimer: ReturnType<typeof setTimeout> | null = null
function flashUnifiedDone() {
  unifiedJustFinished.value = true
  if (unifiedDoneTimer) clearTimeout(unifiedDoneTimer)
  unifiedDoneTimer = setTimeout(() => { unifiedJustFinished.value = false }, 1600)
}
let appliedQueryKey = ''

async function applyRouteQuery() {
  const q = route.query
  const key = JSON.stringify(q)
  if (key === appliedQueryKey) return
  appliedQueryKey = key

  const task = typeof q.task === 'string' && q.task.trim() ? q.task.trim() : ''
  // task 变更 / 退出统一模式时重置消息区（避免污染普通 SSE 会话）
  if (task !== unifiedSessionId.value) {
    unifiedSessionId.value = task
    items.value = []
    activeSessionId.value = ''
    currentTraceId.value = ''
    loading.value = false
    stopTurnTimer()
    if (activeSSE) { activeSSE.close(); activeSSE = null }
    if (task) await loadUnifiedSession(task)
  }
  errorBanner.value = typeof q.error === 'string' && q.error ? q.error : ''
  // 跨台活动直达：?session=<id> 时切到那个会话（"最近活动"里点对话记录会带它过来）。
  // 统一任务模式（task）下不切，避免与 loadUnifiedSession 抢消息区。
  const target = typeof q.session === 'string' ? q.session.trim() : ''
  if (target && !task && target !== activeSessionId.value) {
    try {
      await switchSession(target)
    } catch {
      // 会话已删 / 非本人：留在当前会话，不打扰
    }
  }
  await initContextChips(q)
  void applyChatPrefill()
}

/**
 * 「在对话中继续」的落地端：Agent 会话 / 工作流结果经 sessionStorage 投递到这里，
 * 插入输入框（不自动发送）—— 用户可以先修改再发。
 */
async function applyChatPrefill() {
  const prefill = takeChatPrefill()
  if (!prefill) return
  const text = buildPrefillText(prefill)
  await nextTick()
  if (chatInputRef.value?.insertText) {
    chatInputRef.value.insertText(text)
  } else {
    // 输入框还没挂载（首帧）：把投递放回去，别让用户的那一次点击被静默吞掉
    setChatPrefill(prefill)
  }
}

async function initContextChips(q: Record<string, any>) {
  // URL 约定与多值解析统一在 contextChips 模块里（同名参数可重复 => 可多选）
  const chips = parseContextQuery(q)
  const kb = chips.find(c => c.type === 'kb')?.value || ''
  const agent = chips.find(c => c.type === 'agent')?.value || ''
  const skill = chips.find(c => c.type === 'skill')?.value || ''
  const workflow = chips.find(c => c.type === 'workflow')?.value || ''
  contextChips.value = chips
  // ── 尽力补全展示用的名称（失败则保留 id 占位；Agent 配置本身由网关按 id 补全）──
  if (kb) {
    try {
      const res = await api.get(`/v1/kb/${encodeURIComponent(kb)}`)
      const d = res.data?.data || res.data
      if (d?.name) {
        const c = contextChips.value.find(x => x.type === 'kb')
        if (c) c.label = `知识库 ${d.name}`
      }
    } catch { /* 保留 id 占位 */ }
  }
  if (agent) {
    try {
      const res = await api.get('/v1/agents')
      const list = res.data?.data || []
      const a = list.find((x: any) => x.id === agent)
      if (a?.name) {
        const c = contextChips.value.find(x => x.type === 'agent')
        if (c) c.label = `Agent ${a.name}`
      }
    } catch { /* 列表取不到就保留 id 占位 */ }
  }
  if (workflow) {
    try {
      const res = await api.get('/v1/graphs')
      const list = res.data?.data || []
      const rec = list.find((x: any) => x.id === workflow)
      if (rec?.name) {
        const c = contextChips.value.find(x => x.type === 'workflow')
        if (c) c.label = `工作流 ${rec.name}`
      }
    } catch { /* 无列表时保留原文 */ }
  }
}

/** 移除单个上下文芯片：本地 context 与路由 query 双源同步（侧栏上下文面板触发） */
function removeContextChip(type: ContextChip['type'], value: string) {
  contextChips.value = contextChips.value.filter(c => !(c.type === type && c.value === value))
  const remaining = contextChips.value.filter(c => c.type === type).map(c => c.value)
  const q: Record<string, any> = { ...route.query }
  // 同类还有剩余值时改写该项（多值即数组），否则整项删除
  if (remaining.length === 0) {
    if (q[type] === undefined) return
    delete q[type]
  } else {
    q[type] = remaining.length === 1 ? remaining[0] : remaining
  }
  void router.replace({ path: '/chat', query: q })
  appliedQueryKey = JSON.stringify(q)
}

/** 清空全部上下文：本地 context 与路由 query 一并清除（键取 PARAMS 定义，避免新增类型时漏清） */
function clearContext() {
  contextChips.value = []
  const q: Record<string, any> = { ...route.query }
  let changed = false
  for (const key of CONTEXT_QUERY_KEYS) {
    if (q[key] !== undefined) { delete q[key]; changed = true }
  }
  if (changed) {
    void router.replace({ path: '/chat', query: q })
    appliedQueryKey = JSON.stringify(q)
  }
}

/** 统一任务模式：清空当前消息区（保留会话与上下文，可继续追问） */
function clearUnifiedMessages() {
  items.value = []
  currentTraceId.value = ''
  message.info(t('已清空统一任务消息'))
}

/** 统一任务模式：退出（移除 task/error query；路由 watcher 触发 applyRouteQuery 重置消息区） */
async function exitUnifiedMode() {
  const q: Record<string, any> = { ...route.query }
  delete q.task
  delete q.error
  await router.replace({ path: '/chat', query: q })
}

/** kb_hits 标签增强：跳转到引用的知识库详情 */
function openKb(kbId: string) {
  if (kbId) void router.push(`/knowledge/${encodeURIComponent(kbId)}`)
}

/** 组装发送时附带的 context（普通 SSE 模式与统一任务模式共用） */
function buildContext(): Record<string, any> | undefined {  // 单值字段 + 多值数组的组装规则集中在 contextChips 模块（新旧后端都能工作）。
  //
  // Agent 只发 agent_id，配置由网关按 id 补全（internal/api/agents.go 的
  // resolveAgentContext）。前端此前自己映射一份字段，且只映射了
  // name / system_prompt / model / max_turns —— Agent 自带的 tools / kb_id / skills
  // 全部丢失，用户选了 Agent 却发现"它不会用自己的工具"。把单一事实来源放回后端，
  // 两端就不会各自漂移。
  return buildWorkbenchContext(contextChips.value)
}

/** 安全改造：附件签名 URL 解析，/media/ 公开路径转短时效签名 URL；非 /media/ 前缀原样；失败回退原 url */
async function resolveAttachmentUrls(attachments?: ChatAttachment[]): Promise<ChatAttachment[]> {
  if (!attachments?.length) return []
  return Promise.all(attachments.map(async a => {
    if (!a.url || !a.url.startsWith('/media/')) return a
    const url = await resolveMediaUrl({ id: a.id, file_url: a.url })
    return url && url !== a.url ? { ...a, url } : a
  }))
}

/** 拉取统一会话历史（GET /v1/chat/sessions/{id}/messages） */
async function loadUnifiedSession(sessionId: string) {
  loading.value = true
  try {
    const res = await getChatSessionMessages(sessionId)
    const d = (res?.messages ? res : (res?.data || {})) as any
    const list = Array.isArray(d.messages) ? d.messages : []
    // 会话创建时的 mode（shared_context 优先，其次 query.mode，兜底 auto）
    const sharedMode = d.shared_context?.mode
    unifiedSubmitMode.value =
      (typeof sharedMode === 'string' && sharedMode) ||
      (typeof route.query.mode === 'string' && route.query.mode) ||
      'auto'
    items.value = buildUnifiedItems(list)
  } catch {
    errorBanner.value = errorBanner.value || '统一会话加载失败，可直接发送消息继续'
  } finally {
    loading.value = false
    // 会话加载完成后自动滚到底部
    await nextTick()
    const listEl = document.querySelector<HTMLElement>('.message-list')
    if (listEl) listEl.scrollTop = listEl.scrollHeight
  }
}

/** 统一会话消息 → 现有 ChatItem（user/assistant 映射现有消息组件；metadata 含 kb 时插知识库引用标签） */
function buildUnifiedItems(list: any[]): ChatItem[] {
  const out: ChatItem[] = []
  ;(list || []).forEach((m: any, idx: number) => {
    if (!m || (m.role !== 'user' && m.role !== 'assistant')) return
    const content = typeof m.content === 'string' ? m.content : ''
    if (!content) return
    const time = formatClock(m.timestamp || m.created_at)
    if (m.role === 'user') {
      out.push({
        kind: 'text', role: 'user', content: stripUserInputTag(content), time, id: `uni_u_${idx}`,
        // 来源标记（'subagent_followup' = 子 Agent 自动轮）：带上它，刷新后仍渲染成
        // 系统提示而不是用户气泡（见 internal/api/agent_followup.go）
        ...(m.source ? { source: m.source } : {}),
      })
    } else {
      const { reasoning, body } = splitThinking(content, { loose: true })
      if (reasoning) out.push({ kind: 'reasoning', content: reasoning, time, id: `uni_r_${idx}` })
      if (body) {
        out.push({
          kind: 'text', role: 'assistant', content: body, time, id: `uni_a_${idx}`,
          metadata: normalizeMeta(m.metadata),
        } as any)
        const meta = normalizeMeta(m.metadata) || {}
        const n = typeof meta.kb_hits === 'number' ? meta.kb_hits : meta.kb_id ? 1 : 0
        if (meta.kb_id || n > 0) {
          out.push({ kind: 'kb_hits', count: n, kb_id: meta.kb_id || '', id: `uni_k_${idx}` } as unknown as ChatItem)
        }
      }
    }
  })
  return out
}

/** 统一任务模式发送：POST /v1/chat/submit，返回 output 追加为 assistant 消息 */
async function sendUnified(text: string, attachments?: ChatAttachment[]) {
  if (!unifiedSessionId.value) return
  loading.value = true
  startTurnTimer()
  appendUserText(text, attachments)
  const userItemId = items.value[items.value.length - 1]?.id
  currentTraceId.value = ''
  try {
    // 安全改造：附件若为 /media/ 公开路径，先解析为签名 URL 再随消息发送（loading 期间发送已禁用）
    const resolvedAtts = await resolveAttachmentUrls(attachments)
    const res = await api.post('/v1/chat/submit', {
      message: text,
      session_id: unifiedSessionId.value,
      mode: unifiedSubmitMode.value || 'auto',
      context: buildContext(),
      // 模型路由：统一任务发送同样携带 llm_config.model（空 = 后端默认）
      llm_config: llmModel.value ? { model: llmModel.value } : {},
      ...(resolvedAtts.length
        ? { attachments: resolvedAtts.map(a => ({ id: a.id, name: a.name, mime_type: a.mimeType, url: a.url, is_image: a.isImage })) }
        : {}),
    })
    const d = res.data?.data !== undefined ? res.data.data : (res.data || {})
    if (d.success === false) throw new Error(d.error || '请求失败')
    currentTraceId.value = d.trace_id || ''
    appendAssistantWithKb(d.output || '', d.metadata || {})
    flashUnifiedDone()
  } catch (e: any) {
    const reason = describeApiError(e)
    markMessageFailed(userItemId, reason)
    message.error(t('发送失败：') + reason)
  } finally {
    loading.value = false
    stopTurnTimer()
  }
}

/** 追加 assistant 消息；metadata 含 kb_hits/kb_id 时在其下显示"引用了知识库(×N)"小标签 */
function appendAssistantWithKb(content: string, meta: any) {
  // 统一任务模式的 output 同为引擎产出（可含多段 [thinking] 块）→ 用 loose 状态机解析
  const { reasoning, body } = splitThinking(String(content), { loose: true })
  if (reasoning) items.value.push({ kind: 'reasoning', content: reasoning, id: genItemId() })
  if (body) {
    items.value.push({
      kind: 'text', role: 'assistant', content: body, id: genItemId(),
      metadata: normalizeMeta(meta),
    } as any)
    const n = typeof meta.kb_hits === 'number' ? meta.kb_hits : meta.kb_id ? 1 : 0
    if (meta.kb_id || n > 0) {
      items.value.push({ kind: 'kb_hits', count: n, kb_id: meta.kb_id || '', id: genItemId() } as unknown as ChatItem)
    }
  }
}

// 统一任务模式：新消息后自动滚到底部
watch(() => items.value.length, async () => {
  if (!unifiedMode.value) return
  await nextTick()
  const el = document.querySelector<HTMLElement>('.unified-list')
  if (el) el.scrollTop = el.scrollHeight
})

// 侧面板（主从时间线：轨迹 / 会话历史）；上下文面板：桌面端（>1025px）默认展开常驻，≤1024px 折叠为抽屉
const panelOpen = ref(window.matchMedia('(min-width: 1025px)').matches)
const panelView = ref<'trajectory' | 'sessions' | 'agents' | 'stats'>('trajectory')
/**
 * 会话地图：**独立的整屏大窗格**（不是侧栏里的一个小视图 —— 侧栏太窄，
 * 放不下"空间化排布"这件事本身）。位置稳定性是它的立身之本，
 * 见 docs/session-map-plan.md §〇。
 */

/**
 * 新版会话地图：直接嵌入照抄自 `vendor/dsh-synapse` 的前端（`public/sessionmap/`），
 * 数据面由 `adapter.js` 缝合到 `/v1/session-map`（布局，Redis 热层 + 异步落 PG）与
 * `/v1/conversations`（会话/消息/fork）。
 *
 * 它取代了原先纯 localStorage 的自由摆放地图：新版多了
 * "按真实分支连线"（依赖 `sessions.parent_session_id`）与 SVG 连线层，
 * 旧版先留着作为可回退路径。
 */
const synapseMapOpen = ref(false)

/**
 * 地图上标「运行中」的会话。
 *
 * 此前 ChatView 从不传 `runningIds`，于是地图里的 `.is-running` 与脉冲徽标是死代码。
 * 数据来源是子 Agent 的实时事件流：某会话出现 run 即标记为运行中，收到 `subagent.done`
 * 才清除 —— 多个 run 并发时会话始终保持标记（与"是否还有未终态的 run"同义）。
 */
const runningSessionIds = computed<string[]>(() => {
  const open = new Set<string>()
  for (const raw of subagentLiveEvents.value) {
    const e = raw as { session_id?: string; run_id?: string; type?: string }
    const sid = e?.session_id
    if (!sid || !e?.run_id) continue
    if (e.type === 'subagent.done') open.delete(sid)
    else open.add(sid)
  }
  return [...open]
})

/**
 * 新版地图（iframe）→ 宿主的回传通道。
 *
 * 移植的前端点节点时会 `post("synapse:activate-session", {sessionId})` 到父窗口 ——
 * 原本是让 DSH 切会话，这里对应我们自己的 `switchSession`，并顺手关掉浮层
 * （与旧版地图 `select` → `switchSession` 的行为一致）。
 *
 * 监听放在模块级：ChatView 是主视图，生命周期与应用一致，无需在挂载/卸载之间来回摘挂。
 */
/**
 * 新版地图 iframe 的句柄 + 「宿主 → 地图」的单向状态推送。
 *
 * 协议来自上游 app.js 末尾的 window.addEventListener('message')，它认这些消息：
 *   synapse:workspaces      → 画布下拉（我们映射 /v1/session-map/workspaces）
 *   synapse:current-session → 高亮/居中到当前会话
 *   synapse:theme           → 亮/暗主题
 *   synapse:live-reply      → { sessionId, running, text } 卡片运行态与流式文本
 * 地图侧打开时会先发 `synapse:request-current` 握手，我们再回推上面这些。
 *
 * 注意 source 标识必须与 app.js 一致（chiron-sessionmap）。
 */
const synapseFrame = ref<HTMLIFrameElement | null>(null)
const MAP_SOURCE = 'chiron-sessionmap'
/**
 * 流式增量 → 会话地图（P0-1）。
 *
 * 后端 `submit_handler.go` 已把引擎增量按 50ms 合帧 publish 成
 * `Event{Type:"text", SessionID, Data:{Content}}`，经 `/v1/events` 直接送到这里。
 * 地图（iframe）认的协议是 `synapse:live-reply{sessionId, running, text}`（累积文本），
 * app.js 会写进 state.liveReplies 并**就地 patch 卡片**（app.js:728）——
 * 于是卡片逐字生长，而不是等回合结束才一次性出现。
 *
 * 累积器独立于 items（按 sessionId 存）：地图可能要展示**非当前**会话的进度，
 * 而 items 只反映当前视图。
 */
const mapStreamText = new Map<string, string>()

function forwardStreamToMap(fallbackSessionId: string, raw: unknown) {
  const d = raw as { type?: string; session_id?: string; sessionId?: string; data?: { content?: string } } | null
  if (!d || !d.type) return
  const sid = d.session_id || d.sessionId || fallbackSessionId
  if (!sid) return
  if (d.type === 'text') {
    const chunk = d.data?.content
    if (typeof chunk !== 'string' || !chunk) return
    const acc = (mapStreamText.get(sid) ?? '') + chunk
    mapStreamText.set(sid, acc)
    postToMap({ type: 'synapse:live-reply', sessionId: sid, running: true, text: acc })
    return
  }
  if (d.type === 'turn_done' || d.type === 'error') {
    mapStreamText.delete(sid)
    postToMap({ type: 'synapse:live-reply', sessionId: sid, running: false })
  }
}


/** 上一次推送过的运行中会话，用于算差集（声明必须早于使用它的 pushMapState） */
let lastRunningIds: string[] = []

function postToMap(payload: Record<string, unknown>) {
  const target = synapseFrame.value?.contentWindow
  if (!target) return
  target.postMessage({ source: MAP_SOURCE, ...payload }, window.location.origin)
}

/** 把宿主状态推给地图 */
async function pushMapState() {
  // 画布列表：地图的"工作区"下拉（空 sessionIds 让它自己去拉画布内容）
  try {
    const r = await fetch('/v1/session-map/workspaces', { credentials: 'include' })
    const body = (await r.json()) as { data?: { workspaces?: Array<{ id: string; name?: string }> } }
    postToMap({
      type: 'synapse:workspaces',
      workspaces: (body.data?.workspaces ?? []).map((w) => ({
        id: w.id,
        title: w.name || '我的地图',
        sessionIds: [] as string[],
      })),
    })
  } catch {
    // 拉不到就让它退回空下拉，不影响画布本身
  }
  postToMap({
    type: 'synapse:current-session',
    session: activeSessionId.value ? { id: activeSessionId.value } : null,
  })
  const root = document.documentElement
  postToMap({
    type: 'synapse:theme',
    dark: root.classList.contains('dark') || root.dataset.theme === 'dark',
  })
  for (const id of runningSessionIds.value) {
    postToMap({ type: 'synapse:live-reply', sessionId: id, running: true, text: '' })
  }
  lastRunningIds = [...runningSessionIds.value]
}


window.addEventListener('message', (e: MessageEvent) => {
  if (e.origin !== window.location.origin) return // 只认自己域（iframe 同源）
  const data = e.data as { source?: string; type?: string; sessionId?: string } | null
  if (!data || data.source !== MAP_SOURCE) return
  // 地图打开 / 请求当前状态：把宿主状态推过去
  if (data.type === 'synapse:map-opened' || data.type === 'synapse:request-current') {
    void pushMapState()
    return
  }
  // 点卡片 → 切会话并关掉浮层
  if (data.type === 'synapse:activate-session' && data.sessionId) {
    synapseMapOpen.value = false
    void switchSession(data.sessionId)
  }
})
const trajectoryFocus = ref<number | null>(null)
const trajectoryToken = ref(0)
// 子 Agent 实时事件缓冲（有界）：SSE 里的 `subagent.*` 分流到这里，
// 只供侧边栏观测面板消费，绝不混入主对话流（docs/subagent-design.md §4.2）。
//
// ⚠️ 按**会话**存（与 items / loading 同一套）：真实存储落在 SessionRunState 上，
// 对外仍是 `subagentLiveEvents` 这个名字 —— 用可写 computed 代理到"当前会话"的切片，
// 于是既有读写（push / length / 模板传参）一行都不用改。
// 此前是模块级单值 ref，会让会话 A 的子 Agent 进度串到会话 B 的面板与角标。
const subagentLiveEvents = computed<SubagentEvent[]>({
  get: () => (writingRun.value ?? currentRun.value).subagentEvents,
  set: (v) => {
    ;(writingRun.value ?? currentRun.value).subagentEvents = v
  },
})

// ⚠️ 必须放在 `subagentLiveEvents` 定义**之后**：watch 在 setup 期间就会同步求值一次 source，
// 而 runningSessionIds 读的正是 subagentLiveEvents —— 放在前面会 TDZ（Cannot access before initialization）。
/** 运行态变化 → 推给地图；结束时地图会自行重拉画布，把新消息带出来 */
watch(() => runningSessionIds.value, (ids) => {
  for (const id of ids) {
    if (!lastRunningIds.includes(id)) postToMap({ type: 'synapse:live-reply', sessionId: id, running: true, text: '' })
  }
  for (const id of lastRunningIds) {
    if (!ids.includes(id)) postToMap({ type: 'synapse:live-reply', sessionId: id, running: false })
  }
  lastRunningIds = [...ids]
})
const SUBAGENT_LIVE_MAX = 500

function onTrajectoryFocus(index: number) {
  trajectoryFocus.value = index
  trajectoryToken.value += 1
}

// 打开面板并直达指定视图；点击已激活的入口则收起
function openPanel(view: 'trajectory' | 'sessions' | 'agents' | 'stats') {
  if (panelOpen.value && panelView.value === view) {
    panelOpen.value = false
    return
  }
  panelView.value = view
  panelOpen.value = true
}

/** ChatInput「上下文」快捷按钮：确保上下文面板展开（抽屉模式下亦然）并直达轨迹视图 */
function openContextPanel() {
  panelView.value = 'trajectory'
  panelOpen.value = true
}

// turn 计时（deepseek turnStatusClock）
const turnElapsed = ref(0)
const connectionLost = ref(false)  // SSE 断线横幅（deepseek ConnectionBanner）
let turnTimer: ReturnType<typeof setInterval> | null = null

function startTurnTimer() {
  turnElapsed.value = 0
  if (turnTimer) clearInterval(turnTimer)
  turnTimer = setInterval(() => { turnElapsed.value += 1 }, 1000)
}

function stopTurnTimer() {
  if (turnTimer) { clearInterval(turnTimer); turnTimer = null }
}

function persistSessions() { localStorage.setItem('chat_sessions', JSON.stringify(sessions.value)) }

// ── 会话 CRUD（保留原逻辑） ──
onMounted(async () => {
  // 互联互通：解析 /chat query（task / error / kb / agent / skill / workflow）
  await applyRouteQuery()
  await loadSessions()
  // 统一任务模式不自动切换普通会话；其余保持原有行为
  if (!unifiedMode.value && sessions.value.length > 0) {
    await switchSession(sessions.value[0].id)
  }
  // 互联互通：同一路由下 query 变化（如 WorkstationNav 再次跳转）
  watch(() => route.query, () => applyRouteQuery())
  // 监听网络在线/离线状态
  window.addEventListener('online', onOnline)
  window.addEventListener('offline', onOffline)
  // 全局键盘快捷键
  window.addEventListener('keydown', onGlobalKeydown)
})

onUnmounted(() => {
  stopTurnTimer()
  if (activeSSE) { activeSSE.close(); activeSSE = null }
  if (unifiedDoneTimer) { clearTimeout(unifiedDoneTimer); unifiedDoneTimer = null }
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
  window.removeEventListener('online', onOnline)
  window.removeEventListener('offline', onOffline)
  window.removeEventListener('keydown', onGlobalKeydown)
})

// 离线监听 + 自动重连
const isOnline = ref(navigator.onLine)
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let reconnectAttempts = 0

function onOffline() {
  isOnline.value = false
  connectionLost.value = true
  // 离线时停止 SSE 重试
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
}

function onOnline() {
  isOnline.value = true
  // 上线后指数退避重连，恢复会话
  if (reconnectTimer) clearTimeout(reconnectTimer)
  reconnectAttempts = 0
  attemptReconnect()
}

async function attemptReconnect() {
  if (!isOnline.value) return
  reconnectAttempts++
  // 指数退避：1s, 2s, 4s, 8s, 16s（最大 16s）
  const delay = Math.min(1000 * Math.pow(2, reconnectAttempts - 1), 16000)
  if (reconnectAttempts > 1) {
    await new Promise(r => setTimeout(r, delay))
  }
  if (!isOnline.value) return
  try {
    await api.get('/health', { timeout: 5000 })
    connectionLost.value = false
    reconnectAttempts = 0
    if (activeSessionId.value) {
      await switchSession(activeSessionId.value)
    }
  } catch {
    if (reconnectAttempts < 5) {
      reconnectTimer = setTimeout(attemptReconnect, delay)
    }
  }
}

// 导出当前会话为 Markdown 文件
function exportMarkdown() {
  if (!items.value.length) {
    message.warning(t('当前没有可导出的消息'))
    return
  }
  const session = sessions.value.find(s => s.id === activeSessionId.value)
  const title = session?.title || '对话导出'
  const lines: string[] = [`# ${title}`, '']
  for (const it of items.value) {
    if (it.kind !== 'text') continue
    const role = it.role === 'user' ? '🧑 用户' : '🤖 助手'
    lines.push(`## ${role}`, '')
    lines.push(it.content || '(空消息)')
    if (it.attachments?.length) {
      lines.push('')
      for (const a of it.attachments) {
        if (a.isImage) lines.push(`![${a.name}](${a.url})`)
        else lines.push(`- 📎 [${a.name}](${a.url}) (${formatSize(a.size)})`)
      }
    }
    lines.push('')
  }
  const toolCalls = items.value.filter(i => i.kind === 'tool_call')
  if (toolCalls.length) {
    lines.push('---', '', '## 工具调用记录', '')
    for (const tc of toolCalls) {
      if (tc.kind !== 'tool_call') continue
      lines.push(`### ${tc.name || 'tool'}`, '```json', tc.arguments || '{}', '```', '')
    }
  }
  const md = lines.join('\n')
  const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${title.replace(/[\\/:*?"<>|]/g, '_')}.md`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
  message.success(t('已导出 Markdown'))
}

// 斜杠命令处理
function onSlashCommand(cmd: string) {
  switch (cmd) {
    case '/clear':
      items.value = []
      activeSessionId.value = ''
      message.info(t('已清空当前对话'))
      break
    case '/export':
      exportMarkdown()
      break
    case '/new':
      items.value = []
      activeSessionId.value = ''
      panelOpen.value = false
      message.info(t('已新建会话'))
      break
    case '/theme':
      themeStore.toggleTheme()
      message.success(themeStore.isDark ? t('已切换到暗色模式') : t('已切换到亮色模式'))
      break
    case '/stop':
      stopGeneration()
      break
    default:
      message.warning(`未知命令: ${cmd}`)
  }
}

// 全局键盘快捷键
function onGlobalKeydown(e: KeyboardEvent) {
  // Ctrl/Cmd + K：打开侧边栏 + 切到会话历史视图
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    panelOpen.value = true
    panelView.value = 'sessions'
    nextTick(() => {
      const searchInput = document.querySelector('.panel-search .search-input') as HTMLInputElement | null
      searchInput?.focus()
    })
  }
  // Ctrl/Cmd + F：**会话内查找**。
  // 有意覆盖浏览器的页面查找：应用内查找才回答得出"我在这次会话里什么时候问过什么"，
  // 而浏览器查找只会匹配**当前已挂载的行** —— 我们用的是窗口化列表，
  // 未渲染的历史段落它根本扫不到（这正是"找不到明明存在的内容"的来源）。
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f') {
    e.preventDefault()
    panelOpen.value = true
    panelView.value = 'trajectory'
    nextTick(() => {
      const input = document.querySelector('.panel-search .search-input') as HTMLInputElement | null
      input?.focus()
    })
    return
  }
  // Esc：新版地图（iframe）在最上层，先关它 —— 与其他浮层同一套"逐层关闭"语义
  if (e.key === 'Escape' && synapseMapOpen.value) {
    e.preventDefault()
    synapseMapOpen.value = false
    return
  }
  // Esc：**正在生成时优先停止生成** —— 此时用户的意图是停下，而不是关面板
  if (e.key === 'Escape' && loading.value) {
    e.preventDefault()
    stopGeneration()
    return
  }
  // Esc 关闭观测浮层 —— 全局优先级：**停止生成 > 关闭浮窗 > 关闭侧栏**。
  // （"停止生成"在上一分支已 return，所以能走到这里说明不是在生成中。）
  if (e.key === 'Escape' && (subagentsOpen.value || statsOpen.value)) {
    subagentsOpen.value = false
    statsOpen.value = false
    return
  }
  // Esc 关闭侧边栏
  if (e.key === 'Escape' && panelOpen.value) {
    panelOpen.value = false
  }
}

async function loadSessions() {
  try {
    const res = await api.get('/v1/conversations')
    const apiSessions = res.data?.data || res.data || []
    if (apiSessions.length > 0) { sessions.value = apiSessions; persistSessions() }
    else { const raw = localStorage.getItem('chat_sessions'); sessions.value = raw ? JSON.parse(raw) : [] }
  } catch {
    const raw = localStorage.getItem('chat_sessions')
    if (raw) sessions.value = JSON.parse(raw)
  }
  sortSessions()
}

async function createSession() {
  let session: ChatSession | null = null
  try {
    const res = await api.post('/v1/conversations', { title: t('新对话'), llm_config: buildLlmConfig() })
    const data = res.data?.data || res.data
    if (data?.id) session = { id: data.id, title: data.title || t('新对话'), created_at: data.created_at, updated_at: data.updated_at }
  } catch { /* fallback */ }
  if (!session) {
    const id = crypto.randomUUID()
    session = { id, title: t('新对话'), created_at: new Date().toISOString(), updated_at: new Date().toISOString() }
  }
  sessions.value.unshift(session); persistSessions()
  panelView.value = 'trajectory'
  try { await switchSession(session.id) } catch { /* ignore */ }
}

async function switchSession(id: string) {
  // 互联互通：从统一任务模式切到普通会话时，移除 task/error（保留 kb/agent/skill/workflow 上下文）
  if (unifiedMode.value && unifiedSessionId.value) {
    const q = { ...route.query }
    delete q.task
    delete q.error
    await router.replace({ path: '/chat', query: q })
    appliedQueryKey = JSON.stringify(q)
    unifiedSessionId.value = ''
  }
  if (id === activeSessionId.value) return
  const mySeq = ++switchSeq.value
  // 多会话运行：切换会话**只换视图** —— 不再清空 items、也不再无条件置 loading。
  // 后台会话继续跑，它的 items/loading 留在自己的 run state 里；
  // 切回来时读到的就是它自己的（可能正在增长的）列表。
  // 顺序要紧：先设 activeSessionId，后面的 currentRun 才指向新会话。
  activeSessionId.value = id
  // 切会话要**清掉上一个会话遗留的待审批 / 待答问题**（对齐 Reasonix 的
  // "tab activation clears a stale approval already stored on the target tab"）：
  // 它们是**那个**会话在等用户操作，显示在当前会话上会让人误以为"在等我处理"。
  pendingApprovals.value = []
  pendingQuestions.value = []
  // `activeSSE` 只应指向"**本会话**正在跑的流"。本会话没在跑就置空 ——
  // 否则在会话 B 里点"停止"会去停 A 仍在跑的流。
  // （切回正在跑的会话时这里会置空、因而暂时停不了它：**宁可停不了，也不能停错**；
  //   完整版是给每个会话各自持有连接，属下一批。）
  if (!currentRun.value.loading) activeSSE = null
  // 工具授权模式是会话级状态（存后端 Redis）：切会话时同步拉取，避免沿用上一个会话的模式
  void loadToolsMode(id)
  hasMore.value = false; earliestCursor.value = ''; loadingEarlier.value = false
  initialLoading.value = true
  try {
    const res = await api.get(`/v1/conversations/${id}?limit=${HISTORY_PAGE_SIZE}`)
    if (mySeq !== switchSeq.value) return
    const data = res.data?.data || res.data
    if (data?.messages) {
      // 该会话正在生成（或流里已累积过内容）时，**不要用拉回的历史覆盖它** ——
      // 否则"切走再切回"的瞬间会把这段时间里增长的内容冲掉。
      const run = currentRun.value
      if (!run.loading && run.items.length === 0) {
        items.value = mergeHistory(data.messages, data.tool_calls || [])
      }
      earliestCursor.value = data.cursor || ''
      hasMore.value = !!data.has_more
    }
    // P1：运行时状态是**单一事实源**（Redis 热 + unified_sessions.runtime 持久）。
    // 优先用它回填（刷新/重开会话/换设备都不丢），llm_config 作为老数据兜底。
    let cfg: any = data?.llm_config
    if (typeof cfg === 'string') { try { cfg = JSON.parse(cfg) } catch { cfg = undefined } }
    let rt: any = null
    try {
      const view = await getSessionRuntime(id)
      rt = view.runtime
      // 生效值 + 来源：后端已算好解析链，前端只显示（见 runtimeResolved 的注释）
      runtimeResolved.value = view.resolved || null
    } catch { /* 不可用时静默回落 llm_config */ }
    // 模式兜底：会话没记、或记了个不认识的值时，**必须回落到默认模式**。
    // 过去这里只在"合法"时才赋值 → 非法时**沿用上一个会话的 mode**，
    // 表现为"切了会话，模式看着是选中的，其实是别的会话的设置"。
    const savedMode = rt?.mode || cfg?.mode
    mode.value =
      typeof savedMode === 'string' && modeOptions.some(o => o.value === savedMode)
        ? savedMode
        : DEFAULT_MODE
    // 模型兜底（**这一条就是 "Model is unavailable" 的直接原因**）：
    // 会话没记模型时，过去会留空 → 提交不带 model → 落到**后端默认模型**；
    // 而一旦后端默认模型被上游下架（日志：opencode-go 的模型数 37→33），
    // 就会直接报 "Upstream request failed: Model is unavailable."。
    // 所以按「会话记录 → 会话 llm_config → 可用模型列表第一个 → 空」四级回落，
    // 绝不把空值当成"可以用后端默认"来提交。见 docs/multi-session-runtime-plan.md。
    llmModel.value =
      (typeof rt?.model === 'string' && rt.model)
      || (typeof cfg?.model === 'string' && cfg.model)
      || availableModels.value[0]?.name
      || ''
    // 已激活能力（问题 3）：runtime.context 是单一事实源 —— 把 URL 未带入、
    // 但会话上已保存的激活项（知识库/Agent/技能/插件/记忆分类）补进侧栏展示，
    // 否则刷新后界面上就看不到"这次对话激活了什么"。
    const fromRuntime = chipsFromWorkbenchContext(rt?.context)
    if (fromRuntime.length) {
      const merged = [...contextChips.value]
      for (const chip of fromRuntime) {
        if (!merged.some(c => c.type === chip.type && c.value === chip.value)) merged.push(chip)
      }
      contextChips.value = merged
    }
  } catch { /* fallback */ } finally {
    if (mySeq === switchSeq.value) {
      loading.value = false
      initialLoading.value = false
      // 会话加载完成后自动滚到底部
      await nextTick()
      const listEl = document.querySelector<HTMLElement>('.message-list')
      if (listEl) listEl.scrollTop = listEl.scrollHeight
    }
  }
}

// 性能优化：cursor 分页，触顶加载更早的消息（首屏只加载最新 HISTORY_PAGE_SIZE 条）
const HISTORY_PAGE_SIZE = 50
const hasMore = ref(false)
const earliestCursor = ref('')
const loadingEarlier = ref(false)
const initialLoading = ref(false)
const switchSeq = ref(0)


async function loadEarlier() {
  if (loadingEarlier.value || !hasMore.value || !activeSessionId.value || !earliestCursor.value) return
  loadingEarlier.value = true
  try {
    const res = await api.get(
      `/v1/conversations/${activeSessionId.value}?limit=${HISTORY_PAGE_SIZE}&before=${encodeURIComponent(earliestCursor.value)}`,
    )
    const data = res.data?.data || res.data
    if (data?.messages?.length) {
      const earlier = mergeHistory(data.messages, data.tool_calls || [])
      // 头部插入后由 MessageList 按锚点还原视口（单写者），此处不碰 DOM
      items.value = [...earlier, ...items.value]
      earliestCursor.value = data.cursor || ''
      hasMore.value = !!data.has_more
    } else {
      hasMore.value = false
    }
  } catch {
    hasMore.value = false
  } finally {
    loadingEarlier.value = false
  }
}

async function deleteSession(id: string) {
  try { await api.delete(`/v1/conversations/${id}`) } catch { /* 保留本地删除 */ }
  sessions.value = sessions.value.filter(s => s.id !== id); persistSessions()
  if (activeSessionId.value === id) {
    activeSessionId.value = ''; items.value = []
    if (sessions.value.length > 0) await switchSession(sessions.value[0].id)
  }
}

function requestDelete(id: string) {
  const s = sessions.value.find(x => x.id === id)
  Modal.confirm({
    title: t('删除对话'),
    content: `确定删除「${s?.title || t('新对话')}」？此操作不可恢复。`,
    okText: t('删除'),
    okButtonProps: { danger: true },
    cancelText: t('取消'),
    onOk: () => deleteSession(id),
  })
}

// ── 重命名（deepseek session rename dialog：Modal + 行内输入框） ──
const renameTarget = ref<ChatSession | null>(null)
const renameDraft = ref('')
const renaming = ref(false)

function openRename(id: string, currentTitle: string) {
  const s = sessions.value.find(x => x.id === id)
  if (!s) return
  renameTarget.value = s
  renameDraft.value = currentTitle
}

async function confirmRename() {
  const target = renameTarget.value
  const title = renameDraft.value.trim()
  if (!title || !target) return
  renaming.value = true
  try {
    await updateConversation(target.id, { title, llm_config: buildLlmConfig() } as any)
    const s = sessions.value.find(x => x.id === target.id)
    if (s) s.title = title
    persistSessions()
    message.success(t('已重命名'))
    renameTarget.value = null
  } catch (e: any) {
    message.error(t('重命名失败') + (e?.response?.data?.error || e?.message || t('网络错误')))
  } finally {
    renaming.value = false
  }
}

// ── 置顶（列表排序：pinned DESC + updated_at DESC） ──
function sortSessions() {
  sessions.value = [...sessions.value].sort((a, b) => {
    if (!!a.pinned !== !!b.pinned) return a.pinned ? -1 : 1
    return new Date(b.updated_at || 0).getTime() - new Date(a.updated_at || 0).getTime()
  })
}

async function togglePin(id: string, pinned: boolean) {
  const s = sessions.value.find(x => x.id === id)
  if (!s) return
  const prev = s.pinned
  s.pinned = pinned
  sortSessions(); persistSessions()
  try {
    await updateConversation(id, { pinned, llm_config: buildLlmConfig() } as any)
  } catch {
    s.pinned = prev
    sortSessions(); persistSessions()
    message.error(t('置顶操作失败'))
  }
}

// 设置会话标签（DB 持久化：写 sessions.tag；失败回滚本地状态）
// 注意：旧实现只写 localStorage，而 loadSessions 每次都用 API 数据覆盖 sessions 数组，
// 刷新后标签必然丢失 —— 故必须落库。
async function setSessionTag(id: string, tag: string) {
  const s = sessions.value.find(x => x.id === id)
  if (!s) return
  const prev = s.tag
  s.tag = tag || undefined
  sortSessions(); persistSessions()
  try {
    // 空串表示清除标签（后端 NULLIF 写 NULL）
    await updateConversation(id, { tag: tag || '' })
    message.success(tag ? `已设置标签：${tag}` : t('已清除标签'))
  } catch {
    s.tag = prev
    sortSessions(); persistSessions()
    message.error(t('标签保存失败'))
  }
}

// ── 分享（chat.deepseek.com/share/{id} 风格：选消息 → 生成链接 → 可取消） ──
const shareOpen = ref(false)
const shareTarget = ref<ChatSession | null>(null)
const shareInfo = ref<ShareInfo | null>(null)
const shareLoading = ref(false)
const shareRevoking = ref(false)
const shareError = ref('')
const shareMessageIds = ref<string[]>([])

const isGuest = computed(() => !authStore.user)

// 分享候选：会话中所有文本消息（用户可勾选；工具调用/思考块不分享）
const shareCandidates = computed(() => items.value
  .filter((it): it is Extract<ChatItem, { kind: 'text' }> =>
    it.kind === 'text' && (it.role === 'user' || it.role === 'assistant') && !!it.id)
  .map(it => ({
    id: it.id as string,
    role: it.role,
    preview: (it.content || '').replace(/\s+/g, ' ').trim().slice(0, 56),
  })))

async function openShare(id: string) {
  const s = sessions.value.find(x => x.id === id)
  if (!s) return
  if (id !== activeSessionId.value) {
    await switchSession(id)
  }
  shareTarget.value = s
  shareInfo.value = null
  shareError.value = ''
  shareMessageIds.value = shareCandidates.value.map(c => c.id)
  if (!isGuest.value) {
    try { shareInfo.value = await getActiveShare(s.id) } catch { /* 无活跃分享 */ }
  }
  shareOpen.value = true
}

function toggleShareMessage(id: string) {
  const i = shareMessageIds.value.indexOf(id)
  if (i >= 0) shareMessageIds.value.splice(i, 1)
  else shareMessageIds.value.push(id)
}

async function generateShare() {
  if (!shareTarget.value) return
  if (shareMessageIds.value.length === 0) { message.warning('请至少选择一条要分享的消息'); return }
  shareLoading.value = true
  shareError.value = ''
  try {
    shareInfo.value = await createShare(shareTarget.value.id, shareMessageIds.value)
  } catch (e: any) {
    shareError.value = e?.response?.data?.error || '生成分享链接失败'
  } finally {
    shareLoading.value = false
  }
}

async function revokeCurrentShare() {
  if (!shareTarget.value || !shareInfo.value) return
  shareRevoking.value = true
  try {
    await revokeShare(shareTarget.value.id)
    shareInfo.value = null
    message.success(t('分享已取消，链接已失效'))
  } catch {
    message.error(t('取消分享失败'))
  } finally {
    shareRevoking.value = false
  }
}

function shareUrl(): string {
  return `${window.location.origin}/share/${shareInfo.value?.share_id || ''}`
}

async function copyShareLink() {
  try {
    await navigator.clipboard.writeText(shareUrl())
    message.success(t('链接已复制'))
  } catch {
    message.error(t('复制失败'))
  }
}

// ── SSE 编排：事件 → ChatItem ──
// 流式缓冲：累加 assistant 原始文本后整体重算（让 chunk 中的 [thinking] 标签正确配对）
let streamBuf = ''
let streamTextId = ''
let streamReasonId = ''

function resetStreamState() {
  streamBuf = ''
  streamTextId = ''
  streamReasonId = ''
}

function appendUserText(text: string, attachments?: ChatAttachment[]) {
  items.value.push({ kind: 'text', role: 'user', content: text, id: genItemId(), attachments })
}

// 性能/正确性：稳定 id（虚拟列表 key + 流式定位，loadEarlier 头部插入不错位）
let itemIdSeq = 0
function genItemId() {
  return `msg_${Date.now().toString(36)}_${itemIdSeq++}`
}

function onTextChunk(text: string) {
  streamBuf += text
  // 引擎按 ~80 字分段下发 "[thinking]片段[/thinking]"，用 loose 状态机解析：
  // 多段思考全部归 reasoning，正文不再残留 [/thinking][thinking] 标签。
  const { reasoning, body } = splitThinking(streamBuf, { loose: true })
  if (reasoning) {
    const existing = items.value.find(it => it.id === streamReasonId)
    if (existing?.kind === 'reasoning') {
      existing.content = reasoning
    } else {
      const id = genItemId()
      streamReasonId = id
      items.value.push({ kind: 'reasoning', content: reasoning, streaming: true, id })
    }
  }
  if (body) {
    const existing = items.value.find(it => it.id === streamTextId)
    if (existing?.kind === 'text' && existing.role === 'assistant') {
      existing.content = body
    } else {
      const id = genItemId()
      streamTextId = id
      items.value.push({ kind: 'text', role: 'assistant', content: body, streaming: true, id })
    }
  }
}

function flushStreamingFlags() {
  for (const it of items.value) {
    if (it.kind === 'text' && it.streaming) it.streaming = false
    if (it.kind === 'reasoning' && it.streaming) it.streaming = false
    if (it.kind === 'tool_call' && it.status === 'running') it.status = 'done'
  }
  resetStreamState()
}

function onSSEMessage(raw: any) {
  const type = raw?.type
  const d = raw?.data || {}
  if (type === 'text') {
    const text = d?.content ?? raw?.content ?? ''
    if (!text) return
    onTextChunk(text)
  } else if (type === 'tool_call') {
    items.value.push({
      kind: 'tool_call', id: d?.id ?? String(Date.now()), name: d?.name ?? 'tool',
      arguments: d?.arguments ?? '', status: 'running',
    })
  } else if (type === 'tool_result') {
    const callId = d?.tool_call_id ?? d?.id ?? ''
    const call = items.value.find(it => it.kind === 'tool_call' && it.id === callId)
    if (call && call.kind === 'tool_call') call.status = 'done'
    const content = d?.content ?? d?.result ?? ''
    if (content) {
      items.value.push({
        kind: 'tool_result', toolCallId: callId, id: `${callId}:res`,
        content: typeof content === 'string' ? content : JSON.stringify(content),
        isError: !!d?.error,
      })
    }
  } else if (type === 'usage' || type === 'turn_stats') {
    // 本轮用量：引擎在每个回合结束时发 usage（python-engine/app/agent/loop.py:199：
    // {"type":"usage","input_tokens":N,"output_tokens":N}）。此前**没有这个分支**，
    // 于是 lastTurnStats 恒为空、状态栏整段隐藏 —— 这就是"看不到 tokens/消耗"的直接原因。
    // 会话级累计（tokens/费用/**缓存命中率**/吞吐）由 GET /v1/sessions/{id}/metrics 提供。
    const inputTokens = Number(d?.input_tokens ?? raw?.input_tokens ?? 0) || 0
    const outputTokens = Number(d?.output_tokens ?? raw?.output_tokens ?? 0) || 0
    const durationMs = Number(d?.duration_ms ?? raw?.duration_ms ?? 0) || 0
    if (inputTokens || outputTokens) {
      items.value.push({
        kind: 'turn_stats',
        id: genItemId(),
        inputTokens,
        outputTokens,
        ...(durationMs > 0 ? { durationSec: Math.round(durationMs / 100) / 10 } : {}),
      } as TurnStatsItem)
    }
  } else if (type === 'compaction') {
    // 引擎的自动压缩事件（runtime.py 的 _compact_with_notice）：把"上下文悄悄变短"
    // 变成用户可见的状态栏提示（问题 4）。
    let info: any = {}
    try { info = JSON.parse(String(d?.content ?? raw?.content ?? '{}')) } catch { info = {} }
    if (info?.saved_tokens) {
      lastCompaction.value = {
        beforeTokens: Number(info.before_tokens) || 0,
        afterTokens: Number(info.after_tokens) || 0,
        savedTokens: Number(info.saved_tokens) || 0,
      }
    }
  } else if (type === 'done') {
    flushStreamingFlags()
    loading.value = false
    stopTurnTimer()
    activeSSE?.close(); activeSSE = null
    currentTraceId.value = d?.trace_id || ''
    const doneMeta = normalizeMeta(d?.metadata)
    if (doneMeta && Object.keys(doneMeta).length && streamTextId) {
      const streamItem = items.value.find(x => x.id === streamTextId)
      if (streamItem?.kind === 'text' && streamItem.role === 'assistant') {
        ;(streamItem as any).metadata = { ...((streamItem as any).metadata || {}), ...doneMeta }
      }
    }
    const it = d?.input_tokens ?? 0
    const ot = d?.output_tokens ?? 0
    if (it || ot) {
      items.value.push({
        kind: 'turn_stats', inputTokens: it, outputTokens: ot,
        durationSec: turnElapsed.value,
      })
    }
  } else if (type === 'approval') {
    const callId = d?.id ?? d?.tool_call_id ?? String(Date.now())
    pendingApprovals.value.push({
      id: callId,
      toolName: d?.name ?? 'tool',
      arguments: d?.arguments ?? '',
      // 倒计时：与后端 _await_approval 的 300s 超时对齐（超时按拒绝处理）
      expiresAt: Date.now() + APPROVAL_TIMEOUT_MS,
    } as PendingApproval)
    ensureApprovalTimer()
  } else if (type === 'ask') {
    // 后端 ask_user 工具在等答案：卡片按 tool_call_id 回填
    pendingQuestions.value.push({
      id: d?.id ?? d?.tool_call_id ?? String(Date.now()),
      question: d?.question ?? d?.content ?? '需要你的确认',
      options: Array.isArray(d?.options) ? d.options.map((option: unknown) => String(option)) : [],
      allowFreeText: d?.allow_free_text !== false,
    })
  } else if (type === 'guardrail_blocked') {
    flushStreamingFlags()
    loading.value = false
    stopTurnTimer()
    activeSSE?.close(); activeSSE = null
    message.warning(d?.content || t('请求被安全策略拦截'))
  } else if (type === 'error') {
    flushStreamingFlags()
    loading.value = false
    stopTurnTimer()
    activeSSE?.close(); activeSSE = null
    message.error(d?.content || d?.error || t('请求失败'))
  } else if (typeof type === 'string' && type.startsWith('subagent.')) {
    // 子 Agent 进度（docs/subagent-design.md §4.2）：只进侧边栏观测面板。
    // 刻意不落主对话流 —— 子 Agent 的思考/正文是"数据"，不是会话内容。
    const event = (d && Object.keys(d).length ? { ...d, type } : { ...raw }) as SubagentEvent
    if (event?.run_id) {
      subagentLiveEvents.value.push(event)
      if (subagentLiveEvents.value.length > SUBAGENT_LIVE_MAX) {
        subagentLiveEvents.value.splice(0, subagentLiveEvents.value.length - SUBAGENT_LIVE_MAX)
      }
    }
  }
}

async function sendMessage(text: string, attachments?: ChatAttachment[]) {
  // 互联互通：统一任务模式 → POST /v1/chat/submit（与 SSE 流式并列的新路径）
  if (unifiedMode.value && unifiedSessionId.value) {
    await sendUnified(text, attachments)
    return
  }
  loading.value = true
  startTurnTimer()
  resetStreamState()
  connectionLost.value = false
  appendUserText(text, attachments)
  const userItemId = items.value[items.value.length - 1]?.id
  const sessionId = activeSessionId.value || crypto.randomUUID()
  currentTraceId.value = ''
  try {
    if (activeSSE) { activeSSE.close(); activeSSE = null }
    activeSSE = createSSEConnection(
      sessionId,
      (raw) => withRun(runOf(sessionId), () => { forwardStreamToMap(sessionId, raw); onSSEMessage(raw) }),
      () => {
        loading.value = false
        stopTurnTimer()
        connectionLost.value = true
        activeSSE?.close(); activeSSE = null
        markMessageFailed(userItemId, '连接已断开')
      },
      {
        // 携带上一连接的最后事件 id：服务端按 last_event_id 从缓冲流补发断线缺口
        initialLastEventId: sseLastIdBySession.get(sessionId) || '',
        // 单轮流式进行中断线时交由浏览器原生自动重连（重连自动带 Last-Event-ID 头，无感续传）
        autoReconnect: true,
        onLastEventId: (id) => { sseLastIdBySession.set(sessionId, id) },
      },
    )
    const body: any = {
      content: text,
      session_id: sessionId,
      // 发送侧幂等：同一条消息最多被引擎执行一次（服务端用 Redis SETNX 做 5 分钟去重）。
      // 必须由**客户端**生成 —— 只有客户端知道"这两次提交是同一条消息"：
      // 网络重试或用户重发时这个 ID 不变，服务端据此判定"已在处理"而不再重复触发引擎。
      llm_config: { ...buildLlmConfig(), client_msg_id: crypto.randomUUID() },
    }
    const ctx = buildContext()
    if (ctx) body.context = ctx
    const resolvedAtts = await resolveAttachmentUrls(attachments)
    if (resolvedAtts.length) {
      body.attachments = resolvedAtts.map(a => ({ id: a.id, name: a.name, mime_type: a.mimeType, url: a.url, is_image: a.isImage }))
    }
    await api.post('/submit', body)
    activeSessionId.value = sessionId
  } catch (e: any) {
    if (activeSSE) { activeSSE.close(); activeSSE = null }
    loading.value = false
    stopTurnTimer()
    flushStreamingFlags()
    const reason = describeApiError(e)
    markMessageFailed(userItemId, reason)
    message.error(t('发送失败：') + reason)
  }
}

// ── 失败消息标记 ──
function markMessageFailed(itemId: string | undefined, errorMsg: string) {
  if (!itemId) return
  const it = items.value.find(i => i.id === itemId)
  if (it && it.kind === 'text') {
    it.error = true
    it.errorMsg = errorMsg
  }
}

// ── 消息重试/重新生成 ──
/** 删除指定 itemId 及其后所有消息，返回被删除的用户消息文本（如有） */
function truncateFrom(itemId: string): { text?: string; attachments?: ChatAttachment[] } {
  const idx = items.value.findIndex(i => i.id === itemId)
  if (idx < 0) return {}
  const removed = items.value.slice(idx)
  items.value = items.value.slice(0, idx)
  const userMsg = removed.find(i => i.kind === 'text' && i.role === 'user') as any
  return userMsg ? { text: userMsg.content, attachments: userMsg.attachments } : {}
}

/** 指定消息之后还有多少条（不含自身）：重发会连带删除，先让用户知道代价 */
function messagesAfter(itemId: string): number {
  return countItemsAfter(items.value, itemId)
}

/**
 * 删除类操作前的确认。
 *
 * `truncateFrom` 是**不可逆**的（后续消息直接从前端状态里消失，后端也没有回滚接口），
 * 而「重发 / 重新生成」是消息操作栏里的高频按钮 —— 误点一次就丢掉整段后续内容。
 * 代价为 0 时（消息已在末尾）不打扰。
 */
function confirmDestructive(removeCount: number, action: string, run: () => void) {
  if (removeCount <= 0) {
    run()
    return
  }
  Modal.confirm({
    title: `这会删除后面的 ${removeCount} 条消息`,
    content: `${action}需要截断到这条消息，其后 ${removeCount} 条消息（含助手回复）会被删除，且无法恢复。`,
    okText: t('删除并继续'),
    okType: 'danger',
    cancelText: t('取消'),
    onOk: () => { run() },
  })
}

/** 用户消息编辑后重发：删除该消息及之后所有，用新文本重发 */
function retryFromUserMessage(itemId: string, newText: string) {
  confirmDestructive(messagesAfter(itemId), '重发', () => {
    truncateFrom(itemId)
    sendMessage(newText)
  })
}

/** 助手消息重新生成：删除该消息及之后所有，取上一条用户消息重发 */
function regenerateAssistant(itemId: string) {
  const idx = items.value.findIndex(i => i.id === itemId)
  if (idx < 0) return
  let userMsg: any = null
  for (let i = idx - 1; i >= 0; i--) {
    const it = items.value[i]
    if (it.kind === 'text' && it.role === 'user') { userMsg = it; break }
  }
  if (!userMsg) {
    message.warning(t('未找到对应的用户消息，无法重新生成'))
    return
  }
  confirmDestructive(messagesAfter(itemId), '重新生成', () => {
    truncateFrom(itemId)
    sendMessage(userMsg.content, userMsg.attachments)
  })
}

/** 失败消息重试：清除错误状态，用原文本重发 */
function retryFailedMessage(itemId: string) {
  const idx = items.value.findIndex(i => i.id === itemId)
  if (idx < 0) return
  const it = items.value[idx]
  if (it.kind !== 'text') return
  const text = it.content
  const attachments = it.attachments
  confirmDestructive(messagesAfter(itemId), '重试', () => {
    truncateFrom(itemId)
    sendMessage(text, attachments)
  })
}

function stopGeneration() {
  stopTurnTimer()
  if (activeSSE) { activeSSE.close(); activeSSE = null }
  loading.value = false
  flushStreamingFlags()
  const last = items.value[items.value.length - 1]
  if (last && last.kind === 'text' && last.role === 'assistant') {
    last.stopped = true
  }
}

// 继续生成（停止后）
function continueGeneration() {
  const last = items.value[items.value.length - 1]
  if (last && last.kind === 'text' && last.role === 'assistant' && last.stopped) {
    regenerateAssistant(last.id!)
  }
}
</script>

<template>
  <div class="chat-layout" :class="{ 'is-swapped': layoutSwapped }">
    <!-- 分屏 6a：**只读参考栏**（左侧）。
         布局本身是 flex，所以加一栏不需要改任何 CSS；它自己滚动，
         不会把主会话的滚动位置带跑（与主列表的滚动锚定互不干扰）。 -->
    <SessionPreviewPane
      v-if="splitSessionId"
      :session-id="splitSessionId"
      :title="splitTitle"
      :sessions="sessions"
      :exclude-session-id="activeSessionId"
      :handle-side="layoutSwapped ? 'left' : 'right'"
      @update:session-id="splitSessionId = $event"
    />
    <div class="chat-main">
      <div
        v-if="connectionLost"
        class="connection-banner"
      >
        {{ isOnline ? '与服务器的连接已断开，正在尝试重连...' : '网络已断开，请检查网络连接' }}
      </div>
      <div class="chat-body">
        <!-- 内容区工具条 -->
        <div class="chat-toolbar">
          <div
            class="toolbar-side"
            aria-hidden="true"
          />
          <div class="toolbar-center">
            <span class="toolbar-title">{{ unifiedMode ? '统一任务' : (activeSession?.title || 'Chiron') }}</span>
            <!-- 生效值 + 来源：后端已把解析链（本次请求 > 本会话 > 偏好默认 > 系统默认）算好，
                 这里直接显示 —— 用户由此能回答"我现在用的是哪个模式、是谁定的"。
                 来源为空时不显示后缀，避免出现"常规 · 未知"这种只占位没信息的字样。 -->
            <span
              class="toolbar-mode"
              :title="modeSourceLabel ? `模式来源：${modeSourceLabel}` : '当前对话模式'"
            >{{ modeLabel }}<template v-if="modeSourceLabel"> · {{ modeSourceLabel }}</template></span>
            <!-- 思考档位：点按循环（关 → 低 → 高 → 最高）。
                 与"模式"同类信息（都是"这次怎么回答"），所以并排放；用循环按钮而不是下拉 ——
                 只有 4 档，比下拉省一次点击和一块浮层。
                 值最终要经后端 app/providers/effort.py **归一化**才可能发送
                 （各家词表不一致，直接透传会 400 —— 我们踩过 UNSUPPORTED_REASONING_EFFORT）。 -->
            <!-- 分屏 6a：并排参考另一个会话（只读）。
                 默认取"上一个访问过的会话"，所以不必先做选择器就能立即有用。 -->
            <button
              type="button"
              class="toolbar-mode split-toggle"
              :class="{ active: !!splitSessionId }"
              :title="$t('并排参考另一个会话（只读）')"
              @click="toggleSplit()"
            >
              {{ splitSessionId ? $t('收起参考') : $t('分屏') }}
            </button>
            <button
              type="button"
              class="toolbar-mode toolbar-effort"
              :class="{ active: !!effort }"
              :title="$t('思考档位：点按循环切换（关 / 低 / 高 / 最高）')"
              @click="cycleEffort()"
            >
              {{ $t('思考') }} {{ effortLabel }}
            </button>
          </div>
          <div class="toolbar-side toolbar-actions">
            <Button
              type="text"
              size="small"
              class="toolbar-btn"
              :class="{ active: panelOpen && panelView === 'sessions' }"
              :title="panelOpen && panelView === 'sessions' ? '收起会话列表' : '会话历史'"
              @click="openPanel('sessions')"
            >
              <template #icon>
                <MenuOutlined />
              </template>
              <span class="toolbar-label">{{ $t('会话') }}</span>
            </Button>
            <Button
              type="text"
              size="small"
              class="toolbar-btn"
              :class="{ active: panelOpen && panelView === 'trajectory' }"
              :title="panelOpen && panelView === 'trajectory' ? '收起轨迹' : '查看历史提问'"
              @click="openPanel('trajectory')"
            >
              <template #icon>
                <HistoryOutlined />
              </template>
              <span class="toolbar-label">{{ $t('轨迹') }}</span>
            </Button>
            <Button
              type="text"
              size="small"
              class="toolbar-btn"
              :class="{ active: searchOpen }"
              :title="searchOpen ? '关闭搜索' : '在本会话中搜索（正文与思考）'"
              @click="searchOpen ? closeSearch() : openSearch()"
            >
              <template #icon>
                <SearchOutlined />
              </template>
              <span class="toolbar-label">{{ $t('搜索') }}</span>
            </Button>
            <!-- 用 #overlay + <Menu>，与 ChatSidePanel 里那个**确实可用**的会话菜单保持一致。
                 此前这里用 :menu="{ items, onClick }"，表现是**点击完全不弹菜单**：
                 Console 无报错、命中的确实是按钮本身 ⇒ 既不是遮挡也不是渲染异常。
                 对齐到已验证可用的写法（trigger 用字符串、Button 加 @click.stop、菜单走 #overlay）。 -->
            <Dropdown
              trigger="click"
              placement="bottomRight"
            >
              <Button
                type="text"
                size="small"
                class="toolbar-btn"
                :title="$t('更多操作')"
                @click.stop
              >
                <template #icon>
                  <MoreOutlined />
                </template>
              </Button>
              <template #overlay>
                <Menu class="toolbar-menu">
                  <template
                    v-for="it in (toolbarMenuItems as any[])"
                    :key="String(it.key)"
                  >
                    <MenuDivider v-if="it.type === 'divider'" />
                    <MenuItem
                      v-else
                      :disabled="it.disabled"
                      @click="onToolbarMenu({ key: String(it.key) })"
                    >
                      <component :is="it.icon" />
                      <span class="menu-label">{{ it.label }}</span>
                    </MenuItem>
                  </template>
                </Menu>
              </template>
            </Dropdown>
          </div>
        </div>

        <!-- 会话内检索条：Enter/↓ 下一个、Shift+Enter/↑ 上一个、Esc 关闭 -->
        <div
          v-if="searchOpen"
          class="chat-search"
        >
          <SearchOutlined class="chat-search-icon" />
          <input
            ref="searchInputRef"
            v-model="searchQuery"
            class="chat-search-input"
            type="text"
            :placeholder="$t('在本会话中搜索正文与思考（Enter 下一个，Esc 关闭）')"
            @keydown.enter.exact.prevent="gotoMatch(1)"
            @keydown.enter.shift.prevent="gotoMatch(-1)"
            @keydown.esc.prevent="closeSearch"
          >
          <span class="chat-search-count">
            {{ searchQuery.trim() ? (searchMatches.length ? `${searchCursor + 1} / ${searchMatches.length}` : '无匹配') : '' }}
          </span>
          <div class="chat-search-actions">
            <button
              class="chat-search-btn"
              type="button"
              :title="$t('上一个')"
              :disabled="!searchMatches.length"
              @click="gotoMatch(-1)"
            >
              ↑
            </button>
            <button
              class="chat-search-btn"
              type="button"
              :title="$t('下一个')"
              :disabled="!searchMatches.length"
              @click="gotoMatch(1)"
            >
              ↓
            </button>
            <button
              class="chat-search-btn"
              type="button"
              :title="$t('关闭')"
              @click="closeSearch"
            >
              ✕
            </button>
          </div>
        </div>

        <!-- 互联互通：错误提示条（query.error） -->
        <div
          v-if="errorBanner"
          class="unified-error-banner"
        >
          <span class="ueb-text">{{ errorBanner }}</span>
          <CloseOutlined
            class="ueb-close"
            :title="$t('关闭')"
            @click="errorBanner = ''"
          />
        </div>

        <!-- 互联互通：统一任务模式 -->
        <template v-if="unifiedMode">
          <div class="unified-bar">
            <span
              class="ub-badge"
              :class="{ running: loading, done: unifiedJustFinished }"
            >
              {{ loading ? '编排中' : unifiedJustFinished ? '完成' : '统一任务' }}
            </span>
            <span class="ub-mode">{{ unifiedSubmitMode || 'auto' }}</span>
            <span class="ub-spacer" />
            <button
              type="button"
              class="ub-btn"
              :disabled="!items.length"
              @click="clearUnifiedMessages"
            >
              {{ $t('清空') }}
            </button>
            <button
              type="button"
              class="ub-btn exit"
              :title="$t('退出统一任务模式')"
              @click="exitUnifiedMode"
            >
              {{ $t('退出') }}
            </button>
          </div>
          <div
            v-if="loading"
            class="unified-exec-hint"
          >
            <span class="ueh-dot" />{{ $t('正在编排/执行子任务...') }}<template v-if="turnElapsed >= 2">
              &nbsp;·&nbsp;{{ turnElapsed }}s
            </template>
          </div>
          <div
            v-if="!items.length && !loading"
            class="unified-empty"
          >
            {{ $t('统一任务会话已就绪，直接发送消息即可继续追问') }}
          </div>
          <div class="unified-list">
            <template
              v-for="(it, i) in items"
              :key="it.id ?? i"
            >
              <MessageItem
                v-if="it.kind === 'text' || it.kind === 'reasoning'"
                :item="it"
                @retry-from="retryFromUserMessage"
                @regenerate="regenerateAssistant"
                @continue="continueGeneration"
                @retry-failed="retryFailedMessage"
              />
              <div
                v-else-if="(it as any).kind === 'kb_hits'"
                class="kb-hits-tag"
              >
                <span class="kb-hits-text">引用了知识库（×{{ (it as any).count || 1 }}）</span>
                <a
                  v-if="(it as any).kb_id"
                  class="kb-hits-link"
                  href="#"
                  :title="$t('查看引用的知识库')"
                  @click.prevent="openKb((it as any).kb_id)"
                >{{ $t('查看知识库') }}</a>
              </div>
            </template>
          </div>
          <CallChainTimeline
            v-if="currentTraceId && !loading"
            :trace-id="currentTraceId"
            :tenant-id="authStore.user?.tenant_id || ''"
          />
        </template>

        <!-- 原有 SSE 流式模式 -->
        <ChatEmptyHero
          v-else-if="items.length === 0 && !loading"
          @suggest="sendMessage"
        />
        <template v-else>
          <div
            v-if="loading"
            class="turn-status"
          >
            {{ $t('思考中') }}<template v-if="turnElapsed >= 2">
              &nbsp;·&nbsp;{{ turnElapsed }}s
            </template>
          </div>
          <MessageList
            :items="items"
            :loading="loading"
            :initial-loading="initialLoading"
            :focus-index="trajectoryFocus"
            :focus-token="trajectoryToken"
            :has-more="hasMore"
            :loading-earlier="loadingEarlier"
            :session-key="activeSessionId"
            @load-earlier="loadEarlier"
            @quote-text="onQuoteText"
            @retry-from="retryFromUserMessage"
            @regenerate="regenerateAssistant"
            @continue="continueGeneration"
            @retry-failed="retryFailedMessage"
          />
          <CallChainTimeline
            v-if="currentTraceId && !loading"
            :trace-id="currentTraceId"
            :tenant-id="authStore.user?.tenant_id || ''"
          />
        </template>
      </div>

      <!-- 工具授权模式（ask/auto/yolo）：已移至对话框底部（见 ChatInput 之后的一行下拉）——
           它控制的是"工具执行是否需要确认"，与「对话模式」是两个维度，
           但占用工具栏一整行会让视觉权重过高、且离输入区太远。 -->

      <div
        v-if="pendingApprovals.length"
        class="approval-zone"
      >
        <div
          v-for="a in pendingApprovals"
          :key="a.id"
          class="approval-card"
        >
          <div class="approval-info">
            <span class="approval-tag">{{ $t('工具确认') }}</span>
            <span class="approval-name">{{ a.toolName }}</span>
            <span
              v-if="approvalRemain(a) > 0"
              class="approval-countdown"
            >{{ approvalRemain(a) }}s 后自动拒绝</span>
          </div>
          <div class="approval-args">
            {{ a.arguments }}
          </div>
          <div class="approval-actions">
            <button
              class="approval-btn danger"
              type="button"
              @click="resolveApproval(a, false)"
            >
              {{ $t('拒绝') }}
            </button>
            <button
              class="approval-btn allow"
              type="button"
              @click="resolveApproval(a, true)"
            >
              {{ $t('允许执行') }}
            </button>
          </div>
        </div>
      </div>

      <div
        v-if="pendingQuestions.length"
        class="ask-zone"
      >
        <AskCard
          v-for="q in pendingQuestions"
          :key="q.id"
          :question="q.question"
          :options="q.options"
          :allow-free-text="q.allowFreeText"
          @answer="(value: string) => answerQuestion(q, value)"
        />
      </div>

      <ChatInput
        ref="chatInputRef"
        :loading="loading"
        :mode="mode"
        :mode-options="modeOptions"
        :model="llmModel"
        :session-id="unifiedMode ? unifiedSessionId : activeSessionId"
        @send="sendMessage"
        @stop="stopGeneration"
        @update:mode="onModeChange"
        @model-change="onModelChange"
        @command="onSlashCommand"
        @open-panel="openContextPanel"
        @mention-add="onMentionAdd"
        :tools-mode="toolsMode"
        @tools-mode-change="onToolsModeChange"
        :context-chips="contextChips"
        @remove-context-chip="(c: ContextChip) => removeContextChip(c.type, c.value)"
      />

      <ChatStatusBar
        :model="llmModel"
        :stats="lastTurnStats"
        :context-used="lastTurnStats?.inputTokens ?? null"
        :context-limit="contextWindow"
        :compaction="lastCompaction"
        :subagent-active-count="subagentActiveCount"
        :online="isOnline && !connectionLost"
        @toggle-subagents="subagentsOpen = !subagentsOpen"
        @toggle-stats="statsOpen = !statsOpen"
      />

      <!-- 观测浮层（设计稿 docs/floating-panels-design.md）：
           向上弹出 + 右对齐；限高到输入区顶部，因此**不遮挡正在写的草稿**。
           子 Agent 的内部 tab（运行·输出·用量·事件流）与移动端抽屉在后续批次接入。 -->
      <FloatingPanel
        :open="subagentsOpen"
        :title="$t('子 Agent')"
        :width="420"
        :bottom-inset="floatingBottomInset"
        @close="subagentsOpen = false"
      >
        <SubAgentPanel
          :session-id="activeSessionId"
          :live-events="subagentLiveEvents"
        />
      </FloatingPanel>

      <FloatingPanel
        :open="statsOpen"
        :title="$t('会话统计')"
        :width="320"
        :bottom-inset="floatingBottomInset"
        @close="statsOpen = false"
      >
        <SessionStatsPanel :session-id="activeSessionId" />
      </FloatingPanel>

      <ChatDisplaySettings v-model:open="displaySettingsOpen" />
      <SaveToKnowledgeDialog
        v-model:open="saveToKbOpen"
        :content="sessionMarkdownForDialog"
        :default-title="activeSession?.title || '对话记录'"
      />
      <SaveToMemoryDialog
        v-model:open="saveToMemoryOpen"
        :content="sessionMarkdownForDialog"
        :default-key="activeSession?.title || ''"
      />

      <!-- 存为 Agent：名字由用户确认（会话正文作为人格 system_prompt） -->
      <Modal
        :open="saveAgentOpen"
        :title="$t('存为 Agent')"
        :confirm-loading="savingAgent"
        :ok-text="$t('创建')"
        :cancel-text="$t('取消')"
        @ok="saveAsAgent"
        @cancel="saveAgentOpen = false"
      >
        <div class="save-agent-form">
          <label class="save-agent-label">{{ $t('名称') }}</label>
          <Input
            v-model:value="agentDraft.name"
            :maxlength="60"
            :placeholder="$t('如：技术评审')"
          />
          <label class="save-agent-label">{{ $t('描述') }}</label>
          <Input
            v-model:value="agentDraft.description"
            :maxlength="200"
            :placeholder="$t('一句话说明职责（可留空）')"
          />
          <p class="save-agent-hint">
            会话正文（{{ agentDraft.system_prompt.length }} 字符）会作为它的系统提示词；
            创建后可在 Agents 页继续调整提示词、工具与工作台绑定。
          </p>
        </div>
      </Modal>
    </div>

    <!-- 侧面板 -->
    <Transition name="overlay-fade">
      <div
        v-if="panelOpen"
        class="panel-overlay"
        aria-hidden="true"
        @click="panelOpen = false"
      />
    </Transition>
    <ChatSidePanel
      :open="panelOpen"
      :view="panelView"
      :items="items"
      :selected-index="trajectoryFocus"
      :sessions="sessions"
      :active-session-id="activeSessionId"
      :user-name="authStore.user?.name"
      :context-chips="contextChips"
      :live-events="subagentLiveEvents"
      @update:view="(v: 'trajectory' | 'sessions' | 'agents' | 'stats') => (panelView = v)"
      @open-map="synapseMapOpen = true"
      @focus="onTrajectoryFocus"
      @close="panelOpen = false"
      @create="createSession"
      @switch="switchSession"
      @delete="requestDelete"
      @rename="openRename"
      @pin="togglePin"
      @share="openShare"
      @tag="setSessionTag"
      @remove-context="removeContextChip"
      @clear-context="clearContext"
    />

    <!-- 新版会话地图：嵌入 public/sessionmap/（照抄自 vendor/dsh-synapse 的前端），
         数据面由 adapter.js 缝合到 Chiron 的 /v1/session-map 与 /v1/conversations。 -->
    <div
      v-if="synapseMapOpen"
      class="synapse-map-overlay"
    >
      <iframe
        ref="synapseFrame"
        class="synapse-map-frame"
        src="/sessionmap/index.html"
        :title="$t('会话地图')"
      />
      <button
        type="button"
        class="synapse-map-close"
        @click="synapseMapOpen = false"
      >
        {{ $t('关闭') }}
      </button>
    </div>

    <!-- 重命名对话框 -->
    <Modal
      :open="!!renameTarget"
      :title="$t('重命名对话')"
      :confirm-loading="renaming"
      :ok-text="$t('保存')"
      :cancel-text="$t('取消')"
      @ok="confirmRename"
      @cancel="renameTarget = null"
    >
      <Input
        v-model:value="renameDraft"
        :placeholder="$t('输入新的对话名称')"
        :maxlength="120"
        @press-enter="confirmRename"
      />
    </Modal>

    <!-- 分享对话框 -->
    <Modal
      :open="shareOpen"
      :title="`分享「${shareTarget?.title || '新对话'}」`"
      :footer="null"
      width="560px"
      @cancel="shareOpen = false"
    >
      <Alert
        type="warning"
        show-icon
        class="share-risk"
        message="分享链接对任何获得链接的人可见"
        :description="$t('请勿分享包含敏感或隐私信息的内容。你可以随时取消分享，取消后链接立即失效。')"
      />

      <template v-if="isGuest">
        <div class="share-guest-tip">
          {{ $t('登录后即可生成分享链接。') }}
        </div>
      </template>

      <template v-else-if="shareInfo">
        <div class="share-link-row">
          <Input
            :model-value="shareUrl()"
            readonly
            class="share-link-input"
          >
            <template #prefix>
              <LinkOutlined />
            </template>
          </Input>
          <Button
            type="primary"
            @click="copyShareLink"
          >
            <template #icon>
              <CopyOutlined />
            </template>
            {{ $t('复制链接') }}
          </Button>
        </div>
        <div class="share-manage">
          <span class="share-manage-hint">{{ $t('链接已公开，任何获得链接的人均可查看。') }}</span>
          <Button
            danger
            :loading="shareRevoking"
            @click="revokeCurrentShare"
          >
            {{ $t('取消分享') }}
          </Button>
        </div>
      </template>

      <template v-else>
        <div class="share-select-title">
          选择要分享的消息（{{ shareMessageIds.length }}/{{ shareCandidates.length }}）
        </div>
        <div class="share-select-list">
          <label
            v-for="c in shareCandidates"
            :key="c.id"
            class="share-select-item"
            @click.prevent="toggleShareMessage(c.id)"
          >
            <Checkbox
              :checked="shareMessageIds.includes(c.id)"
              @click.stop
            />
            <span
              class="share-select-role"
              :class="c.role"
            >{{ c.role === 'user' ? '用户' : 'AI' }}</span>
            <span class="share-select-preview">{{ c.preview || '（空消息）' }}</span>
          </label>
        </div>
        <div
          v-if="shareError"
          class="share-error"
        >
          {{ shareError }}
        </div>
        <div class="share-actions">
          <Button
            type="primary"
            :loading="shareLoading"
            @click="generateShare"
          >
            {{ $t('生成分享链接') }}
          </Button>
        </div>
      </template>
    </Modal>
  </div>
</template>

<style scoped>
.chat-layout { position: relative; display: flex; height: 100%; background: var(--bg-page); overflow: hidden; }
.chat-main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
/* 交换布局：侧边栏移到左侧、分屏移到右侧。
   用 flex `order` 而不是改 DOM —— 组件的挂载位置不动，只是视觉顺序变化，
   所以不影响任何依赖 DOM 顺序的逻辑（焦点流、SSR、既有测试）。
   .side-panel 默认在 DOM 末尾（order 0 → 靠右），给它 -1 就排到最左；
   .preview-pane 默认在 DOM 最前（order 0 → 靠左），给它 1 就排到最右。 */
/* ⚠️ 必须用 :deep() —— 这是这个按钮一开始"点了没反应"的原因。
   .side-panel 与 .preview-pane 都是**子组件的根元素**，带的是它们自己的
   data-v 作用域 id。父组件 scoped 里直写 `.chat-layout.is-swapped .side-panel`
   会被编译成 `.chat-layout.is-swapped .side-panel[data-v-父]`，**永不匹配**。
   :deep() 让作用域 id 落在 .chat-layout（父组件自己的元素）上，从而真正命中。 */
.chat-layout.is-swapped :deep(.side-panel) { order: -1; }
.chat-layout.is-swapped :deep(.preview-pane) { order: 1; }
.chat-body { position: relative; flex: 1; display: flex; flex-direction: column; min-height: 0; }
.chat-toolbar {
  flex: none;
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  align-items: center;
  height: 40px;
  padding: 0 16px;
  border-bottom: 1px solid var(--border);
}
.toolbar-side { display: flex; align-items: center; gap: 8px; min-width: 0; }
.toolbar-center {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  justify-content: center;
}
.toolbar-title {
  max-width: 40vw;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.toolbar-mode { flex: none; font-size: 11px; color: var(--text-tertiary); background: var(--bg-secondary); padding: 2px 8px; border-radius: var(--radius-full); }
/* 思考档位按钮：复用 .toolbar-mode 的胶囊外观，只重置 button 默认样式 */
.toolbar-effort { border: none; cursor: pointer; font: inherit; font-size: 11px; }
.toolbar-effort:hover { color: var(--text-primary); }
.toolbar-effort.active { color: var(--primary); }
.toolbar-actions { justify-content: flex-end; gap: 2px; }
.toolbar-btn { color: var(--text-secondary); border-radius: var(--radius-md); }
.toolbar-btn:hover { color: var(--text-primary) !important; background: var(--bg-hover) !important; }
.toolbar-btn.active { color: var(--primary); background: var(--primary-bg); }
.toolbar-btn:not(:disabled):active { transform: scale(0.94); }

/* 会话内检索条：贴在工具条下方，不占用消息区高度（无结果时只显示输入框） */
.chat-search {
  flex: none;
  display: flex; align-items: center; gap: 8px;
  margin: 8px 16px 0; padding: 5px 10px;
  border: 1px solid var(--border); border-radius: var(--radius-lg);
  background: var(--bg-card);
}
.chat-search-icon { flex: none; font-size: 13px; color: var(--text-tertiary); }
.chat-search-input { flex: 1; min-width: 0; border: none; background: none; outline: none; color: var(--text-primary); font-size: 13px; }
.chat-search-input::placeholder { color: var(--text-quaternary); }
.chat-search-count { flex: none; font-size: 11px; color: var(--text-tertiary); font-variant-numeric: tabular-nums; white-space: nowrap; }
.chat-search-actions { flex: none; display: flex; gap: 2px; }
.chat-search-btn { border: none; background: none; color: var(--text-secondary); font-size: 12px; line-height: 18px; padding: 2px 6px; border-radius: var(--radius-sm); cursor: pointer; }
.chat-search-btn:hover:not(:disabled) { background: var(--bg-hover); color: var(--text-primary); }
.chat-search-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.chat-search-btn:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
@media (max-width: 576px) { .chat-search { margin: 6px 12px 0; } .chat-search-input { font-size: 12px; } }
@media (max-width: 1024px) {
  .chat-toolbar { padding: 0 10px; }
  .toolbar-title { max-width: 32vw; }
}
@media (max-width: 768px) {
  .chat-toolbar { padding: 0 8px; }
  .toolbar-actions { gap: 0; }
  .toolbar-btn.ant-btn { height: 36px; min-width: 36px; padding: 0 8px; }
  .toolbar-label { display: none; }
}
@media (max-width: 576px) {
  .chat-toolbar { padding: 0 6px; }
  .toolbar-title { max-width: 24vw; font-size: 12px; }
  .toolbar-mode { display: none; }
  .approval-zone { padding: 0 12px 8px; }
  .turn-status { margin: 8px auto 0; padding: 0 12px; }
}
.approval-zone { padding: 0 20px 8px; display: flex; flex-direction: column; gap: 8px; }
/* 结构化提问卡片区：与审批卡同样贴输入区上方 */
.ask-zone { padding: 0 20px 8px; display: flex; flex-direction: column; gap: 8px; }
.approval-card { background: var(--bg-card); border: 1px solid var(--border); border-left: 3px solid var(--primary); border-radius: 10px; padding: 10px 14px; }
/* 工具授权模式栏（与「对话模式」并列但语义独立的第二个维度） */
/* 工具授权已内联到输入区底栏（模型选择器右侧），样式见 ChatInput.vue 的
   `.tools-mode-select` —— 这里不再需要独立的 `.tools-mode-bar`。 */
.approval-countdown { margin-left: auto; font-size: 11px; color: var(--warning, #f59e0b); }
.approval-info { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.approval-tag { font-size: 11px; color: var(--primary); background: var(--primary-bg); padding: 2px 8px; border-radius: 10px; }
.approval-name { font-weight: 600; font-size: 13px; color: var(--text-primary); }
.approval-args { font-family: var(--font-mono); font-size: 12px; color: var(--text-muted); word-break: break-all; margin-bottom: 8px; }
.approval-actions { display: flex; gap: 8px; }
.approval-btn { border: none; border-radius: 8px; padding: 6px 16px; font-size: 13px; cursor: pointer; transition: transform 0.1s ease, opacity 0.15s ease, background 0.15s ease; }
.approval-btn:active { transform: scale(0.97); }
.approval-btn.allow { background: var(--primary); color: #fff; }
.approval-btn.allow:hover { opacity: 0.9; }
.approval-btn.danger { background: var(--bg-hover); color: var(--text-primary); }
.approval-btn.danger:hover { background: var(--danger-bg, rgba(239,68,68,.12)); color: var(--danger, #ef4444); }
.connection-banner {
  position: fixed; top: 0; left: 0; right: 0; z-index: 100;
  padding: 4px 12px; text-align: center;
  font-size: 12px; line-height: 18px;
  background: var(--error); color: #fff;
}
.share-risk { margin-bottom: 14px; }
.share-guest-tip { padding: 20px 0; text-align: center; color: var(--text-secondary); font-size: 14px; }
.share-link-row { display: flex; gap: 10px; margin-top: 14px; }
.share-link-input { flex: 1; }
.share-manage { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 14px; }
.share-manage-hint { font-size: 12px; color: var(--text-tertiary); }
.share-select-title { font-size: 13px; font-weight: 600; color: var(--text-primary); margin: 14px 0 8px; }
.share-select-list { display: flex; flex-direction: column; gap: 2px; max-height: 260px; overflow-y: auto; width: 100%; }
.share-select-item {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 8px; border-radius: 8px; cursor: pointer;
  transition: background 0.15s ease;
}
.share-select-item:hover { background: var(--bg-hover); }
.share-select-role { flex: none; font-size: 11px; font-weight: 600; padding: 1px 7px; border-radius: 10px; }
.share-select-role.user { color: var(--primary); background: var(--primary-bg); }
.share-select-role.assistant { color: var(--text-secondary); background: var(--bg-secondary); }
.share-select-preview { flex: 1; min-width: 0; font-size: 13px; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.share-error { margin-top: 10px; font-size: 12px; color: var(--error); }
.share-actions { display: flex; justify-content: flex-end; margin-top: 14px; }
.turn-status {
  align-self: flex-start; margin: 10px auto 0; max-width: min(var(--chat-content-width), 92%); padding: 0 24px;
  height: 26px; display: inline-flex; align-items: center;
  font-size: 13px; font-weight: 600; white-space: nowrap;
  background: linear-gradient(90deg, var(--primary) 0%, var(--primary) 40%, var(--accent) 50%, var(--primary) 60%, var(--primary) 100%);
  background-position: 100% 0; background-size: 250% 100%; background-clip: text; -webkit-background-clip: text;
  color: transparent; -webkit-text-fill-color: transparent;
  animation: turnStatusShimmer 1.8s linear infinite;
  font-variant-numeric: tabular-nums;
}
@keyframes turnStatusShimmer { to { background-position: 0 0; } }
@media (prefers-reduced-motion: reduce) {
  .turn-status { background-position: 0 0; background-size: 100% 100%; animation: none; }
}
.panel-overlay {
  position: fixed; inset: 0; z-index: 110;
  background: rgba(10, 10, 12, 0.35);
}
@media (min-width: 1025px) { .panel-overlay { display: none; } }
.overlay-fade-enter-active, .overlay-fade-leave-active { transition: opacity 0.2s ease; }
.overlay-fade-enter-from, .overlay-fade-leave-to { opacity: 0; }

.unified-error-banner {
  flex: none; display: flex; align-items: center; gap: 8px;
  margin: 8px 20px 0; padding: 8px 12px;
  background: rgba(239, 68, 68, 0.08); border: 1px solid rgba(239, 68, 68, 0.25);
  border-radius: 10px; color: var(--danger, #ef4444); font-size: 13px;
}
.ueb-text { flex: 1; min-width: 0; line-height: 1.5; }
.ueb-close { flex: none; cursor: pointer; opacity: 0.7; font-size: 12px; }
.ueb-close:hover { opacity: 1; }

.unified-bar {
  flex: none; display: flex; align-items: center; gap: 8px;
  margin: 8px 20px 0; padding: 6px 10px;
  border: 1px solid var(--border); border-radius: 10px;
  background: var(--bg-card);
}
.ub-badge {
  flex: none; display: inline-flex; align-items: center; gap: 6px;
  padding: 1px 10px; border-radius: 10px;
  background: var(--primary); color: #fff;
  font-size: 11px; font-weight: 600; line-height: 18px;
  transition: background 0.3s ease;
}
.ub-badge::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: rgba(255, 255, 255, 0.85); }
.ub-badge.running::before {
  animation: uehPulse 1.1s ease-in-out infinite;
  box-shadow: 0 0 0 0 rgba(255, 255, 255, 0.5);
}
.ub-badge.running { animation: uehPulse 1.1s ease-in-out infinite; }
.ub-badge.done { background: var(--success); }
.ub-badge.done::before { animation: none; }
.ub-mode {
  flex: none; font-size: 11px; color: var(--text-secondary);
  background: var(--bg-secondary); padding: 1px 8px; border-radius: 10px; line-height: 18px;
}
.ub-spacer { flex: 1; }
.ub-btn {
  flex: none; border: 1px solid var(--border); border-radius: 8px;
  background: var(--bg-card); color: var(--text-secondary);
  font-size: 11px; line-height: 18px; padding: 1px 10px; cursor: pointer;
  transition: all 0.15s ease;
}
.ub-btn:focus-visible,
.ueb-close:focus-visible,
.approval-btn:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: 2px;
}
.ueb-close:focus-visible { border-radius: 4px; }
.ub-btn:hover:not(:disabled) { border-color: var(--primary); color: var(--primary); }
.ub-btn.exit:hover:not(:disabled) { border-color: var(--danger, #ef4444); color: var(--danger, #ef4444); }
.ub-btn:disabled { opacity: 0.4; cursor: not-allowed; }

.unified-exec-hint {
  flex: none; display: inline-flex; align-items: center; gap: 6px;
  align-self: center; margin: 10px auto 0;
  font-size: 12px; color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}
.ueh-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--primary);
  animation: uehPulse 1.2s ease-in-out infinite;
}
@keyframes uehPulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.25; } }
@media (prefers-reduced-motion: reduce) { .ueh-dot { animation: none; } }
@media (prefers-reduced-motion: reduce) {
  .ub-badge.running,
  .ub-badge.running::before { animation: none; }
  .ub-badge { transition: none; }
}

.unified-list { flex: 1; overflow-y: auto; padding: 12px 0 24px; scrollbar-width: thin; scrollbar-color: var(--text-disabled) transparent; }
.unified-empty { padding: 40px 20px; text-align: center; color: var(--text-muted); font-size: 13px; }
.kb-hits-tag {
  display: flex; align-items: center;
  max-width: min(var(--chat-content-width), 92%); margin: 2px auto 6px;
  padding: 2px 10px; border-radius: 10px;
  background: var(--primary-bg); color: var(--primary);
  font-size: 11px; line-height: 18px;
}
.kb-hits-text { flex: 1; min-width: 0; }
.kb-hits-link {
  flex: none; margin-left: auto; padding-left: 12px;
  color: var(--primary); font-weight: 600; white-space: nowrap;
  text-decoration: none;
}
.kb-hits-link:hover { text-decoration: underline; }
@media (max-width: 576px) {
  .unified-error-banner { margin: 6px 12px 0; }
  .unified-bar { margin: 6px 12px 0; }
  .kb-hits-tag { max-width: 88%; }
}

/* ── 存为 Agent 弹窗 ── */
.save-agent-form { display: flex; flex-direction: column; gap: 6px; }
.save-agent-label { font-size: 12px; font-weight: 600; color: var(--text-secondary); }
.save-agent-hint { margin: 8px 0 0; font-size: 12px; line-height: 1.6; color: var(--text-secondary); }

@media (max-width: 768px) {
  .map-overlay {
    padding: 0;
  }

  .map-frame {
    border: none;
    border-radius: 0;
  }

  .map-sub {
    display: none;
  }
}

/* 新版会话地图（嵌入 vendor/dsh-synapse 前端）：整屏 iframe + 一个关闭按钮。
   它自带完整样式（public/sessionmap/styles.css），这里只负责承载与层级 ——
   所以全部走 Chiron 的 token，不引入硬编码值。 */
.synapse-map-overlay {
  position: fixed;
  inset: 0;
  z-index: var(--z-viewer);
  background: var(--bg-page);
}
.synapse-map-frame {
  display: block;
  width: 100%;
  height: 100%;
  border: 0;
}
.synapse-map-close {
  position: absolute;
  top: var(--space-3);
  right: var(--space-4);
  padding: 4px var(--space-3);
  font-size: var(--fs-sm);
  color: var(--text-primary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  cursor: pointer;
}
.synapse-map-close:hover { border-color: var(--accent); color: var(--accent); }
</style>