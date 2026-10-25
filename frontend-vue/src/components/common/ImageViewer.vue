<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ZoomInOutlined, ZoomOutOutlined, DownloadOutlined, ExportOutlined, CloseOutlined, LeftOutlined, RightOutlined } from '@ant-design/icons-vue'
import { useImageViewer } from './imageViewerState'
import { closeLightbox, LIGHTBOX_VT_NAME } from './lightboxTransition'

const { current, list, index, hasMultiple, position, nextImage, prevImage } = useImageViewer()

const MIN_SCALE = 0.25
const MAX_SCALE = 6
const STEP = 0.25
/** ≤ 此缩放自动吸附回 1.0（CDS v1 §5.2.1 的 `v = 1.05`）：放大与否只该有两种状态，
 *  停在 1.03 这种"看起来没放大、又拖不动"的中间态是最差的。 */
const SNAP_BACK_AT = 1.05
/** 指针死区（`oe = 4`）：轻点一下或手抖不该被当成拖拽。 */
const DEAD_ZONE_PX = 4
/** 双击放大比例（`ce = 1.25`）。 */
const TAP_ZOOM = 1.25
/** 滚轮指数因子（`se = 0.01`）：`scale *= exp(-deltaY * 0.01)`。 */
const WHEEL_EXP = 0.01

const scale = ref(1)
/** 相对**视口中心**的平移量（px）。图片始终居中，位移叠加在 transform 上。 */
const tx = ref(0)
const ty = ref(0)
const dragging = ref(false)
/** 图片按 contain 适配后的**布局尺寸**。transform 不改布局，所以它可以稳定测量，
 *  锚点数学与边界钳制都以它为准。 */
const fitW = ref(0)
const fitH = ref(0)

const stageRef = ref<HTMLElement | null>(null)
const imgRef = ref<HTMLImageElement | null>(null)

const percent = computed(() => Math.round(scale.value * 100))
const canPan = computed(() => scale.value > SNAP_BACK_AT)

function clamp(v: number, limit: number) {
  return Math.min(limit, Math.max(-limit, v))
}

/** 按容器尺寸算 contain 后的显示尺寸（小图不放大 —— 1:1 才有意义）。 */
function measureFit() {
  const img = imgRef.value
  const stage = stageRef.value
  if (!img || !stage || !img.naturalWidth || !img.naturalHeight) return
  const cw = stage.clientWidth
  const ch = stage.clientHeight
  if (!cw || !ch) return
  const s = Math.min(cw / img.naturalWidth, ch / img.naturalHeight, 1)
  fitW.value = Math.round(img.naturalWidth * s)
  fitH.value = Math.round(img.naturalHeight * s)
}

/** 某缩放档下允许的最大平移（CDS v1 §5.2.3 的 maxTx 公式，含 1.05 处的淡出）。 */
function boundsFor(s: number) {
  const stage = stageRef.value
  if (!stage) return { maxTx: 0, maxTy: 0 }
  const fade = Math.min(1, Math.max(0, (s - SNAP_BACK_AT) / 0.95))
  return {
    maxTx: Math.max(0, (fitW.value * s - stage.clientWidth) / 2) * fade,
    maxTy: Math.max(0, (fitH.value * s - stage.clientHeight) / 2) * fade,
  }
}

function resetZoom() {
  scale.value = 1
  tx.value = 0
  ty.value = 0
}

/**
 * 以**光标为不动点**缩放（CDS v1 §5.2.3）。
 *
 * 这条公式解决的是"放大后想看的位置被推出屏幕"：目标缩放比 c 越大，
 * 旧平移量就越要按 c 反向补偿，这样光标下的那个像素在屏幕上原地不动。
 * 不传光标坐标时退化为"以中心缩放"（工具栏按钮与快捷键走这条）。
 */
function applyZoom(next: number, clientX?: number, clientY?: number) {
  const target = Math.min(MAX_SCALE, Math.max(MIN_SCALE, Number(next.toFixed(3))))
  const stage = stageRef.value
  if (!stage) {
    scale.value = target
    return
  }
  const rect = stage.getBoundingClientRect()
  const ox = clientX != null ? clientX - (rect.left + rect.width / 2) : 0
  const oy = clientY != null ? clientY - (rect.top + rect.height / 2) : 0
  const c = target / scale.value
  const { maxTx, maxTy } = boundsFor(target)
  const idealTx = ox - (ox - tx.value) * c
  const idealTy = oy - (oy - ty.value) * c
  scale.value = target
  if (target <= SNAP_BACK_AT) {
    // 没有可平移空间：顺手把残留位移清零，避免"缩回 100% 却歪着"
    tx.value = 0
    ty.value = 0
  } else {
    tx.value = clamp(idealTx, maxTx)
    ty.value = clamp(idealTy, maxTy)
  }
}

/** 工具栏与快捷键用的台阶缩放（以中心为锚点）。 */
function zoomStep(delta: number) {
  applyZoom(scale.value + delta)
}

function onWheel(e: WheelEvent) {
  // Ctrl/⌘ + 滚轮交还浏览器（那是页面级缩放，不是看图缩放）
  if (e.ctrlKey || e.metaKey) return
  e.preventDefault()
  const next = scale.value * Math.exp(-e.deltaY * WHEEL_EXP)
  if (next <= SNAP_BACK_AT) {
    resetZoom()
    return
  }
  applyZoom(next, e.clientX, e.clientY)
}

let dragStart: { x: number; y: number; tx: number; ty: number } | null = null
let dragActive = false

function onPointerDown(e: PointerEvent) {
  if (e.button !== 0 || !canPan.value) return
  dragStart = { x: e.clientX, y: e.clientY, tx: tx.value, ty: ty.value }
  dragActive = false
  ;(e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId)
}

function onPointerMove(e: PointerEvent) {
  if (!dragStart) return
  const dx = e.clientX - dragStart.x
  const dy = e.clientY - dragStart.y
  if (!dragActive) {
    if (Math.hypot(dx, dy) < DEAD_ZONE_PX) return   // 还在死区里：不算拖拽
    dragActive = true
    dragging.value = true
  }
  const { maxTx, maxTy } = boundsFor(scale.value)
  tx.value = clamp(dragStart.tx + dx, maxTx)
  ty.value = clamp(dragStart.ty + dy, maxTy)
}

function onPointerUp(e: PointerEvent) {
  ;(e.currentTarget as HTMLElement).releasePointerCapture?.(e.pointerId)
  dragStart = null
  dragActive = false
  dragging.value = false
}

/** 双击：已放大则复位，未放大则以点击处为锚点跳到 1.25 倍。 */
function onDblClick(e: MouseEvent) {
  if (canPan.value) resetZoom()
  else applyZoom(TAP_ZOOM, e.clientX, e.clientY)
}

function onKeydown(event: KeyboardEvent) {
  if (!current.value) return
  if (event.key === 'Escape') void closeLightbox()
  else if (event.key === '+' || event.key === '=') zoomStep(STEP)
  else if (event.key === '-') zoomStep(-STEP)
  else if (event.key === '0') resetZoom()
  // ←/→ 切图（单图时 nextImage/prevImage 内部直接返回，不会有副作用）
  else if (event.key === 'ArrowLeft') prevImage()
  else if (event.key === 'ArrowRight') nextImage()
}

/** 预加载前后各两张（手册的"跑道"预热）：切换时不该看到空白等待。 */
watch(index, (i) => {
  const total = list.value.length
  if (total < 2) return
  for (const d of [-2, -1, 1, 2]) {
    const target = list.value[(i + d + total) % total]
    if (!target?.src) continue
    const pre = new Image()
    pre.src = target.src
  }
})

// 换图时复位（沿用上一张的缩放与位移会很困惑），并重新量一次适配尺寸
watch(current, async () => {
  resetZoom()
  await new Promise(r => requestAnimationFrame(r))
  measureFit()
})

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  window.addEventListener('resize', measureFit)
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('resize', measureFit)
})
</script>

<template>
  <Teleport to="body">
    <div
      v-if="current"
      class="viewer-mask"
      role="dialog"
      aria-modal="true"
      :aria-label="$t('media.image_viewer')"
      @click.self="closeLightbox"
    >
      <div class="viewer-bar">
        <span class="viewer-name">{{ current.alt || $t('media.image') }}</span>
        <span
          v-if="hasMultiple"
          class="viewer-count"
        >{{ position.index + 1 }} / {{ position.total }}</span>
        <span class="viewer-percent">{{ percent }}%</span>
        <button
          class="viewer-btn"
          type="button"
          :title="$t('common.zoom_out')"
          @click="zoomStep(-STEP)"
        >
          <ZoomOutOutlined />
        </button>
        <button
          class="viewer-btn"
          type="button"
          :title="$t('common.zoom_in')"
          @click="zoomStep(STEP)"
        >
          <ZoomInOutlined />
        </button>
        <button
          class="viewer-btn"
          type="button"
          :title="$t('common.original_size_0')"
          @click="resetZoom"
        >
          1:1
        </button>
        <a
          class="viewer-btn"
          :href="current.src"
          target="_blank"
          rel="noopener noreferrer"
          :title="$t('common.open_in_new_tab')"
        >
          <ExportOutlined />
        </a>
        <a
          class="viewer-btn"
          :href="current.src"
          :download="current.alt || 'image'"
          :title="$t('common.download')"
        >
          <DownloadOutlined />
        </a>
        <button
          class="viewer-btn"
          type="button"
          :title="$t('common.close_esc')"
          @click="closeLightbox"
        >
          <CloseOutlined />
        </button>
      </div>
      <!-- 舞台承接全部手势：滚轮（锚点缩放）、指针拖拽（4px 死区）、双击（1.25x / 复位）。
           图片只负责显示，transform 里叠加「平移 + 缩放」两层。 -->
      <div
        ref="stageRef"
        class="viewer-stage"
        :class="{ 'can-pan': canPan, dragging }"
        @wheel="onWheel"
        @pointerdown="onPointerDown"
        @pointermove="onPointerMove"
        @pointerup="onPointerUp"
        @pointercancel="onPointerUp"
        @dblclick="onDblClick"
      >
        <img
          ref="imgRef"
          :key="position.index"
          class="viewer-img"
          :src="current.src"
          :alt="current.alt || ''"
          draggable="false"
          :style="{
            width: fitW ? `${fitW}px` : undefined,
            height: fitH ? `${fitH}px` : undefined,
            transform: `translate3d(${tx}px, ${ty}px, 0) scale(${scale})`,
            viewTransitionName: LIGHTBOX_VT_NAME,
          }"
          @load="measureFit"
        >
      </div>
      <!-- 图集导航：只在多图时出现。贴左右边缘、避开工具栏，且不拦截舞台手势
           （兄弟节点而非子节点，pointerdown 不会冒泡进舞台的拖拽处理）。 -->
      <button
        v-if="hasMultiple"
        class="viewer-nav prev"
        type="button"
        :title="$t('common.previous')"
        :aria-label="$t('common.previous')"
        @click="prevImage"
      >
        <LeftOutlined />
      </button>
      <button
        v-if="hasMultiple"
        class="viewer-nav next"
        type="button"
        :title="$t('common.next')"
        :aria-label="$t('common.next')"
        @click="nextImage"
      >
        <RightOutlined />
      </button>
    </div>
  </Teleport>
</template>

<style scoped>
.viewer-mask {
  position: fixed; inset: 0; z-index: var(--z-viewer);
  display: flex; flex-direction: column; align-items: center;
  padding: 16px;
  background: var(--viewer-scrim);
}
.viewer-bar {
  position: relative; z-index: var(--z-content);
  display: flex; align-items: center; gap: 6px;
  max-width: 100%; padding: 6px 10px; margin-bottom: 12px;
  border-radius: var(--radius-full);
  background: var(--viewer-scrim);
  color: var(--on-solid);
}
.viewer-name { max-width: 40vw; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.viewer-count { font-size: 12px; font-variant-numeric: tabular-nums; opacity: 0.75; }
.viewer-percent { min-width: 44px; text-align: end; font-size: 12px; font-variant-numeric: tabular-nums; opacity: 0.75; }
.viewer-btn {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 28px; height: 28px; padding: 0 6px;
  border: none; border-radius: var(--radius-sm);
  background: none; color: inherit; font-size: 12px; cursor: pointer;
  text-decoration: none;
  transition: color var(--dur-fast) ease, background-color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.viewer-btn:hover { background: var(--on-viewer-faint); }
.viewer-btn:focus-visible { outline: 2px solid var(--on-solid); outline-offset: 1px; }
/* 舞台：占满工具栏之外的全部区域并裁掉溢出 —— 平移与缩放都在内部完成，
   不再靠遮罩层的 auto 溢出滚动（那样锚点缩放会与原生滚动打架）。 */
.viewer-stage {
  position: relative; flex: 1; width: 100%; min-height: 0;
  display: flex; align-items: center; justify-content: center;
  overflow: hidden;
  touch-action: none;          /* 交给 pointer 事件，禁止浏览器的滚动/缩放手势抢走 */
  user-select: none;
}
.viewer-stage.can-pan { cursor: grab; }
.viewer-stage.dragging { cursor: grabbing; }
.viewer-img {
  max-width: none; max-height: none;
  transform-origin: center center;
  transition: transform var(--dur-fast) ease-out;
  will-change: transform;
  /* 切图时整张重挂载（:key=index），这层淡入就是"换了一张"的提示。
     不碰 transform —— 那是内联的缩放/平移，动画覆盖会把缩放吃掉。 */
  animation: cdsViewerSwap var(--dur-fast) var(--ease-out);
}
@keyframes cdsViewerSwap {
  from { opacity: 0; }
  to { opacity: 1; }
}
/* 图集导航：贴左右边缘、垂直居中。position: fixed 是刻意的 ——
   .viewer-mask 是 flex column，绝对定位的兄弟会被 flex 布局拉进流里。 */
.viewer-nav {
  position: fixed; top: 50%; z-index: var(--z-content);
  display: inline-flex; align-items: center; justify-content: center;
  width: 40px; height: 40px; padding: 0;
  border: none; border-radius: var(--radius-full);
  background: var(--viewer-scrim); color: var(--on-solid);
  font-size: 16px; cursor: pointer;
  transform: translateY(-50%);
  opacity: 0.6; transition: opacity var(--dur-fast) var(--ease-out);
}
.viewer-nav:hover { opacity: 1; }
.viewer-nav:focus-visible { outline: 2px solid var(--on-solid); outline-offset: 2px; }
.viewer-nav.prev { left: 24px; }
.viewer-nav.next { right: 24px; }
/* 拖拽期间禁用过渡：否则位移会"追着"指针走，手感发黏 */
.viewer-stage.dragging .viewer-img { transition: none; }
@media (prefers-reduced-motion: reduce) { .viewer-img { transition: none; animation: none; } }
</style>
