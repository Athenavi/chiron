<script setup lang="ts">
import { ref, watch, nextTick, computed, onErrorCaptured, onMounted, onUpdated } from 'vue'
import { ArrowDownOutlined } from '@ant-design/icons-vue'
import MessageItem from './MessageItem.vue'
import FoldHeader from './FoldHeader.vue'
import type { ChatItem, TextItem } from './chat-types'
import { createTranscriptViewport } from './transcriptViewport'
import { captureAnchor, resolveRestoreOffset, type RowRect } from './transcriptAnchor'
import { createLedger } from './transcriptMeasurementLedger'
import {
  applyMeasurements,
  buildGeometry,
  computeWindowRange,
  coversViewport,
  toSegments,
  type WindowRange,
} from './transcriptWindow'
import {
  DEFAULT_RESIDENT_TAIL_TURNS,
  itemKey,
  projectTranscript,
  type FoldKey,
  type ProjectedRow,
} from './transcriptProjection'
import { readFolds, writeFold } from './transcriptFolds'

import { useI18n } from 'vue-i18n'

const { t } = useI18n()

const props = defineProps<{
  items: ChatItem[]
  loading: boolean
  focusIndex?: number | null   // 跳转目标（用户消息索引）
  focusToken?: number          // 递增触发跳转
  hasMore?: boolean            // P 性能：是否还有更早的消息
  loadingEarlier?: boolean     // P 性能：正在加载更早消息
  /** P2-E: 首次加载会话历史时的骨架屏 */
  initialLoading?: boolean
  /** 行尺寸测量（默认 offsetHeight）。注入点让窗口化逻辑可在无布局环境下被测试。 */
  measureRow?: (el: HTMLElement) => number
  /** 视口高度（默认 el.clientHeight）。同样为可测性保留注入点。 */
  viewportHeight?: number
  /** 常驻尾部**按行**兜底值：投影给出的回合对齐起点优先，此值只在无投影时使用。 */
  residentTailRows?: number
  /** 常驻尾部的回合数（含活跃回合）：折叠改变行数但不改变回合数，故按回合常驻更稳。 */
  residentTailTurns?: number
  /** 会话身份：折叠状态按会话持久化（切会话重读，避免继承上一会话的展开态）。 */
  sessionKey?: string
  /** 行数超过该值才启用窗口化（小会话直接全量渲染，无风险）。 */
  windowingMinRows?: number
}>()

const emit = defineEmits<{
  (e: 'load-earlier'): void
  /** P1-1 用户消息编辑后重发 */
  (e: 'retry-from', itemId: string, text: string): void
  /** P1-1 助手消息重新生成 */
  (e: 'regenerate', itemId: string): void
  /** P2-F 停止后继续生成 */
  (e: 'continue', itemId: string): void
  /** P1-3 失败消息重试 */
  (e: 'retry-failed', itemId: string): void
  /** 选中文本：引用到输入框（由父组件转交给 ChatInput） */
  (e: 'quote-text', text: string): void
}>()

const scrollRef = ref<HTMLDivElement | null>(null)
const stickToBottom = ref(true)
const highlightIndex = ref<number | null>(null)
const showBackToBottom = ref(false)
const unseenCount = ref(0)
/** fail-closed 降级开关：置位后停止一切窗口化与滚动编排，只做全量静态渲染。 */
const safeMode = ref(false)

const SCROLL_THRESHOLD = 120
const TOP_LOAD_THRESHOLD = 60
const DEFAULT_OVERSCAN_PX = 800
/** 无投影时的按行兜底；正常情况下由投影的回合对齐起点接管 */
const DEFAULT_RESIDENT_TAIL_ROWS = 40
/** 常驻行数上限：单个巨型回合不得把常驻预算撑爆 */
const RESIDENT_MAX_ROWS = 120
const DEFAULT_WINDOWING_MIN_ROWS = 150
/** 连续覆盖失败达到该次数即永久降级为全量渲染。 */
const COVER_FAILURE_LIMIT = 2

/**
 * 滚动单写者：本组件不再直接写 scrollTop，一律经 vp 提交意图，
 * 使"程序跟随"与"用户滚动"可归因（writer provenance），
 * 用户在阅读时自动跟随让位而不是把视口拽回底部。
 */
const vp = createTranscriptViewport({ leaseMs: 1200, bottomThreshold: SCROLL_THRESHOLD })

// ── 窗口化状态 ───────────────────────────────────────────────
const ledger = createLedger()
const measuredSizes = ref<ReadonlyMap<string, number>>(new Map())
const windowRange = ref<WindowRange>({ start: 0, end: 0, tailStart: 0, tailEnd: 0 })
/** 首次渲染先全量，挂载后测量再切窗口：避免首屏用未经测量的几何画出空白。 */
const fullRender = ref(true)
let coverFailures = 0

/** 用户输入早于 scroll 事件到达：提前取得租约，避免流式增长抢走阅读位置。 */
function onUserInput() {
  vp.noteUserInput()
}

/**
 * 子组件渲染异常时降级（fail-closed）：本会话不再做窗口化与滚动编排，
 * 只保留全量静态渲染 —— 避免"渲染失败 + 窗口/编排写入"叠加成空白视口。
 */
onErrorCaptured((err) => {
  if (!safeMode.value) {
    safeMode.value = true
    console.error(t('[MessageList] 渲染异常，降级为全量静态渲染'), err)
  }
  return false   // 已处理，不再向上冒泡导致整页白屏
})

function isUserAnchor(item: ChatItem | undefined): boolean {
  return !!item && item.kind === 'text' && item.role === 'user'
}

// ── 投影层 ───────────────────────────────────────────────────
// 渲染行序列由 projection 产出（key 规则、回合与工具组结构都在那里定义）。
// grouped 模式注入折叠头并剔除被折叠的行；窗口几何只认「行序列 + key」，无需改动。
const folds = ref<Map<FoldKey, boolean>>(readFolds(props.sessionKey ?? ''))
watch(() => props.sessionKey, key => { folds.value = readFolds(key ?? '') })

const projection = computed(() => projectTranscript({
  items: props.items,
  mode: 'grouped',
  folds: folds.value,
  residentTailTurns: props.residentTailTurns ?? DEFAULT_RESIDENT_TAIL_TURNS,
}))
const rows = computed(() => projection.value.rows)

/** 常驻尾部起点由投影按回合给出：折叠改变行数但不改变回合数，按行计数会漂移。 */
const residentTailStart = computed(() => {
  const list = rows.value
  for (let i = 0; i < list.length; i++) if (list[i]?.resident) return i
  // 没有回合身份的行（实时流 / 旧数据）退化为按行常驻，语义与引入投影前一致
  return Math.max(0, list.length - (props.residentTailRows ?? DEFAULT_RESIDENT_TAIL_ROWS))
})

/** 折叠/展开：几何会变，必须先按锚点定位再提交（顺序反了会先闪一下再跳回）。 */
async function toggleFold(fold: FoldKey) {
  const el = scrollRef.value
  const open = projection.value.folds.get(fold) ?? false
  const next = new Map(folds.value)
  next.set(fold, !open)
  const anchor = el ? captureAnchor(collectRows(el)) : null
  folds.value = next
  writeFold(props.sessionKey ?? '', fold, !open)
  if (!el) return
  await nextTick()
  if (anchor) {
    const offset = resolveRestoreOffset(anchor, collectRows(el), el.scrollTop)
    // 折叠使总高变小时浏览器会改写 scrollTop，故走结构性写入（按真实落点登记 provenance）
    if (offset !== null) vp.afterStructuralChange(el, offset)
  }
  measureMountedRows()
  recomputeWindow()
}

// ── 窗口计算 ─────────────────────────────────────────────────
const measure = (el: HTMLElement): number =>
  props.measureRow ? props.measureRow(el) : el.offsetHeight

const viewportHeightOf = (el: HTMLElement): number =>
  props.viewportHeight ?? el.clientHeight

const rowSpecs = computed(() => rows.value.map(row => ({ key: row.key, kind: row.kind })))
const geometry = computed(() => buildGeometry(applyMeasurements(rowSpecs.value, measuredSizes.value)))

/** 小会话与降级态一律全量渲染。 */
const windowingEnabled = computed(() =>
  !safeMode.value &&
  rows.value.length > (props.windowingMinRows ?? DEFAULT_WINDOWING_MIN_ROWS))

/** 当前渲染的连续段落；全量渲染时是一段覆盖整表。 */
const renderSegments = computed(() =>
  fullRender.value
    ? [{ start: 0, end: rows.value.length }]
    : toSegments(windowRange.value))

/**
 * 渲染节点序列：行 + 段间占位。
 * 占位让未挂载部分仍占用真实高度，滚动条才不会跳；全量渲染时不含任何占位。
 */
const renderNodes = computed(() => {
  const geo = geometry.value
  const full = fullRender.value
  const nodes: Array<
    { kind: 'spacer'; height: number; key: string } |
    { kind: 'row'; row: ProjectedRow; key: string }
  > = []
  let cursor = 0
  renderSegments.value.forEach((seg, si) => {
    const gap = (geo.offsets[seg.start] ?? 0) - cursor
    if (!full && gap > 0) nodes.push({ kind: 'spacer', height: gap, key: `gap:${si}` })
    for (let i = seg.start; i < seg.end; i++) {
      const row = rows.value[i]
      if (!row) continue
      nodes.push({ kind: 'row', row, key: row.key })
    }
    cursor = geo.offsets[seg.end] ?? cursor
  })
  const tailGap = full ? 0 : Math.max(0, geo.total - cursor)
  if (tailGap > 0) nodes.push({ kind: 'spacer', height: tailGap, key: 'gap:tail' })
  return nodes
})

/**
 * 提交窗口区间。全量渲染（`fullRender`）只关心 [start, end)：此时尾部区间无意义，
 * 显式置 0 而不是让调用方补字段，避免"缺字段"的字面量绕过类型检查。
 */
function setRange(next: WindowRange | { start: number; end: number }) {
  const cur = windowRange.value
  if (cur.start === next.start && cur.end === next.end) return   // 避免无变化赋值触发重渲染循环
  const tailStart = 'tailStart' in next ? next.tailStart : 0
  const tailEnd = 'tailEnd' in next ? next.tailEnd : 0
  windowRange.value = { start: next.start, end: next.end, tailStart, tailEnd }
}

/**
 * 重算窗口。覆盖判定不通过时**退回全量渲染**（绝不提交未覆盖的窗口），
 * 连续失败达到阈值则永久降级 —— 宁可慢，也不留空白。
 */
function recomputeWindow() {
  const el = scrollRef.value
  if (!el || !windowingEnabled.value) {
    fullRender.value = true
    setRange({ start: 0, end: rows.value.length })
    return
  }
  const n = rows.value.length
  const geo = geometry.value
  const scrollTop = el.scrollTop
  const viewportHeight = viewportHeightOf(el)
  const range = computeWindowRange(geo, n, scrollTop, viewportHeight, {
    overscanPx: DEFAULT_OVERSCAN_PX,
    residentTail: props.residentTailRows ?? DEFAULT_RESIDENT_TAIL_ROWS,
    residentTailStart: residentTailStart.value,
    residentMaxRows: RESIDENT_MAX_ROWS,
  })
  if (!coversViewport(geo, range, scrollTop, viewportHeight)) {
    if (++coverFailures >= COVER_FAILURE_LIMIT) {
      safeMode.value = true
      console.warn(t('[MessageList] 窗口覆盖连续失败，已降级为全量渲染'))
    }
    fullRender.value = true
    setRange({ start: 0, end: n })
    return
  }
  coverFailures = 0
  fullRender.value = false
  setRange(range)
}

/** 测量已挂载的行并原子发布几何快照。 */
function measureMountedRows() {
  const el = scrollRef.value
  if (!el || !windowingEnabled.value || fullRender.value) return
  el.querySelectorAll<HTMLElement>('[data-item-key]').forEach((node) => {
    ledger.stage(node.dataset.itemKey as string, measure(node))
  })
  const snapshot = ledger.publish()
  if (snapshot) measuredSizes.value = snapshot
}

let windowRecomputePending = false
/** 滚动过程中按帧节流重算窗口。 */
function scheduleWindowRecompute() {
  if (windowRecomputePending) return
  windowRecomputePending = true
  const run = () => {
    windowRecomputePending = false
    recomputeWindow()
  }
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(run)
  else setTimeout(run, 16)
}

onMounted(() => {
  measureMountedRows()
  recomputeWindow()
  syncActiveQuestion()
})

onUpdated(() => {
  measureMountedRows()
  recomputeWindow()
  syncActiveQuestion()
})

// ── 提问导航条（≥2 个提问时出现；点击跳到该提问） ──
const questions = computed(() => {
  const out: { rowIndex: number; key: string; preview: string }[] = []
  rows.value.forEach((row, rowIndex) => {
    if (!isUserAnchor(row.item)) return
    const content = (row.item as TextItem).content || ''
    out.push({
      rowIndex,
      key: row.key,
      preview: content.replace(/\s+/g, ' ').trim().slice(0, 60) || t('（空消息）'),
    })
  })
  return out
})
const questionKeys = computed(() => new Set(questions.value.map(q => q.key)))
/** 当前视口内最靠上的提问（未进入视口时为 null） */
const activeQuestionKey = ref<string | null>(null)

function syncActiveQuestion() {
  const el = scrollRef.value
  if (!el || questions.value.length < 2) {
    activeQuestionKey.value = null
    return
  }
  const cTop = el.getBoundingClientRect().top
  let active: string | null = null
  el.querySelectorAll<HTMLElement>('[data-item-key]').forEach((node) => {
    if (active) return
    const key = node.dataset.itemKey
    if (!key || !questionKeys.value.has(key)) return
    if (node.getBoundingClientRect().bottom > cTop) active = key
  })
  activeQuestionKey.value = active
}

/**
 * 跳到某条提问。走单写者而不是 scrollIntoView：几何偏移是行序列坐标系，
 * 未挂载的行（窗口化把历史行换成了占位）同样能定位，且不绕过滚动归因。
 */
function jumpToRow(rowIndex: number) {
  const el = scrollRef.value
  if (!el) return
  const offset = geometry.value.offsets[rowIndex]
  if (offset === undefined) return
  stickToBottom.value = false
  vp.release()                     // 用户显式跳转：租约作废，写入立即生效
  vp.restore(el, Math.max(0, offset))
  recomputeWindow()
  syncActiveQuestion()
}

// ── 选中文本操作菜单 ─────────────────────────────────────────
// 菜单按内容坐标定位（含 scrollTop 补偿），滚动时关闭：跟着内容漂移的菜单没有意义。
const selectionText = ref('')
const selectionAt = ref<{ x: number; y: number; below: boolean } | null>(null)

function closeSelectionMenu() {
  if (!selectionAt.value) return
  selectionText.value = ''
  selectionAt.value = null
}

function onSelectionUp() {
  const el = scrollRef.value
  const sel = typeof window === 'undefined' ? null : window.getSelection()
  const text = sel ? sel.toString().trim() : ''
  if (!el || !sel || !text || sel.rangeCount === 0) {
    closeSelectionMenu()
    return
  }
  const range = sel.getRangeAt(0)
  // 只接管消息区内的选区：输入框、代码块内走浏览器原生行为
  if (!el.contains(range.commonAncestorContainer)) {
    closeSelectionMenu()
    return
  }
  const rect = range.getBoundingClientRect()
  const host = el.getBoundingClientRect()
  const above = rect.top - host.top > 44
  selectionText.value = text
  selectionAt.value = {
    x: rect.left + rect.width / 2 - host.left,
    y: above ? rect.top - host.top + el.scrollTop - 8 : rect.bottom - host.top + el.scrollTop + 8,
    below: !above,
  }
}

async function copySelection() {
  const text = selectionText.value
  closeSelectionMenu()
  try {
    await navigator.clipboard?.writeText(text)
  } catch { /* 剪贴板权限被拒：用户仍可用系统快捷键 */ }
}

function quoteSelection() {
  const text = selectionText.value
  closeSelectionMenu()
  if (text) emit('quote-text', text)
}

// ── 滚动与锚点 ───────────────────────────────────────────────
function onScroll() {
  const el = scrollRef.value
  if (!el) return
  vp.handleScroll(el)              // 归因：确认程序写入 / 判定用户滚动并夺取租约
  closeSelectionMenu()
  const atBottom = vp.isAtBottom(el)
  stickToBottom.value = atBottom
  if (atBottom) {
    showBackToBottom.value = false
    unseenCount.value = 0
  } else {
    showBackToBottom.value = true
  }
  if (props.hasMore && !props.loadingEarlier && el.scrollTop <= TOP_LOAD_THRESHOLD) {
    emit('load-earlier')
  }
  syncActiveQuestion()
  scheduleWindowRecompute()
}

/** 收集当前渲染行的几何（相对滚动容器视口顶部），供锚点计算使用。 */
function collectRows(el: HTMLElement): RowRect[] {
  const cTop = el.getBoundingClientRect().top
  const out: RowRect[] = []
  el.querySelectorAll<HTMLElement>('[data-item-key]').forEach((n) => {
    const r = n.getBoundingClientRect()
    out.push({ key: n.dataset.itemKey as string, top: r.top - cTop, bottom: r.bottom - cTop })
  })
  return out
}

/** 上一次渲染的首项 key：用于把「头部插入」与「尾部追加」区分开。 */
let lastHeadKey: string | null = null

// 列表长度变化：头部插入（历史分页）保持视口锚点；尾部追加按贴底意图跟随
watch(() => props.items.length, async (n, prev) => {
  const el = scrollRef.value
  const headKey = props.items.length ? itemKey(props.items[0], 0) : null
  const grew = typeof prev === 'number' && n > prev
  const prepended = grew && lastHeadKey !== null && headKey !== lastHeadKey
  lastHeadKey = headKey
  if (!el || safeMode.value) return

  if (prepended) {
    // prepend 前（DOM 仍为旧状态）捕获锚点，更新后按锚点行还原视口。
    // 锚点法只依赖锚点行自身位置，因此对"窗口化导致总高度变化"免疫。
    const anchor = captureAnchor(collectRows(el))
    await nextTick()
    if (anchor) {
      const offset = resolveRestoreOffset(anchor, collectRows(el), el.scrollTop)
      if (offset !== null) vp.restore(el, offset)
    }
    return
  }

  if (!stickToBottom.value) {
    if (grew) unseenCount.value += n - (prev as number)
    return
  }
  await nextTick()
  // 经单写者跟随：用户租约未过期时会被拒绝（不抢用户正在阅读的位置）
  vp.follow(el)
})

function scrollToBottom() {
  const el = scrollRef.value
  if (!el) return
  vp.release()                     // 用户显式要求回底：租约作废，跟随立即生效
  vp.follow(el)
  stickToBottom.value = true
  showBackToBottom.value = false
  unseenCount.value = 0
}

// 跳转（轨迹面板 / 提问导航条 / 会话内检索）：滚动到目标行 + 高亮闪烁
watch(() => props.focusToken, async () => {
  if (props.focusIndex == null) return
  await nextTick()
  // 匹配任意文本行（不只用户消息）：会话内检索要能跳到助手回复上。
  // 走单写者的几何偏移：窗口化把历史行换成占位后，目标行可能未挂载，DOM 查询会落空。
  const rowIndex = rows.value.findIndex(row => row.index === props.focusIndex && row.item?.kind === 'text')
  if (rowIndex >= 0) jumpToRow(rowIndex)
  highlightIndex.value = props.focusIndex
  setTimeout(() => { if (highlightIndex.value === props.focusIndex) highlightIndex.value = null }, 2000)
})

const badgeText = computed(() => (unseenCount.value > 99 ? '99+' : String(unseenCount.value)))
</script>

<template>
  <div
    ref="scrollRef"
    class="message-list"
    @scroll.passive="onScroll"
    @wheel.passive="onUserInput"
    @touchstart.passive="onUserInput"
    @mouseup="onSelectionUp"
  >
    <!-- P2-E: 首次加载骨架屏 -->
    <div
      v-if="initialLoading"
      class="skeleton-list"
    >
      <div
        v-for="n in 4"
        :key="n"
        class="skeleton-msg"
        :class="n % 2 === 0 ? 'user' : 'assistant'"
      >
        <div class="skeleton-avatar" />
        <div class="skeleton-lines">
          <div
            class="skeleton-line"
            :style="{ width: 60 + (n * 7) % 30 + '%' }"
          />
          <div
            class="skeleton-line"
            :style="{ width: 80 + (n * 11) % 15 + '%' }"
          />
        </div>
      </div>
    </div>
    <div
      v-else-if="items.length === 0"
      class="list-empty-placeholder"
    />

    <!-- P 性能：触顶加载更早（infinite scroll） -->
    <div
      v-if="props.hasMore || props.loadingEarlier"
      class="earlier-loader"
    >
      <template v-if="props.loadingEarlier">
        <span class="loading-dot chat-pulse" /><span class="loading-dot chat-pulse" /><span class="loading-dot chat-pulse" />
      </template>
      <span v-else>{{ $t('加载更早的消息') }}</span>
    </div>

    <!-- 消息列表：窗口化渲染（只挂载窗口内的行，其余用 spacer 占位）。
         所有 item 类型统一交给 MessageItem 按 kind 分发（text / reasoning /
         tool_call / tool_result / turn_stats / date_divider），此处只额外处理
         kb_hits（统一任务模式的专属标签，不属于 ChatItem 联合类型） -->
    <div class="message-container">
      <template
        v-for="node in renderNodes"
        :key="node.key"
      >
        <!-- 段间与首尾占位：未挂载部分仍占真实高度，滚动条不跳 -->
        <div
          v-if="node.kind === 'spacer'"
          class="window-spacer"
          :style="{ height: (node as any).height + 'px' }"
        />
        <!-- data-item-key：锚点定位用的稳定身份（不能被内容补丁改写） -->
        <div
          v-else
          class="chat-row"
          :data-item-key="node.key"
        >
          <!-- 折叠头行（回合 / 工具组）：身份来自投影的 header.fold，点击切换折叠态 -->
          <FoldHeader
            v-if="(node as any).row.header"
            :header="(node as any).row.header"
            @toggle="toggleFold((node as any).row.header.fold)"
          />
          <MessageItem
            v-else-if="(node as any).row.item.kind !== 'kb_hits'"
            :item="(node as any).row.item"
            :anchor-key="isUserAnchor((node as any).row.item) ? (node as any).row.index : undefined"
            :highlighted="highlightIndex === (node as any).row.index"
            @retry-from="(id: string, text: string) => emit('retry-from', id, text)"
            @regenerate="(id: string) => emit('regenerate', id)"
            @continue="(id: string) => emit('continue', id)"
            @retry-failed="(id: string) => emit('retry-failed', id)"
            @quote="(text: string) => emit('quote-text', text)"
          />
          <div
            v-else
            class="kb-hits-tag"
          >
            <span class="kb-hits-text">{{ $t('引用了知识库（×{n}）', { n: (node as any).row.item.count || 1 }) }}</span>
            <a
              v-if="(node as any).row.item.kb_id"
              class="kb-hits-link"
              href="#"
              :title="$t('查看引用的知识库')"
              @click.prevent
            >{{ $t('查看知识库') }}</a>
          </div>
        </div>
      </template>
    </div>

    <div
      v-if="loading"
      class="loading-indicator"
    >
      <span class="loading-dot chat-pulse" /><span class="loading-dot chat-pulse" /><span class="loading-dot chat-pulse" />
    </div>

    <!-- 回到底部按钮：离开底部时显示 + 新消息未读徽标（deepseek bottom-follow 语义补充） -->
    <Transition name="back-fade">
      <button
        v-if="showBackToBottom"
        class="back-to-bottom"
        type="button"
        :title="unseenCount ? $t('回到底部（{n} 条新消息）', { n: unseenCount }) : $t('回到底部')"
        @click="scrollToBottom"
      >
        <ArrowDownOutlined />
        <span
          v-if="unseenCount"
          class="back-badge"
        >{{ badgeText }}</span>
      </button>
    </Transition>

    <!-- 提问导航条：sticky + 零高度宿主，既不占内容流空间，又固定在容器中部 -->
    <div
      v-if="questions.length >= 2"
      class="question-rail-host"
    >
      <nav
        class="question-rail"
        :aria-label="$t('提问导航')"
      >
        <button
          v-for="q in questions"
          :key="q.key"
          class="rail-dot"
          :class="{ active: q.key === activeQuestionKey }"
          type="button"
          :title="q.preview"
          :aria-label="$t('跳转到提问：{preview}', { preview: q.preview })"
          @click="jumpToRow(q.rowIndex)"
        />
      </nav>
    </div>

    <!-- 选中文本操作菜单：按内容坐标定位（.message-list 的 layout containment 是定位上下文） -->
    <div
      v-if="selectionAt"
      class="selection-menu"
      :class="{ below: selectionAt.below }"
      :style="{ left: selectionAt.x + 'px', top: selectionAt.y + 'px' }"
      role="menu"
    >
      <button
        class="sel-btn"
        type="button"
        role="menuitem"
        @click="copySelection"
      >
        {{ $t('复制') }}
      </button>
      <button
        class="sel-btn"
        type="button"
        role="menuitem"
        @click="quoteSelection"
      >
        {{ $t('引用到输入框') }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.message-list { flex: 1; overflow-y: auto; position: relative; contain: layout style; }
.list-empty-placeholder { height: 24px; }

/* P2-E: 首屏骨架屏 */
.skeleton-list { padding: 16px 24px; }
.skeleton-msg { display: flex; gap: 12px; margin-bottom: 24px; }
.skeleton-msg.user { flex-direction: row-reverse; }
.skeleton-avatar { width: 28px; height: 28px; border-radius: 50%; background: var(--bg-hover); flex-shrink: 0; animation: skeleton-pulse var(--dur-pulse) ease-in-out infinite; }
.skeleton-lines { flex: 1; display: flex; flex-direction: column; gap: 8px; max-width: 70%; }
.skeleton-msg.user .skeleton-lines { align-items: flex-end; }
.skeleton-line { height: 14px; border-radius: 4px; background: var(--bg-hover); animation: skeleton-pulse var(--dur-pulse) ease-in-out infinite; }
.skeleton-line:nth-child(2) { animation-delay: 0.2s; }
@keyframes skeleton-pulse { 0%, 100% { opacity: 0.5; } 50% { opacity: 1; } }
@media (prefers-reduced-motion: reduce) { .skeleton-avatar, .skeleton-line { animation: none; } }
/* ── 移动端：骨架屏间距/头像压缩 ── */
@media (max-width: 768px) {
  .skeleton-list { padding: 12px 16px; }
  .skeleton-msg { gap: 10px; margin-bottom: 18px; }
  .skeleton-avatar { width: 24px; height: 24px; }
  .skeleton-lines { max-width: 85%; }
}
@media (max-width: 576px) { .skeleton-list { padding: 10px 12px; } }
.earlier-loader { display: flex; align-items: center; justify-content: center; gap: 6px; height: 36px; font-size: 12px; color: var(--text-tertiary); }
/* 消息容器：正常文档流，不遮挡 */
.message-container { width: 100%; }
/* 行容器：仅承载锚点身份，不引入布局变化（无 margin/padding） */
.chat-row { width: 100%; }
/* 窗口占位：只提供高度，不参与锚点身份收集 */
.window-spacer { width: 100%; flex: none; }
.loading-indicator { display: flex; justify-content: center; gap: 6px; padding: 14px 0; }
/* 尺寸/底色保留，脉冲与错峰延迟由全局 .chat-pulse 提供（src/style.css） */
.loading-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--text-tertiary); }
.back-to-bottom { position: sticky; bottom: 16px; left: calc(50% - 22px); width: 44px; height: 44px; border-radius: 50%; border: 1px solid var(--border); background: var(--bg-card); color: var(--text-secondary); cursor: pointer; box-shadow: var(--sig-shadow-card); display: flex; align-items: center; justify-content: center; z-index: var(--z-sticky); }
.back-to-bottom:hover { color: var(--primary); border-color: var(--primary); }
.back-badge { position: absolute; top: -4px; right: -4px; min-width: 18px; height: 18px; padding: 0 4px; border-radius: 9px; background: var(--primary); color: var(--on-solid); font-size: 11px; line-height: 18px; text-align: center; }
.back-fade-enter-active, .back-fade-leave-active { transition: opacity var(--dur-normal), transform var(--dur-normal); }
.back-fade-enter-from, .back-fade-leave-to { opacity: 0; transform: translateY(8px); }

/* 提问导航条：零高度宿主 + sticky，浮在滚动容器中部而不占内容流空间 */
.question-rail-host { position: sticky; bottom: 50%; height: 0; display: flex; justify-content: flex-end; pointer-events: none; z-index: var(--z-sticky); }
.question-rail {
  pointer-events: auto;
  display: flex; flex-direction: column; align-items: center; gap: 7px;
  max-height: 46vh; overflow-y: auto;
  margin-right: 8px; padding: 8px 7px;
  border: 1px solid var(--border-card); border-radius: var(--radius-full);
  background: color-mix(in srgb, var(--bg-card) 88%, transparent);
  box-shadow: var(--sig-shadow-card);
  scrollbar-width: none;
}
.question-rail::-webkit-scrollbar { display: none; }
.rail-dot {
  flex: none; width: 8px; height: 8px; padding: 0;
  border: none; border-radius: 50%;
  background: var(--text-disabled);
  cursor: pointer;
  transition: background var(--dur-fast) var(--ease-out), transform var(--dur-fast) var(--ease-out);
}
.rail-dot:hover { background: var(--text-secondary); transform: scale(1.35); }
.rail-dot.active { background: var(--primary); transform: scale(1.35); }
.rail-dot:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
@media (max-width: 768px) { .question-rail { display: none; } }   /* 窄屏优先保证内容宽度 */

/* 选中文本操作菜单：贴在选区上方（放不下则翻到下方） */
.selection-menu {
  position: absolute; z-index: var(--z-dropdown);
  display: flex; gap: 2px; padding: 3px;
  border: 1px solid var(--border-card); border-radius: var(--sig-radius-button);
  background: var(--bg-card); box-shadow: var(--sig-shadow-hover);
  transform: translate(-50%, -100%);
}
.selection-menu.below { transform: translate(-50%, 0); }
.sel-btn {
  border: none; background: none; color: var(--text-secondary);
  font-size: 12px; line-height: 18px; padding: 3px 8px;
  border-radius: var(--radius-sm); cursor: pointer; white-space: nowrap;
}
.sel-btn:hover { background: var(--bg-hover); color: var(--text-primary); }
.sel-btn:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
</style>
