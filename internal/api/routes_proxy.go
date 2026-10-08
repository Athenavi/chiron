package api

import (
	"log/slog"
	"net/http"
	"net/url"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/engine"
)

// routes_proxy.go —— Python 引擎代理（图/工作流/知识库）的路由注册。
// 由 gateway_router.go 按业务域拆分而来（纯移动：函数签名、函数体、调用点一字未改）。

// ── Python engine proxy routes (graphs / workflows / knowledge base) ──

func registerProxyRoutes(
	mux *http.ServeMux,
	authMW, rlMW, kbRateMW routeMiddleware,
	pythonClient *engine.PythonClient,
) {
	// pythonProxy is a factory for reverse-proxy handlers to the Python engine.
	// All routes using this factory are wrapped with authMW, so claims are
	// guaranteed present in context.
	type proxyOpt struct {
		methods []string // allowed HTTP methods; empty = GET+POST+PUT+DELETE
		logTag  string
		// mutateBody 在把请求体转发给引擎前改写它（P1-c：统一链路注入会话运行时解析结果，
		// 见 docs/session-runtime-spec.md §5）。返回 false 表示已自行写出响应、代理应中止。
		mutateBody func(r *http.Request, body map[string]interface{}) bool
	}
	newProxy := func(prefix string, opt proxyOpt) func(func(*http.Request) string) http.HandlerFunc {
		return func(buildPath func(*http.Request) string) http.HandlerFunc {
			return func(w http.ResponseWriter, r *http.Request) {
				if pythonClient == nil {
					InternalError(w, "python engine not available")
					return
				}
				claims := auth.GetClaims(r.Context())
				if claims == nil || claims.TenantID == "" {
					Unauthorized(w, "missing tenant context")
					return
				}
				// 多租户隔离：透传 tenant_id 给 Python 引擎（query 参数兼容，header 见 pythonClient.WithTenant）
				proxiedPath := buildPath(r) + "?user_id=" + claims.UserID + "&tenant_id=" + claims.TenantID
				var resp interface{}
				var err error
				switch r.Method {
				case "GET":
					err = pythonClient.GetJSON(r.Context(), proxiedPath, &resp)
				case "POST":
					var body map[string]interface{}
					if err2 := DecodeJSON(w, r, &body); err2 != nil {
						BadRequest(w, ErrInvalidReq)
						return
					}
					if opt.mutateBody != nil && !opt.mutateBody(r, body) {
						return
					}
					err = pythonClient.PostJSON(r.Context(), proxiedPath, body, &resp)
				case "PUT":
					var body map[string]interface{}
					if err2 := DecodeJSON(w, r, &body); err2 != nil {
						BadRequest(w, ErrInvalidReq)
						return
					}
					if opt.mutateBody != nil && !opt.mutateBody(r, body) {
						return
					}
					err = pythonClient.PutJSON(r.Context(), proxiedPath, body, &resp)
				case "DELETE":
					err = pythonClient.DeleteJSON(r.Context(), proxiedPath, &resp)
				}
				if err != nil {
					slog.Error(opt.logTag+" proxy error", "path", proxiedPath, "error", err)
					logAndRespond(w, err, http.StatusInternalServerError, "internal error")
					return
				}
				OK(w, resp)
			}
		}
	}

	// Helper: build path functions for parameterised routes
	pathFn := func(static string) func(*http.Request) string {
		return func(*http.Request) string { return static }
	}
	pathParam := func(prefix string) func(*http.Request) string {
		return func(r *http.Request) string { return prefix + "/" + r.PathValue("id") }
	}
	pathParamSuffix := func(prefix, suffix string) func(*http.Request) string {
		return func(r *http.Request) string {
			return prefix + "/" + r.PathValue("id") + suffix
		}
	}
	// pathParamNamed 支持自定义路径参数名（如 {conflict_id}），用于修复参数丢失的代理路由。
	pathParamNamed := func(prefix, param, suffix string) func(*http.Request) string {
		return func(r *http.Request) string {
			return prefix + "/" + r.PathValue(param) + suffix
		}
	}

	// Graphs (auth + rate limited, proxies to Python)
	graphP := newProxy("", proxyOpt{logTag: "graph"})
	mux.Handle("GET /v1/graphs", authMW(rlMW(graphP(pathFn("/v1/graphs")))))
	mux.Handle("POST /v1/graphs", authMW(rlMW(graphP(pathFn("/v1/graphs")))))
	mux.Handle("GET /v1/graphs/{id}", authMW(rlMW(graphP(pathParam("/v1/graphs")))))
	mux.Handle("DELETE /v1/graphs/{id}", authMW(rlMW(graphP(pathParam("/v1/graphs")))))
	mux.Handle("POST /v1/graphs/{id}/execute", authMW(rlMW(graphP(pathParamSuffix("/v1/graphs", "/execute")))))

	// Workflow 执行状态与历史（代理 Python；status 支持内存实例 + DB 回退）
	mux.Handle("GET /v1/workflows/instances", authMW(rlMW(graphP(pathFn("/v1/workflows/instances")))))
	mux.Handle("GET /v1/workflows/{id}/status", authMW(rlMW(graphP(pathParamSuffix("/v1/workflows", "/status")))))

	// Knowledge Base (auth + rate limited + tenant QPS limit, proxies to Python)
	kbP := newProxy("/v1/kb", proxyOpt{logTag: "kb"})
	mux.Handle("GET /v1/kb", authMW(kbRateMW(kbP(pathFn("/v1/kb")))))
	mux.Handle("POST /v1/kb", authMW(kbRateMW(kbP(pathFn("/v1/kb")))))
	mux.Handle("GET /v1/kb/{id}", authMW(kbRateMW(kbP(pathParam("/v1/kb")))))
	mux.Handle("PUT /v1/kb/{id}", authMW(kbRateMW(kbP(pathParam("/v1/kb")))))
	mux.Handle("DELETE /v1/kb/{id}", authMW(kbRateMW(kbP(pathParam("/v1/kb")))))
	mux.Handle("POST /v1/kb/{id}/documents", authMW(kbRateMW(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if pythonClient == nil {
			InternalError(w, "python engine not available")
			return
		}
		claims := auth.GetClaims(r.Context())
		pythonClient.ForwardRequest(w, r, "/v1/kb/"+r.PathValue("id")+"/documents?user_id="+claims.UserID+"&tenant_id="+claims.TenantID)
	}))))
	mux.Handle("GET /v1/kb/{id}/documents", authMW(kbRateMW(kbP(pathParamSuffix("/v1/kb", "/documents")))))
	mux.Handle("POST /v1/kb/{id}/build", authMW(kbRateMW(kbP(pathParamSuffix("/v1/kb", "/build")))))
	mux.Handle("POST /v1/kb/{id}/query", authMW(kbRateMW(kbP(pathParamSuffix("/v1/kb", "/query")))))
	// 知识库删除文档（P1 修复：Python 端已有 DELETE /{kb_id}/documents?doc_id=，
	// 网关此前缺失该路由导致前端删除文档 404）
	mux.Handle("PUT /v1/kb/{id}/visibility", authMW(kbRateMW(http.HandlerFunc(handleKBVisibility))))
	mux.Handle("DELETE /v1/kb/{id}/documents", authMW(kbRateMW(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if pythonClient == nil {
			InternalError(w, "python engine not available")
			return
		}
		claims := auth.GetClaims(r.Context())
		if claims == nil || claims.TenantID == "" {
			Unauthorized(w, "missing tenant context")
			return
		}
		// 保留原始 doc_id 查询参数（newProxy 的 buildPath 会丢弃原始 query）
		target := "/v1/kb/" + r.PathValue("id") + "/documents?doc_id=" + url.QueryEscape(r.URL.Query().Get("doc_id")) +
			"&user_id=" + claims.UserID + "&tenant_id=" + claims.TenantID
		var resp interface{}
		if err := pythonClient.DeleteJSON(r.Context(), target, &resp); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "python engine error")
			return
		}
		OK(w, resp)
	}))))

	// Unified chat / quick-execute (六大工作台统一入口, proxies to Python TaskRouter)
	chatP := newProxy("", proxyOpt{logTag: "chat", mutateBody: InjectSessionRuntime})
	mux.Handle("POST /v1/chat/submit", authMW(rlMW(chatP(pathFn("/v1/chat/submit")))))
	mux.Handle("GET /v1/chat/sessions/{id}/messages", authMW(rlMW(chatP(pathParamSuffix("/v1/chat/sessions", "/messages")))))
	// quick-execute 为 chat/submit 的语义别名（前端快捷执行入口）
	mux.Handle("POST /v1/quick-execute", authMW(rlMW(chatP(pathFn("/v1/chat/submit")))))

	// Capabilities discovery (能力注册中心: 六大工作台能力发现)
	capP := newProxy("", proxyOpt{logTag: "capabilities"})
	mux.Handle("GET /v1/capabilities", authMW(rlMW(capP(pathFn("/v1/capabilities")))))
	mux.Handle("POST /v1/capabilities/search", authMW(rlMW(capP(pathFn("/v1/capabilities/search")))))

	// Memory (用户长期记忆 L2 档案卡: 列表/CRUD/语义检索/智能整理, 代理 Python)
	memP := newProxy("", proxyOpt{logTag: "memory"})
	mux.Handle("GET /v1/memory/profile", authMW(rlMW(memP(pathFn("/v1/memory/profile")))))
	mux.Handle("POST /v1/memory/profile", authMW(rlMW(memP(pathFn("/v1/memory/profile")))))
	mux.Handle("PUT /v1/memory/profile", authMW(rlMW(memP(pathFn("/v1/memory/profile")))))
	mux.Handle("DELETE /v1/memory/profile/{id}", authMW(rlMW(memP(pathParam("/v1/memory/profile")))))
	mux.Handle("POST /v1/memory/profile/clear", authMW(rlMW(memP(pathFn("/v1/memory/profile/clear")))))
	mux.Handle("POST /v1/memory/search", authMW(rlMW(memP(pathFn("/v1/memory/search")))))
	mux.Handle("POST /v1/memory/organize", authMW(rlMW(memP(pathFn("/v1/memory/organize")))))
	mux.Handle("GET /v1/memory/organize/status", authMW(rlMW(memP(pathFn("/v1/memory/organize/status")))))
	mux.Handle("GET /v1/memory/summaries", authMW(rlMW(memP(pathFn("/v1/memory/summaries")))))
	mux.Handle("GET /v1/memory/conflicts", authMW(rlMW(memP(pathFn("/v1/memory/conflicts")))))
	mux.Handle("POST /v1/memory/conflicts/{conflict_id}/resolve", authMW(rlMW(memP(pathParamNamed("/v1/memory/conflicts", "conflict_id", "/resolve")))))
	mux.Handle("DELETE /v1/memory/conflicts/{conflict_id}", authMW(rlMW(memP(pathParamNamed("/v1/memory/conflicts", "conflict_id", "")))))
}
