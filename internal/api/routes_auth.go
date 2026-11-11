package api

import (
	"net/http"
)

// routes_auth.go —— 认证（登录/注册/登出）的路由注册。
// 由 gateway_router.go 按业务域拆分而来（纯移动：函数签名、函数体、调用点一字未改）。

// ── Auth (login / register / logout) ──

func registerAuthRoutes(mux *http.ServeMux, authHandler *AuthHandler, authMW, rlMW routeMiddleware) {
	// Auth (public, rate limited)
	mux.Handle("POST /v1/auth/login", rlMW(http.HandlerFunc(authHandler.Login)))
	// MFA 兑换：公开 + 限流。凭据是登录接口返回的**挑战令牌**（短期、独立签名），
	// 不是会话令牌 —— 因此不能挂 authMW。
	mux.Handle("POST /v1/auth/mfa/verify", rlMW(http.HandlerFunc(authHandler.MFAVerify)))
	mux.Handle("POST /v1/auth/register", rlMW(http.HandlerFunc(authHandler.Register)))
	mux.Handle("POST /v1/auth/refresh", rlMW(http.HandlerFunc(authHandler.Refresh)))
	// 登出必须经过 authMW：Logout 要用 claims 里的 jti 写吊销黑名单，
	// 而 claims 只有 authMW 会注入。此前漏挂导致 GetClaims 恒为 nil，
	// Logout 的函数体从不执行 —— "退出登录"实际是空操作，
	// token 一路有效到自然过期（默认 24h），被盗凭证无法止损。
	mux.Handle("POST /v1/auth/logout", authMW(rlMW(http.HandlerFunc(authHandler.Logout))))
	// SSO cookie → Bearer token 会话引导（公开：httpOnly cookie 自带凭据）
	mux.Handle("GET /v1/auth/session", rlMW(http.HandlerFunc(authHandler.Session)))
	mux.Handle("GET /v1/auth/profile", authMW(rlMW(http.HandlerFunc(authHandler.Profile))))
	mux.Handle("PUT /v1/auth/profile", authMW(rlMW(http.HandlerFunc(authHandler.UpdateProfile))))
	// ── MFA 自助管理（需登录）──
	// 走 authMW：这些端点操作的是"当前登录用户自己的"双因子设置，
	// 身份必须来自会话而非请求体，否则就成了"任意用户改别人的 MFA"。
	mux.Handle("GET /v1/auth/mfa/status", authMW(rlMW(http.HandlerFunc(authHandler.MFAStatus))))
	mux.Handle("POST /v1/auth/mfa/setup", authMW(rlMW(http.HandlerFunc(authHandler.MFASetup))))
	mux.Handle("POST /v1/auth/mfa/enable", authMW(rlMW(http.HandlerFunc(authHandler.MFAEnable))))
	mux.Handle("POST /v1/auth/mfa/disable", authMW(rlMW(http.HandlerFunc(authHandler.MFADisable))))
	// 个人信息导出（数据可携带权）：身份只来自会话，限流防批量拉取。
	mux.Handle("GET /v1/me/export", authMW(rlMW(http.HandlerFunc(authHandler.ExportMyData))))
	// ── 账号注销（冻结 → 宽限期可撤销 → 到期归档）──
	// 这四个端点必须**放行**给 pending_deletion 状态的账号：
	// 它们是"只允许下载或导出、以及撤销"这条规则的落地处。
	// 其余所有端点由 AccountDeletionGate 统一 403（见 account_deletion_gate.go）。
	mux.Handle("GET /v1/me/deletion", authMW(rlMW(http.HandlerFunc(authHandler.AccountDeletionStatus))))
	mux.Handle("POST /v1/me/deletion", authMW(rlMW(http.HandlerFunc(authHandler.RequestAccountDeletion))))
	mux.Handle("POST /v1/me/deletion/cancel", authMW(rlMW(http.HandlerFunc(authHandler.CancelAccountDeletion))))
}
