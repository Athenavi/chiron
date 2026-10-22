package api

import (
	"context"
	"os"
	"testing"

	"github.com/athenavi/chiron/internal/db"
	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
)

// 真实 Redis 用例：会话运行锁的互斥 / 续期 / 释放语义。
//
// 这把锁是「同一会话绝不并发两个 runtime」的唯一跨实例保证（DSH 这类本地单用户 harness
// 结构上不存在这个维度，见 内部差距分析 切口 #1）。此前**没有任何用例**；
// 而它的拒绝路径（429）曾经忘记释放锁，把会话白锁 5 分钟 —— 见 routes_agent.go 两处
// `releaseRun()` 与 agent_followup.go 的心跳接线。
//
// 取值顺序与既有 `*_live_test.go` 一致：CHIRON_TEST_REDIS_ADDR 优先，否则 REDIS_URL；都没有则 skip。
func withLiveRedis(t *testing.T) {
	t.Helper()
	addr := os.Getenv("CHIRON_TEST_REDIS_ADDR")
	password, dbIndex := "", 0
	if addr == "" {
		raw := os.Getenv("REDIS_URL")
		if raw == "" {
			t.Skip("CHIRON_TEST_REDIS_ADDR / REDIS_URL 未设置：跳过真实 Redis 用例")
		}
		opts, err := redis.ParseURL(raw)
		if err != nil {
			t.Fatalf("REDIS_URL 解析失败: %v", err)
		}
		addr, password, dbIndex = opts.Addr, opts.Password, opts.DB
	}
	client, err := db.NewSingleRedis(addr, password, dbIndex, 4)
	if err != nil {
		t.Fatalf("连接 Redis（%s）失败: %v", addr, err)
	}
	if err := client.Ping(context.Background()).Err(); err != nil {
		_ = client.Close()
		t.Fatalf("Redis 不可达（%s）: %v", addr, err)
	}
	previous := db.Redis
	db.Redis = client
	t.Cleanup(func() {
		db.Redis = previous
		_ = client.Close()
	})
}

func TestLiveSessionRunLockExcludesAndReleaseFrees(t *testing.T) {
	withLiveRedis(t)
	sid := "pytest-lock-" + uuid.NewString()
	ctx := context.Background()

	release, ok, err := AcquireSessionRunLock(ctx, sid, "token-a")
	if err != nil || !ok || release == nil {
		t.Fatalf("第一次获取应成功：ok=%v err=%v release_nil=%v", ok, err, release == nil)
	}
	if _, ok2, err2 := AcquireSessionRunLock(ctx, sid, "token-b"); err2 != nil || ok2 {
		t.Fatalf("锁被持有时其它 token 不得获取：ok=%v err=%v", ok2, err2)
	}

	// 「拒绝路径必须放锁」的机制证据：release 之后同一会话**立即可**再次获取，
	// 而不是被一把空锁挡到 TTL（5min）到期。
	release()
	if _, ok3, err3 := AcquireSessionRunLock(ctx, sid, "token-b"); err3 != nil || !ok3 {
		t.Fatalf("释放后必须能重新获取：ok=%v err=%v", ok3, err3)
	}
}

func TestLiveSessionRunLockRefreshIsTokenScoped(t *testing.T) {
	withLiveRedis(t)
	sid := "pytest-lock-refresh-" + uuid.NewString()
	ctx := context.Background()

	release, ok, err := AcquireSessionRunLock(ctx, sid, "token-a")
	if err != nil || !ok {
		t.Fatalf("获取失败：ok=%v err=%v", ok, err)
	}
	defer release()

	if state, err := RefreshSessionRunLock(ctx, sid, "token-b"); err != nil || state == "renewed" {
		t.Fatalf("非持有者不得续期（否则旧 run 能把已易主的锁无限续下去）：state=%s err=%v", state, err)
	}
	if state, err := RefreshSessionRunLock(ctx, sid, "token-a"); err != nil || state != "renewed" {
		t.Fatalf("持有者必须能续期：state=%s err=%v", state, err)
	}
	ttl, err := db.Redis.Do(ctx, "TTL", agentRunLockPrefix+sid).Int64()
	if err != nil {
		t.Fatalf("TTL: %v", err)
	}
	if ttl <= 0 || ttl > int64(agentRunLockTTL.Seconds())+2 {
		t.Fatalf("锁必须带 ≤%v 的 TTL，实际 %ds（崩溃后要能自动过期）", agentRunLockTTL, ttl)
	}
}

// 心跳的"单向终止"缺陷回归：Redis 抖动导致锁中途过期后，仍存活的持有者
// 必须能安全夺回（NX）；而锁已被新 run 接管的场合必须报 lost、绝不抢活人的锁。
func TestLiveSessionRunLockReclaimsExpiredLock(t *testing.T) {
	withLiveRedis(t)
	sid := "pytest-lock-reclaim-" + uuid.NewString()
	ctx := context.Background()

	release, ok, err := AcquireSessionRunLock(ctx, sid, "token-a")
	if err != nil || !ok {
		t.Fatalf("获取失败：ok=%v err=%v", ok, err)
	}
	defer release()

	if err := db.Redis.Del(ctx, agentRunLockPrefix+sid).Err(); err != nil {
		t.Fatalf("Del: %v", err)
	}
	if state, err := RefreshSessionRunLock(ctx, sid, "token-a"); err != nil || state != "renewed" {
		t.Fatalf("消失的锁应被原持有者夺回：state=%s err=%v", state, err)
	}
	v, err := db.Redis.Get(ctx, agentRunLockPrefix+sid).Result()
	if err != nil || v != "token-a" {
		t.Fatalf("夺回后锁仍属原 token：v=%q err=%v", v, err)
	}

	// 新 run 已接管：旧心跳不得抢走，也不得误报续期成功
	if err := db.Redis.Del(ctx, agentRunLockPrefix+sid).Err(); err != nil {
		t.Fatalf("Del: %v", err)
	}
	if _, ok2, err2 := AcquireSessionRunLock(ctx, sid, "token-b"); err2 != nil || !ok2 {
		t.Fatalf("token-b 获取失败：ok=%v err=%v", ok2, err2)
	}
	if state, err := RefreshSessionRunLock(ctx, sid, "token-a"); err != nil || state != "lost" {
		t.Fatalf("锁已被其它 token 持有时必须报 lost：state=%s err=%v", state, err)
	}
	_ = releaseSessionRunLock(ctx, agentRunLockPrefix+sid, "token-b")
}

func TestLiveSessionRunLockReleaseIsCompareAndDelete(t *testing.T) {
	withLiveRedis(t)
	sid := "pytest-lock-cas-" + uuid.NewString()
	ctx := context.Background()

	release, ok, err := AcquireSessionRunLock(ctx, sid, "token-a")
	if err != nil || !ok {
		t.Fatalf("获取失败：ok=%v err=%v", ok, err)
	}
	defer release()

	// 非持有者的释放不得删掉现任租约（CAS：防旧 run 误删新 run 的锁）
	if err := releaseSessionRunLock(ctx, agentRunLockPrefix+sid, "token-b"); err != nil {
		t.Fatalf("releaseSessionRunLock: %v", err)
	}
	if _, ok2, _ := AcquireSessionRunLock(ctx, sid, "token-c"); ok2 {
		t.Fatal("非持有者的 release 误删了锁")
	}
}
