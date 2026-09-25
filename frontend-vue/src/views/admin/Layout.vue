<script setup lang="ts">
import { ref, computed, h, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { useThemeStore } from '../../stores/theme'
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
  KeyOutlined,
  DatabaseOutlined,
  ThunderboltOutlined,
  SettingOutlined,
  ClockCircleOutlined,
  BellOutlined,
  UserOutlined,
  LogoutOutlined,
  TeamOutlined,
  SafetyOutlined,
  FileSearchOutlined,
  IdcardOutlined,
  WalletOutlined,
  PayCircleOutlined,
  ControlOutlined,
  
  FileTextOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  BulbOutlined,
  
  MailOutlined,
} from '@ant-design/icons-vue'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const themeStore = useThemeStore()

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

// 菜单分组：总览监控 / 访问安全 / 系统 / 平台 四组
const menuGroups = computed(() => [
  {
    key: 'g-monitor', label: t('总览监控'),
    children: [
      { key: '/admin/dashboard', label: t('仪表盘'), icon: () => h(DashboardOutlined) },
      // 原「性能监控 / 队列监控 / 缓存监控」三页已合并为运行时监控（Tabs）
      { key: '/admin/monitor', label: t('运行时监控'), icon: () => h(ThunderboltOutlined) },
    ],
  },
  {
    key: 'g-access', label: t('访问安全'),
    children: [
      { key: '/admin/api-keys', label: t('API Key 管理'), icon: () => h(KeyOutlined) },
      // 原「角色管理 + 群组管理」合并为权限与组织（Tabs）
      { key: '/admin/access', label: t('权限与组织'), icon: () => h(IdcardOutlined) },
      // 原「三方登录与人机验证 + 隐私模式管控」合并为认证与防护（Tabs）
      { key: '/admin/identity', label: t('认证与防护'), icon: () => h(SafetyOutlined) },
      // 邮件发信（邮箱验证码登录 / 注册邮箱验证 / 密码重置）的服务端配置
      { key: '/admin/mail', label: t('邮件配置'), icon: () => h(MailOutlined) },
      // 原「模型策略管控 + 模型路由管控」合并为模型管控（Tabs）
      { key: '/admin/models', label: t('模型管控'), icon: () => h(ControlOutlined) },
    ],
  },
  {
    key: 'g-system', label: t('系统'),
    children: [
      { key: '/admin/settings', label: t('系统设置'), icon: () => h(SettingOutlined) },
      // 定时任务：原在仪表盘内，属配置类操作 → 归入系统组
      { key: '/admin/cron', label: t('定时任务'), icon: () => h(ClockCircleOutlined) },
      // 原「Redis 管理 + 数据库管理」合并为数据存储（Tabs）
      { key: '/admin/datastores', label: t('数据存储'), icon: () => h(DatabaseOutlined) },
      // 原「租户管理 + 域名管理」合并为租户与域名（Tabs）
      { key: '/admin/tenancy', label: t('租户与域名'), icon: () => h(TeamOutlined) },
      { key: '/admin/audit', label: t('操作审计'), icon: () => h(FileSearchOutlined) },
    ],
  },
  {
    key: 'g-platform', label: t('平台'),
    children: [
      { key: '/admin/costcenter', label: t('成本中心'), icon: () => h(WalletOutlined) },
      // 支付渠道凭据：与成本中心同属商业化运营 → 平台组
      { key: '/admin/payment', label: t('支付配置'), icon: () => h(PayCircleOutlined) },
      { key: '/admin/api-docs', label: t('API 文档'), icon: () => h(FileTextOutlined) },
    ],
  },
])

// 当前路由命中的菜单项（用于侧边栏 selectedKeys）
const selectedKeys = computed(() => {
  const hit = menuGroups.value
    .flatMap(g => g.children)
    .find(m => route.path === m.key || route.path.startsWith(m.key + '/'))
  return [hit?.key ?? route.path]
})

const userMenuItems = computed<any[]>(() => [
  { key: 'profile', label: t('个人资料'), icon: () => h(UserOutlined) },
  { key: 'toggle-theme', label: themeStore.isDark ? t('浅色模式') : t('深色模式'), icon: () => h(BulbOutlined) },
  { type: 'divider' as const },
  { key: 'logout', label: t('退出登录'), icon: () => h(LogoutOutlined) },
])

function handleMenuClick(info: any) {
  router.push(info.key)
}

async function handleUserAction(info: any) {
  if (info.key === 'logout') {
    // 安全：调 authStore.logout 清后端 httpOnly cookie + 本地 user
    await authStore.logout()
    router.push('/login')
  } else if (info.key === 'profile') {
    router.push('/profile')
  } else if (info.key === 'toggle-theme') {
    themeStore.toggleTheme()
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
            :title="$t('展开或收起导航')"
            :aria-label="collapsed ? $t('展开侧边栏') : $t('收起侧边栏')"
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
        <div class="header-actions">
          <Badge
            :count="0"
            :overflow-count="99"
          >
            <Button
              type="text"
              class="header-btn"
              :title="$t('通知')"
              :aria-label="$t('通知')"
            >
              <template #icon>
                <BellOutlined />
              </template>
            </Button>
          </Badge>
          <Dropdown
            :menu="{ items: userMenuItems, onClick: handleUserAction }"
            placement="bottomRight"
          >
            <div
              class="header-user"
              :title="$t('用户菜单')"
            >
              <Avatar
                :size="30"
                :style="{ backgroundColor: 'var(--primary)', color: 'var(--on-solid)' }"
              >
                {{ userInitial }}
              </Avatar>
              <span class="header-user-name">{{ authStore.user?.name || 'Admin' }}</span>
            </div>
          </Dropdown>
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
  border-right: 1px solid var(--border);
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
  border-bottom: 1px solid var(--border);
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
  font-size: 11px;
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.sider-name {
  font-size: 15px;
  font-weight: 600;
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
  font-size: 10px;
  font-weight: 600;
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
  border-radius: 6px;
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
  font-weight: 600;
}
.sider-menu :deep(.ant-menu-item-selected::after) { display: none; }

/* ── Header ── */
.admin-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px 0 12px;
  height: 56px;
  background: var(--bg-card) !important;
  border-bottom: 1px solid var(--border);
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
  font-weight: 500;
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
  border-radius: 999px;
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