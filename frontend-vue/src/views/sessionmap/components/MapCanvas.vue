<script setup lang="ts">
/**
 * 会话地图画布。
 *
 * 用 `@vue-flow/core` 而不是继续手写：旧实现（`app.js:616-710`）自己实现了相机变换、
 * 碰撞检测、卡片挂载与连线路由，400+ 行几何代码零测试覆盖。这些正是 Vue Flow 已经
 * 解决的问题，而且它自带键盘可达性与 ARIA（`aria-label`、roving tabindex）。
 *
 * ## 主题适配
 *
 * Vue Flow 的默认配色是写死在 `theme-default.css` 里的，但它把可覆盖的钩子暴露成了
 * CSS 变量（`--vf-node-bg` / `--vf-node-text` / `--vf-connection-path` / `--vf-handle`）。
 * 本组件在画布根节点上把这几个变量重绑到 Chiron 的主题 token，于是：
 *   - 四套主题（notion / paper / terminal / aurora）× light/dark 切换时，
 *     `data-theme` 变化 → token 变化 → Vue Flow 内部颜色自动跟随；
 *   - 不需要改库源码，也不需要写 `:deep()` 去硬压样式。
 *
 * 本文件里**不出现任何硬编码色值**：`check-theme-token-contract.mjs` 的基线为空，
 * 新增一处 `#rrggbb` / `rgba(...)` 就会让 `npm run check:ui` 失败。
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { VueFlow } from '@vue-flow/core'
import type { Edge, Node } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'

import SessionCard from './SessionCard.vue'
import type { MapCard, MapEdge } from '../useSessionMapGraph'

const props = defineProps<{
  cards: MapCard[]
  edges: MapEdge[]
  /** 只读模式：禁用拖拽与连线（分享视图用） */
  readonly?: boolean
  /** 当前高亮的卡片 id（详情面板选中的那张） */
  activeId?: string
  /**
   * 会话 id → 正在流式输出的尾部文本。
   *
   * 与 `cards` 分开传是刻意的：`cards` 由节点持久化数据投影而来（拖拽/折叠/血缘都
   * 基于它），而这里是每几十毫秒变一次的运行态。混在一起会让画布把每次 chunk
   * 都当成图结构变化，重新走一遍节点 diff。
   */
  streamPreviews?: Record<string, string>
}>()

const emit = defineEmits<{
  'move-card': [payload: { id: string; x: number; y: number }]
  'select-card': [id: string]
  'toggle-collapse': [id: string]
  'open-detail': [id: string]
  fork: [id: string]
  followup: [id: string]
}>()

const { t } = useI18n()

/**
 * 卡片 → Vue Flow 节点。
 *
 * `data` 里放整张 `MapCard`，自定义节点组件直接吃它，避免"再复制一遍字段"
 * 造成的两处 drift。位置直接用后端持久化的 x/y：会话地图的坐标是用户拖出来的，
 * 不能由前端自动布局覆盖（这一点与工作流画布相反）。
 */
const nodes = computed<Node<MapCard>[]>(() =>
  props.cards.map(card => ({
    id: card.id,
    type: 'map-card',
    position: { x: card.x, y: card.y },
    data: card,
    // Vue Flow 会把 selected 反映到 ARIA；由 activeId 单向控制，避免内部状态分叉
    selected: card.id === props.activeId,
    draggable: !props.readonly,
    connectable: false,
  })),
)

/**
 * 连线。
 *
 * 端点可能在折叠后被隐藏 —— 父组件已经裁过这类边（`buildMapGraph` 只输出两端可见的边），
 * 所以这里不再二次过滤，保持单一职责。
 */
const flowEdges = computed<Edge[]>(() =>
  props.edges.map(edge => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    type: 'smoothstep',
    // 分叉血缘用虚线，普通引用用实线：用户一眼能分清"这是同一个会话的延续"还是"从这里分出去的"
    animated: false,
    style: edge.kind === 'branch' ? { strokeDasharray: '4 3' } : undefined,
    data: { kind: edge.kind },
  })),
)

/** 拖拽结束才写回：`node-drag` 每帧都触发，逐帧保存会把后端打满。 */
function onNodeDragStop(payload: { node: Node }) {
  const { id, position } = payload.node
  emit('move-card', { id, x: Math.round(position.x), y: Math.round(position.y) })
}

function onNodeClick(payload: { node: Node }) {
  emit('select-card', payload.node.id)
}
</script>

<template>
  <div class="map-canvas">
    <VueFlow
      :nodes="nodes"
      :edges="flowEdges"
      :nodes-draggable="!readonly"
      :nodes-connectable="false"
      :elements-selectable="true"
      :min-zoom="0.2"
      :max-zoom="2"
      :fit-view-on-init="cards.length > 0"
      :default-edge-options="{ type: 'smoothstep' }"
      class="map-canvas-flow"
      @node-drag-stop="onNodeDragStop"
      @node-click="onNodeClick"
    >
      <Background
        :gap="16"
        :size="1"
      />
      <Controls :show-interactive="false" />
      <MiniMap
        pannable
        zoomable
      />

      <template #node-map-card="nodeProps">
        <SessionCard
          :card="nodeProps.data"
          :editable="!readonly"
          :preview="streamPreviews?.[nodeProps.data.sessionId] ?? ''"
          @toggle-collapse="emit('toggle-collapse', nodeProps.id)"
          @open-detail="emit('open-detail', nodeProps.id)"
          @fork="emit('fork', nodeProps.id)"
          @followup="emit('followup', nodeProps.id)"
        />
      </template>
    </VueFlow>

    <!-- 空画布：不给一句提示的话，用户会以为是加载失败 -->
    <p
      v-if="cards.length === 0"
      class="map-canvas-empty"
    >
      {{ t('chat.sessionmap.empty') }}
    </p>

    <!-- 触屏/窄屏提示：Vue Flow 的拖拽在触摸设备上是"平移画布"，与鼠标直觉不同 -->
    <p class="map-canvas-hint">
      {{ t('chat.sessionmap.canvasHint') }}
    </p>
  </div>
</template>

<!--
  非 scoped 块：Vue Flow 的样式表必须全局引入（库内部元素不在本组件的 scope 里）。
-->
<style>
@import '@vue-flow/core/dist/style.css';
@import '@vue-flow/core/dist/theme-default.css';
@import '@vue-flow/controls/dist/style.css';
@import '@vue-flow/minimap/dist/style.css';
</style>

<style scoped>
/*
  主题桥接层：把 Vue Flow 暴露的 CSS 变量指向 Chiron token。
  token 本身随 data-theme 变化（App.vue 的 watchEffect），因此这里只需要绑定一次，
  就能让画布的背景、节点文字、连线、连接点跟随四套主题 × light/dark 一起切换。
*/
.map-canvas {
  /* Vue Flow 实际暴露的可覆盖变量只有 6 个（见 theme-default.css 开头），
     名字写错不会报错、只会静默无效 —— 所以这里逐一对齐，不多写。 */
  --vf-node-bg: var(--bg-card);
  --vf-node-text: var(--text-primary);
  --vf-node-color: var(--border-default);
  --vf-connection-path: var(--border-strong);
  --vf-handle: var(--primary);
  --vf-box-shadow: var(--shadow-sm);

  position: relative;
  block-size: 100%;
  inline-size: 100%;
  /* 画布底色用 --bg-base，与卡片（--bg-card）拉开一档层次 */
  background: var(--bg-base);
  border-radius: var(--radius-lg);
  overflow: hidden;
}

.map-canvas-flow {
  block-size: 100%;
  inline-size: 100%;
  /* 画布内部同样不写死颜色：背景点阵也走 token */
  background: var(--bg-base);
}

/* 连线颜色跟随主题；分叉虚线由 JS 的 strokeDasharray 控制，颜色统一在这里 */
.map-canvas :deep(.vue-flow__edge-path) {
  stroke: var(--border-strong);
  stroke-width: 1.5;
}

.map-canvas :deep(.vue-flow__edge.selected .vue-flow__edge-path),
.map-canvas :deep(.vue-flow__edge:hover .vue-flow__edge-path) {
  stroke: var(--primary);
}

/* 背景点阵：默认是硬编码的浅灰，这里换成主题的细边框色 */
.map-canvas :deep(.vue-flow__background) {
  color: var(--border-subtle);
}

/* 控件与小地图：库用的是白色底 + 黑图标，深色主题下会刺眼 */
.map-canvas :deep(.vue-flow__controls) {
  box-shadow: var(--shadow-sm);
}

.map-canvas :deep(.vue-flow__controls-button) {
  background: var(--bg-card);
  border-block-end: var(--hairline-width) solid var(--border-subtle);
  color: var(--text-secondary);
  fill: var(--text-secondary);
}

.map-canvas :deep(.vue-flow__controls-button:hover) {
  background: var(--bg-secondary);
}

.map-canvas :deep(.vue-flow__minimap) {
  background: var(--bg-card);
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-md);
}

.map-canvas :deep(.vue-flow__minimap-mask) {
  fill: var(--bg-secondary);
  fill-opacity: 0.7;
}

/* 选中态：用主题 accent 的描边，不用库默认的深蓝色 */
.map-canvas :deep(.vue-flow__node.selected) {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-lg);
}

.map-canvas :deep(.vue-flow__node:focus-visible) {
  outline: 2px solid var(--border-focus);
  outline-offset: 2px;
}

.map-canvas-empty {
  position: absolute;
  inset-block-start: 50%;
  inset-inline-start: 50%;
  transform: translate(-50%, -50%);
  margin: 0;
  color: var(--text-tertiary);
  font-size: var(--fs-sm);
  pointer-events: none;
}

/*
  移动端提示条：触摸设备上画布手势与页面滚动会打架，明确告知"双指缩放、单指拖动"
  比让用户自己试更省事。用 --z-local 而不是裸 z-index（check-z-index-tokens 会拦截）。
*/
.map-canvas-hint {
  display: none;
  position: absolute;
  inset-block-end: 8px;
  inset-inline: 8px;
  z-index: var(--z-local);
  margin: 0;
  padding: 6px 10px;
  border-radius: var(--radius-md);
  background: var(--bg-secondary);
  color: var(--text-tertiary);
  font-size: var(--fs-xs);
  text-align: center;
  pointer-events: none;
}

@media (max-width: 768px) {
  .map-canvas-hint {
    display: block;
  }

  /* 小屏上小地图纯占地方：Vue Flow 的 MiniMap 有自带 class，直接隐藏 */
  .map-canvas :deep(.vue-flow__minimap) {
    display: none;
  }
}
</style>
