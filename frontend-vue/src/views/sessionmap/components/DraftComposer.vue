<script setup lang="ts">
/**
 * 草稿卡：在画布上直接开一个新会话。
 *
 * 对等旧实现的「新建会话草稿」。与旧实现的差别：旧版把草稿内容存进
 * `localStorage` 再由 `app.js` 拼一串 RPC 消息发给宿主；
 * 这里只是一个受控表单，真正建会话 + 发消息由父组件用 `useMapActions` 完成 ——
 * 组件本身不碰网络，于是它的行为可以纯用事件断言（见 `DraftComposer.spec.ts`）。
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps<{
  /** 提交中：父组件正在等 `createSession` / `sendMessage` 返回 */
  busy?: boolean
  /** 上次提交失败的原因（父组件回填，组件只负责显示） */
  error?: string
}>()

const emit = defineEmits<{
  submit: [payload: { title: string; text: string }]
  cancel: []
}>()

const { t } = useI18n()

const title = ref('')
const text = ref('')

/**
 * 只允许"有正文"时提交。
 *
 * 允许空正文创建空会话没有意义：地图上的卡片指向一个没有任何消息的会话，
 * 用户点进去只会看到空白，还要手动删。
 */
const canSubmit = computed(() => text.value.trim() !== '' && !props.busy)

function submit() {
  if (!canSubmit.value) return
  emit('submit', { title: title.value.trim(), text: text.value.trim() })
}

/** Esc 取消、Cmd/Ctrl+Enter 提交 —— 与对话页输入框的习惯一致。 */
function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.stopPropagation()
    emit('cancel')
    return
  }
  if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
    event.preventDefault()
    submit()
  }
}
</script>

<template>
  <section
    class="map-draft"
    :aria-label="t('chat.sessionmap.draft')"
    @keydown="onKeydown"
  >
    <header class="map-draft-head">
      <h3 class="map-draft-title">
        {{ t('chat.sessionmap.draft') }}
      </h3>
      <button
        type="button"
        class="map-draft-close"
        :aria-label="t('chat.sessionmap.cancel')"
        @click="emit('cancel')"
      >
        <span aria-hidden="true">×</span>
      </button>
    </header>

    <label
      class="map-draft-label"
      for="map-draft-title"
    >{{ t('chat.sessionmap.title') }}</label>
    <input
      id="map-draft-title"
      v-model="title"
      class="map-draft-input"
      type="text"
      maxlength="120"
      :placeholder="t('chat.sessionmap.untitled')"
      :disabled="busy"
    >

    <label
      class="map-draft-label"
      for="map-draft-text"
    >{{ t('chat.sessionmap.messages') }}</label>
    <textarea
      id="map-draft-text"
      v-model="text"
      class="map-draft-textarea"
      rows="4"
      :placeholder="t('chat.sessionmap.draftPlaceholder')"
      :disabled="busy"
    />

    <p
      v-if="error"
      class="map-draft-error"
      role="alert"
    >
      {{ error }}
    </p>

    <div class="map-draft-actions">
      <button
        type="button"
        class="map-draft-btn"
        :disabled="busy"
        @click="emit('cancel')"
      >
        {{ t('chat.sessionmap.cancel') }}
      </button>
      <button
        type="button"
        class="map-draft-btn is-primary"
        :disabled="!canSubmit"
        @click="submit"
      >
        {{ busy ? t('chat.sessionmap.loading') : t('chat.sessionmap.draftSend') }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.map-draft {
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

.map-draft-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.map-draft-title {
  flex: 1;
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
}

.map-draft-close {
  flex-shrink: 0;
  padding: 0 4px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-tertiary);
  font-size: var(--fs-sm);
  cursor: pointer;
}

.map-draft-close:hover {
  background: var(--bg-secondary);
  color: var(--text-primary);
}

.map-draft-close:focus-visible,
.map-draft-btn:focus-visible,
.map-draft-input:focus-visible,
.map-draft-textarea:focus-visible {
  outline: 2px solid var(--border-focus);
  outline-offset: 1px;
}

.map-draft-label {
  font-size: var(--fs-xs);
  color: var(--text-secondary);
}

.map-draft-input,
.map-draft-textarea {
  padding: 4px 8px;
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-base);
  color: var(--text-primary);
  font-size: var(--fs-sm);
  font-family: inherit;
}

/* 竖向缩放由用户决定：横向拉伸会把面板撑破 */
.map-draft-textarea {
  resize: vertical;
  line-height: var(--lh-snug);
}

.map-draft-error {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--warning);
}

.map-draft-actions {
  display: flex;
  gap: 6px;
  margin-block-start: 4px;
}

.map-draft-btn {
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

.map-draft-btn:hover:not(:disabled) {
  background: var(--bg-card);
  color: var(--primary);
  border-color: var(--primary);
}

.map-draft-btn.is-primary {
  border-color: var(--primary);
  background: var(--primary);
  color: var(--bg-card);
}

.map-draft-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
