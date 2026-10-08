package api

import (
	"net/http"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/storage"
)

// routes_public.go —— 公开端点的路由注册。
// 由 gateway_router.go 按业务域拆分而来（纯移动：函数签名、函数体、调用点一字未改）。

// ── Public endpoints ──

func registerPublicEndpoints(
	mux *http.ServeMux,
	authMW, rlMW, publicMW routeMiddleware,
	searchHandler *SearchHandler,
	shareHandler *ShareHandler,
	systemHandler *SystemHandler,
	mediaHandler *MediaHandler,
	cfg *config.Config,
	fileStore *storage.AtomicStore,
) {
	mux.Handle("GET /search", authMW(rlMW(http.HandlerFunc(searchHandler.Search))))
	mux.Handle("GET /v1/search", authMW(rlMW(http.HandlerFunc(searchHandler.Search))))

	// Public share view (no auth; revoked shares return 410 Gone)
	// 修复 P1：移除内层 publicMW 重复包裹（外层 publicMW(mux) 已包含日志/审计/追踪），
	// 避免审计 XAdd 双写、请求 ID 被内层重新生成。
	mux.Handle("GET /v1/share/{id}", rlMW(http.HandlerFunc(shareHandler.PublicGet)))

	// 存活探针刻意不挂限流中间件：限流在 Redis 不可用时是 fail-close 的，
	// 挂在 /health 上会让 kubelet 的 liveness 判定跟着 Redis 一起失败 →
	// 触发全副本重启风暴。重启救不了 Redis，却会丢掉全部会话热缓存、
	// 把一次依赖抖动放大成一次全站抖动。
	mux.Handle("GET /health", http.HandlerFunc(handleHealth))
	// Prometheus 指标端点：生产收敛为需要 PermAdminRead 权限，避免泄漏业务指标
	mux.Handle("GET /metrics", rlMW(metricsAuthMW(cfg, authMW, systemHandler.PrometheusMetrics)))
	// API 文档（OpenAPI spec，公开，供 Swagger/Redoc 展示）
	mux.Handle("GET /docs/", http.StripPrefix("/docs/", http.FileServer(http.Dir("docs"))))
	// 就绪探针同样不挂限流：它自己会检查 Redis/PG 并如实返回 503，
	// 而"限流组件不可用"并不等于"本实例未就绪"，不该借限流中间件的 503 表达。
	mux.Handle("GET /ready", http.HandlerFunc(handleReadiness))
	// 引擎配置下发（X-Internal-Token 保护，Python 引擎启动拉取）
	mux.Handle("GET /v1/internal/engine-config", rlMW(internalTokenMW(cfg, EngineConfig(cfg))))

	// 模型路由配置同步（X-Internal-Token 保护，Python 引擎启动时拉取）
	mux.Handle("GET /v1/internal/model-routes", rlMW(internalTokenMW(cfg, http.HandlerFunc(NewEntModelRouterHandler().SyncRoutes))))

	// Tool Broker：危险工具（delete / external）的服务端授权判定与审计。
	// 引擎在执行这类工具前必须来这里拿"允不允许 + 该走哪些关"，服务端判定是权威的
	// （见 internal/api/tool_broker.go 与 tool_policy.go）。
	mux.Handle("POST /v1/internal/tool-authorize", rlMW(internalTokenMW(cfg, http.HandlerFunc(ToolAuthorizeHandler))))

	// 执行审计的集中摄取（N4）：多副本 / 独立沙箱服务各有自己的落盘目录，
	// 送到这里统一落进同一条审计流，排障时不必逐台去捞。
	mux.Handle("POST /v1/internal/audit/exec", rlMW(internalTokenMW(cfg, http.HandlerFunc(ExecAuditIngestHandler))))

	// Python 引擎数据库/Redis 统一访问端点（X-Internal-Token 保护）
	mux.Handle("POST /v1/internal/db/query", rlMW(internalTokenMW(cfg, http.HandlerFunc(systemHandler.DBQuery))))
	mux.Handle("POST /v1/internal/db/execute", rlMW(internalTokenMW(cfg, http.HandlerFunc(systemHandler.DBExecute))))
	mux.Handle("POST /v1/internal/db/batch-execute", rlMW(internalTokenMW(cfg, http.HandlerFunc(systemHandler.DBBatchExecute))))
	mux.Handle("GET /v1/internal/db/health", rlMW(internalTokenMW(cfg, http.HandlerFunc(systemHandler.DatabaseHealth))))

	mux.Handle("GET /v1/internal/python/health", rlMW(internalTokenMW(cfg, http.HandlerFunc(systemHandler.PythonEngineHealth))))

	mux.Handle("POST /v1/internal/redis/get", rlMW(internalTokenMW(cfg, http.HandlerFunc(systemHandler.RedisGet))))
	mux.Handle("POST /v1/internal/redis/set", rlMW(internalTokenMW(cfg, http.HandlerFunc(systemHandler.RedisSet))))
	mux.Handle("POST /v1/internal/redis/del", rlMW(internalTokenMW(cfg, http.HandlerFunc(systemHandler.RedisDel))))
	mux.Handle("GET /v1/internal/redis/health", rlMW(internalTokenMW(cfg, http.HandlerFunc(systemHandler.RedisHealth))))

	// 媒体资产写入（X-Internal-Token 保护）：Python 引擎的 agent 工具
	//（media_create / image_generate）据此把产物写入 media_assets + 对象存储，
	// 与「媒体库」页面共享同一份数据。
	mux.Handle("POST /v1/internal/media/assets", rlMW(internalTokenMW(cfg, http.HandlerFunc(mediaHandler.InternalCreateAsset))))

	// Agent 文件存储（X-Internal-Token 保护）：引擎的文件工具据此把工作区文件落到
	// 部署方配置的 FileStore（local 共享卷 / S3），解决多副本下"写 A 副本、读 B 副本"
	// 的间歇性失忆（python-engine/app/tools/sandbox.py 的注释已自陈该风险）。
	// 隔离由服务端强制：请求只带相对路径，存储键由 handler 拼成
	// `agent-files/{tenant}/{user}/{相对路径}`（见 internal_storage_handler.go）。
	internalStorage := NewInternalStorageHandler(fileStore)
	mux.Handle("GET /v1/internal/storage/read", rlMW(internalTokenMW(cfg, internalStorage.StorageRead)))
	mux.Handle("POST /v1/internal/storage/write", rlMW(internalTokenMW(cfg, internalStorage.StorageWrite)))
	mux.Handle("GET /v1/internal/storage/list", rlMW(internalTokenMW(cfg, internalStorage.StorageList)))
	mux.Handle("POST /v1/internal/storage/delete", rlMW(internalTokenMW(cfg, internalStorage.StorageDelete)))
}
