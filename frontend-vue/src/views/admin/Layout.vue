<script setup lang="ts">
import { ref, computed, h, watch, onMounted } from 'vue'
import type { VNode } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import ThemeSwitcher from '../../components/ThemeSwitcher.vue'
import LanguageSwitcher from '../../components/common/LanguageSwitcher.vue'
import { getUnreadCount } from '../../api/notifications'
import {
  Layout,
  LayoutSider,
  LayoutHeader,
  LayoutContent,
  Menu,
  
  MenuItem,
  MenuItemGroup,
  Breadcrumb,
  BreadcrumbItem,
  Button,
  Badge,
  Dropdown,
  Avatar,
} from 'ant-design-vue'
import {
  DashboardOutlined,
  HomeOutlined,
  KeyOutlined,
  DatabaseOutlined,
  ThunderboltOutlined,
  SettingOutlined,
  BellOutlined,
  UserOutlined,
  LogoutOutlined,
  TeamOutlined,
  FileSearchOutlined,
  MedicineBoxOutlined,
  BarChartOutlined,
  ShoppingOutlined,
  ControlOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  InboxOutlined,
} from '@ant-design/icons-vue'

import { useI18n } from 'vue-i18n'
import type { MenuProps } from 'ant-design-vue'
const { t } = useI18n()
const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()

// 折叠态：桌面端用 v-model，移动端用抽屉
const collapsed = ref(false)
// 移动端抽屉可见性（< 960px 触发抽屉模式）
const isMobile = ref(false)
const drawerOpen = ref(false)

function checkMobile() {
  isMobile.value = window.innerWidth < 960
  if (!isMobile.value) drawerOpen.value = false
}
if (typeof window !== 'undefined') {
  checkMobile()
  window.addEventListener('resize', checkMobile)
}

// 路由切换时关闭移动端抽屉
watch(() => route.path, () => { drawerOpen.value = false })

const breadcrumbs = computed(() => {
  const matched = route.matched.filter(r => r.meta?.title || r.name)
  return matched.map(item => ({
    path: item.path,
    title: (item.meta?.title as string) || (item.name as string) || '',
  }))
})

// 菜单分组：平台后台只保留平台级能力（租户级配置在 /tenant 下由租户管理员维护）
//
// `ownerOnly` 对齐后端 `platformReadMW`（`RequirePlatformOwner`）。
// 租户 admin 持有 `admin:read`，但打这些端点照样 403 —— 菜单若照常渲染，
// 用户点进去只能看到一个空页面加报错，会误以为系统坏了。
// 过滤放在 computed 里而不是模板里：桌面侧栏与移动抽屉两处渲染共用这一份结果。
interface AdminMenuItem {
  key: string
  label: string
  icon: () => VNode
  ownerOnly?: boolean
}

const menuGroups = computed(() => {
  const groups: { key: string; label: string; children: AdminMenuItem[] }[] = [
  {
    key: 'g-monitor', label: t('admin.overview_monitoring'),
    children: [
      { key: '/admin/dashboard', label: t('common.dashboard'), icon: () => h(DashboardOutlined) },
      // 原「性能监控 / 队列监控 / 缓存监控」三页已合并为运行时监控（Tabs）
      { key: '/admin/monitor', label: t('admin.runtime_monitoring'), icon: () => h(ThunderboltOutlined), ownerOnly: true },
    ],
  },
  {
    key: 'g-system', label: t('common.system'),
    children: [
      { key: '/admin/api-keys', label: t('common.api_key_management'), icon: () => h(KeyOutlined), ownerOnly: true },
      { key: '/admin/settings', label: t('settings.system_settings'), icon: () => h(SettingOutlined), ownerOnly: true },
      // 原「Redis 管理 + 数据库管理」合并为数据存储（Tabs）
      { key: '/admin/datastores', label: t('common.data_storage'), icon: () => h(DatabaseOutlined), ownerOnly: true },
      // 原「租户管理 + 域名管理」合并为租户与域名（Tabs）。
      // 面板级权限不同级（tenants 仅 owner / domains admin 可读），
      // 由 TenancyView 内部按角色裁面板，菜单项本身对 admin 保留。
      { key: '/admin/tenancy', label: t('admin.tenants_and_domains'), icon: () => h(TeamOutlined) },
      // 平台视角只看租户这一级用量；成员级明细在租户管理员后台
      { key: '/admin/usage', label: t('admin.usage_insights'), icon: () => h(BarChartOutlined) },
      // 平台套餐模板：租户新建套餐时从这里预填
      { key: '/admin/package-templates', label: t('admin.package_templates_title'), icon: () => h(ShoppingOutlined), ownerOnly: true },
      // 配额审批：租户提交的调整申请，通过后立即对限流闸门生效
      { key: '/admin/quota-review', label: t('admin.quota_review_title'), icon: () => h(ControlOutlined), ownerOnly: true },
      { key: '/admin/audit', label: t('admin.operation_audit'), icon: () => h(FileSearchOutlined) },
      // 账号注销归档：与审计同属"合规留痕"，放同一分组。
      { key: '/admin/account-archives', label: t('admin.account_archives'), icon: () => h(InboxOutlined), ownerOnly: true },
      // D1：自诊断 —— 与设置/数据存储同属"系统"类：都是"让部署正确运转"的操作面。
      { key: '/admin/diagnostics', label: t('admin.diagnostics'), icon: () => h(MedicineBoxOutlined) },
    ],
  },
  ]
  return groups
    .map(g => ({ ...g, children: g.children.filter(m => !m.ownerOnly || authStore.isOwner) }))
    // 整组都被过滤掉时不留空标题
    .filter(g => g.children.length > 0)
})

// 当前路由命中的菜单项（用于侧边栏 selectedKeys）
const selectedKeys = computed(() => {
  const hit = menuGroups.value
    .flatMap(g => g.children)
    .find(m => route.path === m.key || route.path.startsWith(m.key + '/'))
  return [hit?.key ?? route.path]
})

/** antd Menu / Dropdown 的点击载荷（本页只用 key） */
interface MenuClickInfo {
  key: string | number
}

const userMenuItems = computed<MenuProps['items']>(() => [
  // 后台不再套全局 AppLayout（否则右上角会叠一套浮动胶囊），
  // 所以这里补一个显式的"回前台"入口。
  { key: 'home', label: t('common.home'), icon: () => h(HomeOutlined) },
  { key: 'profile', label: t('settings.profile'), icon: () => h(UserOutlined) },
  { type: 'divider' as const },
  { key: 'logout', label: t('auth.logout'), icon: () => h(LogoutOutlined) },
])

// 通知未读数：侧边栏底部入口上的角标。低频变化的小整数，进页面拉一次即可
// （与 AppLayout 的 5 分钟轮询同口径；管理后台不需要更实时）。
const unreadCount = ref(0)
async function refreshUnread() {
  try {
    unreadCount.value = await getUnreadCount()
  } catch {
    // 角标失败不该打扰用户：保留 0
  }
}
onMounted(refreshUnread)

function goNotifications() {
  router.push('/notifications')
}

function handleMenuClick(info: MenuClickInfo) {
  router.push(String(info.key))
}

async function handleUserAction(info: MenuClickInfo) {
  if (info.key === 'logout') {
    // 安全：调 authStore.logout 清后端 httpOnly cookie + 本地 user
    await authStore.logout()
    router.push('/login')
  } else if (info.key === 'profile') {
    router.push('/profile')
  } else if (info.key === 'home') {
    router.push('/')
  }
}

const userInitial = computed(() => authStore.user?.name?.charAt(0)?.toUpperCase() || 'A')
</script>

<template>
  <Layout class="admin-root">
    <!-- 桌面端固定 Sider -->
    <LayoutSider
      v-if="!isMobile"
      v-model:collapsed="collapsed"
      collapsible
      :trigger="null"
      :width="240"
      :collapsed-width="72"
      class="admin-sider"
    >
      <div class="sider-brand">
        <span class="sider-logo">MC</span>
        <span
          v-if="!collapsed"
          class="sider-name"
        >Chiron Admin</span>
      </div>
      <div class="sider-menu">
        <Menu
          mode="inline"
          :selected-keys="selectedKeys"
          :inline-collapsed="collapsed"
          @click="handleMenuClick"
        >
          <template
            v-for="g in menuGroups"
            :key="g.key"
          >
            <MenuItemGroup :title="collapsed ? '' : g.label">
              <MenuItem
                v-for="m in g.children"
                :key="m.key"
              >
                <template #icon>
                  <component :is="m.icon" />
                </template>
                {{ m.label }}
              </MenuItem>
            </MenuItemGroup>
          </template>
        </Menu>
      </div>
      <!-- 侧栏底部：通知 / 语言 / 主题 / 用户（原右上角功能区下沉到这里） -->
      <div
        class="sider-footer"
        :class="{ 'sider-footer--collapsed': collapsed }"
      >
        <button
          type="button"
          class="sider-footer-btn"
          :title="$t('settings.notification')"
          :aria-label="$t('settings.notification')"
          @click="goNotifications"
        >
          <Badge
            :count="unreadCount"
            :overflow-count="99"
            size="small"
          >
            <BellOutlined />
          </Badge>
          <span
            v-if="!collapsed"
            class="sider-footer-label"
          >{{ $t('settings.notification') }}</span>
        </button>
        <div class="sider-footer-inline">
          <LanguageSwitcher compact />
          <ThemeSwitcher />
        </div>
        <Dropdown
          :menu="{ items: userMenuItems, onClick: handleUserAction }"
          placement="topRight"
        >
          <button
            type="button"
            class="sider-footer-user"
            :title="authStore.user?.name || 'Admin'"
          >
            <Avatar
              :size="26"
              :style="{ backgroundColor: 'var(--primary)', color: 'var(--on-solid)' }"
            >
              {{ userInitial }}
            </Avatar>
            <span
              v-if="!collapsed"
              class="sider-footer-label"
            >{{ authStore.user?.name || 'Admin' }}</span>
          </button>
        </Dropdown>
      </div>
    </LayoutSider>

    <!-- 移动端抽屉 Sider -->
    <LayoutSider
      v-else
      v-model:collapsed="drawerOpen"
      :trigger="null"
      :width="260"
      class="admin-sider admin-sider--drawer"
      :class="{ 'admin-sider--drawer-open': drawerOpen }"
    >
      <div class="sider-brand">
        <span class="sider-logo">MC</span>
        <span
          v-if="drawerOpen"
          class="sider-name"
        >Chiron Admin</span>
      </div>
      <div class="sider-menu">
        <Menu
          mode="inline"
          :selected-keys="selectedKeys"
          @click="handleMenuClick"
        >
          <template
            v-for="g in menuGroups"
            :key="g.key"
          >
            <MenuItemGroup :title="g.label">
              <MenuItem
                v-for="m in g.children"
                :key="m.key"
              >
                <template #icon>
                  <component :is="m.icon" />
                </template>
                {{ m.label }}
              </MenuItem>
            </MenuItemGroup>
          </template>
        </Menu>
      </div>
      <div class="sider-footer">
        <button
          type="button"
          class="sider-footer-btn"
          :title="$t('settings.notification')"
          :aria-label="$t('settings.notification')"
          @click="goNotifications"
        >
          <Badge
            :count="unreadCount"
            :overflow-count="99"
            size="small"
          >
            <BellOutlined />
          </Badge>
          <span class="sider-footer-label">{{ $t('settings.notification') }}</span>
        </button>
        <div class="sider-footer-inline">
          <LanguageSwitcher compact />
          <ThemeSwitcher />
        </div>
        <Dropdown
          :menu="{ items: userMenuItems, onClick: handleUserAction }"
          placement="topRight"
        >
          <button
            type="button"
            class="sider-footer-user"
            :title="authStore.user?.name || 'Admin'"
          >
            <Avatar
              :size="26"
              :style="{ backgroundColor: 'var(--primary)', color: 'var(--on-solid)' }"
            >
              {{ userInitial }}
            </Avatar>
            <span class="sider-footer-label">{{ authStore.user?.name || 'Admin' }}</span>
          </button>
        </Dropdown>
      </div>
    </LayoutSider>
    <div
      v-if="isMobile && drawerOpen"
      class="admin-drawer-mask"
      aria-hidden="true"
      @click="drawerOpen = false"
    />

    <Layout class="admin-main">
      <LayoutHeader class="admin-header">
        <div class="header-left">
          <Button
            type="text"
            class="header-collapse-btn"
            :title="$t('common.expand_or_collapse_navigation')"
            :aria-label="collapsed ? $t('common.expand_sidebar') : $t('common.collapse_sidebar')"
            @click="isMobile ? (drawerOpen = !drawerOpen) : (collapsed = !collapsed)"
          >
            <component :is="isMobile ? (drawerOpen ? MenuUnfoldOutlined : MenuFoldOutlined) : (collapsed ? MenuUnfoldOutlined : MenuFoldOutlined)" />
          </Button>
          <Breadcrumb class="header-breadcrumb">
            <BreadcrumbItem
              v-for="(item, index) in breadcrumbs"
              :key="item.path"
              :class="{ 'u-hide-sm': index > 0 && index < breadcrumbs.length - 1 }"
            >
              {{ item.title }}
            </BreadcrumbItem>
          </Breadcrumb>
        </div>
      </LayoutHeader>
      <LayoutContent class="admin-content">
        <router-view v-slot="{ Component }">
          <!-- 内容区错误边界：某个页面组件抛错时只替换内容区，侧边导航保持可用 -->
          <ErrorBoundary>
            <Transition
              name="fade"
              mode="out-in"
            >
              <component :is="Component" />
            </Transition>
          </ErrorBoundary>
        </router-view>
      </LayoutContent>
    </Layout>
  </Layout>
</template>

<style scoped>
/* ── 设计系统接入：Sider 使用 CSS 变量，不再硬编码颜色 ── */
.admin-root { height: 100vh; height: 100dvh; background: var(--bg-page); }

.admin-sider {
  background: var(--bg-card) !important;
  border-inline-end: var(--hairline-width) solid var(--border);
  position: relative;
  z-index: var(--z-dropdown);
}
.admin-sider :deep(.ant-layout-sider-children) { display: flex; flex-direction: column; }

/* 移动端抽屉：脱离布局，固定定位 */
.admin-sider--drawer {
  position: fixed !important;
  top: 0; left: 0; bottom: 0;
  transform: translateX(-100%);
  transition: transform var(--dur-normal) ease;
  z-index: var(--z-modal);
  box-shadow: var(--shadow-lg);
}
.admin-sider--drawer-open { transform: translateX(0); }
.admin-drawer-mask {
  position: fixed;
  inset: 0;
  background: var(--bg-overlay);
  z-index: var(--z-overlay);
}

/* 品牌区 */
.sider-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 56px;
  padding: 0 18px;
  border-bottom: var(--hairline-width) solid var(--border);
  flex-shrink: 0;
}
.sider-logo {
  width: 28px; height: 28px;
  border-radius: 7px;
  /* 原为 var(--primary-dark) —— 该变量**从未定义**，而含无效色标的
     linear-gradient 会被整体丢弃，logo 实际没有背景。用同色系更深一档的
     --primary-active 代替。 */
  background: linear-gradient(135deg, var(--primary), var(--primary-active));
  color: var(--on-solid);
  font-size: var(--fs-xs);
  font-weight: var(--fw-bold);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.sider-name {
  font-size: 15px;
  font-weight: var(--fw-semibold);
  color: var(--text-primary);
  letter-spacing: -0.01em;
  white-space: nowrap;
}

/* 菜单滚动区 */
.sider-menu { flex: 1; overflow-y: auto; overflow-x: hidden; padding: 8px 8px 16px; }
.sider-menu :deep(.ant-menu) {
  background: transparent !important;
  border: none !important;
}
.sider-menu :deep(.ant-menu-item-group-title) {
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-muted);
  padding: 14px 12px 4px;
}
.sider-menu :deep(.ant-menu-item) {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 36px;
  line-height: 36px;
  margin: 2px 0;
  border-radius: var(--radius-md);
  font-size: 13px;
  color: var(--text-secondary);
}
.sider-menu :deep(.ant-menu-item:hover) {
  background: var(--bg-hover) !important;
  color: var(--text-primary) !important;
}
.sider-menu :deep(.ant-menu-item-selected) {
  background: var(--primary-bg) !important;
  color: var(--primary) !important;
  font-weight: var(--fw-semibold);
}
.sider-menu :deep(.ant-menu-item-selected::after) { display: none; }

/* ── 侧栏底部：通知 / 语言 / 主题 / 用户 ── */
.sider-footer {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 12px;
  border-top: var(--hairline-width) solid var(--border);
  flex-shrink: 0;
}
.sider-footer-btn,
.sider-footer-user {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 6px 8px;
  font: inherit;
  font-size: 13px;
  text-align: start;
  color: var(--text-secondary);
  background: transparent;
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background var(--dur-fast) ease, color var(--dur-fast) ease;
}
.sider-footer-btn:hover,
.sider-footer-user:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}
.sider-footer-inline {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 2px 8px;
}
.sider-footer-label {
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.sider-footer--collapsed .sider-footer-btn,
.sider-footer--collapsed .sider-footer-user {
  justify-content: center;
  padding: 6px 0;
}
.sider-footer--collapsed .sider-footer-inline {
  justify-content: center;
  padding: 2px 0;
}

/* ── Header ── */
.admin-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px 0 12px;
  height: 56px;
  background: var(--bg-card) !important;
  border-bottom: var(--hairline-width) solid var(--border);
  flex-shrink: 0;
}
.header-left { display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1; }
.header-collapse-btn {
  flex-shrink: 0;
  width: 40px;
  height: 40px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--text-secondary);
}
.header-collapse-btn:hover { color: var(--text-primary); background: var(--bg-hover); }
.header-breadcrumb { flex: 1; min-width: 0; }
.header-breadcrumb :deep(.ant-breadcrumb) {
  overflow: hidden;
  white-space: nowrap;
}
.header-breadcrumb :deep(.ant-breadcrumb-link) {
  font-size: 13px;
  color: var(--text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.header-breadcrumb :deep(.ant-breadcrumb-link:last-child) {
  color: var(--text-primary);
  font-weight: var(--fw-medium);
}

.header-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.header-btn {
  width: 40px;
  height: 40px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--text-secondary);
}
.header-btn:hover { color: var(--text-primary); background: var(--bg-hover); }

.header-user {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 10px 4px 4px;
  border-radius: var(--radius-full);
  cursor: pointer;
  transition: background var(--dur-fast) ease;
}
.header-user:hover { background: var(--bg-hover); }
.header-user-name {
  font-size: 13px;
  color: var(--text-secondary);
  white-space: nowrap;
}
@media (max-width: 480px) {
  .header-user-name { display: none; }
}

/* ── Content ── */
.admin-content {
  padding: clamp(12px, 2.5vw, 24px);
  overflow: auto;
  background: var(--bg-page);
}

/* 过渡 */
.fade-enter-active, .fade-leave-active { transition: opacity var(--dur-normal) ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

/* 移动端抽屉菜单：菜单项提高至 40px 触控高度 */
@media (max-width: 960px) {
  .sider-menu :deep(.ant-menu-item) {
    height: 40px;
    line-height: 40px;
  }
}
</style>