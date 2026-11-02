package api

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/broadcast"
)

// errWriteConn 模拟"客户端已经消失"的响应：Write 立刻报错。
//
// 这正是半开连接在网关侧的观感：对端掉线但 TCP 没有 FIN/RST（移动网络切换、
// NAT 表项超时、客户端进程被杀），`r.Context().Done()` 永远不会触发。
// 剩下的唯一信号就是写失败。
type errWriteConn struct {
	hdr http.Header
}

func (w *errWriteConn) Header() http.Header {
	if w.hdr == nil {
		w.hdr = http.Header{}
	}
	return w.hdr
}

func (w *errWriteConn) Write([]byte) (int, error) { return 0, errors.ErrUnsupported }
func (w *errWriteConn) WriteHeader(int)          {}
func (w *errWriteConn) Flush()                   {}

// TestHandleSSEClosesOnWriteError 固定住一条资源边界：首帧写失败时连接必须立即收尾。
//
// 不修时行为是"忽略错误、继续等事件"：读循环会一直挂在 select 上，直到 15s 后的
// ping 再次写、再次被忽略……这条 goroutine、它持有的订阅通道（256 容量）与 fd
// 就永久留在实例里。每个掉线用户都留一份，攒够 fd 后同实例的**所有**新连接都建不起来
// —— 一个人的网络抖动会被放大成整实例不可用。
func TestHandleSSEClosesOnWriteError(t *testing.T) {
	hub := broadcast.NewHub(nil) // localOnly：不需要真 Redis
	defer hub.Close()

	req := httptest.NewRequest(http.MethodGet, "/v1/agent/events?session_id=s1", nil)

	done := make(chan struct{})
	go func() {
		defer close(done)
		handleSSE(&errWriteConn{}, req, hub, "u1:s1", "s1", "")
	}()

	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("写失败后 handleSSE 没有返回：半开连接会永久占住 goroutine、订阅通道与 fd")
	}
}
