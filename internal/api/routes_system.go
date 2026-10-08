package api

import (
	"net/http"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// routes_system.go —— 系统（编辑器/工具/健康/追踪）的路由注册。
// 由 gateway_router.go 按业务域拆分而来（纯移动：函数签名、函数体、调用点一字未改）。

// ── System (editor / tools / health / trace) ──

func registerSystemRoutes(
	mux *http.ServeMux,
	authMW, rlMW, sanitizeMW routeMiddleware,
	editorHandler *EditorHandler,
	toolHandler *ToolHandler,
	systemHandler *SystemHandler,
	traceHandler *TraceHandler,
) {
	// Editor (admin 权限 + rate limited)
	// S 安全修复：编辑器直接读写共享服务器工作区（含沙箱/分片/插件数据），
	// 仅限管理员（列表/读=PermAdminRead，写=PermAdminWrite），普通 user 无权访问。
	mux.Handle("GET /api/editor/files", authMW(rlMW(RequirePermission(auth.PermAdminRead)(http.HandlerFunc(editorHandler.ListFiles)))))
	mux.Handle("GET /api/editor/read", authMW(rlMW(RequirePermission(auth.PermAdminRead)(http.HandlerFunc(editorHandler.ReadFile)))))
	mux.Handle("POST /api/editor/write", authMW(rlMW(RequirePermission(auth.PermAdminWrite)(http.HandlerFunc(editorHandler.WriteFile)))))

	// Tools (rate limited, proxies to Python)
	// 工具面属于内部信息（含 MCP 注入工具名/描述/完整入参 schema），
	// 未认证即可枚举等于免费交付攻击面清单。同组 /v1/tools/execute 早有 authMW。
	mux.Handle("GET /v1/tools", authMW(rlMW(http.HandlerFunc(toolHandler.ListTools))))
	mux.Handle("POST /v1/tools/execute", authMW(rlMW(sanitizeMW(http.HandlerFunc(toolHandler.ExecuteTool)))))

	// System (rate limited; spans/traces 仅管理员可见，S 安全修复：原为公开信息泄露)
	// 健康评分属内部运行信息；同组的 spans/traces 都要求 PermAdminRead，此处漏了鉴权。
	mux.Handle("GET /v1/system/health", authMW(rlMW(http.HandlerFunc(systemHandler.HealthScores))))
	mux.Handle("GET /v1/system/spans", authMW(rlMW(RequirePermission(auth.PermAdminRead)(http.HandlerFunc(systemHandler.Spans)))))
	mux.Handle("GET /v1/system/traces", authMW(rlMW(RequirePermission(auth.PermAdminRead)(http.HandlerFunc(systemHandler.Traces)))))
	mux.Handle("GET /v1/metrics", authMW(rlMW(http.HandlerFunc(systemHandler.Metrics))))
	mux.Handle("POST /v1/admin/log-level", authMW(rlMW(RequirePermission(auth.PermAdminWrite)(http.HandlerFunc(systemHandler.SetLogLevel)))))

	// Trace (user-level call chain tracing, tenant-isolated)
	if traceHandler != nil {
		mux.Handle("GET /v1/traces", authMW(rlMW(http.HandlerFunc(traceHandler.ListTraces))))
		mux.Handle("GET /v1/traces/{trace_id}", authMW(rlMW(http.HandlerFunc(traceHandler.GetTrace))))
	}

	// 子 Agent 运行观测（层级树 / 详情 / 过程）：Redis 优先、DB 回落，租户隔离。
	// 见 docs/subagent-design.md §4.4；前端据此画递归树与侧边栏实时输出。
	subagentHandler := NewSubagentHandler(db.Redis)
	mux.Handle("GET /v1/subagent/runs", authMW(rlMW(http.HandlerFunc(subagentHandler.ListRuns))))
	mux.Handle("GET /v1/subagent/runs/{run_id}", authMW(rlMW(http.HandlerFunc(subagentHandler.GetRun))))
	mux.Handle("GET /v1/subagent/runs/{run_id}/events", authMW(rlMW(http.HandlerFunc(subagentHandler.GetRunEvents))))
	// 中止：网关只做租户校验 + Redis 广播，真正取消由持有该 run 的引擎实例执行
	// （引擎侧订阅见 python-engine/app/subagent/registry.py）
	mux.Handle("POST /v1/subagent/runs/{run_id}/cancel", authMW(rlMW(http.HandlerFunc(subagentHandler.CancelRun))))
	mux.Handle("POST /v1/subagent/sessions/{session_id}/cancel", authMW(rlMW(http.HandlerFunc(subagentHandler.CancelSessionRuns))))
}
