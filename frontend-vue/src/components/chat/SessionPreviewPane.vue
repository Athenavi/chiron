<script setup lang="ts">
/**
 * 参考栏（分屏）：把**另一个会话**以只读方式并排显示。
 *
 * ## 渲染一致性的修复（重要）
 *
 * 此前这里**自己手写**了一份映射：只取 `content`、只区分 user/assistant。
 * 后果是三重的：
 *
 * 1. **内部格式标签直接泄露到界面** —— 主区域的管线会跑 `stripUserInputTag`
 *    （用户输入标记）与 `splitThinking`（思维链），这里全都没跑，
 *    于是 `<thinking>` 之类的内部标记原样显示；
 * 2. **工具调用完全丢失** —— `tool_call` / `tool_result` 条目根本没构造；
 * 3. **与主区域观感不一致** —— 没有日期分隔线，也不是工具分组视图。
 *
 * 现在与主区域**共用同一条管线**：`mergeHistory`（`chat-history.ts`）产生 items，
 * 交给同一个 `MessageList` 渲染。因此标签剥离、工具分组、窗口化行为**完全一致**，
 * 且以后只有一处需要维护。
 *
 * ## 自主切换会话
 *
 * 传入 `sessions` 后，头部会出现下拉，可以直接换分屏显示哪个会话（无需先收起再开）。
 */
import { computed, ref, watch } from 'vue'
import { api } from '../../api'
import MessageList from './MessageList.vue'
import { mergeHistory } from './chat-history'
import type { ChatItem, ChatSession } from './chat-types'

const props = withDefaults(
  defineProps<{
    sessionId: string
    title?: string
    /** 可选会话列表；提供后头部出现下拉，可自主切换分屏显示哪个会话 */
    sessions?: ChatSession[]
    /** 主区域的会话：从下拉里排除，避免两栏显示同一个会话 */
    excludeSessionId?: string
    /**
     * 分栏手柄贴在哪条边 —— 由父级布局决定（`ChatView` 的 `is-swapped` 会把参考栏
     * 换到右侧，此时手柄必须贴左边缘才落在两栏之间）。
     */
    handleSide?: 'left' | 'right'
  }>(),
  { sessions: () => [], excludeSessionId: '', handleSide: 'right' },
)

const emit = defineEmits<{ (e: 'update:sessionId', id: string): void }>()

const items = ref<ChatItem[]>([])
const loading = ref(false)
const failed = ref(false)

/** 可切换的目标：排除主区域正在看的那个会话 */
const switchable = computed(() =>
  (props.sessions || []).filter(s => s.id && s.id !== props.excludeSessionId),
)

async function load() {
  if (!props.sessionId) {
    items.value = []
    return
  }
  loading.value = true
  failed.value = false
  try {
    const res = await api.get(`/v1/conversations/${props.sessionId}?limit=50`)
    const data = res.data?.data || res.data
    // ★ 与主区域同一条管线（含格式标签剥离、工具调用、日期分隔线）
    items.value = mergeHistory(data?.messages || [], data?.tool_calls || [])
  } catch {
    // 只读参考栏失败不该打扰主线：降级成一行提示
    failed.value = true
    items.value = []
  } finally {
    loading.value = false
  }
}

watch(() => props.sessionId, load, { immediate: true })

async function refresh() {
  await load()
}

function onPick(e: Event) {
  const id = (e.target as HTMLSelectElement).value
  if (id && id !== props.sessionId) emit('update:sessionId', id)
}

// ── 分栏宽度：可拖拽 + 本地记忆 ────────────────────────────────────────────
//
// 此前 `.preview-pane` 根本没设宽度，实际宽度由内容撑开 —— 两栏比例不可调。
// 现在给一条分隔手柄：拖动改宽度、写入 localStorage、下次进来自动恢复。
//
// 刻意**不设默认宽度**：只有用户真的拖过之后才应用固定 px 宽度；没拖过就保持
// 「内容决定宽度」的现状。这样对既有用户零观感变化，双击重置也有明确语义
// （回到内容决定宽度，并清掉这条本地记忆）。
const WIDTH_KEY = 'chiron:split-width:v1'
const MIN_WIDTH = 260
/** 上限 = 容器宽度的这个比例：保证主区域永远留得下可用宽度 */
const MAX_RATIO = 0.6

const paneRef = ref<HTMLElement | null>(null)
const paneWidth = ref<number | null>(readWidth())
const resizing = ref(false)

function readWidth(): number | null {
  try {
    const raw = localStorage.getItem(WIDTH_KEY)
    if (!raw) return null
    const n = Number.parseInt(raw, 10)
    return Number.isFinite(n) && n > 0 ? n : null
  } catch {
    return null // 隐私模式下 localStorage 不可用：退回「内容决定宽度」
  }
}

function clampWidth(px: number): number {
  const container = paneRef.value?.parentElement?.clientWidth || 0
  const max = container > 0 ? Math.max(MIN_WIDTH, Math.round(container * MAX_RATIO)) : 900
  return Math.min(Math.max(Math.round(px), MIN_WIDTH), max)
}

function persistWidth() {
  if (paneWidth.value === null) return
  try {
    localStorage.setItem(WIDTH_KEY, String(paneWidth.value))
  } catch {
    /* 存不下就只在本次会话内生效，不影响使用 */
  }
}

let startX = 0
let startWidth = 0

function onResizeStart(e: PointerEvent) {
  const el = paneRef.value
  if (!el) return
  resizing.value = true
  startX = e.clientX
  // 以**当前实际宽度**为起点：否则第一次拖动会突然跳到某个默认值
  startWidth = el.getBoundingClientRect().width
  // 指针捕获：拖出窗口也不丢事件，且不必往 document 上挂/摘监听
  ;(e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId)
  e.preventDefault()
}

function onResizeMove(e: PointerEvent) {
  if (!resizing.value) return
  // 手柄贴右边缘时向右拖 = 变宽；贴左边缘时向左拖 = 变宽
  const delta = props.handleSide === 'left' ? startX - e.clientX : e.clientX - startX
  paneWidth.value = clampWidth(startWidth + delta)
}

function onResizeEnd(e: PointerEvent) {
  if (!resizing.value) return
  resizing.value = false
  ;(e.currentTarget as HTMLElement).releasePointerCapture?.(e.pointerId)
  persistWidth()
}

/** 键盘可达（role="separator"）：←/→ 微调 */
function onResizeKey(e: KeyboardEvent) {
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
  e.preventDefault()
  const base = paneWidth.value ?? paneRef.value?.getBoundingClientRect().width ?? MIN_WIDTH
  const step = props.handleSide === 'left' ? -16 : 16
  const next = clampWidth(base + (e.key === 'ArrowLeft' ? -step : step))
  paneWidth.value = next
  persistWidth()
}

/** 双击手柄：回到「内容决定宽度」并清掉本地记忆 */
function resetWidth() {
  paneWidth.value = null
  resizing.value = false
  try {
    localStorage.removeItem(WIDTH_KEY)
  } catch {
    /* 忽略 */
  }
}

const paneStyle = computed(() =>
  paneWidth.value ? { width: `${paneWidth.value}px`, flex: '0 0 auto' } : {},
)

defineExpose({ refresh })
</script>

<template>
  <aside
    ref="paneRef"
    class="preview-pane"
    :class="{ resizing }"
    :style="paneStyle"
  >
    <!-- 分栏手柄：拖动调整参考栏宽度、双击重置（本地记忆见 WIDTH_KEY） -->
    <div
      class="pp-resize"
      :class="`handle-${handleSide}`"
      role="separator"
      aria-orientation="vertical"
      :aria-label="$t('auth.drag_to_resize_double_click_to_reset')"
      tabindex="0"
      @pointerdown="onResizeStart"
      @pointermove="onResizeMove"
      @pointerup="onResizeEnd"
      @pointercancel="onResizeEnd"
      @dblclick="resetWidth"
      @keydown="onResizeKey"
    />
    <header class="pp-head">
      <!-- 自主切换：列出其它会话；当前显示的会话固定为第一项，保证下拉始终有值 -->
      <select
        v-if="switchable.length"
        class="pp-select"
        :value="sessionId"
        :title="$t('chat.switch_the_split_view_session')"
        @change="onPick"
      >
        <option :value="sessionId">
          {{ title || sessionId.slice(0, 8) }}
        </option>
        <option
          v-for="s in switchable"
          :key="s.id"
          :value="s.id"
        >
          {{ s.title || $t('chat.new_conversation') }}
        </option>
      </select>
      <span
        v-else
        class="pp-title"
      >{{ title || $t('chat.reference_session') }}</span>
      <button
        type="button"
        class="pp-btn"
        :title="$t('common.refresh')"
        @click="refresh"
      >
        ⟳
      </button>
    </header>
    <div class="pp-body">
      <div
        v-if="loading && !items.length"
        class="pp-empty"
      >
        {{ $t('common.loading') }}
      </div>
      <div
        v-else-if="failed"
        class="pp-empty"
      >
        {{ $t('errors.failed_to_load_the_session_may_be_deleted_or_inaccessible') }}
      </div>
      <div
        v-else-if="!items.length"
        class="pp-empty"
      >
        {{ $t('chat.this_session_has_no_messages_yet') }}
      </div>
      <!-- 与主区域同一个渲染组件：标签剥离 / 工具分组 / 窗口化都一致 -->
      <MessageList
        v-else
        :items="items"
        :loading="loading"
        :session-key="sessionId"
      />
    </div>
  </aside>
</template>

<style scoped>
.preview-pane {
  position: relative; /* 分栏手柄的定位锚点 */
  display: flex; flex-direction: column; min-width: 0; height: 100%;
  border-left: 1px solid var(--border-subtle);
  /* 参考栏自己滚动，避免把主会话的滚动位置带跑 */
  overflow: hidden;
}
.pp-head {
  flex: none; display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 6px 12px; border-bottom: 1px solid var(--border-subtle);
}
.pp-title {
  font-size: var(--fs-sm); font-weight: var(--fw-semibold);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
/* 会话切换下拉：尽量轻，融进头部而不喧宾夺主 */
.pp-select {
  flex: 1; min-width: 0; max-width: 100%;
  font-size: var(--fs-sm); font-weight: var(--fw-semibold);
  color: var(--text-primary); background: transparent;
  border: none; outline: none; cursor: pointer;
  padding: 2px 0;
}
.pp-select:hover { color: var(--accent); }
.pp-btn {
  flex: none; border: none; background: transparent; color: var(--text-tertiary);
  cursor: pointer; font-size: 13px; padding: 0 4px;
}
.pp-btn:hover { color: var(--text-primary); }
/* MessageList 自带滚动与窗口化，这里只给高度与最小内边距 */
.pp-body { flex: 1; min-height: 0; display: flex; flex-direction: column; overflow: hidden; padding: 8px 4px; }
.pp-empty { padding: 16px 12px; color: var(--text-tertiary); font-size: 12px; }

/* 分栏手柄：平时不可见（避免多一条视觉噪声），悬停/拖动/聚焦时才显形。
   z-index 用 token —— scripts/check-z-index-tokens.mjs 会拦裸数字。 */
.pp-resize {
  position: absolute; top: 0; bottom: 0; width: 6px;
  z-index: var(--z-sticky);
  cursor: col-resize;
  background: transparent;
  transition: background-color var(--dur-fast) var(--ease-out);
  touch-action: none; /* 触屏上横拖交给手柄，不参与页面滚动 */
}
.pp-resize.handle-right { right: -3px; }
.pp-resize.handle-left { left: -3px; }
.pp-resize:hover,
.preview-pane.resizing .pp-resize { background: var(--primary-bg); }
.pp-resize:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
.preview-pane.resizing { user-select: none; }
</style>
