<script setup lang="ts">
/**
 * 参考栏（分屏）：把**另一个会话**以只读方式并排显示。
 *
 * ## 渲染一致性的修复（重要）
 *
 * 此前这里**自己手写**了一份映射：只取 `content`、只区分 user/assistant。
 * 后果是三重的：
 *
 * 1. **内部格式标签直接泄露到界面** —— 主区域的管线会跑 `stripUserInputTag`
 *    （用户输入标记）与 `splitThinking`（思维链），这里全都没跑，
 *    于是 `<thinking>` 之类的内部标记原样显示；
 * 2. **工具调用完全丢失** —— `tool_call` / `tool_result` 条目根本没构造；
 * 3. **与主区域观感不一致** —— 没有日期分隔线，也不是工具分组视图。
 *
 * 现在与主区域**共用同一条管线**：`mergeHistory`（`chat-history.ts`）产生 items，
 * 交给同一个 `MessageList` 渲染。因此标签剥离、工具分组、窗口化行为**完全一致**，
 * 且以后只有一处需要维护。
 *
 * ## 自主切换会话
 *
 * 传入 `sessions` 后，头部会出现下拉，可以直接换分屏显示哪个会话（无需先收起再开）。
 */
import { computed, ref, watch } from 'vue'
import { api } from '../../api'
import MessageList from './MessageList.vue'
import { mergeHistory } from './chat-history'
import type { ChatItem, ChatSession } from './chat-types'

const props = withDefaults(
  defineProps<{
    sessionId: string
    title?: string
    /** 可选会话列表；提供后头部出现下拉，可自主切换分屏显示哪个会话 */
    sessions?: ChatSession[]
    /** 主区域的会话：从下拉里排除，避免两栏显示同一个会话 */
    excludeSessionId?: string
  }>(),
  { sessions: () => [], excludeSessionId: '' },
)

const emit = defineEmits<{ (e: 'update:sessionId', id: string): void }>()

const items = ref<ChatItem[]>([])
const loading = ref(false)
const failed = ref(false)

/** 可切换的目标：排除主区域正在看的那个会话 */
const switchable = computed(() =>
  (props.sessions || []).filter(s => s.id && s.id !== props.excludeSessionId),
)

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
    // ★ 与主区域同一条管线（含格式标签剥离、工具调用、日期分隔线）
    items.value = mergeHistory(data?.messages || [], data?.tool_calls || [])
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

function onPick(e: Event) {
  const id = (e.target as HTMLSelectElement).value
  if (id && id !== props.sessionId) emit('update:sessionId', id)
}

defineExpose({ refresh })
</script>

<template>
  <aside class="preview-pane">
    <header class="pp-head">
      <!-- 自主切换：列出其它会话；当前显示的会话固定为第一项，保证下拉始终有值 -->
      <select
        v-if="switchable.length"
        class="pp-select"
        :value="sessionId"
        :title="$t('切换分屏显示的会话')"
        @change="onPick"
      >
        <option :value="sessionId">
          {{ title || sessionId.slice(0, 8) }}
        </option>
        <option
          v-for="s in switchable"
          :key="s.id"
          :value="s.id"
        >
          {{ s.title || $t('新对话') }}
        </option>
      </select>
      <span
        v-else
        class="pp-title"
      >{{ title || $t('参考会话') }}</span>
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
        v-if="loading && !items.length"
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
      <!-- 与主区域同一个渲染组件：标签剥离 / 工具分组 / 窗口化都一致 -->
      <MessageList
        v-else
        :items="items"
        :loading="loading"
        :session-key="sessionId"
      />
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
.pp-title {
  font-size: var(--fs-sm); font-weight: var(--fw-semibold);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
/* 会话切换下拉：尽量轻，融进头部而不喧宾夺主 */
.pp-select {
  flex: 1; min-width: 0; max-width: 100%;
  font-size: var(--fs-sm); font-weight: var(--fw-semibold);
  color: var(--text-primary); background: transparent;
  border: none; outline: none; cursor: pointer;
  padding: 2px 0;
}
.pp-select:hover { color: var(--accent); }
.pp-btn {
  flex: none; border: none; background: transparent; color: var(--text-tertiary);
  cursor: pointer; font-size: 13px; padding: 0 4px;
}
.pp-btn:hover { color: var(--text-primary); }
/* MessageList 自带滚动与窗口化，这里只给高度与最小内边距 */
.pp-body { flex: 1; min-height: 0; display: flex; flex-direction: column; overflow: hidden; padding: 8px 4px; }
.pp-empty { padding: 16px 12px; color: var(--text-tertiary); font-size: 12px; }
</style>
