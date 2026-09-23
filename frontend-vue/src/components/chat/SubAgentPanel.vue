<script setup lang="ts">
/**
 * 子 Agent 观测面板：递归层级树 + 选中 run 的实时输出。
 *
 * 数据来源（三层，见 docs/subagent-design.md §3.4/§5.2）：
 *   1. 树骨架与状态：GET /v1/subagent/runs（Redis → DB），轮询刷新；
 *   2. 实时输出：父组件（ChatView）把 SSE 的 `subagent.*` 事件按到达顺序传进来（liveEvents）；
 *   3. 历史输出：选中 run 时 GET /v1/subagent/runs/{id}/events 拉一次，再与实时事件拼接。
 *
 * 层级用「depth 缩进 + 折叠」呈现：后端返回的扁平列表已带 depth/parent_run_id，
 * 折叠即按 parent_run_id 隐藏整棵子树 —— 比递归组件更省，且不会因深层嵌套而抖。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Empty, Spin, Tag, Tooltip, message } from 'ant-design-vue'
import { submitApproval } from '../../api'
import {
  cancelSessionSubagents,
  cancelSubagentRun,
  getSubagentRunEvents,
  listSubagentRuns,
  type SubagentEvent,
  type SubagentRunView,
} from '../../api/subagent'
import SubAgentApprovalCard from './SubAgentApprovalCard.vue'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()

const props = withDefaults(defineProps<{
  /** 当前会话 id；为空时面板提示"无会话" */
  sessionId?: string
  /** 父组件累积的实时事件（按到达顺序），只包含当前会话的 subagent.* 事件 */
  liveEvents?: SubagentEvent[]
}>(), { sessionId: '', liveEvents: () => [] })

const runs = ref<SubagentRunView[]>([])
const loading = ref(false)
const source = ref('')
const selectedRunId = ref('')
const historyEvents = ref<SubagentEvent[]>([])
const historyLoading = ref(false)
const collapsed = ref<Set<string>>(new Set())
const COST_HINT_KEY = 'chiron.subagent.costHintSeen'
const showCostHint = ref(false)

let timer: ReturnType<typeof globalThis.setInterval> | undefined

const STATUS_COLOR: Record<string, string> = {
  queued: 'default',
  running: 'processing',
  reasoning: 'processing',
  responding: 'processing',
  tool: 'processing',
  retrying: 'warning',
  completed: 'success',
  failed: 'error',
  cancelled: 'default',
}

/** 是否被某个已折叠的祖先隐藏（沿 parent_run_id 上溯）。 */
function hiddenByAncestor(run: SubagentRunView): boolean {
  const byId = new Map(runs.value.map(r => [r.run_id, r]))
  let parentId = run.parent_run_id || ''
  let guard = 0
  while (parentId && guard++ < 20) {
    if (collapsed.value.has(parentId)) return true
    parentId = byId.get(parentId)?.parent_run_id || ''
  }
  return false
}

/**
 * 从**实时事件**合成的运行条目 —— 观测面的第二个数据源。
 *
 * 为什么必须有它：`GET /v1/subagent/runs` 依赖后端把运行写进 journal/Redis，那条链路
 * 任何一环出问题（或该 run 尚未落库），面板就会显示"本次会话还没有子 Agent 运行" ——
 * 而 SSE 里其实正在源源不断地送 `subagent.*` 事件。观测不该因为一个数据源失败而全瞎。
 * （与 ZCode 的"run 的真相在 journal、观察面出问题不影响可用性"同一思路。）
 */
const liveRuns = computed(() => {
  const known = new Set(runs.value.map(r => r.run_id))
  const out: Record<string, any> = {}
  for (const raw of props.liveEvents) {
    const e = raw as any
    const id: string = e?.run_id
    if (!id || known.has(id)) continue // API 已有 → 以 API 为准（它带摘要与用量）
    const prev = out[id]
    const done = e.type === 'subagent.done'
    out[id] = {
      run_id: id,
      parent_run_id: e.parent_run_id || prev?.parent_run_id || '',
      depth: Number(e.depth ?? prev?.depth ?? 1) || 1,
      profile: e.profile || prev?.profile || '',
      status: done ? (e.status || 'done') : (prev?.status || 'running'),
      summary: done ? (e.summary ?? prev?.summary) : prev?.summary,
      usage: e.usage ?? prev?.usage,
      created_at: prev?.created_at,
    }
  }
  return Object.values(out)
})

/** 面板实际展示的运行：**API 结果优先，实时事件补齐**（按 run_id 去重） */
const displayRuns = computed(() => {
  const apiIds = new Set(runs.value.map(r => r.run_id))
  return [...runs.value, ...liveRuns.value.filter(r => !apiIds.has(r.run_id))]
})

/**
 * 每个 run 的**最新一段实时输出**，用于列表行上的"它现在在做什么"。
 *
 * 为什么需要：事件流 tab 只渲染选中的 run —— 用户不点开就只看得到状态标签。
 * 这里把每个 run 的最后一段内容（尾部 120 字符）挂到行上，未展开也能看到进度在流动。
 */
const liveTail = computed(() => {
  const out: Record<string, string> = {}
  for (const raw of props.liveEvents || []) {
    const e = raw as { run_id?: string; content?: string }
    const id = e?.run_id
    if (!id || typeof e.content !== 'string' || !e.content) continue
    out[id] = e.content.replace(/\s+/g, ' ').slice(-120)
  }
  return out
})

/** 是否还有运行中的 run（决定「全部停止」是否出现） */
const hasRunning = computed(() => displayRuns.value.some(r => !TERMINAL_STATUSES.has(r.status)))

/** 已请求取消的 run（本地即时反馈；终态到达后由事件/轮询清掉） */
const cancelling = ref<Set<string>>(new Set())

/**
 * 停止单个 run。
 *
 * 只是「请求已受理」：网关做租户校验 + Redis 广播 → 持有该 run 的引擎实例 `task.cancel()`
 * → 收尾后终态（status=cancelled, error=cancelled_by_user）经事件/轮询回来。
 * 所以这里**不直接改 status** —— 状态的真相在引擎，前端只做按钮禁用这类即时反馈。
 */
async function stopRun(runId: string) {
  if (cancelling.value.has(runId)) return
  cancelling.value = new Set(cancelling.value).add(runId)
  try {
    const res = await cancelSubagentRun(runId)
    if (res.status === 'lost') {
      // 网关等不到持有实例的回执（实例已重启/被驱逐）→ 它把作业标成了 lost。
      // 如实告诉用户，而不是让"正在停止"一直挂着、状态永远不变（假成功）。
      void message.warning(t('该子 Agent 已失联（实例已重启或退出），已标记为丢失'))
    }
  } catch {
    // 失败就放开按钮让用户能重试（不做假成功提示）
    const next = new Set(cancelling.value)
    next.delete(runId)
    cancelling.value = next
  }
}

/** 停止该会话下所有活跃子 Agent（一次清场） */
async function stopAll() {
  if (!props.sessionId) return
  try {
    await cancelSessionSubagents(props.sessionId)
    cancelling.value = new Set([
      ...cancelling.value,
      ...displayRuns.value.filter(r => !TERMINAL_STATUSES.has(r.status)).map(r => r.run_id),
    ])
  } catch {
    /* 忽略：下一次 tick 会重新渲染，用户可再点一次 */
  }
}

/** 终态到达后清掉本地 cancelling 标记，避免按钮永久禁用 */
watch(displayRuns, (list) => {
  if (!cancelling.value.size) return
  const next = new Set([...cancelling.value].filter(id =>
    !list.some(r => r.run_id === id && TERMINAL_STATUSES.has(r.status))))
  if (next.size !== cancelling.value.size) cancelling.value = next
})

// ── 子 Agent 审批（P3-后续）──────────────────────────────────────────────
//
// 子 Agent 默认 tools_mode=auto，而 shell_exec 这类工具在 auto 下就需要确认 ——
// 所以"子 Agent 请求审批"几乎每次调用命令类工具都会发生。此前它只以一行 notice
// 出现，用户能看见却**没有任何办法批准**：子 Agent 空转到 300s 超时后以
// "approval timed out" 被拒。这里把 approval 事件渲染成**可点击**的卡片，
// 决定经与主 Agent 完全相同的通道回传（POST /v1/agent/approval）。
//
// 回传凭据只认 `tool_call_id`（见 api/subagent.ts 的说明）。

/** 已提交决定的 tool_call_id → 本地结论（避免重复提交，也给用户一个即时反馈） */
const approvalDecisions = ref<Record<string, 'approved' | 'denied'>>({})
/** 正在提交的 tool_call_id */
const approvalSubmitting = ref<Set<string>>(new Set())
/** 提交失败的原因（按 tool_call_id），失败时保留按钮让用户重试 —— 不做假成功 */
const approvalErrors = ref<Record<string, string>>({})

/** 收集所有已到达的 approval 事件（历史回放 + 实时），按 tool_call_id 去重且保序。 */
const approvalEvents = computed(() => {
  const seen = new Set<string>()
  const out: Array<{ toolCallId: string; runId: string; toolName: string; args: string; content: string }> = []
  for (const e of props.liveEvents || []) {
    const id = e?.tool_call_id
    if (e?.type !== 'subagent.approval' || !id || seen.has(id)) continue
    seen.add(id)
    out.push({
      toolCallId: id,
      runId: e.run_id || '',
      toolName: e.tool_name || '',
      args: e.tool_arguments || '',
      content: e.content || '',
    })
  }
  return out
})

/** 尚未决定、也未被终态作废的审批（子 Agent 已结束 → 卡片自动消失）。 */
const pendingApprovals = computed(() => {
  const terminal = new Set(
    displayRuns.value.filter(r => TERMINAL_STATUSES.has(r.status)).map(r => r.run_id),
  )
  return approvalEvents.value.filter(
    a => !approvalDecisions.value[a.toolCallId] && !terminal.has(a.runId),
  )
})

/** 每个 run 待确认数（列表行上的可见标记：不点开也知道有东西在等）。 */
const pendingByRun = computed(() => {
  const out: Record<string, number> = {}
  for (const a of pendingApprovals.value) out[a.runId] = (out[a.runId] || 0) + 1
  return out
})

/** 审批参数：美化后的 JSON 已由 SubAgentApprovalCard 负责 —— 面板只做数据与回传。 */

/**
 * 提交子 Agent 审批决定。
 *
 * 只做一次乐观标记：**状态真相在引擎**（批准后子 Agent 会继续产出事件；
 * 拒绝则它收到 error 并在结果里体现），前端不伪造后续状态。
 */
async function resolveApproval(a: { toolCallId: string }, approved: boolean) {
  const id = a.toolCallId
  if (!id || approvalSubmitting.value.has(id) || approvalDecisions.value[id]) return
  approvalSubmitting.value = new Set(approvalSubmitting.value).add(id)
  const errors = { ...approvalErrors.value }
  delete errors[id]
  approvalErrors.value = errors
  try {
    const ok = await submitApproval({
      session_id: props.sessionId || '',
      tool_call_id: id,
      approved,
    })
    if (!ok) {
      // 引擎返回 ok=false：通常是超时（决定到得太晚）或该调用已不在等待。
      // 明确说出来，而不是把卡片默默撤掉（用户会以为点生效了）。
      approvalErrors.value = { ...approvalErrors.value, [id]: t('审批未生效（可能已超时）') }
      return
    }
    approvalDecisions.value = { ...approvalDecisions.value, [id]: approved ? 'approved' : 'denied' }
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err)
    approvalErrors.value = { ...approvalErrors.value, [id]: detail }
  } finally {
    const next = new Set(approvalSubmitting.value)
    next.delete(id)
    approvalSubmitting.value = next
  }
}

/** 点列表行上的"待确认"标记：选中该 run 并切到「输出」（可操作卡片在那里）。 */
function focusApprovals(runId: string) {
  saTab.value = 'output'
  void selectRun(runId)
}

const visibleRuns = computed(() => displayRuns.value.filter(r => !hiddenByAncestor(r)))

const selectedRun = computed<SubagentRunView | null>(() => {
  const id = selectedRunId.value
  if (!id) return null
  const known = runs.value.find(r => r.run_id === id)
  if (known) return known
  // 回退到**实时事件合成的条目**：后台 run 往往还没落进 GET /v1/subagent/runs，
  // 而「输出」区（审批卡片就在这里）由 selectedRun 决定是否渲染 ——
  // 只认 API 会让"点得开待确认标记、却看不到卡片"，等于又回到批不了的状态。
  return (liveRuns.value.find(r => r.run_id === id) as SubagentRunView | undefined) ?? null
})

/** 选中 run 的事件流 = 历史（回放）+ 实时（本会话累积中属于该 run 的部分）。 */
const selectedEvents = computed<SubagentEvent[]>(() => {
  const runId = selectedRunId.value
  if (!runId) return []
  const live = (props.liveEvents || []).filter(e => e.run_id === runId)
  // 历史里可能已含同 id 的事件（Redis Stream 的 id）——按 id 去重后再拼接
  const seen = new Set(historyEvents.value.map(e => e.id).filter(Boolean) as string[])
  return [...historyEvents.value, ...live.filter(e => !e.id || !seen.has(e.id))]
})

const totals = computed(() => {
  let input = 0
  let output = 0
  for (const run of runs.value) {
    input += run.usage?.input_tokens || 0
    output += run.usage?.output_tokens || 0
  }
  return { input, output }
})

const childCount = computed(() => {
  const map = new Map<string, number>()
  for (const run of runs.value) {
    const parent = run.parent_run_id || ''
    map.set(parent, (map.get(parent) || 0) + 1)
  }
  return map
})

function toggleCollapse(runId: string) {
  const next = new Set(collapsed.value)
  if (next.has(runId)) next.delete(runId)
  else next.add(runId)
  collapsed.value = next
}

async function loadRuns() {
  if (!props.sessionId) {
    runs.value = []
    return
  }
  loading.value = runs.value.length === 0
  try {
    const res = await listSubagentRuns({ sessionId: props.sessionId })
    runs.value = res.runs
    source.value = res.source
    followActiveRun(res.runs)
  } catch {
    // 面板失败不打断对话；下一轮轮询会再试
  } finally {
    loading.value = false
  }
}

/**
 * 让"选中的 run"始终指向**还在产生内容**的那一个。
 *
 * 为什么必须跟随：选中 run 的增量只靠 3 秒轮询（后台 run 没有 SSE 连接），而轮询在
 * run 到终态后就停了（`tailLoaded`）。此前只在"从未选中"时才自动选第一个 —— 于是一轮
 * 对话结束后（选中的那个已 completed），**之后新派发的子 Agent 永远不会被选中**：
 * 它的进度与审批请求都看不到，用户只能刷新页面（刷新后 selectedRunId 为空 → 重新自选）。
 */
function followActiveRun(list: SubagentRunView[]) {
  if (!list.length) {
    selectedRunId.value = ''
    return
  }
  const active = list.filter(r => !TERMINAL_STATUSES.has(String(r.status)))
  const current = selectedRunId.value
    ? list.find(r => r.run_id === selectedRunId.value)
    : undefined
  if (current && active.some(r => r.run_id === current.run_id)) return // 当前就在跑：不动
  const fallback = list[list.length - 1]?.run_id || ''
  const target = active.length ? active[active.length - 1]?.run_id || '' : (current?.run_id || fallback)
  if (!target || target === selectedRunId.value) return
  selectedRunId.value = target
  // 切换目标时立刻拉一次历史（之后的增量交给 3 秒轮询）
  void selectRun(target)
}

async function selectRun(runId: string) {
  selectedRunId.value = runId
  historyEvents.value = []
  historyLoading.value = true
  try {
    const res = await getSubagentRunEvents(runId)
    historyEvents.value = res.events
  } catch {
    historyEvents.value = []
  } finally {
    historyLoading.value = false
  }
}

/** 已到终态的 run 状态（这些状态之后不再有增量，只补一次尾巴） */
const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled', 'killed', 'stopped', 'lost', 'done'])
/** 已经为某个 run 补过"终态尾巴"的标记（避免终态后继续空轮询） */
const tailLoaded = new Set<string>()

/**
 * 轮询选中 run 的事件流。
 *
 * 为什么需要：选中 run 的事件只有两个来源 —— 父会话 SSE（`props.liveEvents`）与
 * `GET /v1/subagent/runs/{id}/events`。`run_in_background=true` 的子 Agent 在父 turn 结束后
 * 不再有 SSE（事件此时只落运行期缓存），所以必须靠这条轮询把增量拉回来。
 */
async function pollSelectedEvents() {
  const runId = selectedRunId.value
  if (!runId || historyLoading.value) return
  const current = displayRuns.value.find(r => r.run_id === runId)
  const status = String(current?.status || 'running')
  const terminal = TERMINAL_STATUSES.has(status)
  if (terminal && tailLoaded.has(runId)) return
  try {
    const res = await getSubagentRunEvents(runId)
    if (selectedRunId.value !== runId) return // 期间切换了 run：丢弃这次结果
    const seen = new Set(historyEvents.value.map(e => e.id).filter(Boolean) as string[])
    const fresh = res.events.filter(e => !e.id || !seen.has(e.id))
    if (fresh.length) historyEvents.value = [...historyEvents.value, ...fresh]
    if (terminal) tailLoaded.add(runId)
  } catch {
    /* 轮询失败不打断对话；下一次 tick 会再试 */
  }
}

function dismissCostHint() {
  showCostHint.value = false
  try {
    globalThis.localStorage?.setItem(COST_HINT_KEY, '1')
  } catch { /* 隐私模式下不可用，忽略 */ }
}

// 实时流里出现了新 run（通常是刚委派的）→ 立刻刷新树，让卡片尽快出现
/** 面板内部 tab（设计稿第四节 + 产物：运行 · 输出 · 用量 · 产物 · 事件流） */
const SA_TABS = [
  { id: 'runs', label: '运行' },
  { id: 'output', label: '输出' },
  { id: 'usage', label: '用量' },
  { id: 'artifacts', label: '产物' },
  { id: 'events', label: '事件流' },
] as const
const saTab = ref<(typeof SA_TABS)[number]['id']>('runs')

/**
 * 选中运行的**写入路径**。
 *
 * 后端把 `write_paths`（jsonb）**原样透传为字符串**，形状由这里解析 ——
 * 解析不了就当作空（观测面不该因为字段形状不合预期而报错）。
 */
const artifactPaths = computed<string[]>(() => {
  const raw = (selectedRun.value as { write_paths?: string } | null)?.write_paths
  if (!raw) return []
  try {
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.filter((p): p is string => typeof p === 'string') : []
  } catch {
    return []
  }
})

/** 按目录分组：一屏几十个路径平铺是读不出来的，按目录归拢后能一眼看出"动了哪几处" */
const artifactGroups = computed(() => {
  const groups = new Map<string, string[]>()
  for (const path of artifactPaths.value) {
    const idx = path.lastIndexOf('/')
    const dir = idx > 0 ? path.slice(0, idx) : '.'
    const list = groups.get(dir) ?? []
    list.push(path)
    groups.set(dir, list)
  }
  return [...groups.entries()]
    .map(([dir, paths]) => ({ dir, paths }))
    .sort((a, b) => a.dir.localeCompare(b.dir))
})

/** 最近一次已复制的路径（用于就地反馈，避免依赖全局 message 组件） */
const copiedPath = ref('')

async function copyPath(path: string) {
  try {
    await navigator.clipboard.writeText(path)
    copiedPath.value = path
    window.setTimeout(() => {
      if (copiedPath.value === path) copiedPath.value = ''
    }, 1200)
  } catch {
    /* 剪贴板不可用（非安全上下文）：静默，不做假成功提示 */
  }
}

watch(() => props.liveEvents?.length || 0, (len, prev) => {
  if (!len || len === prev) return
  const latest = props.liveEvents?.[len - 1]
  if (latest && !runs.value.some(r => r.run_id === latest.run_id)) void loadRuns()
})

watch(() => props.sessionId, () => { selectedRunId.value = ''; historyEvents.value = []; void loadRuns() })

onMounted(() => {
  try {
    showCostHint.value = globalThis.localStorage?.getItem(COST_HINT_KEY) !== '1'
  } catch { showCostHint.value = true }
  void loadRuns()
  // 3s tick：刷新运行树 + 拉选中 run 的增量事件（后台 run 没有 SSE，只能靠这条）
  timer = globalThis.setInterval(() => {
    void loadRuns()
    void pollSelectedEvents()
  }, 3000)
})

onBeforeUnmount(() => {
  if (timer) globalThis.clearInterval(timer)
})
</script>

<template>
  <div class="subagent-panel">
    <div
      v-if="showCostHint"
      class="cost-hint"
    >
      <span>{{ $t('子 Agent 会独立消耗 token，多个子任务并行时花销成倍增加。') }}</span>
      <button
        type="button"
        class="cost-hint-close"
        @click="dismissCostHint"
      >
        {{ $t('知道了') }}
      </button>
    </div>

    <div class="panel-head">
      <span class="panel-title">{{ $t('子 Agent') }}</span>
      <span class="panel-meta">
        {{ runs.length }} {{ $t('个运行') }} · ↑{{ totals.input }} / ↓{{ totals.output }}
        <Tooltip :title="$t('数据来源：redis=运行期缓存，db=历史记录')">
          <Tag
            v-if="source"
            :color="source === 'redis' ? 'processing' : 'default'"
            class="source-tag"
          >
            {{ source }}
          </Tag>
        </Tooltip>
      </span>
    </div>

    <!-- 面板内部 tab（设计稿第四节）：运行 / 输出 / 用量 / 事件流。
         前两个是"看过程"，后两个是"算花销"与"排查原始事件"。
         用 v-show 而非重排 DOM：保留既有树与输出的全部逻辑，只切换可见性。 -->
    <nav
      class="sa-tabs"
      role="tablist"
      :aria-label="$t('子 Agent 视图')"
    >
      <button
        v-for="t in SA_TABS"
        :key="t.id"
        type="button"
        role="tab"
        class="sa-tab"
        :class="{ active: saTab === t.id }"
        :aria-selected="saTab === t.id"
        @click="saTab = t.id"
      >
        {{ $t(t.label) }}
        <span
          v-if="t.id === 'runs' && runs.length"
          class="sa-tab-count"
        >{{ runs.length }}</span>
        <span
          v-if="t.id === 'events' && liveEvents.length"
          class="sa-tab-count"
        >{{ liveEvents.length }}</span>
      </button>
    </nav>

    <Spin
      v-show="saTab === 'runs'"
      :spinning="loading"
    >
      <Empty
        v-if="!displayRuns.length"
        :description="$t('本次会话还没有子 Agent 运行')"
        :image="Empty.PRESENTED_IMAGE_SIMPLE"
      />
      <div
        v-else
        class="tree"
      >
        <!-- 一次清场：只在确实有运行中的 run 时才出现 -->
        <div
          v-if="hasRunning"
          class="bulk-stop"
        >
          <button
            type="button"
            class="run-stop"
            @click.stop="stopAll"
          >
            {{ $t('停止全部子 Agent') }}
          </button>
        </div>
        <div
          v-for="run in visibleRuns"
          :key="run.run_id"
          class="tree-row"
          :class="{ active: run.run_id === selectedRunId }"
          :style="{ paddingLeft: `${8 + (run.depth - 1) * 14}px` }"
          @click="selectRun(run.run_id)"
        >
          <button
            v-if="childCount.get(run.run_id)"
            type="button"
            class="twisty"
            @click.stop="toggleCollapse(run.run_id)"
          >
            {{ collapsed.has(run.run_id) ? '▸' : '▾' }}
          </button>
          <span
            v-else
            class="twisty placeholder"
          />
          <Tag :color="STATUS_COLOR[run.status] || 'default'">
            {{ run.status }}
          </Tag>
          <span class="run-profile">{{ run.profile || run.run_id }}</span>
          <!-- 运行中的实时预览：最后一段输出（点开该 run 可看完整事件流） -->
          <span
            v-if="liveTail[run.run_id] && !TERMINAL_STATUSES.has(run.status)"
            class="run-tail"
            :title="liveTail[run.run_id]"
          >{{ liveTail[run.run_id] }}</span>
          <!-- 待确认审批：不点开该 run 也要看得见（点它跳到事件流里的可操作卡片） -->
          <button
            v-if="pendingByRun[run.run_id]"
            type="button"
            class="run-approval"
            :title="$t('子 Agent 正在等待你的确认')"
            @click.stop="focusApprovals(run.run_id)"
          >
            ⚠ {{ $t('待确认') }}{{ pendingByRun[run.run_id] > 1 ? ` ×${pendingByRun[run.run_id]}` : '' }}
          </button>
          <span class="run-usage">↑{{ run.usage?.input_tokens || 0 }}/↓{{ run.usage?.output_tokens || 0 }}</span>
          <!-- 中止：只在运行中的 run 上出现（终态没什么可停的） -->
          <button
            v-if="!TERMINAL_STATUSES.has(run.status)"
            type="button"
            class="run-stop"
            :disabled="cancelling.has(run.run_id)"
            :title="$t('停止这个子 Agent')"
            @click.stop="stopRun(run.run_id)"
          >
            {{ cancelling.has(run.run_id) ? '…' : '■' }}
          </button>
          <span
            v-if="run.redacted_count"
            class="run-redacted"
            :title="$t('入库前已脱敏的敏感片段数')"
          >🔒{{ run.redacted_count }}</span>
        </div>
      </div>
    </Spin>

    <!-- 用量：按 run 列明细。会话语义在「统计」浮层，这里是**子 Agent 粒度**，两者不重复 -->
    <div
      v-show="saTab === 'usage'"
      class="usage"
    >
      <div
        v-if="runs.length"
        class="usage-grid"
      >
        <div class="usage-row usage-head">
          <span>{{ $t('运行') }}</span>
          <span>{{ $t('状态') }}</span>
          <span class="num">in</span>
          <span class="num">out</span>
          <span class="num">{{ $t('步数') }}</span>
        </div>
        <div
          v-for="run in runs"
          :key="run.run_id"
          class="usage-row"
        >
          <span class="u-name">{{ run.profile || run.run_id }}</span>
          <Tag :color="STATUS_COLOR[run.status] || 'default'">
            {{ run.status }}
          </Tag>
          <span class="num">{{ run.usage?.input_tokens || 0 }}</span>
          <span class="num">{{ run.usage?.output_tokens || 0 }}</span>
          <span class="num">{{ run.usage?.steps || 0 }}</span>
        </div>
      </div>
      <div
        v-else
        class="sa-empty"
      >
        {{ $t('还没有子 Agent 运行，所以没有用量可算') }}
      </div>
      <p class="usage-note">
        {{ $t('子 Agent 会独立消耗 token，多个并行时花销成倍增加。') }}
      </p>
    </div>

    <!-- 事件流：**原始** subagent.* 事件（未按语义分组），排查"到底发生了什么"时用；
         「输出」tab 才是给人读的结构化过程。 -->
    <div
      v-show="saTab === 'events'"
      class="events"
    >
      <div
        v-for="(e, i) in liveEvents"
        :key="(e.id as string) || i"
        class="ev-row"
      >
        <span class="ev-type">{{ e.type }}</span>
        <span class="ev-run">{{ e.run_id }}</span>
        <span class="ev-text">{{ e.content || '' }}</span>
      </div>
      <div
        v-if="!liveEvents.length"
        class="sa-empty"
      >
        {{ $t('本会话还没有收到子 Agent 事件') }}
      </div>
    </div>

    <!-- 产物：这次运行**写入了哪些文件**（`write_paths`）。
         按目录分组 —— 一屏几十个路径平铺是读不出来的，归拢后能一眼看出"动了哪几处"。
         刻意不做图表/指标：ZCode 的 ArtifactChart/Metrics 围绕其沙箱产物（带结构化元数据），
         而我们目前只有路径，先把这个真实有用的最小版做好。 -->
    <div
      v-show="saTab === 'artifacts'"
      class="artifacts"
    >
      <template v-if="selectedRun">
        <div
          v-if="!artifactPaths.length"
          class="sa-empty"
        >
          {{ $t('这次运行没有写入任何文件') }}
        </div>
        <div
          v-for="group in artifactGroups"
          :key="group.dir"
          class="art-group"
        >
          <div class="art-dir">{{ group.dir }}</div>
          <button
            v-for="path in group.paths"
            :key="path"
            type="button"
            class="art-item"
            :class="{ copied: copiedPath === path }"
            :title="path"
            @click="copyPath(path)"
          >
            {{ copiedPath === path ? $t('已复制') : path.split('/').pop() }}
          </button>
        </div>
      </template>
      <div
        v-else
        class="sa-empty"
      >
        {{ $t('先在「运行」里选一个子 Agent') }}
      </div>
    </div>

    <!-- v-if 负责"无选中不渲染"（同时让 TS 把 selectedRun 收窄为非空），
         v-show 负责 tab 切换 —— 两者可并用；只写 v-show 会丢掉类型收窄。 -->
    <div
      v-if="selectedRun"
      v-show="saTab === 'output'"
      class="output"
    >
      <div class="output-head">
        <span>{{ selectedRun.profile || selectedRun.run_id }}</span>
        <span class="output-meta">
          {{ $t('步数') }} {{ selectedRun.usage?.steps || 0 }}
        </span>
      </div>
      <div class="output-summary">
        {{ selectedRun.summary || $t('（运行中，暂无摘要）') }}
      </div>
      <Spin :spinning="historyLoading">
        <div class="stream">
          <template
            v-for="(event, index) in selectedEvents"
            :key="event.id || index"
          >
            <div
              v-if="event.type === 'subagent.reasoning' && event.content"
              class="line reasoning"
            >
              <span class="line-tag">{{ $t('思考') }}</span>{{ event.content }}
            </div>
            <div
              v-else-if="event.type === 'subagent.text' && event.content"
              class="line text"
            >
              {{ event.content }}
            </div>
            <div
              v-else-if="event.type === 'subagent.notice' && event.content"
              class="line notice"
            >
              {{ event.content }}
            </div>
            <div
              v-else-if="event.type === 'subagent.approval'"
              class="line approval"
            >
              <SubAgentApprovalCard
                :tool-name="event.tool_name || ''"
                :args="event.tool_arguments || ''"
                :content="event.content || ''"
                :submitting="approvalSubmitting.has(event.tool_call_id || '')"
                :decision="approvalDecisions[event.tool_call_id || ''] || ''"
                :error="approvalErrors[event.tool_call_id || ''] || ''"
                @decide="(approved: boolean) => resolveApproval({ toolCallId: event.tool_call_id || '' }, approved)"
              />
            </div>
            <div
              v-else-if="event.type === 'subagent.status'"
              class="line status"
            >
              {{ event.status }}<span v-if="event.truncated"> · {{ $t('预览已截断') }}</span>
            </div>
            <div
              v-else-if="event.type === 'subagent.done'"
              class="line done"
            >
              {{ $t('结束') }}：{{ event.status }}
              <span v-if="event.usage"> · ↑{{ event.usage.input_tokens || 0 }}/↓{{ event.usage.output_tokens || 0 }}</span>
            </div>
          </template>
          <div
            v-if="!selectedEvents.length"
            class="stream-empty"
          >
            {{ $t('暂无输出（运行中或过程已过期）') }}
          </div>
        </div>
      </Spin>
    </div>
  </div>
</template>

<style scoped>
.subagent-panel { display: flex; flex-direction: column; gap: 10px; padding: 8px; font-size: 13px; }

.cost-hint {
  display: flex; align-items: flex-start; gap: 8px;
  padding: 8px 10px; border-radius: 6px;
  background: var(--warning-bg, rgba(245, 158, 11, 0.12));
  color: var(--text-secondary, #595959); line-height: 1.5;
}
.cost-hint-close {
  flex: none; border: none; background: none; cursor: pointer;
  color: var(--primary-color, #1677ff); font-size: 13px;
}

.panel-head { display: flex; align-items: center; justify-content: space-between; }
.panel-title { font-weight: 600; }
.panel-meta { color: var(--text-tertiary, #8c8c8c); font-size: 12px; display: flex; align-items: center; gap: 6px; }
.source-tag { margin: 0; }

.tree { display: flex; flex-direction: column; }
.tree-row {
  display: flex; align-items: center; gap: 6px;
  padding: 4px 8px; border-radius: 6px; cursor: pointer;
}
.tree-row:hover { background: var(--surface-2, rgba(127, 127, 127, 0.08)); }
.tree-row.active { background: var(--primary-light, rgba(22, 119, 255, 0.12)); }
.twisty {
  width: 14px; flex: none; border: none; background: none; cursor: pointer;
  color: var(--text-tertiary, #8c8c8c); font-size: 11px; padding: 0;
}
.twisty.placeholder { cursor: default; }
.run-profile { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.run-usage, .run-redacted { font-size: 12px; color: var(--text-tertiary, #8c8c8c); }
/* 中止按钮：紧凑型，别把行撑高；hover 用错误色（停止是破坏性动作） */
.run-stop {
  flex: none; padding: 0 6px; line-height: 18px; cursor: pointer;
  font-size: 11px; color: var(--text-secondary, #595959);
  background: transparent; border: 1px solid var(--border-color, rgba(127, 127, 127, 0.24));
  border-radius: 4px;
}
.run-stop:hover:not(:disabled) { color: var(--error, #cf1322); border-color: var(--error, #cf1322); }
.run-stop:disabled { opacity: 0.5; cursor: default; }
.bulk-stop { display: flex; justify-content: flex-end; padding: 0 8px 6px; }

/* 运行中 run 的实时预览：尾巴对齐（看到的永远是最新输出），完整内容点开看 */
.run-tail {
  flex: 1 1 auto; min-width: 40px; max-width: 50%;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: 11px; color: var(--text-tertiary, #8c8c8c);
}

/* 待确认标记：让"有东西在等我"在不点开该 run 时也看得见 */
.run-approval {
  flex: none; padding: 0 6px; line-height: 18px; cursor: pointer;
  font-size: 11px; color: var(--warning, #f59e0b);
  background: transparent; border: 1px solid var(--warning, #f59e0b); border-radius: 4px;
}

/* 子 Agent 审批卡片的视觉在 SubAgentApprovalCard.vue（与主对话区的确认卡片同一套） */
.line.approval { width: 100%; }

.output { border-top: 1px solid var(--border-color, rgba(127, 127, 127, 0.24)); padding-top: 10px; }
.output-head { display: flex; justify-content: space-between; font-weight: 600; }
.output-meta { font-size: 12px; color: var(--text-tertiary, #8c8c8c); }
.output-summary {
  margin: 6px 0; padding: 8px; border-radius: 6px;
  background: var(--surface-2, rgba(127, 127, 127, 0.06));
  white-space: pre-wrap; max-height: 160px; overflow: auto;
}
.stream { max-height: 320px; overflow: auto; display: flex; flex-direction: column; gap: 4px; }
.line { white-space: pre-wrap; line-height: 1.5; }
.line.reasoning { color: var(--text-tertiary, #8c8c8c); font-style: italic; }
.line.notice { color: var(--warning, #f59e0b); }
.line.status, .line.done { color: var(--text-secondary, #595959); font-size: 12px; }
.line-tag {
  display: inline-block; margin-right: 4px; padding: 0 4px; border-radius: 4px; font-size: 11px;
  background: var(--surface-2, rgba(127, 127, 127, 0.12));
}
.stream-empty { color: var(--text-tertiary, #8c8c8c); font-size: 12px; }

/* ── 内部 tab（运行 · 输出 · 用量 · 事件流）── */
.sa-tabs {
  display: flex; gap: 2px; margin: 8px 0 10px;
  padding: 2px; border-radius: 8px;
  background: var(--surface-2, rgba(127, 127, 127, 0.06));
}
.sa-tab {
  flex: 1; display: inline-flex; align-items: center; justify-content: center; gap: 4px;
  padding: 4px 8px; border: none; border-radius: 6px; cursor: pointer;
  background: transparent; color: var(--text-secondary, #595959); font-size: 12px;
}
.sa-tab:hover { color: var(--text-primary, #262626); }
.sa-tab.active {
  background: var(--bg-elevated, #fff); color: var(--text-primary, #262626);
  font-weight: 600; box-shadow: 0 1px 2px rgba(0, 0, 0, 0.06);
}
.sa-tab-count {
  min-width: 16px; padding: 0 4px; border-radius: 8px;
  background: var(--surface-2, rgba(127, 127, 127, 0.16));
  font-size: 10px; font-variant-numeric: tabular-nums;
}
.sa-empty { padding: 10px 2px; color: var(--text-tertiary, #8c8c8c); font-size: 12px; }

/* 用量表：数字列右对齐 + tabular-nums，便于竖排比对 */
.usage-grid { display: flex; flex-direction: column; }
.usage-row {
  display: grid; grid-template-columns: 1fr auto 56px 56px 48px;
  align-items: center; gap: 6px; padding: 4px 2px;
  border-bottom: 1px solid var(--border-color, rgba(127, 127, 127, 0.12));
  font-size: 12px;
}
.usage-head { color: var(--text-tertiary, #8c8c8c); font-size: 11px; }
.usage-row .num { text-align: right; font-variant-numeric: tabular-nums; }
.u-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.usage-note { margin: 10px 0 0; color: var(--text-tertiary, #8c8c8c); font-size: 11px; }

/* 事件流：原始事件，等宽 + 紧凑行（--font-mono 已含 CJK 字体栈，中文不会掉宋体） */
.events { display: flex; flex-direction: column; gap: 2px; max-height: 320px; overflow: auto; }
.ev-row { display: grid; grid-template-columns: 132px 96px 1fr; gap: 6px; font-size: 11px; }
.ev-type { color: var(--primary, #1677ff); font-family: var(--font-mono); }
.ev-run { color: var(--text-tertiary, #8c8c8c); font-family: var(--font-mono); }
.ev-text {
  color: var(--text-secondary, #595959);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
/* 产物 tab：按目录分组 + 点击复制（空态复用 .sa-empty） */
.artifacts { display: flex; flex-direction: column; gap: 8px; }
.art-group { display: flex; flex-direction: column; gap: 2px; }
.art-dir { color: var(--text-tertiary, #8c8c8c); font-size: 11px; font-family: var(--font-mono); }
.art-item {
  padding: 3px 6px; border: none; border-radius: 4px; cursor: pointer;
  background: transparent; color: var(--text-secondary, #595959);
  font-size: 12px; font-family: var(--font-mono); text-align: left;
}
.art-item:hover { background: var(--surface-2, rgba(127, 127, 127, 0.08)); color: var(--text-primary, #262626); }
.art-item.copied { color: var(--success, #16a34a); }
</style>
