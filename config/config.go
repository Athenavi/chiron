package config

import (
	"bufio"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	AppSecret string

	// Server
	Port         string
	ReadTimeout  time.Duration
	WriteTimeout time.Duration
	IdleTimeout  time.Duration

	// Database
	PostgresDSN      string
	PostgresMaxConn  int
	PostgresMinConn  int
	PostgresReadDSNs []string // read-replica DSNs (comma-separated)

	// Redis
	RedisMode          string // "single", "cluster", "sentinel"
	RedisAddr          string
	RedisPassword      string
	RedisDB            int
	RedisAddrs         []string // for cluster mode
	RedisMasterName    string   // for sentinel mode
	RedisSentinelAddrs []string // for sentinel mode
	RedisPoolSize      int

	// DegradedMode 显式允许依赖降级（仅单机开发用）。默认 false：
	// 生产模式下 Redis 不可用直接拒绝启动——进程内降级会让副本看到不同的
	// 会话/限流/事件（限流按副本放大、run 锁退化为本地锁），破坏一致性。
	DegradedMode bool

	// AllowSchemaDrift 允许"数据库 schema 版本与代码期望不一致"时仍启动。
	// 默认 false：迁移由发布流程/DBA 执行（见 requirements-migrate.txt），
	// 启动只做只读校验，不一致即拒绝启动，避免"代码已升级、迁移未跑"的静默漂移。
	AllowSchemaDrift bool

	// Auth
	JWTSecret     string
	JWTExpiration time.Duration
	InternalToken string

	// Registration
	DisableRegistration bool

	// EmailBloom 是注册流程里"邮箱是否已存在"判重的布隆过滤器（按租户分片）。
	// 它只做**快速否定**：命中"一定不存在"才跳过 DB 判重，真相源始终是
	// users(tenant_id, email) 唯一索引。默认开启 —— 它是纯性能优化，
	// 任何漏记都只会退化为回落 DB，不会放进重复邮箱。
	EmailBloomEnabled  bool
	EmailBloomCapacity uint64  // 每个租户分片的默认预期容量
	EmailBloomFPRate   float64 // 目标误报率

	// NotificationRetentionDays 站内通知的保留天数（默认 90）。
	//
	// 通知表只增不减：四类事件里 `quota_changed` 会按租户 owner/admin 数量
	// 成倍写入。90 天够覆盖"上季度那笔扣费怎么回事"这类对账追溯，又不会让表
	// 无限膨胀。设为 <= 0 时回落默认值（**不会**变成"立即删光"）。
	NotificationRetentionDays int

	// Cookie
	CookieSecure bool

	// CORS
	CORSOrigins string

	// Storage
	StorageBackend string // "local" or "s3"
	StorageRoot    string // local root path
	S3Endpoint     string
	S3Bucket       string
	S3AccessKey    string
	S3SecretKey    string
	S3UseSSL       bool // S3/MinIO use SSL

	// Rate Limit
	RateLimitRPM       int
	RateLimitFailClose bool
	RateLimitGlobal    int // global requests per minute
	// 注：三级令牌桶（global/tenant/user）均为"每分钟总量配额"，由 Redis 原子计数
	// 承载，与网关副本数无关 —— 历史上曾按副本数线性放大的 RATE_LIMIT_INSTANCES
	// 已移除（扩缩容无需调参，见 docs/deployment-multi-instance.md 第 5 节）。

	// TrustedProxyCIDRs trusted reverse-proxy CIDRs (comma separated).
	// X-Forwarded-For / X-Real-IP are only honored when the direct peer
	// matches one of these CIDRs; otherwise clients could spoof IP-based limits.
	TrustedProxyCIDRs []string

	// MetricsToken shared bearer token for Prometheus to scrape /metrics.
	// When empty, /metrics still requires JWT admin permission.
	MetricsToken string

	// Log
	LogLevel string // debug / info / warn / error

	PublicBaseURL         string
	FrontendURL           string
	AlipayAppID           string
	AlipayPrivateKey      string
	AlipayPublicKey       string
	AlipayGateway         string
	WechatMchID           string
	WechatAppID           string
	WechatAPIv3Key        string
	WechatMchCertSerialNo string
	WechatMchPrivateKey   string

	// Agent behavior
	//
	// ⚠ 这三个字段的命名在 M20 做过一次**破坏性改名**，起因是"同名不同义"：
	//   * `AGENT_MAX_TOKENS` 在网关是"单次调用的 max output tokens"，在**引擎**却是
	//     "一个 run 全程的 token 预算"（默认 400k）。共享 env 的部署按网关口径设 8192，
	//     引擎几乎每个 run 都以 `budget_exceeded:tokens` 收口，两侧还都没有校验；
	//   * `AGENT_MAX_TURNS` 同样是"网关的默认提交回合数"与"引擎的 run 回合上限"两回事。
	// 现在：单次/默认一类的裸名归网关，run 级的归引擎并带 `RUN_`（见 .env.example）。
	// 旧名一律**不再读取**，启动时由 `ValidateRenamedAgentEnv` 直接拒绝（失败要显式）。
	//
	// AgentDefaultMaxTurns 每次提交（一条 SSE 回合）的默认回合数；请求里的
	// `llm_config.max_turns` 优先（前端与 Agent 档案都走这条路）。
	AgentDefaultMaxTurns int
	// AgentMaxOutputTokens 单次 LLM 调用的 max output tokens。它不是 run 预算：
	// run 全程的 token 上限在**引擎**侧（`AGENT_RUN_MAX_TOKENS`，默认 400k）。
	AgentMaxOutputTokens int
	AgentMaxConcurrency   int // max concurrent agent runs (default 20)
	// AgentSubmitTimeout 单次提交（一条 SSE 回合）在网关侧的后台执行上限。
	// 必须 >= api.DefaultAgentTimeout（300s）：曾经硬编码 180s，比它短，
	// 长回合（多轮工具调用）必然被提前取消，表现为"思考/工具调用做一半就断"。
	AgentSubmitTimeout time.Duration

	// Python AI 引擎
	PythonEngineAddress string // HTTP 地址，如 "localhost:8000"
	PythonEngineTimeout time.Duration

	// Temporal / LLMGateway 为遗留配置（未使用），已移除

	// PayPal
	PayPalClientID string
	PayPalSecret   string
	PayPalSandbox  bool

	// Plugins
	PluginsConfigPath string // path to plugins.json (MCP server config)
	PluginDataDir     string // per-user plugin config root: {PluginDataDir}/{user_id}/plugins.json

	// DataDir is the runtime data directory (backups, skills, media, etc.)
	DataDir string
}

func Load() (*Config, error) {
	cfg := loadConfig()

	// APP_SECRET is required（部署级主密钥）。
	if !cfg.ValidateAppSecret() {
		return nil, errors.New("APP_SECRET environment variable must be set to a strong, unique value (32+ chars)")
	}

	if cfg.JWTSecret == "" {
		cfg.JWTSecret = deriveSubsecret(cfg.AppSecret, "chiron-jwt")
	}
	if cfg.InternalToken == "" {
		cfg.InternalToken = deriveSubsecret(cfg.AppSecret, "chiron-internal")
	}

	// JWT_SECRET is required (derived or explicit).
	if !ValidateJWTSecret(cfg.JWTSecret) {
		return nil, errors.New("JWT_SECRET (or its source APP_SECRET) must be set to a strong, unique value")
	}

	return cfg, nil
}

func LoadAllowUnconfigured() *Config {
	cfg := loadConfig()

	if cfg.JWTSecret == "" {
		cfg.JWTSecret = deriveSubsecret(cfg.AppSecret, "chiron-jwt")
	}
	if cfg.InternalToken == "" {
		cfg.InternalToken = deriveSubsecret(cfg.AppSecret, "chiron-internal")
	}
	return cfg
}

func loadConfig() *Config {
	loadDotEnv()     // .env file overrides config file
	loadConfigFile() // JSON config file (lowest priority)
	cfg := &Config{
		AppSecret:           getSecret("APP_SECRET", ""),
		Port:                getEnv("PORT", "8080"),
		ReadTimeout:         getDuration("READ_TIMEOUT", 10*time.Second),
		WriteTimeout:        getDuration("WRITE_TIMEOUT", 60*time.Second),
		IdleTimeout:         getDuration("IDLE_TIMEOUT", 120*time.Second),
		PostgresDSN:         getEnv("POSTGRES_DSN", ""),
		PostgresMaxConn:     getIntAliases(20, "POSTGRES_MAX_CONN", "POSTGRES_MAX_CONNS"),
		PostgresMinConn:     getInt("POSTGRES_MIN_CONN", 2),
		PostgresReadDSNs:    getStringSlice("POSTGRES_READ_DSNS", []string{}),
		RedisMode:           getEnv("REDIS_MODE", "single"),
		RedisAddr:           getEnv("REDIS_ADDR", "localhost:6379"),
		RedisPassword:       getEnv("REDIS_PASSWORD", ""),
		RedisDB:             getInt("REDIS_DB", 0),
		RedisAddrs:          getStringSlice("REDIS_ADDRS", []string{}),
		RedisMasterName:     getEnv("REDIS_MASTER_NAME", ""),
		RedisSentinelAddrs:  getStringSlice("REDIS_SENTINEL_ADDRS", []string{}),
		RedisPoolSize:       getInt("REDIS_POOL_SIZE", 100),
		JWTSecret:           getSecret("JWT_SECRET", ""),
		JWTExpiration:       getDuration("JWT_EXPIRATION", 24*time.Hour),
		InternalToken:       getSecret("INTERNAL_TOKEN", ""),
		DisableRegistration: isTruthy(getEnv("DISABLE_REGISTRATION", "")),
		EmailBloomEnabled:   isTruthy(getEnv("EMAIL_BLOOM_FILTER_ENABLED", "true")),
		EmailBloomCapacity:  uint64(getInt("EMAIL_BLOOM_CAPACITY", 100000)),
		NotificationRetentionDays: getInt("NOTIFICATION_RETENTION_DAYS", 90),
		EmailBloomFPRate:    getFloat("EMAIL_BLOOM_FP_RATE", 0.001),
		// Cookie Secure：显式配置优先；未配置时**按部署协议推导**（见 cookieSecure()）。
		CookieSecure: cookieSecure(),
		CORSOrigins:         getEnv("CORS_ORIGINS", ""),
		StorageBackend:      getEnv("STORAGE_BACKEND", "local"),
		StorageRoot:         getEnv("STORAGE_ROOT", filepath.Join(GetDefaultDataDir(), "workspace")),
		S3Endpoint:          getEnv("S3_ENDPOINT", ""),
		S3Bucket:            getEnv("S3_BUCKET", "chiron"),
		S3AccessKey:         getSecret("S3_ACCESS_KEY", ""),
		S3SecretKey:         getSecret("S3_SECRET_KEY", ""),
		S3UseSSL:            isTruthy(getEnv("S3_USE_SSL", "")),
		RateLimitRPM:        getInt("RATE_LIMIT_RPM", 100),
		RateLimitFailClose:  isTruthy(getEnv("RATE_LIMIT_FAIL_CLOSE", "")),
		RateLimitGlobal:     getInt("RATE_LIMIT_GLOBAL", 10000),
		TrustedProxyCIDRs:   getStringSlice("TRUSTED_PROXY_CIDRS", []string{}),
		MetricsToken:        getSecret("METRICS_TOKEN", ""),
		DegradedMode:        isTruthy(getEnv("DEGRADED_MODE", "")),
		AllowSchemaDrift:    isTruthy(getEnv("ALLOW_SCHEMA_DRIFT", "")),
		LogLevel:            getEnv("LOG_LEVEL", "info"),

		// 支付（支付宝/微信）
		PublicBaseURL:         getEnv("PUBLIC_BASE_URL", ""),
		FrontendURL:           getEnv("FRONTEND_URL", ""),
		AlipayAppID:           getEnv("ALIPAY_APP_ID", ""),
		AlipayPrivateKey:      getEnv("ALIPAY_PRIVATE_KEY", ""),
		AlipayPublicKey:       getEnv("ALIPAY_PUBLIC_KEY", ""),
		AlipayGateway:         getEnv("ALIPAY_GATEWAY", ""),
		WechatMchID:           getEnv("WXPAY_MCH_ID", ""),
		WechatAppID:           getEnv("WXPAY_APP_ID", ""),
		WechatAPIv3Key:        getEnv("WXPAY_API_V3_KEY", ""),
		WechatMchCertSerialNo: getEnv("WXPAY_MCH_CERT_SERIAL_NO", ""),
		WechatMchPrivateKey:   getEnv("WXPAY_MCH_PRIVATE_KEY", ""),
		AgentDefaultMaxTurns:  getInt("AGENT_DEFAULT_MAX_TURNS", 5),
		AgentMaxOutputTokens:  getInt("AGENT_MAX_OUTPUT_TOKENS", 8192),
		AgentMaxConcurrency:   getInt("AGENT_MAX_CONCURRENCY", 20),
		// 默认 5 分钟，与 api.DefaultAgentTimeout 对齐（env AGENT_SUBMIT_TIMEOUT 可调大）
		AgentSubmitTimeout: getDuration("AGENT_SUBMIT_TIMEOUT", 5*time.Minute),

		PythonEngineAddress: getEnv("PYTHON_ENGINE_ADDRESS", "localhost:8000"),
		PythonEngineTimeout: getDuration("PYTHON_ENGINE_TIMEOUT", 5*time.Minute),

		PayPalClientID: getEnv("PAYPAL_CLIENT_ID", ""),
		PayPalSecret:   getEnv("PAYPAL_SECRET", ""),
		PayPalSandbox:  isTruthy(getEnv("PAYPAL_SANDBOX", "")),

		PluginsConfigPath: getEnv("PLUGINS_CONFIG_PATH", filepath.Join(GetDefaultDataDir(), "config", "plugins.json")),
		PluginDataDir:     getEnv("PLUGIN_DATA_DIR", filepath.Join(GetDefaultDataDir(), "plugins")),
		DataDir:           getEnv("CHIRON_DATA_DIR", GetDefaultDataDir()),
	}

	return cfg
}

// GetDefaultDataDir returns the default data directory based on environment:
// - CHIRON_DATA_DIR env var (highest priority, set by caller)
// - ~/.chiron (local development)
// - data (fallback)
// This is the canonical implementation; do not duplicate elsewhere.
func GetDefaultDataDir() string {
	if v := os.Getenv("CHIRON_DATA_DIR"); v != "" {
		return v
	}
	// Try to detect production vs development
	home, err := os.UserHomeDir()
	if err == nil {
		return filepath.Join(home, ".chiron")
	}
	return "data"
}

func (c *Config) ValidateAppSecret() bool {
	return ValidateJWTSecret(c.AppSecret)
}

func deriveSubsecret(secret, domain string) string {
	if secret == "" {
		return ""
	}
	h := hmac.New(sha256.New, []byte(secret))
	h.Write([]byte(domain))
	return base64.RawURLEncoding.EncodeToString(h.Sum(nil))
}

// ValidateJWTSecret returns true if the secret is valid for production use.
func ValidateJWTSecret(secret string) bool {
	if secret == "" {
		return false
	}
	// Reject weak/known secrets
	weakSecrets := []string{
		"dev-secret-change-in-production",
		"dev-secret-change-in-production-12345678",
		"secret",
		"test-secret",
		"change-me",
		"changeme",
	}
	for _, ws := range weakSecrets {
		if secret == ws {
			return false
		}
	}
	// Require minimum length for security (at least 32 chars for strong encryption)
	if len(secret) < 32 {
		return false
	}
	return true
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// getSecret 读取**敏感**配置：优先 `${key}_FILE` 指向的**文件内容**，否则回退 `${key}` 环境变量。
//
// 为什么需要：企业部署的密钥通常不在 env 里，而是由 Vault Agent Injector / K8s Secret
// 卷挂载 / 云 Secrets Manager / CSI driver 以**文件**形式投递（避免密钥出现在 `env`、
// 进程列表或 `docker inspect` 里）。本函数是这类投递方式的通用桥接 —— 设
// `APP_SECRET_FILE=/vault/secrets/app_secret` 即可，无需在部署层把密钥展开成 env。
//
// 文件内容两端的空白（含尾部换行）会被裁掉；文件为空 / 不可读时回退到环境变量。
func getSecret(key, fallback string) string {
	if p := os.Getenv(key + "_FILE"); p != "" {
		if b, err := os.ReadFile(p); err == nil {
			if v := strings.TrimSpace(string(b)); v != "" {
				return v
			}
		}
	}
	return getEnv(key, fallback)
}

func getInt(key string, fallback int) int {
	if v := os.Getenv(key); v != "" {
		if i, err := strconv.Atoi(v); err == nil {
			return i
		}
	}
	return fallback
}

func getFloat(key string, fallback float64) float64 {
	if v := os.Getenv(key); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			return f
		}
	}
	return fallback
}

func getDuration(key string, fallback time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return fallback
}

func getStringSlice(key string, fallback []string) []string {
	if v := os.Getenv(key); v != "" {
		// Split by comma and trim whitespace
		parts := strings.Split(v, ",")
		result := make([]string, 0, len(parts))
		for _, p := range parts {
			p = strings.TrimSpace(p)
			if p != "" {
				result = append(result, p)
			}
		}
		if len(result) > 0 {
			return result
		}
	}
	return fallback
}

// isTruthy returns true if s is "true", "1", "yes", or "on" (case-insensitive).
// cookieSecure 决定会话 cookie 是否带 Secure 标志。
//
// 取值顺序：
//  1. `COOKIE_SECURE` **显式设置**时以它为准（便于"反代终止 TLS、容器内仍是 HTTP"等拓扑）；
//  2. 否则按 `PUBLIC_BASE_URL` 的 scheme 推导：https ⇒ true。
//
// 为什么不沿用"默认 false"：生产漏配 COOKIE_SECURE 会让会话 cookie 在明文 HTTP 上
// 可被窃取（缺 Secure 时浏览器允许经 HTTP 发送）。生产必然设置 PUBLIC_BASE_URL
// （SSO 回调地址也依赖它），因此推导是可靠的；本地 HTTP 开发则推导为 false，
// 不会出现"带 Secure 的 cookie 在 http://localhost 下不被回传、登录不上"的问题。
func cookieSecure() bool {
	if v := strings.TrimSpace(getEnv("COOKIE_SECURE", "")); v != "" {
		return isTruthy(v)
	}
	return strings.HasPrefix(
		strings.ToLower(strings.TrimSpace(getEnv("PUBLIC_BASE_URL", ""))), "https://")
}

func isTruthy(s string) bool {
	switch s {
	case "true", "1", "yes", "on", "TRUE", "YES", "ON":
		return true
	}
	return false
}

// loadDotEnv reads .env file and sets environment variables if not already set.
// findFileUpward searches for a file starting from the current directory
// and walking up to the filesystem root. Returns the first match.
func findFileUpward(name string) string {
	dir, _ := os.Getwd()
	for {
		candidate := filepath.Join(dir, name)
		if _, err := os.Stat(candidate); err == nil {
			return candidate
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break // reached filesystem root
		}
		dir = parent
	}
	return name // fall back to original relative path (will fail with useful error)
}

func loadDotEnv() {
	path := findFileUpward(".env")
	data, err := os.ReadFile(path)
	if err != nil {
		return // .env file not found, skip
	}
	scanner := bufio.NewScanner(strings.NewReader(string(data)))
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		parts := strings.SplitN(line, "=", 2)
		if len(parts) != 2 {
			continue
		}
		key := strings.TrimSpace(parts[0])
		val := strings.TrimSpace(parts[1])
		// Strip quotes if present
		if len(val) >= 2 && ((val[0] == '"' && val[len(val)-1] == '"') || (val[0] == '\'' && val[len(val)-1] == '\'')) {
			val = val[1 : len(val)-1]
		}
		// Only set if not already set (env vars take precedence)
		if os.Getenv(key) == "" {
			os.Setenv(key, val)
		}
	}
}
