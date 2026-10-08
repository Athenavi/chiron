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
) {
	// Admin routes (auth + admin permission + rate limit)
	// P1-3: 所有 admin 路由必须挂限流，防止被劫持的 admin token 无限调用
	// 造成破坏（backup/restore/users DELETE 等敏感操作）。
	// 读操作用 PermAdminRead，写操作（PUT/DELETE/POST）必须 PermAdminWrite。
	// P1-4: 用户管理路由（PUT/DELETE /v1/admin/users）必须 PermUsersManage，
	// 该权限仅 owner 角色持有，普通 admin 不应能删/改用户。
	adminReadMW := RequirePermission(auth.PermAdminRead)
	adminWriteMW := RequirePermission(auth.PermAdminWrite)
	usersManageMW := RequirePermission(auth.PermUsersManage)
	adminMux := http.NewServeMux()
	adminHandler.RegisterRoutes(adminMux)
	// Strip the /v1/admin prefix so adminMux patterns (e.g. "GET /metrics") match correctly
	adminStrip := http.StripPrefix("/v1/admin", adminMux)
	mux.Handle("GET /v1/admin/metrics", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("GET /v1/admin/users", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("GET /v1/admin/users/{id}", authMW(rlMW(adminReadMW(adminStrip))))
	// 用户写操作收紧为 PermUsersManage（仅 owner）
	mux.Handle("PUT /v1/admin/users/{id}", authMW(rlMW(usersManageMW(adminStrip))))
	mux.Handle("DELETE /v1/admin/users/{id}", authMW(rlMW(usersManageMW(adminStrip))))
	mux.Handle("GET /v1/admin/system", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("POST /v1/admin/maintenance", authMW(rlMW(adminWriteMW(adminStrip))))
	mux.Handle("POST /v1/admin/backup", authMW(rlMW(adminWriteMW(adminStrip))))
	mux.Handle("POST /v1/admin/restore", authMW(rlMW(adminWriteMW(adminStrip))))
	mux.Handle("GET /v1/admin/kb", authMW(rlMW(adminReadMW(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if pythonClient == nil || !pythonClient.IsConnected() {
			InternalError(w, "python engine not available")
			return
		}
		claims := auth.GetClaims(r.Context())
		r.Header.Set("X-User-Role", claims.Role)
		pythonClient.ForwardRequest(w, r, "/v1/admin/kb?user_id="+claims.UserID+"&tenant_id="+claims.TenantID)
	})))))

	// Storage admin routes
	mux.Handle("GET /v1/admin/storage", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("PUT /v1/admin/storage", authMW(rlMW(adminWriteMW(adminStrip))))
	mux.Handle("POST /v1/admin/storage/test", authMW(rlMW(adminWriteMW(adminStrip))))

	// Redis admin routes
	mux.Handle("GET /v1/admin/redis", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("PUT /v1/admin/redis", authMW(rlMW(adminWriteMW(adminStrip))))
	mux.Handle("POST /v1/admin/redis/test", authMW(rlMW(adminWriteMW(adminStrip))))

	// Queue admin routes
	mux.Handle("GET /v1/admin/queue", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("POST /v1/admin/queue/flush", authMW(rlMW(adminWriteMW(adminStrip))))
	mux.Handle("POST /v1/admin/queue/pause", authMW(rlMW(adminWriteMW(adminStrip))))

	// Cache admin routes
	mux.Handle("GET /v1/admin/cache/stats", authMW(rlMW(adminReadMW(adminStrip))))

	// Performance admin routes
	mux.Handle("GET /v1/admin/performance", authMW(rlMW(adminReadMW(adminStrip))))

	// API Key admin routes (direct handlers, avoid adminMux path mismatch)
	mux.Handle("GET /v1/admin/api-keys", authMW(rlMW(adminReadMW(http.HandlerFunc(adminHandler.ListLLMKeys)))))
	mux.Handle("POST /v1/admin/api-keys", authMW(rlMW(adminWriteMW(http.HandlerFunc(adminHandler.AddLLMKey)))))
	mux.Handle("PUT /v1/admin/api-keys/{id}", authMW(rlMW(adminWriteMW(http.HandlerFunc(adminHandler.UpdateLLMKeyStatus)))))
	mux.Handle("DELETE /v1/admin/api-keys/{id}", authMW(rlMW(adminWriteMW(http.HandlerFunc(adminHandler.DeleteLLMKey)))))

	// LLM provider catalog routes（服务提供商目录与端点覆盖）
	mux.Handle("GET /v1/admin/llm-providers", authMW(rlMW(adminReadMW(http.HandlerFunc(adminHandler.ListLLMProviders)))))
	mux.Handle("PUT /v1/admin/llm-providers/{id}", authMW(rlMW(adminWriteMW(http.HandlerFunc(adminHandler.SetLLMProviderConfig)))))

	// 模型配置：决定 /v1/models（进而决定 /chat 的模型下拉）里有哪些模型。
	// adminMux 里**早已注册** GET/POST /models 与 PUT/DELETE /models/{id}（admin_ops.go:60-63），
	// 但从未在此暴露到外部 mux → 前端无论怎么点都调不到，只能依赖模型发现自动写入；
	// 一旦发现失败（provider 无 /models、UA 被拦、网络不可达），可用模型集永远是空的。
	mux.Handle("GET /v1/admin/models", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("POST /v1/admin/models", authMW(rlMW(adminWriteMW(adminStrip))))
	mux.Handle("PUT /v1/admin/models/{id}", authMW(rlMW(adminWriteMW(adminStrip))))
	mux.Handle("DELETE /v1/admin/models/{id}", authMW(rlMW(adminWriteMW(adminStrip))))

	// Settings admin routes
	// 读取分组配置此前只在 adminMux 内注册、从未暴露到外部 mux，前端 getSettings 恒 404
	// （页面回填静默失效）。此处补齐 GET，与 PUT 同鉴权口径。
	mux.Handle("GET /v1/admin/settings", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("PUT /v1/admin/settings", authMW(rlMW(adminWriteMW(adminStrip))))

	// 支付渠道配置（后台「支付配置」页面）
	mux.Handle("GET /v1/admin/payments", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("PUT /v1/admin/payments", authMW(rlMW(adminWriteMW(adminStrip))))
}
