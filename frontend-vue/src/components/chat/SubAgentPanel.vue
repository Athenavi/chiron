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

const visibleRuns = computed(() => runs.value.filter(r => !hiddenByAncestor(r)))

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

    <Spin :spinning="loading">
      <Empty
        v-if="!runs.length"
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

    <div
      v-if="selectedRun"
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
</style>
