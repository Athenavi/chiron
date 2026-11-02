package db

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"log/slog"
	"net/http"
	"runtime/debug"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

// AuditEntry 审计日志条目
type AuditEntry struct {
	ID        string    `json:"id"`
	TenantID  string    `json:"tenant_id"`
	UserID    string    `json:"user_id"`
	Action    string    `json:"action"`   // "session.create", "tool.execute", "agent.run"
	Resource  string    `json:"resource"` // 资源标识
	Detail    string    `json:"detail"`   // 详细信息
	IP        string    `json:"ip"`       // 客户端 IP
	Success   bool      `json:"success"`  // 是否成功
	Error     string    `json:"error"`    // 错误信息
	Timestamp time.Time `json:"timestamp"`
}

// Auditor 审计日志记录器
type Auditor struct {
	rdb    RedisClient
	stream string
}

// NewAuditor 创建审计日志记录器
func NewAuditor(rdb RedisClient) *Auditor {
	return &Auditor{
		rdb:    rdb,
		stream: RedisKey("audit:events"),
	}
}

// Log 记录审计事件到 Redis Streams
func (a *Auditor) Log(ctx context.Context, entry AuditEntry) {
	if a.rdb == nil {
		slog.Debug("审计日志跳过（Redis 不可用）", "action", entry.Action)
		return
	}

	entry.Timestamp = time.Now()
	data, err := json.Marshal(entry)
	if err != nil {
		slog.Error("审计日志序列化失败", "error", err)
		return
	}

	_, err = a.rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: a.stream,
		MaxLen: 100000, // 最多保留 10 万条
		Approx: true,
		Values: map[string]any{
			"tenant_id": entry.TenantID,
			"user_id":   entry.UserID,
			"action":    entry.Action,
			"resource":  entry.Resource,
			"success":   entry.Success,
			"data":      string(data),
		},
	}).Result()

	if err != nil {
		slog.Error("审计日志写入失败", "error", err, "action", entry.Action)
		return
	}

	slog.Debug("审计日志已记录",
		"action", entry.Action,
		"user", entry.UserID,
		"tenant", entry.TenantID,
		"success", entry.Success,
	)
}

// UserExtractor 从请求中提取用户信息的函数类型
// 返回 (userID, tenantID)
type UserExtractor func(r *http.Request) (string, string)

// LogAuditMiddleware 审计日志中间件
// extractUser: 从请求中提取用户信息的函数，可以为 nil
func LogAuditMiddleware(auditor *Auditor, extractUser UserExtractor) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			// 包装 ResponseWriter 以捕获状态码
			ww := &auditResponseWriter{ResponseWriter: w, statusCode: 200}

			next.ServeHTTP(ww, r)

			// 记录审计日志
			entry := AuditEntry{
				Action:   r.Method + " " + r.URL.Path,
				Resource: r.URL.String(),
				IP:       r.RemoteAddr,
				Success:  ww.statusCode < 400,
			}

			// 提取用户信息
			if extractUser != nil {
				entry.UserID, entry.TenantID = extractUser(r)
			}

			auditor.Log(r.Context(), entry)
		})
	}
}

// auditResponseWriter 包装 ResponseWriter 以捕获状态码
type auditResponseWriter struct {
	http.ResponseWriter
	statusCode int
}

func (w *auditResponseWriter) WriteHeader(code int) {
	w.statusCode = code
	w.ResponseWriter.WriteHeader(code)
}

// AuditLog 全局审计日志函数（简化接口）
// 用于 middleware.go 中的快速调用
func AuditLog(ctx context.Context, userID string, tenantID int16, action, resource, detail, ip string, meta map[string]interface{}) {
	if Redis == nil {
		return
	}

	// 将 meta 合并到 detail 中，避免额外元数据被丢弃
	if meta != nil {
		metaJSON, err := json.Marshal(meta)
		if err == nil {
			detail = detail + " | meta: " + string(metaJSON)
		}
	}

	entry := AuditEntry{
		TenantID:  strconv.Itoa(int(tenantID)),
		UserID:    userID,
		Action:    action,
		Resource:  resource,
		Detail:    detail,
		IP:        ip,
		Success:   true,
		Timestamp: time.Now(),
	}

	data, err := json.Marshal(entry)
	if err != nil {
		return
	}

	if err := Redis.XAdd(ctx, &redis.XAddArgs{
		Stream: RedisKey("audit:events"),
		MaxLen: 100000,
		Approx: true,
		Values: map[string]any{
			"tenant_id": strconv.Itoa(int(tenantID)),
			"user_id":   userID,
			"action":    action,
			"resource":  resource,
			"data":      string(data),
		},
	}).Err(); err != nil {
		slog.Warn("audit log: XAdd failed", "error", err)
	}
}

// AuditConsumer 审计日志消费者
type AuditConsumer struct {
	rdb     RedisClient
	stream  string
	group   string
	handler func(ctx context.Context, entry AuditEntry) error
}

// 崩溃恢复参数。
//
// 为什么必须有这一组：主循环用 `XREADGROUP > ` 只读**新**消息，处理失败的消息留在
// PEL（pending entries list）里。此前**没有任何认领逻辑** —— 于是
//
//   - 进程在"已读取、未 ACK"之间被杀（发布、OOM、节点回收），那条审计记录就永久
//     滞留在 PEL 里，谁也再读不到。审计是合规产物，"少一条且无人知晓"比报错更糟；
//   - 处理失败的消息同样永不重试，而 PEL 只增不减 —— 长期运行后 XPENDING 越滚越大，
//     内存与 XPENDING/XAUTOCLAIM 的耗时都随之上涨。
//
// 与 webhook dispatcher 的 claimLoop 同一套语义，阈值取一致，便于运维只记一组数字。
const (
	auditClaimIdle      = 90 * time.Second // 认领阈值：超过它仍未 ACK 视为原消费者已死
	auditClaimEvery     = 60 * time.Second // 扫描周期
	auditMaxDeliveries  = 3                // 单条消息最大投递次数，超过则 ACK 止损
	auditClaimBatchSize = 100
)

// runAuditCycle 执行一轮审计消费/认领，把 panic 限制在**这一轮**内。
//
// 为什么必须有：Go 里 goroutine 中未捕获的 panic 会终止**整个进程**。审计消费者是
// 进程级常驻任务，处理的是从 Redis 流里读回的任意消息（可能来自更早版本、可能被截断、
// 可能被外部写入），而 handleMessage 里有 json.Unmarshal 与 handler 回调。一次越界或
// nil 解引用就足以让网关整体退出；若该消息总在 PEL 里被重投（它不会被 ACK），
// 重启后会立刻再炸 —— 变成 CrashLoop，且日志指向与用户操作无关的时刻。
//
// 必须放在 `db` 包内而不是复用 api 包的助手：api 依赖 db，反向引用会形成循环。
func runAuditCycle(name string, fn func()) {
	defer func() {
		if r := recover(); r != nil {
			slog.Error("audit consumer cycle panicked; continuing",
				"cycle", name, "panic", r, "stack", string(debug.Stack()))
		}
	}()
	fn()
}

// NewAuditConsumer 创建审计日志消费者
func NewAuditConsumer(rdb RedisClient, handler func(ctx context.Context, entry AuditEntry) error) *AuditConsumer {
	return &AuditConsumer{
		rdb:     rdb,
		stream:  RedisKey("audit:events"),
		group:   "audit-processor",
		handler: handler,
	}
}

// handleMessage 处理一条审计消息；返回 true 表示可以 ACK（已处理或被判定为不可处理）。
//
// 返回 false 时**故意不 ACK**：消息留在 PEL 里，由 claimLoop 在 idle 超阈值后重投，
// 投递次数超过 auditMaxDeliveries 才止损。这样"瞬时故障"能自愈，"永久损坏的消息"
// 也不会变成无限重投的毒丸。
func (c *AuditConsumer) handleMessage(ctx context.Context, msg redis.XMessage) bool {
	data, ok := msg.Values["data"].(string)
	if !ok {
		// 结构不对的消息重投多少次都一样：ACK 掉，否则它会在 PEL 里永远占位。
		slog.Warn("审计日志消息缺少 data 字段，丢弃", "msg_id", msg.ID)
		return true
	}

	var entry AuditEntry
	if err := json.Unmarshal([]byte(data), &entry); err != nil {
		slog.Error("审计日志反序列化失败，丢弃", "error", err, "msg_id", msg.ID)
		return true
	}

	if err := c.handler(ctx, entry); err != nil {
		slog.Error("审计日志处理失败，稍后重试", "error", err, "action", entry.Action, "msg_id", msg.ID)
		return false
	}
	return true
}

// claimLoop 崩溃恢复：认领 idle 超阈值且仍 pending 的消息重新处理。
func (c *AuditConsumer) claimLoop(ctx context.Context, consumerID string) {
	ticker := time.NewTicker(auditClaimEvery)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}

		// 每轮独立兜 panic：认领与重投要能一直转下去，不能因为一条坏消息
		// 就停掉整个崩溃恢复链路（见 runAuditCycle 注释）。
		runAuditCycle("audit-claim", func() { c.claimOnce(ctx, consumerID) })
	}
}

// claimOnce 执行一轮"扫描 PEL → 止损超限消息 → 认领 → 重投"。
func (c *AuditConsumer) claimOnce(ctx context.Context, consumerID string) {
	pend, err := c.rdb.XPendingExt(ctx, &redis.XPendingExtArgs{
		Stream: c.stream,
		Group:  c.group,
		Start:  "-",
		End:    "+",
		Count:  auditClaimBatchSize,
		Idle:   auditClaimIdle,
	}).Result()
	if err != nil || len(pend) == 0 {
		return
	}

	ids := make([]string, 0, len(pend))
	for _, p := range pend {
		if p.RetryCount >= auditMaxDeliveries {
			// 止损：ACK 掉超限消息，否则它会一直占据 PEL 并被反复认领。
			// 审计记录丢失必须可见 —— 这是唯一能发现"有审计没落库"的线索。
			slog.Error("审计日志消息超过投递上限，放弃",
				"msg_id", p.ID, "deliveries", p.RetryCount, "consumer", p.Consumer)
			c.rdb.XAck(ctx, c.stream, c.group, p.ID)
			continue
		}
		ids = append(ids, p.ID)
	}
	if len(ids) == 0 {
		return
	}

	claimed, err := c.rdb.XClaim(ctx, &redis.XClaimArgs{
		Stream:   c.stream,
		Group:    c.group,
		Consumer: consumerID,
		MinIdle:  auditClaimIdle,
		Messages: ids,
	}).Result()
	if err != nil {
		return
	}
	for _, msg := range claimed {
		// 与主循环一致：成功才 ACK。用 WithoutCancel 保证优雅关闭期间的认领
		// 不会做到一半被取消（认领后不 ACK 只会让它更久地卡在 PEL 里）。
		//
		// panic 时不 ACK：这条消息退出本轮，留在 PEL 里由下一轮重投，
		// 达到 auditMaxDeliveries 后由上面的止损分支 ACK —— 与"处理失败"同一路径。
		msg := msg
		runAuditCycle("audit-claim-msg", func() {
			if c.handleMessage(context.WithoutCancel(ctx), msg) {
				c.rdb.XAck(ctx, c.stream, c.group, msg.ID)
			}
		})
	}
}

// Start 启动消费者
func (c *AuditConsumer) Start(ctx context.Context) error {
	if c.rdb == nil {
		return nil
	}

	// 创建消费者组
	if err := c.rdb.XGroupCreateMkStream(ctx, c.stream, c.group, "0").Err(); err != nil {
		// BUSYGROUP 表示消费组已存在（非错误），因 redis 库未导出专门错误类型，回退字符串判断
		if !strings.Contains(err.Error(), "BUSYGROUP") {
			slog.Warn("audit consumer group create error", "error", err)
		}
	}

	// 随机后缀：多实例同秒启动时 consumer 名仍唯一（同消费组内同名 consumer 会造成重复投递窗口）
	var rb [4]byte
	_, _ = rand.Read(rb[:])
	consumerID := "audit-worker-" + time.Now().Format("20060102150405") + "-" + hex.EncodeToString(rb[:])

	go c.claimLoop(ctx, consumerID)

	for {
		select {
		case <-ctx.Done():
			return nil
		default:
		}

		results, err := c.rdb.XReadGroup(ctx, &redis.XReadGroupArgs{
			Group:    c.group,
			Consumer: consumerID,
			Streams:  []string{c.stream, ">"},
			Count:    10,
			Block:    5 * time.Second,
		}).Result()

		if err != nil {
			if err == redis.Nil {
				continue
			}
			// 优雅关闭时 XReadGroup 会被取消，这不是错误，不该刷 Error 日志。
			if ctx.Err() != nil {
				return nil
			}
			slog.Error("审计日志消费失败", "error", err)
			time.Sleep(time.Second)
			continue
		}

		for _, result := range results {
			for _, msg := range result.Messages {
				// 失败的消息不 ACK，留给 claimLoop 重投（见 handleMessage 注释）。
				// panic 同样不 ACK：退出本轮、留 PEL 等重投，达到投递上限后由
				// claimOnce 的止损分支 ACK —— 与"处理失败"同一路径。
				msg := msg
				runAuditCycle("audit-consume-msg", func() {
					if c.handleMessage(ctx, msg) {
						c.rdb.XAck(ctx, c.stream, c.group, msg.ID)
					}
				})
			}
		}
	}
}
