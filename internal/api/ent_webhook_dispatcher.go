package api

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/redis/go-redis/v9"
)

// ── 企业 Webhook 投递器（多实例可靠投递）──
//
// 事件经 IngestEvent 持久化到 Redis Stream webhook:events；本投递器以消费组
// （webhook-workers）跨实例共享读取并 HTTP 投递：
//   - at-least-once + event_id 幂等（透传 X-Webhook-Event-Id，接收方去重）；
//   - 投递失败按 retry_policy/默认 3 次退避（5/10/20s）→ 消息级重试（requeue，
//     retry_count 递增），超限移入 DLQ（webhook:events:dlq）；
//   - 实例崩溃后未 ACK 消息由 claimLoop（idle 阈值）被其它实例认领续投；
//   - SSRF 防护：目标 URL 拒绝私网/回环/链路本地（含云元数据 169.254.169.254）。

const (
	webhookEventsMaxLen = 100000 // 入流/重投 MAXLEN
	webhookDLQMaxLen    = 10000
	webhookGroup        = "webhook-workers"
	webhookMaxRetries   = 3                // 消息级 requeue 上限（每次 requeue 内部含 3 次退避投递）
	webhookClaimIdle    = 90 * time.Second // 崩溃认领阈值
	webhookClaimEvery   = 60 * time.Second // 认领扫描周期
)

// WebhookDispatcher 消费 webhook:events 并投递到匹配订阅。
type WebhookDispatcher struct {
	rdb       db.RedisClient
	consumer  string
	secretKey []byte // AES key（解密 ent_webhooks.secret）
}

// NewWebhookDispatcher 创建投递器。consumer 名带随机后缀（多实例唯一）。
func NewWebhookDispatcher(rdb db.RedisClient) *WebhookDispatcher {
	return &WebhookDispatcher{
		rdb:       rdb,
		consumer:  fmt.Sprintf("webhook-%d-%d", os.Getpid(), time.Now().UnixNano()%1e6),
		secretKey: deriveWebhookKey(),
	}
}

// Start 启动投递主循环 + 崩溃认领循环（ctx 控制优雅停止）。
func (d *WebhookDispatcher) Start(ctx context.Context) {
	if d.rdb == nil {
		slog.Warn("webhook dispatcher disabled: redis unavailable")
		return
	}
	if err := d.rdb.XGroupCreateMkStream(ctx, db.RedisKey("webhook:events"), webhookGroup, "0").Err(); err != nil {
		// BUSYGROUP 已存在属正常
		slog.Debug("webhook consumer group create", "error", err)
	}
	go d.claimLoop(ctx)

	slog.Info("webhook dispatcher started", "consumer", d.consumer)
	sem := make(chan struct{}, 32) // 并发投递上限（慢目标不阻塞队列）
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}
		msgs, err := d.rdb.XReadGroup(ctx, &redis.XReadGroupArgs{
			Group:    webhookGroup,
			Consumer: d.consumer,
			Streams:  []string{db.RedisKey("webhook:events"), ">"},
			Count:    10,
			Block:    5 * time.Second,
		}).Result()
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			slog.Debug("webhook xreadgroup error", "error", err)
			time.Sleep(500 * time.Millisecond)
			continue
		}
		for _, stream := range msgs {
			for _, msg := range stream.Messages {
				select {
				case sem <- struct{}{}:
				case <-ctx.Done():
					return
				}
				go func(m redis.XMessage) {
					defer func() { <-sem }()
					d.handleMessage(ctx, m)
				}(msg)
			}
		}
	}
}

// handleMessage 投递一条事件：成功 ACK；失败 requeue（retry_count++）；超限进 DLQ。
func (d *WebhookDispatcher) handleMessage(ctx context.Context, msg redis.XMessage) {
	evt, ok := parseWebhookEvent(msg.Values)
	if !ok {
		slog.Warn("webhook event malformed, acking", "msg_id", msg.ID)
		_ = d.rdb.XAck(ctx, db.RedisKey("webhook:events"), webhookGroup, msg.ID)
		return
	}
	streamKey := db.RedisKey("webhook:events")
	// 单事件整体投递时限（含多订阅与重试）；超时视为失败走 requeue
	dctx, dcancel := contextWithTimeout(120 * time.Second)
	defer dcancel()
	if err := d.deliver(dctx, evt); err == nil {
		_ = d.rdb.XAck(ctx, streamKey, webhookGroup, msg.ID)
		return
	}
	// 投递失败：requeue 或 DLQ
	retry := 1
	if rc, err := strconv.Atoi(str(msg.Values["retry_count"])); err == nil {
		retry = rc + 1
	}
	if retry > webhookMaxRetries {
		_ = d.rdb.XAck(ctx, streamKey, webhookGroup, msg.ID)
		_, _ = d.rdb.XAdd(ctx, &redis.XAddArgs{
			Stream: db.RedisKey("webhook:events:dlq"),
			MaxLen: webhookDLQMaxLen, Approx: true,
			Values: msg.Values,
		}).Result()
		slog.Error("webhook event exhausted retries, moved to DLQ", "event_id", evt.ID, "msg_id", msg.ID)
		return
	}
	msg.Values["retry_count"] = strconv.Itoa(retry)
	if _, err := d.rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: db.RedisKey("webhook:events"), MaxLen: webhookEventsMaxLen, Approx: true,
		Values: msg.Values,
	}).Result(); err != nil {
		slog.Warn("webhook requeue failed, dropping", "event_id", evt.ID, "error", err)
	}
	_ = d.rdb.XAck(ctx, streamKey, webhookGroup, msg.ID)
	slog.Warn("webhook event requeued", "event_id", evt.ID, "retry", retry)
}

// claimLoop 崩溃恢复：认领 idle 超阈值且仍 pending 的消息重新投递。
func (d *WebhookDispatcher) claimLoop(ctx context.Context) {
	ticker := time.NewTicker(webhookClaimEvery)
	defer ticker.Stop()
	stream := db.RedisKey("webhook:events")
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			pend, err := d.rdb.XPendingExt(ctx, &redis.XPendingExtArgs{
				Stream: stream,
				Group:  webhookGroup,
				Start:  "-",
				End:    "+",
				Count:  100,
				Idle:   webhookClaimIdle,
			}).Result()
			if err != nil || len(pend) == 0 {
				continue
			}
			ids := make([]string, 0, len(pend))
			for _, p := range pend {
				ids = append(ids, p.ID)
			}
			claimed, err := d.rdb.XClaim(ctx, &redis.XClaimArgs{
				Stream:   stream,
				Group:    webhookGroup,
				Consumer: d.consumer,
				MinIdle:  webhookClaimIdle,
				Messages: ids,
			}).Result()
			if err != nil {
				continue
			}
			for _, m := range claimed {
				mm := m
				go d.handleMessage(context.WithoutCancel(ctx), mm)
			}
		}
	}
}

// deliver 查询匹配订阅并逐一投递；全部成功返回 nil，任一失败返回错误（整体重投）。
func (d *WebhookDispatcher) deliver(ctx context.Context, evt WebhookEvent) error {
	rows, err := db.GlobalDBManager.Query(ctx,
		`SELECT `+webhookColumns+` FROM ent_webhooks
		 WHERE tenant_id = $1 AND enabled = true
		 AND event_types::jsonb @> to_jsonb($2::text)`,
		evt.TenantID, evt.Type)
	if err != nil {
		slog.Error("webhook deliver: query failed", "error", err)
		return err
	}
	defer rows.Close()

	var firstErr error
	for rows.Next() {
		wh := scanWebhookRow(rows)
		if wh == nil {
			continue
		}
		targetURL, _ := wh["url"].(string)
		whID, _ := wh["id"].(string)
		if err := validateWebhookURL(targetURL); err != nil {
			slog.Warn("webhook url rejected (ssrf guard), skipping", "webhook_id", whID, "url", targetURL, "reason", err)
			continue // SSRF 拒绝的订阅不投递、不阻塞其它订阅
		}
		secret, _ := wh["secret"].(string)
		decrypted, _ := decryptWebhookSecret(secret, string(d.secretKey))
		if err := d.postWebhook(ctx, evt, targetURL, decrypted, whID); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}

// postWebhook 单订阅投递：3 次指数退避重试，记录投递审计。
func (d *WebhookDispatcher) postWebhook(ctx context.Context, evt WebhookEvent, targetURL, secret, whID string) error {
	body, _ := json.Marshal(map[string]interface{}{
		"event_id":   evt.ID,
		"event_type": evt.Type,
		"payload":    evt.Payload,
		"timestamp":  evt.Timestamp,
	})
	maxAttempts := 3
	var lastErr error
	for attempt := 0; attempt < maxAttempts; attempt++ {
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, targetURL, bytesReader(body))
		if err != nil {
			lastErr = err
			break
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Webhook-Event-Id", evt.ID)
		if secret != "" {
			req.Header.Set("X-Webhook-Signature", signHMACSHA256(body, secret))
		}
		resp, err := httpClient.Do(req)
		if err == nil {
			resp.Body.Close()
			if resp.StatusCode < 500 {
				d.auditDeliver(ctx, evt, whID, "success", "")
				return nil
			}
			lastErr = fmt.Errorf("webhook target returned HTTP %d", resp.StatusCode)
		} else {
			lastErr = err
		}
		if attempt < maxAttempts-1 {
			time.Sleep(time.Duration(1<<attempt) * 5 * time.Second) // 5s, 10s
		}
	}
	d.auditDeliver(ctx, evt, whID, "failed", lastErr.Error())
	return lastErr
}

func (d *WebhookDispatcher) auditDeliver(ctx context.Context, evt WebhookEvent, whID, status, reason string) {
	details := map[string]interface{}{"event_type": evt.Type, "event_id": evt.ID, "status": status}
	if reason != "" {
		details["error"] = reason
	}
	_, _ = db.GlobalDBManager.Exec(ctx,
		`INSERT INTO audit_logs (id, tenant_id, action, resource_type, resource_id, details)
		 VALUES ($1, $2, $3, $4, $5, $6)`,
		newUUID(), evt.TenantID, "webhook.delivered", "webhook", whID, details)
}

// ── 事件/URL 解析与 SSRF 校验 ──

func parseWebhookEvent(fields map[string]interface{}) (WebhookEvent, bool) {
	evt := WebhookEvent{}
	evt.ID = str(fields["event_id"])
	evt.TenantID = str(fields["tenant_id"])
	evt.Type = str(fields["type"])
	if evt.TenantID == "" || evt.Type == "" {
		return evt, false
	}
	if ts, err := time.Parse(time.RFC3339Nano, str(fields["timestamp"])); err == nil {
		evt.Timestamp = ts
	}
	if raw := str(fields["payload"]); raw != "" {
		_ = json.Unmarshal([]byte(raw), &evt.Payload)
	}
	if evt.Payload == nil {
		evt.Payload = map[string]interface{}{}
	}
	return evt, true
}

func str(v interface{}) string {
	switch t := v.(type) {
	case string:
		return t
	case []byte:
		return string(t)
	default:
		if t == nil {
			return ""
		}
		return fmt.Sprintf("%v", t)
	}
}

// validateWebhookURL SSRF 防护：仅 http/https，禁止私网/回环/链路本地/未指定地址。
// 注：DNS 解析一次，极端 rebinding 场景建议配合出口网络白名单（部署层）。
func validateWebhookURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil {
		return fmt.Errorf("parse url: %w", err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return fmt.Errorf("scheme not allowed: %s", u.Scheme)
	}
	host := u.Hostname()
	if host == "" {
		return fmt.Errorf("empty host")
	}
	ips, err := net.LookupIP(host)
	if err != nil {
		return fmt.Errorf("dns lookup: %w", err)
	}
	for _, ip := range ips {
		if ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() || ip.IsUnspecified() {
			return fmt.Errorf("target address not allowed: %s", ip)
		}
	}
	return nil
}
