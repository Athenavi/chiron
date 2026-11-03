<script setup lang="ts">
/**
 * 可折叠的任务清单面板。
 *
 * 定位：**长任务的锚点**。长任务最典型的失败是"做着做着跑偏"，用户需要一个始终
 * 挂在屏幕上、一眼能看到"还剩几项、卡在哪"的东西。所以它不塞进消息流里随内容滚走，
 * 而是固定在输入区上方。
 *
 * 三个刻意的实现选择：
 *
 * 1. **纯展示组件**：状态由 `useTodoPanel` 按 session 持有，这里只接收投影后的
 *    `board`。组件自身无状态，才能在流式输出期间被频繁重渲染而不丢数据。
 * 2. **按编号渲染**：编号来自 host 赋予的 `id`（`t1`/`t2`…）。模型和用户都靠它
 *    指代"第几项"，所以编号必须稳定显示出来，不能只用视觉顺序。
 * 3. **依赖阻塞显式可见**：多 agent 共享看板时，"这项在等 t2"是用户最需要的信息 ——
 *    否则面板看起来像卡住了，而实际上是在等前序完成。
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

import type { TodoBoard, TodoRow } from './todoProjection'

const props = defineProps<{
  board: TodoBoard
  collapsed: boolean
}>()

const emit = defineEmits<{
  (e: 'toggle'): void
}>()

const { t } = useI18n()

const marks: Record<string, string> = {
  pending: '☐',
  in_progress: '◐',
  completed: '☑',
}

const summary = computed(() => props.board.summary)

/** 有多个认领者时才展示归属 —— 单人场景下每行都标 `main` 只是噪音。 */
const showOwners = computed(() => props.board.owners.length > 1)

function mark(row: TodoRow): string {
  return marks[row.status] ?? marks.pending
}

function statusLabel(row: TodoRow): string {
  return t(`common.todo_${row.status}`)
}

/** 折叠时也显示"正在做什么"，否则折叠等于失明。 */
const activeContent = computed(() => {
  const active = props.board.rows.find((r) => r.status === 'in_progress')
  return active?.content ?? ''
})
</script>

<template>
  <section
    v-if="board.rows.length"
    class="todo-panel"
    :class="{ 'is-collapsed': collapsed }"
  >
    <button
      type="button"
      class="todo-panel-head"
      :aria-expanded="!collapsed"
      :aria-label="collapsed ? t('chat.todo_expand') : t('chat.todo_collapse')"
      @click="emit('toggle')"
    >
      <span
        class="todo-panel-caret"
        aria-hidden="true"
      >{{ collapsed ? '▸' : '▾' }}</span>
      <span class="todo-panel-title">{{ t('chat.todo_panel_title') }}</span>
      <span class="todo-panel-count">
        {{ summary.completed }}/{{ summary.total }}
      </span>
      <!-- 进度条常驻：折叠状态下它就是唯一的进度提示 -->
      <span
        class="todo-panel-track"
        :aria-hidden="true"
      >
        <span
          class="todo-panel-fill"
          :style="{ width: `${summary.percent}%` }"
        />
      </span>
      <span
        v-if="collapsed && activeContent"
        class="todo-panel-active"
      >{{ activeContent }}</span>
      <span
        v-else-if="summary.inProgress > 0"
        class="todo-panel-running"
        :aria-label="t('common.todo_in_progress')"
      >◐</span>
    </button>

    <ol
      v-if="!collapsed"
      class="todo-panel-body"
    >
      <li
        v-for="row in board.rows"
        :key="row.id"
        class="todo-panel-item"
        :data-status="row.status"
        :data-blocked="row.blocked ? '1' : undefined"
      >
        <span
          class="todo-panel-num"
          aria-hidden="true"
        >#{{ row.index }}</span>
        <span
          class="todo-panel-mark"
          :title="statusLabel(row)"
          :aria-label="statusLabel(row)"
        >{{ mark(row) }}</span>
        <span class="todo-panel-text">{{ row.content }}</span>
        <span
          v-if="showOwners && row.owner"
          class="todo-panel-owner"
          :title="t('chat.todo_claimed_by', { owner: row.owner })"
        >@{{ row.owner }}</span>
        <span
          v-if="row.blocked"
          class="todo-panel-blocked"
          :title="t('chat.todo_blocked_by', { ids: row.blockedBy.join(', ') })"
        >⏳ {{ row.blockedBy.join(', ') }}</span>
      </li>
    </ol>
  </section>
</template>

<style scoped>
.todo-panel {
  border: 1px solid var(--border-subtle);
  border-radius: var(--sig-radius-code);
  background: var(--bg-code);
  overflow: hidden;
}

.todo-panel-head {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 12px;
  border: 0;
  background: var(--terminal-header-bg);
  color: var(--text-secondary);
  font: inherit;
  text-align: start;
  cursor: pointer;
}

.todo-panel-head:hover {
  color: var(--text-primary);
}

.todo-panel-caret {
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
}

.todo-panel-title {
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  white-space: nowrap;
}

.todo-panel-count {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.todo-panel-track {
  flex: 1;
  min-width: 40px;
  height: 4px;
  border-radius: 2px;
  background: var(--border-subtle);
  overflow: hidden;
}

.todo-panel-fill {
  display: block;
  height: 100%;
  background: var(--success);
  /* 进度推进是持续发生的（每完成一项就动一次），过渡让它不像闪跳 */
  transition: width var(--dur-normal) var(--ease-out);
}

.todo-panel-active {
  min-width: 0;
  flex: 0 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--fs-xs);
  color: var(--warning);
}

.todo-panel-running {
  font-family: var(--font-mono);
  color: var(--warning);
}

.todo-panel-body {
  list-style: none;
  margin: 0;
  padding: 6px 12px 8px;
  max-height: 34vh;
  overflow-y: auto;
}

.todo-panel-item {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 3px 0;
  font-size: var(--fs-xs);
  line-height: 1.6;
}

.todo-panel-num {
  flex: none;
  width: 28px;
  font-family: var(--font-mono);
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}

.todo-panel-mark {
  flex: none;
  font-family: var(--font-mono);
  color: var(--text-tertiary);
}

.todo-panel-item[data-status='in_progress'] .todo-panel-mark {
  color: var(--warning);
}

.todo-panel-item[data-status='completed'] .todo-panel-mark {
  color: var(--success);
}

.todo-panel-item[data-status='completed'] .todo-panel-text {
  color: var(--text-tertiary);
  text-decoration: line-through;
}

.todo-panel-text {
  flex: 1;
  min-width: 0;
  color: var(--text-primary);
  white-space: pre-wrap;
  word-break: break-word;
}

.todo-panel-owner {
  flex: none;
  font-family: var(--font-mono);
  color: var(--primary);
  max-width: 12ch;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.todo-panel-blocked {
  flex: none;
  font-family: var(--font-mono);
  color: var(--text-tertiary);
  white-space: nowrap;
}

/* 被阻塞项整体压暗：让"能开工的"在一屏里更跳出来 */
.todo-panel-item[data-blocked='1'] .todo-panel-text {
  color: var(--text-secondary);
}

@media (max-width: 768px) {
  .todo-panel-body {
    max-height: 26vh;
  }

  .todo-panel-num {
    width: 24px;
  }
}
</style>
