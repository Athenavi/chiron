package broadcast

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"hash/fnv"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
)

// ── 会话事件缓冲 + 异步发布（跨实例断线重放）──
//
// 事实源：带 SessionID 的事件先 XADD 到 per-session Redis Stream（每会话保留最近
// sseEventsMaxLen 条 + 滑动 TTL），**XADD 流 ID 即事件 ID**（跨实例单调递增），
// 客户端重连时按 Last-Event-ID 从该 Stream 补发缺口（ReplayAfter）。
//
// 发布路径（B2）：Publish 只做「入队」，真正的发布由后台 worker 串行完成——
//
//	XADD 取 ID → 本地 fanout → 跨实例 PUBLISH
//
// 这样请求处理路径上不再有 Redis 往返（此前 XADD + PUBLISH 同步执行，Redis 抖动会拖慢
// Agent 流式输出）；同时因为 ID 在 fanout 前填充，SSE 的 `id:` 行与重放语义保持不变。
// 同一 session 的事件固定进入同一分片，保证「同会话事件顺序 + Stream ID 单调」。
//
// 背压：队列满（Redis 持续慢/突发）时回退为同步发布，宁可让调用方等一会儿也不丢事件；
// 慢订阅者仍走既有的 3s 超时丢弃 + 信号量限流（SSE 可重连补发）。
const (
	sseEventsMaxLen = 200
	sseEventsTTL    = time.Hour
	// sseRedisOpTimeout 单次 Redis 操作超时（worker 内执行，避免 Redis 抖动时 worker 卡住）。
	sseRedisOpTimeout = 200 * time.Millisecond

	// ssePublishShards 发布分片数：同 session 固定分片以保证顺序，分片提升吞吐。
	ssePublishShards = 4
	// ssePublishQueueSize 每分片队列长度：超出即回退同步发布（计入 Stats().Fallback）。
	ssePublishQueueSize = 1024
)

// slowSubSem 限制慢订阅者重试 goroutine 数量（P1 修复：事件风暴下防止
// goroutine 无界堆积导致 DoS）。超出上限时直接丢弃事件（SSE 可重连补发）。
var slowSubSem = make(chan struct{}, 512)

// Event is a generic event for SSE broadcasting.
// ID 由发布端在写入会话缓冲流后填充（XADD 流 ID），非空时 SSE 输出 id: 行。
type Event struct {
	ID        string      `json:"id,omitempty"`
	Type      string      `json:"type"`
	Data      interface{} `json:"data"`
	SessionID string      `json:"session_id,omitempty"`
}

// Hub manages SSE subscribers and cross-instance event broadcasting.
type Hub struct {
	mu         sync.RWMutex
	subs       map[string]chan Event
	closed     bool
	pubsub     *redis.PubSub
	rdb        db.RedisClient
	channel    string
	localOnly  bool
	instanceID string

	// 异步发布（B2）
	queues []chan Event
	rr     uint64
	wg     sync.WaitGroup

	// 背压统计（供 Stats 观测；队列满回退同步发布不丢事件）
	statEnqueued  int64
	statFallback  int64
	statDelivered int64
}

// envelope wraps an event with its originating instance for deduplication.
type envelope struct {
	Origin string `json:"origin"`
	Event  Event  `json:"event"`
}

// HubStats 发布侧统计（背压/吞吐可观测性）。
type HubStats struct {
	Enqueued  int64 // 入队事件数
	Delivered int64 // worker 完成发布的事件数
	Fallback  int64 // 队列满导致同步发布的次数
	Queued    int   // 当前各分片队列积压总和
}

func NewHub(rdb db.RedisClient) *Hub {
	h := &Hub{
		subs:       make(map[string]chan Event),
		rdb:        rdb,
		channel:    db.RedisKey("chiron:events"),
		localOnly:  rdb == nil,
		instanceID: uuid.New().String(),
	}
	h.queues = make([]chan Event, ssePublishShards)
	for i := range h.queues {
		h.queues[i] = make(chan Event, ssePublishQueueSize)
		h.wg.Add(1)
		go h.publishWorker(h.queues[i])
	}

	if !h.localOnly {
		h.pubsub = rdb.Subscribe(context.Background(), h.channel)
		go h.redisListener()
	}

	return h
}

func (h *Hub) Subscribe(id string) chan Event {
	h.mu.Lock()
	defer h.mu.Unlock()

	if oldCh, ok := h.subs[id]; ok {
		close(oldCh)
	}
	ch := make(chan Event, 256)
	h.subs[id] = ch
	return ch
}

func (h *Hub) Unsubscribe(id string) {
	h.mu.Lock()
	defer h.mu.Unlock()

	if ch, ok := h.subs[id]; ok {
		close(ch)
		delete(h.subs, id)
	}
}

// Publish 把事件交给后台发布 worker（非阻塞）；队列满时回退为同步发布（不丢事件）。
func (h *Hub) Publish(event Event) {
	h.mu.RLock()
	if h.closed {
		h.mu.RUnlock()
		return
	}
	ch := h.queueFor(event)
	select {
	case ch <- event:
		atomic.AddInt64(&h.statEnqueued, 1)
		h.mu.RUnlock()
		return
	default:
		h.mu.RUnlock()
		// 背压：队列已满。同步完成本次发布（调用方会等待，但事件不丢）。
		atomic.AddInt64(&h.statFallback, 1)
		slog.Warn("sse publish queue full, publishing synchronously",
			"session", event.SessionID, "type", event.Type, "queue_size", ssePublishQueueSize)
		h.deliver(event)
	}
}

// queueFor 选择分片：同一 session 固定分片（保序）；无 session 时轮转。
func (h *Hub) queueFor(event Event) chan Event {
	if len(h.queues) == 1 {
		return h.queues[0]
	}
	var key uint32
	if event.SessionID != "" {
		f := fnv.New32a()
		_, _ = f.Write([]byte(event.SessionID))
		key = f.Sum32()
	} else {
		key = uint32(atomic.AddUint64(&h.rr, 1))
	}
	return h.queues[int(key)%len(h.queues)]
}

func (h *Hub) publishWorker(ch chan Event) {
	defer h.wg.Done()
	for event := range ch {
		h.deliver(event)
	}
}

// deliver 执行一次完整发布：写会话缓冲流取 ID → 本地 fanout → 跨实例发布。
// worker 内串行调用（同一分片内顺序执行），因此同会话事件顺序与 Stream ID 单调一致。
func (h *Hub) deliver(event Event) {
	// 会话事件先写入 per-session 缓冲流，取得全局单调 ID（跨实例一致排序/重放依据）；
	// 写入失败（Redis 故障）时仅失去重放能力，实时 fanout 照常。
	if !h.localOnly && event.SessionID != "" {
		event.ID = h.appendSessionEvent(context.Background(), event)
	}

	h.fanoutLocal(event)

	if !h.localOnly {
		h.publishCrossInstance(event)
	}
	atomic.AddInt64(&h.statDelivered, 1)
}

// fanoutLocal 把事件推送给本实例订阅者；慢订阅者走 goroutine + 3s 超时丢弃。
func (h *Hub) fanoutLocal(event Event) {
	// Close 期间订阅 channel 可能被关闭：丢弃本次即可（Close 是终态）。
	defer func() {
		if r := recover(); r != nil {
			slog.Debug("fanout to closed subscriber channel skipped")
		}
	}()

	h.mu.RLock()
	defer h.mu.RUnlock()
	if h.closed {
		return
	}
	for _, ch := range h.subs {
		select {
		case ch <- event:
		default:
			// Slow subscriber: spawn goroutine so fast subscribers aren't blocked
			select {
			case slowSubSem <- struct{}{}:
				go func(c chan Event) {
					defer func() {
						if r := recover(); r != nil {
							// channel 已关闭，丢弃事件即可
						}
						<-slowSubSem
					}()
					timer := time.NewTimer(3 * time.Second)
					defer timer.Stop()
					select {
					case c <- event:
					case <-timer.C:
						slog.Warn("subscriber too slow, dropping event after 3s timeout")
					}
				}(ch)
			default:
				slog.Warn("too many slow subscribers, dropping event")
			}
		}
	}
}

// publishCrossInstance 通过 Redis Pub/Sub 通知其它实例（低延迟通道；事实源仍是会话 Stream）。
// RelayEvent 处理**已经通过 Redis 到达本实例**的事件（引擎 pub/sub 通道 → 网关中继）。
//
// 与 Publish 的区别（都是 2026-10-08 双进程演练暴露出来的）：
//   - 仍然追加本实例的会话缓冲流 —— 否则本实例的客户端拿不到 `id:`，重连时无法精确续传；
//   - **不再跨实例广播**：事件本来就是从 Redis 过来的，每个实例再广播一次会让**每个客户端
//     多收 N-1 份**（N = 实例数）；实测就是"同一事件收到两次"。
//
// 多实例下"同一条逻辑事件被 N 个实例各追加一次"的问题，由 ReplayAfter 按逻辑身份去重兜住
// （见 logicalEventID），而不是靠写入端互斥 —— 写入端互斥会让 N-1 个实例的客户端丢 `id`。
func (h *Hub) RelayEvent(event Event) {
	h.mu.RLock()
	closed := h.closed
	h.mu.RUnlock()
	if closed {
		return
	}
	if !h.localOnly && event.SessionID != "" {
		event.ID = h.appendSessionEvent(context.Background(), event)
	}
	h.fanoutLocal(event)
	atomic.AddInt64(&h.statDelivered, 1)
}

// logicalEventID 取事件的**逻辑身份**，用于多实例下的重放去重：
// 优先用 `Data.event_id`（引擎发布时带上的 uuid），否则退化为"type + payload 哈希"。
//
// 退化路径的精度上限：两条**内容完全相同**的事件会被当成同一条（现实里同一 run 的相邻事件
// 至少 phase/status 不同，几率极低）。这条上限写在文档里，不藏着。
func logicalEventID(ev Event) string {
	if m, ok := ev.Data.(map[string]any); ok {
		if id, _ := m["event_id"].(string); id != "" {
			return ev.Type + "|" + id
		}
	}
	raw, err := json.Marshal(ev.Data)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(append([]byte(ev.Type+"|"), raw...))
	return ev.Type + "|" + hex.EncodeToString(sum[:16])
}

func (h *Hub) publishCrossInstance(event Event) {
	env := envelope{Origin: h.instanceID, Event: event}
	data, err := json.Marshal(env)
	if err != nil {
		slog.Error("publish: failed to marshal envelope", "error", err)
		return
	}
	pctx, cancel := context.WithTimeout(context.Background(), sseRedisOpTimeout)
	defer cancel()
	if err := h.rdb.Publish(pctx, h.channel, data).Err(); err != nil {
		slog.Error("redis publish failed", "error", err)
	}
}

// Stats 返回发布侧统计（背压/吞吐可观测性）。
func (h *Hub) Stats() HubStats {
	queued := 0
	for _, ch := range h.queues {
		queued += len(ch)
	}
	return HubStats{
		Enqueued:  atomic.LoadInt64(&h.statEnqueued),
		Delivered: atomic.LoadInt64(&h.statDelivered),
		Fallback:  atomic.LoadInt64(&h.statFallback),
		Queued:    queued,
	}
}

// sessionEventsKey 返回会话事件缓冲流键（统一前缀 + 会话维度命名空间）。
func sessionEventsKey(sessionID string) string {
	return db.RedisKey("sse:events:") + sessionID
}

// appendSessionEvent 将会话事件追加到 per-session Redis Stream（容量上限 + 滑动 TTL），
// 返回流 ID 作为事件全局单调 ID；失败返回空串（调用方继续实时 fanout，仅失去重放能力）。
func (h *Hub) appendSessionEvent(ctx context.Context, ev Event) string {
	payload, err := json.Marshal(ev)
	if err != nil {
		slog.Error("sse buffer: marshal event failed", "error", err)
		return ""
	}
	// 单次 Redis 操作超时：worker 内执行，Redis 慢/抖动时快速失败，
	// 写失败只是失去重放能力，实时 fanout 照常。
	ctx, cancel := context.WithTimeout(ctx, sseRedisOpTimeout)
	defer cancel()
	key := sessionEventsKey(ev.SessionID)
	id, err := h.rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: key,
		MaxLen: sseEventsMaxLen,
		Approx: true,
		Values: map[string]interface{}{"e": string(payload)},
	}).Result()
	if err != nil {
		slog.Warn("sse buffer: xadd failed", "session", ev.SessionID, "error", err)
		return ""
	}
	// 滑动过期：会话持续活动时续期，空闲 1h 后清理流。
	if err := h.rdb.Expire(ctx, key, sseEventsTTL).Err(); err != nil {
		slog.Debug("sse buffer: expire failed", "session", ev.SessionID, "error", err)
	}
	return id
}

// ReplayAfter 返回某会话在 after（流 ID，不含）之后缓存的会话事件（最多 sseEventsMaxLen 条），
// 事件按流序返回并带各自 ID。after 为空 = 新连接，不补历史；Redis 不可用/无缓冲时返回空。
func (h *Hub) ReplayAfter(ctx context.Context, sessionID, after string) ([]Event, error) {
	if h.localOnly || sessionID == "" || after == "" {
		return nil, nil
	}
	msgs, err := h.rdb.XRange(ctx, sessionEventsKey(sessionID), "("+after, "+", sseEventsMaxLen).Result()
	if err != nil {
		return nil, err
	}
	events := make([]Event, 0, len(msgs))
	seen := make(map[string]struct{}, len(msgs))
	for _, m := range msgs {
		raw, ok := m.Values["e"].(string)
		if !ok {
			continue
		}
		var ev Event
		if err := json.Unmarshal([]byte(raw), &ev); err != nil {
			slog.Warn("sse buffer: unmarshal entry failed", "session", sessionID, "id", m.ID)
			continue
		}
		ev.ID = m.ID
		// 多实例去重：同一条逻辑事件会被每个实例各追加一次（N 份），重放时只保留最早的一份。
		// 不去重的话，客户端断线重连后会看到 N 份重复事件（2026-10-08 双进程演练实测 [2,2,3,3]）。
		if key := logicalEventID(ev); key != "" {
			if _, dup := seen[key]; dup {
				continue
			}
			seen[key] = struct{}{}
		}
		events = append(events, ev)
	}
	return events, nil
}

func (h *Hub) redisListener() {
	ch := h.pubsub.Channel()
	for msg := range ch {
		var env envelope
		if err := json.Unmarshal([]byte(msg.Payload), &env); err != nil {
			continue
		}

		// Skip events that originated from this instance (already delivered locally)
		if env.Origin == h.instanceID {
			continue
		}

		h.mu.RLock()
		closed := h.closed
		h.mu.RUnlock()
		if closed {
			return
		}
		h.fanoutLocal(env.Event)
	}
}

// Close 停止发布 worker（先 drain 队列）并关闭订阅。
// 顺序：标记 closed → 关闭队列（worker drain 完剩余事件后退出）→ 等待 worker → 关闭订阅与 pubsub。
func (h *Hub) Close() {
	if h.pubsub != nil {
		h.pubsub.Close()
	}
	h.mu.Lock()
	if h.closed {
		h.mu.Unlock()
		return
	}
	h.closed = true
	for _, ch := range h.queues {
		close(ch)
	}
	for id, ch := range h.subs {
		close(ch)
		delete(h.subs, id)
	}
	h.mu.Unlock()

	// 等 worker drain（不持锁：fanoutLocal 需要读锁）
	h.wg.Wait()
}

// SSE channel format: JSON lines, optional id: line for Last-Event-ID replay
func FormatSSE(event Event) string {
	data, err := json.Marshal(event)
	if err != nil {
		slog.Error("format SSE: failed to marshal event", "error", err)
		return "data: {\"error\":\"marshal failed\"}\n\n"
	}
	if event.ID != "" {
		return "id: " + event.ID + "\ndata: " + string(data) + "\n\n"
	}
	return "data: " + string(data) + "\n\n"
}
