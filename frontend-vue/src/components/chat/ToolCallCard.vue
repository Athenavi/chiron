<script setup lang="ts">
import { ref, computed } from 'vue'
import { CaretRightOutlined } from '@ant-design/icons-vue'
import type { ToolCallItem } from './chat-types'
import { toolSummary } from '../../utils/toolFamily'
import { toolGroupKind } from './transcriptProjection'

const props = defineProps<{ item: ToolCallItem; depth?: number }>()
const expanded = ref(false)

function prettyArgs(): string {
  try {
    return JSON.stringify(JSON.parse(props.item.arguments || '{}'), null, 2)
  } catch {
    return props.item.arguments || ''
  }
}

/** 家族：**复用投影层**的 `toolGroupKind`（组头的划分口径），保证单条与组头同一套分类 */
const family = computed(() => toolGroupKind(props.item.name))

/** 摘要：家族字段优先（读文件看 path、执行看 command、搜索看 pattern…），未登记工具走通用兜底 */
const summary = computed(() => toolSummary(props.item.name, props.item.arguments))

const padLeft = computed(() => (props.depth || 0) * 22)
</script>

<template>
  <div
    class="tool-row-wrap chat-row-shell"
    :data-state="item.status"
    :data-tool="item.name"
    :data-family="family"
  >
    <!-- 工具树缩进连接线 -->
    <div
      v-if="(depth || 0) > 0"
      class="tree-guide"
      :style="{ left: `${padLeft - 14}px` }"
      aria-hidden
    />

    <div
      class="tool-row"
      :style="{ marginLeft: `${padLeft}px` }"
    >
      <button
        class="tool-main chat-row-head"
        type="button"
        :aria-expanded="expanded"
        @click="expanded = !expanded"
      >
        <CaretRightOutlined
          class="chat-chevron"
          :class="{ open: expanded }"
        />
        <span class="tool-name">{{ item.name }}</span>
        <span
          class="chat-sep"
          aria-hidden
        />
        <span
          class="chat-state-dot"
          :data-state="item.status"
          aria-hidden
        />
        <span class="tool-summary">{{ summary }}</span>
      </button>

      <Transition name="chat-expand">
        <div
          v-if="expanded"
          class="tool-args"
        >
          <pre>{{ prettyArgs() }}</pre>
        </div>
      </Transition>
    </div>
  </div>
</template>

<!-- 行节奏 / chevron / 状态点 / 展开动画 / reduced-motion 来自全局 .chat-* 原语（style.css）；
     此处保留工具行独有的缩进导线、running sweep 流光与参数块 -->
<style scoped>
.tool-row-wrap { position: relative; }
.tree-guide { position: absolute; top: 0; bottom: 0; width: 1px; background: var(--border); }

.tool-row { overflow: hidden; border-radius: var(--sig-radius-code); }
.tool-row:hover { background: var(--bg-hover); }
.tool-name { font-family: var(--font-mono); font-size: 13px; color: var(--text-primary); font-weight: 400; white-space: nowrap; }
/* 家族着色：让一屏工具调用能被"扫"出来，而不是一片同色行。
   只按家族调色相、不引入新的颜色体系；未知变量一律带 fallback，缺 token 时退化为原色。 */
.tool-row-wrap[data-family='modify'] .tool-name { color: var(--warning, #d97706); }
.tool-row-wrap[data-family='explore'] .tool-name { color: var(--primary, #2563eb); }
.tool-row-wrap[data-family='delegate'] .tool-name { color: var(--trajectory-tool-call, var(--primary, #2563eb)); }
.tool-summary { flex: 1; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; color: var(--text-tertiary); font-size: 12px; }

/* running sweep 流光（deepseek ToolRow sweep） */
.tool-row-wrap[data-state='running'] .tool-row { position: relative; }
.tool-row-wrap[data-state='running'] .tool-row::after {
  content: ''; position: absolute; top: 0; bottom: 0; left: 0; width: 300px;
  background: linear-gradient(90deg, transparent 0%, color-mix(in srgb, var(--bg-page) 60%, transparent) 55%, transparent 100%);
  animation: toolRowSweep var(--dur-pulse) ease-out infinite; pointer-events: none;
}
@keyframes toolRowSweep { 0% { left: -300px; } 90%, 100% { left: 100%; } }
@media (prefers-reduced-motion: reduce) {
  .tool-row-wrap[data-state='running'] .tool-row::after { display: none; }
}

.tool-args { padding: 8px 12px; margin: 2px 8px 6px; background: var(--bg-secondary); border: 1px solid var(--border-card); border-radius: var(--sig-radius-button); }
.tool-args pre { margin: 0; font-family: var(--font-mono); font-size: 11px; color: var(--text-secondary); white-space: pre-wrap; word-break: break-all; }
@media (max-width: 576px) { .tool-args { margin: 2px 4px 6px; } }
</style>
