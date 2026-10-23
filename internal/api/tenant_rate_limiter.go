package api

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// ── 双限流器说明 ──────────────────────────────────────────────────────
//
// 系统同时存在两个限流器，各有独立用途，非冗余：
//
// 1. TenantRateLimiter（本文件）— 令牌桶算法，QPS 级限流
//    用于知识库（KB）等对 QPS 敏感的场景（每租户 QPS=50, Burst=100）。
//    通过 Redis Lua 脚本实现分布式令牌桶，支持精确的秒级速率控制。
//
// 2. DistributedRateLimiter（distributed_ratelimit.go）— 固定窗口算法，RPM 级限流
//    用于通用 API 限流（全局/租户/用户三级 RPM），基于 Redis INCR + TTL
//    实现固定窗口计数，适合粗粒度每分钟限流。
//
// 二者算法不同、粒度不同、适用场景不同，不可合并。
// ─────────────────────────────────────────────────────────────────────

const tenantBucketLua = `
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local req = tonumber(ARGV[4])

local data = redis.call("HMGET", key, "tokens", "ts")
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  tokens = capacity
  ts = now
end

local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * refill)

local allowed = 0
if tokens >= req then
  tokens = tokens - req
  allowed = 1
end

redis.call("HMSET", key, "tokens", tostring(tokens), "ts", tostring(now))
redis.call("EXPIRE", key, 300)
return tostring(allowed)
`

type TenantRateLimiter struct {
	rdb          db.RedisClient
	maxBurst     int
	refillPerSec float64
	mu           sync.Mutex
	tokens       map[string]*tokenBucketLocal
}

type tokenBucketLocal struct {
	tokens     float64
	lastRefill time.Time
}

func NewTenantRateLimiter(rdb db.RedisClient, maxQPS, burst int) *TenantRateLimiter {
	return &TenantRateLimiter{
		rdb:          rdb,
		maxBurst:     burst,
		refillPerSec: float64(maxQPS),
		tokens:       make(map[string]*tokenBucketLocal),
	}
}

func (rl *TenantRateLimiter) Allow(ctx context.Context, resource, tenantID string) (bool, float64) {
	if rl.rdb == nil {
		return false, 1 // fail-close
	}
	if tenantID == "" {
		return false, 1
	}

	key := fmt.Sprintf("tenantbucket:%s:%s", resource, tenantID)
	now := float64(time.Now().UnixMicro()) / 1e6

	result, err := rl.rdb.Eval(ctx, tenantBucketLua,
		[]string{key},
		rl.maxBurst, rl.refillPerSec, now, 1.0).Text()
	if err != nil {
		slog.Error("tenant rate limit Redis eval failed (fail-close)",
			"error", err, "tenant", tenantID, "resource", resource)
		return false, 1
	}
	if result == "1" {
		return true, 0
	}
	return false, 1
}

// Middleware 返回 HTTP 处理程序，用于 tenant_id + 策略参数
func (rl *TenantRateLimiter) Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		claims := auth.GetClaims(r.Context())
		if claims == nil || claims.TenantID == 0 {
			Unauthorized(w, ErrAuthRequired)
			return
		}
		tenantID := claims.TenantID
		resource := extractResource(r.URL.Path)

		allowed, retryAfter := rl.Allow(r.Context(), resource, tenantStr(tenantID))
		if !allowed {
			slog.Warn("tenant rate limit exceeded",
				"resource", resource, "tenant", tenantID, "retry_after", retryAfter)
			w.Header().Set("Retry-After", formatFloat(retryAfter))
			TooManyRequests(w)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func extractResource(path string) string {
	trimmed := strings.Trim(path, "/")
	if trimmed == "" {
		return "unknown"
	}
	parts := strings.SplitN(trimmed, "/", 4)
	if len(parts) >= 2 {
		resource := parts[1]
		if len(parts) >= 3 {
			action := parts[2]
			if action == "query" || action == "build" || action == "test" {
				return resource + "_" + action
			}
		}
		return resource
	}
	return "unknown"
}

func formatFloat(f float64) string {
	if f < 1 {
		return "1"
	}
	return strconv.Itoa(int(f))
}
