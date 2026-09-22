package api

import (
	"context"
	"encoding/json"
	"log/slog"

	"github.com/athenavi/chiron/internal/broadcast"
	"github.com/athenavi/chiron/internal/db"
)

// subagentEventsChannel 是引擎侧广播子 Agent 事件的 Redis 通道
// （发布端见 python-engine/app/subagent/runtime_cache.py 的 publish_live_event）。
func subagentEventsChannel() string { return db.RedisKey("subagent:events") }

// StartSubagentEventsRelay 订阅引擎发布的子 Agent 事件并转投 SSE hub。
//
// 为什么必须有它 —— 子 Agent 的事件有两条去路，缺一不可：
//
//	Redis Stream（subagent:{tenant}:ev:{run}） → 历史回放
//	    GET /v1/subagent/runs/{id}/events 读它（前端断线重连/刷新后靠它补齐）
//	Redis pub/sub（本函数订阅）              → 实时推送
//	    网关转投 SSE hub → 浏览器 EventSource
//
// 而浏览器只从网关的 `/events` 收推送。没有这个中继，后台子 Agent 的事件永远到不了
// 前端：`run_in_background` 的场景下父 turn 早已结束、父 SSE 流也没了，面板就只能靠
// 「选中某个 run 时 3s 轮询」—— 既不实时，还要求用户先点开那个 run。
//
// 形态与 StartAgentCancelSubscriber 一致：进程内一个 goroutine + 一条 Redis 订阅。
func StartSubagentEventsRelay(ctx context.Context, hub *broadcast.Hub) {
	if db.Redis == nil {
		slog.Warn("subagent events relay disabled: redis unavailable")
		return
	}
	if hub == nil {
		slog.Warn("subagent events relay disabled: event hub unavailable")
		return
	}
	go func() {
		channel := subagentEventsChannel()
		pubsub := db.Redis.Subscribe(ctx, channel)
		defer pubsub.Close()
		ch := pubsub.Channel()
		slog.Info("subagent events relay started", "channel", channel)
		for {
			select {
			case <-ctx.Done():
				return
			case msg, ok := <-ch:
				if !ok {
					slog.Warn("subagent events relay channel closed")
					return
				}
				var payload map[string]interface{}
				if err := json.Unmarshal([]byte(msg.Payload), &payload); err != nil {
					slog.Warn("subagent event payload not json", "error", err)
					continue
				}
				eventType, _ := payload["type"].(string)
				sessionID, _ := payload["session_id"].(string)
				if eventType == "" {
					continue
				}
				if sessionID == "" {
					// 没有会话归属就无法路由：SSE 端点是按 session 过滤的，硬塞会给
					// **所有**会话的订阅者都推一份（串扰）。宁可丢这一条并留日志。
					slog.Warn("subagent event without session_id, dropped", "type", eventType)
					continue
				}
				hub.Publish(broadcast.Event{
					Type:      eventType,
					SessionID: sessionID,
					Data:      payload,
				})
			}
		}
	}()
}
