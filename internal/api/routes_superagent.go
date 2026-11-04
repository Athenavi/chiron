package api

import "net/http"

// routes_superagent.go —— Super-agent 接管的端点注册。
//
// 判据是 `user_id`，不是 `tenant_id`：接管是个人授权行为，不需要任何 admin 权限，
// 同租户的其他用户（含租户 admin）也默认停不了别人的接管。
//
// 定时任务与 `/v1/admin/cron-jobs` 分属两层：那条是平台维护（按租户、直打引擎），
// 这里每个用户只管自己的任务，动作也只有一件事 —— 到点给自己的会话开接管窗口。
func registerSuperAgentRoutes(
	mux *http.ServeMux,
	authMW, rlMW routeMiddleware,
	handler *SuperAgentHandler,
) {
	// 只读快照：待机轮询也要能拿到"现在是否有接管在跑"，因此与写操作分开。
	mux.Handle("GET /v1/superagent/takeovers", authMW(rlMW(http.HandlerFunc(handler.Snapshot))))
	mux.Handle("PUT /v1/superagent/config", authMW(rlMW(http.HandlerFunc(handler.SaveConfig))))
	mux.Handle("POST /v1/superagent/takeovers/{id}/stop",
		authMW(rlMW(http.HandlerFunc(handler.Stop))))

	mux.Handle("GET /v1/superagent/schedules", authMW(rlMW(http.HandlerFunc(handler.ListSchedules))))
	mux.Handle("POST /v1/superagent/schedules", authMW(rlMW(http.HandlerFunc(handler.CreateSchedule))))
	mux.Handle("PUT /v1/superagent/schedules/{id}",
		authMW(rlMW(http.HandlerFunc(handler.UpdateSchedule))))
	mux.Handle("DELETE /v1/superagent/schedules/{id}",
		authMW(rlMW(http.HandlerFunc(handler.DeleteSchedule))))
	// 试跑：与到点触发同一个执行体，只是不等 cron 到点，方便用户确认配置有效。
	mux.Handle("POST /v1/superagent/schedules/{id}/trigger",
		authMW(rlMW(http.HandlerFunc(handler.TriggerSchedule))))

	// P1-1 订阅 CRUD：决定"这条事件投给哪个会话"。判据同样是 `user_id` ——
	// 改别人的投递去向等于替别人决定他的会话里会出现什么。
	mux.Handle("GET /v1/threads/{id}/subscriptions",
		authMW(rlMW(http.HandlerFunc(ListThreadSubscriptions))))
	mux.Handle("POST /v1/threads/{id}/subscriptions",
		authMW(rlMW(http.HandlerFunc(CreateThreadSubscription))))
	mux.Handle("DELETE /v1/threads/{id}/subscriptions/{sub_id}",
		authMW(rlMW(http.HandlerFunc(DeleteThreadSubscription))))
}
