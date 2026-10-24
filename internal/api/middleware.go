package api

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/monitor"
)

// ── 审计日志异步 worker pool ──
// 避免热路径每个请求启动一个 goroutine，在突发流量下防止 goroutine 堆积
type auditLogTask struct {
	ctx        context.Context
	userID     string
	tenantID   int16
	method     string
	path       string
	remoteAddr string
	status     int
}

var (
	auditLogCh   = make(chan auditLogTask, 4096) // 缓冲通道，防背压时丢弃
	auditLogOnce sync.Once
)

// StartAuditLogWorker 启动审计日志 worker（main.go 中调用一次）
func StartAuditLogWorker(ctx context.Context) {
	auditLogOnce.Do(func() {
		const workerCount = 4
		for i := 0; i < workerCount; i++ {
			go func(id int) {
				slog.Debug("audit log worker started", "id", id)
				for {
					select {
					case <-ctx.Done():
						return
					case task := <-auditLogCh:
						meta := map[string]interface{}{
							"status": task.status,
							"method": task.method,
							"path":   task.path,
						}
						db.AuditLog(task.ctx, task.userID, task.tenantID, task.method, task.path, "", task.remoteAddr, meta)
					}
				}
			}(i)
		}
	})
}

type responseWriter struct {
	http.ResponseWriter
	status  int
	bytes   int
	flusher http.Flusher
}

// ── JWT 黑名单本地 TTL 缓存（P1 优化：减少热路径每次请求的 Redis 往返）──
// 正缓存（已拉黑）15 分钟有效；负缓存（未拉黑）仅 30 秒，确保登出撤销
// 在 ≤30s 内全局生效（多副本仍以 Redis 为最终事实源）。
type jwtBlacklistEntry struct {
	blacklisted bool
	checkedAt   time.Time
}

var jwtBlacklistCache sync.Map // jti -> jwtBlacklistEntry

const (
	jwtBlacklistHitTTL  = 15 * time.Minute
	jwtBlacklistMissTTL = 30 * time.Second
)

// checkJWTBlacklisted 优先查本地缓存，miss 时回源 Redis 并回填。
func checkJWTBlacklisted(ctx context.Context, jti string) (bool, error) {
	if v, ok := jwtBlacklistCache.Load(jti); ok {
		e := v.(jwtBlacklistEntry)
		ttl := jwtBlacklistMissTTL
		if e.blacklisted {
			ttl = jwtBlacklistHitTTL
		}
		if time.Since(e.checkedAt) < ttl {
			return e.blacklisted, nil
		}
	}
	if db.Redis == nil {
		// fail-close（安全优先，用户策略）：Redis 不可用视为无法验证，
		// 返回错误让调用方拒绝请求，而非放行登出后的 token。
		return false, errors.New("jwt blacklist store unavailable (redis not configured)")
	}
	n, err := db.Redis.Exists(ctx, db.RedisKey("jwt:blacklist:")+jti).Result()
	if err != nil {
		return false, err
	}
	jwtBlacklistCache.Store(jti, jwtBlacklistEntry{blacklisted: n > 0, checkedAt: time.Now()})
	return n > 0, nil
}

// markJWTBlacklisted 登出时同步本地正缓存（配合 Redis 写入）。
func markJWTBlacklisted(jti string) {
	if jti == "" {
		return
	}
	jwtBlacklistCache.Store(jti, jwtBlacklistEntry{blacklisted: true, checkedAt: time.Now()})
}

// StartBlacklistCleaner 定期清理过期的 JWT 黑名单本地缓存条目，防止内存泄漏。
// 每 10 分钟扫描一次，删除超过 1 小时未更新的条目。
func StartBlacklistCleaner(ctx context.Context) {
	go func() {
		ticker := time.NewTicker(10 * time.Minute)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				expired := 0
				cutoff := time.Now().Add(-1 * time.Hour)
				jwtBlacklistCache.Range(func(key, value any) bool {
					e := value.(jwtBlacklistEntry)
					if e.checkedAt.Before(cutoff) {
						jwtBlacklistCache.Delete(key)
						expired++
					}
					return true
				})
				if expired > 0 {
					slog.Debug("cleaned expired JWT blacklist cache entries", "count", expired)
				}
			}
		}
	}()
}

// StartBlacklistPubSub 启动 Redis Pub/Sub 监听器，实现跨实例黑名单同步。
// 当任一实例将 token 加入黑名单时，通过 Redis channel 广播给所有其他实例。
func StartBlacklistPubSub(ctx context.Context) {
	if db.Redis == nil {
		slog.Warn("Redis not available, JWT blacklist cross-instance sync disabled")
		return
	}

	go func() {
		pubsub := db.Redis.Subscribe(ctx, db.RedisKey("jwt:blacklist:sync"))
		defer pubsub.Close()

		ch := pubsub.Channel()
		slog.Info("JWT blacklist Pub/Sub listener started")

		for {
			select {
			case <-ctx.Done():
				return
			case msg, ok := <-ch:
				if !ok {
					slog.Warn("JWT blacklist Pub/Sub channel closed")
					return
				}
				// 消息格式: "jti"
				jti := msg.Payload
				if jti != "" {
					slog.Debug("received cross-instance blacklist notification", "jti", jti)
					markJWTBlacklisted(jti)
				}
			}
		}
	}()
}

// broadcastBlacklistSync 向其他实例广播 token 黑名单事件
func broadcastBlacklistSync(jti string) {
	if db.Redis == nil || jti == "" {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	if err := db.Redis.Publish(ctx, db.RedisKey("jwt:blacklist:sync"), jti).Err(); err != nil {
		slog.Warn("failed to broadcast JWT blacklist sync", "jti", jti, "error", err)
	}
}

func (rw *responseWriter) WriteHeader(status int) {
	rw.status = status
	rw.ResponseWriter.WriteHeader(status)
}

func (rw *responseWriter) Write(b []byte) (int, error) {
	n, err := rw.ResponseWriter.Write(b)
	rw.bytes += n
	return n, err
}

func (rw *responseWriter) Flush() {
	if rw.flusher != nil {
		rw.flusher.Flush()
	}
}

// Hijack implements http.Hijacker, required for WebSocket upgrades through middleware.
func (rw *responseWriter) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	if hj, ok := rw.ResponseWriter.(http.Hijacker); ok {
		return hj.Hijack()
	}
	return nil, nil, fmt.Errorf("underlying ResponseWriter does not support hijacking")
}

func LoggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		var flusher http.Flusher
		if f, ok := w.(http.Flusher); ok {
			flusher = f
		}
		rw := &responseWriter{ResponseWriter: w, status: http.StatusOK, flusher: flusher}

		next.ServeHTTP(rw, r)

		slog.Info("request",
			"method", r.Method,
			"path", r.URL.Path,
			"status", rw.status,
			"bytes", rw.bytes,
			"duration", time.Since(start).String(),
			"ip", r.RemoteAddr,
			"trace_id", GetTraceID(r.Context()),
		)

		// Write audit log for non-GET requests (P0-性能: 异步写入，不阻塞响应返回)
		if r.Method != "GET" && r.Method != "OPTIONS" && rw.status < 500 {
			claims := auth.GetClaims(r.Context())
			if claims != nil {
				select {
				case auditLogCh <- auditLogTask{
					ctx:        r.Context(),
					userID:     claims.UserID,
					tenantID:   claims.TenantID,
					method:     r.Method,
					path:       r.URL.Path,
					remoteAddr: r.RemoteAddr,
					status:     rw.status,
				}:
				default:
					slog.Warn("audit log channel full, dropping entry",
						"path", r.URL.Path, "method", r.Method)
				}
			}
		}
	})
}

// TracingMiddleware creates a span for each HTTP request and attaches it to the context.
// Must be placed early in the middleware chain so downstream handlers can use monitor.GetSpan.
// Also injects a trace_id into context for structured log correlation.
func TracingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ctx, span := monitor.StartSpan(r.Context(), r.Method+" "+r.URL.Path, "server")
		span.SetTag("http.method", r.Method)
		span.SetTag("http.path", r.URL.Path)
		span.SetTag("http.remote_addr", r.RemoteAddr)
		// Inject trace_id into context for structured log correlation
		ctx = context.WithValue(ctx, traceIDKey, span.TraceID.String())
		defer span.End()

		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// traceIDKey is a context key type for trace_id to avoid collisions.
type traceIDKeyType struct{}

var traceIDKey = traceIDKeyType{}

// GetTraceID extracts the trace_id from context, if present.
func GetTraceID(ctx context.Context) string {
	if v, ok := ctx.Value(traceIDKey).(string); ok {
		return v
	}
	return ""
}

func RecoverMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				// http.ErrAbortHandler 是标准库中用于强制中止连接的 sentinel panic，
				// 不应被拦截，否则 http.Server 无法正确关闭连接。
				if rec == http.ErrAbortHandler {
					panic(rec)
				}
				slog.Error("panic recovered", "panic", rec, "path", r.URL.Path)
				InternalError(w, "internal server error")
			}
		}()
		next.ServeHTTP(w, r)
	})
}

// CORSMiddleware 处理 CORS。白名单取自进程内运行时共享源（cors_runtime.go）：
// 启动由 gateway_router 注入 cfg.CORSOrigins，后台保存 cors 分类后本实例与
// 跨副本订阅者热更新；"*" 在 AllowCredentials=true 下违反 CORS 规范且高危，显式拒绝。
func CORSMiddleware() func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			origin := r.Header.Get("Origin")
			allowed := false
			if origin != "" && corsOriginAllowed(origin) {
				allowed = true
			}
			if allowed {
				w.Header().Set("Access-Control-Allow-Origin", origin)
				w.Header().Set("Vary", "Origin")
				w.Header().Set("Access-Control-Allow-Credentials", "true")
			}
			w.Header().Set("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
			w.Header().Set("Access-Control-Allow-Headers", "Content-Type,Authorization,X-API-Key")
			w.Header().Set("Access-Control-Max-Age", "86400")

			if r.Method == http.MethodOptions {
				w.WriteHeader(http.StatusNoContent)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

// SecurityHeadersMiddleware 注入通用安全响应头。
// CSP_CONNECT_SRC 通过 env 注入（默认留空则不强制 connect-src 白名单，
// 由部署方按生产域名配置，避免 localhost 写死导致生产环境被阻断）。
func SecurityHeadersMiddleware(next http.Handler) http.Handler {
	cspConnectSrc := strings.TrimSpace(os.Getenv("CSP_CONNECT_SRC"))
	if cspConnectSrc == "" {
		cspConnectSrc = "'self'"
	}
	csp := "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; " +
		"img-src 'self' data: blob:; connect-src " + cspConnectSrc + "; " +
		"font-src 'self' https://fonts.gstatic.com; frame-ancestors 'none'"
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("X-Frame-Options", "DENY")
		w.Header().Set("X-XSS-Protection", "1; mode=block")
		w.Header().Set("Content-Security-Policy", csp)
		w.Header().Set("Referrer-Policy", "strict-origin-when-cross-origin")
		w.Header().Set("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
		w.Header().Set("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
		next.ServeHTTP(w, r)
	})
}

// AuthMiddleware validates JWT from cookie or Authorization header.
func AuthMiddleware(a *auth.Authenticator) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			tokenStr := ""

			// 1. Cookie (primary for browser clients; SSE 经 withCredentials 携带)
			if c, err := r.Cookie("chiron_token"); err == nil && c.Value != "" {
				tokenStr = c.Value
			}

			// 2. Authorization: Bearer <token> (for API clients)
			if tokenStr == "" {
				if ah := r.Header.Get("Authorization"); ah != "" {
					if strings.HasPrefix(ah, "Bearer ") {
						tokenStr = strings.TrimPrefix(ah, "Bearer ")
					}
				}
			}

			// 2. Try X-API-Key header
			if tokenStr == "" {
				if key := r.Header.Get("X-API-Key"); key != "" {
					// Validate API key against PostgreSQL（含 tenant_id 与 revoked 状态校验，多租户隔离）
					var userID, role, tenantID string
					keyHash := sha256.Sum256([]byte(key))
					err := db.GlobalDBManager.QueryRow(r.Context(),
						`SELECT u.id, u.role, COALESCE(u.tenant_id::text, '') AS tenant_id
					 FROM users u
					 JOIN api_keys ak ON ak.user_id = u.id
					 WHERE ak.key_hash = $1
					   AND COALESCE(ak.revoked, false) = false
					   AND (ak.expires_at IS NULL OR ak.expires_at > NOW())`,
						hex.EncodeToString(keyHash[:])).Scan(&userID, &role, &tenantID)
					if err == nil {
						// P1-5: tenant_id 为空直接拒绝，不再回退 DefaultTenantID。
						// 历史数据中 tenant_id=NULL 的 user 走 DefaultTenantID 会落到默认租户，
						// 造成跨租户数据访问；多租户部署必须强制每个用户绑定租户。
						if tenantID == "" {
							slog.Warn("API key bound to user with null tenant_id, rejecting",
								"user_id", userID)
							Unauthorized(w, "user has no tenant binding; contact admin")
							return
						}
						// API Key 是长期凭据（不过期或长过期），泄漏面远大于短期 JWT：
						// 这里只授予常规业务权限，剔除管理类权限（admin/users/policy/
						// ent/market/audit）—— 管理操作必须走交互式登录。
						claims := &auth.Claims{
							UserID:   userID,
							Role:     role,
							TenantID: parseTenantID(tenantID),
							Perms:    auth.APIKeyPermissions(role),
						}
						ctx := auth.WithClaims(r.Context(), claims)
						next.ServeHTTP(w, r.WithContext(ctx))
						return
					}
					Unauthorized(w, "invalid API key")
					return
				}
			}

			if tokenStr == "" {
				Unauthorized(w, "missing authorization")
				return
			}

			claims, err := a.ValidateToken(tokenStr)
			if err != nil {
				Unauthorized(w, "invalid or expired token")
				return
			}

			// ── 会话纪元检查（改密/密码重置后旧 token 立即失效）──
			// 与黑名单同为 fail-close：Redis 不可用时拒绝请求，避免撤销在故障期间失效。
			// 纪元 > token.iat ⇒ 该 token 签发于撤销之前 ⇒ 拒绝。
			epoch, epochErr := sessionEpoch(r.Context(), claims.UserID)
			if epochErr != nil {
				slog.Warn("session epoch check failed (fail-close)", "error", epochErr)
				Unauthorized(w, "authentication service unavailable")
				return
			}
			if epoch > 0 && claims.IssuedAt != nil && epoch > claims.IssuedAt.Unix() {
				Unauthorized(w, "token has been revoked")
				return
			}

			// ── JWT 黑名单检查（登出后的 token 立即失效）──
			// fail-close：Redis 不可用/检查失败时拒绝请求（安全优先），
			// 避免故障期间登出 token 被复用。
			if claims.ID != "" {
				blacklisted, err := checkJWTBlacklisted(r.Context(), claims.ID)
				if err != nil {
					slog.Warn("jwt blacklist check failed (fail-close)", "error", err)
					Unauthorized(w, "authentication service unavailable")
					return
				}
				if blacklisted {
					Unauthorized(w, "token has been revoked")
					return
				}
			}

			ctx := auth.WithClaims(r.Context(), claims)
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

// RequirePermission checks that the authenticated user has the specified permission.
func RequirePermission(perm string) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			claims := auth.GetClaims(r.Context())
			if !auth.HasPermission(claims, perm) {
				Forbidden(w, "insufficient permissions")
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

// RateLimitMiddleware implements a simple in-memory token bucket per IP.
// For production, this should use Redis (sliding window).
type RateLimiter struct {
	mu       sync.Mutex
	visitors map[string]*visitor
	rpm      int
	stopCh   chan struct{}
}

type visitor struct {
	count   int
	resetAt time.Time
}

func NewRateLimiter(rpm int) *RateLimiter {
	return &RateLimiter{
		visitors: make(map[string]*visitor),
		rpm:      rpm,
		stopCh:   make(chan struct{}),
	}
}

func (rl *RateLimiter) Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ip := r.RemoteAddr
		if idx := strings.LastIndex(ip, ":"); idx >= 0 {
			ip = ip[:idx]
		}

		rl.mu.Lock()
		v, exists := rl.visitors[ip]
		now := time.Now()

		if !exists || now.After(v.resetAt) {
			rl.visitors[ip] = &visitor{count: 1, resetAt: now.Add(1 * time.Minute)}
			rl.mu.Unlock()
			next.ServeHTTP(w, r)
			return
		}

		if v.count >= rl.rpm {
			rl.mu.Unlock()
			TooManyRequests(w)
			return
		}

		v.count++
		rl.mu.Unlock()
		next.ServeHTTP(w, r)
	})
}

// CleanupVisitors periodically removes stale entries.
func (rl *RateLimiter) CleanupVisitors(interval time.Duration) {
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		defer func() {
			if r := recover(); r != nil {
				slog.Error("rate limiter cleanup panic", "panic", r)
			}
		}()
		for {
			select {
			case <-ticker.C:
				rl.mu.Lock()
				now := time.Now()
				for ip, v := range rl.visitors {
					if now.After(v.resetAt) {
						delete(rl.visitors, ip)
					}
				}
				rl.mu.Unlock()
			case <-rl.stopCh:
				return
			}
		}
	}()
}

// Stop cleanly terminates the cleanup goroutine.
func (rl *RateLimiter) Stop() {
	close(rl.stopCh)
}

// MonitoringMiddleware tracks request counts and request latency.
func MonitoringMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		monitor.IncRequests()
		start := time.Now()
		// 并发数与耗时都在 defer 中记录：异常路径也不遗漏采样，避免 p99 只统计
		// 成功请求而失真。此前 RequestHistogram 只有定义、无任何写入点，
		// 导致看板的 api_latency_p99 恒为空值。
		defer func() {
			monitor.DecRequests()
			monitor.RequestHistogram.Record(time.Since(start))
		}()
		next.ServeHTTP(w, r)
	})
}
