<script setup lang="ts">
import { CaretRightOutlined } from '@ant-design/icons-vue'
import type { ProjectedHeader } from './transcriptProjection'

defineProps<{ header: ProjectedHeader }>()
const emit = defineEmits<{ (e: 'toggle'): void }>()
</script>

<template>
  <div
    class="fold-header chat-row-shell"
    :data-scope="header.scope"
  >
    <button
      class="fold-main chat-row-head"
      type="button"
      :aria-expanded="header.open"
      @click="emit('toggle')"
    >
      <CaretRightOutlined
        class="chat-chevron"
        :class="{ open: header.open }"
      />
      <span
        class="chat-state-dot"
        :data-state="header.status"
        aria-hidden
      />
      <span class="fold-title">{{ header.title }}</span>
      <span
        class="chat-sep"
        aria-hidden
      />
      <span class="fold-summary">{{ header.summary }}</span>
    </button>
  </div>
</template>

<!-- 折叠头是投影产出的行，节奏/chevron/状态点沿用全局 .chat-* 原语 -->
<style scoped>
.fold-title { font-size: 13px; font-weight: var(--fw-semibold); color: var(--text-primary); white-space: nowrap; }
.fold-summary { flex: 1; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; color: var(--text-tertiary); font-size: 12px; text-align: start; }
/* 工具组是回合内的次级结构：标题降一档，避免与回合头争夺注意力 */
.fold-header[data-scope='tool_group'] .fold-title { font-weight: var(--fw-medium); color: var(--text-secondary); }
</style>
