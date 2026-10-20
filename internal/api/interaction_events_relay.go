package api

import (
	"context"
	"encoding/json"
	"log/slog"

	"github.com/athenavi/chiron/internal/broadcast"
	"github.com/athenavi/chiron/internal/db"
)

// interactionEventsChannel 是引擎侧广播"多了一张待办"的 Redis 通道
// （发布端见 python-engine/app/agent/interactions.py 的 `_publish_pending_event`）。
func interactionEventsChannel() string { return db.RedisKey("interaction:events") }

// interactionEventFromPayload 把引擎发来的 payload 映射成 hub 事件；不可路由时返回 false。
//
// 抽成独立函数是为了可测：这条映射决定"提醒该给谁"，而它的两种失败（缺 `type`、
// 缺 `user_id`）都会让提醒**静默消失** —— 正是 M21 要修的那类"看不见"。
func interactionEventFromPayload(payload map[string]interface{}) (broadcast.Event, bool) {
	eventType, _ := payload["type"].(string)
	userID, _ := payload["user_id"].(string)
	if eventType == "" || userID == "" {
		return broadcast.Event{}, false
	}
	sessionID, _ := payload["session_id"].(string)
	return broadcast.Event{
		Type:      eventType,
		SessionID: sessionID,
		UserID:    userID,
		Data:      payload,
	}, true
}

// StartInteractionEventsRelay 订阅引擎发布的待办事件并转投 SSE hub（M21）。
//
// 与 `StartSubagentEventsRelay` 同形，差别只有一处：**带 `UserID`**。待办提醒的受众是
// "这个人"（跨会话角标），而不只是"那个会话"—— 所以事件同时带 `SessionID` 与 `UserID`：
// 正开着那个会话的人从会话通道实时看到卡片，没开着的人从用户通道拿到角标更新。
//
// 为什么必须有这条中继：落票发生在引擎 runtime 内部，网关的 submit 流可能早已结束
// （`awaiting_input` 收尾，或用户已经离开）。没有它，用户只能靠"打开那个会话"才发现
// 有一张票在等他 —— 那正是 M21 要修的可见性缺口。
func StartInteractionEventsRelay(ctx context.Context, hub *broadcast.Hub) {
	if db.Redis == nil {
		slog.Warn("interaction events relay disabled: redis unavailable")
		return
	}
	if hub == nil {
		slog.Warn("interaction events relay disabled: event hub unavailable")
		return
	}
	go func() {
		channel := interactionEventsChannel()
		pubsub := db.Redis.Subscribe(ctx, channel)
		defer pubsub.Close()
		ch := pubsub.Channel()
		slog.Info("interaction events relay started", "channel", channel)
		for {
			select {
			case <-ctx.Done():
				return
			case msg, ok := <-ch:
				if !ok {
					slog.Warn("interaction events relay channel closed")
					return
				}
				var payload map[string]interface{}
				if err := json.Unmarshal([]byte(msg.Payload), &payload); err != nil {
					slog.Warn("interaction event payload not json", "error", err)
					continue
				}
				eventType, _ := payload["type"].(string)
				userID, _ := payload["user_id"].(string)
				sessionID, _ := payload["session_id"].(string)
				event, ok := interactionEventFromPayload(payload)
				if !ok {
					// 没有用户归属就路由不到用户通道 —— 而用户通道正是这条事件存在的理由。
					// 宁可丢这一条并留日志，也不把它塞给所有会话（那会变成串扰）。
					slog.Warn("interaction event without type/user_id, dropped",
						"type", eventType, "user_id", userID, "session_id", sessionID)
					continue
				}
				// 走 RelayEvent：事件本来就是跨实例来的（Redis pub/sub），再 Publish 会让
				// 每个实例都把同一事件跨实例广播一次 ⇒ 客户端收 N 份（双进程演练的教训）。
				hub.RelayEvent(event)
			}
		}
	}()
}
