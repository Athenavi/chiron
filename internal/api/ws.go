package api

// 实时通道统一为 SSE。
// 原 GET /ws/{sessionId} 与 WebSocketHub 已下线（Vue 前端仅使用 EventSource；
// SSE 链路经 broadcast.Hub 的 Redis Stream + Pub/Sub 实现跨网关副本一致，并支持
// Last-Event-ID 断线重放）。本文件仅保留 RPA 插件 WebSocket（/ws/rpa）与
// 各 WebSocket 共用的 Origin 校验。

import (
	"log/slog"
	"net/http"
)

// checkWebSocketOrigin 是 RPA WebSocket（/ws/rpa）的共享 CheckOrigin。
// Origin 白名单读取进程内运行时共享源（cors_runtime.go，支持后台热更/跨副本广播）：
// 白名单未配置时拒绝带 Origin 的浏览器请求，放行无 Origin 的非浏览器客户端
// （curl / python websockets）。
func checkWebSocketOrigin(r *http.Request) bool {
	origin := r.Header.Get("Origin")
	if origin == "" {
		return true
	}
	if corsOriginAllowed(origin) {
		return true
	}
	slog.Warn("websocket origin rejected: not in allowlist",
		"origin", origin, "path", r.URL.Path)
	return false
}
