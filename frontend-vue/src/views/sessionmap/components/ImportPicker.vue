<script setup lang="ts">
/**
 * 导入选择器：把已有会话摆到画布上。
 *
 * 旧实现（`app.js` 的导入区）是一次性把**全部**会话塞进画布，用户随后得逐个删掉
 * 不要的卡片。这里改成显式勾选：默认什么都不选，选完点确认才落地。
 *
 * 已经在这张画布上的会话默认不出现在候选里 —— 允许重复导入会在画布上产生两张
 * 指向同一会话的卡片，而删除时只删掉其中一张，另一张变成"看起来还有内容"的孤儿。
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import type { ChatSession } from '../../../components/chat/chat-types'

const props = defineProps<{
  /** 全部会话（宿主已加载好的列表，避免这里再发一次请求） */
  sessions: readonly ChatSession[]
  /** 已经在地图上的会话 id */
  existing: readonly string[]
  busy?: boolean
}>()

const emit = defineEmits<{
  import: [sessionIds: string[]]
  cancel: []
}>()

const { t } = useI18n()

const query = ref('')
const picked = ref<string[]>([])

/** 候选：排除已在地图上的，再按标题/别名过滤。 */
const candidates = computed(() => {
  const used = new Set(props.existing.filter(Boolean))
  const q = query.value.trim().toLowerCase()
  return props.sessions
    .filter(s => s.id && !used.has(s.id))
    .filter(s => {
      if (!q) return true
      const title = (s.title ?? '').toLowerCase()
      return title.includes(q)
    })
})

function isPicked(id: string): boolean {
  return picked.value.includes(id)
}

function toggle(id: string) {
  picked.value = isPicked(id) ? picked.value.filter(x => x !== id) : [...picked.value, id]
}

function confirm() {
  if (picked.value.length === 0 || props.busy) return
  emit('import', picked.value)
}
</script>

<template>
  <section
    class="map-import"
    :aria-label="t('chat.sessionmap.import')"
  >
    <header class="map-import-head">
      <h3 class="map-import-title">
        {{ t('chat.sessionmap.import') }}
      </h3>
      <button
        type="button"
        class="map-import-close"
        :aria-label="t('chat.sessionmap.cancel')"
        @click="emit('cancel')"
      >
        <span aria-hidden="true">×</span>
      </button>
    </header>

    <input
      v-model="query"
      class="map-import-search"
      type="search"
      :placeholder="t('chat.sessionmap.importSearch')"
      :aria-label="t('chat.sessionmap.importSearch')"
    >

    <p
      v-if="candidates.length === 0"
      class="map-import-note"
    >
      {{ t('chat.sessionmap.importEmpty') }}
    </p>

    <ul
      v-else
      class="map-import-list"
    >
      <li
        v-for="session in candidates"
        :key="session.id"
        class="map-import-item"
      >
        <label class="map-import-label">
          <input
            type="checkbox"
            :checked="isPicked(session.id)"
            @change="toggle(session.id)"
          >
          <span class="map-import-name">{{ session.title || t('chat.sessionmap.untitled') }}</span>
        </label>
      </li>
    </ul>

    <div class="map-import-actions">
      <button
        type="button"
        class="map-import-btn"
        :disabled="busy"
        @click="emit('cancel')"
      >
        {{ t('chat.sessionmap.cancel') }}
      </button>
      <button
        type="button"
        class="map-import-btn is-primary"
        :disabled="picked.length === 0 || busy"
        @click="confirm"
      >
        {{ t('chat.sessionmap.import') }} ({{ picked.length }})
      </button>
    </div>
  </section>
</template>

<style scoped>
.map-import {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
  color: var(--text-primary);
  text-align: start;
}

.map-import-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.map-import-title {
  flex: 1;
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
}

.map-import-close {
  flex-shrink: 0;
  padding: 0 4px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-tertiary);
  font-size: var(--fs-sm);
  cursor: pointer;
}

.map-import-close:hover {
  background: var(--bg-secondary);
  color: var(--text-primary);
}

.map-import-close:focus-visible,
.map-import-btn:focus-visible,
.map-import-search:focus-visible {
  outline: 2px solid var(--border-focus);
  outline-offset: 1px;
}

.map-import-search {
  padding: 4px 8px;
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-base);
  color: var(--text-primary);
  font-size: var(--fs-sm);
}

.map-import-note {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
}

/* 候选列表自己滚动：会话多的账号会把面板撑到屏幕之外 */
.map-import-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin: 0;
  padding: 0;
  max-block-size: 240px;
  overflow-y: auto;
  list-style: none;
}

.map-import-label {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 6px;
  border-radius: var(--radius-sm);
  font-size: var(--fs-sm);
  cursor: pointer;
}

.map-import-label:hover {
  background: var(--bg-secondary);
}

.map-import-name {
  min-inline-size: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
}

.map-import-actions {
  display: flex;
  gap: 6px;
}

.map-import-btn {
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

.map-import-btn:hover:not(:disabled) {
  background: var(--bg-card);
  color: var(--primary);
  border-color: var(--primary);
}

.map-import-btn.is-primary {
  border-color: var(--primary);
  background: var(--primary);
  color: var(--bg-card);
}

.map-import-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
