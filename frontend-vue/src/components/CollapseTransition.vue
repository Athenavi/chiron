<!-- CollapseTransition - 折叠展开动画组件 -->
<template>
  <transition name="collapse">
    <slot />
  </transition>
</template>

<script setup lang="ts">
</script>

<style>
/* 折叠展开动画：轻量 ≤200ms。
   注意：此组件通过 <slot /> 渲染调用方内容，scoped 样式无法命中插槽元素，
   因此这里必须使用非 scoped 样式，动画类才能作用于实际过渡元素。 */
.collapse-enter-active,
.collapse-leave-active {
  transition: all var(--dur-normal) ease;
  overflow: hidden;
  transform-origin: top;
}

.collapse-enter-from,
.collapse-leave-to {
  opacity: 0;
  transform: scaleY(0);
}

.collapse-enter-to,
.collapse-leave-from {
  opacity: 1;
  transform: scaleY(1);
}

@media (prefers-reduced-motion: reduce) {
  .collapse-enter-active,
  .collapse-leave-active {
    transition: none;
  }
}
</style>
