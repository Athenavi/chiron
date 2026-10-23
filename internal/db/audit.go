package db

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"log/slog"
	"net/http"
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

// NewAuditConsumer 创建审计日志消费者
func NewAuditConsumer(rdb RedisClient, handler func(ctx context.Context, entry AuditEntry) error) *AuditConsumer {
	return &AuditConsumer{
		rdb:     rdb,
		stream:  RedisKey("audit:events"),
		group:   "audit-processor",
		handler: handler,
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
			slog.Error("审计日志消费失败", "error", err)
			time.Sleep(time.Second)
			continue
		}

		for _, result := range results {
			for _, msg := range result.Messages {
				data, ok := msg.Values["data"].(string)
				if !ok {
					continue
				}

				var entry AuditEntry
				if err := json.Unmarshal([]byte(data), &entry); err != nil {
					slog.Error("审计日志反序列化失败", "error", err)
					continue
				}

				if err := c.handler(ctx, entry); err != nil {
					slog.Error("审计日志处理失败", "error", err, "action", entry.Action)
					continue
				}

				// 确认消息
				c.rdb.XAck(ctx, c.stream, c.group, msg.ID)
			}
		}
	}
}
