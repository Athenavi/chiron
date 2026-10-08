package api

import (
	"net/http"
)

// routes_market.go —— 用户市场的路由注册。
// 由 gateway_router.go 按业务域拆分而来（纯移动：函数签名、函数体、调用点一字未改）。

// registerUserMarketRoutes 用户侧市场路由（技能/Agent/MCP 浏览与一键安装）。
func registerUserMarketRoutes(mux *http.ServeMux, h *UserMarketHandler, authMW, rlMW routeMiddleware) {
	mux.Handle("GET /v1/market", authMW(rlMW(http.HandlerFunc(h.List))))
	mux.Handle("POST /v1/market/{type}/{itemID}/install", authMW(rlMW(http.HandlerFunc(h.Install))))
}
