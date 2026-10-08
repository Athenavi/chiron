package api

import (
	"net/http"
)

// routes_conversation.go —— 会话的路由注册。
// 由 gateway_router.go 按业务域拆分而来（纯移动：函数签名、函数体、调用点一字未改）。

// ── Conversations ──

func registerConversationRoutes(
	mux *http.ServeMux,
	conversationHandler *ConversationHandler,
	shareHandler *ShareHandler,
	authMW, rlMW routeMiddleware,
) {
	// Conversations (auth + rate limited)
	mux.Handle("GET /v1/conversations", authMW(rlMW(http.HandlerFunc(conversationHandler.List))))
	mux.Handle("POST /v1/conversations", authMW(rlMW(http.HandlerFunc(conversationHandler.Create))))
	mux.Handle("GET /v1/conversations/{id}", authMW(rlMW(http.HandlerFunc(conversationHandler.Get))))
	mux.Handle("PUT /v1/conversations/{id}", authMW(rlMW(http.HandlerFunc(conversationHandler.Update))))
	mux.Handle("DELETE /v1/conversations/{id}", authMW(rlMW(http.HandlerFunc(conversationHandler.Delete))))

	// Conversation shares (auth + rate limited; public GET in registerPublicEndpoints)
	mux.Handle("POST /v1/conversations/{id}/share", authMW(rlMW(http.HandlerFunc(shareHandler.Create))))
	mux.Handle("GET /v1/conversations/{id}/share", authMW(rlMW(http.HandlerFunc(shareHandler.GetActive))))
	mux.Handle("DELETE /v1/conversations/{id}/share", authMW(rlMW(http.HandlerFunc(shareHandler.Revoke))))

	// 会话地图布局：Redis 热层 + 异步落 PG（见 internal/api/sessionmap.go）
	smHandler := NewSessionMapHandler()
	mux.Handle("GET /v1/session-map/workspaces", authMW(rlMW(http.HandlerFunc(smHandler.ListWorkspaces))))
	mux.Handle("POST /v1/session-map/workspaces", authMW(rlMW(http.HandlerFunc(smHandler.CreateWorkspace))))
	mux.Handle("GET /v1/session-map/workspaces/{id}", authMW(rlMW(http.HandlerFunc(smHandler.GetWorkspace))))
	mux.Handle("PUT /v1/session-map/workspaces/{id}", authMW(rlMW(http.HandlerFunc(smHandler.SaveWorkspace))))
	mux.Handle("DELETE /v1/session-map/workspaces/{id}", authMW(rlMW(http.HandlerFunc(smHandler.DeleteWorkspace))))
	mux.Handle("POST /v1/session-map/workspaces/{id}/fork-node", authMW(rlMW(http.HandlerFunc(smHandler.ForkNode))))
	// 当前用户的分享列表（数据管理 → 分享管理）；与上面的按会话接口同组、同鉴权。
	mux.Handle("GET /v1/shares", authMW(rlMW(http.HandlerFunc(shareHandler.List))))
}
