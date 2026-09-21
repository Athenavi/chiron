<script setup lang="ts">
/**
 * 通用悬浮窗外壳（设计稿：docs/floating-panels-design.md）。
 *
 * 为什么不用现成的 Popover：它表达不了本设计最关键的那条约束 ——
 * **向上长高，但最多到"输入区顶部"为止**。有了它，浮窗天然不会遮挡用户正在写的草稿。
 *
 * 只负责「位置 / 限高 / 焦点 / 动效」四件事：
 * - 定位：底部**向上弹出 + 右对齐**（`position: fixed` 贴右下角）；
 * - 限高：`bottomInset` 由父组件传入（= 输入区 + 状态栏高度），浮层最高只能长到那儿；
 * - 焦点：打开时移入首个可聚焦元素，关闭时**归还给触发按钮**（键盘用户不会"掉到页面顶部"）；
 * - 动效：淡入 + 轻微上移；`prefers-reduced-motion` 时只淡入不位移。
 *
 * **`Esc` 与点击外部不在这里处理**：全局优先级是
 * `停止生成 > 关闭浮窗 > 关闭侧栏`，必须由父组件的统一键盘处理决定，否则多个浮层会互相抢 Esc。
 */
import { computed, nextTick, ref, watch } from 'vue'

const props = defineProps<{
  open: boolean
  title: string
  /** 面板宽度（px）。统计 320 / 子 Agent 420（设计稿第三节） */
  width?: number
  /** 底部预留高度（输入区 + 状态栏）—— 浮层最高只能长到这里 */
  bottomInset?: number
}>()

const emit = defineEmits<{ (e: 'close'): void }>()

const root = ref<HTMLElement | null>(null)
let lastFocused: HTMLElement | null = null

const maxHeight = computed(() => {
  const inset = props.bottomInset ?? 0
  const viewport = typeof window !== 'undefined' ? window.innerHeight : 800
  // 再留 24px 呼吸位，避免浮层顶到视口边缘
  return `${Math.max(160, viewport - inset - 24)}px`
})

watch(
  () => props.open,
  async (open) => {
    if (!open) {
      // 关闭后归还焦点：键盘用户按 Esc 后应回到触发按钮，而不是被丢到页面顶部
      lastFocused?.focus?.()
      return
    }
    lastFocused = (document.activeElement as HTMLElement) || null
    await nextTick()
    root.value
      ?.querySelector<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      )
      ?.focus?.()
  },
)
</script>

<template>
  <Teleport to="body">
    <Transition name="fp">
      <section
        v-if="open"
        ref="root"
        class="fp"
        role="dialog"
        aria-modal="false"
        :aria-label="title"
        :style="{ width: `${width ?? 320}px`, maxHeight }"
      >
        <header class="fp-head">
          <span class="fp-title">{{ title }}</span>
          <button
            type="button"
            class="fp-close"
            :title="$t('关闭')"
            @click="emit('close')"
          >
            ✕
          </button>
        </header>
        <div class="fp-body">
          <slot />
        </div>
      </section>
    </Transition>
  </Teleport>
</template>

<style scoped>
/* 层级 / 圆角 / 阴影 / 间距全部复用既有令牌（裸数字会被 scripts/check-z-index-tokens.mjs 拦下） */
.fp {
  position: fixed;
  right: var(--space-3);
  bottom: var(--space-3);
  z-index: var(--z-dropdown);
  display: flex;
  flex-direction: column;
  background: var(--bg-elevated, var(--bg-page));
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}
.fp-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--border-subtle);
}
.fp-title {
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--text-primary);
}
.fp-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  padding: 0;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
}
.fp-close:hover {
  background: var(--bg-hover, var(--bg-page));
  color: var(--text-primary);
}
.fp-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

/* 动效：淡入 + 轻微上移；尊重 prefers-reduced-motion（只淡入不位移） */
.fp-enter-active,
.fp-leave-active {
  transition:
    opacity var(--dur-fast) var(--ease-out),
    transform var(--dur-fast) var(--ease-out);
}
.fp-enter-from,
.fp-leave-to {
  opacity: 0;
  transform: translateY(6px);
}
@media (prefers-reduced-motion: reduce) {
  .fp-enter-active,
  .fp-leave-active {
    transition: opacity var(--dur-fast) linear;
  }
  .fp-enter-from,
  .fp-leave-to {
    transform: none;
  }
}

/* 移动端（≤768px）：降级为**底部抽屉**（设计稿第五节）——
   贴上/左/右三边、圆角只在顶部，并覆盖内联的 width / maxHeight。 */
@media (max-width: 768px) {
  .fp {
    right: 0;
    left: 0;
    bottom: 0;
    width: 100% !important;
    max-height: 72vh !important;
    border-radius: var(--radius-2xl) var(--radius-2xl) 0 0;
    border-bottom: none;
  }
}
</style>
