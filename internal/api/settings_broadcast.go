package api

import (
	"context"
	"encoding/json"
	"log/slog"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/settings"
)

// ── 系统设置变更跨副本广播（批 B-2′）──
//
// 背景：后台「系统设置」保存到 DB 后，只有收到保存请求的那个网关副本会立即生效
// （rate_limit 热更、redis 热换），其它副本沿用启动快照，造成多副本配置不一致。
//
// 方案：保存成功 → Publish {prefix}chiron:settings:changed；每网关副本的订阅者：
//   - rate_limit → 令牌桶限流器 Configure（即时生效）
//   - cors       → 运行时 CORS 白名单更新（即时生效）
//   - payment    → 支付凭据热重载 + 重建渠道客户端（即时生效）
//   - 其余分类   → slog.Warn 提示需滚动重启（如 redis 集群切换/存储后端等高风险项）

type settingsChangedEvent struct {
	Category string                 `json:"category"`
	Config   map[string]interface{} `json:"config"`
	Ts       int64                  `json:"ts"` // 毫秒时间戳，用于丢弃重启窗口的过期消息
}

// settingsStaleWindow 超过该时长的广播视为过期（实例长时间暂停恢复后不应重复应用旧配置）。
const settingsStaleWindow = 10 * time.Second

func settingsChangedChannel() string {
	return db.RedisKey("chiron:settings:changed")
}

// PublishSettingsChanged 后台保存「系统设置」成功后广播到所有网关副本。
func PublishSettingsChanged(ctx context.Context, category string, config map[string]interface{}) error {
	if db.Redis == nil {
		return nil // Redis 不可用：跳过广播（保存方为单副本，退化为现状行为）
	}
	ev := settingsChangedEvent{Category: category, Config: config, Ts: time.Now().UnixMilli()}
	data, err := json.Marshal(ev)
	if err != nil {
		return err
	}
	return db.Redis.Publish(ctx, settingsChangedChannel(), data).Err()
}

// StartSettingsSubscriber 每网关副本启动一次，消费系统设置变更广播。
// billingHandler 用于 payment 分类的热重载，store 为其回源句柄；任一为 nil 时
// 该分类退化为仅告警。
func StartSettingsSubscriber(ctx context.Context, rdb db.RedisClient, limiter *DistributedRateLimiter, billingHandler *BillingHandler, store *settings.Store) {
	if rdb == nil {
		return
	}
	pubsub := rdb.Subscribe(context.Background(), settingsChangedChannel())
	go func() {
		defer func() {
			if r := recover(); r != nil {
				slog.Error("settings subscriber panic", "panic", r)
			}
		}()
		for msg := range pubsub.Channel() {
			var ev settingsChangedEvent
			if err := json.Unmarshal([]byte(msg.Payload), &ev); err != nil {
				slog.Warn("settings broadcast unmarshal failed", "error", err)
				continue
			}
			if ev.Ts > 0 && time.Since(time.UnixMilli(ev.Ts)) > settingsStaleWindow {
				continue
			}
			switch ev.Category {
			case "rate_limit":
				if limiter == nil {
					continue
				}
				limiter.Configure(
					intFromValue(ev.Config["global"], 0),
					intFromValue(ev.Config["tenant"], 0),
					intFromValue(ev.Config["user"], 0),
				)
				slog.Info("rate limiter hot-reloaded via settings broadcast")
			case "cors":
				if v, ok := ev.Config["origins"].(string); ok {
					SetCORSAllowOrigin(v)
					slog.Info("cors allowlist hot-reloaded via settings broadcast", "origins", v)
				}
			case "payment":
				if billingHandler == nil {
					continue
				}
				// 从 DB 重载（与保存方同一事实源），副本间不会漂移
				billingHandler.ReloadPaymentConfig(ctx, store)
				slog.Info("payment config hot-reloaded via settings broadcast")
			default:
				// redis/storage/s3/agent 等：涉及进程级组件（连接池/存储后端），
				// 热切高风险，提示滚动重启以消除副本间不一致
				slog.Warn("system settings changed on category; rolling restart required to apply",
					"category", ev.Category)
			}
		}
	}()
}
