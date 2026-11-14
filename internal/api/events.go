package api

import (
	"errors"
	"fmt"
	"log/slog"
	"math/rand"
	"net/http"
	"os"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/broadcast"
	"github.com/athenavi/chiron/internal/session"
)

// sseWriteTimeout 是单帧 SSE 写入的期限。
//
// 客户端不读（接收窗口长期为满）或连接已半开时，无期限的 Write 会永久阻塞；
// 30s 足够覆盖正常慢链路，又不至于让一条死连接长期占着 goroutine 与 fd。
const sseWriteTimeout = 30 * time.Second

// handleSSE manages a Server-Sent Events connection for real-time streaming.
// It subscribes to the event hub and writes events to the response writer.
//
// 两种订阅身份（M21 起），**都不做全站放行**：
//   - 会话通道（sessionID 非空）：只转发该会话的事件；无 session 的系统事件照收（既有行为）。
//   - 用户通道（sessionID 为空、userID 非空）：只转发 `Event.UserID == userID` 的事件 ——
//     跨会话的待办提醒要送到"这个人"，而不是某一个会话。
//
// 两者都为空时不转发任何事件（调用方本不该构造这种连接）。
func handleSSE(w http.ResponseWriter, r *http.Request, hub *broadcast.Hub, subID string, sessionID string, userID string) {
	if _, ok := w.(http.Flusher); !ok {
		InternalError(w, "streaming not supported")
		return
	}

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")

	rc := http.NewResponseController(w)
	// P1 修复：SSE 长连接豁免服务器 WriteTimeout（默认 60s 会切断流）。
	// 豁免是**逐帧**完成的：writeFrame 每次写前重设 sseWriteTimeout ——
	// 既不让全局 WriteTimeout 切断活跃的流，也不让死连接无限阻塞。
	_ = rc.SetWriteDeadline(time.Time{})

	// writeFrame 写一帧并 flush；返回 false 表示连接已不可用，读循环须立即收尾。
	//
	// 为什么必须看错误：客户端掉线但 TCP 未收到 FIN/RST（移动网络切换、NAT 超时、
	// 进程被杀）时 `r.Context()` 不会 Done，Write 会一直阻塞在发送窗口上。
	// 不检查错误、不设写期限，这条连接就永久占住 goroutine、订阅通道和 fd；
	// 攒够 fd 之后同实例的**所有**新连接都建不起来 —— 一个用户掉线放大成整实例不可用。
	writeFrame := func(frame string) bool {
		_ = rc.SetWriteDeadline(time.Now().Add(sseWriteTimeout))
		if _, err := w.Write([]byte(frame)); err != nil {
			slog.Debug("sse write failed, closing stream",
				"session", sessionID, "user", userID, "error", err)
			return false
		}
		if err := rc.Flush(); err != nil {
			slog.Debug("sse flush failed, closing stream",
				"session", sessionID, "user", userID, "error", err)
			return false
		}
		return true
	}

	ch := hub.Subscribe(subID)
	defer hub.Unsubscribe(subID)

	// 断线重连补发：浏览器对同一 EventSource 自动重连时携带 Last-Event-ID；
	// 前端亦可显式传 last_event_id 参数。从 per-session 缓冲流补发缺口后再进入实时转发。
	lastEventID := r.Header.Get("Last-Event-ID")
	if lastEventID == "" {
		lastEventID = r.URL.Query().Get("last_event_id")
	}
	// 只接受**可解析**的流 ID 作为去重基线：伪造/损坏的 ID 既不能用于补发，还会让后面的
	// 去重把所有真实事件判成"旧的"而全部丢弃（连接还在、却再也收不到事件）。
	lastSentID := ""
	if lastEventID != "" {
		if _, _, ok := broadcast.ParseStreamID(lastEventID); ok {
			lastSentID = lastEventID
		} else {
			slog.Warn("ignoring malformed Last-Event-ID",
				"session", sessionID, "last_event_id", lastEventID)
		}
	}
	if sessionID != "" && lastSentID != "" {
		replayed, err := hub.ReplayAfter(r.Context(), sessionID, lastSentID)
		if err != nil {
			// 缓冲不可读时降级为仅实时（不阻断连接；事件仍可由前端按 DB 状态自愈）
			slog.Warn("sse replay failed, falling back to live-only", "session", sessionID, "error", err)
		}
		for _, ev := range replayed {
			if !writeFrame(broadcast.FormatSSE(ev)) {
				return
			}
			lastSentID = ev.ID
		}
	}

	// Send initial connected event
	if !writeFrame(broadcast.FormatSSE(broadcast.Event{Type: "connected", Data: map[string]string{"id": subID}})) {
		return
	}

	pingTimer := time.NewTimer(15 * time.Second)
	defer pingTimer.Stop()

	// 停机信号：进程开始优雅关闭时主动收尾，否则 srv.Shutdown 会一直等到客户端断开
	// （SSE 不会自己结束），把宽限期吃满后强杀进程，连带切断同实例的正常请求。
	var draining <-chan struct{}
	if hub != nil {
		draining = hub.Draining()
	}

	for {
		select {
		case <-r.Context().Done():
			return
		case <-draining:
			// 先告诉客户端这是服务端主动结束、可立即重连，再关闭响应。
			writeFrame("event: server_shutdown\ndata: {\"reason\":\"server restarting\"}\n\n")
			return
		case event, ok := <-ch:
			if !ok {
				return
			}
			// 过滤（M21 起两种订阅身份）：会话通道按会话，用户通道按人；
			// 两者都不放行全站事件。
			if !sseEventMatches(event, sessionID, userID) {
				continue
			}
			// 去重：补发与实时在订阅切换窗口可能重叠，丢弃已发送过的旧事件
			// （按流 ID **数值**比较；字符串比较会在 seq 变宽时判错，见 broadcast.IsNewerStreamID）
			if event.ID != "" && !broadcast.IsNewerStreamID(event.ID, lastSentID) {
				continue
			}
			if !writeFrame(broadcast.FormatSSE(event)) {
				return
			}
			if event.ID != "" {
				lastSentID = event.ID
			}
			// Reset ping timer after activity
			if !pingTimer.Stop() {
				select {
				case <-pingTimer.C:
				default:
				}
			}
			pingTimer.Reset(15 * time.Second)
		case <-pingTimer.C:
			// Keep-alive ping：也是唯一能发现"半开连接"的探针 ——
			// 客户端已消失但 TCP 未发 FIN 时，只有这次写会报错。
			if !writeFrame(": ping\n\n") {
				return
			}
			pingTimer.Reset(15 * time.Second)
		}
	}
}

// SSEHandler returns an http.HandlerFunc for SSE connections.
// Requires authentication (authMW) — session_id is checked for ownership
// against the authenticated user (S1: prevent subscribing to other users' streams).
func SSEHandler(hub *broadcast.Hub, sessionMgr *session.Manager) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		subID := r.URL.Query().Get("client_id")
		if subID == "" {
			// 使用 math/rand 生成快速随机 ID（非安全敏感场景：subID 仅用于订阅标识）
			subID = fmt.Sprintf("anon-%x-%d", rand.Uint64(), os.Getpid())
		}
		sessionID := r.URL.Query().Get("session_id")
		// P0-S5: 必须显式指定 session_id，否则订阅到全站事件流（含其他用户对话内容）
		if sessionID == "" {
			BadRequest(w, "session_id is required")
			return
		}
		claims := auth.GetClaims(r.Context())
		if claims == nil {
			Unauthorized(w, ErrAuthRequired)
			return
		}
		// 订阅键归属隔离：hub 以 subID 为键，且 hub.Subscribe 在**同键**时会关闭旧
		// 订阅通道（见 internal/broadcast/hub.go）。若直接采信客户端传来的 client_id，
		// 攻击者用**他人**的 client_id 订阅即可把对方的 SSE 连接顶掉（拒绝服务）。
		// 这里强制加上调用者维度前缀，使不同用户的订阅键不可能相同。
		subID = claims.UserID + ":" + subID
		if sessionMgr != nil {
			s, err := sessionMgr.GetSession(r.Context(), sessionID)
			if err != nil {
				// 新会话：前端先建立 SSE 连接，/submit 才会创建 session。
				// 此时会话尚不存在、无历史事件可泄露，放行连接等待创建；
				// 其他错误（DB 故障等）拒绝。
				if !errors.Is(err, session.ErrSessionNotFound) {
					InternalError(w, "session check failed")
					return
				}
			} else if s == nil || s.UserID != claims.UserID {
				Forbidden(w, "session does not belong to the current user")
				return
			}
		}
		handleSSE(w, r, hub, subID, sessionID, "")
	}
}

// sseEventMatches 判断一条事件是否该发给这个订阅者（M21 起两种身份）。
//
// 抽成独立函数是为了**可测**：这段判据是 SSE 的越权边界，
// 而它长在一条长驻连接的读循环里，直接测连接既慢又容易 flaky。
//
//   - 会话通道（sessionID 非空）：本会话的事件照发；**无 session 的系统事件**也照发
//     （既有行为：`connected` / 系统通知这类没有会话归属）；别的会话一律不发。
//   - 用户通道（sessionID 为空、userID 非空）：**只**发挂在这个用户名下的事件 ——
//     它服务的正是"用户没打开那个会话"的场景，因此不能退化成"按会话放行"。
//   - 两者都为空：不发任何事件（调用方本不该构造这种连接）。
func sseEventMatches(event broadcast.Event, sessionID, userID string) bool {
	if sessionID != "" {
		return event.SessionID == "" || event.SessionID == sessionID
	}
	if userID != "" {
		return event.UserID == userID
	}
	return false
}

// UserSSEHandler 是**用户级**的 SSE 入口（M21）：一条连接收"挂在这个用户名下"的事件。
//
// 与 `SSEHandler` 的两点不同，都是刻意的：
//   - **不要求 `session_id`**：它服务的正是"用户没打开那个会话"的场景（全局角标）；
//   - **身份只能来自 claims**：订阅键与过滤值都取自 `auth.GetClaims`，客户端传什么都不作数。
//     否则任何人都能订阅别人的待办提醒。
//
// 订阅键沿用 `SSEHandler` 的防顶替做法（`user:sub` 前缀 + 客户端随机 client_id）：
// hub 在同键订阅时会关掉旧通道，直接采信 client_id 就能把别人的连接顶掉；
// 而多标签页各自带一个随机 client_id，因此不会互相顶掉。
func UserSSEHandler(hub *broadcast.Hub) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		claims := auth.GetClaims(r.Context())
		if claims == nil || claims.UserID == "" {
			Unauthorized(w, ErrAuthRequired)
			return
		}
		clientID := r.URL.Query().Get("client_id")
		if clientID == "" {
			clientID = fmt.Sprintf("anon-%x-%d", rand.Uint64(), os.Getpid())
		}
		subID := claims.UserID + ":user:" + clientID
		handleSSE(w, r, hub, subID, "", claims.UserID)
	}
}
