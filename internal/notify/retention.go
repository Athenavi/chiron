package notify

import (
	"context"
	"log/slog"
	"time"
)

// retention.go —— 通知表保留期清理（T2）。
//
// 为什么需要它：`notifications` 此前只增不减。四类事件里 `quota_changed` 会按
// 租户 owner/admin 数量成倍写入，时间一长这张表就成了最大的死重量 ——
// 既不会被看（超过三个月的通知没人翻），也不会自己消失。

const (
	// DefaultRetentionDays 默认保留天数。
	//
	// 90 天是个折中：够覆盖"上季度那笔扣费到底怎么回事"这类对账追溯，又不至于
	// 让通知表无限膨胀。可用配置覆盖。
	DefaultRetentionDays = 90

	// retentionBatchSize 单批删除行数。
	//
	// 一次删太多会长时间持有行锁，把并发的支付回调 / 配额变更写入堵在后面；
	// 太小则轮次太多。1000 是两者之间的经验值。
	retentionBatchSize = 1000

	// retentionMaxBatchesPerRound 单轮上限，防止某次积压把一轮扫描拖成分钟级。
	//
	// 超出上限时本轮停下、下个周期继续 —— 清理不需要在一个周期内追上，
	// 但**不能**让它占用连接池太久。
	retentionMaxBatchesPerRound = 50
)

// RetentionResult 是一轮清理的统计，供日志与测试断言。
type RetentionResult struct {
	// Deleted 是本轮实际删除的行数。
	Deleted int
	// Batches 是实际执行的批次数。
	Batches int
}

// SweepRetention 删除 `created_at < now()-retentionDays` 的通知，分批进行。
//
// **已读与未读一视同仁**：90 天没点开过的通知，保留下去既不会被看也不会被清理，
// 等于永久垃圾；"怕丢重要通知"的诉求应由"重要事件同时发邮件"承担，
// 而不是靠永不删除。
func (m *Manager) SweepRetention(ctx context.Context, retentionDays int) (RetentionResult, error) {
	var res RetentionResult
	if retentionDays <= 0 {
		retentionDays = DefaultRetentionDays
	}
	cutoff := time.Now().AddDate(0, 0, -retentionDays)

	for i := 0; i < retentionMaxBatchesPerRound; i++ {
		// 每批前检查退出：清理是低优先级后台工作，网关关闭时应尽快让出。
		if err := ctx.Err(); err != nil {
			return res, err
		}
		n, err := m.store.DeleteOlderThan(ctx, cutoff, retentionBatchSize)
		if err != nil {
			// 返回已删行数与错误：调用方据此记录"删了一部分就失败了"。
			return res, err
		}
		res.Batches++
		res.Deleted += n
		// 不足一批说明已删空，提前结束，省掉一次空查询。
		if n < retentionBatchSize {
			return res, nil
		}
	}
	slog.Info("notify: retention sweep hit per-round batch cap; continuing next cycle",
		"deleted", res.Deleted, "batches", res.Batches)
	return res, nil
}

// StartRetentionSweep 启动周期性保留期清理（受 ctx 控制，随网关退出）。
//
// 与 StartEntitlementExpiryScan 同形：ticker + 启动即跑一次 + 错误只记日志。
// 启动即扫一次的理由不同 —— 那里是"别让窗口内的权益等一个周期"，这里是
// "网关重启后尽快把积压清掉"，否则重启越频繁，清理越可能被反复推迟。
func (m *Manager) StartRetentionSweep(ctx context.Context, interval time.Duration, retentionDays int) {
	if interval <= 0 {
		interval = 24 * time.Hour
	}
	if retentionDays <= 0 {
		retentionDays = DefaultRetentionDays
	}
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		m.runRetentionSweep(ctx, retentionDays)
		slog.Info("notification retention sweep started",
			"interval", interval.String(), "retention_days", retentionDays)
		for {
			select {
			case <-ctx.Done():
				slog.Info("notification retention sweep stopped")
				return
			case <-ticker.C:
				m.runRetentionSweep(ctx, retentionDays)
			}
		}
	}()
}

// runRetentionSweep 执行一轮清理并记录统计；错误只记日志（下一轮会重试）。
func (m *Manager) runRetentionSweep(ctx context.Context, retentionDays int) {
	res, err := m.SweepRetention(ctx, retentionDays)
	if err != nil {
		// ctx 取消是正常退出路径，不该报成清理失败。
		if ctx.Err() != nil {
			return
		}
		slog.Warn("notification retention sweep failed",
			"deleted", res.Deleted, "batches", res.Batches, "error", err)
		return
	}
	if res.Deleted > 0 {
		slog.Info("notification retention sweep done",
			"deleted", res.Deleted, "batches", res.Batches, "retention_days", retentionDays)
	}
}
