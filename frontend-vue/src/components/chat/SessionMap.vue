<template>
  <div
    ref="stageRef"
    class="session-map"
    tabindex="0"
    @pointerdown="onStagePointerDown"
    @wheel.prevent="onWheel"
    @dblclick="onStageDblClick"
    @keydown="onKeydown"
    @keydown.esc="onEscape"
  >
    <!-- 空态：第一次打开时给一条明确的路，而不是一张空白画布 -->
    <div
      v-if="!nodes.length"
      class="sm-empty"
    >
      <div class="sm-empty-title">
        {{ $t('这张地图还是空的') }}
      </div>
      <p class="sm-empty-text">
        {{ $t('节点只是对会话的「引用」：在这里重命名、隐藏、移除都不会影响会话本身。先把几个会话导入进来，再拖成你要的样子。') }}
      </p>
      <button
        type="button"
        class="sm-btn sm-btn-wide is-primary"
        @click="openImport"
      >
        {{ $t('导入会话') }}
      </button>
    </div>

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
          'is-hidden': n.hidden,
          'is-pinned': n.pinned,
          'is-dragging': dragId === n.id,
          'is-selected': selected.has(n.id),
          'is-grouped': !!n.groupId,
          'is-hit': searchHits.has(n.id),
        }"
        :style="cardStyle(n)"
        :title="titleOf(n)"
        role="button"
        tabindex="0"
        :aria-pressed="selected.has(n.id)"
        :aria-label="titleOf(n)"
        @pointerdown.stop="onCardPointerDown($event, n.id)"
        @click="onCardClick($event, n)"
        @keydown.enter.prevent="onCardClick($event, n)"
        @keydown.space.prevent="onCardClick($event, n)"
        @keydown.left.prevent.stop="nudge(n, -1, 0)"
        @keydown.right.prevent.stop="nudge(n, 1, 0)"
        @keydown.up.prevent.stop="nudge(n, 0, -1)"
        @keydown.down.prevent.stop="nudge(n, 0, 1)"
      >
        <!-- 内联编辑：双击标题 / 点 ✎ 进入；Enter 提交、Esc 取消、失焦提交 -->
        <input
          v-if="editingId === n.id"
          v-focus
          class="sm-card-title-input"
          :value="titleOf(n)"
          :aria-label="$t('重命名节点')"
          @pointerdown.stop
          @click.stop
          @keydown.enter.prevent="commitRename(n, ($event.target as HTMLInputElement).value)"
          @keydown.esc.prevent.stop="editingId = null"
          @blur="commitRename(n, ($event.target as HTMLInputElement).value)"
        />
        <div
          v-else
          class="sm-card-title"
          @dblclick.stop="onRename(n)"
        >
          {{ titleOf(n) }}
        </div>
        <div class="sm-card-meta">
          <span v-if="!n.sessionId" class="sm-tag">{{ $t('便签') }}</span>
          <span v-else-if="sessionOf(n.sessionId)?.tag" class="sm-tag">{{ sessionOf(n.sessionId)!.tag }}</span>
          <span v-if="n.sessionId && runningSet.has(n.sessionId)" class="sm-running">{{ $t('运行中') }}</span>
          <span v-if="n.hidden" class="sm-tag sm-tag-hidden">{{ $t('已隐藏') }}</span>
        </div>
        <!-- 分组：显示组名 + 折叠开关（此前 groupId 只当一个 class 判据，组名从不显示） -->
        <div
          v-if="groupOf(n)"
          class="sm-card-group"
        >
          <button
            type="button"
            class="sm-group-toggle"
            :aria-label="$t('折叠或展开分组')"
            :title="$t('折叠或展开分组')"
            @click.stop="toggleGroup(groupOf(n)!.id)"
          >{{ collapsedGroups.has(groupOf(n)!.id) ? '▸' : '▾' }}</button>
          <span class="sm-group-name">{{ groupOf(n)!.name }}</span>
        </div>
        <!-- 节点操作：只作用于**地图层**，绝不触碰会话本身 -->
        <div class="sm-card-ops">
          <button type="button" class="sm-op" :title="$t('重命名（只改地图上的名字）')" @click.stop="onRename(n)">✎</button>
          <button
            type="button"
            class="sm-op"
            :title="n.hidden ? $t('恢复显示') : $t('隐藏（不删除）')"
            @click.stop="map.setHidden(n.id, !n.hidden)"
          >{{ n.hidden ? '↺' : '◌' }}</button>
          <button type="button" class="sm-op is-danger" :title="$t('仅从地图移除（不会删除会话）')" @click.stop="onRemove(n)">×</button>
        </div>
      </div>
    </div>

    <!-- 浮动工具条：导入 / 便签 / 成组 / 视图 -->
    <div class="sm-hud">
      <button type="button" class="sm-btn sm-btn-wide" :title="$t('从会话列表导入')" @click="openImport">
        {{ $t('导入会话') }}
      </button>
      <button
        type="button"
        class="sm-btn sm-btn-wide"
        :disabled="selected.size < 2"
        :title="selected.size < 2 ? $t('先选中两个以上节点（点击卡片选中）') : $t('把选中的节点拼成一组')"
        @click="groupSelected"
      >
        {{ $t('成组') }}
      </button>
      <!-- 解组入口：此前 ungroup() 只被测试调用过，用户拼了组就拆不开 -->
      <button
        type="button"
        class="sm-btn sm-btn-wide"
        :disabled="!nodes.some(n => selected.has(n.id) && n.groupId)"
        :title="$t('解除选中节点所在的分组')"
        @click="ungroupSelected"
      >
        {{ $t('解组') }}
      </button>
      <!-- 已隐藏：把"隐藏"从单行道变成可逆操作（否则隐藏后永远找不回） -->
      <button
        type="button"
        class="sm-btn sm-btn-wide"
        :disabled="!hiddenCount"
        :title="hiddenCount ? $t('显示被隐藏的节点，之后可逐个恢复') : $t('没有被隐藏的节点')"
        @click="showHidden = !showHidden"
      >
        {{ showHidden ? $t('收起已隐藏') : $t('显示已隐藏') }} ({{ hiddenCount }})
      </button>
      <button
        v-if="showHidden && hiddenCount"
        type="button"
        class="sm-btn sm-btn-wide"
        :title="$t('一次恢复全部隐藏节点')"
        @click="unhideAll"
      >
        {{ $t('全部恢复') }}
      </button>
      <button
        v-if="collapsedGroups.size"
        type="button"
        class="sm-btn sm-btn-wide"
        :title="$t('展开全部分组')"
        @click="collapsedGroups = new Set()"
      >
        {{ $t('展开分组') }} ({{ collapsedGroups.size }})
      </button>
      <button
        type="button"
        class="sm-btn"
        :disabled="!canUndo"
        :title="$t('撤销（Ctrl+Z）')"
        @click="undo"
      >
        ↶
      </button>
      <button
        type="button"
        class="sm-btn"
        :disabled="!canRedo"
        :title="$t('重做（Ctrl+Shift+Z）')"
        @click="redo"
      >
        ↷
      </button>
      <span class="sm-scale">{{ Math.round(camera.scale * 100) }}%</span>
      <button type="button" class="sm-btn" :title="$t('缩小')" @click="zoomBy(1 / 1.2)">−</button>
      <button type="button" class="sm-btn" :title="$t('放大')" @click="zoomBy(1.2)">+</button>
      <button type="button" class="sm-btn" :title="$t('回到原点')" @click="resetView">⌂</button>
      <button type="button" class="sm-btn" :title="$t('适配全部')" @click="fitView">⤢</button>
      <!-- 搜索：节点一多靠肉眼平移找不现实；命中高亮 + 回车飞到第一个 -->
      <input
        v-model="query"
        class="sm-search"
        type="search"
        :placeholder="$t('搜索节点…')"
        :aria-label="$t('搜索节点')"
        @pointerdown.stop
        @keydown.enter.prevent="flyToFirstHit"
        @keydown.esc.prevent.stop="query = ''"
      />
      <span
        v-if="hitCount >= 0"
        class="sm-hit"
      >{{ hitCount ? `${$t('命中')} ${hitCount}` : $t('无匹配') }}</span>
    </div>

    <div class="sm-hint">
      {{ $t('双击空白新建便签 · 拖动卡片排布 · 点击选中（2 个以上可成组）· 空白处平移 · 滚轮缩放') }}
    </div>

    <!-- 导入面板：从**会话列表**挑选要放进地图的会话 -->
    <div v-if="importOpen" class="sm-import" @pointerdown.stop>
      <div class="sm-import-head">
        <span>{{ $t('从会话列表导入（已在地图中的会自动跳过）') }}</span>
        <button type="button" class="sm-op" @click="importOpen = false">×</button>
      </div>
      <div class="sm-import-body">
        <label v-for="s in sessions" :key="s.id" class="sm-import-item">
          <input type="checkbox" :value="s.id" :checked="picked.has(s.id)" @change="togglePick(s.id)" />
          <span class="sm-import-name">{{ s.title || $t('新对话') }}</span>
          <span v-if="importedSessionIds.has(s.id)" class="sm-import-done">{{ $t('已在图中') }}</span>
        </label>
        <div v-if="!sessions.length" class="sm-import-empty">{{ $t('会话列表为空') }}</div>
      </div>
      <div class="sm-import-foot">
        <button type="button" class="sm-btn sm-btn-wide" @click="selectAllVisible">{{ $t('全选未导入') }}</button>
        <button type="button" class="sm-btn sm-btn-wide is-primary" @click="doImport">
          {{ $t('导入所选') }}（{{ picked.size }}）
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
import { t } from '../../i18n'
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

const emit = defineEmits<{ (e: 'select', id: string): void; (e: 'close'): void }>()

const map = useSessionMap()
const { nodes, groups, camera, canUndo, canRedo, undo, redo } = map

/**
 * 被折叠的组（**组件内状态**，不入 localStorage）。
 *
 * 为什么放组件而不是 composable：`MapGroup.collapsed` 字段一直存在却从未被读写，
 * 而折叠本质是"看地图的姿势"而不是地图数据 —— 换设备/刷新后重新展开是合理默认。
 */
const collapsedGroups = ref<Set<string>>(new Set())

/** 节点所属组（含展示名）—— 组名不再只是一个 class 判据 */
function groupOf(n: MapNode): { id: string; name: string } | null {
  if (!n.groupId) return null
  const g = groups.value.find(v => v.id === n.groupId)
  return g ? { id: g.id, name: g.name || t('分组') } : null
}

function toggleGroup(groupId: string) {
  const next = new Set(collapsedGroups.value)
  if (next.has(groupId)) next.delete(groupId)
  else next.add(groupId)
  collapsedGroups.value = next
}

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
  if (s) return s.title || t('新对话')
  return n.sessionId ? t('未载入的会话') : t('便签')
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

/**
 * 是否显示被隐藏的节点。
 *
 * 为什么需要这个开关：隐藏入口一直有（卡片上的 ◌），但**没有任何恢复入口** ——
 * `setHidden(id, false)` 此前只被测试调用过。数据还在 localStorage 里，用户却再也
 * 看不见、找不回。这个开关把"隐藏"从单行道变成可逆操作。
 */
const showHidden = ref(false)

/** 被隐藏的节点数（HUD 角标与按钮可用性都看它） */
const hiddenCount = computed(() => nodes.value.filter(n => n.hidden).length)

/** 可见节点：按开关决定是否含隐藏项，再做视口裁剪（与 MessageList 的窗口化同法） */
const visibleNodes = computed(() => {
  const r = viewRect.value
  return nodes.value.filter((n) => {
    if (n.hidden && !showHidden.value) return false
    // 折叠组：整组从画布上退场（展开入口在 HUD，避免"折叠后连展开按钮都看不见"）
    if (n.groupId && collapsedGroups.value.has(n.groupId)) return false
    return n.x >= r.left && n.x <= r.right && n.y >= r.top && n.y <= r.bottom
  })
})

/** 一次找回全部隐藏节点（逐个恢复太费事，而"点错隐藏"之后想找的恰恰是全部） */
function unhideAll() {
  for (const n of nodes.value) {
    if (n.hidden) map.setHidden(n.id, false)
  }
  showHidden.value = false
}

/**
 * 解除选中节点所在的分组。
 *
 * 补上一直缺失的解组入口：`ungroup()` 此前只在测试里被调用过 —— 用户能把节点拼成组，
 * 却没有任何办法拆开。
 */
function ungroupSelected() {
  const groupIds = new Set(
    nodes.value.filter(n => selected.value.has(n.id) && n.groupId).map(n => n.groupId as string),
  )
  for (const gid of groupIds) map.ungroup(gid)
  selected.value = new Set()
}

/* ── 变换 ── */

const worldStyle = computed(() => ({
  transform: `translate(${camera.value.tx}px, ${camera.value.ty}px) scale(${camera.value.scale})`,
}))

const dragId = ref('')
const dragPos = ref<{ x: number; y: number } | null>(null)

/** 内联编辑中的节点（替代 window.prompt：不阻塞、可样式化、Esc 可退） */
const editingId = ref<string | null>(null)

/** 搜索关键词：命中高亮，Enter 飞到第一个命中项 */
const query = ref('')

/** 命中的节点 id 集合（节点名 / 会话标题 / 标签 三处模糊匹配） */
const searchHits = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return new Set<string>()
  return new Set(nodes.value.filter((n) => {
    const s = sessionOf(n.sessionId)
    const hay = [n.title, s?.title, s?.tag].filter(Boolean).join(' ').toLowerCase()
    return hay.includes(q)
  }).map(n => n.id))
})

/** -1 表示"没在搜索"，0 表示"有查询但没命中" */
const hitCount = computed(() => (query.value.trim() ? searchHits.value.size : -1))

/** 把节点飞到视野中央（搜索命中后用；相机统一经 setCamera 落盘） */
function flyTo(n: MapNode) {
  const { w, h } = viewport.value
  const scale = Math.max(0.6, Math.min(1, camera.value.scale))
  map.setCamera({ scale, tx: w / 2 - n.x * scale, ty: h / 2 - n.y * scale })
}

/** 跳到第一个命中项（搜索框回车） */
function flyToFirstHit() {
  const first = nodes.value.find(n => searchHits.value.has(n.id))
  if (first) flyTo(first)
}

/** 适配全部：把所有可见节点收进视野（HUD 的「适配」） */
function fitView() {
  const list = nodes.value.filter(n => !n.hidden)
  if (!list.length) {
    resetView()
    return
  }
  const xs = list.map(n => n.x)
  const ys = list.map(n => n.y)
  const minX = Math.min(...xs) - GRID
  const maxX = Math.max(...xs) + GRID
  const minY = Math.min(...ys) - GRID
  const maxY = Math.max(...ys) + GRID
  const { w, h } = viewport.value
  // 上限压到 1：适配的目的是"全看得见"，放大会让边缘节点出界
  const scale = Math.min(1, clampScale(Math.min(w / Math.max(1, maxX - minX),
                                               h / Math.max(1, maxY - minY))))
  const cx = (minX + maxX) / 2
  const cy = (minY + maxY) / 2
  map.setCamera({ scale, tx: w / 2 - cx * scale, ty: h / 2 - cy * scale })
}

/** Esc：编辑态优先（先取消编辑），否则请外层关闭地图 */
function onEscape() {
  if (editingId.value) {
    editingId.value = null
    return
  }
  emit('close')
}

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

function onCardClick(e: MouseEvent | KeyboardEvent, n: MapNode) {
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

/**
 * 从地图移除节点。
 *
 * 不再用阻塞式 `window.confirm`：这一步是**可撤销**的（Ctrl+Z / HUD 的 ↶）。
 * 用确认弹窗拦住一个能一键回退的操作，代价（打断、不可样式化、与产品观感割裂）
 * 比收益大。
 */
function onRemove(n: MapNode) {
  map.removeNode(n.id)
}

/** 开始内联重命名（替代 `window.prompt`） */
function onRename(n: MapNode) {
  editingId.value = n.id
}

function commitRename(n: MapNode, value: string) {
  map.renameNode(n.id, value.trim())
  editingId.value = null
}

function groupSelected() {
  if (selected.value.size < 2) return
  // 先建成默认名，名字在组容器上内联编辑（不再用 prompt 打断"成组"这个动作）
  map.groupNodes([...selected.value], '')
  selected.value = new Set()
}

/** 键盘微调：方向键把节点挪一格（吸附由 moveNode 负责，顺带置 pinned） */
function nudge(n: MapNode, dx: number, dy: number) {
  map.moveNode(n.id, n.x + dx * GRID, n.y + dy * GRID)
}

/**
 * 局部指令：内联输入框一出现就聚焦。
 * 原生 `autofocus` 对"动态插入的节点"并不可靠（浏览器只在解析阶段认它）。
 */
const vFocus = {
  mounted: (el: HTMLInputElement) => {
    el.focus()
    el.select()
  },
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

/** 双击空白 → 新建便签并**立即进入内联编辑**（不再用 prompt 打断） */
function onStageDblClick(e: MouseEvent) {
  if (e.target !== stageRef.value) return
  const { x, y } = toWorld(e.clientX, e.clientY)
  editingId.value = map.addNote(x, y, '')
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
  position: relative;
  transition: border-color var(--dur-fast) var(--ease-out), box-shadow var(--dur-fast) var(--ease-out);
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
  box-shadow: var(--shadow-lg);
  z-index: var(--z-sticky);
}

/* 运行中的会话脉冲发光 —— 状态一目了然（时长用 token 的倍数，避免硬编码） */
.sm-card.is-running {
  border-color: var(--success);
  animation: sm-pulse calc(var(--dur-slow) * 5) var(--ease-in-out) infinite;
}

/* 隐藏态：仅在「显示已隐藏」开启时出现，弱化但仍可操作（一键恢复） */
.sm-card.is-hidden {
  opacity: 0.45;
  border-style: dashed;
}

/* 手动摆过的节点：右上角图钉，表示不再参与「一键整理」 */
.sm-card.is-pinned::after {
  content: '📌';
  position: absolute;
  top: 4px;
  right: 6px;
  font-size: var(--fs-xs);
  opacity: 0.6;
}

.sm-tag-hidden { color: var(--text-tertiary); border-style: dashed; }

/* 搜索框与命中计数（HUD 内），尺寸与其它 HUD 控件对齐 */
.sm-search {
  height: 26px;
  min-width: 150px;
  padding: 0 var(--space-2);
  font-size: var(--fs-xs);
  color: var(--text-primary);
  background: var(--bg-primary);
  border: 1px solid var(--border-default);
  border-radius: var(--radius-full);
}

.sm-hit {
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
  white-space: nowrap;
}

/* 搜索命中：加一圈强调环（不改底色，避免破坏地图本身的可读性） */
.sm-card.is-hit {
  box-shadow: 0 0 0 2px var(--accent);
}

/* 内联编辑输入框：与标题同排版，进入编辑时整卡尽量不跳动 */
.sm-card-title-input {
  width: 100%;
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--text-primary);
  background: var(--bg-secondary);
  border: 1px solid var(--accent);
  border-radius: var(--radius-sm);
  padding: 0 var(--space-1);
}

/* 分组标识：组名 + 折叠开关 */
.sm-card-group {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
}
.sm-group-toggle {
  border: none;
  background: transparent;
  color: inherit;
  cursor: pointer;
  padding: 0 2px;
  font-size: var(--fs-xs);
}
.sm-group-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 空态：第一次打开时给一条明确的路，而不是一张空白画布 */
.sm-empty {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-3);
  padding: var(--space-6);
  text-align: center;
  pointer-events: none;
}
.sm-empty-title {
  font-size: var(--fs-md);
  font-weight: var(--fw-semibold);
  color: var(--text-primary);
}
.sm-empty-text {
  max-width: 44ch;
  margin: 0;
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
  color: var(--text-tertiary);
}
.sm-empty .sm-btn {
  pointer-events: auto;
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
