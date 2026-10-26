import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileViewerRenderers } from '@file-viewer/vite-plugin'
import Components from 'unplugin-vue-components/vite'
import { AntDesignVueResolver } from 'unplugin-vue-components/resolvers'
import { compression, defineAlgorithm } from 'vite-plugin-compression2'
import AutoImport from 'unplugin-auto-import/vite'
import { visualizer } from 'rollup-plugin-visualizer'
import path from 'path'
import { fileURLToPath } from 'url'

const __dirname = import.meta.dirname ?? path.dirname(fileURLToPath(import.meta.url))

// 开发代理指向的后端：默认与 run.py 的 Gateway 端口一致，可用环境变量覆盖。
const API_ORIGIN = process.env.CHIRON_DEV_API_ORIGIN || 'http://localhost:8080'
const WS_ORIGIN = API_ORIGIN.replace(/^http/, 'ws')

// https://vite.dev/config/
export default defineConfig(({ mode }) => ({
  // 桌面壳（--mode desktop）用 loadFile 从 file:// 打开 dist：资源必须是相对路径，
  // 否则 /assets/* 会解析成 file:///X:/assets/*。Web 构建保持根绝对路径。
  base: mode === 'desktop' ? './' : '/',
  plugins: [
    vue(),
    fileViewerRenderers({ copyAssets: true }),
    AutoImport({
      imports: ['vue', 'vue-router'],
      dts: 'src/auto-imports.d.ts',
    }),
    // antd 按需引入：只打包模板中实际使用的 a-* 组件，显著削减首屏体积。
    // 组件样式由 antd v4 cssinjs 运行时注入，无需额外 style 导入。
    Components({
      resolvers: [AntDesignVueResolver({ importStyle: false })],
      dts: 'src/components.d.ts',
    }),
    // Gzip 压缩：减少传输体积
    compression({
      algorithms: [defineAlgorithm('gzip')],
      exclude: [/\.(br)$/, /\.(gz)$/],
      threshold: 1024,
      deleteOriginalAssets: false,
    }),
    // Brotli 压缩：更高压缩比
    compression({
      algorithms: [defineAlgorithm('brotliCompress')],
      exclude: [/\.(atlas)$/, /\.map$/, /\.json$/],
      threshold: 1024,
      deleteOriginalAssets: false,
    }),
    ...(process.env.ANALYZE ? [visualizer({
      open: true,
      gzipSize: true,
      brotliSize: true,
      filename: 'dist/stats.html',
    }) as any] : []),
  ],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
    // 强制 Vue 单实例：pnpm 为 ant-design-vue/@ant-design/icons-vue 解析到
    // node_modules/.pnpm 下的 vue@3.5.41，与 app 的 vue@3.5.39 不一致，
    // 导致 provide/inject 上下文断裂（prefixCls undefined）
    dedupe: ['vue'],
  },
  server: {
    // 代理后端 API：避免开发时 CORS 跨域问题（后端 CORS_ORIGINS 仅含 5173）
    proxy: {
      '/v1': {
        target: API_ORIGIN,
        changeOrigin: true,
      },
      '/events': {
        target: API_ORIGIN,
        changeOrigin: true,
      },
      '/ws': {
        target: WS_ORIGIN,
        ws: true,
      },
      '/metrics': {
        target: API_ORIGIN,
        changeOrigin: true,
      },
      // S 修复：/submit /cancel /media 为网关非 /v1 前缀路由，dev 亦需代理
      '/submit': {
        target: API_ORIGIN,
        changeOrigin: true,
      },
      '/cancel': {
        target: API_ORIGIN,
        changeOrigin: true,
      },
      '/media': {
        target: API_ORIGIN,
        changeOrigin: true,
      },
    },
  },
  build: {
    target: 'es2020',
    minify: 'esbuild',
    sourcemap: false,
    cssCodeSplit: true,
    chunkSizeWarningLimit: 1000,
    // 启用 Treeshaking
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          if (id.includes('node_modules')) {
            if (id.includes('vue') || id.includes('pinia')) return 'vendor-vue'
            if (id.includes('ant-design')) return 'vendor-ui'
            if (id.includes('echarts')) return 'vendor-charts'
            if (id.includes('mermaid')) return 'vendor-diagram'
            if (id.includes('katex') || id.includes('markdown-it')) return 'vendor-markdown'
            if (id.includes('vue-flow')) return 'vendor-flow'
            if (id.includes('three')) return 'vendor-three'
          }
        },
        // 优化代码分割和 tree-shaking
        hoistTransitiveImports: false,
      },
      // 强制 tree-shaking
      treeshake: {
        moduleSideEffects: 'no-external',
      },
    },
  },
}))
