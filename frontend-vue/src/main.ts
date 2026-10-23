import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { message } from 'ant-design-vue'
// antd 已按需引入（unplugin-vue-components），无需全局 app.use(Antd)
import 'ant-design-vue/dist/reset.css'
import '@fontsource-variable/geist'
import router from './router'
import App from './App.vue'
import './style.css'
import { usePerformanceMonitor } from './composables/usePerformanceMonitor'
import { setupRequestInterceptors } from './utils/requestOptimizer'
import { api } from './api'
import { i18n, applyDocumentLocale } from './i18n'

// 首屏语言与书写方向必须在 mount 前落到 <html lang/dir>：否则 RTL 语言会先按 LTR
// 渲染再跳变（可见的重排闪烁）。
applyDocumentLocale()

const app = createApp(App)

// 标记应用启动
performance.mark('app-init-start')

// 初始化性能监控
const { initMonitor, mark, measure } = usePerformanceMonitor()
initMonitor()

// 全局暴露性能数据 (仅开发环境，供 DevTools 调试)
if (import.meta.env.DEV) {
  window.__ChironPerf = { mark, measure, getReport: () => usePerformanceMonitor().getReport() }
}

// 初始化请求优化（需在全局 api 实例创建后调用）
setupRequestInterceptors(api)

const pinia = createPinia()

app.use(i18n)
app.use(pinia)
app.use(router)

// 统一提示时长：成功/信息类 2.5s（"复制成功"看一眼就够）。
// 错误类需要读完，调用点单独传 { duration: 4 }（见 docs/ui-refinement-design.md §8）。
message.config({ duration: 2.5 })

app.mount('#app')

// 标记首次渲染完成
performance.mark('vue-first-render')
performance.mark('app-init-end')

// Lighthouse 性能评分优化标记
if (import.meta.env.PROD) {
  performance.measure('app-init-duration', 'app-init-start', 'app-init-end')
}

// PWA：生产环境注册 Service Worker（离线壳；开发环境跳过）
if (import.meta.env.PROD && 'serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {})
  })
}
