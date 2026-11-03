<script setup lang="ts">
/**
 * 地图上的一张卡片 = 一个会话。
 *
 * 与旧实现（`app.js:984-1050` 的模板字符串）的差别不是"换了个框架"，而是三件具体的事：
 *   1. **不再拼 HTML 字符串**：旧实现每张卡片都是一段含 `escapeHtml(...)` 的模板字面量，
 *      漏一处就是 XSS；这里由 Vue 的文本插值转义。
 *   2. **不再整树重渲染**：旧实现每次交互都 `app.innerHTML = ...`，输入焦点与滚动位置全丢。
 *   3. **不再用 `data-action` 字符串分发**：事件是类型化的 emit，改名会编译失败。
 *
 * 所有颜色走 `var(--*)` 主题 token —— `check-theme-token-contract.mjs` 的基线为空，
 * 任何硬编码色值都会直接失败，这也保证了四套主题 × light/dark 都能正确跟随。
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Handle, Position } from '@vue-flow/core'

import type { MapCard } from '../useSessionMapGraph'

const props = defineProps<{
  card: MapCard
  /** 画布是否允许编辑（只读模式下隐藏写操作入口） */
  editable?: boolean
  /**
   * 正在流式输出的尾部文本（宿主运行态，由 `SessionMapView` 透传）。
   *
   * 刻意**不放进 `MapCard`**：`MapCard` 是节点持久化数据的投影，而预览每几十毫秒
   * 就变一次。混进去会让血缘/折叠这些基于图结构的计算带上高频抖动的字段，
   * 也会让「布局已保存」的比较逻辑误判为脏数据。
   */
  preview?: string
}>()

const emit = defineEmits<{
  'toggle-collapse': []
  'open-detail': []
  fork: []
  followup: []
}>()

const { t } = useI18n()

/** 血缘徽标：分叉点必须显示，否则用户分不清两张长得一样的卡片。 */
const branchLabel = computed(() => {
  const { branchFromSeq, parentTitle } = props.card
  if (branchFromSeq <= 0) return ''
  return parentTitle
    ? t('chat.sessionmap.branchFrom', { title: parentTitle, seq: branchFromSeq })
    : t('chat.sessionmap.branchFromUnknown', { seq: branchFromSeq })
})

/** 折叠按钮的提示文案：带上后代数量，用户才知道点下去会藏掉多少。 */
const collapseLabel = computed(() =>
  props.card.collapsed
    ? t('chat.sessionmap.expandChildren', { n: props.card.descendantCount })
    : t('chat.sessionmap.collapseChildren', { n: props.card.descendantCount }),
)
</script>

<template>
  <div
    class="map-card"
    :class="{
      'is-running': card.running,
      'is-collapsed': card.collapsed,
      'is-detached': !card.hasSession,
      'is-pinned': card.pinned,
    }"
  >
    <Handle
      type="target"
      :position="Position.Top"
      class="map-card-handle"
    />

    <header class="map-card-head">
      <span
        v-if="card.pinned"
        class="map-card-pin"
        aria-hidden="true"
      >★</span>
      <span
        class="map-card-title"
        :title="card.title"
      >{{ card.title || t('chat.sessionmap.untitled') }}</span>
      <span
        v-if="card.running"
        class="map-card-pulse"
        :aria-label="t('chat.sessionmap.running')"
        role="status"
      />
    </header>

    <p
      v-if="branchLabel"
      class="map-card-branch"
    >
      {{ branchLabel }}
    </p>

    <p
      v-if="card.tag"
      class="map-card-tag"
    >
      {{ card.tag }}
    </p>

    <p
      v-if="!card.hasSession"
      class="map-card-detached"
    >
      {{ t('chat.sessionmap.detached') }}
    </p>

    <!--
      实时流式预览：旧 iframe 版靠 postMessage 把每个 chunk 推进画布，改造时随
      iframe 一起被删掉了，于是卡片只剩脉冲点、看不到文字。这里直接读宿主投影
      出来的尾部文本（见 streamPreview.ts），不新建第二份缓冲区。
    -->
    <p
      v-if="card.running && preview"
      class="map-card-preview"
      role="status"
    >
      {{ preview }}
    </p>

    <footer class="map-card-actions">
      <button
        v-if="card.childCount > 0"
        type="button"
        class="map-card-btn"
        :aria-expanded="!card.collapsed"
        :aria-label="collapseLabel"
        :title="collapseLabel"
        @click.stop="emit('toggle-collapse')"
      >
        <span aria-hidden="true">{{ card.collapsed ? '+' : '−' }}</span>
        <span
          v-if="card.collapsed && card.descendantCount > 0"
          class="map-card-count"
        >{{ card.descendantCount }}</span>
      </button>

      <button
        type="button"
        class="map-card-btn"
        :aria-label="t('chat.sessionmap.openDetail')"
        :title="t('chat.sessionmap.openDetail')"
        @click.stop="emit('open-detail')"
      >
        <span aria-hidden="true">⋯</span>
      </button>

      <button
        v-if="editable && card.hasSession"
        type="button"
        class="map-card-btn"
        :aria-label="t('chat.sessionmap.followupHere')"
        :title="t('chat.sessionmap.followupHere')"
        @click.stop="emit('followup')"
      >
        <span aria-hidden="true">↩</span>
      </button>

      <button
        v-if="editable && card.hasSession"
        type="button"
        class="map-card-btn"
        :aria-label="t('chat.sessionmap.forkHere')"
        :title="t('chat.sessionmap.forkHere')"
        @click.stop="emit('fork')"
      >
        <span aria-hidden="true">⑂</span>
      </button>
    </footer>

    <Handle
      type="source"
      :position="Position.Bottom"
      class="map-card-handle"
    />
  </div>
</template>

<style scoped>
/* 卡片本体：圆角、描边、底色全部来自主题 token。
   --vf-node-bg / --vf-node-text 由画布容器统一重绑（见 MapCanvas.vue），
   这里显式给 background 是为了让 scoped 样式不依赖库的默认值。 */
.map-card {
  width: 220px;
  padding: 10px 12px;
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
  color: var(--text-primary);
  box-shadow: var(--shadow-sm);
  text-align: start;
  transition: border-color var(--dur-fast), box-shadow var(--dur-fast);
}

.map-card:hover {
  border-color: var(--primary);
  box-shadow: var(--shadow-md);
}

/* 运行中：用主题的 accent 描边 + 脉冲，不写死颜色 */
.map-card.is-running {
  border-color: var(--accent);
  animation: map-card-pulse var(--dur-pulse) var(--ease-in-out) infinite;
}

@keyframes map-card-pulse {
  0%, 100% { box-shadow: 0 0 0 0 var(--accent-bg); }
  50% { box-shadow: 0 0 0 4px var(--accent-bg); }
}

/* 折叠态：虚线描边，暗示"下面还有内容被收起来了" */
.map-card.is-collapsed {
  border-style: dashed;
}

/* 未绑定会话的占位卡：整体降透明度，且不可追问/分叉 */
.map-card.is-detached {
  border-style: dotted;
  opacity: 0.72;
}

.map-card.is-pinned {
  border-color: var(--border-strong);
}

.map-card-head {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.map-card-pin {
  flex-shrink: 0;
  color: var(--warning);
  font-size: var(--fs-xs);
}

.map-card-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: var(--fw-semibold);
  font-size: var(--fs-sm);
  color: var(--text-primary);
}

/* 脉冲点：纯 CSS 动画，不依赖 JS 定时器（旧实现每 100ms 重建整页） */
.map-card-pulse {
  flex-shrink: 0;
  width: 6px;
  height: 6px;
  border-radius: var(--radius-full);
  background: var(--accent);
  animation: map-card-dot var(--dur-pulse) var(--ease-in-out) infinite;
}

@keyframes map-card-dot {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}

.map-card-branch,
.map-card-tag,
.map-card-detached {
  margin: 6px 0 0;
  font-size: var(--fs-xs);
  line-height: var(--lh-snug);
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.map-card-branch {
  color: var(--text-link);
}

/*
  流式预览：限制两行，避免输出变长时卡片高度不断跳动 —— 画布上高度抖动会让
  连线端点跟着移动，看起来像整张图在抽搐。用 line-clamp 裁剪而不是截断字符串，
  这样换语言/改变字号也不会溢出。
*/
.map-card-preview {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
  margin: 6px 0 0;
  font-size: var(--fs-xs);
  line-height: var(--lh-snug);
  color: var(--text-secondary);
  overflow-wrap: anywhere;
  /* 旧实现这里会整页重渲染；用 CSS 过渡让文本变化不产生视觉跳变 */
  transition: opacity var(--dur-fast);
}

.map-card-tag {
  display: inline-block;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  background: var(--bg-secondary);
  color: var(--text-secondary);
}

.map-card-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 8px;
}

.map-card-btn {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 2px 6px;
  border: var(--hairline-width) solid transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-tertiary);
  font-size: var(--fs-xs);
  line-height: 1.4;
  cursor: pointer;
  transition: background var(--dur-fast), color var(--dur-fast);
}

.map-card-btn:hover {
  background: var(--bg-secondary);
  color: var(--primary);
  border-color: var(--border-subtle);
}

.map-card-btn:focus-visible {
  outline: 2px solid var(--border-focus);
  outline-offset: 1px;
}

.map-card-count {
  font-variant-numeric: tabular-nums;
}

/* 连接点：跟着主题走（Vue Flow 用 --vf-handle 控制默认色，这里显式覆盖以保证对比度） */
.map-card-handle {
  width: 8px;
  height: 8px;
  background: var(--primary);
  border: var(--hairline-width) solid var(--bg-card);
}

/* RTL：一律用逻辑属性，物理 left/right 会在阿拉伯语下整体镜像错位 */
.map-card {
  margin-inline: 0;
}
</style>
