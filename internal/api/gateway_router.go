package api

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"log/slog"
	"net"
	"net/http"
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
	"github.com/athenavi/chiron/internal/settings"
	"github.com/athenavi/chiron/internal/storage"
)

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
//
// ⚠️ 值必须是 ***sessionCancel**（指针）。`sessionCancel` 里有 `cancel` 函数字段，
// 函数不可比较 —— 存值类型会让 `sync.Map.CompareAndDelete` 直接 panic
// （`sync: comparing non-comparable value`），而取消路径正是靠它做原子认领的。
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
// lifecycleCtx 用于控制内部后台协程（tenantResMgr / sessionmap 等）的优雅关闭。
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

	// SSO 三方登录 + 人机验证（防接口滥用）+ 短信验证码登录 + 邮箱验证码/密码重置
	ssoHandler := NewSSOHandler(authenticator, cfg)
	captchaHandler := NewCaptchaHandler(cfg)
	authHandler.SetCaptchaHandler(captchaHandler)
	smsHandler := NewSmsHandler(authenticator, cfg, captchaHandler)
	mailHandler := NewMailHandler(authenticator, cfg, captchaHandler)
	// 注册流程复用邮件能力（邮箱验证码校验 + 欢迎邮件）
	authHandler.SetMailHandler(mailHandler)

	// Billing
	billingStore := billing.NewPGStore()
	if err := billingStore.VerifySchema(context.Background()); err != nil {
		// schema 由 Alembic 迁移维护（应用不建表），这里只做只读校验：
		// 宁可启动日志里明确报出缺表，也不要拖到首次下单才以笼统 500 暴露。
		slog.Error("billing schema verification failed; run `alembic upgrade head`", "error", err)
	}
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

	// 后台「支付配置」保存在 system_settings.payment 的凭据叠加到 env 之上，
	// 使页面里配置的渠道在重启后依然生效（DB 值优先、env 兜底）。
	var settingsStore *settings.Store
	if db.Pool != nil {
		settingsStore = settings.New(db.Pool, cfg.AppSecret)
	}
	billingHandler.ReloadPaymentConfig(context.Background(), settingsStore)

	// 系统设置变更订阅（批 B-2′）：rate_limit / cors / payment 跨副本即时热更。
	// 必须晚于 billingHandler 构造：订阅者需要它来重载 payment 分类的支付凭据。
	StartSettingsSubscriber(lifecycleCtx, atomicRedis, distLimiter, billingHandler, settingsStore)

	// Skill handler (proxies to Python)
	skillHandler := NewSkillHandler(pythonClient)

	// 工具授权模式（ask/auto/yolo）不在这里持有状态：它是前端的实时状态，随每次提交经
	// llm_config.tools_mode 携带，引擎侧校验后用于工具裁决（guards.py）。
	// 曾有的 ModeStore（Redis）与 /v1/mode 接口已删除。

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
	// 复用同一 settings.Store：后台「支付配置」的保存路径要用它加密落库
	adminHandler.settingsStore = settingsStore
	// 后台「支付配置」需要读取生效快照并触发热重载
	adminHandler.billingHandler = billingHandler

	// ── Route registration by functional domain ──

	registerPublicEndpoints(mux, authMW, rlMW, publicMW, searchHandler, shareHandler, systemHandler, mediaHandler, cfg, fileStore)
	registerAgentRoutes(mux, authMW, rlMW, publicMW, sanitizeMW, submitHandler, billingMgr, agentSem, tenantResMgr, eventHub, sessionMgr, authenticator, rpaHub, cfg.InternalToken, cfg.AgentSubmitTimeout)

	// 子 Agent 完成 → 父会话新一轮：由引擎队列（agent_followup 任务）调用，
	// 复用 /submit 的全套闸门（幂等/会话锁/并发/计费预检，见 agent_followup.go）。
	mux.Handle("POST /v1/internal/agent-followup",
		rlMW(internalTokenMW(cfg, AgentFollowupHandler(submitHandler, billingMgr, agentSem, tenantResMgr, cfg.AgentSubmitTimeout))))

	// 会话分叉：地图"按真实分支连线"的前提（见 session_fork.go）。
	// 放这里而不是 registerAgentRoutes：那个函数的作用域里没有 sessionMgr。
	mux.Handle("POST /v1/conversations/{id}/fork", authMW(rlMW(ForkConversationHandler(sessionMgr))))
	// 会话分支（裁剪 + 压缩）：前端用它创建"带核心上下文的分支"（见 session_branch.go
	// 与 docs/session-map-branch-design.md）。与 /fork 共用 BranchSession，只差 mode。
	mux.Handle("POST /v1/conversations/{id}/branch", authMW(rlMW(BranchConversationHandler(sessionMgr, pythonClient))))
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

	// ── 邮件：邮箱验证码登录 / 注册邮箱验证 / 密码重置（公开流程 rlMW）──
	// 后台「邮件配置」在管理端挂载（authMW + sso:manage），发信服务器地址与凭据全部可配置。
	mailHandler.RegisterPublicRoutes(mux, rlMW)
	mailHandler.RegisterAdminRoutes(mux, authMW)
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
	// 建表已收敛到 Alembic；这里只做只读校验，缺表时明确告警（而不是静默让引擎侧 500）。
	modelRouter := NewEntModelRouterHandler()
	if err := modelRouter.VerifyTable(context.Background()); err != nil {
		slog.Error("ent_model_routes not ready; model routing will fail", "error", err)
	}
	modelRouter.RegisterRoutes(mux, authMW)

	// Enterprise webhook（authMW + RequireEntPerm("webhook:manage")）：事件通知
	// 企业 Webhook 可靠投递器：Redis 消费组跨实例投递（入口 IngestEvent 已持久化入流）
	whDispatcher := NewWebhookDispatcher(atomicRedis)
	go whDispatcher.Start(lifecycleCtx)
	NewEntWebhookHandler().RegisterRoutes(mux, authMW)

	// RPA 跨实例桥接（批 D）：订阅 rpa:cmd/rpa:res，让插件 WS 与 exec 请求可落在不同网关副本。
	// rpaHub 由 NewGatewayRouter 的调用方（main.go）以 db.Redis 构造；Redis 不可用时为 localOnly 空操作。
	rpaHub.Start(lifecycleCtx)

	// Enterprise chaos（authMW + RequireEntPerm("chaos:manage")）：混沌工程。
	// handler 需要 Redis 来失效 chaos:active:<tenant> 缓存，保证多副本一致。
	NewEntChaosHandler(atomicRedis).RegisterRoutes(mux, authMW)

	// 工具授权模式（GET/POST /v1/mode）已移除：模式不再是服务端状态，而是前端随
	// 每次提交携带的请求参数（llm_config.tools_mode）。见 mode.go 的说明。
	//
	// 旧 /v1/permission/approve|reject 已移除：审批统一由 Python 侧处理
	// （前端经 /v1/agent/approval 提交决定），避免 Go/Python 双实现漂移。

	registerAdminRoutes(mux, authMW, rlMW, adminHandler, pythonClient)

	// Wrap main mux with public middleware.
	//
	// 混沌注入在**最外层**（默认关闭，见 CHAOS_ENABLED）：整站故障应当也影响未认证
	// 请求与静态资源；关闭时它第一个判断就放行。
	return ChaosMiddleware(atomicRedis)(publicMW(mux))
}
