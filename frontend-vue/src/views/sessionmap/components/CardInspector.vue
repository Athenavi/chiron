<script setup lang="ts">
/**
 * 侧栏 inspector：一张卡片的会话信息与操作。
 *
 * 对等旧实现（`app.js` 的 inspector 区域）的三块内容：轮次/工具/错误统计、
 * 元数据编辑（别名 / 标签）、继续追问与创建分支。
 *
 * ## 为什么统计在这里算，而不是让后端出一个接口
 *
 * 后端已有 `GET /v1/sessions/{id}/metrics`，但它的 `totals` 只有 tokens / 费用 /
 * 缓存命中 —— **没有工具数与错误数**。为这两个数字新增一个聚合端点，等于让后端
 * 多背一份"地图上的统计口径"；而这两项本来就是消息内容的直接投影，前端从同一份
 * 消息里数出来更准，也不会与详情面板的数字打架。
 *
 * 数据量上有界：`/v1/chat/sessions/{id}/messages` 默认只返回最近一页（50 条），
 * 统计的是"这一页的轮次"，与用户真正在看的内容一致。
 */
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import { getChatSessionMessages } from '../../../api'
import { updateSessionMeta } from '../useMapActions'
import type { MapCard } from '../useSessionMapGraph'

const props = defineProps<{
  card: MapCard
  /** 只读模式（分享视图）下不显示任何写操作 */
  editable?: boolean
}>()

const emit = defineEmits<{
  close: []
  followup: []
  fork: []
  /** 元数据保存成功：父组件据此刷新卡片标题/标签，避免等下一次轮询 */
  'meta-updated': [patch: { alias: string; tag: string }]
}>()

const { t } = useI18n()

/** 消息的形状只需要这三项 —— 详情面板要更多字段，见 `ThreadDetail.vue`。 */
interface RawMessage {
  role?: string
  content?: string
  tool_calls?: string
}

const loading = ref(false)
const failed = ref(false)
const messages = ref<RawMessage[]>([])

/**
 * 读消息。
 *
 * 响应有两种形状：直接 `{messages}`，或再包一层 `{data:{messages}}`
 * （网关与引擎的包装不一致，`ChatView.vue:1221` 同样两态兼容）。
 */
async function load() {
  const sessionId = props.card.sessionId
  if (!sessionId) {
    messages.value = []
    failed.value = false
    return
  }
  loading.value = true
  failed.value = false
  try {
    const res = await getChatSessionMessages(sessionId)
    const payload = (res?.messages ? res : (res?.data ?? {})) as { messages?: unknown[] }
    messages.value = Array.isArray(payload.messages) ? (payload.messages as RawMessage[]) : []
  } catch {
    // 统计读不到不该让整个面板消失：降级成"没有数据"，编辑功能仍然可用
    failed.value = true
    messages.value = []
  } finally {
    loading.value = false
  }
}

watch(() => props.card.sessionId, load, { immediate: true })

/**
 * `tool_calls` 是 OpenAI 格式的 JSON **字符串**（`model.Message.ToolCalls` 是 text 列）。
 *
 * 解析失败一律计 0：历史数据里可能存着非 JSON 的旧格式，为它抛错会让整块面板白屏，
 * 而这只是一个数字。
 */
function countToolCalls(raw?: string): number {
  if (!raw) return 0
  try {
    const parsed: unknown = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.length : 0
  } catch {
    return 0
  }
}

const stats = computed(() => {
  const list = messages.value
  let turns = 0
  let tools = 0
  for (const m of list) {
    if (m.role === 'user') turns += 1
    tools += countToolCalls(m.tool_calls)
  }
  return { turns, tools, messages: list.length }
})

// ── 元数据编辑 ──
// 本地草稿初始值跟着卡片走：切换选中卡片时必须重置，否则会把上一张卡的别名
// 显示到这一张上（旧实现整页重渲染，天然没有这个坑）。
const aliasDraft = ref('')
const tagDraft = ref('')
const saving = ref(false)
const saveError = ref('')

watch(
  () => [props.card.id, props.card.alias, props.card.tag] as const,
  () => {
    aliasDraft.value = props.card.alias ?? ''
    tagDraft.value = props.card.tag ?? ''
    saveError.value = ''
  },
  { immediate: true },
)

const dirty = computed(
  () => aliasDraft.value !== (props.card.alias ?? '') || tagDraft.value !== (props.card.tag ?? ''),
)

async function save() {
  const sessionId = props.card.sessionId
  if (!sessionId || !dirty.value || saving.value) return
  saving.value = true
  saveError.value = ''
  try {
    const alias = aliasDraft.value.trim()
    const tag = tagDraft.value.trim()
    await updateSessionMeta(sessionId, { alias, tag })
    emit('meta-updated', { alias, tag })
  } catch {
    // 失败必须留在面板里可见：静默失败会让人以为"存上了"，下次打开又变回旧值
    saveError.value = t('chat.sessionmap.serviceFailed')
  } finally {
    saving.value = false
  }
}

/** 分叉点：优先用卡片上的血缘标记，退回落到最后一条用户消息。 */
const forkPoint = computed(() => {
  if (props.card.branchFromSeq > 0) return props.card.branchFromSeq
  return stats.value.turns
})
</script>

<template>
  <aside
    class="map-inspector"
    :aria-label="t('chat.sessionmap.inspector')"
  >
    <header class="map-inspector-head">
      <h3 class="map-inspector-title">
        {{ card.title || t('chat.sessionmap.untitled') }}
      </h3>
      <button
        type="button"
        class="map-inspector-close"
        :aria-label="t('chat.sessionmap.cancel')"
        @click="emit('close')"
      >
        <span aria-hidden="true">×</span>
      </button>
    </header>

    <p
      v-if="failed"
      class="map-inspector-note"
    >
      {{ t('chat.sessionmap.serviceFailed') }}
    </p>

    <dl class="map-inspector-stats">
      <div class="map-inspector-stat">
        <dt>{{ t('chat.sessionmap.turns') }}</dt>
        <dd>{{ stats.turns }}</dd>
      </div>
      <div class="map-inspector-stat">
        <dt>{{ t('chat.sessionmap.tools') }}</dt>
        <dd>{{ stats.tools }}</dd>
      </div>
      <div class="map-inspector-stat">
        <dt>{{ t('chat.sessionmap.messages') }}</dt>
        <dd>{{ stats.messages }}</dd>
      </div>
    </dl>

    <p
      v-if="loading"
      class="map-inspector-note"
    >
      {{ t('chat.sessionmap.loading') }}
    </p>

    <template v-if="editable && card.hasSession">
      <div class="map-inspector-field">
        <label
          class="map-inspector-label"
          for="map-inspector-alias"
        >{{ t('chat.sessionmap.alias') }}</label>
        <input
          id="map-inspector-alias"
          v-model="aliasDraft"
          class="map-inspector-input"
          type="text"
          maxlength="64"
          :placeholder="t('chat.sessionmap.aliasPlaceholder')"
        >
      </div>

      <div class="map-inspector-field">
        <label
          class="map-inspector-label"
          for="map-inspector-tag"
        >{{ t('chat.sessionmap.tag') }}</label>
        <input
          id="map-inspector-tag"
          v-model="tagDraft"
          class="map-inspector-input"
          type="text"
          maxlength="64"
          :placeholder="t('chat.sessionmap.tagPlaceholder')"
        >
      </div>

      <p
        v-if="saveError"
        class="map-inspector-error"
        role="alert"
      >
        {{ saveError }}
      </p>

      <div class="map-inspector-actions">
        <button
          type="button"
          class="map-inspector-btn is-primary"
          :disabled="!dirty || saving"
          @click="save"
        >
          {{ t('chat.sessionmap.save') }}
        </button>
      </div>

      <div class="map-inspector-actions">
        <button
          type="button"
          class="map-inspector-btn"
          @click="emit('followup')"
        >
          {{ t('chat.sessionmap.followupHere') }}
        </button>
        <button
          type="button"
          class="map-inspector-btn"
          :disabled="forkPoint <= 0"
          @click="emit('fork')"
        >
          {{ t('chat.sessionmap.forkHere') }}
        </button>
      </div>
    </template>
  </aside>
</template>

<style scoped>
.map-inspector {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
  color: var(--text-primary);
  text-align: start;
}

.map-inspector-head {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.map-inspector-title {
  flex: 1;
  min-inline-size: 0;
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  line-height: var(--lh-snug);
  color: var(--text-primary);
  overflow-wrap: anywhere;
}

.map-inspector-close {
  flex-shrink: 0;
  padding: 0 4px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-tertiary);
  font-size: var(--fs-sm);
  cursor: pointer;
}

.map-inspector-close:hover {
  background: var(--bg-secondary);
  color: var(--text-primary);
}

.map-inspector-close:focus-visible,
.map-inspector-btn:focus-visible,
.map-inspector-input:focus-visible {
  outline: 2px solid var(--border-focus);
  outline-offset: 1px;
}

.map-inspector-stats {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 6px;
  margin: 0;
}

.map-inspector-stat {
  padding: 6px 8px;
  border-radius: var(--radius-md);
  background: var(--bg-secondary);
  text-align: start;
}

.map-inspector-stat dt {
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
}

.map-inspector-stat dd {
  margin: 2px 0 0;
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  font-variant-numeric: tabular-nums;
  color: var(--text-primary);
}

.map-inspector-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.map-inspector-label {
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}

.map-inspector-input {
  padding: 4px 8px;
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-base);
  color: var(--text-primary);
  font-size: var(--fs-sm);
}

.map-inspector-note {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
}

.map-inspector-error {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--warning);
}

.map-inspector-actions {
  display: flex;
  gap: 6px;
}

.map-inspector-btn {
  flex: 1;
  padding: 4px 10px;
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-secondary);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  cursor: pointer;
  transition: background var(--dur-fast), color var(--dur-fast);
}

.map-inspector-btn:hover:not(:disabled) {
  background: var(--bg-card);
  color: var(--primary);
  border-color: var(--primary);
}

.map-inspector-btn.is-primary {
  border-color: var(--primary);
  background: var(--primary);
  color: var(--bg-card);
}

.map-inspector-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
