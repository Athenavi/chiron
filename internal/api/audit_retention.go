package api

// audit_retention.go —— 审计日志保留期清理（M3）。
//
// 为什么需要它：`audit_logs` 按**每个请求一行**写入（middleware 的审计 worker），
// 增长速度比通知表快一个量级，而此前没有任何清理机制 —— 表只增不减。
// 一个中等规模的部署跑一年就是数千万行，查询页的 COUNT(*) 会先变成瓶颈
// （即便命中 idx_audit_logs_tenant_created），磁盘也会持续膨胀。
//
// 三条不可动摇的约定：
//
//  1. **分批删，不包长事务**。一次 DELETE 几十万行会长时间持有行锁，
//     把并发的审计写入（每个请求都在写）堵在后面，表现为"接口突然变慢"。
//     每批是独立语句（各自独立隐式事务），锁窗口只有一批的大小。
//
//  2. **清理动作自己也要写审计**。删的是审计记录，如果不留痕，
//     事后无法回答"那批记录是谁删的、按什么策略删的"——这正是审计系统
//     最不该出现的盲区。写入内容包含保留天数、截止时间、删除行数。
//
//  3. **保留天数 <= 0 一律回落默认值**，绝不解释成"删光"。配置手滑写成 0
//     就把全部审计清掉，是不可恢复的灾难；而"少删一些"最多是磁盘多占一点。

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strconv"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/settings"
)

// settingsCategoryAudit 是"审计"配置在 system_settings 中的分类名。
//
// 与 admin_extras.go 的 settingsCategories 白名单必须同时包含 "audit"，
// 否则后台保存会被 400 拒绝（前端看得到输入框、点了保存却失败）。
const settingsCategoryAudit = "audit"

const (
	// DefaultAuditRetentionDays 默认保留天数。
	//
	// 180 天是合规与成本的折中：覆盖"上季度那次越权操作是谁做的"这类追溯，
	// 又不至于让按请求计数的审计表无限膨胀。可用 AUDIT_RETENTION_DAYS 覆盖。
	DefaultAuditRetentionDays = 180

	// auditRetentionBatchSize 单批删除行数。
	//
	// 比通知表的 1000 大：审计行更小、写入更密，2000 一批在锁窗口与轮次之间
	// 更平衡。仍然分批 —— 关键不在总数，而在于不让单条语句持有过久的锁。
	auditRetentionBatchSize = 2000

	// auditRetentionMaxBatchesPerRound 单轮上限。
	//
	// 积压严重时（例如首次开启）不应让一轮清理把连接池占住几分钟：
	// 超出上限就停下，下个周期继续。清理不需要在一个周期内追平。
	auditRetentionMaxBatchesPerRound = 50
)

// auditActionRetentionSweep 是"清理动作"自身的审计 action 名。
//
// 独立成常量而不是就地写字符串：这条记录将来会被审计页的 action 过滤命中，
// 拼写漂移会让"查所有清理记录"变成一个猜谜游戏。
const auditActionRetentionSweep = "audit.retention_sweep"

// AuditRetentionResult 是一轮清理的统计，供日志与测试断言。
type AuditRetentionResult struct {
	// Deleted 是本轮实际删除的行数。
	Deleted int
	// Batches 是实际执行的批次数。
	Batches int
	// Cutoff 是本次使用的截止时刻（早于它的记录被删）。
	Cutoff time.Time
}

// SweepAuditRetention 删除 `created_at < now()-retentionDays` 的审计记录。
//
// 返回 (result, err)：err != nil 时 result 里仍然带着"删了一部分"的统计，
// 调用方据此记录日志而不是当成"什么都没发生"。
func SweepAuditRetention(ctx context.Context, retentionDays int) (AuditRetentionResult, error) {
	var res AuditRetentionResult
	if retentionDays <= 0 {
		retentionDays = DefaultAuditRetentionDays
	}
	// created_at 是 `timestamp without time zone`，存的是写入时会话时区的墙钟。
	// 与查询/导出同一条口径：先 cast 成 timestamptz 再比较，否则本地 (+08)
	// 的墙钟会被当成 UTC，实际多删/少删 8 小时的数据。
	cutoff := time.Now().AddDate(0, 0, -retentionDays)
	res.Cutoff = cutoff

	if db.GlobalDBManager == nil {
		return res, fmt.Errorf("audit retention: database unavailable")
	}

	for i := 0; i < auditRetentionMaxBatchesPerRound; i++ {
		// 每批前检查退出：清理是低优先级后台工作，网关关闭时应尽快让出。
		if err := ctx.Err(); err != nil {
			return res, err
		}
		n, err := deleteAuditBatch(ctx, cutoff, auditRetentionBatchSize)
		if err != nil {
			return res, err
		}
		res.Batches++
		res.Deleted += n
		// 不足一批说明已删空，提前结束，省掉一次空查询。
		if n < auditRetentionBatchSize {
			return res, nil
		}
	}
	slog.Info("audit: retention sweep hit per-round batch cap; continuing next cycle",
		"deleted", res.Deleted, "batches", res.Batches)
	return res, nil
}

// deleteAuditBatch 删除一批过期记录并返回本批行数。
//
// PG 的 DELETE 不支持直接写 LIMIT，所以用「子查询选出本批 id → 按 id 删」
// 两步合在一条语句里。子查询按 created_at 排序并 LIMIT，能直接用上
// `ix_audit_logs_tenant_created` 的 created_at 部分。
func deleteAuditBatch(ctx context.Context, cutoff time.Time, batchSize int) (int, error) {
	if batchSize <= 0 {
		// 防呆：batchSize = 0 会被当成 LIMIT 0 而静默什么都不删，
		// 调用方会误以为"已经删空"。
		batchSize = auditRetentionBatchSize
	}
	tag, err := db.GlobalDBManager.Exec(ctx,
		`DELETE FROM audit_logs
		  WHERE id IN (
		        SELECT id FROM audit_logs
		         WHERE created_at::timestamptz < $1
		         ORDER BY created_at
		         LIMIT $2
		  )`,
		cutoff, batchSize)
	if err != nil {
		return 0, fmt.Errorf("audit: delete old audit logs: %w", err)
	}
	return int(tag.RowsAffected()), nil
}

// StartAuditRetentionSweep 启动周期性清理（受 ctx 控制，随网关退出）。
//
// 与 StartRetentionSweep（通知）同形：ticker + 启动即跑一次 + 错误只记日志。
// 启动即跑一次的理由是"网关重启后尽快把积压清掉"——重启越频繁，
// 清理越可能被反复推迟。
//
// `store` 可为 nil：非 nil 时**每轮**都从 `system_settings.audit` 读
// `retention_days`，于是后台改完下一轮就生效，无需重启。store 缺失时
// 回落到 `fallbackDays`（来自 AUDIT_RETENTION_DAYS）与内置默认值。
func StartAuditRetentionSweep(
	ctx context.Context, interval time.Duration, store *settings.Store, fallbackDays int,
) {
	if interval <= 0 {
		interval = 24 * time.Hour
	}
	if fallbackDays <= 0 {
		fallbackDays = DefaultAuditRetentionDays
	}
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		runAuditRetentionSweep(ctx, effectiveAuditRetentionDays(ctx, store, fallbackDays))
		slog.Info("audit retention sweep started",
			"interval", interval.String(), "retention_days", fallbackDays)
		for {
			select {
			case <-ctx.Done():
				slog.Info("audit retention sweep stopped")
				return
			case <-ticker.C:
				// 每轮重读：后台改保留天数后下一轮即生效，不必重启进程。
				runAuditRetentionSweep(ctx, effectiveAuditRetentionDays(ctx, store, fallbackDays))
			}
		}
	}()
}

// effectiveAuditRetentionDays 取当前生效的保留天数：后台配置 > env/默认值。
//
// 非法值（<=0 或非整数）一律回落 fallback，绝不解释成"删光"：
// 配置被写坏时最多"少删一些"，而误删审计是不可恢复的。
func effectiveAuditRetentionDays(ctx context.Context, store *settings.Store, fallbackDays int) int {
	if fallbackDays <= 0 {
		fallbackDays = DefaultAuditRetentionDays
	}
	if store == nil {
		return fallbackDays
	}
	cfg, err := store.LoadConfig(ctx, settingsCategoryAudit)
	if err != nil || cfg == nil {
		return fallbackDays
	}
	raw, ok := cfg["retention_days"]
	if !ok {
		return fallbackDays
	}
	var days int
	switch v := raw.(type) {
	case float64:
		days = int(v)
	case int:
		days = v
	case int64:
		days = int(v)
	case json.Number:
		if n, err := v.Int64(); err == nil {
			days = int(n)
		}
	case string:
		if n, err := strconv.Atoi(v); err == nil {
			days = n
		}
	}
	if days <= 0 {
		slog.Warn("audit retention: configured days is invalid, falling back",
			"configured", raw, "fallback_days", fallbackDays)
		return fallbackDays
	}
	return days
}

// runAuditRetentionSweep 执行一轮清理：删数据 + 把这次清理本身写进审计。
//
// 只删不写会让"谁在什么时候按什么策略删了审计"永久失联 ——
// 而那恰恰是审计系统被信任的前提。
func runAuditRetentionSweep(ctx context.Context, retentionDays int) {
	res, err := SweepAuditRetention(ctx, retentionDays)
	if err != nil {
		// ctx 取消是正常退出路径，不该报成清理失败。
		if ctx.Err() != nil {
			return
		}
		slog.Warn("audit retention sweep failed",
			"deleted", res.Deleted, "batches", res.Batches, "error", err)
		// 即使失败也要留痕：部分删除同样改变了数据。
		recordRetentionSweep(ctx, retentionDays, res, err)
		return
	}
	if res.Deleted == 0 {
		// 没删任何东西就不写审计：每天一条 "deleted: 0" 会在审计表里
		// 制造与业务无关的噪音，反而掩盖真正的清理记录。
		return
	}
	slog.Info("audit retention sweep done",
		"deleted", res.Deleted, "batches", res.Batches, "retention_days", retentionDays)
	recordRetentionSweep(ctx, retentionDays, res, nil)
}

// recordRetentionSweep 把这次清理写入审计（失败只记日志）。
//
// 用 db.AuditLog（Redis Stream → PG sink）而不是直接 INSERT：
// 与其它审计事件走同一条写入路径，避免出现"清理记录绕过了审计管道"
// 这种只在排障时才会发现的差异。
func recordRetentionSweep(ctx context.Context, retentionDays int, res AuditRetentionResult, sweepErr error) {
	detail := fmt.Sprintf("deleted %d rows in %d batches, cutoff %s, retention %d days",
		res.Deleted, res.Batches, res.Cutoff.UTC().Format(time.RFC3339), retentionDays)
	if sweepErr != nil {
		detail += "; error: " + sweepErr.Error()
	}
	// tenant_id 用系统租户（DefaultTenantID）：清理是平台级动作，不属于任何租户。
	// 这样在按租户查看审计时它不会混进某个租户的记录，而在平台视图里可见。
	db.AuditLog(ctx, "", DefaultTenantID, auditActionRetentionSweep, "audit_logs", detail, "", map[string]interface{}{
		"retention_days": retentionDays,
		"deleted":        res.Deleted,
		"batches":        res.Batches,
		"cutoff":         res.Cutoff.UTC().Format(time.RFC3339),
	})
}
