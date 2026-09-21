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
import { Empty, Spin, Tag, Tooltip } from 'ant-design-vue'
import {
  getSubagentRunEvents,
  listSubagentRuns,
  type SubagentEvent,
  type SubagentRunView,
} from '../../api/subagent'

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

const visibleRuns = computed(() => displayRuns.value.filter(r => !hiddenByAncestor(r)))

const selectedRun = computed(() => runs.value.find(r => r.run_id === selectedRunId.value) || null)

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
    if (!selectedRunId.value && res.runs.length) selectedRunId.value = res.runs[0].run_id
  } catch {
    // 面板失败不打断对话；下一轮轮询会再试
  } finally {
    loading.value = false
  }
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

function dismissCostHint() {
  showCostHint.value = false
  try {
    globalThis.localStorage?.setItem(COST_HINT_KEY, '1')
  } catch { /* 隐私模式下不可用，忽略 */ }
}

// 实时流里出现了新 run（通常是刚委派的）→ 立刻刷新树，让卡片尽快出现
/** 面板内部 tab（设计稿第四节：运行 · 输出 · 用量 · 事件流） */
const SA_TABS = [
  { id: 'runs', label: '运行' },
  { id: 'output', label: '输出' },
  { id: 'usage', label: '用量' },
  { id: 'events', label: '事件流' },
] as const
const saTab = ref<(typeof SA_TABS)[number]['id']>('runs')

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
  timer = globalThis.setInterval(() => { void loadRuns() }, 3000)
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
          <span class="run-usage">↑{{ run.usage?.input_tokens || 0 }}/↓{{ run.usage?.output_tokens || 0 }}</span>
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
</style>
