// ESLint 9+ flat config
import js from '@eslint/js'
import vue from 'eslint-plugin-vue'
import vueParser from 'vue-eslint-parser'
import tseslint from 'typescript-eslint'
import globals from 'globals'

export default [
  // 全局忽略
  {
    ignores: ['dist/**', 'node_modules/**', 'public/**'],
  },
  // JS 基础规则
  js.configs.recommended,
  // TypeScript 规则
  ...tseslint.configs.recommended,
  // Vue 规则
  ...vue.configs['flat/recommended'],
  // 浏览器 / ES 全局变量。
  // no-undef 需要知道 window / document / setTimeout 这类运行时全局量，
  // 而 flat config 从 ESLint 9 起不再由 eslint-env 注释或插件隐式注入，
  // 必须显式声明，否则整个 src 会被 398 条误报淹没。
  {
    languageOptions: {
      globals: {
        ...globals.browser,
        ...globals.es2021,
      },
    },
  },
  // Vue 文件解析器配置（TypeScript + Vue SFC）
  {
    files: ['**/*.vue'],
    languageOptions: {
      parser: vueParser,
      parserOptions: {
        ecmaVersion: 'latest',
        sourceType: 'module',
        parser: tseslint.parser,
        extraFileExtensions: ['.vue'],
      },
    },
  },
  // 项目特定配置
  {
    rules: {
      // 允许 console（开发调试用）
      'no-console': 'off',
      // 允许 any 类型（警告而非错误，避免阻塞构建）
      '@typescript-eslint/no-explicit-any': 'warn',
      // 允许未使用的变量（警告而非错误）
      '@typescript-eslint/no-unused-vars': 'warn',
      // Vue：允许多根节点（Vue 3 支持）
      'vue/multi-word-component-names': 'off',
      // Vue：允许 v-html（经 DOMPurify 净化）
      'vue/no-v-html': 'off',
    },
  },
  // 测试文件配置
  {
    files: ['**/*.spec.ts', '**/*.test.ts'],
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
    },
  },
]
