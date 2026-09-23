package api

import (
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"sync/atomic"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/monitor"
	"github.com/athenavi/chiron/internal/settings"
)

// ── Queue Stats ──

// webhookStreamName 是网关侧真实存在的 Redis Stream（生产者见 ent_webhook_handler.go，
// 消费者为 WebhookDispatcher）。这是仓库中唯一有真实生产/消费语义的队列实体。
const webhookStreamName = "webhook:events"

// QueueStats 描述该事件队列的真实状态。
// 历史实现读取的 queue:tasks:length / queue:vip:length 两个计数键
// 在全仓库没有任何写入方，因此恒为 0 —— 现改由 Redis Stream 命令直接观测。
type QueueStats struct {
	StreamKey       string      `json:"stream_key"`
	TaskQueueLength int64       `json:"task_queue_length"` // Stream 消息总数（积压）
	VIPQueueLength  int64       `json:"vip_queue_length"`  // 已投递未 ACK（XPENDING）
	Groups          int         `json:"groups"`            // 消费者组数
	Consumers       int         `json:"consumers"`         // 消费者总数
	Lag             int64       `json:"lag"`               // 未投递积压
	ThroughputQPS   float64     `json:"throughput_qps"`    // 网关请求吞吐（差值采样）
	ActiveRequests  int64       `json:"active_requests"`   // 进行中的请求数
	WaitingTasks    []QueueTask `json:"waiting_tasks"`
}

type QueueTask struct {
	TaskID   string `json:"task_id"`
	UserID   string `json:"user_id"`
	Content  string `json:"content"`
	QueuedAt string `json:"queued_at"`
	Position int    `json:"position"`
	IsVIP    bool   `json:"is_vip"`
}

func (h *AdminHandler) GetQueueStats(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	key := db.RedisKey(webhookStreamName)
	stats := QueueStats{
		StreamKey:    webhookStreamName,
		WaitingTasks: []QueueTask{},
	}

	// 事件队列：Redis Stream 的真实积压
	stats.TaskQueueLength = streamLen(ctx, key)
	if g := readStreamGroups(ctx, key); g.Groups > 0 {
		stats.Groups = g.Groups
		stats.Consumers = g.Consumers
		stats.VIPQueueLength = g.Pending
		stats.Lag = g.Lag
	}
	stats.WaitingTasks = recentStreamTasks(ctx, key, 20)

	// 吞吐与并发：网关请求计数器的真实值
	snap := monitor.Snapshot()
	stats.ActiveRequests = snapInt64(snap["requests_active"])
	stats.ThroughputQPS = requestsPerSecond(snapInt64(snap["requests_total"]))

	OK(w, stats)
}

var queuePaused atomic.Bool

func (h *AdminHandler) FlushQueue(w http.ResponseWriter, r *http.Request) {
	if db.Redis == nil {
		InternalError(w, "redis not available")
		return
	}

	ctx := r.Context()
	// 通过设置标志通知 worker 清空队列（统一键前缀）
	db.Redis.Set(ctx, db.RedisKey("queue:flush"), "1", 10*time.Second)

	OK(w, map[string]string{"status": "flush_requested"})
}

func (h *AdminHandler) PauseQueue(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Pause bool `json:"pause"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, "invalid request")
		return
	}

	queuePaused.Store(body.Pause)

	// 通过 Redis 通知所有 worker（统一键前缀）
	if db.Redis != nil {
		ctx := r.Context()
		if body.Pause {
			db.Redis.Set(ctx, db.RedisKey("queue:paused"), "1", 0)
		} else {
			db.Redis.Del(ctx, db.RedisKey("queue:paused"))
		}
	}

	OK(w, map[string]interface{}{"paused": body.Pause})
}

// ── Cache Stats ──

// CacheStats 描述真实可得的缓存统计：
//   - 引擎侧多级缓存（L1/L2/L3）命中次数与总命中率，来源 python-engine「GET /info」
//     的 gateway.cache（见 gateway/cache.py stats()）；
//   - Redis 缓存层的 keyspace 命中率与内存占用，来源 Redis INFO。
//
// 历史实现读取的 cache:stats:hits / cache:stats:misses 两个键在全仓库没有任何写入方
// （且未走 db.RedisKey 前缀，多环境共享 Redis 时必然读不到），因此恒为 0。
type CacheStats struct {
	TotalHitRate  float64 `json:"total_hit_rate"` // 引擎侧总命中率（%）
	TotalRequests int64   `json:"total_requests"`
	TotalHits     int64   `json:"total_hits"`
	TotalMisses   int64   `json:"total_misses"`
	L1Hits        int64   `json:"l1_hits"`
	L2Hits        int64   `json:"l2_hits"`
	L3Hits        int64   `json:"l3_hits"`

	RedisHitRate        float64 `json:"redis_hit_rate"` // Redis keyspace 命中率（%）
	RedisKeyspaceHits   int64   `json:"redis_keyspace_hits"`
	RedisKeyspaceMisses int64   `json:"redis_keyspace_misses"`
	RedisMemoryMB       float64 `json:"redis_memory_mb"`
	RedisMaxMemoryMB    float64 `json:"redis_max_memory_mb"`

	HotQueries []HotQuery `json:"hot_queries"`
}

type HotQuery struct {
	Query        string  `json:"query"`
	Hits         int     `json:"hits"`
	HitRate      float64 `json:"hit_rate"`
	AvgLatencyMs float64 `json:"avg_latency_ms"`
}

func (h *AdminHandler) GetCacheStats(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	stats := CacheStats{HotQueries: []HotQuery{}}

	// ── 引擎侧多级缓存（真实来源：python-engine /info → gateway.cache）──
	if info := h.fetchPyEngineInfo(ctx); info != nil && info.Gateway.Cache != nil {
		c := info.Gateway.Cache
		stats.L1Hits, stats.L2Hits, stats.L3Hits = c.L1Hits, c.L2Hits, c.L3Hits
		stats.TotalMisses = c.Misses
		stats.TotalHits = c.L1Hits + c.L2Hits + c.L3Hits
		stats.TotalRequests = stats.TotalHits + c.Misses
		// 引擎 hit_rate 为 0-1 小数，对外统一为百分比
		stats.TotalHitRate = round2(c.HitRate * 100)
	}

	// ── Redis 缓存层（真实 INFO 计数器）──
	if s := redisInfoSection(ctx, "stats"); s != nil {
		stats.RedisKeyspaceHits = infoInt64(s, "keyspace_hits")
		stats.RedisKeyspaceMisses = infoInt64(s, "keyspace_misses")
		if total := stats.RedisKeyspaceHits + stats.RedisKeyspaceMisses; total > 0 {
			stats.RedisHitRate = round2(float64(stats.RedisKeyspaceHits) / float64(total) * 100)
		}
	}
	if m := redisInfoSection(ctx, "memory"); m != nil {
		stats.RedisMemoryMB = round2(float64(infoInt64(m, "used_memory")) / (1024 * 1024))
		if max := infoInt64(m, "maxmemory"); max > 0 {
			stats.RedisMaxMemoryMB = round2(float64(max) / (1024 * 1024))
		}
	}

	OK(w, stats)
}

// ── Performance Stats ──

type PerformanceStats struct {
	Gateway GatewayStats `json:"gateway"`
	Python  PythonStats  `json:"python_engine"`
}

type GatewayStats struct {
	Instances      int     `json:"instances"`
	CPUPercent     float64 `json:"cpu_percent"`
	MemoryMB       float64 `json:"memory_mb"`
	Goroutines     int     `json:"goroutines"`
	Connections    int     `json:"connections"`
	RedisLatencyMs float64 `json:"redis_latency_ms"`
	DBLatencyMs    float64 `json:"db_latency_ms"`
	UptimeSeconds  int64   `json:"uptime_seconds"`
	Version        string  `json:"version"`
}

type PythonStats struct {
	Pods           int     `json:"pods"`
	CPUPercent     float64 `json:"cpu_percent"`
	MemoryMB       float64 `json:"memory_mb"`
	ActiveTasks    int     `json:"active_tasks"`
	AvgInferenceMs float64 `json:"avg_inference_ms"`
	RedisLatencyMs float64 `json:"redis_latency_ms"`
	UptimeSeconds  int64   `json:"uptime_seconds"`
	Version        string  `json:"version"`
}

// GetPerformance GET /v1/admin/performance
// 网关侧全部取自 monitor.Snapshot（真实计数器 + Go runtime）、runtime/metrics 的 CPU 占用
// 与 Redis/DB 的真实 PING 往返；Python 引擎侧取自引擎「GET /info」。
// 采集不到的字段保持零值，不伪造。
func (h *AdminHandler) GetPerformance(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	snap := monitor.Snapshot()

	stats := PerformanceStats{
		Gateway: GatewayStats{
			// 进程内指标只覆盖当前副本：多副本部署时各副本各自上报
			Instances: 1,
			Version:   Version,
			// 并发连接数 = 进行中的请求数（由请求中间件维护）
			Connections: int(snapInt64(snap["requests_active"])),
			// 修复：monitor.Snapshot 的键是 go_goroutines，此前误取 "goroutines" 恒为 0
			Goroutines: int(snapInt64(snap["go_goroutines"])),
			// Go 堆分配内存
			MemoryMB:      round2(float64(snapInt64(snap["go_memory_alloc_bytes"])) / (1024 * 1024)),
			CPUPercent:    processCPUPercent(),
			UptimeSeconds: int64(snapFloat64(snap["uptime_seconds"])),
		},
	}

	// Redis 往返延迟（真实 PING）
	if db.Redis != nil {
		start := time.Now()
		if err := db.Redis.Ping(ctx); err == nil {
			stats.Gateway.RedisLatencyMs = round2(float64(time.Since(start).Microseconds()) / 1000)
		}
	}

	// DB 往返延迟（真实 PING）
	start := time.Now()
	if err := db.GlobalDBManager.Ping(ctx); err == nil {
		stats.Gateway.DBLatencyMs = round2(float64(time.Since(start).Microseconds()) / 1000)
	}

	// ── Python 引擎（真实来源：已配置实例列表 + 引擎 /info）──
	if h.pythonClient != nil {
		stats.Python.Pods = len(h.pythonClient.StaticAddresses())
		// 引擎与网关共用同一 Redis，延迟测量复用网关结果
		stats.Python.RedisLatencyMs = stats.Gateway.RedisLatencyMs
		if info := h.fetchPyEngineInfo(ctx); info != nil {
			stats.Python.Version = info.Version
			stats.Python.UptimeSeconds = info.UptimeSeconds
			stats.Python.MemoryMB = round2(info.MemoryMB)
			stats.Python.CPUPercent = round2(info.CPUPercent)
			stats.Python.ActiveTasks = info.ActiveTasks
			// 平均推理延迟：引擎路由器中已采样 provider 延迟的均值
			var sum float64
			var n int
			for _, p := range info.Gateway.Providers {
				if p.AvgLatencyMs > 0 {
					sum += p.AvgLatencyMs
					n++
				}
			}
			if n > 0 {
				stats.Python.AvgInferenceMs = round2(sum / float64(n))
			}
		}
	}

	OK(w, stats)
}

// ── API Keys ──

type ApiKey struct {
	ID         string `json:"id"`
	Provider   string `json:"provider"`
	KeyPreview string `json:"key_preview"`
	Status     string `json:"status"`
	Weight     int    `json:"weight"`
	Failures   int    `json:"failures"`
	LastUsed   string `json:"last_used"`
	Remark     string `json:"remark"`
}

func (h *AdminHandler) ListApiKeys(w http.ResponseWriter, r *http.Request) {
	if h.pythonClient == nil {
		OK(w, map[string]interface{}{"keys": []ApiKey{}, "stats": map[string]interface{}{"total": 0, "active": 0, "rate_limited": 0, "circuit_open": 0}})
		return
	}
	h.pythonClient.ForwardRequest(w, r, "/v1/admin/api-keys")
}

func (h *AdminHandler) AddApiKey(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Provider string `json:"provider"`
		Key      string `json:"key"`
		Remark   string `json:"remark"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, "invalid request")
		return
	}

	if body.Provider == "" || body.Key == "" {
		BadRequest(w, "provider and key are required")
		return
	}

	if h.pythonClient == nil {
		InternalError(w, "python engine not available")
		return
	}
	var resp interface{}
	if err := h.pythonClient.PostJSON(r.Context(), "/v1/admin/api-keys", body, &resp); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "python engine error")
		return
	}
	OK(w, resp)
}

func (h *AdminHandler) UpdateApiKey(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "" {
		BadRequest(w, "id is required")
		return
	}

	if h.pythonClient == nil {
		InternalError(w, "python engine not available")
		return
	}
	h.pythonClient.ForwardRequest(w, r, "/v1/admin/api-keys/"+id)
}

func (h *AdminHandler) DeleteApiKey(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "" {
		BadRequest(w, "id is required")
		return
	}

	if h.pythonClient == nil {
		InternalError(w, "python engine not available")
		return
	}
	h.pythonClient.ForwardRequest(w, r, "/v1/admin/api-keys/"+id)
}

// ── Settings ──

// settingsCategories 后台「系统设置」允许的配置分组。
// 敏感键（password/secret/api_key/dsn/token 等）由 settings.Store 用 APP_SECRET
// 派生密钥加密落库；非敏感配置明文存储。上级配置经 DB 持久化，env 作为默认值。
var settingsCategories = []string{
	"rate_limit", "degradation", "cache", "api_key",
	"agent", "llm", "storage", "payment", "redis", "postgres", "cors", "s3",
	"python",
}

func validSettingsCategory(c string) bool {
	for _, v := range settingsCategories {
		if v == c {
			return true
		}
	}
	return false
}

var settingsCategoryList = strings.Join(settingsCategories, ", ")

// intFromValue 从 JSON 解码出的值安全取整数，非数值/越界时返回 fallback。
func intFromValue(v interface{}, fallback int) int {
	switch n := v.(type) {
	case float64:
		i := int(n)
		if i > 0 {
			return i
		}
	case int:
		if n > 0 {
			return n
		}
	case string:
		var i int
		if _, err := fmt.Sscanf(n, "%d", &i); err == nil && i > 0 {
			return i
		}
	}
	return fallback
}

// SaveSettings PUT /v1/admin/settings
// 将某分组配置按 key 逐条 upsert 到 system_settings 表。
// config 中 value 为 null 的 key 会被删除，使该配置回落到 env 默认值。
func (h *AdminHandler) SaveSettings(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Category string                 `json:"category"`
		Config   map[string]interface{} `json:"config"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.Category == "" {
		BadRequest(w, "category is required: "+settingsCategoryList)
		return
	}
	if body.Config == nil {
		BadRequest(w, "config is required")
		return
	}
	if !validSettingsCategory(body.Category) {
		BadRequest(w, "invalid category: must be one of "+settingsCategoryList)
		return
	}

	ctx := r.Context()
	s := h.ensureSettingsStore()
	if s == nil {
		InternalError(w, "settings store unavailable")
		return
	}

	// 当前操作用户（可空，用于审计 updated_by）
	userID := ""
	if claims := auth.GetClaims(r.Context()); claims != nil {
		userID = claims.ID
	}

	if err := s.SaveConfig(ctx, body.Category, body.Config, userID); err != nil {
		slog.Error("save settings failed", "category", body.Category, "error", err)
		if err == settings.ErrEncryptedKeyNotFound {
			InternalError(w, "APP_SECRET not configured; cannot encrypt sensitive settings")
			return
		}
		InternalError(w, "failed to save settings")
		return
	}

	// rate_limit 分组：热更新分布式限流阈值（保存成功后生效）
	if body.Category == "rate_limit" && h.rateLimiter != nil {
		global := intFromValue(body.Config["global"], 0)
		tenant := intFromValue(body.Config["tenant"], 0)
		user := intFromValue(body.Config["user"], 0)
		if global > 0 || tenant > 0 || user > 0 {
			h.rateLimiter.Configure(global, tenant, user)
			slog.Info("rate limiter hot-reloaded", "global", global, "tenant", tenant, "user", user)
		}
	}

	// redirect：保存 redis 配置后热换 Redis 连接
	if body.Category == "redis" {
		h.hotReloadRedis(body.Config)
	}

	// cors：本实例立即生效（跨副本由下方广播的订阅者同步）
	if body.Category == "cors" {
		if v, ok := body.Config["origins"].(string); ok {
			SetCORSAllowOrigin(v)
			slog.Info("cors allowlist hot-reloaded", "origins", v)
		}
	}

	// payment：保存支付凭据后从 DB 重载并热重建渠道客户端（与专用接口 PUT /v1/admin/payments 同一路径）。
	// 通用接口不做保存前校验，非法凭据只会让对应渠道不可用并记日志 —— 见 buildPaymentClients 说明。
	if body.Category == settingsCategoryPayment && h.billingHandler != nil {
		h.billingHandler.ReloadPaymentConfig(ctx, s)
	}

	// 跨副本广播（批 B-2′）：rate_limit / cors / payment 由各副本订阅者即时热更；
	// 其余分类（redis/storage/s3/agent）在副本侧告警提示滚动重启。
	if err := PublishSettingsChanged(ctx, body.Category, body.Config); err != nil {
		slog.Warn("publish settings changed failed", "category", body.Category, "error", err)
	}

	slog.Info("settings saved", "category", body.Category, "keys", len(body.Config))
	OK(w, map[string]interface{}{"status": "saved", "category": body.Category})
}

// ensureSettingsStore 惰性初始化 DB 加密设置存储。
func (h *AdminHandler) ensureSettingsStore() *settings.Store {
	if h.settingsStore == nil && db.Pool != nil {
		h.settingsStore = settings.New(db.Pool, h.appSecret)
	}
	return h.settingsStore
}

// hotReloadRedis 在保存 redis 分组设置后热换 Redis 连接（AtomicRedis.Swap）。
// 仅当配置了新地址才执行；失败仅记日志不影响保存结果。
// P0 安全修复：旧连接显式关闭，防止连接泄漏。
func (h *AdminHandler) hotReloadRedis(cfg map[string]interface{}) {
	if h.redis == nil {
		return
	}
	addr := strFromValue(cfg["addr"])
	if addr == "" {
		return
	}
	rc := db.RedisConfig{
		Mode:     "single",
		Addr:     addr,
		Password: strFromValue(cfg["password"]),
		DB:       intFromValue(cfg["db"], 0),
		PoolSize: intFromValue(cfg["pool_size"], 50),
	}
	client, err := db.NewRedisClient(rc)
	if err != nil {
		slog.Error("redis hot-swap failed", "addr", addr, "error", err)
		return
	}
	// 关闭旧连接，释放资源
	oldClient := h.redis.LoadRaw()
	h.redis.Swap(client)
	if oldClient != nil {
		if err := oldClient.Close(); err != nil {
			slog.Warn("failed to close old redis connection", "error", err)
		}
	}
	slog.Info("redis hot-swapped", "addr", addr)
}

// strFromValue 从 JSON 解码出的值安全取字符串；非字符串返回 ""。
func strFromValue(v interface{}) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

// GetSettings GET /v1/admin/settings?category=... 读取某分组已持久化配置。
// 无记录时返回空 config（前端保留默认值）。
func (h *AdminHandler) GetSettings(w http.ResponseWriter, r *http.Request) {
	category := r.URL.Query().Get("category")
	if category == "" {
		BadRequest(w, "category is required: "+settingsCategoryList)
		return
	}
	if !validSettingsCategory(category) {
		BadRequest(w, "invalid category: must be one of "+settingsCategoryList)
		return
	}
	s := h.ensureSettingsStore()
	if s == nil {
		OK(w, map[string]interface{}{"category": category, "config": map[string]interface{}{}})
		return
	}
	config, err := s.LoadConfig(r.Context(), category)
	if err != nil {
		OK(w, map[string]interface{}{"category": category, "config": map[string]interface{}{}})
		return
	}
	OK(w, map[string]interface{}{"category": category, "config": config})
}
