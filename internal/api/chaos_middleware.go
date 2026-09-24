package api

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

// ── 混沌工程注入中间件（网关请求路径）────────────────────────────────────────
//
// 与 “python-engine/app/chaos/injector.py“ 的 ASGI 中间件**同构**：读同一份 Redis
// 缓存（“chaos:active:<tenant>“，键见 “chaosActiveKey“），命中 “target=gateway“
// 的活跃实验就施加 latency / error；否则原样放行。
//
// **默认关闭**：“CHAOS_ENABLED != "true"“ 时第一个判断就放行，连 Redis 都不读。
//
// 为什么挂在**最外层**（“publicMW“ 之外）：注入模拟的是基础设施/整站故障，未认证
// 请求也应该被影响；实验的**创建**才需要 “chaos:manage“ 权限。租户身份取
// “ResolveTenantID“（无 claims 时回落到 “DefaultTenantID“）。
//
// **失败安全**：缓存不可用、JSON 坏掉、DB 查询失败等一律放行 —— 混沌工程自身出故障时
// 绝不能反过来把正常流量打挂。
const (
	// chaosMaxInjectMS 单次注入的时长上限（毫秒），防止一个实验把请求挂死到连接超时。
	chaosMaxInjectMS = 10_000
	// chaosCacheTTL 与 Python 侧 injector.ACTIVE_TTL_SECONDS 保持一致。
	chaosCacheTTL = 5 * time.Second
)

type chaosFault struct {
	ID         string          `json:"id"`
	FaultType  string          `json:"fault_type"`
	Target     string          `json:"target"`
	DurationMs int             `json:"duration_ms"`
	Intensity  float64         `json:"intensity"`
	Config     json.RawMessage `json:"config"`
}

// chaosEnabled 与 Python 侧的 settings.chaos_enabled 同义（同一个环境变量）。
func chaosEnabled() bool {
	return os.Getenv("CHAOS_ENABLED") == "true"
}

// ChaosMiddleware 在网关请求路径上施加混沌故障（target=gateway）。
func ChaosMiddleware(redis db.RedisClient) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if !chaosEnabled() || redis == nil {
				next.ServeHTTP(w, r)
				return
			}

			fault := pickGatewayFault(r.Context(), redis, ResolveTenantID(r))
			if fault == nil {
				next.ServeHTTP(w, r)
				return
			}

			if fault.FaultType == "error" {
				code := chaosErrorCode(fault.Config)
				slog.Info("chaos: injecting error", "code", code,
					"path", r.URL.Path, "experiment", fault.ID)
				w.Header().Set("X-Chaos-Injected", fault.ID)
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(code)
				_, _ = w.Write([]byte(
					`{"detail":"chaos experiment injected this failure","chaos_experiment_id":"` +
						fault.ID + `"}`))
				return
			}

			// latency / timeout：先延迟再交给下游（timeout 按同一预算封顶）
			ms := fault.DurationMs
			if ms > chaosMaxInjectMS {
				ms = chaosMaxInjectMS
			}
			delay := time.Duration(float64(ms)*fault.Intensity) * time.Millisecond
			if fault.FaultType == "timeout" {
				delay = time.Duration(ms) * time.Millisecond
			}
			if delay > 0 {
				slog.Info("chaos: injecting delay", "type", fault.FaultType,
					"ms", delay.Milliseconds(), "path", r.URL.Path, "experiment", fault.ID)
				w.Header().Set("X-Chaos-Injected", fault.ID)
				select {
				case <-time.After(delay):
				case <-r.Context().Done():
					// 客户端已经走了 —— 别再占用资源等下去
					return
				}
			}
			next.ServeHTTP(w, r)
		})
	}
}

// chaosErrorCode 从实验 config 里取 error_code，落在合法 5xx 之外时回退 503。
func chaosErrorCode(raw json.RawMessage) int {
	var cfg struct {
		ErrorCode int `json:"error_code"`
	}
	if len(raw) == 0 || json.Unmarshal(raw, &cfg) != nil {
		return http.StatusServiceUnavailable
	}
	if cfg.ErrorCode < 400 || cfg.ErrorCode > 599 {
		return http.StatusServiceUnavailable
	}
	return cfg.ErrorCode
}

// pickGatewayFault 取一个作用于网关的活跃故障；读缓存未命中时回源数据库并回填。
//
// 回源是必要的：纯网关流量（不经过 Python 引擎）不会触发 Python 侧回填，没有回源就
// 只能靠 TTL 内的旧值 —— 新建的实验会"看起来创建成功、却始终不生效"。
func pickGatewayFault(ctx context.Context, redis db.RedisClient, tenantID string) *chaosFault {
	if tenantID == "" {
		return nil
	}

	faults, ok := chaosCachedFaults(ctx, redis, tenantID)
	if !ok {
		faults = chaosLoadFaultsFromDB(ctx, tenantID)
		if faults == nil {
			return nil
		}
		chaosCacheFaults(ctx, redis, tenantID, faults)
	}

	for i := range faults {
		if faults[i].Target == "gateway" && chaosSupportedFaults[faults[i].FaultType] {
			return &faults[i]
		}
	}
	return nil
}

// chaosCachedFaults 读缓存。ok=false 表示"需要回源"（miss 或读失败）。
func chaosCachedFaults(ctx context.Context, redis db.RedisClient, tenantID string) ([]chaosFault, bool) {
	raw, err := redis.Get(ctx, chaosActiveKey(tenantID)).Result()
	if err != nil || raw == "" {
		return nil, false
	}
	var faults []chaosFault
	if err := json.Unmarshal([]byte(raw), &faults); err != nil {
		slog.Debug("chaos: bad cache payload, will reload from db", "error", err)
		return nil, false
	}
	return faults, true
}

// chaosCacheFaults 回填缓存（含"空列表"—— 否则每个请求都要打一次库）。
func chaosCacheFaults(ctx context.Context, redis db.RedisClient, tenantID string, faults []chaosFault) {
	payload, err := json.Marshal(faults)
	if err != nil {
		return
	}
	if err := redis.Set(ctx, chaosActiveKey(tenantID), payload, chaosCacheTTL).Err(); err != nil {
		slog.Debug("chaos: cache backfill failed", "error", err)
	}
}

// chaosLoadFaultsFromDB 回源数据库；失败返回 nil（= 不注入）。
func chaosLoadFaultsFromDB(ctx context.Context, tenantID string) []chaosFault {
	pool := db.ReadPool()
	if pool == nil {
		pool = db.Pool
	}
	if pool == nil {
		return nil
	}

	rows, err := pool.Query(ctx, `
		SELECT id, fault_type, target, duration_ms, intensity, config::text
		  FROM ent_chaos_experiments
		 WHERE tenant_id = $1 AND status IN ('pending', 'running')`, tenantID)
	if err != nil {
		slog.Debug("chaos: db load failed, not injecting", "error", err)
		return nil
	}
	defer rows.Close()

	faults := make([]chaosFault, 0, 4)
	for rows.Next() {
		var f chaosFault
		var cfg string
		if err := rows.Scan(&f.ID, &f.FaultType, &f.Target, &f.DurationMs, &f.Intensity, &cfg); err != nil {
			slog.Debug("chaos: db scan failed, not injecting", "error", err)
			return nil
		}
		f.Config = json.RawMessage(cfg)
		faults = append(faults, f)
	}
	if err := rows.Err(); err != nil {
		slog.Debug("chaos: db iterate failed, not injecting", "error", err)
		return nil
	}
	return faults
}
