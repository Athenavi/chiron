<script setup lang="ts">
/**
 * 会话地图外壳。
 *
 * 这是旧 iframe 浮层的替代品：同一 SPA 内的一屏，没有 postMessage、没有 RPC 回执、
 * 没有 20 秒超时，也不需要把 token 塞进 URL（axios 实例已带鉴权头）。
 *
 * ## 数据流
 *
 * ```
 * useSessionMapWorkspace   ← /v1/session-map/workspaces（整体读写 + 800ms 防抖）
 * buildMapGraph            ← 节点 + 会话元数据 → 卡片/连线（纯函数）
 * useMapActions            ← 新建 / 追问 / 分叉 / 元数据，直接 await 宿主 REST
 * ```
 *
 * 会话的**主人始终是宿主**（`ChatView`）：这里只按 id 引用。新建/切换会话后回传
 * `switch-session`，否则用户在地图里建的会话，回到对话页侧栏里找不到。
 */
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import { getChatSessionMessages } from '../../api'
import type { ChatSession } from '../../components/chat/chat-types'
import CardInspector from './components/CardInspector.vue'
import DraftComposer from './components/DraftComposer.vue'
import ImportPicker from './components/ImportPicker.vue'
import MapCanvas from './components/MapCanvas.vue'
import ThreadDetail from './components/ThreadDetail.vue'
import { requestForkNode } from './api'
import { createSession, forkSession, sendMessage, type MapActionDeps } from './useMapActions'
import { ancestorsToExpand, buildMapGraph, findCardBySession } from './useSessionMapGraph'
import { layoutNodes } from './useMapLayout'
import { useSessionMapWorkspace } from './useSessionMapWorkspace'

const props = defineProps<{
  /** 宿主已加载的会话列表（地图不重复拉一份，避免两处列表不一致） */
  sessions: readonly ChatSession[]
  activeSessionId?: string
  runningSessionIds?: readonly string[]
  /** 宿主的请求级配置（模型/工具授权…），由这里注入而不是地图自己重建 */
  buildLlmConfig?: () => Record<string, unknown>
}>()

const emit = defineEmits<{
  close: []
  /** 地图里新建/切换了会话 → 宿主刷新列表并切过去 */
  'switch-session': [sessionId: string]
  /** 卡片元数据改过 → 宿主刷新会话列表（标题/别名/标签都在那上面） */
  'refresh-sessions': []
}>()

const { t } = useI18n()

const deps: MapActionDeps = { buildLlmConfig: props.buildLlmConfig }

// 解构而不是整体持有 `ws`：模板只对**顶层 ref** 自动解包，嵌套在对象里的 ref
// 会原样输出成 { value: ... }，`ws.activeId.value` 这种写法在模板里既丑又易错。
const {
  workspaces,
  activeId,
  nodes,
  loading,
  loadError,
  saveError,
  load,
  select,
  removeActive,
  addNode,
  dropNode,
  dragNode,
  collapseNode,
  replaceNodes,
} = useSessionMapWorkspace()

const selectedCardId = ref('')
const detailCardId = ref('')
const followupCardId = ref('')
const followupText = ref('')
const draftOpen = ref(false)
const draftBusy = ref(false)
const draftError = ref('')
const importOpen = ref(false)
const importBusy = ref(false)
const actionError = ref('')
const deleteArmed = ref(false)
let deleteTimer: ReturnType<typeof setTimeout> | null = null

/** 会话 id → 元数据（投影层要的扁平表，避免每张卡线性查找）。 */
const sessionIndex = computed(() => new Map(props.sessions.map(s => [s.id, s])))

/** 折叠根：后端持久化的 `collapsed` 是唯一真相，投影层不自己读节点。 */
const collapsedIds = computed(() => new Set(nodes.value.filter(n => n.collapsed).map(n => n.id)))

const runningIds = computed(() => new Set(props.runningSessionIds ?? []))

const graph = computed(() =>
  buildMapGraph(nodes.value, {
    sessions: sessionIndex.value,
    collapsed: collapsedIds.value,
    running: runningIds.value,
  }),
)

const selectedCard = computed(() => graph.value.cards.find(c => c.id === selectedCardId.value) ?? null)
const detailCard = computed(() => graph.value.cards.find(c => c.id === detailCardId.value) ?? null)
const followupCard = computed(() => graph.value.cards.find(c => c.id === followupCardId.value) ?? null)
const existingSessionIds = computed(() => nodes.value.map(n => n.session_id ?? '').filter(Boolean))

void load()

// 换画布后，选中的卡片可能已经不在了 —— 留着会让侧栏显示上一张画布的会话
watch(graph, (g) => {
  const ids = new Set(g.cards.map(c => c.id))
  if (selectedCardId.value && !ids.has(selectedCardId.value)) selectedCardId.value = ''
  if (detailCardId.value && !ids.has(detailCardId.value)) detailCardId.value = ''
  if (followupCardId.value && !ids.has(followupCardId.value)) followupCardId.value = ''
})

function openDetail(cardId: string) {
  detailCardId.value = cardId
  selectedCardId.value = cardId
}

function startFollowup(cardId: string) {
  const card = graph.value.cards.find(c => c.id === cardId)
  if (!card?.hasSession) return
  followupCardId.value = cardId
  followupText.value = ''
}

async function submitFollowup() {
  const card = followupCard.value
  const text = followupText.value.trim()
  if (!card?.sessionId || !text) return
  actionError.value = ''
  try {
    // `followup: true` 会套上协议前缀（老会话依赖同一文本格式）
    await sendMessage(card.sessionId, text, { followup: true, deps })
    followupText.value = ''
    followupCardId.value = ''
    emit('switch-session', card.sessionId)
  } catch {
    actionError.value = t('chat.sessionmap.serviceFailed')
  }
}

/**
 * 该会话当前有多少条消息 —— 分叉点的语义就是这个数。
 *
 * 后端 `session_fork.go:33-35` 把 `from_index` 定义为「保留前多少条消息（含第
 * `from_index` 条）」，`ForkSession` 走 `Mode: "truncate"`。所以"从这张卡片分叉"
 * = 复制到该会话的最后一条。用图上的子节点数或节点自身的 `branch_from_seq` 都不对：
 * 前者是画布层级，后者是**本节点当初从父会话分出来的位置**，两者与消息条数无关。
 */
async function messageCount(sessionId: string): Promise<number> {
  try {
    const res = await getChatSessionMessages(sessionId)
    // 该接口返回**未拆包**的 `data`，两种形状都见过（见 ThreadDetail 的同款兜底）
    const payload = res && typeof res === 'object' && 'messages' in res ? res : (res as { data?: { messages?: unknown[] } })?.data
    const list = (payload as { messages?: unknown[] } | undefined)?.messages
    return Array.isArray(list) && list.length > 0 ? list.length : 1
  } catch {
    // 拿不到条数时不阻断分叉：1 是后端接受的最小合法值
    return 1
  }
}

/** 分叉：先算分叉点 → 问后端接回地图的格式 → 真复制 → 新节点挂到父节点下并落库。 */
async function forkFrom(cardId: string) {
  const card = graph.value.cards.find(c => c.id === cardId)
  if (!card?.sessionId || !activeId.value) return
  actionError.value = ''
  try {
    const fromIndex = await messageCount(card.sessionId)
    const hint = await requestForkNode(activeId.value, { node_id: card.id, from_index: fromIndex })
    const created = await forkSession(card.sessionId, hint?.from_index ?? fromIndex, card.title)
    addNode({
      sessionId: created.id,
      title: created.title,
      parentNodeId: card.id,
      branchFromSeq: hint?.from_index ?? fromIndex,
    })
    emit('switch-session', created.id)
  } catch {
    actionError.value = t('chat.sessionmap.serviceFailed')
  }
}

/** 从画布移除（连带子节点）。会话记录本身保留 —— 这一步只删节点。 */
function archive(cardId: string) {
  dropNode(cardId)
  if (selectedCardId.value === cardId) selectedCardId.value = ''
  if (detailCardId.value === cardId) detailCardId.value = ''
}

function moveCard(payload: { id: string; x: number; y: number }) {
  dragNode(payload.id, { x: payload.x, y: payload.y })
}

function toggleCollapse(cardId: string) {
  collapseNode(cardId)
}

async function submitDraft(payload: { title: string; text: string }) {
  draftBusy.value = true
  draftError.value = ''
  try {
    const created = await createSession(payload.title, deps, t('chat.sessionmap.newSession'))
    await sendMessage(created.id, payload.text, { deps })
    addNode({ sessionId: created.id, title: created.title })
    draftOpen.value = false
    emit('switch-session', created.id)
  } catch {
    draftError.value = t('chat.sessionmap.serviceFailed')
  } finally {
    draftBusy.value = false
  }
}

function confirmImport(sessionIds: string[]) {
  actionError.value = ''
  for (const id of sessionIds) {
    addNode({ sessionId: id, title: sessionIndex.value.get(id)?.title ?? '' })
  }
  importOpen.value = false
}

/** 整理布局：把深度映射成列。只在用户点按钮时执行，不覆盖手摆的位置。 */
function tidy() {
  replaceNodes(layoutNodes(nodes.value))
}

/**
 * 定位当前会话：先把它的折叠祖先全部展开，否则"定位"到的是一张看不见的卡片。
 * 一次性替换节点数组，避免逐个折叠触发多次防抖保存。
 */
function focusActive() {
  const sid = props.activeSessionId ?? ''
  if (!sid) return
  const card = findCardBySession(graph.value.cards, sid)
  const targetId = card?.id ?? nodes.value.find(n => n.session_id === sid)?.id
  if (!targetId) return
  const expand = new Set(ancestorsToExpand(nodes.value, targetId))
  if (expand.size > 0) {
    replaceNodes(nodes.value.map(n => (expand.has(n.id) ? { ...n, collapsed: false } : n)))
  }
  selectedCardId.value = targetId
}

/** 删除画布要二次确认；3 秒不点就自己复位，避免一直停在"待确认"状态。 */
async function armDelete() {
  if (!deleteArmed.value) {
    deleteArmed.value = true
    deleteTimer = setTimeout(() => {
      deleteArmed.value = false
      deleteTimer = null
    }, 3000)
    return
  }
  if (deleteTimer) clearTimeout(deleteTimer)
  deleteTimer = null
  deleteArmed.value = false
  await removeActive()
}
</script>

<template>
  <div class="map-view">
    <header class="map-view-bar">
      <h2 class="map-view-heading">
        {{ t('chat.session_map') }}
      </h2>

      <label
        class="map-view-label"
        for="map-workspace"
      >{{ t('chat.sessionmap.workspace') }}</label>
      <select
        id="map-workspace"
        class="map-view-select"
        :value="activeId"
        @change="select(($event.target as HTMLSelectElement).value)"
      >
        <option
          v-for="item in workspaces"
          :key="item.id"
          :value="item.id"
        >
          {{ item.name }}
        </option>
      </select>

      <div class="map-view-actions is-trailing">
        <button
          type="button"
          class="map-view-btn"
          @click="tidy"
        >
          {{ t('chat.sessionmap.layout') }}
        </button>
        <button
          type="button"
          class="map-view-btn"
          :disabled="!activeSessionId"
          @click="focusActive"
        >
          {{ t('chat.sessionmap.focusActive') }}
        </button>
        <button
          type="button"
          class="map-view-btn"
          @click="importOpen = true"
        >
          {{ t('chat.sessionmap.import') }}
        </button>
        <button
          type="button"
          class="map-view-btn"
          @click="draftOpen = true"
        >
          {{ t('chat.sessionmap.draft') }}
        </button>
        <button
          type="button"
          class="map-view-btn"
          @click="load()"
        >
          {{ t('chat.sessionmap.refreshCanvas') }}
        </button>
        <button
          type="button"
          class="map-view-btn"
          :class="{ 'is-danger': deleteArmed }"
          @click="armDelete"
        >
          {{ deleteArmed ? t('chat.sessionmap.deleteCanvasConfirm') : t('chat.sessionmap.deleteCanvas') }}
        </button>
        <button
          type="button"
          class="map-view-btn"
          @click="emit('close')"
        >
          {{ t('chat.sessionmap.close') }}
        </button>
      </div>
    </header>

    <p
      v-if="actionError || saveError || loadError"
      class="map-view-error"
      role="alert"
    >
      {{ actionError || saveError || loadError }}
    </p>

    <div class="map-view-body">
      <div class="map-view-canvas">
        <MapCanvas
          :cards="graph.cards"
          :edges="graph.edges"
          :active-id="selectedCardId"
          @move-card="moveCard"
          @select-card="selectedCardId = $event"
          @toggle-collapse="toggleCollapse"
          @open-detail="openDetail"
          @fork="forkFrom"
          @followup="startFollowup"
        />
        <p
          v-if="loading"
          class="map-view-note"
        >
          {{ t('chat.sessionmap.loading') }}
        </p>
      </div>

      <aside class="map-view-side">
        <DraftComposer
          v-if="draftOpen"
          :busy="draftBusy"
          :error="draftError"
          @submit="submitDraft"
          @cancel="draftOpen = false"
        />

        <ImportPicker
          v-if="importOpen"
          :sessions="sessions"
          :existing="existingSessionIds"
          :busy="importBusy"
          @import="confirmImport"
          @cancel="importOpen = false"
        />

        <form
          v-if="followupCard"
          class="map-view-followup"
          @submit.prevent="submitFollowup"
        >
          <label
            class="map-view-label"
            for="map-followup-text"
          >{{ t('chat.sessionmap.followupHere') }}</label>
          <textarea
            id="map-followup-text"
            v-model="followupText"
            class="map-view-textarea"
            rows="3"
            :placeholder="t('chat.sessionmap.followupPlaceholder')"
          />
          <div class="map-view-actions">
            <button
              type="submit"
              class="map-view-btn is-primary"
              :disabled="!followupText.trim()"
            >
              {{ t('chat.sessionmap.followupSend') }}
            </button>
            <button
              type="button"
              class="map-view-btn"
              @click="followupCardId = ''"
            >
              {{ t('chat.sessionmap.cancel') }}
            </button>
          </div>
        </form>

        <ThreadDetail
          v-if="detailCard?.sessionId"
          :session-id="detailCard.sessionId"
          :title="detailCard.title"
          @close="detailCardId = ''"
        />

        <CardInspector
          v-if="selectedCard"
          :card="selectedCard"
          editable
          @close="selectedCardId = ''"
          @followup="startFollowup(selectedCard.id)"
          @fork="forkFrom(selectedCard.id)"
          @meta-updated="emit('refresh-sessions')"
        />

        <button
          v-if="selectedCard"
          type="button"
          class="map-view-btn is-danger"
          @click="archive(selectedCard.id)"
        >
          {{ t('chat.sessionmap.archive') }}
        </button>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.map-view {
  display: flex;
  flex-direction: column;
  block-size: 100%;
  min-block-size: 0;
  background: var(--bg-page);
  color: var(--text-primary);
}

.map-view-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-block-end: var(--hairline-width) solid var(--border-default);
  background: var(--bg-card);
}

.map-view-heading {
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
}

.map-view-label {
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}

.map-view-select {
  padding: 2px 6px;
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-base);
  color: var(--text-primary);
  font-size: var(--fs-xs);
}

.map-view-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

/* 把动作组推到行尾；RTL 下自动镜像，不需要写 right/left */
.map-view-actions.is-trailing {
  margin-inline-start: auto;
}

.map-view-btn {
  padding: 3px 10px;
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-secondary);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  cursor: pointer;
  transition: background var(--dur-fast), color var(--dur-fast);
}

.map-view-btn:hover:not(:disabled) {
  background: var(--bg-card);
  color: var(--primary);
  border-color: var(--primary);
}

.map-view-btn.is-primary {
  border-color: var(--primary);
  background: var(--primary);
  color: var(--bg-card);
}

.map-view-btn.is-danger {
  border-color: var(--warning);
  color: var(--warning);
}

.map-view-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.map-view-btn:focus-visible,
.map-view-select:focus-visible,
.map-view-textarea:focus-visible {
  outline: 2px solid var(--border-focus);
  outline-offset: 1px;
}

.map-view-error {
  margin: 0;
  padding: 6px 12px;
  background: var(--bg-secondary);
  color: var(--warning);
  font-size: var(--fs-xs);
}

.map-view-body {
  display: flex;
  flex: 1;
  gap: 12px;
  min-block-size: 0;
  padding: 12px;
}

.map-view-canvas {
  position: relative;
  flex: 1;
  min-inline-size: 0;
  min-block-size: 0;
}

.map-view-side {
  display: flex;
  flex-direction: column;
  gap: 10px;
  inline-size: 320px;
  min-block-size: 0;
  overflow-y: auto;
}

.map-view-followup {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
}

.map-view-textarea {
  padding: 4px 8px;
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-base);
  color: var(--text-primary);
  font-family: inherit;
  font-size: var(--fs-sm);
  resize: vertical;
}

.map-view-note {
  position: absolute;
  inset-block-start: 50%;
  inset-inline-start: 50%;
  transform: translate(-50%, -50%);
  margin: 0;
  color: var(--text-tertiary);
  font-size: var(--fs-sm);
  pointer-events: none;
}

/* 窄屏：侧栏落到画布下方并限高，画布保底 440px（与 WorkflowView 一致） */
@media (max-width: 768px) {
  .map-view-body {
    flex-direction: column;
  }

  .map-view-canvas {
    min-block-size: 440px;
  }

  .map-view-side {
    inline-size: auto;
    max-block-size: 50vh;
  }
}
</style>
