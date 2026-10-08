package engine

import (
	"context"
	"encoding/json"
	"os"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
)

// session → 引擎实例归属（`engine:run:*`）的契约与读取路径。
//
// 为什么这块必须有测试：归属映射是"审批/取消要打到**持有该 run 的那个实例**"的唯一依据
// （多副本差异化能力，见 docs/dsh-gap-analysis.md 切口 #1）。它**读失败不报错**，
// 而是静默回退一致性哈希 —— 于是字段名漂移、键前缀不一致、垃圾记录这类问题
// 都不会有异常，只会表现为"审批偶尔落到错实例"。本项目已经吃过一次同类亏
// （`subagent_cancel_test.go` 里那条逐字契约用例），这里把 run 归属补齐。

// 字段名必须与 python-engine/app/run_registry.py 的 _payload() 逐字一致。
func TestRunRecordContractMatchesEnginePayload(t *testing.T) {
	raw := `{"instance_id":"eng-1","run_token":"rt-1","owner_uid":"u-1",` +
		`"url":"http://eng-1:8000","started_at":"2026-10-08T00:00:00Z",` +
		`"last_seen":"2026-10-08T00:00:00Z"}`
	var rec runRecord
	if err := json.Unmarshal([]byte(raw), &rec); err != nil {
		t.Fatalf("unmarshal engine payload: %v", err)
	}
	if rec.InstanceID != "eng-1" || rec.RunToken != "rt-1" ||
		rec.OwnerUID != "u-1" || rec.URL != "http://eng-1:8000" {
		t.Fatalf("归属字段漂移会让路由静默回退哈希：%+v", rec)
	}
}

func TestRunOwnerWithoutRedis(t *testing.T) {
	previous := affinityRedisClient()
	SetAffinityRedis(nil)
	t.Cleanup(func() { SetAffinityRedis(previous) })

	ctx := context.Background()
	if _, ok := RunOwner(ctx, "s1"); ok {
		t.Fatal("无 Redis 必须 ok=false（调用方回退哈希）")
	}
	if _, ok := RunOwner(ctx, ""); ok {
		t.Fatal("空 session 必须 ok=false")
	}
	if _, _, ok := RunOwnerURL(ctx, "s1"); ok {
		t.Fatal("无 Redis 时 RunOwnerURL 必须 ok=false")
	}
	if _, ok := SubagentRunOwner(ctx, "rs_1"); ok {
		t.Fatal("无 Redis 时子 Agent 归属必须 ok=false（= 无人认领的明确结论）")
	}
}

func withAffinityRedis(t *testing.T) db.RedisClient {
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
	previous := affinityRedisClient()
	SetAffinityRedis(client)
	t.Cleanup(func() {
		SetAffinityRedis(previous)
		_ = client.Close()
	})
	return client
}

func TestLiveRunOwnerPrefersRecordURLThenInstanceRegistry(t *testing.T) {
	rdb := withAffinityRedis(t)
	ctx := context.Background()
	sid := "pytest-affinity-" + uuid.NewString()
	key := db.RedisKey(runRecordPrefix) + sid
	instA := db.RedisKey(instanceKeyPrefix) + "eng-A"
	instB := db.RedisKey(instanceKeyPrefix) + "eng-B"
	t.Cleanup(func() { _ = rdb.Del(ctx, key, instA, instB).Err() })

	// ① 记录自带 url ⇒ 直接用它，不必查注册表
	if err := rdb.Set(ctx, key, `{"instance_id":"eng-A","run_token":"rt-1","url":"http://a:8000"}`, time.Minute).Err(); err != nil {
		t.Fatalf("seed: %v", err)
	}
	rec, ok := RunOwner(ctx, sid)
	if !ok || rec.InstanceID != "eng-A" || rec.RunToken != "rt-1" {
		t.Fatalf("归属读取失败：ok=%v rec=%+v", ok, rec)
	}
	if url, _, ok := RunOwnerURL(ctx, sid); !ok || url != "http://a:8000" {
		t.Fatalf("应直接用记录里的 url，得到 %q ok=%v", url, ok)
	}

	// ② 记录没有 url ⇒ 回退到引擎注册表（engine:instance:{id}）
	if err := rdb.Set(ctx, key, `{"instance_id":"eng-B","run_token":"rt-2"}`, time.Minute).Err(); err != nil {
		t.Fatalf("seed: %v", err)
	}
	if err := rdb.Set(ctx, instB, `{"url":"http://b:8000"}`, time.Minute).Err(); err != nil {
		t.Fatalf("seed instance: %v", err)
	}
	if url, _, ok := RunOwnerURL(ctx, sid); !ok || url != "http://b:8000" {
		t.Fatalf("应回退查实例注册表，得到 %q ok=%v", url, ok)
	}

	// ③ 注册表也没有 ⇒ ok=false（调用方回退哈希），不能拿空 URL 当成功
	if err := rdb.Del(ctx, key).Err(); err != nil {
		t.Fatalf("del: %v", err)
	}
	if _, _, ok := RunOwnerURL(ctx, sid); ok {
		t.Fatal("映射缺失必须 ok=false，不能返回空 URL 当成功")
	}
}

func TestLiveRunOwnerRejectsGarbageRecords(t *testing.T) {
	rdb := withAffinityRedis(t)
	ctx := context.Background()
	sid := "pytest-affinity-bad-" + uuid.NewString()
	key := db.RedisKey(runRecordPrefix) + sid
	t.Cleanup(func() { _ = rdb.Del(ctx, key).Err() })

	cases := []struct {
		name  string
		value string
	}{
		{"非法 JSON", "not json"},
		{"instance_id 为空", `{"run_token":"rt"}`},
		{"JSON 数组", `[1,2,3]`},
	}
	for _, tc := range cases {
		if err := rdb.Set(ctx, key, tc.value, time.Minute).Err(); err != nil {
			t.Fatalf("seed %s: %v", tc.name, err)
		}
		if _, ok := RunOwner(ctx, sid); ok {
			t.Fatalf("%s：必须 ok=false（不能让垃圾记录把路由带偏）", tc.name)
		}
	}
}

func TestLiveSubagentRunOwnerReadsRecord(t *testing.T) {
	rdb := withAffinityRedis(t)
	ctx := context.Background()
	runID := "pytest-rs-" + uuid.NewString()
	key := db.RedisKey(subagentRunPrefix) + runID
	t.Cleanup(func() { _ = rdb.Del(ctx, key).Err() })

	raw := `{"instance_id":"eng-9","url":"http://e9:8000","token":"tok",` +
		`"run_id":"` + runID + `","session_id":"s1","tenant_id":"t1"}`
	if err := rdb.Set(ctx, key, raw, time.Minute).Err(); err != nil {
		t.Fatalf("seed: %v", err)
	}
	owner, ok := SubagentRunOwner(ctx, runID)
	if !ok || owner.InstanceID != "eng-9" || owner.TenantID != "t1" || owner.SessionID != "s1" {
		t.Fatalf("子 Agent 归属读取失败：ok=%v owner=%+v", ok, owner)
	}
	if url, _, ok := SubagentRunOwnerURL(ctx, runID); !ok || url != "http://e9:8000" {
		t.Fatalf("应返回持有实例的 url，得到 %q ok=%v", url, ok)
	}
}
