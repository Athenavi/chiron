import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import path from 'path'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, 'src'),
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['src/test-setup.ts'],
    include: ['src/**/*.{test,spec}.ts'],
    exclude: ['node_modules/**', 'dist/**'],
    // jsdom 环境创建是本套件的主要开销（基线实测：118 个文件各建一次，
    // 共 642s，占追踪时间的 48%）。`vmThreads` 让每个 worker 只建一次、
    // 在 vm 上下文里按文件复用，**保留 per-file 隔离**。
    //
    // 为什么不用官方提到的另一条 `isolate: false`：那会跨文件共享同一个
    // jsdom 实例，而本套件有大量实例级全局状态 —— `document.documentElement`
    // 上的 dark class、localStorage polyfill、`config.global.plugins` 里的
    // i18n 单例、以及若干模块级 mock。共享环境会让这些状态互相串味，
    // 表现为"单跑绿、全量跑红"的随机失败，排查成本远高于省下的几秒。
    pool: 'vmThreads',
  },
})
