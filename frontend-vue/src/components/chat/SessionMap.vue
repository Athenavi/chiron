<template>
  <div
    ref="stageRef"
    class="session-map"
    tabindex="0"
    @pointerdown="onStagePointerDown"
    @wheel.prevent="onWheel"
    @dblclick="onStageDblClick"
    @keydown="onKeydown"
  >
    <!-- 世界层：整体 translate + scale；节点按世界坐标绝对定位 -->
    <div class="sm-world" :style="worldStyle">
      <div
        v-for="n in visibleNodes"
        :key="n.id"
        class="sm-card"
        :class="{
          'is-active': n.sessionId && n.sessionId === activeSessionId,
          'is-running': n.sessionId ? runningSet.has(n.sessionId) : false,
          'is-note': !n.sessionId,
          'is-dragging': dragId === n.id,
          'is-selected': selected.has(n.id),
          'is-grouped': !!n.groupId,
        }"
        :style="cardStyle(n)"
        :title="titleOf(n)"
        @pointerdown.stop="onCardPointerDown($event, n.id)"
        @click="onCardClick($event, n)"
      >
        <div class="sm-card-title">{{ titleOf(n) }}</div>
        <div class="sm-card-meta">
          <span v-if="!n.sessionId" class="sm-tag">便签</span>
          <span v-else-if="sessionOf(n.sessionId)?.tag" class="sm-tag">{{ sessionOf(n.sessionId)!.tag }}</span>
          <span v-if="n.sessionId && runningSet.has(n.sessionId)" class="sm-running">运行中</span>
        </div>
        <!-- 节点操作：只作用于**地图层**，绝不触碰会话本身 -->
        <div class="sm-card-ops">
          <button type="button" class="sm-op" title="重命名（只改地图上的名字）" @click.stop="onRename(n)">✎</button>
          <button type="button" class="sm-op" title="隐藏（不删除）" @click.stop="map.setHidden(n.id, true)">◌</button>
          <button type="button" class="sm-op is-danger" title="仅从地图移除（不会删除会话）" @click.stop="onRemove(n)">×</button>
        </div>
      </div>
    </div>

    <!-- 浮动工具条：导入 / 便签 / 成组 / 视图 -->
    <div class="sm-hud">
      <button type="button" class="sm-btn sm-btn-wide" title="从会话列表导入" @click="openImport">
        导入会话
      </button>
      <button
        type="button"
        class="sm-btn sm-btn-wide"
        :disabled="selected.size < 2"
        :title="selected.size < 2 ? '先选中两个以上节点（点击卡片选中）' : '把选中的节点拼成一组'"
        @click="groupSelected"
      >
        成组
      </button>
      <button
        type="button"
        class="sm-btn"
        :disabled="!canUndo"
        title="撤销（Ctrl+Z）"
        @click="undo"
      >
        ↶
      </button>
      <button
        type="button"
        class="sm-btn"
        :disabled="!canRedo"
        title="重做（Ctrl+Shift+Z）"
        @click="redo"
      >
        ↷
      </button>
      <span class="sm-scale">{{ Math.round(camera.scale * 100) }}%</span>
      <button type="button" class="sm-btn" title="缩小" @click="zoomBy(1 / 1.2)">−</button>
      <button type="button" class="sm-btn" title="放大" @click="zoomBy(1.2)">+</button>
      <button type="button" class="sm-btn" title="回到原点" @click="resetView">⌂</button>
    </div>

    <div class="sm-hint">
      双击空白新建便签 · 拖动卡片排布 · 点击选中（2 个以上可成组）· 空白处平移 · 滚轮缩放
    </div>

    <!-- 导入面板：从**会话列表**挑选要放进地图的会话 -->
    <div v-if="importOpen" class="sm-import" @pointerdown.stop>
      <div class="sm-import-head">
        <span>从会话列表导入（已在地图中的会自动跳过）</span>
        <button type="button" class="sm-op" @click="importOpen = false">×</button>
      </div>
      <div class="sm-import-body">
        <label v-for="s in sessions" :key="s.id" class="sm-import-item">
          <input type="checkbox" :value="s.id" :checked="picked.has(s.id)" @change="togglePick(s.id)" />
          <span class="sm-import-name">{{ s.title || '新对话' }}</span>
          <span v-if="importedSessionIds.has(s.id)" class="sm-import-done">已在图中</span>
        </label>
        <div v-if="!sessions.length" class="sm-import-empty">会话列表为空</div>
      </div>
      <div class="sm-import-foot">
        <button type="button" class="sm-btn sm-btn-wide" @click="selectAllVisible">全选未导入</button>
        <button type="button" class="sm-btn sm-btn-wide is-primary" @click="doImport">
          导入所选（{{ picked.size }}）
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 会话地图（P0.5）：**独立的记忆层**画布。
 *
 * 关键语义（见 docs/session-map-plan.md §九）：地图节点是**独立实体**，
 * 与会话列表是"引用"关系而非同一份数据。因此本组件里：
 * - 渲染的是 `nodes`（地图节点），不是会话列表；
 * - 节点操作（重命名 / 隐藏 / 移除）**只作用于地图层**，UI 文案已显式说明；
 * - 导入只为新节点找空位，**绝不移动既有节点**（铁律）。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { ChatSession } from './chat-types'
import { GRID, clampScale, useSessionMap, type MapNode } from './useSessionMap'

const props = withDefaults(
  defineProps<{
    /** 会话列表（只用于显示标题/标签/状态，以及导入时挑选） */
    sessions: ChatSession[]
    activeSessionId?: string
    runningIds?: string[]
  }>(),
  { activeSessionId: '', runningIds: () => [] },
)

const emit = defineEmits<{ (e: 'select', id: string): void }>()

const map = useSessionMap()
const { nodes, camera, canUndo, canRedo, undo, redo } = map

/**
 * 键盘操作：撤销 / 重做。
 * 画布根元素可聚焦（tabindex="0"），所以只在焦点落在画布时才响应 ——
 * 不会抢走输入框里其它地方的 Ctrl+Z。
 */
function onKeydown(e: KeyboardEvent) {
  const mod = e.ctrlKey || e.metaKey
  if (mod && e.key.toLowerCase() === 'z') {
    e.preventDefault()
    if (e.shiftKey) redo()
    else undo()
  }
}

const runningSet = computed(() => new Set(props.runningIds || []))
const importedSessionIds = computed(
  () => new Set(nodes.value.map(n => n.sessionId).filter((v): v is string => !!v)),
)

/* ── 节点与会话的映射（两层之间的唯一桥梁） ── */

function sessionOf(sessionId?: string): ChatSession | undefined {
  return sessionId ? props.sessions.find(s => s.id === sessionId) : undefined
}

/** 节点显示名：**节点自己的名字优先**（独立层的体现） */
function titleOf(n: MapNode): string {
  if (n.title) return n.title
  const s = sessionOf(n.sessionId)
  if (s) return s.title || '新对话'
  return n.sessionId ? '未载入的会话' : '便签'
}

/* ── 视口 ── */

const stageRef = ref<HTMLElement | null>(null)
const viewport = ref({ w: 1200, h: 800 })
let ro: ResizeObserver | null = null

onMounted(() => {
  const el = stageRef.value
  if (!el) return
  const measure = () => {
    viewport.value = { w: el.clientWidth, h: el.clientHeight }
  }
  measure()
  // 让画布拿到焦点，键盘操作（撤销/重做）开箱可用；preventScroll 避免页面被拽动
  el.focus?.({ preventScroll: true })
  if (typeof ResizeObserver !== 'undefined') {
    ro = new ResizeObserver(measure)
    ro.observe(el)
  }
})

onBeforeUnmount(() => ro?.disconnect())

const viewRect = computed(() => {
  const { scale, tx, ty } = camera.value
  const pad = GRID * 1.5
  return {
    left: (-tx - pad) / scale,
    top: (-ty - pad) / scale,
    right: (-tx + viewport.value.w + pad) / scale,
    bottom: (-ty + viewport.value.h + pad) / scale,
  }
})

/** 可见节点：排除隐藏的，再做视口裁剪（与 MessageList 的窗口化同法） */
const visibleNodes = computed(() => {
  const r = viewRect.value
  return nodes.value.filter((n) => {
    if (n.hidden) return false
    return n.x >= r.left && n.x <= r.right && n.y >= r.top && n.y <= r.bottom
  })
})

/* ── 变换 ── */

const worldStyle = computed(() => ({
  transform: `translate(${camera.value.tx}px, ${camera.value.ty}px) scale(${camera.value.scale})`,
}))

const dragId = ref('')
const dragPos = ref<{ x: number; y: number } | null>(null)

function cardStyle(n: MapNode): Record<string, string> {
  const pos = dragId.value === n.id && dragPos.value ? dragPos.value : n
  return { left: `${pos.x}px`, top: `${pos.y}px`, transform: 'translate(-50%, -50%)' }
}

/** 屏幕坐标 → 世界坐标（新建便签用） */
function toWorld(clientX: number, clientY: number): { x: number; y: number } {
  const el = stageRef.value
  if (!el) return { x: 0, y: 0 }
  const rect = el.getBoundingClientRect()
  const { scale, tx, ty } = camera.value
  return {
    x: (clientX - rect.left - tx) / scale,
    y: (clientY - rect.top - ty) / scale,
  }
}

/* ── 拖动节点 ── */

function onCardPointerDown(e: PointerEvent, id: string) {
  if (e.button !== 0 && e.pointerType === 'mouse') return
  const node = nodes.value.find(n => n.id === id)
  if (!node) return

  const startX = e.clientX
  const startY = e.clientY
  const originX = node.x
  const originY = node.y
  const scale = camera.value.scale
  const el = e.currentTarget as HTMLElement
  let moved = false

  dragId.value = id
  dragPos.value = { x: originX, y: originY }
  try {
    el.setPointerCapture(e.pointerId)
  } catch {
    /* 某些环境不支持，忽略 */
  }

  const onMove = (ev: PointerEvent) => {
    // 屏幕位移换算回世界位移（除以 scale，否则缩放后拖动会"发飘"）
    const dx = (ev.clientX - startX) / scale
    const dy = (ev.clientY - startY) / scale
    if (Math.abs(ev.clientX - startX) > 3 || Math.abs(ev.clientY - startY) > 3) moved = true
    dragPos.value = { x: originX + dx, y: originY + dy }
  }

  const onUp = () => {
    el.removeEventListener('pointermove', onMove)
    el.removeEventListener('pointerup', onUp)
    el.removeEventListener('pointercancel', onUp)
    const pos = dragPos.value
    dragId.value = ''
    dragPos.value = null
    if (moved) movedSinceDown.value = true
    // 松手才落盘 —— 唯一的坐标写入路径（用户拖动 = 最高权威）
    if (pos && moved) map.moveNode(id, pos.x, pos.y)
  }

  el.addEventListener('pointermove', onMove)
  el.addEventListener('pointerup', onUp)
  el.addEventListener('pointercancel', onUp)
}

const movedSinceDown = ref(false)

/* ── 选中（点击卡片；Ctrl/Cmd 多选） ── */

const selected = ref<Set<string>>(new Set())

function onCardClick(e: MouseEvent, n: MapNode) {
  if (movedSinceDown.value) {
    movedSinceDown.value = false
    return
  }
  e.stopPropagation()
  if (e.ctrlKey || e.metaKey) {
    const next = new Set(selected.value)
    if (next.has(n.id)) next.delete(n.id)
    else next.add(n.id)
    selected.value = next
    return
  }
  // 单击：打开它引用的会话（便签没有会话可开）
  selected.value = new Set([n.id])
  if (n.sessionId) emit('select', n.sessionId)
}

watch(dragPos, (pos) => {
  if (pos) movedSinceDown.value = true
})

/* ── 节点操作（只作用于地图层） ── */

function onRemove(n: MapNode) {
  const name = titleOf(n)
  const ok = window.confirm(`仅从地图移除「${name}」？\n\n会话本身不会被删除。`)
  if (ok) map.removeNode(n.id)
}

function onRename(n: MapNode) {
  const next = window.prompt('地图上的名字（不影响会话标题）', titleOf(n))
  if (next !== null) map.renameNode(n.id, next.trim())
}

function groupSelected() {
  if (selected.value.size < 2) return
  const name = window.prompt('给这组起个名字', '') ?? ''
  map.groupNodes([...selected.value], name.trim())
  selected.value = new Set()
}

/* ── 导入 ── */

const importOpen = ref(false)
const picked = ref<Set<string>>(new Set())

function openImport() {
  picked.value = new Set()
  importOpen.value = true
}

function togglePick(id: string) {
  const next = new Set(picked.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  picked.value = next
}

function selectAllVisible() {
  picked.value = new Set(props.sessions.filter(s => !importedSessionIds.value.has(s.id)).map(s => s.id))
}

function doImport() {
  const entries = [...picked.value].map(id => ({
    sessionId: id,
    title: sessionOf(id)?.title,
  }))
  map.importSessions(entries)
  importOpen.value = false
  picked.value = new Set()
}

/* ── 平移 / 缩放 / 便签 ── */

function onStagePointerDown(e: PointerEvent) {
  if (e.button !== 0 && e.pointerType === 'mouse') return
  const el = stageRef.value
  if (!el) return
  // 点空白：清空选中
  selected.value = new Set()
  const startX = e.clientX
  const startY = e.clientY
  const { tx, ty } = camera.value

  const onMove = (ev: PointerEvent) => {
    map.setCamera({ tx: tx + (ev.clientX - startX), ty: ty + (ev.clientY - startY) })
  }
  const onUp = () => {
    el.removeEventListener('pointermove', onMove)
    el.removeEventListener('pointerup', onUp)
    window.removeEventListener('pointerup', onUp)
  }
  el.addEventListener('pointermove', onMove)
  el.addEventListener('pointerup', onUp)
  window.addEventListener('pointerup', onUp)
}

/** 双击空白 → 新建便签（独立记忆层最直接的体现） */
function onStageDblClick(e: MouseEvent) {
  if (e.target !== stageRef.value) return
  const { x, y } = toWorld(e.clientX, e.clientY)
  const title = window.prompt('便签内容', '')
  if (title === null) return
  map.addNote(x, y, title.trim())
}

/** 以指针为中心缩放（否则放大后目标会滑出视野） */
function onWheel(e: WheelEvent) {
  const el = stageRef.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  const next = clampScale(camera.value.scale * (e.deltaY < 0 ? 1.12 : 1 / 1.12))
  zoomAt(next, e.clientX - rect.left, e.clientY - rect.top)
}

function zoomAt(nextScale: number, px: number, py: number) {
  const { scale, tx, ty } = camera.value
  const k = nextScale / scale
  // 保持指针下的世界点不动：t' = p - (p - t) * k
  map.setCamera({ scale: nextScale, tx: px - (px - tx) * k, ty: py - (py - ty) * k })
}

function zoomBy(factor: number) {
  const el = stageRef.value
  zoomAt(clampScale(camera.value.scale * factor), el ? el.clientWidth / 2 : 0, el ? el.clientHeight / 2 : 0)
}

function resetView() {
  map.setCamera({ scale: 1, tx: 0, ty: 0 })
}
</script>

<style scoped>
.session-map {
  position: relative;
  flex: 1;
  min-height: 0;
  overflow: hidden;
  border-radius: var(--radius-md);
  background: var(--bg-secondary);
  /* 网格底纹：让"位置"可被感知（也是吸附网格的视觉依据） */
  background-image:
    linear-gradient(to right, var(--border-subtle) 1px, transparent 1px),
    linear-gradient(to bottom, var(--border-subtle) 1px, transparent 1px);
  background-size: 260px 260px;
  background-position: center;
  cursor: grab;
  touch-action: none;
}

.sm-world {
  position: absolute;
  inset: 0;
  transform-origin: 0 0;
  will-change: transform;
}

.sm-card {
  position: absolute;
  width: 228px;
  min-height: 96px;
  padding: var(--space-3);
  box-sizing: border-box;
  border: 1px solid var(--border-default, var(--border-subtle));
  border-radius: var(--radius-md);
  background: var(--bg-primary);
  cursor: grab;
  user-select: none;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  transition: border-color 0.15s, box-shadow 0.15s;
}

.sm-card:hover {
  border-color: var(--accent);
}

.sm-card.is-active,
.sm-card.is-selected {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-subtle, transparent);
}

/* 便签：视觉上与"会话节点"区分开 —— 它没有对应的会话 */
.sm-card.is-note {
  background: var(--bg-tertiary, var(--bg-secondary));
  border-style: dashed;
}

.sm-card.is-grouped {
  border-width: 2px;
}

.sm-card.is-dragging {
  cursor: grabbing;
  opacity: 0.9;
  box-shadow: var(--shadow-lg, 0 8px 24px rgb(0 0 0 / 18%));
  z-index: 2;
}

/* 运行中的会话脉冲发光 —— 状态一目了然 */
.sm-card.is-running {
  border-color: var(--success);
  animation: sm-pulse 1.8s ease-in-out infinite;
}

@keyframes sm-pulse {
  0%, 100% { box-shadow: 0 0 0 0 var(--success-subtle, transparent); }
  50% { box-shadow: 0 0 0 6px transparent; }
}

@media (prefers-reduced-motion: reduce) {
  .sm-card.is-running { animation: none; }
  .sm-card { transition: none; }
}

.sm-card-title {
  font-size: var(--fs-sm);
  color: var(--text-primary);
  line-height: 1.4;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-word;
}

.sm-card-meta {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.sm-tag,
.sm-running {
  font-size: var(--fs-xs);
  padding: 1px 6px;
  border-radius: var(--radius-full);
}

.sm-tag {
  color: var(--text-tertiary);
  background: var(--bg-tertiary, var(--bg-secondary));
}

.sm-running {
  color: var(--success);
  background: var(--success-subtle, transparent);
}

/* 节点操作：悬停才出现，避免画布噪声 */
.sm-card-ops {
  position: absolute;
  top: var(--space-1);
  right: var(--space-1);
  display: none;
  gap: 2px;
}

.sm-card:hover .sm-card-ops {
  display: flex;
}

.sm-op {
  width: 20px;
  height: 20px;
  border: none;
  border-radius: var(--radius-sm, 4px);
  background: var(--bg-secondary);
  color: var(--text-tertiary);
  cursor: pointer;
  font-size: var(--fs-xs);
  line-height: 1;
}

.sm-op:hover {
  color: var(--text-primary);
  background: var(--bg-tertiary, var(--bg-secondary));
}

.sm-op.is-danger:hover {
  color: #fff;
  background: var(--danger, #d4380d);
}

.sm-hud {
  position: absolute;
  right: var(--space-3);
  bottom: var(--space-3);
  display: flex;
  align-items: center;
  gap: var(--space-1);
  padding: var(--space-1);
  border-radius: var(--radius-full);
  background: var(--bg-primary);
  border: 1px solid var(--border-default, var(--border-subtle));
}

.sm-btn {
  height: 26px;
  min-width: 26px;
  padding: 0 6px;
  border: none;
  border-radius: var(--radius-full);
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: var(--fs-md);
  line-height: 1;
}

.sm-btn-wide {
  width: auto;
  padding: 0 10px;
  font-size: var(--fs-xs);
}

.sm-btn:hover:not(:disabled) {
  background: var(--bg-tertiary, var(--bg-secondary));
  color: var(--text-primary);
}

.sm-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.sm-btn.is-primary {
  color: var(--accent);
}

.sm-scale {
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
  min-width: 42px;
  text-align: center;
}

.sm-hint {
  position: absolute;
  left: 50%;
  bottom: var(--space-3);
  transform: translateX(-50%);
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
  pointer-events: none;
  text-align: center;
  max-width: 60%;
}

/* 导入面板 */
.sm-import {
  position: absolute;
  top: var(--space-3);
  left: var(--space-3);
  width: 300px;
  max-height: 60%;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border-default, var(--border-subtle));
  border-radius: var(--radius-md);
  background: var(--bg-primary);
  box-shadow: var(--shadow-lg, 0 8px 24px rgb(0 0 0 / 18%));
  overflow: hidden;
}

.sm-import-head,
.sm-import-foot {
  flex: none;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
}

.sm-import-head {
  border-bottom: 1px solid var(--border-subtle);
}

.sm-import-foot {
  border-top: 1px solid var(--border-subtle);
  justify-content: flex-end;
}

.sm-import-head > span:first-child {
  flex: 1;
}

.sm-import-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: var(--space-1) var(--space-2);
}

.sm-import-item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: 4px var(--space-1);
  border-radius: var(--radius-sm, 4px);
  cursor: pointer;
  font-size: var(--fs-sm);
  color: var(--text-secondary);
}

.sm-import-item:hover {
  background: var(--bg-secondary);
}

.sm-import-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sm-import-done {
  flex: none;
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
}

.sm-import-empty {
  padding: var(--space-3);
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
  text-align: center;
}

@media (max-width: 768px) {
  .sm-hint {
    display: none;
  }

  .sm-import {
    width: calc(100% - var(--space-3) * 2);
  }
}
</style>
