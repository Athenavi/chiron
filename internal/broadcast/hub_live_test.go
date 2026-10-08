package broadcast

import (
	"context"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
)

// 真实 Redis 用例（断线重放的核心契约）。
//
// 为什么值得单独一套：`ReplayAfter` 是"多副本 / 断线重连不丢事件"这条**对外可核实行为**
// 的唯一实现（见 docs/dsh-gap-analysis.md 切口 #1），但此前只有 `FormatSSE` 与
// `localOnly` 两条无关用例 —— 补测时正是这里挖出了流 ID 字符串比较导致的丢事件缺陷
// （见 streamid_test.go）。
//
// 取值顺序与既有 `*_live_test.go` 一致：CHIRON_TEST_REDIS_ADDR（host:port）优先，
// 否则用 CI real-stack job 注入的 REDIS_URL；都没有则 skip。
func liveRedisClient(t *testing.T) db.RedisClient {
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
	t.Cleanup(func() { _ = client.Close() })
	return client
}

func newLiveHub(t *testing.T, rdb db.RedisClient) *Hub {
	t.Helper()
	h := NewHub(rdb)
	t.Cleanup(h.Close)
	return h
}

func liveSession(t *testing.T, rdb db.RedisClient, tag string) (string, string) {
	t.Helper()
	sid := fmt.Sprintf("pytest-sse-%s-%s", tag, uuid.NewString())
	key := sessionEventsKey(sid)
	t.Cleanup(func() { _ = rdb.Del(context.Background(), key).Err() })
	return sid, key
}

func waitXLen(t *testing.T, rdb db.RedisClient, key string, want int) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if n, err := rdb.XLen(context.Background(), key).Result(); err == nil && int(n) >= want {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	n, _ := rdb.XLen(context.Background(), key).Result()
	t.Fatalf("等待流 %s 达到 %d 条超时（当前 %d）", key, want, n)
}

func TestLiveReplayAfterReturnsOnlyNewerEvents(t *testing.T) {
	rdb := liveRedisClient(t)
	hub := newLiveHub(t, rdb)
	sid, key := liveSession(t, rdb, "order")

	for i := 0; i < 3; i++ {
		hub.Publish(Event{Type: fmt.Sprintf("ev-%d", i), SessionID: sid, Data: map[string]int{"i": i}})
	}
	waitXLen(t, rdb, key, 3)

	all, err := hub.ReplayAfter(context.Background(), sid, "0-0")
	if err != nil {
		t.Fatalf("ReplayAfter: %v", err)
	}
	if len(all) != 3 {
		t.Fatalf("应补发 3 条，实际 %d", len(all))
	}
	for i, ev := range all {
		if ev.ID == "" {
			t.Fatalf("第 %d 条没有流 ID（SSE 的 id: 行依赖它）", i)
		}
		if ev.Type != fmt.Sprintf("ev-%d", i) {
			t.Fatalf("补发必须按流序：第 %d 条是 %s", i, ev.Type)
		}
		if ev.SessionID != sid {
			t.Fatalf("会话 ID 丢失：%q", ev.SessionID)
		}
	}

	// 排他性：after 本身不补发
	rest, err := hub.ReplayAfter(context.Background(), sid, all[0].ID)
	if err != nil {
		t.Fatalf("ReplayAfter: %v", err)
	}
	if len(rest) != 2 || rest[0].ID != all[1].ID {
		t.Fatalf("补发应不含 after 本身且保序：%+v", rest)
	}

	none, err := hub.ReplayAfter(context.Background(), sid, all[2].ID)
	if err != nil || len(none) != 0 {
		t.Fatalf("最后一条之后应为空：err=%v n=%d", err, len(none))
	}

	// after 为空 = 新连接：不补历史
	if got, err := hub.ReplayAfter(context.Background(), sid, ""); err != nil || got != nil {
		t.Fatalf("after 为空不应补历史：got=%v err=%v", got, err)
	}
}

func TestLiveReplayAfterSeesAnotherInstanceEvents(t *testing.T) {
	rdb := liveRedisClient(t)
	publisher := newLiveHub(t, rdb)
	reader := newLiveHub(t, rdb) // 独立 instanceID，模拟另一副本
	sid, key := liveSession(t, rdb, "cross")

	publisher.Publish(Event{Type: "from-other-instance", SessionID: sid})
	waitXLen(t, rdb, key, 1)

	got, err := reader.ReplayAfter(context.Background(), sid, "0-0")
	if err != nil {
		t.Fatalf("ReplayAfter: %v", err)
	}
	if len(got) != 1 || got[0].Type != "from-other-instance" {
		t.Fatalf("另一实例必须能从同一条 Redis 流补发：%+v", got)
	}
}

func TestLiveReplayAfterRespectsBufferCap(t *testing.T) {
	rdb := liveRedisClient(t)
	hub := newLiveHub(t, rdb)
	sid, key := liveSession(t, rdb, "cap")

	total := sseEventsMaxLen + 20
	for i := 0; i < total; i++ {
		hub.Publish(Event{Type: "ev", SessionID: sid})
	}
	waitXLen(t, rdb, key, sseEventsMaxLen)

	got, err := hub.ReplayAfter(context.Background(), sid, "0-0")
	if err != nil {
		t.Fatalf("ReplayAfter: %v", err)
	}
	if len(got) >= total {
		t.Fatalf("缓冲应被裁剪：published=%d replayed=%d", total, len(got))
	}
	if len(got) > 2*sseEventsMaxLen {
		t.Fatalf("补发条数必须受缓冲上限约束（%d 条）", len(got))
	}
}

func TestLiveStreamHasSlidingTTL(t *testing.T) {
	rdb := liveRedisClient(t)
	hub := newLiveHub(t, rdb)
	sid, key := liveSession(t, rdb, "ttl")

	hub.Publish(Event{Type: "ev", SessionID: sid})
	waitXLen(t, rdb, key, 1)

	ttl, err := rdb.Do(context.Background(), "TTL", key).Int64()
	if err != nil {
		t.Fatalf("TTL: %v", err)
	}
	if ttl <= 0 || ttl > int64(sseEventsTTL.Seconds())+5 {
		t.Fatalf("会话流必须带滑动 TTL（%.0fs 以内、>0），实际 %ds", sseEventsTTL.Seconds(), ttl)
	}
}
