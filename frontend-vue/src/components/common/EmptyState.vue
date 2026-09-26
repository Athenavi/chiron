<script setup lang="ts">
import { computed, type Component } from 'vue'

interface Props {
  /** 图标：Ant Design 图标组件或任意组件，留空显示默认空盒 */
  icon?: Component
  /** 主文案，如「暂无知识库」 */
  description?: string
  /** 辅助文案，如「点击右上角创建第一个知识库」 */
  hint?: string
  /** 尺寸：list 用于列表内紧凑，page 用于整页空状态 */
  size?: 'list' | 'page'
  /** 是否禁用自动内边距（由父级控制时设为 false） */
  autoPad?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  size: 'list',
  autoPad: true,
})

const wrapClass = computed(() => [
  'empty-state',
  `empty-state--${props.size}`,
  { 'empty-state--auto-pad': props.autoPad },
])
</script>

<template>
  <div
    :class="wrapClass"
    role="status"
    aria-live="polite"
  >
    <div class="empty-state-icon">
      <component
        :is="icon"
        v-if="icon"
      />
      <svg
        v-else
        viewBox="0 0 64 48"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
        aria-hidden
      >
        <rect
          x="6"
          y="8"
          width="52"
          height="36"
          rx="6"
          stroke="currentColor"
          stroke-width="1.5"
          stroke-dasharray="3 3"
          opacity="0.5"
        />
        <path
          d="M22 26 L28 20 L34 26 M30 20 V34"
          stroke="currentColor"
          stroke-width="1.5"
          stroke-linecap="round"
          stroke-linejoin="round"
          opacity="0.6"
        />
      </svg>
    </div>
    <div
      v-if="description"
      class="empty-state-desc"
    >
      {{ description }}
    </div>
    <div
      v-if="hint"
      class="empty-state-hint"
    >
      {{ hint }}
    </div>
    <div
      v-if="$slots.default"
      class="empty-state-actions"
    >
      <slot />
    </div>
  </div>
</template>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  color: var(--text-muted);
}
.empty-state--auto-pad { padding: 40px 16px; }
.empty-state--list { padding: 32px 16px; min-height: 160px; }
.empty-state--page { padding: 72px 24px; min-height: 320px; }

.empty-state-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 56px;
  height: 56px;
  border-radius: 50%;
  background: var(--bg-secondary);
  color: var(--text-tertiary);
  margin-bottom: 14px;
}
.empty-state-icon :deep(svg) { width: 28px; height: 28px; }
.empty-state--page .empty-state-icon { width: 72px; height: 72px; }
.empty-state--page .empty-state-icon :deep(svg) { width: 36px; height: 36px; }

.empty-state-desc {
  font-size: 14px;
  font-weight: 500;
  color: var(--text-secondary);
}
.empty-state-hint {
  margin-top: 4px;
  font-size: 13px;
  color: var(--text-tertiary);
  max-width: 360px;
  line-height: 20px;
}
.empty-state-actions { margin-top: 16px; }

/* 响应式：窄屏收敛留白，按钮/操作换行 */
@media (max-width: 576px) {
  .empty-state--page { padding: 48px 16px; min-height: 240px; }
  .empty-state--list { min-height: 140px; }
  .empty-state-actions { width: 100%; display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; }
  .empty-state-hint { max-width: 280px; }
}
</style>
