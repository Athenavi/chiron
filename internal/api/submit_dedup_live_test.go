package api

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"

	"github.com/athenavi/chiron/internal/broadcast"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/session"
	"github.com/google/uuid"
)

// 发送侧幂等（`client_msg_id` + Redis `SET … NX EX 300`）的用例 —— 此前**一条都没有**。
//
// 机制在 `submit_handler.go` 的 `HandleSubmit` 里：`SET submit:dedup:<sid>:<cid> 1 NX EX 300`，
// 命中（`redis.Nil`）即**不再触发引擎**。它防的是"网络抖动导致的重试 / 用户以为没发出去
// 而重发"让**同一条消息被执行两次** —— 表现是重复写文件、**重复计费**。Redis 本身故障时
// 一律放行（去重是优化，不该把正常提交卡住），这条也一并断言。
//
// 观测手法：`SubmitHandler.python` 是具体类型（不是接口），没法注入 spy；
// 把 `NewPythonClient` 指向一个 **httptest 假引擎**，就能直接数"引擎被调了几次"。
// 其余依赖用安全替身：`session.NewManager(nil, nil)`（各写方法都 `if m.pool == nil` 早返回）、
// `broadcast.NewHub(nil)`（本地 hub）、`biller` 传 nil（调用处有 `if h.biller != nil`）。
//
// 依赖 Redis ⇒ 走 `withLiveRedis`（见 session_coord_live_test.go），未设 REDIS_URL 时 skip。

// countingEngine 起一个只数请求数的假引擎（回一条合法 SSE，让调用方走正常路径）。
func countingEngine(t *testing.T) (*httptest.Server, *int32) {
	t.Helper()
	var calls int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		atomic.AddInt32(&calls, 1)
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("data: {\"type\":\"done\"}\n\n"))
	}))
	t.Cleanup(srv.Close)
	return srv, &calls
}

func dedupTestHandler(srv *httptest.Server) *SubmitHandler {
	return NewSubmitHandler(
		engine.NewPythonClient(srv.URL),
		session.NewManager(nil, nil),
		broadcast.NewHub(nil),
		nil,
	)
}

func dedupKeyFor(sessionID, clientMsgID string) string {
	return db.RedisKey("submit:dedup:" + sessionID + ":" + clientMsgID)
}

// **去重命中 ⇒ 不触发引擎**（这就是"不重复执行 / 不重复计费"的落点）。
func TestLiveSubmitDedupSkipsEngineWhenClientMsgIDAlreadySeen(t *testing.T) {
	withLiveRedis(t)
	srv, calls := countingEngine(t)
	h := dedupTestHandler(srv)

	sid, cid := uuid.NewString(), uuid.NewString()
	key := dedupKeyFor(sid, cid)
	t.Cleanup(func() { _ = db.Redis.Del(context.Background(), key).Err() })

	// 预置去重键 = "上一次提交仍在处理中"（5 分钟窗口内）
	if err := db.Redis.Do(context.Background(), "SET", key, "1", "EX", "300").Err(); err != nil {
		t.Fatalf("预置去重键失败: %v", err)
	}

	h.HandleSubmit(context.Background(), uuid.NewString(), sid, "hello",
		map[string]interface{}{"client_msg_id": cid}, nil)

	if n := atomic.LoadInt32(calls); n != 0 {
		t.Fatalf("去重命中时不该触发引擎，实际调用 %d 次", n)
	}
}

// **正对照**：没有去重键时首次提交必须打到引擎，并且**落下带 5 分钟窗口的去重键** ——
// 否则上一条的"0 次"可能只是"这条路根本不通"。
func TestLiveSubmitDedupLetsFirstRequestThroughAndSetsWindow(t *testing.T) {
	withLiveRedis(t)
	srv, calls := countingEngine(t)
	h := dedupTestHandler(srv)

	sid, cid := uuid.NewString(), uuid.NewString()
	key := dedupKeyFor(sid, cid)
	t.Cleanup(func() { _ = db.Redis.Del(context.Background(), key).Err() })

	h.HandleSubmit(context.Background(), uuid.NewString(), sid, "hello",
		map[string]interface{}{"client_msg_id": cid}, nil)

	if n := atomic.LoadInt32(calls); n == 0 {
		t.Fatal("首次提交必须打到引擎（否则上一条的 0 次没有意义）")
	}
	ttl, err := db.Redis.Do(context.Background(), "TTL", key).Int64()
	if err != nil {
		t.Fatalf("查 TTL 失败: %v", err)
	}
	if ttl <= 0 || ttl > 300 {
		t.Fatalf("去重键应是 5 分钟窗口，实际 TTL=%ds", ttl)
	}
}

// **行为不变**：没有 `client_msg_id`（老客户端 / 外部调用）时不去重、照常提交。
func TestLiveSubmitWithoutClientMsgIDIsNotDeduped(t *testing.T) {
	withLiveRedis(t)
	srv, calls := countingEngine(t)
	h := dedupTestHandler(srv)

	sid := uuid.NewString()
	h.HandleSubmit(context.Background(), uuid.NewString(), sid, "hello",
		map[string]interface{}{}, nil)

	if n := atomic.LoadInt32(calls); n == 0 {
		t.Fatal("没有 client_msg_id 时不该被去重：老客户端行为必须不变")
	}

	// 只给空白串时同样按"没有"处理（代码里 `strings.TrimSpace` 后为空即跳过）
	sid2 := uuid.NewString()
	blankKey := dedupKeyFor(sid2, "")
	h.HandleSubmit(context.Background(), uuid.NewString(), sid2, "hello",
		map[string]interface{}{"client_msg_id": "   "}, nil)

	exists, err := db.Redis.Exists(context.Background(), blankKey).Result()
	if err != nil {
		t.Fatalf("查键失败: %v", err)
	}
	if exists != 0 {
		t.Fatalf("空白 client_msg_id 不该产生去重键：%s", blankKey)
	}
}
