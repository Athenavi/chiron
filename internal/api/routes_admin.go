package api

import (
	"net/http"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/engine"
)

// routes_admin.go —— 管理后台的路由注册。
// 由 gateway_router.go 按业务域拆分而来（纯移动：函数签名、函数体、调用点一字未改）。

// ── Admin ──

func registerAdminRoutes(
	mux *http.ServeMux,
	authMW routeMiddleware,
	rlMW routeMiddleware,
	adminHandler *AdminHandler,
	pythonClient *engine.PythonClient,
	invoiceHandler *InvoiceHandler,
	billingHandler *BillingHandler,
	quotaHandler *QuotaHandler,
) {
	// Admin routes (auth + admin permission + rate limit)
	// P1-3: 所有 admin 路由必须挂限流，防止被劫持的 admin token 无限调用
	// 造成破坏（backup/restore/users DELETE 等敏感操作）。
	// 读操作用 PermAdminRead，写操作（PUT/DELETE/POST）必须 PermAdminWrite。
	// P1-4: 用户管理路由（PUT/DELETE /v1/admin/users）必须 PermUsersManage，
	// 该权限仅 owner 角色持有，普通 admin 不应能删/改用户。
	adminReadMW := RequirePermission(auth.PermAdminRead)
	adminWriteMW := RequirePermission(auth.PermAdminWrite)
	// 平台级端点：`admin:write` 是租户 admin 就持有的权限，光靠它挡不住跨租户操作。
	// 见 middleware.go 的 RequirePlatformOwner 注释（租户 admin 可读全库 / 删任意租户）。
	platformReadMW := RequirePlatformOwner(auth.PermAdminRead)
	platformWriteMW := RequirePlatformOwner(auth.PermAdminWrite)
	usersManageMW := RequirePermission(auth.PermUsersManage)
	adminMux := http.NewServeMux()
	adminHandler.RegisterRoutes(adminMux)
	// 发票审核（P1-2 / roadmap §11.4）：注册在 adminMux 内部，才能拿到 {id} 路径参数
	// （外层 mux 已按 /v1/admin 前缀统一套了 authMW + 权限），用法与 adminHandler 一致。
	invoiceHandler.RegisterAdminRoutes(adminMux)
	// 套餐目录已改为租户级（/v1/tenant/packages），平台后台不再提供管理入口。
	// Strip the /v1/admin prefix so adminMux patterns (e.g. "GET /metrics") match correctly
	adminStrip := http.StripPrefix("/v1/admin", adminMux)
	mux.Handle("GET /v1/admin/metrics", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("GET /v1/admin/users", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("GET /v1/admin/users/{id}", authMW(rlMW(adminReadMW(adminStrip))))
	// 用户写操作收紧为 PermUsersManage（仅 owner）
	mux.Handle("PUT /v1/admin/users/{id}", authMW(rlMW(usersManageMW(adminStrip))))
	mux.Handle("DELETE /v1/admin/users/{id}", authMW(rlMW(usersManageMW(adminStrip))))
	mux.Handle("GET /v1/admin/system", authMW(rlMW(adminReadMW(adminStrip))))
	// D1：自诊断报告（只读）—— 权限口径与其它 admin 读端点一致。
	mux.Handle("GET /v1/admin/diagnostics", authMW(rlMW(adminReadMW(adminStrip))))
	// D3：用量目录（只读）—— 与其它 admin 读端点同一权限口径。
	mux.Handle("GET /v1/admin/usage/catalog", authMW(rlMW(adminReadMW(adminStrip))))
	// 租户级用量：本租户按天/模型/成员（平台侧只允许租户级）。
	mux.Handle("GET /v1/tenant/usage/catalog", authMW(rlMW(adminReadMW(http.HandlerFunc(adminHandler.GetTenantUsageCatalog)))))
	// 新用户引导漏斗（P0-3 验收「完成率可查」）—— 只读统计，与其它 admin 读端点同口径。
	mux.Handle("GET /v1/admin/onboarding/funnel", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("POST /v1/admin/maintenance", authMW(rlMW(platformWriteMW(adminStrip))))
	mux.Handle("GET /v1/admin/kb", authMW(rlMW(adminReadMW(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if pythonClient == nil || !pythonClient.IsConnected() {
			InternalError(w, "python engine not available")
			return
		}
		claims := auth.GetClaims(r.Context())
		r.Header.Set("X-User-Role", claims.Role)
		pythonClient.ForwardRequest(w, r, "/v1/admin/kb?user_id="+claims.UserID+"&tenant_id="+tenantStr(claims.TenantID))
	})))))

	// Storage admin routes
	mux.Handle("GET /v1/admin/storage", authMW(rlMW(platformReadMW(adminStrip))))
	mux.Handle("PUT /v1/admin/storage", authMW(rlMW(platformWriteMW(adminStrip))))
	mux.Handle("POST /v1/admin/storage/test", authMW(rlMW(platformWriteMW(adminStrip))))

	// Redis admin routes
	mux.Handle("GET /v1/admin/redis", authMW(rlMW(platformReadMW(adminStrip))))
	mux.Handle("PUT /v1/admin/redis", authMW(rlMW(platformWriteMW(adminStrip))))
	mux.Handle("POST /v1/admin/redis/test", authMW(rlMW(platformWriteMW(adminStrip))))
	// 慢日志 / 清库：handler 早已注册在 adminMux（admin_ops.go:56-57），但**从未在此暴露到外部 mux**
	// ⇒ 前端「刷新慢日志」「执行 FLUSHALL」恒 404（与下面 models / settings 是同一个坑）。
	// FLUSHDB 是**破坏性写操作** ⇒ 必须走 adminWriteMW，不能给读取权限。
	mux.Handle("GET /v1/admin/redis/slow-log", authMW(rlMW(platformReadMW(adminStrip))))
	mux.Handle("POST /v1/admin/redis/flush-all", authMW(rlMW(platformWriteMW(adminStrip))))

	// Database admin routes（/admin/datastores?tab=database 整页依赖它们）
	// 同样的问题：handler 早在 adminMux 里注册（admin_ops.go:47-53），但从未暴露 ⇒
	// 「数据库状态 / 配置 / 查询 / 优化」四个端点全部 404。
	// 判据照本文件既定口径（见文件头 P1-3）：GET 用 adminReadMW；
	// 任意 SELECT、VACUUM/ANALYZE 都是**写操作** ⇒ 一律 adminWriteMW。
	// 备份 / 恢复端点已移除：企业部署用云数据库（快照/PITR），容器内 pg_dump/psql 不适用。
	mux.Handle("GET /v1/admin/database/status", authMW(rlMW(platformReadMW(adminStrip))))
	mux.Handle("GET /v1/admin/database/configs", authMW(rlMW(platformReadMW(adminStrip))))
	mux.Handle("POST /v1/admin/database/query", authMW(rlMW(platformWriteMW(adminStrip))))
	mux.Handle("POST /v1/admin/database/optimize/{action}", authMW(rlMW(platformWriteMW(adminStrip))))

	// Queue admin routes
	mux.Handle("GET /v1/admin/queue", authMW(rlMW(platformReadMW(adminStrip))))
	mux.Handle("POST /v1/admin/queue/flush", authMW(rlMW(platformWriteMW(adminStrip))))
	mux.Handle("POST /v1/admin/queue/pause", authMW(rlMW(platformWriteMW(adminStrip))))

	// Cache admin routes
	mux.Handle("GET /v1/admin/cache/stats", authMW(rlMW(platformReadMW(adminStrip))))

	// Performance admin routes
	mux.Handle("GET /v1/admin/performance", authMW(rlMW(platformReadMW(adminStrip))))

	// API Key admin routes (direct handlers, avoid adminMux path mismatch)
	mux.Handle("GET /v1/admin/api-keys", authMW(rlMW(platformReadMW(http.HandlerFunc(adminHandler.ListLLMKeys)))))
	mux.Handle("POST /v1/admin/api-keys", authMW(rlMW(platformWriteMW(http.HandlerFunc(adminHandler.AddLLMKey)))))
	mux.Handle("PUT /v1/admin/api-keys/{id}", authMW(rlMW(platformWriteMW(http.HandlerFunc(adminHandler.UpdateLLMKeyStatus)))))
	// M27：改分配权重（smooth WRR 的比例）。与 status 分开是有意的 —— `weight = 0` 的语义是
	// "不参与选择但保留状态"，而 status 的语义是"这把 key 能不能用"。
	mux.Handle("PUT /v1/admin/api-keys/{id}/weight", authMW(rlMW(platformWriteMW(http.HandlerFunc(adminHandler.UpdateLLMKeyWeight)))))
	mux.Handle("DELETE /v1/admin/api-keys/{id}", authMW(rlMW(platformWriteMW(http.HandlerFunc(adminHandler.DeleteLLMKey)))))

	// LLM provider catalog routes（服务提供商目录与端点覆盖）
	mux.Handle("GET /v1/admin/llm-providers", authMW(rlMW(platformReadMW(http.HandlerFunc(adminHandler.ListLLMProviders)))))
	mux.Handle("PUT /v1/admin/llm-providers/{id}", authMW(rlMW(platformWriteMW(http.HandlerFunc(adminHandler.SetLLMProviderConfig)))))

	// 模型配置：决定 /v1/models（进而决定 /chat 的模型下拉）里有哪些模型。
	// adminMux 里**早已注册** GET/POST /models 与 PUT/DELETE /models/{id}（admin_ops.go:60-63），
	// 但从未在此暴露到外部 mux → 前端无论怎么点都调不到，只能依赖模型发现自动写入；
	// 一旦发现失败（provider 无 /models、UA 被拦、网络不可达），可用模型集永远是空的。
	mux.Handle("GET /v1/admin/models", authMW(rlMW(platformReadMW(adminStrip))))
	mux.Handle("POST /v1/admin/models", authMW(rlMW(platformWriteMW(adminStrip))))
	// 批量开关（/models 页的组内全选 → 启用/禁用）。必须**在 {id} 之外单独注册**：
	// 前者是写操作，鉴权口径与 PUT /models/{id} 一致。
	mux.Handle("PUT /v1/admin/models/batch", authMW(rlMW(platformWriteMW(adminStrip))))
	mux.Handle("PUT /v1/admin/models/{id}", authMW(rlMW(platformWriteMW(adminStrip))))
	mux.Handle("DELETE /v1/admin/models/{id}", authMW(rlMW(platformWriteMW(adminStrip))))

	// Settings admin routes
	// 读取分组配置此前只在 adminMux 内注册、从未暴露到外部 mux，前端 getSettings 恒 404
	// （页面回填静默失效）。此处补齐 GET，与 PUT 同鉴权口径。
	mux.Handle("GET /v1/admin/settings", authMW(rlMW(platformReadMW(adminStrip))))
	mux.Handle("PUT /v1/admin/settings", authMW(rlMW(platformWriteMW(adminStrip))))

	// 支付渠道配置（后台「支付配置」页面）
	mux.Handle("GET /v1/admin/payments", authMW(rlMW(platformReadMW(adminStrip))))
	mux.Handle("PUT /v1/admin/payments", authMW(rlMW(platformWriteMW(adminStrip))))
	// 租户级支付配置：每个租户一套渠道凭据（admin 角色即可，数据按 claims.tenant_id 隔离）。
	// 注意：必须直接挂 handler —— adminStrip 会把 /v1/admin 前缀剥掉，用在 /v1/tenant 上
	// 只会得到 404（StripPrefix 不匹配时返回 404）。
	mux.Handle("GET /v1/tenant/payments", authMW(rlMW(adminReadMW(http.HandlerFunc(adminHandler.GetTenantPaymentConfig)))))
	mux.Handle("PUT /v1/tenant/payments", authMW(rlMW(adminWriteMW(http.HandlerFunc(adminHandler.SaveTenantPaymentConfig)))))
	// 租户退款：订单按 user→tenant 归属；写入要求管理员。
	mux.Handle("GET /v1/tenant/refunds", authMW(rlMW(adminReadMW(http.HandlerFunc(billingHandler.ListTenantRefunds)))))
	mux.Handle("GET /v1/tenant/payments/{id}/refunds", authMW(rlMW(adminReadMW(http.HandlerFunc(billingHandler.ListPaymentRefunds)))))
	mux.Handle("POST /v1/tenant/payments/{id}/refunds", authMW(rlMW(adminWriteMW(http.HandlerFunc(billingHandler.CreateRefund)))))
	mux.Handle("GET /v1/tenant/overview", authMW(rlMW(adminReadMW(http.HandlerFunc(adminHandler.GetTenantOverview)))))
	// 租户身份能力通知：租户普通用户进入前端时一次性提示（不限管理员）。
	mux.Handle("GET /v1/tenant/identity-notice", authMW(rlMW(http.HandlerFunc(adminHandler.GetTenantIdentityNotice))))
	// 租户站点品牌：读取不限管理员（品牌要对每个登录用户生效），写入要求管理员。
	mux.Handle("GET /v1/tenant/site-config", authMW(rlMW(http.HandlerFunc(adminHandler.GetTenantSiteConfig))))
	mux.Handle("PUT /v1/tenant/site-config", authMW(rlMW(adminWriteMW(http.HandlerFunc(adminHandler.SaveTenantSiteConfig)))))
	mux.Handle("POST /v1/tenant/site-icon", authMW(rlMW(adminWriteMW(http.HandlerFunc(adminHandler.UploadTenantSiteIcon)))))

	// 发票审核（P1-2 / roadmap §11.4）：handler 早已注册在 adminMux（invoice_handler.go
	// 的 RegisterAdminRoutes），但外层 mux 从未暴露这两个 pattern ⇒ 前端 InvoiceReviewView
	// 一打开就 404「请求的资源不存在」。读用 adminReadMW；审核是写操作 ⇒ adminWriteMW。
	mux.Handle("GET /v1/admin/invoices", authMW(rlMW(adminReadMW(adminStrip))))
	// 发票导出（P1-8）：与列表同口径（status 过滤 + 时间范围），复用 BOM 与
	// 公式注入防护。平台导全量，租户只导本租户 —— tenant_id 取自 claims。
	mux.Handle("GET /v1/admin/invoices.csv", authMW(rlMW(adminReadMW(http.HandlerFunc(invoiceHandler.ExportAdmin)))))
	mux.Handle("POST /v1/admin/invoices/{id}/review", authMW(rlMW(adminWriteMW(adminStrip))))
	// 租户级发票审核：只看/只审本租户用户的申请（admin 角色即可）。
	mux.Handle("GET /v1/tenant/invoices", authMW(rlMW(adminReadMW(http.HandlerFunc(invoiceHandler.TenantList)))))
	mux.Handle("GET /v1/tenant/invoices.csv", authMW(rlMW(adminReadMW(http.HandlerFunc(invoiceHandler.ExportTenant)))))
	mux.Handle("POST /v1/tenant/invoices/{id}/review", authMW(rlMW(adminWriteMW(http.HandlerFunc(invoiceHandler.TenantReview)))))

	// 套餐目录 CRUD（P2-2c / roadmap §13.5 第 1 项）。
	// 套餐目录：租户级（每个租户一套定价与权益），平台后台不再提供管理页。
	// 数据按 claims.tenant_id 隔离，admin 角色即可维护。
	mux.Handle("GET /v1/tenant/packages", authMW(rlMW(adminReadMW(http.HandlerFunc(billingHandler.AdminListPackages)))))
	// 平台内置套餐模板：租户新建套餐时可预填（只读，不写入租户目录）。
	mux.Handle("GET /v1/tenant/package-templates", authMW(rlMW(adminReadMW(http.HandlerFunc(billingHandler.ListPackageTemplates)))))
	// 平台套餐模板管理（owner）：平台后台维护模板本身。
	mux.Handle("GET /v1/admin/package-templates", authMW(rlMW(platformReadMW(http.HandlerFunc(billingHandler.AdminListPackageTemplates)))))
	mux.Handle("POST /v1/admin/package-templates", authMW(rlMW(platformWriteMW(http.HandlerFunc(billingHandler.AdminCreatePackageTemplate)))))
	mux.Handle("PUT /v1/admin/package-templates/{id}", authMW(rlMW(platformWriteMW(http.HandlerFunc(billingHandler.AdminUpdatePackageTemplate)))))
	mux.Handle("DELETE /v1/admin/package-templates/{id}", authMW(rlMW(platformWriteMW(http.HandlerFunc(billingHandler.AdminDisablePackageTemplate)))))
	mux.Handle("POST /v1/admin/package-templates/{id}/restore", authMW(rlMW(platformWriteMW(http.HandlerFunc(billingHandler.AdminEnablePackageTemplate)))))
	mux.Handle("POST /v1/tenant/packages", authMW(rlMW(adminWriteMW(http.HandlerFunc(billingHandler.AdminCreatePackage)))))
	mux.Handle("PUT /v1/tenant/packages/{id}", authMW(rlMW(adminWriteMW(http.HandlerFunc(billingHandler.AdminUpdatePackage)))))
	// DELETE = 软下架（enabled=false），POST restore = 重新上架。
	mux.Handle("DELETE /v1/tenant/packages/{id}", authMW(rlMW(adminWriteMW(http.HandlerFunc(billingHandler.AdminDisablePackage)))))
	mux.Handle("POST /v1/tenant/packages/{id}/restore", authMW(rlMW(adminWriteMW(http.HandlerFunc(billingHandler.AdminEnablePackage)))))

	// 租户配额管理（查看 → 申请 → 平台审批）。
	// 租户侧：admin 角色即可查看自己的配额与提交申请（tenant_id 只取自 claims）。
	// 平台侧：审批会改配额上限，属平台运维 ⇒ 必须 RequirePlatformOwner，
	// 否则任一租户管理员都能给自己提额度。
	mux.Handle("GET /v1/tenant/quota", authMW(rlMW(adminReadMW(http.HandlerFunc(quotaHandler.List)))))
	mux.Handle("GET /v1/tenant/quota/requests", authMW(rlMW(adminReadMW(http.HandlerFunc(quotaHandler.ListRequests)))))
	mux.Handle("POST /v1/tenant/quota/requests", authMW(rlMW(adminWriteMW(http.HandlerFunc(quotaHandler.CreateRequest)))))
	mux.Handle("GET /v1/admin/quota/requests", authMW(rlMW(platformReadMW(http.HandlerFunc(quotaHandler.AdminListRequests)))))
	mux.Handle("POST /v1/admin/quota/requests/{id}/review", authMW(rlMW(platformWriteMW(http.HandlerFunc(quotaHandler.ReviewRequest)))))

	// 租户 / 域名 / 定时任务管理（后台「租户管理」「域名管理」「定时任务」三页）
	// 与上面 redis/database/models/settings 是**同一个坑的第四次**：handler 早已
	// 注册在 adminMux（admin_ops.go 的 registerOpsRoutes），但从未在此暴露到外部
	// mux ⇒ 前端 CronView / DomainManagementView / TenantManagementView 恒 404。
	// 鉴权口径沿用文件头 P1-3：GET 用 adminReadMW；创建/修改/删除/触发（含发信、
	// 签发 SSL、写 DB）一律 adminWriteMW。
	mux.Handle("GET /v1/admin/tenants", authMW(rlMW(platformReadMW(adminStrip))))
	mux.Handle("POST /v1/admin/tenants", authMW(rlMW(platformWriteMW(adminStrip))))
	mux.Handle("PUT /v1/admin/tenants/{id}", authMW(rlMW(platformWriteMW(adminStrip))))
	mux.Handle("DELETE /v1/admin/tenants/{id}", authMW(rlMW(platformWriteMW(adminStrip))))
	mux.Handle("POST /v1/admin/tenants/{id}/suspend", authMW(rlMW(platformWriteMW(adminStrip))))
	mux.Handle("GET /v1/admin/tenants/{id}/usage", authMW(rlMW(platformReadMW(adminStrip))))

	// 租户级 harness 配置（P1-④ 批 B）：读用 adminReadMW；**写用 PermPolicyManage**
	// —— 它决定该租户所有会话的 system prompt，属"策略"而非"日常运维"。
	harnessAdminHandler := NewHarnessProfileHandler(pgEntStore{}, notifyHarnessProfileChanged)
	policyManageMW := RequirePermission(auth.PermPolicyManage)
	mux.Handle("GET /v1/admin/settings/harness-profiles", authMW(rlMW(adminReadMW(http.HandlerFunc(harnessAdminHandler.GetTenant)))))
	mux.Handle("PUT /v1/admin/settings/harness-profiles", authMW(rlMW(policyManageMW(http.HandlerFunc(harnessAdminHandler.PutTenant)))))
	mux.Handle("DELETE /v1/admin/settings/harness-profiles/{prefix}", authMW(rlMW(policyManageMW(http.HandlerFunc(harnessAdminHandler.DeleteTenant)))))

	mux.Handle("GET /v1/admin/domains", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("POST /v1/admin/domains", authMW(rlMW(adminWriteMW(adminStrip))))
	mux.Handle("PUT /v1/admin/domains/{id}", authMW(rlMW(adminWriteMW(adminStrip))))
	mux.Handle("DELETE /v1/admin/domains/{id}", authMW(rlMW(adminWriteMW(adminStrip))))
	mux.Handle("POST /v1/admin/domains/{id}/verify", authMW(rlMW(adminWriteMW(adminStrip))))
	mux.Handle("POST /v1/admin/domains/{id}/renew-ssl", authMW(rlMW(adminWriteMW(adminStrip))))

	// 定时任务管理面已随 /admin/cron 页面下线；调度器（cron_scheduler.go）仍按
	// cron_jobs 表运行既有任务，Webhook 触发入口也保持不变。

	// 租户邀请码（F2）：注册时据邀请码决定新用户归属哪个租户。
	mux.Handle("GET /v1/admin/invites", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("POST /v1/admin/invites", authMW(rlMW(adminWriteMW(adminStrip))))
	mux.Handle("DELETE /v1/admin/invites/{code}", authMW(rlMW(adminWriteMW(adminStrip))))

	// 团队与成员（C1 / roadmap §12.4）。
	// **不接收请求体里的 tenant_id**：租户一律取当前登录者的（GetTenantID）。
	// 成员写操作收紧到 PermUsersManage（与 PUT/DELETE /users/{id} 同口径）——
	// 改别人的角色、把成员踢出去，都属于"用户管理"而不是普通 admin 写操作。
	adminMux.HandleFunc("GET /tenant/members", adminHandler.ListTenantMembers)
	adminMux.HandleFunc("PUT /tenant/members/{user_id}", adminHandler.SetTenantMemberRole)
	adminMux.HandleFunc("DELETE /tenant/members/{user_id}", adminHandler.RemoveTenantMember)
	adminMux.HandleFunc("GET /tenant/members/invites", adminHandler.ListTenantInvites)
	adminMux.HandleFunc("POST /tenant/members/invites", adminHandler.CreateTenantInvite)
	adminMux.HandleFunc("DELETE /tenant/members/invites/{code}", adminHandler.DeleteTenantInvite)
	mux.Handle("GET /v1/admin/tenant/members", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("PUT /v1/admin/tenant/members/{user_id}", authMW(rlMW(usersManageMW(adminStrip))))
	mux.Handle("DELETE /v1/admin/tenant/members/{user_id}", authMW(rlMW(usersManageMW(adminStrip))))
	mux.Handle("GET /v1/admin/tenant/members/invites", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("POST /v1/admin/tenant/members/invites", authMW(rlMW(adminWriteMW(adminStrip))))
	mux.Handle("DELETE /v1/admin/tenant/members/invites/{code}", authMW(rlMW(adminWriteMW(adminStrip))))
}
