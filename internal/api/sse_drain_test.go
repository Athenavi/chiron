package api

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/broadcast"
)

// flushRecorder 是 handleSSE 需要的 ResponseWriter：支持 Flush 并记录写出的字节。
type flushRecorder struct {
	header  http.Header
	body    bytes.Buffer
	flushed int
}

func newFlushRecorder() *flushRecorder { return &flushRecorder{header: http.Header{}} }

func (f *flushRecorder) Header() http.Header         { return f.header }
func (f *flushRecorder) Write(p []byte) (int, error) { return f.body.Write(p) }
func (f *flushRecorder) WriteHeader(int)             {}
func (f *flushRecorder) Flush()                      { f.flushed++ }

// TestHandleSSEReturnsOnDrain 钉住"停机时 SSE 会主动收尾"。
//
// 不这么做的后果是可观测的：`srv.Shutdown` 等待所有活跃连接结束，而 SSE 连接只有
// 客户端断开才结束。滚动发布时只要有用户开着页面，宽限期（10s）就会被吃满，
// Shutdown 超时返回错误、进程被强杀，同实例上正在处理的普通请求一起被切断。
func TestHandleSSEReturnsOnDrain(t *testing.T) {
	hub := broadcast.NewHub(nil) // localOnly：不连 Redis，无需外部依赖
	defer hub.Close()

	rec := newFlushRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/events?session_id=s1", nil)
	req = req.WithContext(req.Context())

	done := make(chan struct{})
	go func() {
		defer close(done)
		handleSSE(rec, req, hub, "u1:sub", "s1", "")
	}()

	// 等连接就绪（handleSSE 会先写 connected 事件），再触发停机。
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) && !strings.Contains(rec.body.String(), "connected") {
		time.Sleep(5 * time.Millisecond)
	}
	if !strings.Contains(rec.body.String(), "connected") {
		t.Fatal("SSE 连接未就绪：没有收到 connected 事件")
	}

	hub.BeginDrain()

	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("BeginDrain 后 handleSSE 仍未返回：停机时会被 Shutdown 宽限期强杀")
	}

	if !strings.Contains(rec.body.String(), "server_shutdown") {
		t.Error("收尾时未告知客户端 server_shutdown，前端只能看到静默断流并等待重连退避")
	}
}

func TestBeginDrainIsIdempotentAndVisible(t *testing.T) {
	hub := broadcast.NewHub(nil)
	defer hub.Close()

	select {
	case <-hub.Draining():
		t.Fatal("未开始停机时 Draining 不应关闭")
	default:
	}

	hub.BeginDrain()
	hub.BeginDrain() // 二次调用不能 panic（close 已关闭通道会 panic）

	select {
	case <-hub.Draining():
	case <-time.After(time.Second):
		t.Fatal("BeginDrain 之后 Draining 应已关闭")
	}
}

func TestHubCloseAlsoDrains(t *testing.T) {
	// Close 若不走 drain，直接 close(sub) 也能唤醒读循环，但客户端收到的是"静默断流"
	// 而不是明确的 server_shutdown —— 前端因此无法区分"服务端重启"与"网络故障"。
	hub := broadcast.NewHub(nil)
	hub.Close()

	select {
	case <-hub.Draining():
	case <-time.After(time.Second):
		t.Fatal("Close 之后 Draining 应已关闭")
	}
}
