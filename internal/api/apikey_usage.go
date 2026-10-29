package api

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

// apiKeyLastUsedMinGap 是同一把 key 两次写库之间的最小间隔。
//
// 为什么不每次都写：API Key 鉴权是**每个请求**都会走到的路径，而 `last_used_at`
// 只是给人看的运维信息（"这把 key 还在用吗、能不能安全吊销"）。每个请求都
// UPDATE 一次，等于给整条鉴权链路加一次写放大 —— 高频调用的 key 会持续产生
// 只写同一行的 UPDATE，既浪费 I/O 也会让 autovacuum 跟着忙。
//
// 一分钟的粒度足够回答那个问题：用户看到"3 分钟前用过"和"3 秒前用过"，
// 对决策没有任何差别。
const apiKeyLastUsedMinGap = time.Minute

// apiKeyUsageTrackerMaxEntries 是跟踪表的容量上限。
//
// `last` 的键是 key id，正常情况下规模等于"活跃 key 数"（每用户上限 50 把），
// 不会无限增长。但进程长跑时用户可能反复创建/吊销，此时键会累积 ——
// 到上限就顺手清理已经超过间隔的条目，避免成为内存泄漏点。
const apiKeyUsageTrackerMaxEntries = 4096

// apiKeyUsageTracker 决定"这次请求要不要回写 last_used_at"。
//
// 抽成独立类型而不是塞进 `AuthMiddleware` 的闭包，是为了能用一个假时钟直接
// 单测节流行为 —— 仓库的单元测试不允许联网，也不该依赖真实时间流逝。
//
// 并发安全：鉴权中间件是 http 主流程，同一把 key 的并发请求会同时到达。
type apiKeyUsageTracker struct {
	mu     sync.Mutex
	minGap time.Duration
	// now 可注入：测试里换成假时钟，生产用 time.Now
	now  func() time.Time
	last map[string]time.Time
}

func newAPIKeyUsageTracker(minGap time.Duration) *apiKeyUsageTracker {
	return &apiKeyUsageTracker{
		minGap: minGap,
		now:    time.Now,
		last:   make(map[string]time.Time),
	}
}

// allow 报告本次是否应该写库，并就地记下这次写入的时间。
//
// 语义是"先占坑再写"：返回 true 的那一刻就更新时间戳，因此即使后续写库失败，
// 也要等满一个间隔才会重试 —— 写失败通常是库不可用，此时每个请求都去撞一次
// 只会把故障放大。
func (t *apiKeyUsageTracker) allow(keyID string) bool {
	if keyID == "" {
		return false
	}
	now := t.now()

	t.mu.Lock()
	defer t.mu.Unlock()

	if prev, ok := t.last[keyID]; ok && now.Sub(prev) < t.minGap {
		return false
	}
	if len(t.last) >= apiKeyUsageTrackerMaxEntries {
		t.evictLocked(now)
	}
	t.last[keyID] = now
	return true
}

// evictLocked 清理已经过了节流窗口的条目。调用方必须持有 t.mu。
//
// 只清"本来就可以再写"的条目：它们即使被删掉，下次请求也只是多写一次库，
// 不会改变可观察行为。
func (t *apiKeyUsageTracker) evictLocked(now time.Time) {
	for id, at := range t.last {
		if now.Sub(at) >= t.minGap {
			delete(t.last, id)
		}
	}
}

// apiKeyUsage 是进程级单例：节流状态必须跨请求共享，否则每个请求都算"第一次"，
// 节流等于没做。
var apiKeyUsage = newAPIKeyUsageTracker(apiKeyLastUsedMinGap)

// apiKeyLastUsedTimeout 限制这次后台写入的耗时。
//
// 单独开 context 而不是复用 `r.Context()`：请求可能在这条 UPDATE 跑完前就返回了，
// 复用会让写入被取消 —— 那正是我们最需要留下痕迹的时候（比如一次很快的 401）。
const apiKeyLastUsedTimeout = 3 * time.Second

// markAPIKeyUsed 按节流回写 `api_keys.last_used_at`。
//
// 三件事刻意如此：
//
//  1. **同步**而不是 `go func()`：节流后每个 key 每分钟最多一次，一次 UPDATE
//     的代价相对整个请求可以忽略；换成 goroutine 反而要处理"请求结束时它还在写"
//     的生命周期问题，以及高频创建/吊销下的 goroutine 堆积。
//  2. **失败只告警**：`last_used_at` 是给人看的运维信息，写不进去绝不能让一个
//     本来合法的请求失败。库不可用时这里会每分钟撞一次，日志量可控。
//  3. **先占坑再写**：见 `allow` 的注释 —— 失败也等满一个间隔再重试。
func markAPIKeyUsed(keyID string) {
	if !apiKeyUsage.allow(keyID) {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), apiKeyLastUsedTimeout)
	defer cancel()
	if _, err := db.GlobalDBManager.Exec(ctx,
		apiKeyMarkUsedQuery, keyID); err != nil {
		slog.Warn("update api key last_used_at failed", "key_id", keyID, "error", err)
	}
}
