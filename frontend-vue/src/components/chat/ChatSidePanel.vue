<script setup lang="ts">
import { computed, ref, watch, onMounted, onUnmounted } from 'vue'
import { Button, Avatar, Dropdown, Menu, MenuItem, MenuDivider, SubMenu, Input, message } from 'ant-design-vue'
import {
  SearchOutlined, CloseOutlined, LeftOutlined, DownOutlined,
  PlusOutlined, EllipsisOutlined, EditOutlined, PushpinOutlined,
  ShareAltOutlined, DeleteOutlined, TagOutlined, ReloadOutlined, ThunderboltOutlined,
  ApartmentOutlined,
} from '@ant-design/icons-vue'
import { useRouter } from 'vue-router'
import { api, listTools, quickExecute } from '../../api'
import type { ToolInfo } from '../../utils/toolList'
import type { ContextChip } from './contextChips'
import { formatRelativeTime } from './chat-types'
import type { ChatItem, ChatSession } from './chat-types'
import SubAgentPanel from './SubAgentPanel.vue'
import type { SubagentEvent } from '../../api/subagent'

import { useI18n } from 'vue-i18n'
const { t: tr } = useI18n()
const props = withDefaults(defineProps<{
  items: ChatItem[]
  selectedIndex: number | null
  open: boolean
  /** 面板视图：trajectory（主，当前会话提问轨迹）/ sessions（从，会话历史列表）/ agents（子 Agent 层级） */
  view: 'trajectory' | 'sessions' | 'agents'
  sessions: ChatSession[]
  activeSessionId: string
  userName?: string
  /** 当前会话上下文芯片（知识库/Agent/技能/工作流，可移除） */
  contextChips?: ContextChip[]
  /** 本会话实时到达的 subagent.* 事件（由 ChatView 从 SSE 分流后传入） */
  liveEvents?: SubagentEvent[]
}>(), {
  contextChips: () => [],
  liveEvents: () => [],
})

const emit = defineEmits<{
  (e: 'focus', index: number): void
  (e: 'close'): void
  (e: 'update:view', view: 'trajectory' | 'sessions' | 'agents'): void
  (e: 'create'): void
  (e: 'switch', id: string): void
  (e: 'delete', id: string): void
  (e: 'rename', id: string, currentTitle: string): void
  (e: 'pin', id: string, pinned: boolean): void
  (e: 'share', id: string): void
  /** P3-D: 设置会话标签 */
  (e: 'tag', id: string, tag: string): void
  /** 移除单个上下文芯片（父级同步改写路由 query；同类可能还有其它值） */
  (e: 'remove-context', type: ContextChip['type'], value: string): void
  /** 清空全部上下文（父级同步清空路由 query） */
  (e: 'clear-context'): void
}>()

const router = useRouter()
const trajectoryQuery = ref('')
const sessionQuery = ref('')
const hoveredIndex = ref<number | null>(null)
// 当前展开菜单的会话行 id：菜单打开时行保持 hover 态
const menuSessionId = ref<string | null>(null)

// ── 抽屉模式判定（≤1024px）：触摸手势仅在抽屉模式下生效，桌面常驻面板不受影响 ──
const drawerMq = window.matchMedia('(max-width: 1024px)')
const isDrawerMode = ref(drawerMq.matches)
drawerMq.addEventListener?.('change', (e: MediaQueryListEvent) => { isDrawerMode.value = e.matches })

// ── 移动端左滑关闭手势 ──
const dragX = ref(0)
const dragStartX = ref(0)
const dragging = ref(false)
const SWIPE_THRESHOLD = 80 // 拖拽超过 80px 触发关闭

function onTouchStart(e: TouchEvent) {
  if (!props.open || !isDrawerMode.value) return
  const touch = e.touches[0]
  dragStartX.value = touch.clientX
  dragging.value = true
}

function onTouchMove(e: TouchEvent) {
  if (!dragging.value) return
  const touch = e.touches[0]
  const delta = touch.clientX - dragStartX.value
  // 仅跟随向左拖拽（delta < 0），向右拖拽不超出
  dragX.value = Math.min(0, delta)
}

function onTouchEnd() {
  if (!dragging.value) return
  dragging.value = false
  if (dragX.value < -SWIPE_THRESHOLD) {
    emit('close')
  }
  dragX.value = 0
}

const panelStyle = computed(() => {
  if (isDrawerMode.value && dragX.value !== 0) {
    return { transform: `translateX(${dragX.value}px)`, transition: dragging.value ? 'none' : 'transform 0.25s ease' }
  }
  return undefined
})

const activeSession = computed(() => props.sessions.find(s => s.id === props.activeSessionId) || null)

/**
 * 同类上下文芯片的序号（1 起）。同类只有一个时返回 0 = 不显示序号。
 *
 * 顺序是有语义的：多个 Agent 取第一个为主、其余作为可委派的专家；多个工作流按
 * 顺序依次执行。所以同类多选时必须把次序显式画出来，否则用户无从知道哪个是主。
 */
function chipOrder(chip: ContextChip): number {
  const sameType = props.contextChips.filter(c => c.type === chip.type)
  if (sameType.length < 2) return 0
  return sameType.findIndex(c => c.value === chip.value) + 1
}

// ── 快捷操作：发起统一任务（复用快速命令：创建 uni 会话 → 跳转 /chat?task=）──
const unifiedTaskInput = ref('')
const launchingUnified = ref(false)

async function launchUnified() {
  const text = unifiedTaskInput.value.trim()
  if (!text || launchingUnified.value) return
  launchingUnified.value = true
  try {
    // 与 WorkstationNav 一致：客户端生成 uni_ 会话 id → quick-execute 创建 → 跳转统一会话
    const sessionId = `uni_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
    const res = await quickExecute({ message: text, session_id: sessionId, mode: 'auto' })
    const q: Record<string, string> = { task: sessionId }
    // 携带当前上下文（kb/agent/skill/workflow）到统一会话
    for (const c of props.contextChips || []) q[c.type] = c.value
    if (res?.success === false) q.error = res.error || 'execution failed'
    unifiedTaskInput.value = ''
    await router.push({ path: '/chat', query: q })
    message.success(tr('任务已提交，正在对话页展示结果'))
  } catch (e: any) {
    message.error('发起统一任务失败: ' + (e?.message || tr('网络错误')))
  } finally {
    launchingUnified.value = false
  }
}

// ── 可用工具（/v1/tools：含 MCP/插件注入的代理工具，source='mcp'）──
// 懒加载：展开时才请求，避免每次打开面板都打一次接口
const availableTools = ref<ToolInfo[]>([])
const toolsLoading = ref(false)
const toolsError = ref(false)
const toolsExpanded = ref(false)
let toolsLoaded = false

const mcpToolCount = computed(() => availableTools.value.filter(t => t.source === 'mcp').length)

/** 标题右侧摘要：直接列出 MCP 工具名 —— 只报数字等于没说清"到底激活了哪些能力" */
const toolsSummary = computed(() => {
  if (toolsError.value) return '加载失败'
  if (toolsLoading.value) return '…'
  const total = availableTools.value.length
  if (!total) return '无'
  const mcpNames = availableTools.value.filter(isMcpTool).map(t => t.name).slice(0, 3)
  if (mcpNames.length) {
    const more = mcpToolCount.value > mcpNames.length ? ` +${mcpToolCount.value - mcpNames.length}` : ''
    return `${total} 个 · MCP ${mcpNames.join(', ')}${more}`
  }
  return `${total} 个`
})

function isMcpTool(t: ToolInfo): boolean {
  return t.source === 'mcp'
}

async function loadTools() {
  if (toolsLoading.value || toolsLoaded) return
  toolsLoading.value = true
  toolsError.value = false
  try {
    availableTools.value = await listTools()
    toolsLoaded = true
  } catch {
    // 失败不自动重试：收起再展开即可重来（toolsLoaded 仍为 false）
    toolsError.value = true
  } finally {
    toolsLoading.value = false
  }
}

function toggleTools() {
  toolsExpanded.value = !toolsExpanded.value
  if (toolsExpanded.value) void loadTools()
}

// ── 最近活动（/v1/activities，30s 轮询；点击跳转）──
interface ActivityItem {
  id: string
  title: string
  route: string
  status: string
  timestamp: string | number
}
const recentActivities = ref<ActivityItem[]>([])
const activitiesLoading = ref(false)
let activityTimer: ReturnType<typeof setInterval> | null = null

async function loadActivities() {
  if (!props.open) return
  activitiesLoading.value = true
  try {
    const res = await api.get('/v1/activities?limit=8')
    const list = res.data?.activities || []
    recentActivities.value = list.map((a: any, i: number) => ({
      id: a.id || `${a.workstation || 'act'}_${a.timestamp || i}`,
      title: a.title || tr('暂无标题'),
      route: a.route || '/chat',
      status: a.status || '',
      timestamp: a.timestamp || 0,
    }))
  } catch {
    /* 拉取失败保留上次列表 */
  } finally {
    activitiesLoading.value = false
  }
}

function actTime(ts: string | number): string {
  if (!ts) return ''
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? '' : formatRelativeTime(d.toISOString(), tr)
}

function goActivity(a: ActivityItem) {
  void router.push(a.route || '/chat')
}

// 面板打开时立即刷新一次（常驻挂载，onMounted 只跑一次）
watch(() => props.open, (v) => { if (v) void loadActivities() })

onMounted(() => {
  void loadActivities()
  activityTimer = setInterval(() => { void loadActivities() }, 30000)
})

onUnmounted(() => {
  if (activityTimer) { clearInterval(activityTimer); activityTimer = null }
})

// ── 主视图：当前会话轨迹（仅用户提问作为锚点） ──
const userIndexes = computed(() =>
  props.items
    .map((it, i) => ({ it, i }))
    .filter(x => x.it.kind === 'text' && x.it.role === 'user')
    .map(x => x.i),
)

const filteredIndexes = computed(() => {
  const q = trajectoryQuery.value.trim().toLowerCase()
  if (!q) return userIndexes.value
  return userIndexes.value.filter(i => {
    const it = props.items[i]
    return it.kind === 'text' && it.content.toLowerCase().includes(q)
  })
})

function summary(index: number): string {
  const it = props.items[index]
  if (it.kind !== 'text') return ''
  const s = it.content.replace(/\s+/g, ' ').trim()
  return s.length > 40 ? s.slice(0, 40) + '…' : s
}

// ── 从视图：会话历史列表 ──
const filteredSessions = computed(() => {
  const q = sessionQuery.value.trim().toLowerCase()
  const all = props.sessions || []
  let list = all
  // P3-D: 按标签筛选
  if (activeTag.value) {
    list = list.filter(s => (s.tag || '') === activeTag.value)
  }
  if (!q) return list
  return list.filter(s => (s.title || '').toLowerCase().includes(q))
})

// P3-D: 会话标签筛选
const TAGS = ['工作', '学习', '项目', '其他']
const activeTag = ref('')
// 从所有会话中提取已使用的标签
const usedTags = computed(() => {
  const set = new Set<string>()
  for (const s of props.sessions || []) {
    if (s.tag) set.add(s.tag)
  }
  return Array.from(set)
})
function toggleTag(tag: string) {
  activeTag.value = activeTag.value === tag ? '' : tag
}

// P2-B: 会话按时间分组（置顶单独一组，其余按 今日/昨天/7天/更早）
interface SessionGroup { label: string; sessions: any[] }
const groupedSessions = computed<SessionGroup[]>(() => {
  const list = filteredSessions.value
  // 置顶组始终在最前
  const pinned = list.filter(s => s.pinned)
  const rest = list.filter(s => !s.pinned)
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const startOfYesterday = startOfToday - 86400000
  const startOf7Days = startOfToday - 7 * 86400000
  const groups: SessionGroup[] = []
  if (pinned.length) groups.push({ label: tr('置顶'), sessions: pinned })
  const buckets: Record<string, any[]> = { '今天': [], '昨天': [], '7 天内': [], '更早': [] }
  for (const s of rest) {
    const ts = new Date(s.updated_at || s.created_at || 0).getTime()
    if (ts >= startOfToday) buckets['今天'].push(s)
    else if (ts >= startOfYesterday) buckets['昨天'].push(s)
    else if (ts >= startOf7Days) buckets['7 天内'].push(s)
    else buckets['更早'].push(s)
  }
  for (const label of ['今天', '昨天', '7 天内', '更早']) {
    if (buckets[label].length) groups.push({ label, sessions: buckets[label] })
  }
  return groups
})

function onMenuOpenChange(open: boolean, id: string) {
  menuSessionId.value = open ? id : null
}

// 选中会话：切到该会话轨迹（父级 switchSession 加载消息）
function pickSession(id: string) {
  emit('switch', id)
  emit('update:view', 'trajectory')
}
</script>

<template>
  <div
    class="side-panel"
    :class="{ open, dragging }"
    :style="panelStyle"
    role="complementary"
    :aria-hidden="!open"
    @touchstart.passive="onTouchStart"
    @touchmove.passive="onTouchMove"
    @touchend.passive="onTouchEnd"
  >
    <!-- 顶部：会话选择器（主从钻取入口）+ 关闭 -->
    <div class="panel-toolbar">
      <button
        v-if="view === 'trajectory'"
        type="button"
        class="session-picker"
        :title="'切换会话：' + (activeSession?.title || '新对话')"
        @click="emit('update:view', 'sessions')"
      >
        <span class="session-picker-name">{{ activeSession?.title || '新对话' }}</span>
        <DownOutlined class="session-picker-arrow" />
      </button>
      <button
        v-else
        type="button"
        class="session-back"
        @click="emit('update:view', 'trajectory')"
      >
        <LeftOutlined />
        <span class="session-picker-name">{{ activeSession?.title || '新对话' }}</span>
      </button>
      <button
        type="button"
        class="session-back"
        :class="{ active: view === 'agents' }"
        :title="view === 'agents' ? $t('返回提问轨迹') : $t('子 Agent 层级')"
        @click="emit('update:view', view === 'agents' ? 'trajectory' : 'agents')"
      >
        <ApartmentOutlined />
      </button>
      <CloseOutlined
        class="toolbar-close"
        :title="$t('收起面板')"
        @click="emit('close')"
      />
    </div>

    <!-- 顶部：当前会话上下文（知识库/Agent/技能/工作流芯片，可移除；移除由父级清空 query 与 context） -->
    <div
      v-if="contextChips.length"
      class="panel-context"
    >
      <span class="ctx-title">{{ $t('当前上下文') }}</span>
      <div class="ctx-chips">
        <span
          v-for="c in contextChips"
          :key="c.type"
          class="ctx-chip"
          :title="`${c.label}（点击移除）`"
        >
          <span class="ctx-chip-label">
            <template v-if="chipOrder(c)">{{ chipOrder(c) }}. </template>{{ c.label }}
          </span>
          <CloseOutlined
            class="ctx-chip-remove"
            :title="`移除${c.label}`"
            @click="emit('remove-context', c.type, c.value)"
          />
        </span>
      </div>
    </div>

    <!-- 中部：快捷操作（发起统一任务 / 清空上下文） -->
    <div class="panel-quick">
      <span class="quick-title">{{ $t('快捷操作') }}</span>
      <div class="quick-task-row">
        <Input
          v-model:value="unifiedTaskInput"
          size="small"
          class="quick-task-input"
          :placeholder="$t('输入任务，发起统一执行…')"
          :disabled="launchingUnified"
          @press-enter="launchUnified"
        />
        <Button
          size="small"
          type="primary"
          class="quick-launch-btn"
          :loading="launchingUnified"
          :disabled="!unifiedTaskInput.trim()"
          @click="launchUnified"
        >
          <template #icon>
            <ThunderboltOutlined />
          </template>
          {{ $t('发起') }}
        </Button>
      </div>
      <button
        type="button"
        class="quick-clear"
        :disabled="!contextChips.length"
        :title="$t('清空知识库/Agent/技能/工作流上下文')"
        @click="emit('clear-context')"
      >
        {{ $t('清空上下文') }}
      </button>
    </div>

    <!-- 中部：可用工具（/v1/tools）——把 MCP/插件注入的工具从"看不见"变成"看得见"。
         默认收起：内置工具数十个，展开会挤掉下面的轨迹与活动区 -->
    <div class="panel-tools">
      <button
        type="button"
        class="tools-head"
        :title="toolsExpanded ? '收起工具列表' : '展开工具列表'"
        @click="toggleTools"
      >
        <span class="tools-title">{{ $t('可用工具') }}</span>
        <span class="tools-count">{{ toolsSummary }}</span>
        <DownOutlined
          class="tools-arrow"
          :class="{ expanded: toolsExpanded }"
        />
      </button>
      <div
        v-if="toolsExpanded"
        class="tools-body"
      >
        <div
          v-if="toolsLoading"
          class="tools-empty"
        >
          {{ $t('加载中…') }}
        </div>
        <div
          v-else-if="toolsError"
          class="tools-empty"
        >
          {{ $t('工具列表加载失败') }}
        </div>
        <div
          v-else-if="!availableTools.length"
          class="tools-empty"
        >
          {{ $t('没有可用工具') }}
        </div>
        <template v-else>
          <div
            v-for="t in availableTools"
            :key="t.name"
            class="tool-row"
            :title="t.description || t.name"
          >
            <span class="tool-name">{{ t.name }}</span>
            <span
              v-if="isMcpTool(t)"
              class="tool-badge"
            >MCP</span>
          </div>
        </template>
      </div>
    </div>

    <!-- 子 Agent 层级视图：递归树 + 选中运行的实时输出 -->
    <SubAgentPanel
      v-if="view === 'agents'"
      :session-id="activeSessionId"
      :live-events="liveEvents"
    />

    <!-- 主视图：当前会话轨迹（搜索 + 时间线 + 提问锚点） -->
    <template v-if="view === 'trajectory'">
      <div class="panel-search">
        <SearchOutlined class="search-icon" />
        <input
          v-model="trajectoryQuery"
          class="search-input"
          :placeholder="$t('搜索提问')"
        >
        <CloseOutlined
          v-if="trajectoryQuery"
          class="search-clear"
          @click="trajectoryQuery = ''"
        />
      </div>

      <div class="timeline">
        <div class="timeline-labels">
          <span>0</span><span>50</span><span>100</span>
        </div>
        <div class="timeline-track">
          <div
            v-for="i in filteredIndexes"
            :key="i"
            class="timeline-span"
            :class="{ selected: i === selectedIndex }"
            :data-selected="i === selectedIndex ? undefined : (selectedIndex === null ? undefined : 'false')"
            :data-hovered="i === hoveredIndex"
            :data-current="i === selectedIndex"
            :style="{
              left: `calc(${((userIndexes.indexOf(i)) / Math.max(userIndexes.length, 1)) * 100}% + 4px)`,
              width: `calc(${100 / Math.max(userIndexes.length, 1)}% - 8px)`,
            }"
            :title="summary(i)"
            role="button"
            tabindex="0"
            :aria-label="summary(i)"
            @click.stop="emit('focus', i)"
            @keydown.enter.prevent="emit('focus', i)"
            @keydown.space.prevent="emit('focus', i)"
            @mouseenter="hoveredIndex = i"
            @mouseleave="hoveredIndex = null"
          />
          <div
            v-if="filteredIndexes.length === 0"
            class="timeline-empty"
          >
            {{ $t('无提问') }}
          </div>
        </div>
      </div>

      <div class="anchor-list">
        <div
          v-for="i in filteredIndexes"
          :key="i"
          class="anchor-row"
          :class="{ active: i === selectedIndex }"
          role="button"
          tabindex="0"
          :aria-label="summary(i)"
          @click="emit('focus', i)"
          @keydown.enter.prevent="emit('focus', i)"
          @keydown.space.prevent="emit('focus', i)"
        >
          <span
            class="row-dot"
            aria-hidden
          />
          <span class="row-text">{{ summary(i) }}</span>
        </div>
        <div
          v-if="filteredIndexes.length === 0"
          class="list-empty"
        >
          {{ $t('当前会话暂无提问') }}
        </div>
      </div>

      <!-- 底部：最近活动（/v1/activities，30s 轮询，点击跳转） -->
      <div class="panel-activities">
        <div class="act-head">
          <span class="act-title">{{ $t('最近活动') }}</span>
          <span
            class="act-refresh"
            role="button"
            tabindex="0"
            :title="$t('刷新')"
            :aria-label="$t('刷新')"
            @click="loadActivities"
            @keydown.enter.prevent="loadActivities"
            @keydown.space.prevent="loadActivities"
          ><ReloadOutlined /></span>
        </div>
        <div
          v-if="activitiesLoading && !recentActivities.length"
          class="act-empty"
        >
          {{ $t('加载中…') }}
        </div>
        <div
          v-else-if="!recentActivities.length"
          class="act-empty"
        >
          {{ $t('暂无活动') }}
        </div>
        <div
          v-else
          class="act-list"
        >
          <button
            v-for="a in recentActivities"
            :key="a.id"
            type="button"
            class="act-row"
            :title="a.title"
            @click="goActivity(a)"
          >
            <span
              class="act-dot"
              :class="a.status || ''"
            />
            <span class="act-text">{{ a.title }}</span>
            <span class="act-time">{{ actTime(a.timestamp) }}</span>
          </button>
        </div>
      </div>
    </template>

    <!-- 从视图：会话历史列表（新对话 + 搜索 + 行操作菜单 + 用户） -->
    <template v-else>
      <div class="sessions-head">
        <Button
          block
          type="primary"
          size="small"
          @click="emit('create')"
        >
          <template #icon>
            <PlusOutlined />
          </template>
          {{ $t('新对话') }}
        </Button>
        <div class="panel-search">
          <SearchOutlined class="search-icon" />
          <input
            v-model="sessionQuery"
            class="search-input"
            :placeholder="$t('搜索会话')"
          >
          <CloseOutlined
            v-if="sessionQuery"
            class="search-clear"
            @click="sessionQuery = ''"
          />
        </div>
        <!-- P3-D: 标签筛选 chips -->
        <div
          v-if="usedTags.length"
          class="tag-filter"
        >
          <button
            v-for="tag in usedTags"
            :key="tag"
            class="tag-chip"
            :class="{ active: activeTag === tag }"
            type="button"
            @click="toggleTag(tag)"
          >
            {{ tag }}
          </button>
        </div>
      </div>

      <div class="session-list">
        <div
          v-if="filteredSessions.length === 0"
          class="list-empty"
        >
          {{ $t('暂无对话') }}
        </div>
        <!-- P2-B: 按时间分组渲染（置顶/今天/昨天/7天内/更早） -->
        <template
          v-for="group in groupedSessions"
          :key="group.label"
        >
          <div class="session-group-label">
            {{ group.label }}
          </div>
          <div
            v-for="s in group.sessions"
            :key="s.id"
            class="session-row"
            :class="{ active: s.id === activeSessionId, pinned: s.pinned, 'menu-open': menuSessionId === s.id }"
            role="button"
            tabindex="0"
            @click="pickSession(s.id)"
            @keydown.enter.prevent="pickSession(s.id)"
            @keydown.space.prevent="pickSession(s.id)"
          >
            <div class="session-info">
              <div class="session-title-line">
                <PushpinOutlined
                  v-if="s.pinned"
                  class="pin-icon"
                />
                <span class="session-title">{{ s.title || '新对话' }}</span>
                <span
                  v-if="s.tag"
                  class="session-tag"
                >{{ s.tag }}</span>
              </div>
              <span class="session-time">{{ formatRelativeTime(s.updated_at || s.created_at, tr) }}</span>
            </div>
            <Dropdown
              trigger="click"
              placement="bottomRight"
              @open-change="(v: boolean) => onMenuOpenChange(v, s.id)"
            >
              <Button
                type="text"
                size="small"
                class="session-more-btn"
                :aria-label="`会话操作：${s.title || '新对话'}`"
                @click.stop
              >
                <template #icon>
                  <EllipsisOutlined />
                </template>
              </Button>
              <template #overlay>
                <Menu class="session-menu">
                  <MenuItem
                    key="rename"
                    @click="emit('rename', s.id, s.title || '')"
                  >
                    <EditOutlined class="menu-icon" />{{ $t('重命名') }}
                  </MenuItem>
                  <MenuItem
                    key="pin"
                    @click="emit('pin', s.id, !s.pinned)"
                  >
                    <PushpinOutlined class="menu-icon" />{{ s.pinned ? '取消置顶' : '置顶' }}
                  </MenuItem>
                  <!-- P3-D: 标签设置（用 MenuDivider 分组，避免 SubMenu 在 Dropdown overlay 中丢失上下文） -->
                  <MenuDivider />
                  <MenuItem
                    v-for="t in TAGS"
                    :key="'tag-'+t"
                    @click="emit('tag', s.id, t)"
                  >
                    <TagOutlined class="menu-icon" />标签：{{ t }}
                  </MenuItem>
                  <MenuItem
                    key="tag-clear"
                    @click="emit('tag', s.id, '')"
                  >
                    <CloseOutlined class="menu-icon" />{{ $t('清除标签') }}
                  </MenuItem>
                  <MenuDivider />
                  <MenuItem
                    key="share"
                    @click="emit('share', s.id)"
                  >
                    <ShareAltOutlined class="menu-icon" />{{ $t('分享') }}
                  </MenuItem>
                  <MenuDivider />
                  <MenuItem
                    key="delete"
                    danger
                    @click="emit('delete', s.id)"
                  >
                    <DeleteOutlined class="menu-icon" />{{ $t('删除') }}
                  </MenuItem>
                </Menu>
              </template>
            </Dropdown>
          </div>
        </template>
      </div>

      <!-- 底部：最近活动（/v1/activities，30s 轮询，点击跳转） -->
      <div class="panel-activities">
        <div class="act-head">
          <span class="act-title">{{ $t('最近活动') }}</span>
          <span
            class="act-refresh"
            role="button"
            tabindex="0"
            :title="$t('刷新')"
            :aria-label="$t('刷新')"
            @click="loadActivities"
            @keydown.enter.prevent="loadActivities"
            @keydown.space.prevent="loadActivities"
          ><ReloadOutlined /></span>
        </div>
        <div
          v-if="activitiesLoading && !recentActivities.length"
          class="act-empty"
        >
          {{ $t('加载中…') }}
        </div>
        <div
          v-else-if="!recentActivities.length"
          class="act-empty"
        >
          {{ $t('暂无活动') }}
        </div>
        <div
          v-else
          class="act-list"
        >
          <button
            v-for="a in recentActivities"
            :key="a.id"
            type="button"
            class="act-row"
            :title="a.title"
            @click="goActivity(a)"
          >
            <span
              class="act-dot"
              :class="a.status || ''"
            />
            <span class="act-text">{{ a.title }}</span>
            <span class="act-time">{{ actTime(a.timestamp) }}</span>
          </button>
        </div>
      </div>

      <div class="panel-foot">
        <Avatar
          :size="22"
          :style="{ backgroundColor: 'var(--primary)' }"
        >
          {{ (userName || 'U').charAt(0).toUpperCase() }}
        </Avatar>
        <span class="foot-name">{{ userName || '用户' }}</span>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* 上下文面板：≤1024px 为自由浮动抽屉（悬浮于整页右侧，覆盖聊天区，不参与文档流；
   隐藏时 translateX 移出 + visibility 延迟隐藏，避免溢出视口产生横向滚动条）；
   ≥1025px 为文档流内常驻面板（见下方 min-width 媒体查询） */
.side-panel {
  position: absolute;
  top: 0; right: 0; bottom: 0;
  width: 320px;
  z-index: 120;
  display: flex; flex-direction: column;
  background: var(--bg-card);
  border-left: 1px solid var(--border);
  box-shadow: var(--sig-shadow-hover);
  transform: translateX(100%);
  visibility: hidden;
  transition: transform 0.25s ease, visibility 0.25s;
  touch-action: pan-y; /* 允许纵向滚动，横向交给手势 */
  will-change: transform;
}
.side-panel.open { transform: translateX(0); visibility: visible; }
.side-panel.dragging { transition: none; }
@media (max-width: 768px) { .side-panel { width: 100%; } }
/* ── 上下文面板：≥1025px 常驻展开（文档流内 flex 子项，不覆盖聊天区）── */
@media (min-width: 1025px) {
  .side-panel {
    position: relative; top: auto; right: auto; bottom: auto;
    width: 300px; z-index: auto;
    flex: none;
    transform: none; visibility: visible;
    box-shadow: none;
  }
  .side-panel:not(.open) { display: none; }
}
/* ── 响应式：≤1024px 抽屉宽度自适应（平板半屏抽屉），≤576px 全屏 ── */
@media (max-width: 1024px) { .side-panel { width: min(420px, 100%); } }
@media (max-width: 768px) {
  .session-picker, .session-back { height: 36px; }
  .toolbar-close { padding: 10px; font-size: 14px; }
  .session-more-btn { width: 36px; height: 36px; }
  .anchor-row { min-height: 36px; }
  .tag-chip { min-height: 32px; }
  .panel-foot { padding-bottom: calc(10px + env(safe-area-inset-bottom)); }
}
@media (max-width: 576px) { .side-panel { width: 100%; } }
/* 触屏无 hover：行操作按钮常驻可点 */
@media (hover: none) { .session-more-btn { opacity: 1; } }

/* 顶部工具栏：会话选择器（主从钻取）+ 关闭 */
.panel-toolbar {
  flex: none; display: flex; align-items: center; gap: 8px;
  height: 44px; padding: 0 12px;
  border-bottom: 1px solid var(--border);
}
.session-picker, .session-back {
  flex: 1; min-width: 0; display: flex; align-items: center; gap: 6px;
  height: 28px; padding: 0 8px; border: none; border-radius: var(--sig-radius-button);
  background: transparent; color: var(--text-primary);
  font-size: 13px; font-weight: 600; cursor: pointer;
  transition: background 0.15s ease;
}
.session-picker:hover, .session-back:hover { background: var(--bg-hover); }
.session-picker-name { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; text-align: left; }
.session-picker-arrow { flex: none; font-size: 10px; color: var(--text-tertiary); }
.toolbar-close { flex: none; font-size: 13px; color: var(--text-tertiary); cursor: pointer; padding: 4px; border-radius: 4px; transition: color 0.15s ease, background 0.15s ease; }
.toolbar-close:hover { color: var(--text-primary); background: var(--bg-hover); }

/* ── 当前会话上下文 chips（与消息区/侧栏 tag-chip 同设计语言）── */
.panel-context { flex: none; padding: 8px 12px 0; }
.ctx-title { display: block; font-size: 11px; color: var(--text-tertiary); margin-bottom: 6px; }
.ctx-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.ctx-chip {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 2px 8px; border-radius: var(--sig-radius-button);
  border: 1px solid var(--border); background: var(--bg-card);
  color: var(--text-secondary); font-size: 12px;
  transition: border-color 0.15s ease, color 0.15s ease;
}
.ctx-chip:hover { border-color: var(--primary); color: var(--primary); }
.ctx-chip-label { max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ctx-chip-remove { font-size: 10px; color: var(--text-tertiary); cursor: pointer; }
.ctx-chip-remove:hover { color: var(--danger, #ef4444); }

/* ── 快捷操作：发起统一任务 + 清空上下文 ── */
.panel-quick { flex: none; padding: 10px 12px; border-bottom: 1px solid var(--border); }
.quick-title { display: block; font-size: 11px; color: var(--text-tertiary); margin-bottom: 6px; }
.quick-task-row { display: flex; gap: 6px; }
.quick-task-input { flex: 1; min-width: 0; }
.quick-task-input :deep(input) { font-size: 12px; }
.quick-launch-btn { flex: none; }
.quick-clear {
  margin-top: 6px; padding: 0; border: none; background: none;
  font-size: 11px; color: var(--text-tertiary); cursor: pointer;
  transition: color 0.15s ease;
}
.quick-clear:hover:not(:disabled) { color: var(--danger, #ef4444); }
.quick-clear:disabled { opacity: 0.4; cursor: not-allowed; }

/* ── 可用工具：让 MCP/插件注入的工具在对话页可见（默认收起）── */
.panel-tools { flex: none; border-bottom: 1px solid var(--border); }
.tools-head {
  display: flex; align-items: center; gap: 6px; width: 100%;
  padding: 8px 12px; border: none; background: none;
  color: var(--text-tertiary); font-size: 11px; text-align: left; cursor: pointer;
  transition: color 0.15s ease;
}
.tools-head:hover { color: var(--primary); }
.tools-title { flex: none; }
.tools-count { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tools-arrow { flex: none; font-size: 10px; transition: transform 0.15s ease; }
.tools-arrow.expanded { transform: rotate(180deg); }
.tools-body { max-height: 200px; overflow-y: auto; padding: 0 6px 6px; scrollbar-width: thin; scrollbar-color: var(--text-disabled) transparent; }
.tools-empty { padding: 10px 8px; text-align: center; color: var(--text-muted); font-size: 12px; }
.tool-row { display: flex; align-items: center; gap: 6px; padding: 5px 8px; border-radius: var(--sig-radius-button); }
.tool-row:hover { background: var(--bg-hover); }
.tool-name { flex: 1; min-width: 0; font-size: 12px; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tool-badge {
  flex: none; padding: 0 6px; border-radius: var(--sig-radius-card);
  background: var(--primary-bg); color: var(--primary);
  font-size: 10px; line-height: 16px; font-weight: 600;
}

/* 搜索框（轨迹 / 会话通用） */
.panel-search { flex: none; display: flex; align-items: center; gap: 4px; margin: 8px 12px 0; padding: 0 8px; height: 28px; background: var(--bg-secondary); border-radius: var(--sig-radius-button); }
.search-icon { font-size: 11px; color: var(--text-tertiary); }
.search-input { flex: 1; border: none; outline: none; background: none; font-size: 12px; color: var(--text-primary); }
.search-clear { font-size: 10px; color: var(--text-tertiary); cursor: pointer; }

/* ── 主视图：时间线 ── */
.timeline {
  flex: none; display: grid; grid-template-columns: 44px minmax(0, 1fr);
  height: 50px; margin-top: 8px; overflow: hidden;
  border-bottom: 1px solid var(--border);
  background: var(--bg-secondary);
  user-select: none;
}
.timeline-labels { position: relative; border-right: 1px solid var(--border); color: var(--text-tertiary); font-size: 10px; line-height: 1; }
.timeline-labels span { position: absolute; right: 3px; height: 8px; display: flex; align-items: center; }
.timeline-labels span:nth-child(1) { top: 7px; }
.timeline-labels span:nth-child(2) { top: 21px; }
.timeline-labels span:nth-child(3) { top: 35px; }
.timeline-track { position: relative; overflow: hidden; cursor: crosshair; }
.timeline-span {
  position: absolute; top: 21px; height: 8px; min-width: 2px;
  border-radius: 1px;
  background: var(--primary);
  opacity: 0.78;
  transition: opacity 0.15s ease;
}
.timeline-span[data-hovered='true']:not([data-current='true']) {
  opacity: 1;
  box-shadow: 0 0 0 1px var(--bg-secondary), 0 0 0 2px color-mix(in srgb, var(--primary) 80%, transparent);
}
.timeline-span[data-selected='false'] { opacity: 0.2; }
.timeline-span[data-current='true'] { opacity: 1; }
.timeline-empty { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); color: var(--text-tertiary); font-size: 12px; }

/* ── 主视图：提问锚点列表 ── */
.anchor-list { flex: 1; overflow-y: auto; padding: 6px; scrollbar-width: thin; scrollbar-color: var(--text-disabled) transparent; }
.anchor-row { display: flex; align-items: center; gap: 8px; padding: 7px 8px; border-radius: var(--sig-radius-button); cursor: pointer; transition: background 0.15s ease; }
.anchor-row:hover { background: var(--bg-hover); }
.anchor-row.active { background: var(--primary-bg); }
.row-dot { flex: none; width: 6px; height: 6px; border-radius: 50%; background: var(--primary); }
.row-text { flex: 1; min-width: 0; font-size: 12px; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.anchor-row.active .row-text { color: var(--primary); font-weight: 600; }
.list-empty { padding: 20px 8px; text-align: center; color: var(--text-muted); font-size: 12px; }

/* ── 从视图：会话历史列表 ── */
.sessions-head { flex: none; display: flex; flex-direction: column; gap: 8px; padding: 10px 12px 0; }
.session-list { flex: 1; overflow-y: auto; padding: 6px; scrollbar-width: thin; scrollbar-color: var(--text-disabled) transparent; }

/* P3-D: 标签筛选与展示 */
.tag-filter { display: flex; gap: 6px; padding: 4px 12px 8px; flex-wrap: wrap; }
.tag-chip { padding: 2px 10px; border-radius: var(--sig-radius-button); border: 1px solid var(--border); background: var(--bg-card); color: var(--text-tertiary); font-size: 11px; cursor: pointer; transition: all 0.15s ease; }
.tag-chip:hover { border-color: var(--primary); color: var(--primary); }
.tag-chip.active { background: var(--primary); color: #fff; border-color: var(--primary); }
.session-tag { display: inline-block; padding: 0 6px; border-radius: var(--sig-radius-card); background: var(--bg-hover); color: var(--text-tertiary); font-size: 10px; line-height: 16px; margin-left: 4px; flex-shrink: 0; }
.session-group-label { font-size: 11px; font-weight: 600; color: var(--text-tertiary); padding: 12px 8px 4px; text-transform: uppercase; letter-spacing: 0.5px; }
.session-row {
  display: flex; align-items: center; gap: 4px;
  padding: 0 10px; height: 40px; border-radius: var(--sig-radius-card);
  cursor: pointer; margin-bottom: 1px;
  transition: background 0.15s ease;
  position: relative;
}
.session-row:hover, .session-row.menu-open { background: var(--bg-hover); }
.session-row.active { background: var(--primary-bg); }
.session-row.active::before {
  content: ''; position: absolute; left: 0; top: 8px; bottom: 8px;
  width: 2px; border-radius: 1px; background: var(--primary);
}
.session-info { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 1px; }
.session-title-line { display: flex; align-items: center; gap: 4px; min-width: 0; }
.pin-icon { flex: none; font-size: 11px; color: var(--primary); }
.session-title { font-size: 13px; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; line-height: 18px; }
.session-row.active .session-title { color: var(--primary); font-weight: 600; }
.session-time { font-size: 11px; color: var(--text-muted); line-height: 14px; }
.session-more-btn { opacity: 0; flex-shrink: 0; width: 22px; height: 22px; color: var(--text-muted); }
.session-row:hover .session-more-btn, .session-row.menu-open .session-more-btn { opacity: 1; }
.session-more-btn:hover { color: var(--text-primary); }
.session-menu { min-width: 148px; border-radius: var(--sig-radius-code); padding: 4px; box-shadow: var(--shadow-lg); }
.session-menu :deep(.ant-dropdown-menu-item) { display: flex; align-items: center; gap: 8px; font-size: 13px; border-radius: var(--sig-radius-button); }
.menu-icon { font-size: 14px; }

/* ── 最近活动（/v1/activities，30s 轮询）── */
.panel-activities {
  flex: none; display: flex; flex-direction: column;
  border-top: 1px solid var(--border);
  max-height: 220px;
}
.act-head { display: flex; align-items: center; justify-content: space-between; padding: 8px 12px 4px; }
.act-title { font-size: 11px; color: var(--text-tertiary); }
.act-refresh { font-size: 11px; color: var(--text-tertiary); cursor: pointer; padding: 2px; border-radius: 4px; transition: color 0.15s ease; }
.act-refresh:hover { color: var(--primary); }
.act-list { overflow-y: auto; padding: 0 6px 6px; scrollbar-width: thin; scrollbar-color: var(--text-disabled) transparent; }
.act-row {
  display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 6px 8px; border: none; border-radius: var(--sig-radius-button);
  background: transparent; color: var(--text-secondary);
  font-size: 12px; text-align: left; cursor: pointer;
  transition: background 0.15s ease;
}
.act-row:hover { background: var(--bg-hover); }
.act-dot { flex: none; width: 6px; height: 6px; border-radius: 50%; background: var(--text-disabled); }
.act-dot.completed, .act-dot.success { background: var(--success); }
.act-dot.running, .act-dot.pending { background: var(--primary); }
.act-dot.failed, .act-dot.error { background: var(--danger, #ef4444); }
.act-text { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.act-time { flex: none; font-size: 10px; color: var(--text-muted); }
.act-empty { padding: 10px 12px; font-size: 11px; color: var(--text-muted); }

/* 底部用户信息 */
.panel-foot {
  flex: none; display: flex; align-items: center; gap: 8px;
  padding: 10px 14px; border-top: 1px solid var(--border);
}
.foot-name { font-size: 13px; color: var(--text-secondary); }
</style>
