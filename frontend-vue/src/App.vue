<script setup lang="ts">
import { computed, onMounted, watchEffect } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ConfigProvider, theme } from 'ant-design-vue'
import AppLayout from './components/AppLayout.vue'
import RouteProgressBar from './components/common/RouteProgressBar.vue'
import ErrorBoundary from './components/common/ErrorBoundary.vue'
import ImageViewer from './components/common/ImageViewer.vue'
import { useThemeStore } from './stores/theme'
import { useTypographyStore } from './stores/typography'
import { languageMeta } from './i18n/languages'

const route = useRoute()
const { locale } = useI18n()
// ant-design-vue 组件库语言（分页器/日期选择器/空态等）必须与界面语言同步，
// 否则会出现"界面英文、组件中文"的混搭。
const antdLocale = computed(() => languageMeta(locale.value).antd)
const themeStore = useThemeStore()
// 实例化即应用持久化的排版偏好（store 内 immediate watch 写入 <html> 变量）
useTypographyStore()
const showLayout = computed(() => !['Login', 'Register'].includes(route.name as string))

const themeConfig = computed(() => ({
  algorithm: themeStore.isDark ? theme.darkAlgorithm : theme.defaultAlgorithm,
  token: themeStore.antdTokens,
}))

onMounted(() => {
  themeStore.init()
})

watchEffect(() => {
  const root = document.documentElement
  root.setAttribute('data-theme', themeStore.cssThemeId)
  if (themeStore.isDark) root.classList.add('dark')
  else root.classList.remove('dark')
})
</script>

<template>
  <ConfigProvider
    :theme="themeConfig"
    :locale="antdLocale"
  >
    <RouteProgressBar />
    <ErrorBoundary>
      <AppLayout v-if="showLayout" />
      <router-view v-else />
    </ErrorBoundary>
    <!-- 全局单例：任何位置的图片都能唤起同一个查看器 -->
    <ImageViewer />
  </ConfigProvider>
</template>

<style>
html, body, #app {
  width: 100%;
  height: 100%;
  margin: 0;
  padding: 0;
}

#app {
  min-height: 100dvh;
  background: var(--bg-page);
  color: var(--text-primary);
  transition: background-color var(--dur-slow) cubic-bezier(0.22, 1, 0.36, 1),
              color var(--dur-slow) cubic-bezier(0.22, 1, 0.36, 1);
}

body {
  font-family: var(--font-sans);
  font-feature-settings: 'cv02', 'cv03', 'cv04', 'cv11';
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  background: var(--bg-page);
  transition: background-color var(--dur-slow) cubic-bezier(0.22, 1, 0.36, 1),
              background-image var(--dur-slow) cubic-bezier(0.22, 1, 0.36, 1);
}

/* 主题切换：全局平滑过渡 - 仅对特定属性生效，避免 * 选择器性能问题 */
:root {
  transition-property: background-color, border-color, box-shadow, color;
  transition-duration: 0.3s;
  transition-timing-function: cubic-bezier(0.22, 1, 0.36, 1);
  transition-delay: 0s;
}

/* 减少动画偏好 */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    transition: none !important;
  }
}
</style>
