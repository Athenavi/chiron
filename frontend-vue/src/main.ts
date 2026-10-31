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
import { useAuthStore } from './stores/auth'
import { i18n, applyDocumentLocale } from './i18n'
import { installLocaleSync } from './utils/localeSync'

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

// 语言偏好回写服务端（T1）：邮件等**服务端渲染**的通知按 users.settings.locale
// 决定语言，只存 localStorage 时服务端永远不知道用户选了什么。
// 必须在 app.use(pinia) 之后安装 —— 探测登录态要读 auth store。
installLocaleSync(() => {
  try {
    return !!useAuthStore(pinia).user
  } catch {
    return false
  }
})

// 统一提示时长：成功/信息类 2.5s（"复制成功"看一眼就够）。
// 错误类需要读完，调用点单独传 { duration: 4 }。
message.config({ duration: 2.5 })

// 桌面端 token 由 safeStorage 异步解密；先拿到再挂载，避免首屏请求缺少 Bearer。
// Web 端 initToken 立即返回，不影响启动。
void useAuthStore(pinia)
  .initToken()
  .finally(() => {
    app.mount('#app')

    // 标记首次渲染完成
    performance.mark('vue-first-render')
    performance.mark('app-init-end')

    // Lighthouse 性能评分优化标记
    if (import.meta.env.PROD) {
      performance.measure('app-init-duration', 'app-init-start', 'app-init-end')
    }
  })

// PWA：生产环境注册 Service Worker（离线壳；开发环境跳过）
// file://（Electron 桌面壳）不是安全上下文，注册只会产生一条 404 失败请求。
// 用 BASE_URL 拼路径：Web 子路径部署时不能再假设站点位于域名根。
if (import.meta.env.PROD && 'serviceWorker' in navigator && location.protocol !== 'file:') {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register(`${import.meta.env.BASE_URL}sw.js`).catch(() => {})
  })
}
