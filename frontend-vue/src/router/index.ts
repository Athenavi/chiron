import { createRouter, createWebHistory } from 'vue-router'
import { authGuard } from './guard'
import { setupRouteProgress } from './progress'

import { t } from '../i18n'
const routes = [
  {
    path: '/',
    name: 'Home',
    component: () => import('../views/HomeView.vue'),
  },
  {
    // 登录 / 注册 / 找回密码。
    path: '/login',
    name: 'Login',
    component: () => import('../views/LoginView.vue'),
  },
  {
    path: '/register',
    name: 'Register',
    component: () => import('../views/RegisterView.vue'),
  },
  {
    // 忘记密码：申请邮件重置链接（邮件服务未启用时后端 403，入口在登录页按状态隐藏）
    path: '/forgot-password',
    name: 'ForgotPassword',
    component: () => import('../views/ForgotPasswordView.vue'),
  },
  {
    // 重置密码：令牌由邮件里的 /reset-password?token=... 链接带入
    path: '/reset-password',
    name: 'ResetPassword',
    component: () => import('../views/ResetPasswordView.vue'),
  },
  {
    path: '/chat',
    name: 'Chat',
    component: () => import('../views/ChatView.vue'),
    meta: { requiresAuth: true },
  },
  {
    // 模型配置：决定 /chat 模型下拉里能选到什么（写 llm_models 全局表，
    // 与后端 /v1/admin/models 的权限口径一致）。
    path: '/models',
    name: 'Models',
    component: () => import('../views/ModelsView.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
  },
  {
    // 参考价格目录：只读，数据经多源交叉验证后由引擎侧同步任务写入。
    // 与 /models（配置哪些模型可选）分开放：一个是"我能用哪些"，一个是"市场上多少钱"。
    path: '/pricing',
    name: 'Pricing',
    component: () => import('../views/PricingView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/share/:id',
    name: 'Share',
    component: () => import('../views/ShareView.vue'),
    // 注意：无 requiresAuth — 共享会话可能公开访问，后端应验证 share_id 的权限范围
  },
  {
    path: '/agents',
    name: 'Agents',
    component: () => import('../views/AgentsView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/skills',
    name: 'Skills',
    component: () => import('../views/SkillsView.vue'),
    meta: { requiresAuth: true },
  },
  {
    // 计费 / 充值
    path: '/billing',
    name: 'Billing',
    component: () => import('../views/BillingView.vue'),
    meta: { requiresAuth: true },
  },
  {
    // 个人用量：与账单同源（/v1/billing/usage），但独立成页 —— 用户查"我这个月
    // 花在哪个模型/会话上"不该先翻到计费页的 tab 里。
    path: '/usage',
    name: 'Usage',
    component: () => import('../views/UsageView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/profile',
    name: 'Profile',
    component: () => import('../views/ProfileView.vue'),
    meta: { requiresAuth: true },
  },
  {
    // 用户级 API Key 自助：管的是「我自己的账号调网关的凭证」，
    // 与 admin 的 LLM 供应商密钥池无关。
    path: '/api-keys',
    name: 'UserApiKeys',
    component: () => import('../views/UserApiKeysView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/notifications',
    name: 'Notifications',
    component: () => import('../views/NotificationsView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/media',
    name: 'Media',
    component: () => import('../views/MediaView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/plugins',
    name: 'Plugins',
    component: () => import('../views/PluginsView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/knowledge',
    name: 'Knowledge',
    component: () => import('../views/KnowledgeView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/knowledge/settings',
    name: 'KnowledgeSettings',
    component: () => import('../views/KnowledgeSettingsView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/memory',
    name: 'Memory',
    component: () => import('../views/MemoryView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/knowledge/:id',
    name: 'KnowledgeDetail',
    component: () => import('../views/KnowledgeDetailView.vue'),
    meta: { requiresAuth: true },
  },
  // 管理后台路由：角色与权限判定见 router/guard.ts。
  {
    path: '/admin',
    component: () => import('../views/admin/Layout.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
    children: [
      {
        path: '',
        redirect: '/admin/dashboard',
      },
      {
        path: 'dashboard',
        name: 'AdminDashboard',
        component: () => import('../views/admin/DashboardView.vue'),
        meta: { title: t('common.dashboard') },
      },
      {
        path: 'api-keys',
        name: 'AdminApiKeys',
        component: () => import('../views/admin/ApiKeysView.vue'),
        meta: { title: t('common.api_key_management') },
      },
      {
        // 运行时监控：原「性能监控 / 队列监控 / 缓存监控」三页合并为一个 Tabs 页面（懒加载）
        path: 'monitor',
        name: 'AdminMonitor',
        component: () => import('../views/admin/MonitorView.vue'),
        meta: { title: t('admin.runtime_monitoring') },
      },
      // 旧入口保留重定向，避免书签 / 文档 / 告警链接失效
      {
        path: 'performance',
        redirect: { path: '/admin/monitor', query: { tab: 'performance' } },
      },
      {
        path: 'queue',
        redirect: { path: '/admin/monitor', query: { tab: 'queue' } },
      },
      {
        path: 'cache',
        redirect: { path: '/admin/monitor', query: { tab: 'cache' } },
      },
      {
        path: 'settings',
        name: 'AdminSettings',
        component: () => import('../views/admin/SettingsView.vue'),
        meta: { title: t('settings.system_settings') },
      },
      // ── 数据存储（原「Redis 管理」+「数据库管理」合并）──
      {
        path: 'datastores',
        name: 'AdminDataStores',
        component: () => import('../views/admin/DataStoresView.vue'),
        meta: { title: t('common.data_storage') },
      },
      // ── 租户与域名（原「租户管理」+「域名管理」合并）──
      {
        path: 'tenancy',
        name: 'AdminTenancy',
        component: () => import('../views/admin/TenancyView.vue'),
        meta: { title: t('admin.tenants_and_domains') },
      },
      {
        path: 'audit',
        name: 'AdminAudit',
        component: () => import('../views/admin/AuditView.vue'),
        meta: { title: t('admin.operation_audit') },
      },
      // D1：自诊断（"配了但不生效"的自答清单）
      {
        path: 'diagnostics',
        name: 'AdminDiagnostics',
        component: () => import('../views/admin/DiagnosticsView.vue'),
        meta: { title: t('admin.diagnostics') },
      },
      // ── 平台 ──
      // 团队 / 租户用量：替代已下线的「用量目录」——按天/成员/租户看 token 与成本构成。
      {
        path: 'usage',
        name: 'AdminUsage',
        component: () => import('../views/admin/UsageInsightsView.vue'),
        meta: { title: t('admin.usage_insights') },
      },
      {
        // 平台套餐模板：租户新建套餐时可从这里预填（平台后台维护模板本身）。
        path: 'package-templates',
        name: 'AdminPackageTemplates',
        component: () => import('../views/admin/PackagesView.vue'),
        meta: { title: t('admin.package_templates_title') },
      },
      {
        path: 'market',
        name: 'AdminMarket',
        component: () => import('../views/admin/MarketView.vue'),
        meta: { title: t('common.enterprise_capability_marketplace') },
      },
      {
        path: 'api-docs',
        name: 'AdminApiDocs',
        component: () => import('../views/admin/ApiDocsView.vue'),
        meta: { title: t('knowledge.api_documentation') },
      },
      // ── 旧入口重定向（书签 / 文档 / 告警链接兼容）──
      { path: 'redis', redirect: { path: '/admin/datastores', query: { tab: 'redis' } } },
      { path: 'database', redirect: { path: '/admin/datastores', query: { tab: 'database' } } },
      { path: 'tenants', redirect: { path: '/admin/tenancy', query: { tab: 'tenants' } } },
      { path: 'domains', redirect: { path: '/admin/tenancy', query: { tab: 'domains' } } },
    ],
  },
  // 租户管理员后台：与 /admin（平台）并列的一套入口，菜单不同、数据按租户隔离。
  {
    path: '/tenant',
    component: () => import('../views/tenant/Layout.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
    children: [
      {
        path: '',
        name: 'TenantOverview',
        component: () => import('../views/tenant/OverviewView.vue'),
        meta: { title: t('admin.overview') },
      },
      {
        path: 'identity',
        name: 'TenantIdentity',
        component: () => import('../views/admin/IdentityView.vue'),
        meta: { title: t('common.authentication_and_protection') },
      },
      {
        path: 'payment',
        name: 'TenantPayment',
        component: () => import('../views/admin/PaymentView.vue'),
        meta: { title: t('billing.payment_config') },
      },
      {
        path: 'invoices',
        name: 'TenantInvoices',
        component: () => import('../views/admin/InvoiceReviewView.vue'),
        meta: { title: t('admin.invoice_review') },
      },
      {
        path: 'domains',
        name: 'TenantDomains',
        component: () => import('../views/admin/DomainManagementView.vue'),
        meta: { title: t('admin.tenants_and_domains') },
      },
      {
        path: 'access',
        name: 'TenantAccess',
        component: () => import('../views/admin/AccessView.vue'),
        meta: { title: t('errors.permissions_and_organization') },
      },
      {
        path: 'users',
        name: 'TenantUsers',
        component: () => import('../views/tenant/UsersView.vue'),
        meta: { title: t('admin.user_management') },
      },
      {
        path: 'audit',
        name: 'TenantAudit',
        component: () => import('../views/admin/AuditView.vue'),
        meta: { title: t('admin.operation_audit') },
      },
      {
        path: 'site',
        name: 'TenantSite',
        component: () => import('../views/tenant/SiteSettingsView.vue'),
        meta: { title: t('admin.site_settings') },
      },
      {
        path: 'refunds',
        name: 'TenantRefunds',
        component: () => import('../views/tenant/RefundsView.vue'),
        meta: { title: t('admin.refunds') },
      },
      {
        path: 'usage',
        name: 'TenantUsage',
        component: () => import('../views/admin/UsageInsightsView.vue'),
        meta: { title: t('admin.usage_insights') },
      },
      {
        path: 'packages',
        name: 'TenantPackages',
        component: () => import('../views/admin/PackagesView.vue'),
        meta: { title: t('admin.packages_admin_title') },
      },
      {
        path: 'mail',
        name: 'TenantMail',
        component: () => import('../views/admin/MailView.vue'),
        meta: { title: t('mail.email_config') },
      },
    ],
  },
  // 404 兜底：避免未知地址白屏
  {
    path: '/:pathMatch(.*)*',
    name: 'NotFound',
    component: () => import('../views/NotFoundView.vue'),
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior(_to, _from, savedPosition) {
    if (savedPosition) return savedPosition
    return { top: 0 }
  },
})

// 路由守卫（逻辑在 guard.ts，独立可测）
router.beforeEach(authGuard)

// 路由进度条
setupRouteProgress(router)

// 路由预加载：页面空闲时预加载常用路由
// 使用 RequestIdleCallback 避免阻塞主线程
if (import.meta.env.PROD && 'requestIdleCallback' in window) {
  const ROUTE_CHUNKS = [
    () => import('../views/ChatView.vue'),
    () => import('../views/AgentsView.vue'),
    () => import('../views/ProfileView.vue'),
  ]

  const preloadIfNeeded = (): void => {
    if ('connection' in navigator) {
      const conn = (navigator as Navigator & { connection?: { saveData?: boolean; effectiveType?: string } }).connection
      if (!conn) return
      // 弱网或省流模式不预加载
      if (conn.saveData || /2g|3g/i.test(conn.effectiveType)) return
    }
    ROUTE_CHUNKS.forEach(imp => imp().catch(() => {}))
  }

  requestIdleCallback(preloadIfNeeded, { timeout: 3000 })
}

export default router
