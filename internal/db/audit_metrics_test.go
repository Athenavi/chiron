package db

import (
	"context"
	"errors"
	"testing"

	"github.com/athenavi/chiron/internal/monitor"
	"github.com/redis/go-redis/v9"
)

// 审计写入必须有可观测信号。
//
// 背景：`AuditLog` 的失败路径此前只打一条 `slog.Warn`，而审计写入发生在异步
// worker 里、不影响任何响应。于是「审计正在成片丢失」在线上没有任何可见症状 ——
// 日志会淹没在请求日志里，`/metrics` 上也没有对应的数。合规产物静默变少，
// 比直接报错糟得多。
//
// 本文件锁住三件事：
//  1. 写入成功 → audit_log_writes 增加；
//  2. XAdd 失败 → audit_log_errors 增加，且**不**计入 writes；
//  3. Redis 未配置 → 两种计数都不动（这是正常跳过，不是故障）。
//
// 注意不要加 t.Parallel()：计数器是全局单例，并行用例会互相污染差值断言。

// auditFakeRedis 只覆写 XAdd；其余方法由内嵌接口提供（不会被调用）。
type auditFakeRedis struct {
	RedisClient
	err error
}

func (f *auditFakeRedis) XAdd(context.Context, *redis.XAddArgs) *redis.StringCmd {
	if f.err != nil {
		return redis.NewStringResult("", f.err)
	}
	return redis.NewStringResult("1-0", nil)
}

// withAuditRedis 临时替换全局 Redis，并在用例结束时还原。
func withAuditRedis(t *testing.T, r RedisClient) {
	t.Helper()
	prev := Redis
	Redis = r
	t.Cleanup(func() { Redis = prev })
}

func TestAuditLogCountsSuccessfulWrites(t *testing.T) {
	withAuditRedis(t, &auditFakeRedis{})

	beforeWrites := monitor.Global.AuditLogWrites.Load()
	beforeErrors := monitor.Global.AuditLogErrors.Load()

	AuditLog(context.Background(), "u1", 1, "session.create", "sess-1", "", "127.0.0.1", nil)

	if got := monitor.Global.AuditLogWrites.Load() - beforeWrites; got != 1 {
		t.Fatalf("audit_log_writes 增量 = %d，期望 1（写成功必须可观测）", got)
	}
	if got := monitor.Global.AuditLogErrors.Load() - beforeErrors; got != 0 {
		t.Fatalf("成功写入不该增加 audit_log_errors，实际 +%d", got)
	}
}

func TestAuditLogCountsFailedWrites(t *testing.T) {
	withAuditRedis(t, &auditFakeRedis{err: errors.New("redis down")})

	beforeWrites := monitor.Global.AuditLogWrites.Load()
	beforeErrors := monitor.Global.AuditLogErrors.Load()

	AuditLog(context.Background(), "u1", 1, "session.create", "sess-1", "", "127.0.0.1", nil)

	if got := monitor.Global.AuditLogErrors.Load() - beforeErrors; got != 1 {
		t.Fatalf("audit_log_errors 增量 = %d，期望 1（XAdd 失败必须可观测）", got)
	}
	if got := monitor.Global.AuditLogWrites.Load() - beforeWrites; got != 0 {
		t.Fatalf("失败的写入不该计入 audit_log_writes，实际 +%d", got)
	}
}

// Redis 未配置时要静默跳过 —— 桌面/单机模式没有 Redis 是正常状态，
// 不能把它算成错误，否则 audit_log_errors 会恒大于 0，告警失去意义。
func TestAuditLogSkipsWithoutRedis(t *testing.T) {
	withAuditRedis(t, nil)

	beforeWrites := monitor.Global.AuditLogWrites.Load()
	beforeErrors := monitor.Global.AuditLogErrors.Load()

	AuditLog(context.Background(), "u1", 1, "session.create", "sess-1", "", "127.0.0.1", nil)

	if got := monitor.Global.AuditLogWrites.Load() - beforeWrites; got != 0 {
		t.Fatalf("无 Redis 时不该计入 writes，实际 +%d", got)
	}
	if got := monitor.Global.AuditLogErrors.Load() - beforeErrors; got != 0 {
		t.Fatalf("无 Redis 属正常跳过，不该计入 errors，实际 +%d", got)
	}
}
