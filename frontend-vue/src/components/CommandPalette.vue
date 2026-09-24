<script setup lang="ts">
/**
 * Chiron 全局命令面板（Ctrl/Cmd + K）
 * 类似 Linear / VS Code 的全局命令入口
 *  - 静态动作：切换六大工作台、切换主题、打开快速命令（/chat?task=）、查看市场、新建会话
 *  - 远程搜索：GET /v1/search?q=xxx（防抖300ms，输入≥2字符触发）→ 消息 / 媒体结果
 *  - 最近活动：打开时加载 GET /v1/activities?limit=5
 *  - 键盘导航：↑↓选择、Enter 执行、Esc 关闭；鼠标 hover 同步高亮
 *  - 打开期间锁定 body 滚动；组件卸载时移除全局监听并恢复
 */
import { computed, markRaw, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useThemeStore } from '../stores/theme'
import { useAuthStore } from '../stores/auth'
import { api } from '../api'
import EmptyState from './common/EmptyState.vue'
import {
  WORKSTATION_DESCRIPTIONS,
  WORKSTATION_LABELS,
  WORKSTATION_ROUTES,
} from '../types/workstation'
// Ant Design 图标（均为代码库既有使用，避免引入未知导出）
import {
  MessageOutlined,
  RobotOutlined,
  ApartmentOutlined,
  ThunderboltOutlined,
  BookOutlined,
  AppstoreOutlined,
  BulbOutlined,
  ConsoleSqlOutlined,
  PlusOutlined,
  SearchOutlined,
  HistoryOutlined,
  PictureOutlined,
} from '@ant-design/icons-vue'

import { useI18n } from 'vue-i18n'
const { t: tr } = useI18n()
const router = useRouter()
const route = useRoute()
const themeStore = useThemeStore()
const authStore = useAuthStore()

// ── 数据模型：面板内一条可执行条目 ──
interface PaletteEntry {
  id: string
  /** 分组标题：工作台 / 操作 / 最近活动 / 消息 / 媒体 */
  group: string
  label: string
  desc?: string
  /** 本地过滤关键词（空格分隔；与 label/desc 一起参与匹配） */
  keywords: string
  icon?: any
  run: () => void | Promise<void>
}

// ── 静态动作：六大工作台 ──
// 图标 markRaw：避免被 Vue 响应式代理（图标是静态组件，代理会破坏渲染）
// 文案与路由都取自唯一源（src/types/workstation.ts）：此前这里把 /workflow 写成
// 「工作台」，与 AppLayout 的「工作流」同名不同写 —— 同一个入口两个名字。
const workstationActions: PaletteEntry[] = [
  { id: 'ws_chat', group: tr('工作台'), label: WORKSTATION_LABELS.dialogue, desc: WORKSTATION_DESCRIPTIONS.dialogue, keywords: tr('chat 对话 消息 聊天'), icon: markRaw(MessageOutlined), run: () => void router.push(WORKSTATION_ROUTES.dialogue) },
  { id: 'ws_agents', group: tr('工作台'), label: WORKSTATION_LABELS.agent, desc: WORKSTATION_DESCRIPTIONS.agent, keywords: tr('agent 智能体 协同 任务'), icon: markRaw(RobotOutlined), run: () => void router.push(WORKSTATION_ROUTES.agent) },
  { id: 'ws_workflow', group: tr('工作台'), label: WORKSTATION_LABELS.workflow, desc: WORKSTATION_DESCRIPTIONS.workflow, keywords: tr('workflow 工作流 dag 流程 编排'), icon: markRaw(ApartmentOutlined), run: () => void router.push(WORKSTATION_ROUTES.workflow) },
  { id: 'ws_skills', group: tr('工作台'), label: WORKSTATION_LABELS.skill, desc: WORKSTATION_DESCRIPTIONS.skill, keywords: tr('skill 技能 mcp 工具'), icon: markRaw(ThunderboltOutlined), run: () => void router.push(WORKSTATION_ROUTES.skill) },
  { id: 'ws_knowledge', group: tr('工作台'), label: WORKSTATION_LABELS.knowledge, desc: WORKSTATION_DESCRIPTIONS.knowledge, keywords: tr('knowledge 知识 rag 检索 文档'), icon: markRaw(BookOutlined), run: () => void router.push(WORKSTATION_ROUTES.knowledge) },
  { id: 'ws_plugins', group: tr('工作台'), label: WORKSTATION_LABELS.plugin, desc: WORKSTATION_DESCRIPTIONS.plugin, keywords: tr('plugin 插件 扩展'), icon: markRaw(AppstoreOutlined), run: () => void router.push(WORKSTATION_ROUTES.plugin) },
]

// ── 静态动作：通用操作 ──
function openQuickCommand() {
  // 创建统一会话 id，跳转 /chat?task=<id>（ChatView 统一任务模式；会话不存在时提示可直接发送）
  const sessionId = `uni_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
  void router.push({ path: '/chat', query: { task: sessionId } })
}

const utilityActions: PaletteEntry[] = [
  {
    id: 'act_theme',
    group: '操作',
    label: tr('切换主题'),
    desc: '深色 / 浅色模式',
    keywords: 'theme 主题 深色 浅色 暗色 明亮 外观 dark light',
    icon: markRaw(BulbOutlined),
    run: () => themeStore.toggleTheme(),
  },
  {
    id: 'act_quick',
    group: '操作',
    label: tr('打开快速命令'),
    desc: '自然语言任务 · 自动编排六大工作台',
    keywords: 'quick command 快速命令 统一任务 自然语言 执行',
    icon: markRaw(ConsoleSqlOutlined),
    run: openQuickCommand,
  },
  {
    id: 'act_market',
    group: '操作',
    label: tr('查看市场'),
    desc: '技能 / Agent / MCP 市场',
    keywords: 'market 市场 技能市场 安装 浏览',
    icon: markRaw(AppstoreOutlined),
    run: () => void router.push('/skills?tab=market'),
  },
  {
    id: 'act_newchat',
    group: '操作',
    label: tr('新建会话'),
    desc: '开始一段新的对话',
    keywords: 'new chat 新会话 新对话 新建 开始',
    icon: markRaw(PlusOutlined),
    run: () => void router.push('/chat'),
  },
]

// ── 远程状态：搜索 + 最近活动 ──
const open = ref(false)
const query = ref('')
const activeIndex = ref(0)

const searchLoading = ref(false)
const searchError = ref(false)
const remoteEntries = ref<PaletteEntry[]>([])
let searchTimer: number | undefined
let searchSeq = 0

const recentEntries = ref<PaletteEntry[]>([])
const recentLoading = ref(false)

// ── 远程搜索（防抖300ms，≥2字符） ──
async function runSearch(q: string) {
  const seq = ++searchSeq
  try {
    const res = await api.get('/v1/search', { params: { q } })
    if (seq !== searchSeq) return // 丢弃过期响应（新输入已发出）
    // 兼容 { data: {...} } / 直接返回两种包装
    const d = res.data?.data ?? res.data ?? {}
    const messages = Array.isArray(d.messages)
      ? d.messages
      : Array.isArray(d.results?.messages) ? d.results.messages : []
    const media = Array.isArray(d.media)
      ? d.media
      : Array.isArray(d.results?.media) ? d.results.media : []
    remoteEntries.value = [
      ...messages.map((m: any, i: number) => toMessageEntry(m, i)),
      ...media.map((m: any, i: number) => toMediaEntry(m, i)),
    ]
    searchError.value = false
  } catch {
    if (seq === searchSeq) {
      remoteEntries.value = []
      searchError.value = true
    }
  } finally {
    if (seq === searchSeq) searchLoading.value = false
  }
}

function toMessageEntry(m: any, i: number): PaletteEntry {
  const title = m.title || m.content || '消息'
  const summary = m.summary || m.snippet || m.abstract || ''
  const routePath = typeof m.route === 'string' && m.route ? m.route : ''
  const session = m.session_id || m.conversation_id || m.id || i
  return {
    id: `msg_${session}`,
    group: '消息',
    label: truncate(title, 48),
    desc: truncate(summary, 80),
    keywords: '',
    icon: markRaw(MessageOutlined),
    run: () => {
      if (routePath) void router.push(routePath)
      else if (String(m.kind || '').toLowerCase().includes('knowledge')) void router.push('/knowledge')
      else void router.push('/chat')
    },
  }
}

function toMediaEntry(m: any, i: number): PaletteEntry {
  const title = m.title || m.name || '媒体'
  const summary = m.summary || m.description || ''
  const routePath = typeof m.route === 'string' && m.route ? m.route : ''
  const kind = String(m.type || m.kind || '').toLowerCase()
  return {
    id: `med_${m.id || i}`,
    group: '媒体',
    label: truncate(title, 48),
    desc: truncate(summary, 80),
    keywords: '',
    icon: markRaw(PictureOutlined),
    run: () => {
      if (routePath) void router.push(routePath)
      else if (kind.includes('knowledge') || kind.includes('kb') || kind === 'document') void router.push('/knowledge')
      else void router.push('/media')
    },
  }
}

function truncate(s: string, n: number): string {
  const t = String(s || '').trim()
  return t.length > n ? `${t.slice(0, n)}…` : t
}

// ── 最近活动（打开时加载一次） ──
async function loadRecent() {
  recentLoading.value = true
  try {
    const res = await api.get('/v1/activities?limit=5')
    const list = res.data?.activities || []
    recentEntries.value = list.map((a: any) => ({
      id: `act_${a.id || a.workstation || ''}_${a.timestamp || 0}`,
      group: '最近活动',
      label: a.title || tr('暂无标题'),
      desc: a.status_text || '',
      keywords: '',
      icon: markRaw(HistoryOutlined),
      run: () => void router.push(a.route || '/chat'),
    }))
  } catch {
    recentEntries.value = [] // 拉取失败保留静态动作，不阻塞面板
  } finally {
    recentLoading.value = false
  }
}

// ── 过滤：空输入 = 最近活动 + 快捷动作；有输入 = 本地静态过滤 + 远程结果 ──
const filtered = computed<PaletteEntry[]>(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return [...recentEntries.value, ...workstationActions, ...utilityActions]
  const local = [...workstationActions, ...utilityActions].filter((e) =>
    `${e.label} ${e.desc || ''} ${e.keywords}`.toLowerCase().includes(q),
  )
  return [...local, ...remoteEntries.value]
})

// ── 分组渲染行（连续分组合并为单个标题） ──
interface Row {
  kind: 'header' | 'item'
  key: string
  label?: string
  entry?: PaletteEntry
  index?: number
}

const rows = computed<Row[]>(() => {
  const out: Row[] = []
  let lastGroup = ''
  let idx = 0
  for (const e of filtered.value) {
    if (e.group !== lastGroup) {
      out.push({ kind: 'header', key: `h-${e.group}`, label: e.group })
      lastGroup = e.group
    }
    out.push({ kind: 'item', key: e.id, entry: e, index: idx })
    idx++
  }
  return out
})

function onRowEnter(row: Row) {
  if (row.kind === 'item' && row.index !== undefined) activeIndex.value = row.index
}

function onRowClick(row: Row) {
  if (row.kind === 'item' && row.entry) runEntry(row.entry)
}

// ── 键盘导航 ──
function move(delta: number) {
  const len = filtered.value.length
  if (len === 0) return
  activeIndex.value = (activeIndex.value + delta + len) % len
}

function runEntry(entry: PaletteEntry) {
  closePalette()
  void entry.run()
}

function runActive() {
  const entry = filtered.value[activeIndex.value]
  if (entry) runEntry(entry)
}

// ── 开关 / 滚动锁定 / 卸载清理 ──
const inputEl = ref<HTMLInputElement | null>(null)
const listEl = ref<HTMLElement | null>(null)

function openPalette() {
  open.value = true
  query.value = ''
  remoteEntries.value = []
  searchError.value = false
  searchLoading.value = false
  activeIndex.value = 0
  document.body.style.overflow = 'hidden'
  if (authStore.token) void loadRecent()
  else recentEntries.value = []
  void nextTick(() => inputEl.value?.focus())
}

function closePalette() {
  if (!open.value) return
  open.value = false
  document.body.style.overflow = ''
  query.value = ''
}

function togglePalette() {
  if (open.value) closePalette()
  else openPalette()
}

// 输入框内按键
function onPaletteKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.preventDefault()
    closePalette()
  } else if (e.key === 'ArrowDown') {
    e.preventDefault()
    move(1)
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    move(-1)
  } else if (e.key === 'Enter') {
    e.preventDefault()
    runActive()
  }
}

// 全局 Ctrl/Cmd+K（preventDefault 屏蔽浏览器行为）；打开后 Esc 也可关闭
function onGlobalKeydown(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
    e.preventDefault()
    togglePalette()
    return
  }
  if (e.key === 'Escape' && open.value) {
    e.preventDefault()
    closePalette()
  }
}

// 输入防抖触发远程搜索
watch(query, (q) => {
  activeIndex.value = 0
  if (searchTimer !== undefined) window.clearTimeout(searchTimer)
  const trimmed = q.trim()
  if (trimmed.length < 2) {
    remoteEntries.value = []
    searchLoading.value = false
    searchError.value = false
    return
  }
  if (!authStore.token) {
    // 未登录：不发起需要鉴权的搜索
    remoteEntries.value = []
    searchLoading.value = false
    return
  }
  searchLoading.value = true
  searchTimer = window.setTimeout(() => { void runSearch(trimmed) }, 300)
})

// 结果变化时钳制高亮索引
watch(filtered, () => {
  const len = filtered.value.length
  if (len === 0) activeIndex.value = 0
  else if (activeIndex.value >= len) activeIndex.value = len - 1
})

// 高亮项滚动可见（列表容器 nearest）
watch(activeIndex, () => {
  void nextTick(() => {
    listEl.value?.querySelector('.palette-item.active')?.scrollIntoView({ block: 'nearest' })
  })
})

// 路由变化（浏览器前进后退等）时收起面板
watch(() => route.path, () => {
  if (open.value) closePalette()
})

onMounted(() => {
  window.addEventListener('keydown', onGlobalKeydown)
})

onUnmounted(() => {
  window.removeEventListener('keydown', onGlobalKeydown)
  if (searchTimer !== undefined) window.clearTimeout(searchTimer)
  document.body.style.overflow = ''
})
</script>

<template>
  <Teleport to="body">
    <Transition name="palette">
      <div
        v-if="open"
        class="palette-overlay"
        @click.self="closePalette"
      >
        <div
          class="palette-panel"
          role="dialog"
          aria-modal="true"
          aria-label="全局命令面板"
          @keydown="onPaletteKeydown"
        >
          <!-- 输入行 -->
          <div class="palette-input-row">
            <SearchOutlined class="palette-search-icon" />
            <input
              ref="inputEl"
              v-model="query"
              type="text"
              class="palette-input"
              :placeholder="$t('搜索消息、媒体，或输入命令...')"
              autocomplete="off"
              spellcheck="false"
            >
            <kbd class="palette-kbd">Esc</kbd>
          </div>

          <!-- 结果列表 -->
          <div
            ref="listEl"
            class="palette-body"
            role="listbox"
          >
            <template v-if="rows.length > 0">
              <div
                v-for="row in rows"
                :key="row.key"
                class="palette-row"
              >
                <div
                  v-if="row.kind === 'header'"
                  class="palette-group"
                  role="presentation"
                >
                  {{ row.label }}
                </div>
                <div
                  v-else
                  class="palette-item"
                  :class="{ active: row.index === activeIndex }"
                  role="option"
                  :aria-selected="row.index === activeIndex"
                  @mouseenter="onRowEnter(row)"
                  @click="onRowClick(row)"
                >
                  <span class="palette-item-icon">
                    <component :is="row.entry?.icon || SearchOutlined" />
                  </span>
                  <span class="palette-item-main">
                    <span class="palette-item-label">{{ row.entry?.label }}</span>
                    <span
                      v-if="row.entry?.desc"
                      class="palette-item-desc"
                    >{{ row.entry?.desc }}</span>
                  </span>
                  <span class="palette-item-go">↵</span>
                </div>
              </div>
            </template>

            <!-- 搜索中 -->
            <div
              v-else-if="searchLoading"
              class="palette-status"
            >
              <span class="palette-status-spinner" />
              <span>{{ $t('正在搜索...') }}</span>
            </div>

            <!-- 搜索失败 -->
            <div
              v-else-if="searchError"
              class="palette-status"
            >
              {{ $t('搜索失败，请稍后重试') }}
            </div>

            <!-- 最近活动加载中（空输入、活动未就绪） -->
            <div
              v-else-if="recentLoading"
              class="palette-status"
            >
              <span class="palette-status-spinner" />
              <span>{{ $t('正在加载最近活动...') }}</span>
            </div>

            <!-- 无结果 -->
            <EmptyState
              v-else-if="query.trim().length >= 2"
              size="list"
              :description="$t('未找到匹配结果')"
              :hint="$t('试试更短的关键词，或直接用 Enter 用本地命令执行')"
            />
          </div>

          <!-- 底部快捷键提示 -->
          <div class="palette-footer">
            <span><kbd>↑</kbd><kbd>↓</kbd> {{ $t('选择') }}</span>
            <span><kbd>Enter</kbd> {{ $t('执行') }}</span>
            <span><kbd>Esc</kbd> {{ $t('关闭') }}</span>
            <span class="palette-footer-tip">{{ $t('Ctrl/Cmd + K 随时唤起') }}</span>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
/* ── 遮罩：覆盖全屏，点击空白关闭 ── */
.palette-overlay {
  position: fixed;
  inset: 0;
  z-index: var(--z-palette);
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding: min(14vh, 120px) 12px 24px;
  background: var(--bg-overlay);
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
}

/* ── 面板：玻璃拟态，居中顶部 ── */
.palette-panel {
  width: min(620px, 100%);
  max-height: min(560px, calc(100vh - 160px));
  display: flex;
  flex-direction: column;
  border-radius: 14px;
  overflow: hidden;
  /* 原为 var(--menu-bg) —— 该变量**从未定义**且无 fallback，背景实际会失效。
     语义是「毛玻璃面板底」，改用 --bg-surface（多个主题下本身就是半透明，
     与下面的 backdrop-filter 配套）。 */
  background: var(--bg-surface);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  border: 1px solid var(--border);
  box-shadow: var(--shadow-lg);
}

/* ── 输入行 ── */
.palette-input-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
  flex: none;
}
.palette-search-icon {
  font-size: 16px;
  color: var(--text-tertiary);
  flex: none;
}
.palette-input {
  flex: 1;
  min-width: 0;
  border: none;
  outline: none;
  background: transparent;
  color: var(--text-primary);
  font-size: 15px;
  font-family: inherit;
}
.palette-input::placeholder { color: var(--text-muted); }
.palette-kbd {
  flex: none;
  padding: 2px 7px;
  border-radius: 5px;
  border: 1px solid var(--border);
  background: var(--bg-secondary);
  color: var(--text-tertiary);
  font-size: 11px;
  font-family: var(--font-mono);
}

/* ── 结果列表 ── */
.palette-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 8px;
  overscroll-behavior: contain;
}

.palette-group {
  padding: 8px 10px 4px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-tertiary);
}

.palette-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 10px;
  border-radius: 8px;
  cursor: pointer;
  transition: background var(--dur-fast) ease;
}
.palette-item:hover { background: var(--bg-hover); }
.palette-item.active {
  background: var(--primary-bg);
  box-shadow: inset 2px 0 0 var(--primary);
}
.palette-item-icon {
  flex: none;
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 7px;
  background: var(--bg-secondary);
  color: var(--text-secondary);
  font-size: 14px;
}
.palette-item.active .palette-item-icon {
  background: var(--primary);
  color: var(--on-solid);
}
.palette-item-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.palette-item-label {
  font-size: 13.5px;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.palette-item-desc {
  font-size: 12px;
  color: var(--text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.palette-item-go {
  flex: none;
  font-size: 12px;
  color: var(--text-muted);
  opacity: 0;
  transition: opacity var(--dur-fast) ease;
}
.palette-item.active .palette-item-go { opacity: 1; color: var(--primary); }

/* ── 状态行（搜索中 / 失败 / 加载中） ── */
.palette-status {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 36px 16px;
  font-size: 13px;
  color: var(--text-tertiary);
}
.palette-status-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid var(--border);
  border-top-color: var(--primary);
  border-radius: 50%;
  animation: paletteSpin var(--dur-spin) linear infinite;
}
@keyframes paletteSpin { to { transform: rotate(360deg); } }

/* ── 底部提示 ── */
.palette-footer {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 9px 16px;
  border-top: 1px solid var(--border);
  background: var(--bg-secondary);
  color: var(--text-tertiary);
  font-size: 11px;
  flex: none;
}
.palette-footer kbd {
  padding: 1px 5px;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--bg-card);
  color: var(--text-secondary);
  font-family: var(--font-mono);
  font-size: 10px;
}
.palette-footer-tip {
  margin-left: auto;
  color: var(--text-muted);
}

/* ── 开合过渡 ── */
.palette-enter-active,
.palette-leave-active {
  transition: opacity var(--dur-normal) ease;
}
.palette-enter-active .palette-panel,
.palette-leave-active .palette-panel {
  transition: transform var(--dur-normal) ease, opacity var(--dur-normal) ease;
}
.palette-enter-from,
.palette-leave-to {
  opacity: 0;
}
.palette-enter-from .palette-panel,
.palette-leave-to .palette-panel {
  opacity: 0;
  transform: translateY(-8px) scale(0.985);
}

/* ── 移动端：面板贴近顶部、收敛留白 ── */
@media (max-width: 768px) {
  .palette-overlay {
    padding: 8vh 10px 16px;
  }
  .palette-panel {
    max-height: calc(100vh - 16vh - 32px);
  }
  .palette-footer-tip { display: none; }
}

@media (prefers-reduced-motion: reduce) {
  .palette-enter-active,
  .palette-leave-active,
  .palette-enter-active .palette-panel,
  .palette-leave-active .palette-panel,
  .palette-item {
    transition: none;
  }
}
</style>