<script setup lang="ts">
/**
 * 待办角标（M21）：跨会话回答"我还欠哪些答复"。
 *
 * 与 `InteractionInbox.vue` 的分工：收件箱在**打开那个会话**时列出该会话的票（可答复），
 * 角标在**任何页面**给出未读数 —— 点一下跳到欠答复的那条会话。M7 只做到"打开就看得见"，
 * 而"用户不知道哪个会话欠答复"正是它没覆盖的那一半。
 *
 * 数据源两条，缺一不可：
 *   - **用户级 SSE**（`createUserEventsConnection`）：落票瞬间刷新（用户可能正看着别的会话）；
 *   - **低频轮询**：SSE 断开（浏览器休眠、网关重启、代理掐长连接）时的自愈路径 ——
 *     提醒不该依赖某一条连接活着。
 *
 * 两条都失败时**保持上一次读数**、不弹错：角标是增强，不是页面能否使用的条件。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { Badge } from 'ant-design-vue'
import {
  createUserEventsConnection,
  listMyPendingInteractions,
  type PendingInteraction,
} from '../../api/interactions'

const props = defineProps<{ pollMs?: number }>()
const emit = defineEmits<{ (e: 'select', sessionId: string): void }>()

const items = ref<PendingInteraction[]>([])
const source = ref<EventSource | null>(null)
let timer: ReturnType<typeof setInterval> | null = null

async function refresh() {
  try {
    items.value = await listMyPendingInteractions()
  } catch {
    // 保持上一次读数：拉不到不等于"没有待办"。
  }
}

function openFirst() {
  const sessionId = items.value[0]?.session_id
  if (sessionId) emit('select', sessionId)
}

onMounted(() => {
  void refresh()
  timer = setInterval(() => void refresh(), props.pollMs ?? 30_000)
  try {
    source.value = createUserEventsConnection(() => void refresh())
  } catch {
    // 环境不支持 EventSource（或构造失败）：轮询仍然兜住。
    source.value = null
  }
})

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  timer = null
  source.value?.close()
  source.value = null
})
</script>

<template>
  <button
    v-if="items.length > 0"
    type="button"
    class="interaction-badge"
    :title="$t('agent.pending_interactions_title')"
    @click="openFirst"
  >
    <Badge :count="items.length" :overflow-count="99" />
  </button>
</template>

<style scoped>
.interaction-badge {
  display: inline-flex;
  align-items: center;
  border: none;
  background: transparent;
  cursor: pointer;
  padding: 0 4px;
  transition: color var(--dur-fast) ease, background-color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.interaction-badge:hover { background: var(--bg-hover); }
</style>
