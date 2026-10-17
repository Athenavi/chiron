package api

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strconv"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/redis/go-redis/v9"
)

// quotaKeyPrefix 脱敏 Redis key 中的 tenantID，仅保留前缀用于日志。
func quotaKeyPrefix(key string) string {
	parts := strings.SplitN(key, ":", 4)
	if len(parts) >= 3 {
		return strings.Join(parts[:3], ":") + ":***"
	}
	return key[:len(key)/2] + "***"
}

// ErrTenantQuotaExceeded 表示租户当期 token 配额已耗尽（EnforceTenantQuota 唯一非 nil 返回）。
var ErrTenantQuotaExceeded = errors.New("tenant quota exceeded")

// quotaRedis 是配额计数所需的最小 Redis 接口（db.RedisClient 满足；测试可注入 fake）。
type quotaRedis interface {
	Exists(ctx context.Context, keys ...string) *redis.IntCmd
	Get(ctx context.Context, key string) *redis.StringCmd
	Set(ctx context.Context, key string, value interface{}, expiration time.Duration) *redis.StatusCmd
	Eval(ctx context.Context, script string, keys []string, args ...interface{}) *redis.Cmd
}

// quotaStore 是配额强制所需的最小数据访问接口（pgEntCostStore 满足）。
type quotaStore interface {
	ResolveTenantID(ctx context.Context, userID string) (string, error)
	TenantTokenPools(ctx context.Context, tenantID string) ([]EntQuotaPool, error)
	TokenUsageSQL(ctx context.Context, tenantID string, since time.Time) (int64, error)
}

// quotaIncrScript 原子自增并刷新月末/日末过期时间，返回自增后的值。
const quotaIncrScript = `
local v = redis.call('INCRBY', KEYS[1], ARGV[1])
redis.call('EXPIREAT', KEYS[1], ARGV[2])
return v`

// quotaDeltaScript 用于**负增量**（回合末把预检预留扣回）。
//
// 扣回可能把计数压到 0 以下（回填值本来就偏低时），负数会让"用量 > 上限"永久不成立 ——
// 那比偏高更危险（配额形同虚设），所以夹到 0：宁可少算一个回合，也不给租户一个负数额度。
const quotaDeltaScript = `
local v = redis.call('INCRBY', KEYS[1], ARGV[1])
if v < 0 then
  redis.call('SET', KEYS[1], 0)
  v = 0
end
redis.call('EXPIREAT', KEYS[1], ARGV[2])
return v`

// tenantQuotaEnforcer 组合 store/redis/时钟，便于单测注入。
type tenantQuotaEnforcer struct {
	store quotaStore
	redis quotaRedis
	now   func() time.Time
}

// EnforceTenantQuota 是聊天请求的租户 token 配额预检（fail-open）。
//
// 语义（P0-3：计数单位必须是 token，不是请求条数）：
//   - 租户无 token 配额池配置 → 直接放行（reserved=0，nil）。
//   - Redis 计数器 ent:quota:tokens:{tenantID}:{yyyymm|yyyymmdd} 按 **estimatedTokens**
//     INCR（Lua 原子自增 + 周期末过期）；缓存键缺失时先从 billing_records SQL 聚合回填。
//   - 预检只能拿到**估算**（真实用量要到回合末才知道），所以调用方必须把返回值 `reserved`
//     带进回合，结束时用 TrueUpTenantQuota 扣回并按真实 usage 入账 ——
//     否则一次长回合（数万 token）只在池子里留下 1。
//   - 任何策略/存储查询失败一律 fail-open（返回 nil），
//     保证现有聊天链路永不因配额子系统中断。
//
// 返回 `reserved` = 实际写进计数器的估算量（0 表示什么都没计入：无池、Redis 失败、或被拒）。
// 被拒时返回 (0, ErrTenantQuotaExceeded) —— 请求不会起回合，不需要校正。
func EnforceTenantQuota(ctx context.Context, claims *auth.Claims, estimatedTokens int64) (reserved int64, err error) {
	var rr quotaRedis
	if db.Redis != nil {
		rr = db.Redis
	}
	e := &tenantQuotaEnforcer{store: newPGEntCostStore(), redis: rr, now: time.Now}
	return e.enforce(ctx, claims, estimatedTokens)
}

// enforce 是核心逻辑（可注入依赖，独立可测）。
func (e *tenantQuotaEnforcer) enforce(ctx context.Context, claims *auth.Claims, estimatedTokens int64) (int64, error) {
	if claims == nil || claims.UserID == "" {
		return 0, nil // 无身份信息无从强制，放行
	}
	if ctx == nil {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
	} else {
		// 确保传入的 ctx 有超时，防止无限阻塞
		if _, hasDeadline := ctx.Deadline(); !hasDeadline {
			var cancel context.CancelFunc
			ctx, cancel = context.WithTimeout(ctx, 10*time.Second)
			defer cancel()
		}
	}
	if estimatedTokens <= 0 {
		estimatedTokens = 1 // 无预估时最小增量，保持计数器热度
	}

	tenantID := claims.TenantID
	if tenantID == "" {
		var err error
		tenantID, err = e.store.ResolveTenantID(ctx, claims.UserID)
		if err != nil {
			slog.Warn("quota enforce: tenant resolve failed, fail-open",
				"user_id", claims.UserID, "error", err)
			return 0, nil
		}
	}

	pools, err := e.store.TenantTokenPools(ctx, tenantID)
	if err != nil {
		slog.Warn("quota enforce: pool query failed, fail-open",
			"tenant_id", tenantID, "error", err)
		return 0, nil
	}
	if len(pools) == 0 {
		return 0, nil // 该租户无配额池配置 → 直接放行
	}

	// committed：至少有一个池计入了预估 ⇒ 回合末必须扣回同样这一份。
	// 多池时只要有一池失败就仍按预估上报：校正增量会同样施加到**那个失败的池**上
	// （它拿到的是"全额真实值"，偏差不超过一次预估）。计数器每周期由 billing_records 回填，
	// 这种漂移不会累积。
	var committed int64
	for _, pool := range pools {
		used, ok := e.consume(ctx, tenantID, pool, estimatedTokens)
		if !ok {
			continue // 计数链路任何失败 → fail-open
		}
		committed = estimatedTokens
		// TotalAmount <= 0 = 不限额：仍然**计数**（此前直接 continue 连计数都跳过，
		// 与"仅计数不拦截"的注释相反，配额面板只能退回 SQL 聚合）。
		if pool.TotalAmount > 0 && used > pool.TotalAmount {
			return 0, fmt.Errorf("%w: tenant %s token %s used %d/%d",
				ErrTenantQuotaExceeded, tenantID, pool.Period, used, pool.TotalAmount)
		}
	}
	return committed, nil
}

// TrueUpTenantQuota 把预检时预留的**估算** token 换成回合的**真实** token。
//
// 增量 = actual − reserved：预留与真实通常不同向（低估则补、高估则退），所以被闸门挡下、
// 引擎不可达等"没跑起来"的请求也必须调用它（actual=0）退还预留，否则每被拒一次就在池子里
// 永久留下一次预估 —— 那是**自伤式**的偏差（用户还没花钱就被记了钱）。
//
// 与预检同为 fail-open：计数失败只让配额变松，绝不影响已经完成的对话。
func TrueUpTenantQuota(ctx context.Context, userID, tenantID string, actualTokens, reservedTokens int64) {
	delta := actualTokens - reservedTokens
	if delta == 0 || userID == "" {
		return
	}
	var rr quotaRedis
	if db.Redis != nil {
		rr = db.Redis
	}
	e := &tenantQuotaEnforcer{store: newPGEntCostStore(), redis: rr, now: time.Now}
	e.trueUp(ctx, userID, tenantID, delta)
}

// trueUp 对租户的每个 token 池施加校正增量（不判定超限：判定归预检）。
func (e *tenantQuotaEnforcer) trueUp(ctx context.Context, userID, tenantID string, delta int64) {
	if ctx == nil {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
	} else if _, hasDeadline := ctx.Deadline(); !hasDeadline {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, 10*time.Second)
		defer cancel()
	}
	if tenantID == "" {
		resolved, err := e.store.ResolveTenantID(ctx, userID)
		if err != nil {
			slog.Warn("quota true-up: tenant resolve failed, skip",
				"user_id", userID, "error", err)
			return
		}
		tenantID = resolved
	}
	pools, err := e.store.TenantTokenPools(ctx, tenantID)
	if err != nil {
		slog.Warn("quota true-up: pool query failed, skip", "tenant_id", tenantID, "error", err)
		return
	}
	for _, pool := range pools {
		if _, ok := e.consume(ctx, tenantID, pool, delta); !ok {
			continue
		}
	}
}

// consume 对单个配额池执行"回填（如需）+ 原子自增"，返回自增后的用量。
// ok=false 表示计数失败，调用方应 fail-open。
func (e *tenantQuotaEnforcer) consume(ctx context.Context, tenantID string, pool EntQuotaPool, delta int64) (int64, bool) {
	if e.redis == nil {
		slog.Warn("quota enforce: redis unavailable, fail-open", "tenant_id", tenantID)
		return 0, false
	}
	now := time.Now()
	if e.now != nil {
		now = e.now()
	}
	key, periodStart, expireAt := quotaPeriodKey(tenantID, pool.Period, now)

	// 缓存键缺失 → 从 billing_records SQL 聚合回填
	if n, err := e.redis.Exists(ctx, key).Result(); err != nil {
		slog.Warn("quota enforce: redis exists failed, fail-open", "key", quotaKeyPrefix(key), "error", err)
		return 0, false
	} else if n == 0 {
		used, err := e.store.TokenUsageSQL(ctx, tenantID, periodStart)
		if err != nil {
			slog.Warn("quota enforce: usage backfill query failed, fail-open",
				"tenant_id", tenantID, "error", err)
			return 0, false
		}
		if err := e.redis.Set(ctx, key, used, time.Until(expireAt)).Err(); err != nil {
			slog.Warn("quota enforce: usage backfill set failed, fail-open",
				"key", quotaKeyPrefix(key), "error", err)
			return 0, false
		}
	}

	script := quotaIncrScript
	if delta < 0 {
		script = quotaDeltaScript // 负增量（预留扣回）需要夹住下界
	}
	res, err := e.redis.Eval(ctx, script, []string{key}, delta, expireAt.Unix()).Result()
	if err != nil {
		slog.Warn("quota enforce: redis incr failed, fail-open", "key", quotaKeyPrefix(key), "error", err)
		return 0, false
	}
	used, ok := res.(int64)
	if !ok {
		slog.Warn("quota enforce: unexpected incr result, fail-open", "key", quotaKeyPrefix(key), "result", res)
		return 0, false
	}
	return used, true
}

// quotaPeriodKey 返回计数器键、周期起点与过期时间（UTC）。
// monthly → ent:quota:tokens:{tenant}:{yyyymm}；daily → ent:quota:tokens:{tenant}:{yyyymmdd}。
func quotaPeriodKey(tenantID, period string, now time.Time) (key string, start, expireAt time.Time) {
	now = now.UTC()
	if period == "daily" {
		start = time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, time.UTC)
		return db.RedisKey(fmt.Sprintf("ent:quota:tokens:%s:%s", tenantID, now.Format("20060102"))),
			start, start.AddDate(0, 0, 1)
	}
	start = time.Date(now.Year(), now.Month(), 1, 0, 0, 0, 0, time.UTC)
	return db.RedisKey(fmt.Sprintf("ent:quota:tokens:%s:%s", tenantID, now.Format("200601"))),
		start, start.AddDate(0, 1, 0)
}

// tenantTokenUsage 读取租户当期 token 用量：优先 Redis 计数器，缺失时
// billing_records SQL 聚合（只读，不回填）。供 QuotaUsage 端点复用。
func tenantTokenUsage(ctx context.Context, store quotaStore, r quotaRedis, tenantID, period string, now time.Time) (int64, string) {
	if r != nil {
		key, _, _ := quotaPeriodKey(tenantID, period, now)
		if v, err := r.Get(ctx, key).Result(); err == nil && v != "" {
			if used, pErr := strconv.ParseInt(v, 10, 64); pErr == nil {
				return used, "redis"
			}
		}
	}
	_, periodStart, _ := quotaPeriodKey(tenantID, period, now)
	used, err := store.TokenUsageSQL(ctx, tenantID, periodStart)
	if err != nil {
		slog.Warn("quota usage: sql aggregation failed", "tenant_id", tenantID, "error", err)
		return 0, "none"
	}
	return used, "sql"
}

// ── 预检估算与预留回执 ───────────────────────────────────────────

// quotaAttachmentEstimateTokens 是**单个引用型附件**的占位预估。
//
// 附件只带 `{id,name,mime_type,url}`（见 ChatView.vue 的 body.attachments）：真实文本量
// 要等引擎抽取（PDF/docx→文本、图片→视觉块）才知道，网关无从获得。给一个保守常数，
// 回合末由 TrueUpTenantQuota 换成真实值 —— 预估只影响"这一瞬间能不能过闸门"，不留永久偏差。
const quotaAttachmentEstimateTokens = 1024

// EstimateRequestTokens 按请求体粗估输入 token 量（预检预留用，非计费依据）。
//
// 中日韩字符约 1 token/字，其余约 1 token/4 字符 —— 纯按字符数除 4 会把中文请求低估 4 倍，
// 而本仓库的主要用户输入是中文。
func EstimateRequestTokens(content string, attachments []map[string]interface{}) int64 {
	var wide, other int64
	for _, r := range content {
		if isWideRune(r) {
			wide++
			continue
		}
		other++
	}
	est := wide + other/4
	if est < 1 {
		est = 1
	}
	return est + int64(len(attachments))*quotaAttachmentEstimateTokens
}

// isWideRune 判定"一字≈一 token"的书写系统：CJK（含扩展 A/兼容表意文字）、假名、
// 韩文、以及全角标点。
func isWideRune(r rune) bool {
	switch {
	case r >= 0x3000 && r <= 0x30FF, // 标点 + 平/片假名
		r >= 0x3400 && r <= 0x4DBF,   // 扩展 A
		r >= 0x4E00 && r <= 0x9FFF,   // 基本区
		r >= 0xAC00 && r <= 0xD7A3,   // 韩文音节
		r >= 0xF900 && r <= 0xFAFF,   // 兼容表意
		r >= 0xFF00 && r <= 0xFF60,   // 全角
		r >= 0x20000 && r <= 0x2FA1F: // 扩展 B 及以后
		return true
	}
	return false
}

// quotaReserveKey 是"本次请求已在配额池里预留了多少 token"的上下文键。
type quotaReserveKey struct{}

// WithQuotaReserve 把预检实际预留的量放进回合上下文。
// 预留值为 0（无池 / fail-open）时原样返回，不注入无用值。
func WithQuotaReserve(ctx context.Context, reserved int64) context.Context {
	if reserved <= 0 {
		return ctx
	}
	return context.WithValue(ctx, quotaReserveKey{}, reserved)
}

// QuotaReserveFromContext 取回预留值。返回 0 的两种情形语义相同：
// 池子里没有本次请求的任何预估 ⇒ 校正按真实用量全额计入。
func QuotaReserveFromContext(ctx context.Context) int64 {
	v, _ := ctx.Value(quotaReserveKey{}).(int64)
	return v
}

// ConsumeQuotaReserve 清掉上下文里的预留值（供多轮续跑的**第二轮之后**使用）。
//
// 预留只能扣一次：HandleSubmit 的目标续跑循环会把同一请求跑成多个回合，
// 若每轮都扣预留，池子里就凭空多退了一次预估。
func ConsumeQuotaReserve(ctx context.Context) context.Context {
	if QuotaReserveFromContext(ctx) <= 0 {
		return ctx
	}
	return context.WithValue(ctx, quotaReserveKey{}, int64(0))
}
