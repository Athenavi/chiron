<script setup lang="ts">
/**
 * 参考栏（分屏 6a）：把**另一个会话**以**只读**方式并排显示。
 *
 * ## 为什么先做只读
 *
 * 真正"两栏都可写"需要把 `activeSessionId` / `items` / 流式连接从单值改成
 * **按 pane 归属**（ZCode 为此有 `paneLayoutStore` + `SessionPane` 一整套）——
 * 那是一次状态归属重构。而"边跑子 Agent 边看另一个会话"这个高频需求**只读就够**，
 * 且完全不触碰现有状态模型（零回归风险）。
 *
 * 可写双栏是 6b 之后的范围。
 */
import { ref, watch } from 'vue'
import { api } from '../../api'
import MessageItem from './MessageItem.vue'
import type { ChatItem } from './chat-types'

const props = defineProps<{ sessionId: string; title?: string }>()

const items = ref<ChatItem[]>([])
const loading = ref(false)
const failed = ref(false)

async function load() {
  if (!props.sessionId) {
    items.value = []
    return
  }
  loading.value = true
  failed.value = false
  try {
    const res = await api.get(`/v1/conversations/${props.sessionId}?limit=50`)
    const data = res.data?.data || res.data
    const messages: Array<{ id?: string; role?: string; content?: string }> = data?.messages || []
    items.value = messages.map((m, i) => ({
      kind: 'text' as const,
      id: m.id || `preview-${i}`,
      role: (m.role === 'assistant' ? 'assistant' : 'user') as 'user' | 'assistant',
      content: m.content || '',
    }))
  } catch {
    // 只读参考栏失败不该打扰主线：降级成一行提示
    failed.value = true
    items.value = []
  } finally {
    loading.value = false
  }
}

watch(() => props.sessionId, load, { immediate: true })

async function refresh() {
  await load()
}
defineExpose({ refresh })
</script>

<template>
  <aside class="preview-pane">
    <header class="pp-head">
      <span class="pp-title">{{ title || $t('参考会话') }}</span>
      <button
        type="button"
        class="pp-btn"
        :title="$t('刷新')"
        @click="refresh"
      >
        ⟳
      </button>
    </header>
    <div class="pp-body">
      <div
        v-if="loading"
        class="pp-empty"
      >
        {{ $t('加载中…') }}
      </div>
      <div
        v-else-if="failed"
        class="pp-empty"
      >
        {{ $t('加载失败（会话可能已删除或无权访问）') }}
      </div>
      <div
        v-else-if="!items.length"
        class="pp-empty"
      >
        {{ $t('这个会话还没有消息') }}
      </div>
      <template v-else>
        <MessageItem
          v-for="item in items"
          :key="item.id"
          :item="item"
        />
      </template>
    </div>
  </aside>
</template>

<style scoped>
.preview-pane {
  display: flex; flex-direction: column; min-width: 0; height: 100%;
  border-left: 1px solid var(--border-subtle);
  /* 参考栏自己滚动，避免把主会话的滚动位置带跑 */
  overflow: hidden;
}
.pp-head {
  flex: none; display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 6px 12px; border-bottom: 1px solid var(--border-subtle);
}
.pp-title { font-size: var(--fs-sm); font-weight: var(--fw-semibold); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pp-btn { border: none; background: transparent; color: var(--text-tertiary); cursor: pointer; font-size: 13px; padding: 0 4px; }
.pp-btn:hover { color: var(--text-primary); }
.pp-body { flex: 1; min-height: 0; overflow-y: auto; padding: 8px 4px; }
.pp-empty { padding: 16px 12px; color: var(--text-tertiary); font-size: 12px; }
</style>
