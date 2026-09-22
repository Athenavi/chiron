<script lang="ts" setup>
import { computed, h, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useThemeStore } from '../stores/theme'
import { useAuthStore } from '../stores/auth'

// Ant Design Vue 组件
import {
  Avatar,
  Button,
  Dropdown,
  Menu,
  Modal,
  message,
} from 'ant-design-vue'
// Ant Design 图标
import {
  HomeOutlined,
  MessageOutlined,
  UserOutlined,
  ApartmentOutlined,
  BlockOutlined,
  PictureOutlined,
  BookOutlined,
  ThunderboltOutlined,
  CreditCardOutlined,
  SettingOutlined,
  LogoutOutlined,
  UserSwitchOutlined,
  DownOutlined,
  HistoryOutlined,
  RobotOutlined,
  AppstoreOutlined,
  ConsoleSqlOutlined,
  ApiOutlined,
} from '@ant-design/icons-vue'
import CommandPalette from './CommandPalette.vue'
import ThemeSwitcher from './ThemeSwitcher.vue'
import SettingsPanel from './settings/SettingsPanel.vue'
import { executeQuickCommand } from "@/components/WorkstationNav.vue"
import {
  WORKSTATION_DESCRIPTIONS,
  WORKSTATION_LABELS,
  WORKSTATION_ROUTES,
} from '../types/workstation'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const themeStore = useThemeStore()

/**
 * 聊天页：右上角这个**固定定位**胶囊（top:12 / right:12 / z-index:30）会盖住
 * ChatView 消息区工具栏右端的「更多操作」。
 *
 * 侧栏在右侧时 chat-main 变窄、两者不重叠，所以平时看不出来；一旦交换布局把
 * 侧栏移到左侧，chat-main 变宽、工具栏右端顶到页面右边缘，就正好被它盖住。
 * 该页的主题 / 用户入口改由侧栏底部（ChatSidePanel 的 .panel-foot）承载。
 */
const isChatPage = computed(() => route.path === '/chat')

// 监听 API 错误
function handleApiError(e: Event) {
  const detail = (e as CustomEvent).detail
  message.error(detail.message || '请求失败')
}

onMounted(() => {
  window.addEventListener('api:error', handleApiError)
  if (authStore.token) {
    authStore.fetchProfile()
  }
})
onUnmounted(() => {
  window.removeEventListener('api:error', handleApiError)
  document.removeEventListener('click', onQuickDocClick)
  window.removeEventListener('keydown', onQuickKeydown)
})

// 导航菜单（品牌下拉）
interface MenuItem {
  key: string
  label: string
  icon?: any
}

const menuItems = computed<MenuItem[]>(() => {
  const items: MenuItem[] = [
    { key: '/', label: '首页', icon: () => h(HomeOutlined) },
    { key: WORKSTATION_ROUTES.dialogue, label: WORKSTATION_LABELS.dialogue, icon: () => h(MessageOutlined) },
    { key: WORKSTATION_ROUTES.agent, label: WORKSTATION_LABELS.agent, icon: () => h(UserOutlined) },
    { key: WORKSTATION_ROUTES.workflow, label: WORKSTATION_LABELS.workflow, icon: () => h(ApartmentOutlined) },
    { key: WORKSTATION_ROUTES.skill, label: WORKSTATION_LABELS.skill, icon: () => h(BlockOutlined) },
    { key: '/media', label: '媒体', icon: () => h(PictureOutlined) },
    { key: WORKSTATION_ROUTES.knowledge, label: WORKSTATION_LABELS.knowledge, icon: () => h(BookOutlined) },
    { key: '/memory', label: '记忆', icon: () => h(HistoryOutlined) },
    { key: WORKSTATION_ROUTES.plugin, label: WORKSTATION_LABELS.plugin, icon: () => h(ThunderboltOutlined) },
    { key: '/billing', label: '计费', icon: () => h(CreditCardOutlined) },
    ...(authStore.isAdmin
      ? [
          // 模型配置：决定对话页模型下拉里能选到什么（后端 /v1/admin/models，需管理员）
          { key: '/models', label: '模型', icon: () => h(ApiOutlined) },
          { key: '/admin', label: '管理', icon: () => h(SettingOutlined) },
        ]
      : []),
  ]
  return items
})

const currentLabel = computed(() => {
  const hit = menuItems.value.find(m => route.path === m.key || route.path.startsWith(m.key + '/'))
  return hit?.label || ''
})

const selectedKeys = computed(() => {
  const exact = menuItems.value.find(m => route.path === m.key)
  return [exact?.key ?? route.path]
})

function handleMenuClick(info: any) {
  router.push(info.key)
}

const userMenuItems = computed<any[]>(() => [
  { key: 'settings', label: '设置', icon: () => h(SettingOutlined) },
  { key: 'profile', label: '个人资料', icon: () => h(UserSwitchOutlined) },
  { key: 'logout', label: '退出登录', icon: () => h(LogoutOutlined) },
])

// 设置弹窗与 /profile 页面共用 SettingsPanel，避免两套实现各自漂移
const settingsOpen = ref(false)

async function handleUserMenuClick(info: any) {
  if (info.key === 'logout') {
    await authStore.logout()
    router.push('/login')
  } else if (info.key === 'settings') {
    settingsOpen.value = true
  } else if (info.key === 'profile') {
    router.push('/profile')
  }
}

// ── 工作台停靠坞：六大工作台全局一键切换 ──
interface DockItem {
  key: string
  label: string
  desc: string
  icon: any
}

const dockItems: DockItem[] = [
  { key: WORKSTATION_ROUTES.dialogue, label: WORKSTATION_LABELS.dialogue, desc: WORKSTATION_DESCRIPTIONS.dialogue, icon: MessageOutlined },
  { key: WORKSTATION_ROUTES.agent, label: WORKSTATION_LABELS.agent, desc: WORKSTATION_DESCRIPTIONS.agent, icon: RobotOutlined },
  { key: WORKSTATION_ROUTES.workflow, label: WORKSTATION_LABELS.workflow, desc: WORKSTATION_DESCRIPTIONS.workflow, icon: ApartmentOutlined },
  { key: WORKSTATION_ROUTES.skill, label: WORKSTATION_LABELS.skill, desc: WORKSTATION_DESCRIPTIONS.skill, icon: ThunderboltOutlined },
  { key: WORKSTATION_ROUTES.knowledge, label: WORKSTATION_LABELS.knowledge, desc: WORKSTATION_DESCRIPTIONS.knowledge, icon: BookOutlined },
  { key: WORKSTATION_ROUTES.plugin, label: WORKSTATION_LABELS.plugin, desc: WORKSTATION_DESCRIPTIONS.plugin, icon: AppstoreOutlined },
]

const showDock = computed(() => !!authStore.token && route.path !== '/')

function isDockActive(key: string) {
  return route.path === key || route.path.startsWith(key + '/')
}

function goDock(key: string) {
  if (route.path !== key) router.push(key)
}

// ── 停靠坞快速命令弹层 ──
const quickOpen = ref(false)
const quickInput = ref('')
const quickLoading = ref(false)
const quickPanelEl = ref<HTMLElement | null>(null)
const quickBtnEl = ref<HTMLElement | null>(null)

function onQuickDocClick(e: MouseEvent) {
  const t = e.target as Node
  if (quickPanelEl.value?.contains(t) || quickBtnEl.value?.contains(t)) return
  quickOpen.value = false
}

function onQuickKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') closeQuickCommand()
}

function toggleQuickCommand() {
  quickOpen.value = !quickOpen.value
}

function closeQuickCommand() {
  quickOpen.value = false
  quickInput.value = ''
}

watch(quickOpen, (open) => {
  if (open) {
    document.addEventListener('click', onQuickDocClick)
    window.addEventListener('keydown', onQuickKeydown)
    nextTick(() => {
      const input = quickPanelEl.value?.querySelector('input') as HTMLInputElement | null
      input?.focus()
    })
  } else {
    document.removeEventListener('click', onQuickDocClick)
    window.removeEventListener('keydown', onQuickKeydown)
  }
})

watch(() => route.path, () => {
  if (quickOpen.value) quickOpen.value = false
})

async function runQuickCommand() {
  const command = quickInput.value.trim()
  if (!command || quickLoading.value) return
  quickLoading.value = true
  try {
    await executeQuickCommand(command)
    message.success('任务已提交，正在对话页展示结果')
    closeQuickCommand()
  } catch {
    // 错误已由统一处理器处理
  } finally {
    quickLoading.value = false
  }
}
</script>

<template>
  <div class="app-shell">
    <!-- 左上角浮动品牌胶囊 -->
    <header
      :title="currentLabel || '导航菜单'"
      class="topbar"
    >
      <Dropdown
        placement="bottomLeft"
        trigger="click"
      >
        <button
          class="brand-btn"
          :title="$t('导航菜单')"
          type="button"
        >
          <span class="brand-logo">MC</span>
          <span class="brand-name">Chiron</span>
          <DownOutlined class="brand-caret" />
        </button>
        <template #overlay>
          <Menu
            :items="menuItems"
            :selected-keys="selectedKeys"
            class="nav-menu"
            @click="handleMenuClick"
          />
        </template>
      </Dropdown>
    </header>

    <!-- 工作台停靠坞 -->
    <nav
      v-if="showDock"
      aria-label="工作台停靠坞"
      class="dock"
    >
      <div class="dock-items">
        <button
          v-for="item in dockItems"
          :key="item.key"
          :aria-label="item.label"
          :class="{ active: isDockActive(item.key) }"
          class="dock-item"
          type="button"
          @click="goDock(item.key)"
        >
          <component
            :is="item.icon"
            class="dock-icon"
          />
          <span
            class="dock-tip"
            role="tooltip"
          >
            <span class="dock-tip-name">{{ item.label }}</span>
            <span class="dock-tip-desc">{{ item.desc }}</span>
          </span>
        </button>
      </div>

      <div class="dock-command">
        <button
          ref="quickBtnEl"
          :aria-expanded="quickOpen"
          :aria-label="quickOpen ? '关闭快速命令' : '打开快速命令'"
          :class="{ open: quickOpen }"
          class="dock-command-btn"
          type="button"
          @click="toggleQuickCommand"
        >
          <ConsoleSqlOutlined />
        </button>
        <Transition name="dock-pop">
          <div
            v-if="quickOpen"
            ref="quickPanelEl"
            aria-label="快速命令"
            class="dock-popover"
            role="dialog"
          >
            <div class="dock-popover-head">
              <span class="dock-popover-title">{{ $t('快速命令') }}</span>
              <span class="dock-popover-hint">{{ $t('自然语言任务 · 自动编排六大工作台，结果进入统一会话') }}</span>
            </div>
            <div class="dock-command-row">
              <input
                v-model="quickInput"
                class="dock-command-input"
                :placeholder="$t('例如：帮我分析 sales.csv 并生成报告')"
                type="text"
                @keydown.enter="runQuickCommand"
                @keydown.esc="closeQuickCommand"
              >
              <button
                :disabled="quickLoading"
                class="dock-command-go"
                type="button"
                @click="runQuickCommand"
              >
                <span
                  v-if="quickLoading"
                  class="dock-spinner"
                />
                <span v-else>{{ $t('执行') }}</span>
              </button>
            </div>
          </div>
        </Transition>
      </div>
    </nav>

    <!-- 右上角用户胶囊（聊天页隐藏，理由见 isChatPage 的注释） -->
    <div
      v-if="!isChatPage"
      class="topbar-actions"
    >
      <ThemeSwitcher />
      <div
        v-if="authStore.user"
        class="user-fab"
      >
        <!-- 与上方品牌下拉同构：显式 trigger="click" + overlay 插槽。
             此前是 `:menu` 单属性且未指定 trigger —— Dropdown 默认 hover 触发，
             所以点击头像不会有任何反应。 -->
        <Dropdown
          trigger="click"
          placement="bottomRight"
        >
          <Avatar
            :size="30"
            :style="{ backgroundColor: 'var(--primary)' }"
            class="user-fab-avatar"
          >
            {{ authStore.user.name?.charAt(0)?.toUpperCase() || 'U' }}
          </Avatar>
          <template #overlay>
            <Menu
              :items="userMenuItems"
              @click="handleUserMenuClick"
            />
          </template>
        </Dropdown>
      </div>
    </div>

    <!-- 全宽内容区 -->
    <main
      :class="{ 'app-content--docked': showDock }"
      class="app-content"
    >
      <router-view v-slot="{ Component }">
        <!-- 内容区错误边界：某个页面组件抛错时只替换内容区，侧栏/顶栏保持可用 -->
        <ErrorBoundary>
          <Transition
            mode="out-in"
            name="fade"
          >
            <component :is="Component" />
          </Transition>
        </ErrorBoundary>
      </router-view>
    </main>

    <!-- 全局命令面板 -->
    <CommandPalette />

    <!-- 设置弹窗：与 /profile 页面共用 SettingsPanel，两条入口一份实现。
         destroy-on-close 保证每次打开都重新挂载并拉取最新数据。 -->
    <Modal
      v-model:open="settingsOpen"
      :title="$t('设置')"
      :footer="null"
      :width="720"
      destroy-on-close
    >
      <SettingsPanel />
    </Modal>
  </div>
</template>

<style scoped>
.app-shell {
  position: relative;
  height: 100vh;
  background: var(--bg-page);
  color: var(--text-primary);
  --dock-w: 60px;
  --dock-h: 56px;
  --dock-gap: 12px;
  --panel-bg: var(--bg-elevated);
  --panel-border: var(--border-default);
  --hover-bg: var(--bg-surface-hover);
  --sidebar-bg: var(--bg-sidebar);
}

/* ── 左上角浮动品牌胶囊 ── */
.topbar {
  position: fixed;
  top: 12px;
  left: 12px;
  z-index: 30;
  height: 40px;
  display: flex;
  align-items: center;
  padding: 0 6px 0 4px;
  border-radius: var(--sig-radius-input);
  background: var(--comp-header-bg);
  backdrop-filter: blur(var(--sig-blur-header));
  -webkit-backdrop-filter: blur(var(--sig-blur-header));
  border: 1px solid var(--border-default);
  box-shadow: var(--sig-shadow-card);
}

.brand-btn {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 32px;
  padding: 0 8px 0 4px;
  border: none;
  border-radius: var(--sig-radius-button);
  background: transparent;
  cursor: pointer;
  transition: background 0.15s ease;
}

.brand-btn:hover {
  background: var(--hover-bg);
}

.brand-logo {
  width: 24px;
  height: 24px;
  border-radius: var(--sig-radius-button);
  background: linear-gradient(135deg, var(--primary), var(--accent));
  color: #fff;
  font-weight: 700;
  font-size: 11px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-shadow: var(--sig-shadow-card);
  flex-shrink: 0;
}

.brand-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  letter-spacing: -0.01em;
  white-space: nowrap;
}

.brand-caret {
  font-size: 10px;
  color: var(--text-tertiary);
}

@media (max-width: 480px) {
  .brand-name {
    display: none;
  }
}

/* ── 顶栏右侧操作组 ── */
.topbar-actions {
  position: fixed;
  top: 12px;
  right: 12px;
  z-index: 30;
  display: flex;
  align-items: center;
  gap: 8px;
}

.user-fab {
  padding: 2px;
  border-radius: 50%;
  background: var(--comp-header-bg);
  backdrop-filter: blur(var(--sig-blur-header));
  -webkit-backdrop-filter: blur(var(--sig-blur-header));
  border: 1px solid var(--border-default);
  box-shadow: var(--sig-shadow-card);
  cursor: pointer;
}

.user-fab-avatar {
  cursor: pointer;
  display: block;
}

.user-fab-avatar:hover {
  opacity: 0.9;
}

/* 导航下拉菜单 */
.nav-menu {
  min-width: 200px;
  border-radius: var(--sig-radius-button);
  padding: 4px;
  box-shadow: var(--sig-shadow-hover);
  background: var(--panel-bg) !important;
  backdrop-filter: blur(calc(var(--sig-blur-header) + 4px));
  -webkit-backdrop-filter: blur(calc(var(--sig-blur-header) + 4px));
  border: 1px solid var(--panel-border);
}

.nav-menu :deep(.ant-dropdown-menu-item) {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  border-radius: var(--sig-radius-button);
  padding: 8px 12px !important;
}

.nav-menu :deep(.ant-dropdown-menu-item-selected) {
  background: var(--primary-bg) !important;
  color: var(--primary) !important;
  font-weight: 600;
}

.app-content {
  height: 100vh;
  overflow-y: auto;
  overflow-x: hidden;
}

/* ── 工作台停靠坞 ── */
.dock {
  position: fixed;
  top: 64px;
  left: 10px;
  bottom: 10px;
  z-index: 20;
  width: var(--dock-w);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 10px 8px;
  border-radius: var(--sig-radius-card);
  background: var(--panel-bg);
  backdrop-filter: blur(calc(var(--sig-blur-header) + 4px));
  -webkit-backdrop-filter: blur(calc(var(--sig-blur-header) + 4px));
  border: 1px solid var(--panel-border);
  box-shadow: var(--sig-shadow-card);
  overflow-y: auto;
  scrollbar-width: none;
}

.dock::-webkit-scrollbar {
  display: none;
}

.dock-items {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
}

.dock-item {
  position: relative;
  width: 44px;
  height: 44px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: var(--sig-radius-button);
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease, transform 0.08s ease;
}

.dock-item:hover {
  background: var(--hover-bg);
  color: var(--text-primary);
}

.dock-item:active {
  transform: scale(0.94);
}

.dock-item:focus-visible,
.dock-command-btn:focus-visible,
.brand-btn:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: 2px;
}

.dock-item.active {
  background: var(--primary);
  color: var(--text-inverse);
  box-shadow: 0 4px 14px var(--primary-bg), inset 0 1px 1px hsla(0, 0%, 100%, 0.25);
}

.dock-item.active::before {
  content: '';
  position: absolute;
  left: -9px;
  top: 50%;
  width: 2px;
  height: 20px;
  border-radius: 1px;
  background: var(--primary);
  transform: translateY(-50%) scaleY(0);
  transform-origin: center;
  animation: dockBarIn 0.22s ease-out forwards;
}

@keyframes dockBarIn {
  to {
    transform: translateY(-50%) scaleY(1);
  }
}

.dock-icon {
  font-size: 18px;
}

.dock-tip {
  position: absolute;
  left: calc(100% + 10px);
  top: 50%;
  transform: translateY(-50%) translateX(-4px);
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: 5px 10px;
  border-radius: var(--sig-radius-button);
  background: var(--panel-bg);
  backdrop-filter: blur(var(--sig-blur-header));
  -webkit-backdrop-filter: blur(var(--sig-blur-header));
  border: 1px solid var(--panel-border);
  box-shadow: var(--sig-shadow-card);
  color: var(--text-primary);
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.15s ease, transform 0.15s ease;
  z-index: 25;
}

.dock-tip-name {
  line-height: 16px;
}

.dock-tip-desc {
  font-size: 11px;
  font-weight: 400;
  color: var(--text-tertiary);
  line-height: 15px;
}

.dock-item:hover .dock-tip {
  opacity: 1;
  transform: translateY(-50%) translateX(0);
}

@media (hover: none) {
  .dock-tip {
    display: none;
  }
}

/* ── 快速命令 ── */
.dock-command {
  position: relative;
  margin-top: auto;
}

.dock-command-btn {
  width: 44px;
  height: 44px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px dashed var(--panel-border);
  border-radius: var(--sig-radius-button);
  background: var(--hover-bg);
  color: var(--text-secondary);
  font-size: 17px;
  cursor: pointer;
  transition: color 0.15s ease, border-color 0.15s ease, background 0.15s ease;
}

.dock-command-btn:hover,
.dock-command-btn.open {
  color: var(--primary);
  border-color: var(--primary);
  background: var(--primary-bg);
}

.dock-popover {
  position: absolute;
  left: calc(100% + 10px);
  bottom: 0;
  width: 340px;
  padding: 12px;
  border-radius: var(--sig-radius-code);
  background: var(--panel-bg);
  backdrop-filter: blur(calc(var(--sig-blur-header) + 4px));
  -webkit-backdrop-filter: blur(calc(var(--sig-blur-header) + 4px));
  border: 1px solid var(--panel-border);
  box-shadow: var(--sig-shadow-hover);
  z-index: 25;
}

.dock-popover-head {
  margin-bottom: 10px;
}

.dock-popover-title {
  display: block;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.dock-popover-hint {
  display: block;
  margin-top: 2px;
  font-size: 11px;
  line-height: 16px;
  color: var(--text-tertiary);
}

.dock-command-row {
  display: flex;
  gap: 8px;
}

.dock-command-input {
  flex: 1;
  min-width: 0;
  min-height: 40px;
  padding: 8px 12px;
  border: 1px solid var(--border-default);
  border-radius: var(--sig-radius-card);
  background: var(--bg-surface);
  color: var(--text-primary);
  font-size: 13px;
  outline: none;
  transition: border-color 0.2s ease, box-shadow 0.2s ease;
}

.dock-command-input:focus {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px var(--primary-bg);
}

.dock-command-input::placeholder {
  color: var(--text-quaternary);
}

.dock-command-go {
  flex: none;
  min-height: 40px;
  min-width: 64px;
  padding: 0 14px;
  border: none;
  border-radius: var(--sig-radius-card);
  background: var(--primary);
  color: var(--text-inverse);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.2s ease, opacity 0.2s ease;
}

.dock-command-go:hover:not(:disabled) {
  background: var(--primary-hover);
}

.dock-command-go:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.dock-spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid rgba(255, 255, 255, 0.35);
  border-top-color: #fff;
  border-radius: 50%;
  animation: dockSpin 0.6s linear infinite;
}

@keyframes dockSpin {
  to {
    transform: rotate(360deg);
  }
}

.dock-pop-enter-active,
.dock-pop-leave-active {
  transition: opacity 0.16s ease, transform 0.16s ease;
}

.dock-pop-enter-from,
.dock-pop-leave-to {
  opacity: 0;
  transform: translateY(4px);
}

.app-content--docked {
  margin-left: calc(10px + var(--dock-w) + var(--dock-gap));
}

@media (max-width: 768px) {
  .dock {
    top: auto;
    left: 10px;
    right: 10px;
    bottom: calc(10px + env(safe-area-inset-bottom, 0px));
    width: auto;
    height: var(--dock-h);
    flex-direction: row;
    align-items: center;
    gap: 4px;
    padding: 8px 10px;
    border-radius: var(--sig-radius-card);
  }

  .dock-items {
    flex: 1;
    flex-direction: row;
    justify-content: space-around;
    gap: 2px;
    min-width: 0;
  }

  .dock-item {
    width: 40px;
    height: 40px;
  }

  .dock-item.active::before {
    left: 50%;
    top: -7px;
    width: 20px;
    height: 2px;
    transform: translateX(-50%) scaleX(0);
    animation-name: dockBarInX;
  }

  @keyframes dockBarInX {
    to {
      transform: translateX(-50%) scaleX(1);
    }
  }

  .dock-tip {
    display: none;
  }

  .dock-command {
    margin-top: 0;
  }

  .dock-command-btn {
    width: 40px;
    height: 40px;
  }

  .dock-popover {
    position: absolute;
    left: auto;
    right: 0;
    bottom: calc(100% + 10px);
    width: min(340px, calc(100vw - 44px));
  }

  .app-content--docked {
    margin-left: 0;
    padding-bottom: calc(10px + var(--dock-h) + var(--dock-gap) + env(safe-area-inset-bottom, 0px));
  }
}

@media (prefers-reduced-motion: reduce) {
  .dock-item,
  .dock-command-btn,
  .dock-command-input,
  .dock-command-go,
  .dock-tip {
    transition: none;
  }

  .dock-item.active::before {
    animation: none;
    transform: translateY(-50%) scaleY(1);
  }

  @media (max-width: 768px) {
    .dock-item.active::before {
      animation: none;
      transform: translateX(-50%) scaleX(1);
    }
  }
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>