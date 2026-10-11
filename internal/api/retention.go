package api

import (
	"context"
	"log/slog"
	"os"
	"strconv"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

// ── 保留策略（A7/C1）──
//
// turns / task_idempotency 这类"每回合/每任务写一行"的表如果不清理，企业化长期运行会
// 持续膨胀（turns ≈ 每轮对话一行，task_idempotency ≈ 每个后台任务一行）。
//
// 分工：本文件清理 turns（网关写入）；task_idempotency 由引擎侧同类清理负责
// （python-engine/app/queue/idempotency.py 的 purge_older_than）。
//
// 可配置：TURN_RETENTION_DAYS（默认 30）、RETENTION_INTERVAL_HOURS（默认 6）。

const (
	retentionIntervalDefault = 6 * time.Hour
	turnRetentionDaysDefault = 30
	retentionTimeout         = 30 * time.Second
	// retentionLockTTL 跨实例互斥键的 TTL：远大于单次清理超时（retentionTimeout），
	// 持有者崩溃后最多等这么久即可重新参与。
	retentionLockTTL = 10 * time.Minute
)

// retentionLockKey 保留清理的跨实例互斥键。
//
// 清理本身是幂等 DELETE（第二次删 0 行），多实例同时跑不会出错；但 N 个实例各扫
// 一遍 turns 表是纯浪费，并发 DELETE 还会争行锁。这里与 cron_scheduler 的租约做法
// 保持一致：同一时刻只让一个实例执行。
var retentionLockKey = db.RedisKey("retention:lock")

// retentionLockAcquireLua 抢清理互斥（SET NX + EX），与 session_coord.go 的
// sessionRunLockAcquireLua 同形。
const retentionLockAcquireLua = `
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) then
  return 1
end
return 0
`

// tryAcquireRetentionLock 抢清理互斥；Redis 不可用时返回 ok=true —— 退回"每个实例
// 都跑一遍"。清理是幂等的，安全只是浪费，不该因 Redis 抖动就不清理。
func tryAcquireRetentionLock(ctx context.Context) (release func(), ok bool) {
	if db.Redis == nil {
		return func() {}, true
	}
	res := db.Redis.Eval(ctx, retentionLockAcquireLua,
		[]string{retentionLockKey}, "1", int(retentionLockTTL.Seconds()))
	if err := res.Err(); err != nil {
		slog.Debug("retention lock acquire failed, skipping this round", "error", err)
		return nil, false
	}
	if n, err := res.Int(); err != nil || n != 1 {
		return nil, false
	}
	return func() {
		// 不做 CAS 释放：清理幂等，最坏情况是锁被 TTL 回收后另一实例重复跑一轮
		// （0 行受影响）。为这点风险引入 token 不值得。
		_ = db.Redis.Del(context.Background(), retentionLockKey).Err()
	}, true
}

// StartRetentionCleaner 启动 turns 保留策略清理循环（受 lifecycleCtx 控制）。
func StartRetentionCleaner(ctx context.Context) {
	if db.Pool == nil {
		return
	}
	days := turnRetentionDays()
	interval := retentionInterval()
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		slog.Info("turn retention cleaner started",
			"retention_days", days, "interval_hours", int(interval.Hours()))
		for {
			select {
			case <-ctx.Done():
				slog.Info("turn retention cleaner stopped")
				return
			case <-ticker.C:
				release, ok := tryAcquireRetentionLock(ctx)
				if !ok {
					slog.Debug("retention purge skipped: another instance is running it")
					continue
				}
				purgeFinishedTurns(ctx, days)
				release()
			}
		}
	}()
}

func turnRetentionDays() int {
	if v := os.Getenv("TURN_RETENTION_DAYS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	return turnRetentionDaysDefault
}

func retentionInterval() time.Duration {
	if v := os.Getenv("RETENTION_INTERVAL_HOURS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return time.Duration(n) * time.Hour
		}
	}
	return retentionIntervalDefault
}

// purgeFinishedTurns 删除保留期外已完结的回合。
// running 状态不删（避免误删进行中的现场）；created_at 在模型里生成为 VARCHAR(255)
// （写入值是 PG NOW() 的文本形式），因此显式 ::timestamptz 解析——解析失败只会让本次
// 清理报错并留下告警，不影响主链路。
func purgeFinishedTurns(ctx context.Context, days int) {
	cctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), retentionTimeout)
	defer cancel()
	// created_at 是 VARCHAR(255)，存的是 PG NOW() 的文本形式。**不要**写成
	// `created_at::timestamptz`：表达式上的强转会让索引失效（且 text→timestamptz
	// 只是 STABLE，无法建表达式索引），于是每 6h 全表扫 turns（P1）。
	// 改为把右侧也转成同样的文本形式、左侧保持裸列 ⇒ 可走 0009 迁移补的
	// ix_turns_created_at 做范围扫。
	tag, err := db.Pool.Exec(cctx,
		`DELETE FROM turns
		 WHERE status <> 'running'
		   AND created_at < ((NOW() - make_interval(days => $1))::text)`,
		days)
	if err != nil {
		slog.Warn("turn retention purge failed", "retention_days", days, "error", err)
		return
	}
	if n := tag.RowsAffected(); n > 0 {
		slog.Info("turn retention purge done", "removed", n, "retention_days", days)
	}
}
