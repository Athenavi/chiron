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
    // 与后端 /v1/admin/models 的权限口径一致）
    path: '/models',
    name: 'Models',
    component: () => import('../views/ModelsView.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
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
    path: '/billing',
    name: 'Billing',
    component: () => import('../views/BillingView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/profile',
    name: 'Profile',
    component: () => import('../views/ProfileView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/media',
    name: 'Media',
    component: () => import('../views/MediaView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/workflow',
    name: 'Workflow',
    component: () => import('../views/WorkflowView.vue'),
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
  // 管理后台路由
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
        meta: { title: t('仪表盘') },
      },
      {
        path: 'api-keys',
        name: 'AdminApiKeys',
        component: () => import('../views/admin/ApiKeysView.vue'),
        meta: { title: t('API Key 管理') },
      },
      {
        // 运行时监控：原「性能监控 / 队列监控 / 缓存监控」三页合并为一个 Tabs 页面（懒加载）
        path: 'monitor',
        name: 'AdminMonitor',
        component: () => import('../views/admin/MonitorView.vue'),
        meta: { title: t('运行时监控') },
      },
      {
        // 定时任务：从仪表盘拆出的配置类页面
        path: 'cron',
        name: 'AdminCron',
        component: () => import('../views/admin/CronView.vue'),
        meta: { title: t('定时任务') },
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
        meta: { title: t('系统设置') },
      },
      // ── 邮件发信（邮箱验证码登录 / 注册邮箱验证 / 密码重置 / 欢迎邮件）──
      // 发信服务器地址与凭据全部在此配置，代码中不含任何厂商默认地址。
      {
        path: 'mail',
        name: 'AdminMail',
        component: () => import('../views/admin/MailView.vue'),
        meta: { title: t('邮件配置') },
      },
      // ── 支付渠道配置（支付宝 / 微信支付 / PayPal）──
      // 凭据加密入库 + 保存后热生效；原「系统设置」里的支付卡片已迁移到此处，
      // 避免同一份配置在两处维护。
      {
        path: 'payment',
        name: 'AdminPayment',
        component: () => import('../views/admin/PaymentView.vue'),
        meta: { title: t('支付配置') },
      },
      // ── 数据存储（原「Redis 管理」+「数据库管理」合并）──
      {
        path: 'datastores',
        name: 'AdminDataStores',
        component: () => import('../views/admin/DataStoresView.vue'),
        meta: { title: t('数据存储') },
      },
      // ── 租户与域名（原「租户管理」+「域名管理」合并）──
      {
        path: 'tenancy',
        name: 'AdminTenancy',
        component: () => import('../views/admin/TenancyView.vue'),
        meta: { title: t('租户与域名') },
      },
      {
        path: 'audit',
        name: 'AdminAudit',
        component: () => import('../views/admin/AuditView.vue'),
        meta: { title: t('操作审计') },
      },
      // ── 访问安全 ──
      {
        // 角色 + 群组合并
        path: 'access',
        name: 'AdminAccess',
        component: () => import('../views/admin/AccessView.vue'),
        meta: { title: t('权限与组织') },
      },
      {
        // 三方登录与人机验证 + 隐私模式管控合并
        path: 'identity',
        name: 'AdminIdentity',
        component: () => import('../views/admin/IdentityView.vue'),
        meta: { title: t('认证与防护') },
      },
      {
        // 模型策略 + 模型路由合并
        path: 'models',
        name: 'AdminModels',
        component: () => import('../views/admin/ModelGovernanceView.vue'),
        meta: { title: t('模型管控') },
      },
      // ── 平台 ──
      {
        path: 'costcenter',
        name: 'AdminCostCenter',
        component: () => import('../views/admin/CostCenterView.vue'),
        meta: { title: t('成本中心') },
      },
      {
        path: 'market',
        name: 'AdminMarket',
        component: () => import('../views/admin/MarketView.vue'),
        meta: { title: t('企业能力市场') },
      },
      {
        path: 'api-docs',
        name: 'AdminApiDocs',
        component: () => import('../views/admin/ApiDocsView.vue'),
        meta: { title: t('API 文档') },
      },
      // ── 旧入口重定向（书签 / 文档 / 告警链接兼容）──
      { path: 'roles', redirect: { path: '/admin/access', query: { tab: 'roles' } } },
      { path: 'groups', redirect: { path: '/admin/access', query: { tab: 'groups' } } },
      { path: 'oauth-providers', redirect: { path: '/admin/identity', query: { tab: 'oauth' } } },
      { path: 'privacy', redirect: { path: '/admin/identity', query: { tab: 'privacy' } } },
      { path: 'model-policy', redirect: { path: '/admin/models', query: { tab: 'policy' } } },
      { path: 'model-router', redirect: { path: '/admin/models', query: { tab: 'routing' } } },
      { path: 'redis', redirect: { path: '/admin/datastores', query: { tab: 'redis' } } },
      { path: 'database', redirect: { path: '/admin/datastores', query: { tab: 'database' } } },
      { path: 'tenants', redirect: { path: '/admin/tenancy', query: { tab: 'tenants' } } },
      { path: 'domains', redirect: { path: '/admin/tenancy', query: { tab: 'domains' } } },
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
    () => import('../views/WorkflowView.vue'),
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
