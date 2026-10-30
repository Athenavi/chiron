package api

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/google/uuid"
	"github.com/gorilla/websocket"
	"github.com/redis/go-redis/v9"
)

// ── RPA 消息类型 ──

type RPAMessageType string

const (
	RPAMsgCommand RPAMessageType = "command"
	RPAMsgResult  RPAMessageType = "result"
	RPAMsgEvent   RPAMessageType = "event"
	RPAMsgAck     RPAMessageType = "ack"
)

// RPAMessage 是所有 RPA WebSocket 消息的统一 envelope
type RPAMessage struct {
	Type   RPAMessageType         `json:"type"`
	ID     string                 `json:"id"`
	Method string                 `json:"method,omitempty"`
	Params map[string]interface{} `json:"params,omitempty"`
	Result map[string]interface{} `json:"result,omitempty"`
	Error  *RPAError              `json:"error,omitempty"`
	TabID  int                    `json:"tabId,omitempty"`
	TS     int64                  `json:"ts"`
}

type RPAError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

func (e *RPAError) Error() string {
	return fmt.Sprintf("rpa error %d: %s", e.Code, e.Message)
}

// RPACommand 封装发送给插件的命令
type RPACommand struct {
	Method string
	Params map[string]interface{}
	TabID  int
}

// RPAResult 封装插件返回的结果
type RPAResult struct {
	Result map[string]interface{}
	Error  *RPAError
}

// ── RPA 客户端 ──

type RPAClient struct {
	ID       string
	Conn     *websocket.Conn
	UserID   string
	Tabs     []RPATabInfo
	LastSeen time.Time
	mu       sync.Mutex
}

type RPATabInfo struct {
	ID    int    `json:"id"`
	URL   string `json:"url"`
	Title string `json:"title"`
}

func (c *RPAClient) TouchLastSeen() {
	c.mu.Lock()
	c.LastSeen = time.Now()
	c.mu.Unlock()
}

func (c *RPAClient) SendMessage(msg RPAMessage) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	msg.TS = time.Now().UnixMilli()
	// P1 修复：写超时，防止死客户端无限阻塞广播 goroutine
	_ = c.Conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
	return c.Conn.WriteJSON(msg)
}

// ── RPA 跨实例桥接（批 D：多网关副本）──
//
// 背景：clients/pending 均为进程内注册表。多实例部署时，浏览器插件 WebSocket
// 可能落在网关副本 A，而 python engine 的 POST /v1/rpa/exec 经负载均衡落到
// 副本 B —— 原实现会在 B 的 hub 中找不到对端连接。
//
// 方案（与 broadcast.Hub 同款 Redis Pub/Sub 模式，见 internal/broadcast/hub.go）：
//   - 每实例持有自己进程内的 WS 连接（物理连接不可迁移），并维护 Redis 注册中心：
//     key = {REDIS_KEY_PREFIX}rpa:client:{clientID}，value = {instance_id,user_id}，
//     TTL = rpaRegistryTTL，由 WS ping/pong（30s 间隔）滑动续期；
//   - 跨实例命令：exec 落在副本 B 时按注册中心定位目标副本 A，把命令 publish 到
//     Redis channel "rpa:cmd"（envelope 带 To=目标实例），A 的订阅者写本地 WS；
//   - 结果回传：插件回 result 时 A 的本地 pending 未命中 → publish 到 Redis channel
//     "rpa:res"，B 按 msg.ID 命中本地 pending 并返回（与广播 Hub 的 origin 去重同思路）。
//   - Redis 不可用（rdb == nil）时保持单机进程内行为（localOnly=true）。

const (
	rpaRegistryTTL       = 90 * time.Second // client 注册 TTL；WS ping 间隔 30s，留 3 倍余量
	rpaCrossInstanceWait = 15 * time.Second // 远端实例命令等待上限（远端离线/无订阅时快速失败）
)



// RPAHub 管理本实例进程内的 RPA 插件连接，并桥接跨网关副本的命令/结果。
type RPAHub struct {
	mu      sync.RWMutex
	clients map[string]*RPAClient      // clientID → client（本实例持有）
	pending map[string]chan *RPAResult // msgID → result channel（本实例发起）

	// Redis 跨实例桥接（rdb == nil 时为单机模式，localOnly=true）
	rdb               db.RedisClient
	localOnly         bool
	instanceID        string
	registryKeyPrefix string
	cmdChannel        string
	resChannel        string
	startOnce         sync.Once
	psub              *redis.PubSub
}

func NewRPAHub(rdb db.RedisClient) *RPAHub {
	h := &RPAHub{
		clients:           make(map[string]*RPAClient),
		pending:           make(map[string]chan *RPAResult),
		rdb:               rdb,
		localOnly:         rdb == nil,
		instanceID:        uuid.NewString(),
		registryKeyPrefix: db.RedisKey("rpa:client:"),
		cmdChannel:        db.RedisKey("rpa:cmd"),
		resChannel:        db.RedisKey("rpa:res"),
	}
	if h.localOnly {
		slog.Info("rpa hub running in local-only mode (no Redis); cross-instance commands disabled")
	}
	return h
}

// Start 启动跨实例订阅者（幂等），由 NewGatewayRouter 调用一次。
// localOnly 时为空操作；进程退出时订阅随进程回收，无需显式 Close。
func (h *RPAHub) Start(ctx context.Context) {
	if h.localOnly {
		return
	}
	h.startOnce.Do(func() {
		h.psub = h.rdb.Subscribe(context.Background(), h.cmdChannel, h.resChannel)
		go h.crossInstanceLoop(ctx)
		slog.Info("rpa hub cross-instance bridge started", "instance_id", h.instanceID[:8])
	})
}




// ── Redis 注册中心 ──





// Register 注册一个插件连接。返回 false 表示该 client_id 已被**其它用户**占用 ——
// 拒绝顶替：否则他人用同一 id 连接即可顶掉原连接，并接管引擎发往该 id 的浏览器命令。
func (h *RPAHub) Register(client *RPAClient) bool {
	h.mu.Lock()
	if existing, ok := h.clients[client.ID]; ok && existing.UserID != client.UserID {
		h.mu.Unlock()
		slog.Warn("rpa register rejected: client_id owned by another user",
			"client_id", client.ID, "owner_user", existing.UserID)
		return false
	}
	h.clients[client.ID] = client
	h.mu.Unlock()
	if !h.localOnly {
		h.RefreshRegistry(client.ID, client.UserID)
	}
	slog.Info("rpa client registered", "client_id", client.ID, "user_id", client.UserID, "instance", h.instanceID[:8])
	return true
}

// Unregister 注销一个插件连接
func (h *RPAHub) Unregister(clientID string) {
	h.mu.Lock()
	delete(h.clients, clientID)
	h.mu.Unlock()
	h.dropRegistry(clientID)
	slog.Info("rpa client unregistered", "client_id", clientID)
}

// GetClient 获取指定客户端
func (h *RPAHub) GetClient(clientID string) (*RPAClient, bool) {
	h.mu.RLock()
	defer h.mu.RUnlock()
	c, ok := h.clients[clientID]
	return c, ok
}

// GetClientByUser 获取指定用户的最近活跃客户端
func (h *RPAHub) GetClientByUser(userID string) (*RPAClient, bool) {
	h.mu.RLock()
	defer h.mu.RUnlock()
	var latest *RPAClient
	for _, c := range h.clients {
		if c.UserID == userID {
			if latest == nil || c.LastSeen.After(latest.LastSeen) {
				latest = c
			}
		}
	}
	return latest, latest != nil
}

// SendCommand 发送命令并等待结果（带超时）。
// 本地未命中时若 Redis 注册中心指示对端在其它网关副本，则跨实例投递并等待结果。
func (h *RPAHub) SendCommand(ctx context.Context, clientID string, cmd *RPACommand, userID string) (*RPAResult, error) {
	if client, ok := h.GetClient(clientID); ok {
		// 归属校验：调用者（引擎按其当前用户传入）必须与连接 owner 一致，
		// 否则会把命令投给另一个用户的浏览器。
		if userID != "" && client.UserID != userID {
			return nil, fmt.Errorf("rpa client not owned by caller: %s", clientID)
		}
		return h.sendLocal(ctx, client, cmd)
	}
	if h.localOnly {
		return nil, fmt.Errorf("rpa client not connected: %s", clientID)
	}
	// 跨实例：查注册中心定位持有该 client 的网关副本
	owner := h.registryOwner(clientID)
	if owner == "" || owner == h.instanceID {
		// 无记录 = 未连接；记录在本实例但本地已断 = 清理窗口，与未连接等价
		return nil, fmt.Errorf("rpa client not connected: %s", clientID)
	}
	return h.sendRemote(ctx, clientID, owner, cmd, userID)
}

// sendLocal 本实例持有连接：原同步请求-响应路径。
func (h *RPAHub) sendLocal(ctx context.Context, client *RPAClient, cmd *RPACommand) (*RPAResult, error) {
	msgID := fmt.Sprintf("cmd_%s_%d", h.instanceID[:8], time.Now().UnixNano())
	msg := RPAMessage{
		Type:   RPAMsgCommand,
		ID:     msgID,
		Method: cmd.Method,
		Params: cmd.Params,
		TabID:  cmd.TabID,
	}

	// 注册 pending channel
	ch := make(chan *RPAResult, 1)
	h.mu.Lock()
	h.pending[msgID] = ch
	h.mu.Unlock()

	defer func() {
		h.mu.Lock()
		delete(h.pending, msgID)
		h.mu.Unlock()
	}()

	// 发送命令
	if err := client.SendMessage(msg); err != nil {
		return nil, fmt.Errorf("send command: %w", err)
	}

	// 等待结果
	select {
	case result := <-ch:
		return result, nil
	case <-ctx.Done():
		return nil, fmt.Errorf("rpa command timeout: %s", cmd.Method)
	}
}

// sendRemote 跨实例投递：命令发到目标实例，结果经 rpa:res 回填本地 pending。
func (h *RPAHub) sendRemote(ctx context.Context, clientID, owner string, cmd *RPACommand, userID string) (*RPAResult, error) {
	msgID := fmt.Sprintf("cmd_%s_%d", h.instanceID, time.Now().UnixNano())
	msg := RPAMessage{
		Type:   RPAMsgCommand,
		ID:     msgID,
		Method: cmd.Method,
		Params: cmd.Params,
		TabID:  cmd.TabID,
	}

	ch := make(chan *RPAResult, 1)
	h.mu.Lock()
	h.pending[msgID] = ch
	h.mu.Unlock()

	defer func() {
		h.mu.Lock()
		delete(h.pending, msgID)
		h.mu.Unlock()
	}()

	env := rpaCmdEnvelope{From: h.instanceID, To: owner, ClientID: clientID, UserID: userID, Msg: msg}
	data, err := json.Marshal(env)
	if err != nil {
		return nil, fmt.Errorf("marshal rpa cmd: %w", err)
	}
	pctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := h.rdb.Publish(pctx, h.cmdChannel, data).Err(); err != nil {
		return nil, fmt.Errorf("publish rpa command: %w", err)
	}

	// 等待远端结果；超时（目标实例离线/插件无响应）则失败，语义与本地一致
	waitCtx, waitCancel := context.WithTimeout(ctx, rpaCrossInstanceWait)
	defer waitCancel()
	select {
	case result := <-ch:
		return result, nil
	case <-waitCtx.Done():
		return nil, fmt.Errorf("rpa command timeout (target instance %s): %s", owner, cmd.Method)
	}
}

// HandleResult 处理从插件返回的结果。
// 本地 pending 未命中且启用了 Redis 桥接时，把结果转发回发起实例（跨实例请求）。
func (h *RPAHub) HandleResult(msg *RPAMessage) {
	h.mu.RLock()
	ch, ok := h.pending[msg.ID]
	h.mu.RUnlock()

	if !ok {
		if h.localOnly {
			slog.Warn("rpa result for unknown msg", "id", msg.ID)
			return
		}
		// 跨实例请求的结果：转发给发起实例（其本地 pending 持有该 msgID）
		h.forwardResult(msg)
		return
	}

	result := &RPAResult{Result: msg.Result}
	if msg.Error != nil {
		result.Error = msg.Error
	}
	ch <- result
}

// forwardResult 把本地未匹配的插件结果发布到 rpa:res，供发起实例回填。
func (h *RPAHub) forwardResult(msg *RPAMessage) {
	env := rpaResEnvelope{From: h.instanceID, Msg: *msg}
	data, err := json.Marshal(env)
	if err != nil {
		return
	}
	pctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := h.rdb.Publish(pctx, h.resChannel, data).Err(); err != nil {
		slog.Debug("rpa res publish failed", "id", msg.ID, "error", err)
	}
}

// BroadcastToUser 向指定用户的所有客户端广播事件
func (h *RPAHub) BroadcastToUser(userID string, msg RPAMessage) {
	h.mu.RLock()
	defer h.mu.RUnlock()
	for _, c := range h.clients {
		if c.UserID == userID {
			if err := c.SendMessage(msg); err != nil {
				slog.Warn("rpa broadcast failed", "client_id", c.ID, "error", err)
			}
		}
	}
}

// ConnectedClients 返回已连接的客户端列表
func (h *RPAHub) ConnectedClients() []*RPAClient {
	h.mu.RLock()
	defer h.mu.RUnlock()
	clients := make([]*RPAClient, 0, len(h.clients))
	for _, c := range h.clients {
		clients = append(clients, c)
	}
	return clients
}

// AllClientIDs 返回全量已连接客户端（本实例 + Redis 注册中心）。
// 供跨实例的 /v1/rpa/clients 使用：python engine 无论请求落在哪个副本都能看到全部插件。
func (h *RPAHub) AllClientIDs() []string {
	set := make(map[string]struct{})
	for _, c := range h.ConnectedClients() {
		set[c.ID] = struct{}{}
	}
	if !h.localOnly {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		var cursor uint64
		for {
			keys, next, err := h.rdb.Scan(ctx, cursor, h.registryKeyPrefix+"*", 100).Result()
			if err != nil {
				slog.Debug("rpa registry scan failed", "error", err)
				break
			}
			for _, k := range keys {
				if id, ok := registryIDFromKey(k, h.registryKeyPrefix); ok {
					set[id] = struct{}{}
				}
			}
			cursor = next
			if cursor == 0 {
				break
			}
		}
	}
	ids := make([]string, 0, len(set))
	for id := range set {
		ids = append(ids, id)
	}
	return ids
}

// ClientIDsForUser 返回指定用户可见的已连接客户端（本实例 + Redis 注册中心）。
// userID 为空时退化为全量（兼容未带身份的旧调用方）。
func (h *RPAHub) ClientIDsForUser(userID string) []string {
	if userID == "" {
		return h.AllClientIDs()
	}
	set := make(map[string]struct{})
	for _, c := range h.ConnectedClients() {
		if c.UserID == userID {
			set[c.ID] = struct{}{}
		}
	}
	if !h.localOnly {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		var cursor uint64
		for {
			keys, next, err := h.rdb.Scan(ctx, cursor, h.registryKeyPrefix+"*", 100).Result()
			if err != nil {
				slog.Debug("rpa registry scan failed", "error", err)
				break
			}
			for _, k := range keys {
				id, ok := registryIDFromKey(k, h.registryKeyPrefix)
				if !ok {
					continue
				}
				if h.registryUser(ctx, k) == userID {
					set[id] = struct{}{}
				}
			}
			cursor = next
			if cursor == 0 {
				break
			}
		}
	}
	ids := make([]string, 0, len(set))
	for id := range set {
		ids = append(ids, id)
	}
	return ids
}



// ExecCommand sends a command to a connected browser extension and returns the result map.
// This implements tools.RPABrowserHub interface, breaking the import cycle.
func (h *RPAHub) ExecCommand(ctx context.Context, clientID string, method string, params map[string]interface{}, userID string) (map[string]interface{}, error) {
	cmd := &RPACommand{Method: method, Params: params}
	result, err := h.SendCommand(ctx, clientID, cmd, userID)
	if err != nil {
		return nil, err
	}
	if result.Error != nil {
		return nil, fmt.Errorf("rpa error %d: %s", result.Error.Code, result.Error.Message)
	}
	return result.Result, nil
}

// ConnectedClientIDs returns the IDs of all connected RPA clients.
// This implements tools.RPABrowserHub interface.
func (h *RPAHub) ConnectedClientIDs() []string {
	clients := h.ConnectedClients()
	ids := make([]string, len(clients))
	for i, c := range clients {
		ids[i] = c.ID
	}
	return ids
}

// ── RPA WebSocket Handler ──

var rpaUpgrader = websocket.Upgrader{
	ReadBufferSize:  4096,
	WriteBufferSize: 4096,
	CheckOrigin:     checkWebSocketOrigin,
}

const (
	rpaReadTimeout  = 60 * time.Second
	rpaWriteTimeout = 10 * time.Second
	rpaPingInterval = 30 * time.Second
)

// RPAWebSocketHandler 处理 RPA 插件的 WebSocket 连接
func RPAWebSocketHandler(hub *RPAHub, authenticator *auth.Authenticator) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// 验证 JWT token
		token := r.URL.Query().Get("token")
		clientID := r.URL.Query().Get("client_id")
		if clientID == "" {
			http.Error(w, "client_id required", http.StatusBadRequest)
			return
		}

		claims, err := authenticator.ValidateToken(token)
		if err != nil || claims == nil {
			http.Error(w, "invalid or missing token", http.StatusUnauthorized)
			return
		}
		userID := claims.UserID

		conn, err := rpaUpgrader.Upgrade(w, r, nil)
		if err != nil {
			slog.Warn("rpa ws upgrade", "error", err)
			return
		}

		client := &RPAClient{
			ID:       clientID,
			Conn:     conn,
			UserID:   userID,
			LastSeen: time.Now(),
		}
		if !hub.Register(client) {
			// 同 client_id 已被他人占用：拒绝并断开，避免静默顶替（接管他人的浏览器命令）。
			slog.Warn("rpa ws rejected: client_id already in use by another user", "client_id", clientID)
			conn.Close()
			return
		}

		// 发送连接确认
		client.SendMessage(RPAMessage{
			Type: RPAMsgAck,
			ID:   "init",
			Result: map[string]interface{}{
				"status":    "connected",
				"client_id": clientID,
			},
		})

		// 设置读写超时
		conn.SetReadDeadline(time.Now().Add(rpaReadTimeout))
		conn.SetPongHandler(func(string) error {
			conn.SetReadDeadline(time.Now().Add(rpaReadTimeout))
			client.TouchLastSeen()
			// 续期 Redis 注册（多副本下防 TTL 误清理活跃连接）
			hub.RefreshRegistry(client.ID, client.UserID)
			return nil
		})

		// 心跳 goroutine
		done := make(chan struct{})
		go func() {
			defer func() {
				if r := recover(); r != nil {
					slog.Error("rpa heartbeat panic", "panic", r)
				}
			}()
			ticker := time.NewTicker(rpaPingInterval)
			defer ticker.Stop()
			for {
				select {
				case <-ticker.C:
					if err := conn.WriteControl(websocket.PingMessage, nil, time.Now().Add(rpaWriteTimeout)); err != nil {
						return
					}
				case <-done:
					return
				}
			}
		}()

		// 消息读取循环
		defer func() {
			close(done)
			hub.Unregister(clientID)
			conn.Close()
		}()

		for {
			_, message, err := conn.ReadMessage()
			if err != nil {
				if websocket.IsUnexpectedCloseError(err, websocket.CloseGoingAway, websocket.CloseNormalClosure) {
					slog.Warn("rpa ws read error", "error", err)
				}
				break
			}

			var msg RPAMessage
			if err := json.Unmarshal(message, &msg); err != nil {
				slog.Warn("rpa ws unmarshal", "error", err)
				continue
			}

			client.TouchLastSeen()

			switch msg.Type {
			case RPAMsgResult:
				hub.HandleResult(&msg)
			case RPAMsgEvent:
				handleRPAEvent(hub, client, &msg)
			default:
				slog.Debug("rpa ws unknown msg type", "type", msg.Type)
			}
		}
	}
}

// ── RPA HTTP Bridge（Python engine → Go gateway → 浏览器插件） ──

// rpaInternalTokenOK 常量时间比较 X-Internal-Token（网关↔引擎互信）。
func rpaInternalTokenOK(r *http.Request, internalToken string) bool {
	if internalToken == "" {
		return false
	}
	provided := r.Header.Get("X-Internal-Token")
	return provided != "" && subtle.ConstantTimeCompare([]byte(provided), []byte(internalToken)) == 1
}

// RPAExecHandler 供 Python engine 的 GatewayBrowserHub 把浏览器命令发给
// 已连接插件(Chrome Extension /ws/rpa)。要求共享 internal token，防止直连滥用。
// 多副本下命令可跨实例投递：本实例未持有该 client 时经 Redis 注册中心路由到目标副本。
func RPAExecHandler(hub *RPAHub, internalToken string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !rpaInternalTokenOK(r, internalToken) {
			Unauthorized(w, "invalid internal token")
			return
		}
		var req struct {
			ClientID string                 `json:"client_id"`
			Method   string                 `json:"method"`
			Params   map[string]interface{} `json:"params"`
			// UserID 由引擎按其当前工具上下文的用户注入；非空时网关校验 client owner。
			UserID string `json:"user_id,omitempty"`
		}
		if err := DecodeJSON(w, r, &req); err != nil || req.Method == "" || req.ClientID == "" {
			BadRequest(w, "client_id and method are required")
			return
		}
		result, err := hub.ExecCommand(r.Context(), req.ClientID, req.Method, req.Params, req.UserID)
		if err != nil {
			JSON(w, http.StatusBadGateway, APIResponse{Success: false, Error: "rpa command execution failed"})
			return
		}
		OK(w, result)
	}
}

// RPAClientsHandler 返回已连接浏览器插件客户端列表（本实例 + Redis 注册中心的全量）。
func RPAClientsHandler(hub *RPAHub, internalToken string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !rpaInternalTokenOK(r, internalToken) {
			Unauthorized(w, "invalid internal token")
			return
		}
		// 归属过滤：引擎按其当前用户查询 —— 未带 user_id 时退化为全量（兼容旧引擎）。
	OK(w, map[string]interface{}{"client_ids": hub.ClientIDsForUser(r.URL.Query().Get("user_id"))})
	}
}

// handleRPAEvent 处理插件主动推送的事件
func handleRPAEvent(hub *RPAHub, client *RPAClient, msg *RPAMessage) {
	switch msg.Method {
	case "tab_updated", "tab_created", "tab_closed":
		slog.Info("rpa tab event", "client_id", client.ID, "event", msg.Method, "tab_id", msg.TabID)
	case "init":
		if tabs, ok := msg.Params["tabs"].([]interface{}); ok {
			client.Tabs = make([]RPATabInfo, 0, len(tabs))
			for _, t := range tabs {
				if tab, ok := t.(map[string]interface{}); ok {
					info := RPATabInfo{}
					if id, ok := tab["id"].(float64); ok {
						info.ID = int(id)
					}
					if url, ok := tab["url"].(string); ok {
						info.URL = url
					}
					if title, ok := tab["title"].(string); ok {
						info.Title = title
					}
					client.Tabs = append(client.Tabs, info)
				}
			}
		}
		slog.Info("rpa client init", "client_id", client.ID, "tabs", len(client.Tabs))
	default:
		slog.Debug("rpa unknown event", "method", msg.Method)
	}
}
