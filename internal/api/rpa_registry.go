package api

// 本文件由 rpa_ws.go 按职责外移而来（Redis 注册中心 + 跨实例命令/结果桥接）。
// 代码逐字未改：只搬位置，不改逻辑。

import (
	"context"
	"encoding/json"
	"log/slog"
	"time"
)

// rpaCmdEnvelope Redis "rpa:cmd" 通道负载：跨实例投递的命令。
type rpaCmdEnvelope struct {
	From     string     `json:"from"` // 发起实例 ID
	To       string     `json:"to"`   // 目标实例 ID（持有该 client WS 连接的实例）
	ClientID string     `json:"client_id"`
	// UserID 是发起方声明的调用者身份；目标实例据此校验 client owner（防跨用户投递）。
	UserID string     `json:"user_id,omitempty"`
	Msg    RPAMessage `json:"msg"`
}
// rpaResEnvelope Redis "rpa:res" 通道负载：跨实例命令的结果回传。
type rpaResEnvelope struct {
	From string     `json:"from"` // 执行实例 ID（日志/审计用）
	Msg  RPAMessage `json:"msg"`
}
// crossInstanceLoop 消费 Redis channel：远程命令投递 + 远程结果回填。
func (h *RPAHub) crossInstanceLoop(ctx context.Context) {
	defer func() {
		if r := recover(); r != nil {
			slog.Error("rpa cross-instance loop panic", "panic", r)
		}
	}()
	ch := h.psub.Channel()
	for msg := range ch {
		switch msg.Channel {
		case h.cmdChannel:
			var env rpaCmdEnvelope
			if err := json.Unmarshal([]byte(msg.Payload), &env); err != nil {
				slog.Warn("rpa cmd envelope unmarshal", "error", err)
				continue
			}
			h.handleRemoteCmd(&env)
		case h.resChannel:
			var env rpaResEnvelope
			if err := json.Unmarshal([]byte(msg.Payload), &env); err != nil {
				slog.Warn("rpa res envelope unmarshal", "error", err)
				continue
			}
			h.handleRemoteRes(&env)
		}
	}
}
// handleRemoteCmd 目标实例收到跨实例命令：写本地 WS 连接。
// 结果由插件回传后走 HandleResult（本地 pending 未命中 → 转发到 rpa:res）。
func (h *RPAHub) handleRemoteCmd(env *rpaCmdEnvelope) {
	if env.To != h.instanceID {
		return
	}
	client, ok := h.GetClient(env.ClientID)
	if !ok {
		slog.Warn("rpa remote cmd: client not connected on this instance",
			"client_id", env.ClientID, "from", env.From)
		return
	}
	// 归属校验：发起方声明的调用者必须与本地连接的 owner 一致（防跨用户投递）。
	if env.UserID != "" && client.UserID != env.UserID {
		slog.Warn("rpa remote cmd rejected: client owner mismatch",
			"client_id", env.ClientID, "caller", env.UserID, "owner", client.UserID)
		return
	}
	if err := client.SendMessage(env.Msg); err != nil {
		slog.Warn("rpa remote cmd send failed", "client_id", env.ClientID, "error", err)
	}
}
// handleRemoteRes 发起实例收到跨实例结果：按 msg.ID 回填本地 pending。
func (h *RPAHub) handleRemoteRes(env *rpaResEnvelope) {
	h.mu.RLock()
	ch, ok := h.pending[env.Msg.ID]
	h.mu.RUnlock()
	if !ok {
		return // 非本实例发起（或已超时清理），静默忽略
	}
	result := &RPAResult{Result: env.Msg.Result}
	if env.Msg.Error != nil {
		result.Error = env.Msg.Error
	}
	select {
	case ch <- result:
	default:
		slog.Warn("rpa remote res channel full", "id", env.Msg.ID)
	}
}
// putRegistry 写/续期 client 注册记录（滑动 TTL）。
func (h *RPAHub) putRegistry(ctx context.Context, clientID, userID string) {
	payload, err := json.Marshal(map[string]interface{}{
		"instance_id": h.instanceID,
		"user_id":     userID,
	})
	if err != nil {
		return
	}
	if err := h.rdb.Set(ctx, h.registryKeyPrefix+clientID, payload, rpaRegistryTTL).Err(); err != nil {
		slog.Debug("rpa registry set failed", "client_id", clientID, "error", err)
	}
}
// RefreshRegistry WS pong 心跳续期注册（防止活跃连接被 TTL 误清理）。
func (h *RPAHub) RefreshRegistry(clientID, userID string) {
	if h.localOnly {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	h.putRegistry(ctx, clientID, userID)
}
// registryOwner 返回注册中心记录的 client 所在实例 ID；无记录返回 ""。
func (h *RPAHub) registryOwner(clientID string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	data, err := h.rdb.Get(ctx, h.registryKeyPrefix+clientID).Bytes()
	if err != nil {
		return ""
	}
	var rec struct {
		InstanceID string `json:"instance_id"`
		UserID     string `json:"user_id"`
	}
	if err := json.Unmarshal(data, &rec); err != nil {
		return ""
	}
	return rec.InstanceID
}
// dropRegistry 移除 client 注册记录（连接断开时调用）。
func (h *RPAHub) dropRegistry(clientID string) {
	if h.localOnly {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := h.rdb.Del(ctx, h.registryKeyPrefix+clientID).Err(); err != nil {
		slog.Debug("rpa registry del failed", "client_id", clientID, "error", err)
	}
}
// registryUser 读注册记录里的 user_id（按用户过滤列举时用）。
func (h *RPAHub) registryUser(ctx context.Context, key string) string {
	data, err := h.rdb.Get(ctx, key).Bytes()
	if err != nil {
		return ""
	}
	var rec struct {
		UserID string `json:"user_id"`
	}
	if err := json.Unmarshal(data, &rec); err != nil {
		return ""
	}
	return rec.UserID
}
// registryIDFromKey 从注册 key（prefix+clientID）解析 clientID。
func registryIDFromKey(key, prefix string) (string, bool) {
	if len(key) <= len(prefix) || key[:len(prefix)] != prefix {
		return "", false
	}
	return key[len(prefix):], true
}
