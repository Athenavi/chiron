package api

import (
	"bufio"
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/broadcast"
	"github.com/athenavi/chiron/internal/db"
	"github.com/google/uuid"
)

// 「断线 → 重连 → 无缺口」的**跨实例 HTTP 级**取证（DSH 缺口 #1 保证表的最后一块）。
//
// 拓扑：两个 `broadcast.Hub` = 两个网关实例（共享同一个 Redis）。客户端把 SSE 连接建在
// **实例 A** 上，而事件由**实例 B** 发布（真实多副本形态：run 跑在 B，用户连在 A）。
//
// 与 `internal/broadcast/hub_live_test.go` 的分工：那套是 **Hub 级**；这里走**真实 HTTP + SSE 处理器**
// （含 `Last-Event-ID` 解析、按 session 过滤、按流 ID 数值去重），覆盖的是**对外可见的那一层**。
// 仍非两个 OS 进程（那属部署演练，见路线图），但"补发读共享流"这条语义完全一致。
func TestLiveSSEAcrossInstancesReplaysGapThenGoesLive(t *testing.T) {
	withLiveRedis(t)
	rdb := db.Redis

	hubA := broadcast.NewHub(rdb) // 客户端连接的实例
	hubB := broadcast.NewHub(rdb) // 发布事件的实例（run 所在）
	t.Cleanup(func() {
		hubA.Close()
		hubB.Close()
	})

	sid := "pytest-xinst-" + uuid.NewString()
	for i := 1; i <= 3; i++ {
		hubB.Publish(broadcast.Event{
			Type: "text", SessionID: sid, Data: map[string]int{"n": i},
		})
	}
	// 用 **A** 读 **B** 写出来的流：这一句本身就是"跨实例共享缓冲"的前提。
	ids := waitForCrossInstanceReplay(t, hubA, sid, 3)
	baseline := ids[1] // 客户端声称"我已经收到前两条"

	mux := http.NewServeMux()
	mux.Handle("GET /v1/events", injectTestClaims(SSEHandler(hubA, nil)))
	srv := httptest.NewServer(mux)
	defer srv.Close()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		srv.URL+"/v1/events?session_id="+sid+"&client_id=probe", nil)
	if err != nil {
		t.Fatalf("构造请求失败: %v", err)
	}
	req.Header.Set("Last-Event-ID", baseline)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("连接 SSE 失败: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("SSE 连接应 200，得到 %d", resp.StatusCode)
	}

	idsCh := make(chan string, 16)
	go func() {
		sc := bufio.NewScanner(resp.Body)
		for sc.Scan() {
			if id, ok := strings.CutPrefix(sc.Text(), "id: "); ok {
				select {
				case idsCh <- id:
				case <-ctx.Done():
					return
				}
			}
		}
	}()

	// ① 补发**只补缺口**：第一条收到的必须是第 3 条 —— 若把已收到的第 1/2 条又补一遍，这里会先收到它们。
	if got := recvSSEID(t, idsCh, "补发"); got != ids[2] {
		t.Fatalf("跨实例补发应从 baseline 之后开始：want %s got %s（重复补发或漏发）", ids[2], got)
	}

	// ② 补发之后**实时继续**：run 仍在实例 B 上，后续事件必须经共享通道到达连在 A 上的这条连接。
	hubB.Publish(broadcast.Event{Type: "text", SessionID: sid, Data: map[string]int{"n": 4}})
	live := recvSSEID(t, idsCh, "实时")
	if !broadcast.IsNewerStreamID(live, ids[2]) {
		t.Fatalf("实时事件应比补发末条更新：last=%s got=%s", ids[2], live)
	}
	// ③ 无缺口也无重叠：共享流里 baseline 之后**恰好**只剩实时那一条，且与收到的同一条。
	rest, err := hubA.ReplayAfter(context.Background(), sid, ids[2])
	if err != nil {
		t.Fatalf("ReplayAfter: %v", err)
	}
	if len(rest) != 1 || rest[0].ID != live {
		t.Fatalf("baseline 之后应恰好剩实时那条：%+v（收到 %s）", rest, live)
	}
}

func injectTestClaims(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ctx := auth.WithClaims(r.Context(), &auth.Claims{TenantID: "t1", UserID: "u1"})
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// waitForCrossInstanceReplay 轮询直到 hub 能读到 want 条事件，返回它们的流 ID（按流序）。
// `Publish` 是异步的（入队 + worker），因此必须轮询而不是立即断言。
func waitForCrossInstanceReplay(t *testing.T, hub *broadcast.Hub, sid string, want int) []string {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		events, err := hub.ReplayAfter(context.Background(), sid, "0-0")
		if err == nil && len(events) >= want {
			ids := make([]string, 0, len(events))
			for _, ev := range events {
				ids = append(ids, ev.ID)
			}
			return ids
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("实例 A 在 5s 内没读到实例 B 发布的 %d 条事件：跨实例共享缓冲不成立", want)
	return nil
}

func recvSSEID(t *testing.T, ch <-chan string, what string) string {
	t.Helper()
	select {
	case id, ok := <-ch:
		if !ok {
			t.Fatalf("%s：SSE 连接提前关闭", what)
		}
		return id
	case <-time.After(5 * time.Second):
		t.Fatalf("%s：5s 内没有收到任何事件", what)
		return ""
	}
}
