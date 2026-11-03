<script setup lang="ts">
/**
 * 分叉对话框：选档次（全量 / 压缩 / 自定义）。
 *
 * 三档不是三套实现，而是同一份后端能力的三种参数组合
 * （`POST /v1/conversations/{id}/branch`，见 `useMapActions.ts::buildForkRequest`）：
 *
 *   - 全量 = `truncate` + 落点：逐字复制，新会话与源会话一致；
 *   - 压缩 = `condense` + `keep_tail: 0`：历史压成**一条**摘要，新会话只带摘要；
 *   - 自定义 = 由用户挑落点与方式（`truncate` / `condense`）。
 *
 * 组件本身**不碰网络**：它只把选择翻译成 `confirm` 事件，取数据与调用后端由父组件
 * 负责（与 `DraftComposer` 同风格）。这样三档的语义可以纯用事件断言，
 * 不必去 mock axios。
 */
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import type { ForkMode, ForkTier } from '../useMapActions'
import type { ForkPoint } from '../types'

const props = withDefaults(
  defineProps<{
    /** 源会话标题，用于对话框抬头；空则回落到通用文案 */
    sourceTitle?: string
    /** 可选的落点（来自 `GET /v1/conversations/{id}/fork-points`） */
    points?: readonly ForkPoint[]
    /** 源会话消息总数；0 表示空会话，任何分叉都不合法 */
    total?: number
    /** 落点还在加载 */
    loading?: boolean
    /** 父组件正在执行分叉 */
    busy?: boolean
    /** 上次失败的原文（父组件回填，组件只负责显示） */
    error?: string
  }>(),
  { points: () => [], total: 0, sourceTitle: '', error: '' },
)

const emit = defineEmits<{
  confirm: [payload: { tier: ForkTier; fromIndex: number; mode?: ForkMode }]
  cancel: []
}>()

const { t } = useI18n()

/**
 * 压缩所需的最少历史条数。
 *
 * 与后端 `minCondenseMessages = 3`（`manager_branch.go`）对齐 —— 但这里只用它
 * **提前禁用并按原因提示**，真正的裁决始终在后端：窗口太短时后端会把整段原样复制
 * 并把 `condensed` 置 false，不会失败。前端若静默降级，用户会以为"压缩过了"，
 * 点进新会话却发现是一堆原文 —— 那是更坏的体验，所以这里选择禁用 + 说清原因。
 */
const MIN_CONDENSE = 3

/** 压缩式裁剪默认保留的原文条数（与后端 `defaultBranchKeepTail = 4` 一致）。 */
const DEFAULT_KEEP_TAIL = 4

const tier = ref<ForkTier>('full')
/** 自定义档的"方式"：默认压缩（与后端默认 mode 一致） */
const customMode = ref<ForkMode>('condensed')
/** 自定义档选中的落点 */
const customPoint = ref(1)

const total = computed(() => (props.total > 0 ? props.total : props.points.length))
const isEmpty = computed(() => !props.loading && total.value <= 0)

/** 用户在当前选项下实际使用的落点 */
const fromIndex = computed(() => {
  if (tier.value !== 'custom') return total.value
  return customPoint.value
})

/** 当前选项是否走压缩 */
const usesCondense = computed(() => {
  if (tier.value === 'condensed') return true
  if (tier.value === 'custom') return customMode.value === 'condensed'
  return false
})

/** 压缩档的可用性：历史太短就没法压（后端会退回整段复制，与"压缩"承诺不符） */
const condenseBlocked = computed(
  () => usesCondense.value && !props.loading && fromIndex.value < MIN_CONDENSE,
)

const canConfirm = computed(
  () => !props.loading && !props.busy && !isEmpty.value && !condenseBlocked.value,
)

/** 预览文案：告诉用户"这一次到底会发生什么"，而不是让他点完才发现 */
const preview = computed(() => {
  if (isEmpty.value) return t('chat.sessionmap.forkEmptySession')
  if (condenseBlocked.value) return t('chat.sessionmap.forkTooShort', { n: MIN_CONDENSE })
  if (!usesCondense.value) {
    return t('chat.sessionmap.forkPreviewFull', { n: fromIndex.value })
  }
  if (tier.value === 'condensed') {
    // 压缩档：keep_tail=0，新会话里只有摘要这一条
    return t('chat.sessionmap.forkPreviewCondensed', { n: fromIndex.value })
  }
  return t('chat.sessionmap.forkPreviewKeepTail', { k: DEFAULT_KEEP_TAIL })
})

/**
 * 落点变化时纠正选择。
 *
 * `total` 是异步来的（先渲染、后拿到数据），期间 `customPoint` 会停在初值 1，
 * 等数据到位后若不重设，用户会以"从第 1 条分叉"为目标打开对话框。
 */
watch(
  total,
  (n) => {
    if (n > 0 && (customPoint.value > n || customPoint.value < 1)) {
      customPoint.value = n
    }
  },
  { immediate: true },
)

function confirm() {
  if (!canConfirm.value) return
  const payload: { tier: ForkTier; fromIndex: number; mode?: ForkMode } = {
    tier: tier.value,
    fromIndex: fromIndex.value,
  }
  if (tier.value === 'custom') payload.mode = customMode.value
  emit('confirm', payload)
}

/** Esc 取消、Cmd/Ctrl+Enter 确认 —— 与 `DraftComposer` 保持一致的手感。 */
function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.stopPropagation()
    emit('cancel')
    return
  }
  if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
    event.preventDefault()
    confirm()
  }
}

/**
 * 下拉里给每条落点一个可辨认的短标签：`#序号 · 正文预览`。
 *
 * 刻意**不**附带角色名的本地化：`chat.sessionmap` 下只有 `roleUser` 一个角色键，
 * 其余角色（assistant / system / tool）都没有对应文案。用 `t()` 拼一个可能不存在的
 * 键名，i18n 在缺键时会原样吐出键路径，下拉里就会出现
 * `chat.sessionmap.roleAssistant` 这种给终端用户看的乱码。
 * 预览文本本身已经能让人认出是哪一条。
 */
function pointLabel(point: ForkPoint): string {
  return `#${point.index} · ${point.preview}`
}
</script>

<template>
  <section
    class="map-fork"
    :aria-label="t('chat.sessionmap.forkTitle')"
    @keydown="onKeydown"
  >
    <header class="map-fork-head">
      <h3 class="map-fork-title">
        {{ t('chat.sessionmap.forkTitle') }}
      </h3>
      <button
        type="button"
        class="map-fork-close"
        :aria-label="t('chat.sessionmap.cancel')"
        @click="emit('cancel')"
      >
        <span aria-hidden="true">×</span>
      </button>
    </header>

    <p
      v-if="sourceTitle"
      class="map-fork-source"
    >
      {{ sourceTitle }}
    </p>

    <fieldset class="map-fork-tiers">
      <legend class="map-fork-legend">
        {{ t('chat.sessionmap.forkMode') }}
      </legend>

      <label
        v-for="option in [
          { value: 'full', label: 'forkTierFull', hint: 'forkTierFullHint' },
          { value: 'condensed', label: 'forkTierCondensed', hint: 'forkTierCondensedHint' },
          { value: 'custom', label: 'forkTierCustom', hint: 'forkTierCustomHint' },
        ]"
        :key="option.value"
        class="map-fork-tier"
        :class="{ 'is-active': tier === option.value }"
      >
        <input
          v-model="tier"
          type="radio"
          name="map-fork-tier"
          :value="option.value"
          :disabled="busy"
        >
        <span class="map-fork-tier-body">
          <span class="map-fork-tier-label">{{ t(`chat.sessionmap.${option.label}`) }}</span>
          <span class="map-fork-tier-hint">{{ t(`chat.sessionmap.${option.hint}`) }}</span>
        </span>
      </label>
    </fieldset>

    <template v-if="tier === 'custom'">
      <label
        class="map-fork-label"
        for="map-fork-point"
      >{{ t('chat.sessionmap.forkPoint') }}</label>
      <select
        id="map-fork-point"
        v-model.number="customPoint"
        class="map-fork-select"
        :disabled="busy || loading || isEmpty"
      >
        <option
          v-for="point in points"
          :key="point.index"
          :value="point.index"
        >
          {{ pointLabel(point) }}
        </option>
      </select>
      <p
        v-if="!loading && points.length === 0"
        class="map-fork-note"
      >
        {{ t('chat.sessionmap.forkNoPoints') }}
      </p>
      <p class="map-fork-note">
        {{ t('chat.sessionmap.forkPointHint') }}
      </p>

      <label
        class="map-fork-label"
        for="map-fork-custom-mode"
      >{{ t('chat.sessionmap.forkMode') }}</label>
      <select
        id="map-fork-custom-mode"
        v-model="customMode"
        class="map-fork-select"
        :disabled="busy"
      >
        <option value="condensed">
          {{ t('chat.sessionmap.forkModeCondensed') }}
        </option>
        <option value="full">
          {{ t('chat.sessionmap.forkModeFull') }}
        </option>
      </select>
    </template>

    <p
      class="map-fork-preview"
      :class="{ 'is-blocked': !canConfirm && !loading }"
    >
      {{ loading ? t('chat.sessionmap.loading') : preview }}
    </p>

    <p
      v-if="error"
      class="map-fork-error"
      role="alert"
    >
      {{ error }}
    </p>

    <div class="map-fork-actions">
      <button
        type="button"
        class="map-fork-btn"
        :disabled="busy"
        @click="emit('cancel')"
      >
        {{ t('chat.sessionmap.cancel') }}
      </button>
      <button
        type="button"
        class="map-fork-btn is-primary"
        :disabled="!canConfirm"
        @click="confirm"
      >
        {{ busy ? t('chat.sessionmap.loading') : t('chat.sessionmap.forkConfirm') }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.map-fork {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px;
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
  color: var(--text-primary);
  text-align: start;
}

.map-fork-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.map-fork-title {
  flex: 1;
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
}

.map-fork-close {
  flex-shrink: 0;
  padding: 0 4px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-tertiary);
  font-size: var(--fs-sm);
  cursor: pointer;
}

.map-fork-close:hover {
  background: var(--bg-secondary);
  color: var(--text-primary);
}

.map-fork-source {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.map-fork-tiers {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0;
  padding: 0;
  border: 0;
}

.map-fork-legend {
  padding: 0;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}

.map-fork-tier {
  display: flex;
  gap: 6px;
  padding: 4px 6px;
  border: var(--hairline-width) solid transparent;
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.map-fork-tier.is-active {
  border-color: var(--primary);
  background: var(--bg-secondary);
}

.map-fork-tier input {
  margin-block-start: 2px;
}

.map-fork-tier-body {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.map-fork-tier-label {
  font-size: var(--fs-sm);
}

.map-fork-tier-hint {
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
}

.map-fork-label {
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}

.map-fork-select {
  padding: 4px 8px;
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-base);
  color: var(--text-primary);
  font-size: var(--fs-sm);
  font-family: inherit;
}

.map-fork-note {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
}

.map-fork-preview {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}

/* 不能分叉时把原因变成警告色：这是用户唯一能看到的"为什么按钮是灰的" */
.map-fork-preview.is-blocked {
  color: var(--warning);
}

.map-fork-error {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--warning);
}

.map-fork-actions {
  display: flex;
  gap: 6px;
  margin-block-start: 4px;
}

.map-fork-btn {
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

.map-fork-btn:hover:not(:disabled) {
  background: var(--bg-card);
  color: var(--primary);
  border-color: var(--primary);
}

.map-fork-btn.is-primary {
  border-color: var(--primary);
  background: var(--primary);
  color: var(--bg-card);
}

.map-fork-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.map-fork-close:focus-visible,
.map-fork-btn:focus-visible,
.map-fork-select:focus-visible {
  outline: 2px solid var(--border-focus);
  outline-offset: 1px;
}
</style>
