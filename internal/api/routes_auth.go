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
}
