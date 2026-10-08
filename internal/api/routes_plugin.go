package api

import (
	"net/http"
)

// routes_plugin.go —— 插件的路由注册。
// 由 gateway_router.go 按业务域拆分而来（纯移动：函数签名、函数体、调用点一字未改）。

// ── Plugins ──

func registerPluginRoutes(mux *http.ServeMux, pluginHandler *PluginHandler, authMW, rlMW routeMiddleware) {
	// Plugins (auth + rate limited)
	mux.Handle("GET /v1/plugins", authMW(rlMW(http.HandlerFunc(pluginHandler.List))))
	mux.Handle("POST /v1/plugins/{name}/install", authMW(rlMW(http.HandlerFunc(pluginHandler.Install))))
	mux.Handle("PUT /v1/plugins/{name}", authMW(rlMW(http.HandlerFunc(pluginHandler.Update))))
	mux.Handle("POST /v1/plugins/{name}/test", authMW(rlMW(http.HandlerFunc(pluginHandler.Test))))
	mux.Handle("DELETE /v1/plugins/{name}", authMW(rlMW(http.HandlerFunc(pluginHandler.Uninstall))))
}
