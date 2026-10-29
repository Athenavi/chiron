package api

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"sync/atomic"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// DistributedRateLimiter 基于 Redis 原子令牌桶的分布式限流器。
// 语义（批 B-1′，令牌桶重构）：
//   - 三级独立桶：global（全局限额总量）/ tenant / user；
//   - 桶容量 = 该层每分钟配额（rpm），补充速率 = rpm/60 每秒：允许突发一整分钟额度，
//     之后按速率平滑限流——与实例数无关，天然适配多副本（不再按网关副本数放大）；
//   - 上限来源统一为“总量语义”：构造参数（RATE_LIMIT_GLOBAL / RATE_LIMIT_RPM /
//     租户派生值），后台「系统设置」rate_limit 保存后经 Configure 覆盖（跨副本广播生效）。
type DistributedRateLimiter struct {
	rdb db.RedisClient

	// 每分钟配额（令牌桶换算：rate = rpm/60/s, capacity = rpm）。atomic 保证
	// Configure（后台热更）与 Allow（每请求）并发安全。
	globalRPM atomic.Int64
	tenantRPM atomic.Int64
	userRPM   atomic.Int64
}

// NewDistributedRateLimiter 创建分布式限流器（rpm = 每分钟配额，总量语义）。
func NewDistributedRateLimiter(rdb db.RedisClient, globalRPM, tenantRPM, userRPM int) *DistributedRateLimiter {
	if globalRPM <= 0 {
		globalRPM = 1000
	}
	if tenantRPM <= 0 {
		tenantRPM = 100
	}
	if userRPM <= 0 {
		userRPM = 30
	}
	l := &DistributedRateLimiter{rdb: rdb}
	l.globalRPM.Store(int64(globalRPM))
	l.tenantRPM.Store(int64(tenantRPM))
	l.userRPM.Store(int64(userRPM))
	return l
}

// Configure 运行时热更新三级每分钟配额（阈值 ≤0 表示不修改该级）。
// 由后台「系统设置」保存回调与本实例 settings 订阅者（跨副本广播）调用。
func (l *DistributedRateLimiter) Configure(globalRPM, tenantRPM, userRPM int) {
	if globalRPM > 0 {
		l.globalRPM.Store(int64(globalRPM))
	}
	if tenantRPM > 0 {
		l.tenantRPM.Store(int64(tenantRPM))
	}
	if userRPM > 0 {
		l.userRPM.Store(int64(userRPM))
	}
}

// rateLimitLua 三级令牌桶原子脚本。
//
// KEYS[1] 全局桶   KEYS[2] 租户桶（空串=跳过）   KEYS[3] 用户桶（空串=跳过）
// ARGV[1..6] 三级 (rate, capacity)（每秒补充速率、容量；rate≤0 表示该层跳过）
// ARGV[7] 桶 key 空闲过期秒数
// 返回 "ok" / "global" / "tenant" / "user"
//
// 桶结构：HSET key v(当前令牌,浮点) t(最后补充时刻,浮点秒)；
// 时间取自 Redis TIME（服务器时钟，跨副本一致），避免各副本本地时钟偏差。
const rateLimitLua = `
local function refill(key, rate_str, cap_str)
    if key == "" then return true end
    local rate = tonumber(rate_str)
    local cap = tonumber(cap_str)
    if rate <= 0 or cap <= 0 then return true end
    local t = redis.call("TIME")
    local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
    local cur = cap
    local last = now
    local st = redis.call("HMGET", key, "v", "t")
    if st[1] then
        cur = tonumber(st[1])
        last = tonumber(st[2])
        if not last then last = now end
    end
    cur = cur + (now - last) * rate
    if cur > cap then cur = cap end
    if cur < 1 then return false end
    redis.call("HSET", key, "v", cur - 1, "t", now)
    redis.call("EXPIRE", key, ARGV[7])
    return true
end
if not refill(KEYS[1], ARGV[1], ARGV[2]) then return "global" end
if not refill(KEYS[2], ARGV[3], ARGV[4]) then return "tenant" end
if not refill(KEYS[3], ARGV[5], ARGV[6]) then return "user" end
return "ok"
`

// : 三级桶共用的 hash tag。
// :
// : Redis Cluster 要求**一次 EVAL 里的所有 key 同槽**，否则直接 `CROSSSLOT` 失败；
// : 而限流是 fail-close —— 失败即 **全站 503**（见 `Allow` 的错误分支）。限流操作极短，
// : 同槽带来的热点可接受；不正确不可接受。
const rateLimitHashTag = "{ratelimit}"

// rateLimitKeys 返回三级桶的 Redis key（global / tenant / user；无用户身份时 user 为空串，
// Lua 据此跳过该级）。
//
// 抽成函数是为了让「**三级 key 同槽**」这条不变量能被单测钉住 —— 它此前**没有任何测试**，
// 而它的失效形态只在 Redis Cluster 上暴露（单机/哨兵下多 key EVAL 不校验同槽）。
func rateLimitKeys(tenantID, userID string) (string, string, string) {
	globalKey := db.RedisKey("ratelimit:" + rateLimitHashTag + ":global")
	tenantKey := db.RedisKey("ratelimit:" + rateLimitHashTag + ":tenant:" + tenantID)
	userKey := ""
	if userID != "" {
		userKey = db.RedisKey("ratelimit:" + rateLimitHashTag + ":user:" + userID)
	}
	return globalKey, tenantKey, userKey
}

// rpmRateArgs 把每分钟配额换算为令牌桶 (rate, capacity) 参数串。
// rate = rpm/60（每秒补充）, capacity = rpm（允许一次性突发整分钟额度）。
func rpmRateArgs(rpm int64) (string, string) {
	if rpm <= 0 {
		return "0", "0"
	}
	return strconv.FormatFloat(float64(rpm)/60.0, 'f', -1, 64),
		strconv.FormatFloat(float64(rpm), 'f', -1, 64)
}

// ErrRateLimitExceeded 表示"速率闸门确实拦下了这次请求"，与"限流服务不可用"是两回事。
//
// 必须分开：此前 `Allow` 两种情况都返回普通 error，中间件只能一律当成后者。
var ErrRateLimitExceeded = errors.New("rate limit exceeded")

// rateLimitDecision 把 Lua 的返回值翻译成 `Allow` 的两个出口。
//
// 抽成纯函数是为了能被测试直接锁住"桶拦下了"这条路径 —— 它曾经是**不可达**的：
// 两类失败都是普通 error，中间件一律回 503 "rate limiter unavailable"。后果不只是
// 文案难看，而是动作给错了：超额的用户被告知"服务坏了"，客户端按 503 判定不可重试，
// 恰恰放弃了正确的做法（等一会儿原样重发）。
func rateLimitDecision(result, tenantID, userID string) (bool, error) {
	switch result {
	case "global":
		return false, fmt.Errorf("%w: 全局请求频率超限", ErrRateLimitExceeded)
	case "tenant":
		return false, fmt.Errorf("%w: 租户 %s 请求频率超限", ErrRateLimitExceeded, tenantID)
	case "user":
		return false, fmt.Errorf("%w: 用户 %s 请求频率超限", ErrRateLimitExceeded, userID)
	default:
		return true, nil
	}
}

// Allow 检查并消费一个令牌 — 单次原子 eval 完成三级检查与扣减。
// fail-close 策略：Redis 不可用或 Eval 错误时拒绝请求（生产安全优先）。
func (l *DistributedRateLimiter) Allow(ctx context.Context, tenantID, userID string) (bool, error) {
	if l.rdb == nil {
		return false, fmt.Errorf("限流 Redis 不可用，按 fail-close 拒绝请求")
	}
	if tenantID == "" {
		// 未认证公开端点（install/login/register/health 等）共用 public 桶限流，
		// 防止单一来源滥用，但不拒绝（否则 install 首次部署无法完成）
		tenantID = "public"
	}

	globalKey, tenantKey, userKey := rateLimitKeys(tenantID, userID)

	gRate, gCap := rpmRateArgs(l.globalRPM.Load())
	tRate, tCap := rpmRateArgs(l.tenantRPM.Load())
	uRate, uCap := rpmRateArgs(l.userRPM.Load())
	if userKey == "" {
		uRate, uCap = "0", "0" // 无用户身份时跳过 user 层
	}

	// 空闲过期：10 分钟（覆盖最大容量的补充周期后即可清理，防止 key 堆积）
	result, err := l.rdb.Eval(ctx, rateLimitLua,
		[]string{globalKey, tenantKey, userKey},
		gRate, gCap, tRate, tCap, uRate, uCap, 600).Text()
	if err != nil {
		slog.Error("限流检查失败（fail-close）", "error", err, "tenant", tenantID)
		return false, fmt.Errorf("限流服务暂时不可用: %w", err)
	}

	return rateLimitDecision(result, tenantID, userID)
}

// DistributedRateLimitMiddleware 分布式限流中间件
func DistributedRateLimitMiddleware(limiter *DistributedRateLimiter) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			// 提取 tenant_id 与 user_id（多租户隔离键）
			var tenantID, userID string
			claims := auth.GetClaims(r.Context())
			if claims != nil {
				tenantID = tenantStr(claims.TenantID)
				userID = claims.UserID
			}

			allowed, err := limiter.Allow(r.Context(), tenantID, userID)
			if writeRateLimitRejection(w, err, userID, r.URL.Path) {
				return
			}
			if !allowed {
				// 兜底：正常路径下"拒绝"都带 error（见 rateLimitDecision），走不到这里。
				TooManyRequestsAfterSeconds(w, 60)
				return
			}

			next.ServeHTTP(w, r)
		})
	}
}

// writeRateLimitRejection 把 `Allow` 的失败翻译成 HTTP 响应，返回 true 表示已写出响应、调用方应直接返回。
//
// 两类失败必须分开，否则 429 分支永远不可达：
//  1. 真正的"超额"（`ErrRateLimitExceeded`）→ 429 + Retry-After，客户端该等一会儿重试；
//  2. 限流服务本身不可用（Redis 故障 / Eval 失败）→ 503，重试同样没用，但这是**我们**的
//     故障，不该让用户以为自己发得太快（此前两者都是 503，用户看到的正是"服务坏了"）。
//
// 抽成独立函数是为了能被测试直接区分这两条路径 —— 否则只能起一个真实 Redis 才验证得了。
func writeRateLimitRejection(w http.ResponseWriter, err error, userID, path string) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, ErrRateLimitExceeded) {
		// 令牌桶按 rpm 补充，窗口上限就是 60s —— 这是算得出来的，不是猜的。
		TooManyRequestsAfterSeconds(w, 60)
		return true
	}
	slog.Error("限流服务不可用（fail-close）", "error", err, "user", userID, "path", path)
	ServiceUnavailable(w, "rate limiter unavailable")
	return true
}
