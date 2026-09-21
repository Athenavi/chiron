package api

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/billing"
	"github.com/athenavi/chiron/internal/broadcast"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/monitor"
	"github.com/athenavi/chiron/internal/session"
	"github.com/athenavi/chiron/internal/storage"
)

// metricsAuthMW 允许两种鉴权方式抓取 /metrics：
// 1. METRICS_TOKEN 配置的 Bearer token（Prometheus 抓取，常量时间比较）；
// 2. JWT admin 权限（PermAdminRead）。
func metricsAuthMW(cfg *config.Config, authMW routeMiddleware, h http.HandlerFunc) http.Handler {
	if cfg == nil || cfg.MetricsToken == "" {
		return authMW(RequirePermission(auth.PermAdminRead)(h))
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte("Bearer "+cfg.MetricsToken)) == 1 {
			h(w, r)
			return
		}
		authMW(RequirePermission(auth.PermAdminRead)(h)).ServeHTTP(w, r)
	})
}

// sessionCancels tracks running session contexts for cancellation support.
var sessionCancels sync.Map

// sessionCancel tracks the owner and cancel function of a running session task.
type sessionCancel struct {
	userID string
	cancel context.CancelFunc
}

// routeMiddleware is a middleware wrapper used by route registration helpers.
type routeMiddleware func(http.Handler) http.Handler

// middlewareChain wraps an http.Handler with a list of middleware functions.
func middlewareChain(h http.Handler, mws ...func(http.Handler) http.Handler) http.Handler {
	for i := len(mws) - 1; i >= 0; i-- {
		h = mws[i](h)
	}
	return h
}

// requestIDHeader generates a lightweight request ID.
func requestIDHeader(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var buf [8]byte
		if _, err := rand.Read(buf[:]); err != nil {
			// 极端回退：rand 失败用时间纳秒填充
			nano := time.Now().UnixNano()
			for i := range buf {
				buf[i] = byte(nano >> (i * 8))
			}
		}
		id := hex.EncodeToString(buf[:])
		r.Header.Set("X-Request-ID", id)
		w.Header().Set("X-Request-ID", id)
		next.ServeHTTP(w, r)
	})
}

// realIPHeader extracts the real IP from X-Forwarded-For / X-Real-IP,
// but ONLY when the direct peer is a trusted reverse proxy (P1 修复)：
// 无条件信任客户端可伪造的 XFF 会绕过按 IP 的限流与验证码失败升级。
func realIPHeader(trustedCIDRs []string) func(http.Handler) http.Handler {
	var trusted []*net.IPNet
	for _, c := range trustedCIDRs {
		if _, n, err := net.ParseCIDR(strings.TrimSpace(c)); err == nil {
			trusted = append(trusted, n)
		}
	}
	peerTrusted := func(remoteAddr string) bool {
		if len(trusted) == 0 {
			return false
		}
		host, _, err := net.SplitHostPort(remoteAddr)
		if err != nil {
			host = remoteAddr
		}
		ip := net.ParseIP(strings.TrimSpace(host))
		if ip == nil {
			return false
		}
		for _, n := range trusted {
			if n.Contains(ip) {
				return true
			}
		}
		return false
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if peerTrusted(r.RemoteAddr) {
				if fwd := r.Header.Get("X-Forwarded-For"); fwd != "" {
					if idx := strings.Index(fwd, ","); idx >= 0 {
						r.RemoteAddr = strings.TrimSpace(fwd[:idx])
					} else {
						r.RemoteAddr = strings.TrimSpace(fwd)
					}
				} else if realIP := r.Header.Get("X-Real-IP"); realIP != "" {
					r.RemoteAddr = realIP
				}
			}
			next.ServeHTTP(w, r)
		})
	}
}

// NewGatewayRouter creates a pure gateway router that proxies all business logic to Python.
// lifecycleCtx 用于控制内部后台协程（tenantResMgr / modeStore cleanup）的优雅关闭。
func NewGatewayRouter(
	lifecycleCtx context.Context,
	cfg *config.Config,
	pythonClient *engine.PythonClient,
	eventHub *broadcast.Hub,
	sessionMgr *session.Manager,
	fileStore *storage.AtomicStore,
	atomicRedis *db.AtomicRedis,
	rpaHub *RPAHub,
) http.Handler {
	mux := http.NewServeMux()

	// 运行时 CORS 白名单注入（批 B-2′）：CORSMiddleware / checkWebSocketOrigin / billing 统一读该共享源
	SetCORSAllowOrigin(cfg.CORSOrigins)

	// SSE 发布管线指标（B2）：hub 改为异步发布后，`fallback > 0` 表示队列曾被写满
	// （Redis 慢或事件突发，此时回退同步发布、调用方被阻塞），`queued` 持续高位表示
	// 投递跟不上生产。这两项是背压的直接信号，接入 /metrics 与 /v1/system/metrics。
	if eventHub != nil {
		monitor.RegisterExtraStats(func() map[string]interface{} {
			s := eventHub.Stats()
			return map[string]interface{}{
				"sse_publish_enqueued":  s.Enqueued,
				"sse_publish_delivered": s.Delivered,
				"sse_publish_fallback":  s.Fallback,
				"sse_publish_queued":    s.Queued,
			}
		})
	}

	// Rate limiter — 当 Redis 可用时使用分布式限流器。
	// 批 B-1′（令牌桶重构）：global/tenant/user 三级桶均为“每分钟配额（总量）”，
	// 与网关副本数无关——不再按 RATE_LIMIT_INSTANCES 放大，天然适配多副本。
	// 生产策略：
	//   - Redis 可用：用分布式限流器（推荐）
	//   - Redis 不可用 + 生产环境（cfg.RateLimitFailClose=true）：写操作拒绝
	//   - Redis 不可用 + 开发/测试：降级内存限流（单实例有效，多副本失效）
	var rlMW func(http.Handler) http.Handler
	var distLimiter *DistributedRateLimiter
	rateLimitRPM := cfg.RateLimitRPM
	if rateLimitRPM <= 0 {
		slog.Warn("RateLimitRPM is 0 or unset, using default 60 RPM")
		rateLimitRPM = 60
	}
	// 缺省上限（总量语义）：global = RATE_LIMIT_GLOBAL（后台 rate_limit.global 保存后覆盖），
	// tenant = global/10（下限不高于 rateLimitRPM），user = rateLimitRPM。
	globalLimit := cfg.RateLimitGlobal
	if globalLimit <= 0 {
		globalLimit = rateLimitRPM
	}
	tenantLimit := globalLimit / 10
	if tenantLimit < rateLimitRPM {
		tenantLimit = rateLimitRPM
	}
	if atomicRedis != nil {
		distLimiter = NewDistributedRateLimiter(
			atomicRedis.LoadRaw(),
			globalLimit,  // 全局：每分钟配额（总量）
			tenantLimit,  // 租户：每分钟配额
			rateLimitRPM, // 用户：每分钟配额
		)
		rlMW = DistributedRateLimitMiddleware(distLimiter)
		slog.Info("distributed token-bucket rate limiter enabled",
			"global", globalLimit, "tenant", tenantLimit, "user", rateLimitRPM)
		// 系统设置变更订阅（批 B-2′）：rate_limit / cors 跨副本即时热更
		StartSettingsSubscriber(lifecycleCtx, atomicRedis, distLimiter)
	} else if cfg.RateLimitFailClose {
		// 生产 fail-close：只读放行，写操作拒绝
		rlMW = func(next http.Handler) http.Handler {
			return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodGet && r.Method != http.MethodHead {
					http.Error(w, "rate limiter unavailable (redis down)", http.StatusServiceUnavailable)
					return
				}
				next.ServeHTTP(w, r)
			})
		}
		slog.Warn("Redis unavailable + fail-close enabled: write operations rejected")
	} else {
		// 开发/测试降级：内存限流（单实例有效，多副本部署下应改用 Redis）
		rateLimiter := NewRateLimiter(rateLimitRPM)
		rateLimiter.CleanupVisitors(5 * time.Minute)
		rlMW = rateLimiter.Middleware
		slog.Warn("local rate limiter enabled (no Redis); not safe for multi-replica production")
	}

	// Input sanitizer (prompt injection protection)
	inputSanitizer := NewInputSanitizer()
	sanitizeMW := SanitizeMiddleware(inputSanitizer)

	publicMW := func(next http.Handler) http.Handler {
		return middlewareChain(next,
			RecoverMiddleware,
			TracingMiddleware,
			LoggingMiddleware,
			SecurityHeadersMiddleware,
			CORSMiddleware(),
			MonitoringMiddleware,
			requestIDHeader,
			realIPHeader(cfg.TrustedProxyCIDRs),
			SanitizeResponseMiddleware, // P0-4: 敏感信息脱敏
		)
	}

	// Auth
	authenticator := auth.NewAuthenticator(cfg.JWTSecret, cfg.JWTExpiration)
	authHandler := NewAuthHandler(cfg)
	authMW := AuthMiddleware(authenticator)

	// SSO 三方登录 + 人机验证（防接口滥用）+ 短信验证码登录
	ssoHandler := NewSSOHandler(authenticator, cfg)
	captchaHandler := NewCaptchaHandler(cfg)
	authHandler.SetCaptchaHandler(captchaHandler)
	smsHandler := NewSmsHandler(authenticator, cfg, captchaHandler)

	// Billing
	billingStore := billing.NewPGStore()
	billingStore.EnsureTables(context.Background())
	billingMgr := billing.NewManager(billingStore)
	// P0-P1 修复：余额已由 Deduct/AddCredits 同步写库（PG 原子 UPDATE），
	// 移除 BalanceSyncer 异步落库订阅，避免多副本 split-brain 与重复扣费。
	// 2026-09 增强：扣减/入账与 credit_transactions 流水已同 PG 事务落库
	// （pgstore.applyCreditTx），异步 TransactionRecorder 随之移除，杜绝"已扣未记流水"。

	// Agent execution semaphore — global concurrency limit
	agentSem := NewSharedSemaphore(atomicRedis, "agent", cfg.AgentMaxConcurrency)

	// Tenant resource manager — per-tenant concurrency & storage quotas
	tenantResMgr := NewTenantResourceManager(atomicRedis, cfg.AgentMaxConcurrency)
	tenantResMgr.StartCleanup(lifecycleCtx, 30*time.Minute)

	// Submit handler (proxies to Python)
	submitHandler := NewSubmitHandler(pythonClient, sessionMgr, eventHub, billingMgr)

	// Plugins (MCP server config management)
	pluginHandler := NewPluginHandler(cfg, authenticator)

	// Search (auth + rate limited)
	searchHandler := NewSearchHandler()

	// Editor
	editorHandler := NewEditorHandler(cfg.StorageRoot)

	// Conversation
	conversationHandler := NewConversationHandler(authenticator, sessionMgr)
	shareHandler := NewShareHandler(authenticator, sessionMgr)

	// Tool (proxies to Python)
	toolHandler := NewToolHandler(pythonClient, authenticator)

	// System
	systemHandler := NewSystemHandlerWithEngine(pythonClient)

	// Media
	mediaHandler := NewMediaHandler(fileStore, authenticator)
	mediaHandler.SetMediaRoot(cfg.StorageRoot + "/media")

	// 通用分片上传（断点续传）
	uploadHandler := NewUploadHandler(authenticator, cfg.StorageRoot, fileStore)
	uploadHandler.RegisterRoutes(mux, authMW, rlMW)

	// 用户侧市场（技能/Agent/MCP 浏览与一键安装）
	userMarketHandler := NewUserMarketHandler(cfg, pythonClient)
	registerUserMarketRoutes(mux, userMarketHandler, authMW, rlMW)

	// 模型路由：对话可用模型列表
	mux.Handle("GET /v1/models", authMW(rlMW(http.HandlerFunc(ListUserModels))))
	// 模板市场：工作流/Agent/技能 一键使用
	templateHandler := NewTemplateHandler(pythonClient)
	templateHandler.RegisterRoutes(mux, authMW, rlMW)

	// 定时自动化：Webhook 触发（token 即鉴权，公开但限流）
	mux.Handle("POST /v1/hooks/{jobID}", rlMW(http.HandlerFunc(HandleCronWebhook)))

	// Billing handler (uses the same billingMgr as /submit to avoid split-brain cache)
	billingHandler := NewBillingHandler(billingMgr, authenticator, cfg)

	// Skill handler (proxies to Python)
	skillHandler := NewSkillHandler(pythonClient)

	// Mode（会话授权模式 ask/auto/yolo）：状态存 Redis 以保持多副本一致；
	// 审批本身在 Python 侧（guards + /v1/agent/approval），Go 侧不再保留第二套实现。
	modeStore := NewModeStore(atomicRedis)
	modeStore.StartCleanup(lifecycleCtx)
	modeHandler := NewModeHandler(modeStore, sessionMgr, eventHub)

	// Trace handler (Redis-backed, tenant-isolated)
	var traceHandler *TraceHandler
	if atomicRedis != nil {
		traceHandler = NewTraceHandler(atomicRedis.LoadRaw())
	}

	// Knowledge base — proxied to Python engine
	// SaaS 安全: 知识库独立限流 (每租户 QPS=50, Burst=100)
	var kbRateRedis db.RedisClient
	if atomicRedis != nil {
		kbRateRedis = atomicRedis.LoadRaw()
	}
	kbRateLimiter := NewTenantRateLimiter(kbRateRedis, 50, 100)
	kbRateMW := kbRateLimiter.Middleware

	// Admin handler
	adminHandler := NewAdminHandler(cfg, authenticator, fileStore, atomicRedis, pythonClient)
	adminHandler.rateLimiter = distLimiter
	adminHandler.appSecret = cfg.AppSecret

	// ── Route registration by functional domain ──

	registerPublicEndpoints(mux, authMW, rlMW, publicMW, searchHandler, shareHandler, systemHandler, mediaHandler, cfg)
	registerAgentRoutes(mux, authMW, rlMW, publicMW, sanitizeMW, submitHandler, billingMgr, agentSem, tenantResMgr, eventHub, sessionMgr, authenticator, rpaHub, cfg.InternalToken, cfg.AgentSubmitTimeout)
	registerAuthRoutes(mux, authHandler, authMW, rlMW)

	// ── SSO 三方登录（公开流程 rlMW；用户自助 authMW；管理 authMW + sso:manage）──
	ssoHandler.RegisterPublicRoutes(mux, rlMW)
	ssoHandler.RegisterUserRoutes(mux, authMW)
	ssoHandler.RegisterAdminRoutes(mux, authMW)

	// ── 人机验证：公开配置下发（登录页拉取）+ 管理配置 ──
	captchaHandler.RegisterPublicRoutes(mux, rlMW)
	captchaHandler.RegisterAdminRoutes(mux, authMW)

	// ── 短信验证码登录（公开流程 rlMW；用户自助 authMW；管理 authMW + sso:manage）──
	smsHandler.RegisterPublicRoutes(mux, rlMW)
	smsHandler.RegisterUserRoutes(mux, authMW)
	smsHandler.RegisterAdminRoutes(mux, authMW)
	registerSystemRoutes(mux, authMW, rlMW, sanitizeMW, editorHandler, toolHandler, systemHandler, traceHandler)
	// 六大工作台互联：跨台最近活动聚合（租户+用户隔离）
	mux.Handle("GET /v1/activities", authMW(rlMW(http.HandlerFunc(handleActivities))))
	registerConversationRoutes(mux, conversationHandler, shareHandler, authMW, rlMW)
	registerMediaRoutes(mux, mediaHandler, authMW, rlMW, cfg.StorageRoot)
	registerPluginRoutes(mux, pluginHandler, authMW, rlMW)
	registerBillingRoutes(mux, billingHandler, authMW, rlMW)
	registerProxyRoutes(mux, authMW, rlMW, kbRateMW, pythonClient)

	// Agents (auth + rate limited; DB 驱动 CRUD + 运行会话)
	agentHandler := NewAgentHandler(authenticator, pythonClient, agentSem)
	mux.Handle("GET /v1/agents", authMW(rlMW(http.HandlerFunc(agentHandler.List))))
	mux.Handle("POST /v1/agents", authMW(rlMW(http.HandlerFunc(agentHandler.Create))))
	mux.Handle("GET /v1/agents/{id}", authMW(rlMW(http.HandlerFunc(agentHandler.Get))))
	mux.Handle("PUT /v1/agents/{id}", authMW(rlMW(http.HandlerFunc(agentHandler.Update))))
	mux.Handle("DELETE /v1/agents/{id}", authMW(rlMW(http.HandlerFunc(agentHandler.Delete))))
	mux.Handle("PUT /v1/agents/{id}/visibility", authMW(rlMW(http.HandlerFunc(agentHandler.SetVisibility))))
	mux.Handle("POST /v1/agents/{id}/run", authMW(rlMW(http.HandlerFunc(agentHandler.Run))))
	mux.Handle("GET /v1/agents/sessions", authMW(rlMW(http.HandlerFunc(agentHandler.ListSessions))))
	mux.Handle("GET /v1/agents/sessions/{id}", authMW(rlMW(http.HandlerFunc(agentHandler.GetSession))))

	// 会话运行时状态与遥测（P1 单一事实源；见 docs/session-runtime-spec.md）
	sessionRuntimeHandler := NewSessionRuntimeHandler(db.Redis, sessionMgr)
	mux.Handle("GET /v1/sessions/{session_id}/runtime", authMW(rlMW(http.HandlerFunc(sessionRuntimeHandler.GetRuntime))))
	mux.Handle("PUT /v1/sessions/{session_id}/runtime", authMW(rlMW(http.HandlerFunc(sessionRuntimeHandler.PutRuntime))))
	mux.Handle("GET /v1/sessions/{session_id}/metrics", authMW(rlMW(http.HandlerFunc(sessionRuntimeHandler.GetMetrics))))
	// dispatch 保留 Python 代理（agent 工具链内部调用，非页面主链路）
	// 安全：必须经过 authMW，否则未认证可触发工具执行
	mux.Handle("POST /v1/agents/dispatch", authMW(rlMW(sanitizeMW(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if pythonClient == nil {
			InternalError(w, "python engine not available")
			return
		}
		var body map[string]interface{}
		if err := DecodeJSON(w, r, &body); err != nil {
			BadRequest(w, "invalid request")
			return
		}
		// 身份注入（P1）：引擎无鉴权，必须用 JWT claims 覆盖透传的租户/用户身份，
		// 防止客户端伪造 tenant_id/user_id 冒充他人。
		if claims := auth.GetClaims(r.Context()); claims != nil {
			body["tenant_id"] = claims.TenantID
			body["user_id"] = claims.UserID
		}
		var resp map[string]interface{}
		if err := pythonClient.PostJSON(r.Context(), "/v1/agents/dispatch", body, &resp); err != nil {
			InternalError(w, "python agent dispatch failed")
			return
		}
		OK(w, resp)
	})))))

	// Skills (auth + rate limited + prompt sanitized, proxies to Python)
	skillHandler.RegisterRoutes(mux, authMW, rlMW, sanitizeMW)

	// Enterprise audit (auth + RequireEntPerm("audit:read"))
	NewEntAuditHandler().RegisterRoutes(mux, authMW)

	// Enterprise identity (auth + RequireEntPerm("ent:manage"))：用户/角色/群组/租户
	NewEntIdentityHandler().RegisterRoutes(mux, authMW)

	// Enterprise cost center（authMW；handler 内按 admin:read / admin:write 分级，
	// 判定经 AllowedByEntOrLegacy —— 企业 RBAC 优先、无 ent 配置时回退 legacy 角色
	// 权限，与其它 ent 模块的 RequireEntPerm 同语义）
	NewEntCostCenterHandler(nil, nil).RegisterRoutes(mux, authMW)

	// Enterprise policy（authMW + RequireEntPerm("policy:manage")）：隐私模式 + 模型策略
	NewEntPolicyHandler().RegisterRoutes(mux, authMW)

	// Enterprise market（authMW + RequireEntPerm("market:manage")）：能力市场 + 租户授权
	NewMarketHandler().RegisterRoutes(mux, authMW)

	// Enterprise model router（authMW + RequireEntPerm("model:route")）：租户模型路由配置
	// 修复：InitTable 原设计"服务启动时调用"但缺失调用点，导致引擎启动拉取
	// /v1/internal/model-routes 时 500（ent_model_routes 表不存在）。此处建表后再注册。
	modelRouter := NewEntModelRouterHandler()
	modelRouter.InitTable()
	modelRouter.RegisterRoutes(mux, authMW)

	// Enterprise webhook（authMW + RequireEntPerm("webhook:manage")）：事件通知
	// 企业 Webhook 可靠投递器：Redis 消费组跨实例投递（入口 IngestEvent 已持久化入流）
	whDispatcher := NewWebhookDispatcher(atomicRedis)
	go whDispatcher.Start(lifecycleCtx)
	NewEntWebhookHandler().RegisterRoutes(mux, authMW)

	// RPA 跨实例桥接（批 D）：订阅 rpa:cmd/rpa:res，让插件 WS 与 exec 请求可落在不同网关副本。
	// rpaHub 由 NewGatewayRouter 的调用方（main.go）以 db.Redis 构造；Redis 不可用时为 localOnly 空操作。
	rpaHub.Start(lifecycleCtx)

	// Enterprise chaos（authMW + RequireEntPerm("chaos:manage")）：混沌工程
	NewEntChaosHandler().RegisterRoutes(mux, authMW)

	// Mode (auth + rate limited)
	mux.Handle("GET /v1/mode", authMW(rlMW(http.HandlerFunc(modeHandler.GetMode))))
	mux.Handle("POST /v1/mode", authMW(rlMW(http.HandlerFunc(modeHandler.SetMode))))
	// 旧 /v1/permission/approve|reject 已移除：审批统一由 Python 侧处理
	// （前端经 /v1/agent/approval 提交决定），避免 Go/Python 双实现漂移。

	registerAdminRoutes(mux, authMW, rlMW, adminHandler, pythonClient)

	// Wrap main mux with public middleware
	return publicMW(mux)
}

// ── Public endpoints ──

func registerPublicEndpoints(
	mux *http.ServeMux,
	authMW, rlMW, publicMW routeMiddleware,
	searchHandler *SearchHandler,
	shareHandler *ShareHandler,
	systemHandler *SystemHandler,
	mediaHandler *MediaHandler,
	cfg *config.Config,
) {
	mux.Handle("GET /search", authMW(rlMW(http.HandlerFunc(searchHandler.Search))))
	mux.Handle("GET /v1/search", authMW(rlMW(http.HandlerFunc(searchHandler.Search))))

	// Public share view (no auth; revoked shares return 410 Gone)
	// 修复 P1：移除内层 publicMW 重复包裹（外层 publicMW(mux) 已包含日志/审计/追踪），
	// 避免审计 XAdd 双写、请求 ID 被内层重新生成。
	mux.Handle("GET /v1/share/{id}", rlMW(http.HandlerFunc(shareHandler.PublicGet)))

	mux.Handle("GET /health", rlMW(http.HandlerFunc(handleHealth)))
	// Prometheus 指标端点：生产收敛为需要 PermAdminRead 权限，避免泄漏业务指标
	mux.Handle("GET /metrics", rlMW(metricsAuthMW(cfg, authMW, systemHandler.PrometheusMetrics)))
	// API 文档（OpenAPI spec，公开，供 Swagger/Redoc 展示）
	mux.Handle("GET /docs/", http.StripPrefix("/docs/", http.FileServer(http.Dir("docs"))))
	mux.Handle("GET /ready", rlMW(http.HandlerFunc(handleReadiness)))
	// 引擎配置下发（X-Internal-Token 保护，Python 引擎启动拉取）
	mux.Handle("GET /v1/internal/engine-config", rlMW(internalTokenMW(cfg, EngineConfig(cfg))))

	// 模型路由配置同步（X-Internal-Token 保护，Python 引擎启动时拉取）
	mux.Handle("GET /v1/internal/model-routes", rlMW(internalTokenMW(cfg, http.HandlerFunc(NewEntModelRouterHandler().SyncRoutes))))

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
}

// ── Agent submit/cancel/events ──

func registerAgentRoutes(
	mux *http.ServeMux,
	authMW, rlMW, publicMW, sanitizeMW routeMiddleware,
	submitHandler *SubmitHandler,
	billingMgr *billing.Manager,
	agentSem *SharedSemaphore,
	tenantResMgr *TenantResourceManager,
	eventHub *broadcast.Hub,
	sessionMgr *session.Manager,
	authenticator *auth.Authenticator,
	rpaHub *RPAHub,
	internalToken string,
	// submitTimeout 单次提交（一条 SSE 回合）的后台执行上限；
	// 来自 config.AgentSubmitTimeout，<=0 时回退 DefaultAgentTimeout。
	submitTimeout time.Duration,
) {
	mux.Handle("POST /v1/agent/approval", authMW(rlMW(http.HandlerFunc(submitHandler.SubmitApproval))))

	// submitHandlerFunc 提取为命名函数，用于 legacy 和 v1 双路由注册
	submitHandlerFunc := func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Content   string                 `json:"content"`
			SessionID string                 `json:"session_id"`
			LLMConfig map[string]interface{} `json:"llm_config"`
			// 工作台上下文（kb_id / agent / skill_names / workflow_id 等）：由前端
			// ChatView.buildContext 组装。此前该字段未被解析 → 前端的"带知识库/技能/
			// Agent 进入对话"在网关就被丢弃，是六大工作台互通的根断点。
			Context map[string]interface{} `json:"context"`
		}
		if err := DecodeJSON(w, r, &body); err != nil {
			BadRequest(w, "invalid request")
			return
		}
		if body.Content == "" {
			BadRequest(w, "content is required")
			return
		}
		claims := auth.GetClaims(r.Context())
		if claims == nil {
			Unauthorized(w, ErrAuthRequired)
			return
		}
		userID := claims.UserID

		// Billing pre-check
		if billingMgr != nil {
			count, err := billingMgr.DailyFreeCount(r.Context(), userID)
			overFreeQuota := err != nil || count >= billing.DailyFreeLimit
			if overFreeQuota {
				if balance, balErr := billingMgr.GetBalance(userID); balErr == nil && balance <= 0 {
					JSON(w, http.StatusPaymentRequired, APIResponse{
						Success: false,
						Error:   "insufficient credits — please recharge in Billing",
					})
					return
				}
			}
		}

		// 租户 token 配额预检（企业版配额池 ent_quota_pools；fail-open，见
		// ent_quota_enforce.go）。接线位置在 billing 预检之后、run 锁与并发槽位之前：
		// 此时尚未占用任何资源，直接拒绝即可，无需回滚。
		//
		// 注意：EnforceTenantQuota 仅在配额超限时返回非 nil；其计数是"请求级近似
		// 用量"（每次调用 INCR 1，缓存缺失时先从 billing_records 回填），真实 token
		// 用量仍以计费链路为准。
		if err := EnforceTenantQuota(r.Context(), claims, 0); err != nil {
			monitor.IncQuotaExceeded()
			slog.Warn("tenant token quota exceeded, submit rejected",
				"user_id", userID, "tenant_id", claims.TenantID)
			JSON(w, http.StatusTooManyRequests, APIResponse{
				Success: false,
				Error:   "tenant token quota exceeded",
			})
			return
		}

		// Reject concurrent submits within the same session（跨实例：Redis 运行锁）
		// 修复：后台任务不得挂在 r.Context() 上——202 响应返回后客户端连接可关闭/断开，
		// 会立即取消整条 submit 链路（曾致 "request cancelled before attempt 1" 的
		// "Service temporarily unavailable"）。WithoutCancel 保留 ctx 携带值（trace 等），
		// 仅剥离取消/超时；下方独立超时兜底。
		//
		// 修复：此前硬编码 180s，比 api.DefaultAgentTimeout(300s) 更短，长回合
		// （多轮 LLM + 工具调用）必然被提前取消，表现为"思考/工具调用做一半就断"。
		// 现取配置项 AGENT_SUBMIT_TIMEOUT（默认 5 分钟），并兜底不低于 DefaultAgentTimeout。
		limit := submitTimeout
		if limit < DefaultAgentTimeout {
			limit = DefaultAgentTimeout
		}
		ctx, cancel := context.WithTimeout(context.WithoutCancel(r.Context()), limit)
		// 批 E2：每次 run 唯一 token（锁归属校验：续期/释放均需匹配，防旧 run 误删新锁）
		var rnd [12]byte
		_, _ = rand.Read(rnd[:])
		runToken := userID + "-" + hex.EncodeToString(rnd[:])
		releaseRun, runLocked, lockErr := AcquireSessionRunLock(r.Context(), body.SessionID, runToken)
		if lockErr != nil {
			// Redis 不可用：兑底进程内防重（多实例下退化为近似限制）
			slog.Warn("session run lock degraded to in-process (redis unavailable)",
				"session_id", body.SessionID)
			if _, loaded := sessionCancels.LoadOrStore(body.SessionID, sessionCancel{userID: userID, cancel: cancel}); loaded {
				cancel()
				BadRequest(w, "task already running for this session")
				return
			}
		} else if !runLocked {
			cancel() // cancel the new one since there's already an active task
			BadRequest(w, "task already running for this session")
			return
		} else {
			// 分布式锁持有成功：登记本地 registry（供取消）
			sessionCancels.Store(body.SessionID, sessionCancel{userID: userID, cancel: cancel})
		}

		releaseSem, ok := agentSem.TryAcquire(r.Context())
		if !ok {
			sessionCancels.Delete(body.SessionID)
			cancel()
			TooManyRequests(w)
			return
		}

		// 租户并发闸门（ent_quota_pools: resource_type='concurrency'，跨副本共享计数；
		// 未配置该配额的租户恒放行，见 TenantResourceManager.Acquire）。
		// 与全局 agentSem 合并为一个释放函数：后台 goroutine 继续 defer releaseSem()
		// 即可，无需再感知第二把闸门（避免漏释放导致租户槽位泄漏）。
		releaseTenant, tenantOK := tenantResMgr.Acquire(claims.TenantID)
		if !tenantOK {
			monitor.IncRateLimitBlocked()
			slog.Warn("tenant concurrency quota exhausted, submit rejected",
				"tenant_id", claims.TenantID, "user_id", userID)
			releaseSem()
			sessionCancels.Delete(body.SessionID)
			cancel()
			TooManyRequests(w)
			return
		}
		if releaseTenant != nil {
			releaseAgentSem := releaseSem
			releaseSem = func() {
				releaseTenant()
				releaseAgentSem()
			}
		}

		// 批 E2：run 锁心跳续期（60s/次，TTL=5min）。随 submit ctx 结束/run 完成自动停止；
		// 持有实例崩溃后无续期，锁 ≤5min 自动过期，用户可重试（历史消息已持久化）。
		var stopHeartbeat chan struct{}
		if releaseRun != nil {
			stopHeartbeat = make(chan struct{})
			go func() {
				ticker := time.NewTicker(60 * time.Second)
				defer ticker.Stop()
				for {
					select {
					case <-ctx.Done():
						return
					case <-stopHeartbeat:
						return
					case <-ticker.C:
						if !RefreshSessionRunLock(ctx, body.SessionID, runToken) {
							return // 锁已易主/过期：停止续期
						}
					}
				}
			}()
		}

		// ── 工作台互通兜底：只带 agent_id 时由网关补全 Agent 配置 ──
		// 前端「带 Agent 进对话」在拉不到 /v1/agents 配置时只发 agent_id，而引擎侧
		// 只读 context.agent（dict）→ 会静默退化成"没带 Agent"。桌面端与直连 API
		// 同样只传 id，所以补全放在网关，而不是要求每个客户端都发全量配置。
		resolveAgentContext(r.Context(), body.Context, claims.TenantID, userID)

		Accepted(w, map[string]string{"status": "accepted", "session_id": body.SessionID})
		go func() {
			if releaseRun != nil {
				defer releaseRun()
			}
			if stopHeartbeat != nil {
				defer close(stopHeartbeat)
			}
			defer releaseSem()
			defer func() {
				if r := recover(); r != nil {
					slog.Error("submit handler panic", "panic", r)
				}
			}()
			defer cancel()
			defer sessionCancels.Delete(body.SessionID)
			submitHandler.HandleSubmit(ctx, userID, body.SessionID, body.Content, body.LLMConfig, body.Context)
		}()
	}
	submitMW := authMW(sanitizeMW(http.HandlerFunc(submitHandlerFunc)))
	// Legacy 路由：向前兼容旧版前端
	mux.Handle("POST /submit", submitMW)
	// 版本化路由：v1 agent submit 入口
	mux.Handle("POST /v1/agent/submit", submitMW)

	// cancelHandlerFunc 提取为命名函数，用于 legacy 和 v1 双路由注册
	cancelHandlerFunc := func(w http.ResponseWriter, r *http.Request) {
		handleCancel(w, r)
	}
	cancelMW := authMW(rlMW(http.HandlerFunc(cancelHandlerFunc)))
	mux.Handle("POST /cancel", cancelMW)
	mux.Handle("POST /v1/agent/cancel", cancelMW)

	// 实时通道统一为 SSE（events.go：Redis Stream + Pub/Sub 跨实例、Last-Event-ID 断线重放）。
	// 批 B-3′：下线 /ws/{sessionId} 与 WebSocketHub（前端仅 EventSource）；RPA 插件通道 /ws/rpa 保留。
	sseHandler := SSEHandler(eventHub, sessionMgr)
	mux.Handle("GET /events", authMW(rlMW(sseHandler)))
	mux.Handle("GET /v1/events", authMW(rlMW(sseHandler)))
	mux.HandleFunc("GET /ws/rpa", RPAWebSocketHandler(rpaHub, authenticator))
	// 浏览器 RPA 桥（Python engine → 网关 → 插件；仅共享 internal token 可调）
	mux.Handle("POST /v1/rpa/exec", rlMW(http.HandlerFunc(RPAExecHandler(rpaHub, internalToken))))
	mux.Handle("GET /v1/rpa/clients", rlMW(http.HandlerFunc(RPAClientsHandler(rpaHub, internalToken))))
}

// ── Auth (login / register / logout) ──

func registerAuthRoutes(mux *http.ServeMux, authHandler *AuthHandler, authMW, rlMW routeMiddleware) {
	// Auth (public, rate limited)
	mux.Handle("POST /v1/auth/login", rlMW(http.HandlerFunc(authHandler.Login)))
	mux.Handle("POST /v1/auth/register", rlMW(http.HandlerFunc(authHandler.Register)))
	mux.Handle("POST /v1/auth/refresh", rlMW(http.HandlerFunc(authHandler.Refresh)))
	mux.Handle("POST /v1/auth/logout", rlMW(http.HandlerFunc(authHandler.Logout)))
	// SSO cookie → Bearer token 会话引导（公开：httpOnly cookie 自带凭据）
	mux.Handle("GET /v1/auth/session", rlMW(http.HandlerFunc(authHandler.Session)))
	mux.Handle("GET /v1/auth/profile", authMW(rlMW(http.HandlerFunc(authHandler.Profile))))
	mux.Handle("PUT /v1/auth/profile", authMW(rlMW(http.HandlerFunc(authHandler.UpdateProfile))))
}

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
	mux.Handle("GET /v1/tools", rlMW(http.HandlerFunc(toolHandler.ListTools)))
	mux.Handle("POST /v1/tools/execute", authMW(rlMW(sanitizeMW(http.HandlerFunc(toolHandler.ExecuteTool)))))

	// System (rate limited; spans/traces 仅管理员可见，S 安全修复：原为公开信息泄露)
	mux.Handle("GET /v1/system/health", rlMW(http.HandlerFunc(systemHandler.HealthScores)))
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
}

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
	// 当前用户的分享列表（数据管理 → 分享管理）；与上面的按会话接口同组、同鉴权。
	mux.Handle("GET /v1/shares", authMW(rlMW(http.HandlerFunc(shareHandler.List))))
}

// ── Media ──

func registerMediaRoutes(
	mux *http.ServeMux,
	mediaHandler *MediaHandler,
	authMW, rlMW routeMiddleware,
	storageRoot string,
) {
	// Media (auth + rate limited)
	mux.Handle("GET /v1/media", authMW(rlMW(http.HandlerFunc(mediaHandler.List))))
	mux.Handle("POST /v1/media", authMW(rlMW(http.HandlerFunc(mediaHandler.Create))))
	mux.Handle("POST /v1/media/folders", authMW(rlMW(http.HandlerFunc(mediaHandler.CreateFolder))))
	mux.Handle("GET /v1/media/folders", authMW(rlMW(http.HandlerFunc(mediaHandler.ListFolders))))
	mux.Handle("POST /v1/media/upload", authMW(rlMW(http.HandlerFunc(mediaHandler.Upload))))
	mux.Handle("POST /v1/media/presign", authMW(rlMW(http.HandlerFunc(mediaHandler.PresignUpload))))
	mux.Handle("POST /v1/media/complete", authMW(rlMW(http.HandlerFunc(mediaHandler.CompleteUpload))))
	mux.Handle("POST /v1/media/batch-delete", authMW(rlMW(http.HandlerFunc(mediaHandler.BatchDelete))))
	mux.Handle("PUT /v1/media/{id}", authMW(rlMW(http.HandlerFunc(mediaHandler.Update))))
	mux.Handle("GET /v1/media/{id}/download", authMW(rlMW(http.HandlerFunc(mediaHandler.Download))))
	mux.Handle("POST /v1/media/{id}/share", authMW(rlMW(http.HandlerFunc(mediaHandler.Share))))
	mux.Handle("DELETE /v1/media/{id}", authMW(rlMW(http.HandlerFunc(mediaHandler.Delete))))

	// Media file serving（P0 安全修复：禁止目录遍历，仅允许签名URL访问）
	// 原代码允许通过 /media/ 直接浏览所有用户文件，现已移除
	// 所有媒体访问必须通过签名URL (/media/s/{assetID}) 或 API端点 (/v1/media/{id}/download)

	// 签名 URL（P0 修复）：签发 + 校验后服务
	mux.Handle("POST /v1/media/{id}/sign", authMW(rlMW(http.HandlerFunc(mediaHandler.SignMedia))))
	mux.Handle("GET /media/s/{assetID}", rlMW(http.HandlerFunc(mediaHandler.ServeSignedMedia)))
}

// registerUserMarketRoutes 用户侧市场路由（技能/Agent/MCP 浏览与一键安装）。
func registerUserMarketRoutes(mux *http.ServeMux, h *UserMarketHandler, authMW, rlMW routeMiddleware) {
	mux.Handle("GET /v1/market", authMW(rlMW(http.HandlerFunc(h.List))))
	mux.Handle("POST /v1/market/{type}/{itemID}/install", authMW(rlMW(http.HandlerFunc(h.Install))))
}

// ── Plugins ──

func registerPluginRoutes(mux *http.ServeMux, pluginHandler *PluginHandler, authMW, rlMW routeMiddleware) {
	// Plugins (auth + rate limited)
	mux.Handle("GET /v1/plugins", authMW(rlMW(http.HandlerFunc(pluginHandler.List))))
	mux.Handle("POST /v1/plugins/{name}/install", authMW(rlMW(http.HandlerFunc(pluginHandler.Install))))
	mux.Handle("PUT /v1/plugins/{name}", authMW(rlMW(http.HandlerFunc(pluginHandler.Update))))
	mux.Handle("POST /v1/plugins/{name}/test", authMW(rlMW(http.HandlerFunc(pluginHandler.Test))))
	mux.Handle("DELETE /v1/plugins/{name}", authMW(rlMW(http.HandlerFunc(pluginHandler.Uninstall))))
}

// ── Billing ──

func registerBillingRoutes(mux *http.ServeMux, billingHandler *BillingHandler, authMW, rlMW routeMiddleware) {
	// Billing (auth + rate limited)
	mux.Handle("GET /v1/billing/balance", authMW(rlMW(http.HandlerFunc(billingHandler.GetBalance))))
	mux.Handle("GET /v1/billing/history", authMW(rlMW(http.HandlerFunc(billingHandler.GetHistory))))
	mux.Handle("POST /v1/billing/recharge", authMW(rlMW(http.HandlerFunc(billingHandler.Recharge))))
	mux.Handle("POST /v1/billing/pay", authMW(rlMW(http.HandlerFunc(billingHandler.CreatePayment))))
	mux.Handle("GET /v1/billing/orders/{id}", authMW(rlMW(http.HandlerFunc(billingHandler.GetOrder))))
	// 支付渠道异步回调（无 auth：支付宝验签 / 微信平台证书验签 + AES-GCM 解密）
	mux.Handle("POST /v1/billing/callback/alipay", rlMW(http.HandlerFunc(billingHandler.AlipayCallback)))
	mux.Handle("POST /v1/billing/callback/wechat", rlMW(http.HandlerFunc(billingHandler.WechatCallback)))
	mux.Handle("POST /v1/billing/paypal-capture", authMW(rlMW(http.HandlerFunc(billingHandler.PayPalCapture))))
	mux.Handle("GET /v1/billing/usage", authMW(rlMW(http.HandlerFunc(billingHandler.GetUsage))))
}

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

// ── Admin ──

func registerAdminRoutes(
	mux *http.ServeMux,
	authMW routeMiddleware,
	rlMW routeMiddleware,
	adminHandler *AdminHandler,
	pythonClient *engine.PythonClient,
) {
	// Admin routes (auth + admin permission + rate limit)
	// P1-3: 所有 admin 路由必须挂限流，防止被劫持的 admin token 无限调用
	// 造成破坏（backup/restore/users DELETE 等敏感操作）。
	// 读操作用 PermAdminRead，写操作（PUT/DELETE/POST）必须 PermAdminWrite。
	// P1-4: 用户管理路由（PUT/DELETE /v1/admin/users）必须 PermUsersManage，
	// 该权限仅 owner 角色持有，普通 admin 不应能删/改用户。
	adminReadMW := RequirePermission(auth.PermAdminRead)
	adminWriteMW := RequirePermission(auth.PermAdminWrite)
	usersManageMW := RequirePermission(auth.PermUsersManage)
	adminMux := http.NewServeMux()
	adminHandler.RegisterRoutes(adminMux)
	// Strip the /v1/admin prefix so adminMux patterns (e.g. "GET /metrics") match correctly
	adminStrip := http.StripPrefix("/v1/admin", adminMux)
	mux.Handle("GET /v1/admin/metrics", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("GET /v1/admin/users", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("GET /v1/admin/users/{id}", authMW(rlMW(adminReadMW(adminStrip))))
	// 用户写操作收紧为 PermUsersManage（仅 owner）
	mux.Handle("PUT /v1/admin/users/{id}", authMW(rlMW(usersManageMW(adminStrip))))
	mux.Handle("DELETE /v1/admin/users/{id}", authMW(rlMW(usersManageMW(adminStrip))))
	mux.Handle("GET /v1/admin/system", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("POST /v1/admin/maintenance", authMW(rlMW(adminWriteMW(adminStrip))))
	mux.Handle("POST /v1/admin/backup", authMW(rlMW(adminWriteMW(adminStrip))))
	mux.Handle("POST /v1/admin/restore", authMW(rlMW(adminWriteMW(adminStrip))))
	mux.Handle("GET /v1/admin/kb", authMW(rlMW(adminReadMW(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if pythonClient == nil || !pythonClient.IsConnected() {
			InternalError(w, "python engine not available")
			return
		}
		claims := auth.GetClaims(r.Context())
		r.Header.Set("X-User-Role", claims.Role)
		pythonClient.ForwardRequest(w, r, "/v1/admin/kb?user_id="+claims.UserID+"&tenant_id="+claims.TenantID)
	})))))

	// Storage admin routes
	mux.Handle("GET /v1/admin/storage", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("PUT /v1/admin/storage", authMW(rlMW(adminWriteMW(adminStrip))))
	mux.Handle("POST /v1/admin/storage/test", authMW(rlMW(adminWriteMW(adminStrip))))

	// Redis admin routes
	mux.Handle("GET /v1/admin/redis", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("PUT /v1/admin/redis", authMW(rlMW(adminWriteMW(adminStrip))))
	mux.Handle("POST /v1/admin/redis/test", authMW(rlMW(adminWriteMW(adminStrip))))

	// Queue admin routes
	mux.Handle("GET /v1/admin/queue", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("POST /v1/admin/queue/flush", authMW(rlMW(adminWriteMW(adminStrip))))
	mux.Handle("POST /v1/admin/queue/pause", authMW(rlMW(adminWriteMW(adminStrip))))

	// Cache admin routes
	mux.Handle("GET /v1/admin/cache/stats", authMW(rlMW(adminReadMW(adminStrip))))

	// Performance admin routes
	mux.Handle("GET /v1/admin/performance", authMW(rlMW(adminReadMW(adminStrip))))

	// API Key admin routes (direct handlers, avoid adminMux path mismatch)
	mux.Handle("GET /v1/admin/api-keys", authMW(rlMW(adminReadMW(http.HandlerFunc(adminHandler.ListLLMKeys)))))
	mux.Handle("POST /v1/admin/api-keys", authMW(rlMW(adminWriteMW(http.HandlerFunc(adminHandler.AddLLMKey)))))
	mux.Handle("PUT /v1/admin/api-keys/{id}", authMW(rlMW(adminWriteMW(http.HandlerFunc(adminHandler.UpdateLLMKeyStatus)))))
	mux.Handle("DELETE /v1/admin/api-keys/{id}", authMW(rlMW(adminWriteMW(http.HandlerFunc(adminHandler.DeleteLLMKey)))))

	// LLM provider catalog routes（服务提供商目录与端点覆盖）
	mux.Handle("GET /v1/admin/llm-providers", authMW(rlMW(adminReadMW(http.HandlerFunc(adminHandler.ListLLMProviders)))))
	mux.Handle("PUT /v1/admin/llm-providers/{id}", authMW(rlMW(adminWriteMW(http.HandlerFunc(adminHandler.SetLLMProviderConfig)))))

	// 模型配置：决定 /v1/models（进而决定 /chat 的模型下拉）里有哪些模型。
	// adminMux 里**早已注册** GET/POST /models 与 PUT/DELETE /models/{id}（admin_ops.go:60-63），
	// 但从未在此暴露到外部 mux → 前端无论怎么点都调不到，只能依赖模型发现自动写入；
	// 一旦发现失败（provider 无 /models、UA 被拦、网络不可达），可用模型集永远是空的。
	mux.Handle("GET /v1/admin/models", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("POST /v1/admin/models", authMW(rlMW(adminWriteMW(adminStrip))))
	mux.Handle("PUT /v1/admin/models/{id}", authMW(rlMW(adminWriteMW(adminStrip))))
	mux.Handle("DELETE /v1/admin/models/{id}", authMW(rlMW(adminWriteMW(adminStrip))))

	// Settings admin routes
	mux.Handle("PUT /v1/admin/settings", authMW(rlMW(adminWriteMW(adminStrip))))
}
