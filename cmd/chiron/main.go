package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/api"
	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/broadcast"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/monitor"
	"github.com/athenavi/chiron/internal/session"
	"github.com/athenavi/chiron/internal/settings"
	"github.com/athenavi/chiron/internal/storage"
)

func main() {
	// 严格加载：APP_SECRET 是部署级主密钥（派生 JWT_SECRET / INTERNAL_TOKEN、加密后台
	// 敏感配置），缺失或强度不足时直接拒绝启动；用 `python scripts/init.py` 生成并写入 .env。
	cfg, err := config.Load()
	if err != nil {
		slog.Error("FATAL: invalid configuration - refusing to start (run `python scripts/init.py`)", "error", err)
		return
	}

	// Logger
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level: parseLogLevel(cfg.LogLevel),
	})))

	slog.Info("starting chiron gateway", "version", "0.1.260825.01", "port", cfg.Port)

	// lifecycleCtx 受 OS signal 控制，用于所有后台协程优雅关闭。
	lifecycleCtx, lifecycleStop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer lifecycleStop()

	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	// 60s 初始超时：DB 连接 + 迁移可能耗时较长，15s 不足
	defer cancel()

	// ── PostgreSQL ──
	pgConnected := false
	if len(cfg.PostgresReadDSNs) > 0 {
		// Read replicas configured — use DatabaseRouter for read/write splitting
		poolCfg := db.PoolConfig{
			MaxConns:          cfg.PostgresMaxConn,
			MinConns:          cfg.PostgresMinConn,
			MaxConnLifetime:   30 * time.Minute,
			MaxConnIdleTime:   5 * time.Minute,
			HealthCheckPeriod: 30 * time.Second,
		}
		router, err := db.NewDatabaseRouter(ctx, cfg.PostgresDSN, cfg.PostgresReadDSNs, poolCfg)
		if err != nil {
			slog.Warn("database router init failed, falling back to single pool", "error", err)
		} else {
			db.Router = router
			db.Pool = router.Write() // backward compatibility alias
			pgConnected = true
			defer router.Close()
			// schema 版本校验（只读）：迁移由发布流程/DBA 执行，应用不再自行迁移
			verifySchemaVersion(ctx, cfg)
			slog.Info("database router enabled", "read_replicas", len(cfg.PostgresReadDSNs))
		}
	}

	if !pgConnected {
		// No read replicas or router failed — fall back to single pool
		if err := db.ConnectPostgres(ctx, cfg.PostgresDSN, cfg.PostgresMaxConn, cfg.PostgresMinConn); err != nil {
			// 依赖门禁：PostgreSQL 是必需依赖，不可达时拒绝启动（连接串由 scripts/init.py 生成）
			slog.Error("FATAL: PostgreSQL is required but unavailable — refusing to start (check POSTGRES_DSN)", "error", err)
			return
		}
		pgConnected = true
		defer db.ClosePostgres()
		// 幂等 seed 默认租户（不依赖迁移状态；缺失时注册会违反外键 23503）
		if err := db.EnsureDefaultTenant(ctx, db.Pool); err != nil {
			slog.Warn("ensure default tenant failed", "error", err)
		}
		// schema 版本校验（只读）：迁移由发布流程/DBA 执行，应用不再自行迁移
		verifySchemaVersion(ctx, cfg)
	}

	if !pgConnected {
		slog.Error("FATAL: no PostgreSQL connection was established — refusing to start (check POSTGRES_DSN)")
		return
	}

	// 幂等播种市场目录示例（技能/Agent/MCP；目录非空则跳过）
	// 该目录非空则跳过，避免重复播种。

	// 引导：连上数据库后，读取后台已持久化的基础设施/业务配置覆盖 cfg。
	// 使后续 Redis/存储/路由初始化使用 DB 值——支持仅靠 APP_SECRET 切换 Redis 集群等，重启生效。
	applyDBSettingsAfterConnect(ctx, cfg)

	// ── Redis ──
	// 产品决策(2026-08-22)：Redis 必需、无降级」已修订：Redis 不可用时降级运行
	// （内存限流、无会话热缓存/广播/审计流）。
	var atomicRedis *db.AtomicRedis
	redisCfg := db.RedisConfig{
		Mode:          cfg.RedisMode,
		Addr:          cfg.RedisAddr,
		Password:      cfg.RedisPassword,
		DB:            cfg.RedisDB,
		Addrs:         cfg.RedisAddrs,
		MasterName:    cfg.RedisMasterName,
		SentinelAddrs: cfg.RedisSentinelAddrs,
		PoolSize:      cfg.RedisPoolSize,
	}
	redisClient, redisErr := db.NewRedisClient(redisCfg)
	if redisErr != nil {
		// 依赖门禁：Redis 是必需依赖，未显式 DEGRADED_MODE=true 时直接拒绝启动——
		// 进程内降级会让多副本看到不同的限流/会话/事件（限流被按副本放大、run 锁退化为本地锁）。
		if !cfg.DegradedMode {
			slog.Error("FATAL: Redis is required but unavailable — refusing to start "+
				"(fix REDIS_ADDR/REDIS_PASSWORD, or set DEGRADED_MODE=true for single-instance development)",
				"error", redisErr)
			return
		}
		slog.Warn("Redis unavailable — degraded mode (no distributed rate limit / session cache / broadcast / audit stream)", "error", redisErr)
	} else {
		atomicRedis = db.NewAtomicRedis(redisClient)
		db.Redis = atomicRedis
		defer atomicRedis.Close()
		slog.Info("redis initialized", "mode", cfg.RedisMode)
	}

	// ── Audit Consumer: Redis Stream audit:events → PG audit_logs 批量落库 ──
	if db.Redis != nil {
		auditSink := db.NewDefaultAuditSink()
		defer auditSink.Close()
		go func() { _ = db.NewAuditConsumer(db.Redis, auditSink.Handle).Start(lifecycleCtx) }()
		slog.Info("audit consumer started", "stream", "audit:events")
	}
	// 启动审计中间件 worker（受 lifecycleCtx 控制，支持优雅关闭）
	// P1 修复：LoggingMiddleware 审计经 auditLogCh 投递，必须由 StartAuditLogWorker 消费；
	// 此前误调用了 ent_audit_middleware 的 StartAuditWorker（空转 worker，无人投递其 chan），
	// 导致所有中间件写请求审计被静默丢弃。
	api.StartAuditLogWorker(lifecycleCtx)

	// ── Monitor ──
	monitor.Init()

	// ── Auth: Initialize JWT authenticator ──
	// APP_SECRET 已在启动时校验（config.Load），JWT_SECRET 由其派生或显式注入。
	// 确保 JWT_SECRET 环境变量已设置（从 cfg.JWTSecret 派生）
	if cfg.JWTSecret != "" {
		os.Setenv("JWT_SECRET", cfg.JWTSecret)
	}
	auth.InitJWTAuth()
	if !config.ValidateJWTSecret(cfg.JWTSecret) {
		slog.Error("FATAL: JWT_SECRET is weak or not set. Generate a strong secret (32+ chars) and set JWT_SECRET env var")
		return
	}
	slog.Info("auth initialized", "jwt_secret_set", cfg.JWTSecret != "")

	// ── Rate Limiter: initialized per-router in GatewayRouter ──
	slog.Info("rate limiter configured", "default_rpm", cfg.RateLimitRPM)

	// ── Event Hub ──
	var eventHub *broadcast.Hub
	if db.Redis != nil {
		eventHub = broadcast.NewHub(db.Redis)
	} else {
		eventHub = broadcast.NewHub(nil)
	}
	defer eventHub.Close()

	// ── Python AI Engine Client ──
	var pythonClient *engine.PythonClient
	if cfg.PythonEngineAddress != "" {
		// Support comma-separated addresses for multi-instance deployment
		var addrs []string
		for _, a := range strings.Split(cfg.PythonEngineAddress, ",") {
			a = strings.TrimSpace(a)
			if a != "" {
				if !strings.HasPrefix(a, "http://") && !strings.HasPrefix(a, "https://") {
					a = "http://" + a
				}
				addrs = append(addrs, a)
			}
		}
		if len(addrs) > 0 {
			pythonClient = engine.NewPythonClient(addrs...)
			pythonClient.SetInternalToken(cfg.InternalToken)
			if cfg.InternalToken == "" {
				slog.Error("INTERNAL_TOKEN not set but python engine is configured — refusing to start (set INTERNAL_TOKEN env var or remove PYTHON_ENGINE_ADDRESS)")
				return
			}
			api.StartCronScheduler(lifecycleCtx, pythonClient)
			// 批 E1：引擎 Redis 动态发现——注册表非空则动态优先，为空/Redis 不可用回退静态地址
			if db.Redis != nil {
				engine.StartEngineDiscovery(lifecycleCtx, db.Redis, pythonClient)
			}
			slog.Info("python engine configured", "addresses", addrs)
		}
	} else {
		slog.Warn("no python engine address configured — agent/graph/skill will be unavailable")
	}

	// ── RPA Browser Hub ──
	// 传入 db.Redis 以启用跨网关副本桥接（批 D）;Redis 不可用时自动退回单机模式。
	rpaHub := api.NewRPAHub(db.Redis)

	// ── Storage / Session Manager / HTTP ──
	var sessionMgr *session.Manager
	var router http.Handler
	// ── Storage ──
	fileStore, err := storage.NewStore(cfg.StorageBackend, cfg.StorageRoot, cfg.S3Endpoint, cfg.S3Bucket, cfg.S3AccessKey, cfg.S3SecretKey, cfg.S3UseSSL)
	if err != nil {
		slog.Error("file store init", "error", err)
		return
	}
	atomicStore := storage.NewAtomicStore(fileStore)
	if cfg.StorageBackend == "local" {
		// 分片上传与媒体下载都经由存储后端寻址：多副本 + 本地盘会让"写分片/读分片"
		// 落到不同实例，上传损坏、媒体 404。多副本部署必须用共享卷或 STORAGE_BACKEND=s3。
		slog.Warn("storage backend is local — multi-replica deployments must mount a shared volume at this path, or set STORAGE_BACKEND=s3",
			"root", cfg.StorageRoot)
	}
	slog.Info("storage initialized", "backend", cfg.StorageBackend)

	// ── Session Manager ──
	sessionMgr = session.NewManager(db.Pool, db.Redis)
	slog.Info("session manager initialized")

	// ── Background Maintenance ──
	api.StartBlacklistCleaner(lifecycleCtx)
	// A7/C1: turns 保留策略清理（避免长期运行表膨胀；TURN_RETENTION_DAYS 可配）
	api.StartRetentionCleaner(lifecycleCtx)
	// P0-1: 启动JWT黑名单跨实例同步
	api.StartBlacklistPubSub(lifecycleCtx)
	// 跨实例 agent 取消广播订阅
	api.StartAgentCancelSubscriber(lifecycleCtx)
	// 子 Agent 事件中继：引擎把事件 pub 到 Redis，这里转投 SSE hub ——
	// 前端 /events 才可能**实时**看到后台子 Agent 的进度（否则只剩选中 run 时的轮询）。
	api.StartSubagentEventsRelay(lifecycleCtx, eventHub)

	// 会话地图布局：Redis 热层 → 异步落 PG（关机前做一次 flush，避免"拖过的位置"丢失）
	api.StartSessionMapFlusher(lifecycleCtx)

	// 会话地图：周期对账（删掉指向已删会话的节点）—— 事件驱动的失效兜不住"别的实例删的会话"
	api.StartSessionMapReconciler(lifecycleCtx)

	// P1-1: 启动数据库连接池自动调优（每5分钟检查一次）
	if db.GlobalDBManager != nil {
		db.GlobalDBManager.SetTuneInterval(5 * time.Minute)
		db.GlobalDBManager.StartAutoTuner(lifecycleCtx)
		slog.Info("database connection pool auto-tuner started")
	}

	router = api.NewGatewayRouter(lifecycleCtx, cfg, pythonClient, eventHub, sessionMgr, atomicStore, atomicRedis, rpaHub)

	// ── HTTP Server ──
	srv := &http.Server{
		Addr:              ":" + cfg.Port,
		Handler:           router,
		ReadTimeout:       cfg.ReadTimeout,
		ReadHeaderTimeout: 10 * time.Second,
		WriteTimeout:      cfg.WriteTimeout,
		IdleTimeout:       cfg.IdleTimeout,
	}

	// Graceful shutdown
	done := make(chan os.Signal, 1)
	signal.Notify(done, os.Interrupt, syscall.SIGTERM)

	go func() {
		slog.Info("server listening", "addr", srv.Addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			slog.Error("server error", "error", err)
			done <- syscall.SIGQUIT
			return
		}
	}()

	<-done
	slog.Info("shutting down...")

	// 注意：这里必须从 context.Background() 派生，**不能**用 lifecycleCtx。
	// lifecycleCtx 由 signal.NotifyContext 创建，收到 SIGTERM 时它已经被 cancel，
	// 以它为父会让 shutdownCtx 立即到期、srv.Shutdown 随即返回 —— 10s 宽限形同
	// 虚设，in-flight 请求（含正在流式输出的 SSE）会被直接切断。
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()

	if err := srv.Shutdown(shutdownCtx); err != nil {
		slog.Error("shutdown error", "error", err)
	}
	slog.Info("server stopped")
}

// applyDBSettingsAfterConnect 在完成首次数据库连接后，读取 system_settings 中
// 已持久化的基础设施/业务配置并覆盖 cfg，使后续 Redis/存储/路由初始化使用 DB 值。
// 依赖：db.Pool 已就绪，cfg.AppSecret 已校验。
// 作用范围：仅影响进程「后续初始化」使用的配置（Redis 集群、CORS、存储、S3、
// Agent、限流、支付）；DB 连接本身使用 env/默认引导串，切换数据库集群需重启。
func applyDBSettingsAfterConnect(ctx context.Context, cfg *config.Config) {
	if db.Pool == nil {
		return
	}
	store := settings.New(db.Pool, cfg.AppSecret)

	apply := func(category string, fn func(map[string]interface{})) {
		m, err := store.LoadConfig(ctx, category)
		if err != nil || len(m) == 0 {
			return
		}
		fn(m)
		slog.Info("applied settings from DB", "category", category)
	}

	apply("redis", func(m map[string]interface{}) {
		if v, ok := m["addr"].(string); ok && v != "" {
			cfg.RedisAddr = v
		}
		if v, ok := m["password"].(string); ok {
			cfg.RedisPassword = v
		}
		if v, ok := m["db"].(float64); ok {
			cfg.RedisDB = int(v)
		}
		if v, ok := m["mode"].(string); ok && v != "" {
			cfg.RedisMode = v
		}
	})

	apply("cors", func(m map[string]interface{}) {
		if v, ok := m["origins"].(string); ok && v != "" {
			cfg.CORSOrigins = v
		}
	})

	apply("storage", func(m map[string]interface{}) {
		if v, ok := m["backend"].(string); ok && v != "" {
			cfg.StorageBackend = v
		}
		if v, ok := m["root"].(string); ok && v != "" {
			cfg.StorageRoot = v
		}
	})

	apply("s3", func(m map[string]interface{}) {
		if v, ok := m["endpoint"].(string); ok && v != "" {
			cfg.S3Endpoint = v
		}
		if v, ok := m["bucket"].(string); ok && v != "" {
			cfg.S3Bucket = v
		}
		if v, ok := m["access_key"].(string); ok {
			cfg.S3AccessKey = v
		}
		if v, ok := m["secret_key"].(string); ok {
			cfg.S3SecretKey = v
		}
		if v, ok := m["use_ssl"].(bool); ok {
			cfg.S3UseSSL = v
		}
	})

	apply("agent", func(m map[string]interface{}) {
		if v, ok := m["max_turns"].(float64); ok && v > 0 {
			cfg.AgentMaxTurns = int(v)
		}
		if v, ok := m["max_tokens"].(float64); ok && v > 0 {
			cfg.AgentMaxTokens = int(v)
		}
		if v, ok := m["context_limit"].(float64); ok && v > 0 {
			cfg.AgentContextLimit = int(v)
		}
	})

	apply("rate_limit", func(m map[string]interface{}) {
		if v, ok := m["global"].(float64); ok && v > 0 {
			cfg.RateLimitGlobal = int(v)
		}
	})

	apply("payment", func(m map[string]interface{}) {
		if v, ok := m["public_base_url"].(string); ok && v != "" {
			cfg.PublicBaseURL = v
		}
		if v, ok := m["alipay_gateway"].(string); ok && v != "" {
			cfg.AlipayGateway = v
		}
	})
}

// verifySchemaVersion 只读校验数据库 schema 与代码期望的迁移 head 是否一致。
//
// 应用不再自行迁移（旧实现会 shell 出 python -m alembic 并写 .env，在无 python 的应用
// 镜像里必然静默失败）。迁移由发布流程/DBA 用 requirements-migrate.txt 的环境执行；
// 这里比对 migrations/versions 的 head 与数据库 alembic_version，不一致时默认拒绝启动，
// ALLOW_SCHEMA_DRIFT=true 可放行（迁移超前/回滚等场景）。
func verifySchemaVersion(ctx context.Context, cfg *config.Config) {
	expected, actual, match, err := db.CheckSchemaVersion(ctx)
	if err != nil {
		slog.Warn("schema version check unavailable", "error", err)
		return
	}
	if match {
		slog.Info("schema version verified", "migration", expected)
		// revision 一致 ≠ 表都在：手工删表后 revision 仍匹配，缺失会推迟到运行时
		// 才以 relation does not exist 暴露。补一道只读的存在性校验。
		verifyRequiredTables(ctx, cfg)
		return
	}
	slog.Error("database schema does not match this build",
		"expected_migration", expected, "database_migration", actual,
		"hint", "run `alembic upgrade head` (see requirements-migrate.txt) before starting")
	if cfg.AllowSchemaDrift {
		slog.Warn("ALLOW_SCHEMA_DRIFT=true — continuing despite schema drift")
		return
	}
	slog.Error("FATAL: refusing to start on mismatched schema; set ALLOW_SCHEMA_DRIFT=true to bypass")
	os.Exit(1)
}

// verifyRequiredTables 只读校验网关必需的表是否存在（清单见 db.RequiredTables）。
//
// 与 revision 校验互补：版本对上但表缺了（手工删表、迁移在别处被回滚过），这里能提前发现。
func verifyRequiredTables(ctx context.Context, cfg *config.Config) {
	missing, err := db.CheckMissingTables(ctx)
	if err != nil {
		slog.Warn("required-table check unavailable", "error", err)
		return
	}
	if len(missing) == 0 {
		slog.Info("required tables verified", "count", len(db.RequiredTables))
		return
	}
	slog.Error("database is missing required tables",
		"missing", strings.Join(missing, ", "),
		"hint", "run `alembic upgrade head` (see requirements-migrate.txt) before starting")
	if cfg.AllowSchemaDrift {
		slog.Warn("ALLOW_SCHEMA_DRIFT=true — continuing despite missing tables")
		return
	}
	slog.Error("FATAL: refusing to start on missing tables; set ALLOW_SCHEMA_DRIFT=true to bypass")
	os.Exit(1)
}

func parseLogLevel(level string) slog.Level {
	switch level {
	case "debug":
		return slog.LevelDebug
	case "warn":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}
