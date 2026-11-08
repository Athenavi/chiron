<script setup lang="ts">
/**
 * 租户管理员后台布局（/tenant/**）。
 *
 * 与平台后台（/admin）是**两套**入口：
 *   - 平台后台：租户与域名、租户级用量、操作审计、系统诊断等平台运维能力；
 *   - 租户后台：本租户的身份与安全、支付、发票、域名、成员/角色、用量、套餐。
 *
 * 两者共用同一套路由守卫（requiresAdmin）——平台 owner 与租户 admin 都可进入，
 * 数据隔离由后端按 claims.tenant_id 负责。
 */
import { computed, h, onMounted, onUnmounted, ref } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useAuthStore } from '../../stores/auth'
import ThemeSwitcher from '../../components/ThemeSwitcher.vue'
import LanguageSwitcher from '../../components/common/LanguageSwitcher.vue'
import {
  Avatar,
  Breadcrumb,
  BreadcrumbItem,
  Button,
  Drawer,
  Dropdown,
  Layout,
  LayoutContent,
  LayoutHeader,
  LayoutSider,
  Menu,
  MenuItem,
  MenuItemGroup,
} from 'ant-design-vue'
import {
  BarChartOutlined,
  ControlOutlined,
  DashboardOutlined,
  GlobalOutlined,
  HomeOutlined,
  MailOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  PayCircleOutlined,
  ReconciliationOutlined,
  SafetyOutlined,
  FileSearchOutlined,
  SettingOutlined,
  ShoppingOutlined,
  WalletOutlined,
  TeamOutlined,
  UserOutlined,
  LogoutOutlined,
} from '@ant-design/icons-vue'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

const collapsed = ref(false)
const isMobile = ref(false)
const drawerOpen = ref(false)

const menuGroups = computed(() => [
  {
    key: 'g-tenant-config', label: t('admin.tenant_config'),
    children: [
      { key: '/tenant', label: t('admin.overview'), icon: () => h(DashboardOutlined) },
      { key: '/tenant/identity', label: t('common.authentication_and_protection'), icon: () => h(SafetyOutlined) },
      { key: '/tenant/payment', label: t('billing.payment_config'), icon: () => h(PayCircleOutlined) },
      { key: '/tenant/invoices', label: t('admin.invoice_review'), icon: () => h(ReconciliationOutlined) },
      { key: '/tenant/domains', label: t('admin.tenants_and_domains'), icon: () => h(GlobalOutlined) },
      { key: '/tenant/access', label: t('errors.permissions_and_organization'), icon: () => h(TeamOutlined) },
      { key: '/tenant/users', label: t('admin.user_management'), icon: () => h(UserOutlined) },
      { key: '/tenant/audit', label: t('admin.operation_audit'), icon: () => h(FileSearchOutlined) },
      { key: '/tenant/site', label: t('admin.site_settings'), icon: () => h(SettingOutlined) },
    ],
  },
  {
    key: 'g-tenant-business', label: t('common.platform'),
    children: [
      { key: '/tenant/usage', label: t('admin.usage_insights'), icon: () => h(BarChartOutlined) },
      { key: '/tenant/quota', label: t('admin.quota_title'), icon: () => h(ControlOutlined) },
      { key: '/tenant/packages', label: t('admin.packages_admin_title'), icon: () => h(ShoppingOutlined) },
      { key: '/tenant/refunds', label: t('admin.refunds'), icon: () => h(WalletOutlined) },
      { key: '/tenant/mail', label: t('mail.email_config'), icon: () => h(MailOutlined) },
    ],
  },
])

const selectedKeys = computed(() => {
  const hit = menuGroups.value
    .flatMap(g => g.children)
    .find(m => route.path === m.key || route.path.startsWith(m.key + '/'))
  return hit ? [hit.key] : []
})

const breadcrumbs = computed(() => route.matched
  .filter(r => r.meta?.title || r.name)
  .map(item => ({
    path: item.path,
    title: (item.meta?.title as string) || (item.name as string) || '',
  })))

const userInitial = computed(() => authStore.user?.name?.charAt(0)?.toUpperCase() || 'T')

const userMenuItems = computed(() => [
  { key: 'profile', label: t('settings.profile'), icon: () => h(UserOutlined) },
  { key: 'home', label: t('common.home'), icon: () => h(HomeOutlined) },
  { type: 'divider' as const },
  { key: 'logout', label: t('auth.logout'), icon: () => h(LogoutOutlined) },
])

function onMenuClick(info: { key: string | number }) {
  router.push(String(info.key))
  drawerOpen.value = false
}

async function onUserAction(info: { key: string | number }) {
  const key = String(info.key)
  if (key === 'logout') {
    await authStore.logout()
    router.push('/login')
  } else if (key === 'profile') {
    router.push('/profile')
  } else if (key === 'home') {
    router.push('/')
  }
}

function toggleSider() {
  if (isMobile.value) drawerOpen.value = true
  else collapsed.value = !collapsed.value
}

function updateIsMobile() {
  isMobile.value = window.innerWidth <= 768
}

onMounted(() => {
  updateIsMobile()
  window.addEventListener('resize', updateIsMobile)
})
onUnmounted(() => {
  window.removeEventListener('resize', updateIsMobile)
})
</script>

<template>
  <Layout class="tenant-root">
    <LayoutSider
      v-if="!isMobile"
      v-model:collapsed="collapsed"
      collapsible
      :trigger="null"
      :width="240"
      :collapsed-width="72"
      class="tenant-sider"
    >
      <div class="sider-brand">
        <span class="sider-logo">T</span>
        <span
          v-if="!collapsed"
          class="sider-name"
        >{{ $t('admin.tenant_console') }}</span>
      </div>
      <div class="sider-menu">
        <Menu
          mode="inline"
          :selected-keys="selectedKeys"
          :inline-collapsed="collapsed"
          @click="onMenuClick"
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
      <div
        class="sider-footer"
        :class="{ 'sider-footer--collapsed': collapsed }"
      >
        <div class="sider-footer-inline">
          <LanguageSwitcher compact />
          <ThemeSwitcher />
        </div>
        <Dropdown
          :menu="{ items: userMenuItems, onClick: onUserAction }"
          placement="topRight"
        >
          <button
            type="button"
            class="sider-footer-user"
            :title="authStore.user?.name || 'Tenant'"
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
            >{{ authStore.user?.name || 'Tenant' }}</span>
          </button>
        </Dropdown>
      </div>
    </LayoutSider>

    <Layout class="tenant-main">
      <LayoutHeader class="tenant-header">
        <Button
          type="text"
          :aria-label="collapsed ? $t('common.expand') : $t('common.collapse')"
          @click="toggleSider"
        >
          <MenuUnfoldOutlined v-if="collapsed" />
          <MenuFoldOutlined v-else />
        </Button>
        <Breadcrumb>
          <BreadcrumbItem
            v-for="b in breadcrumbs"
            :key="b.path"
          >
            {{ b.title }}
          </BreadcrumbItem>
        </Breadcrumb>
        <span class="tenant-header-spacer" />
        <Button
          type="link"
          @click="router.push('/')"
        >
          {{ $t('common.home') }}
        </Button>
      </LayoutHeader>
      <LayoutContent class="tenant-content">
        <RouterView />
      </LayoutContent>
    </Layout>
  </Layout>

  <Drawer
    v-if="isMobile"
    v-model:open="drawerOpen"
    placement="left"
    :width="260"
    :body-style="{ padding: 0 }"
  >
    <div class="drawer-menu">
      <Menu
        mode="inline"
        :selected-keys="selectedKeys"
        @click="onMenuClick"
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
  </Drawer>
</template>

<style scoped>
.tenant-root { min-height: 100vh; background: var(--bg-app); }
.tenant-sider {
  position: sticky;
  top: 0;
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: var(--bg-sider, var(--bg-card));
  border-inline-end: var(--hairline-width) solid var(--border);
}
.tenant-main { min-width: 0; }
.tenant-header {
  display: flex;
  align-items: center;
  gap: 12px;
  height: 56px;
  padding: 0 16px;
  background: var(--bg-card);
  border-bottom: var(--hairline-width) solid var(--border);
}
.tenant-header-spacer { flex: 1; }
.tenant-content { padding: 16px; overflow-x: hidden; }

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
  width: 28px;
  height: 28px;
  border-radius: 7px;
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
  white-space: nowrap;
}
.sider-menu { flex: 1; overflow-y: auto; padding: 8px 8px 16px; }
.sider-menu :deep(.ant-menu),
.drawer-menu :deep(.ant-menu) { background: transparent !important; border: none !important; }
.sider-menu :deep(.ant-menu-item-group-title) {
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  letter-spacing: 0.08em;
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
.sider-menu :deep(.ant-menu-item-selected) {
  background: var(--primary-soft);
  color: var(--primary);
}
.sider-footer {
  padding: 10px;
  border-top: var(--hairline-width) solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.sider-footer-inline { display: flex; align-items: center; gap: 6px; }
.sider-footer-user {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 4px 6px;
  border: none;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  text-align: start;
}
.sider-footer-user:hover { background: var(--bg-hover); }
.sider-footer-label { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sider-footer--collapsed .sider-footer-inline { flex-direction: column; }
.drawer-menu { padding: 8px; }
</style>
