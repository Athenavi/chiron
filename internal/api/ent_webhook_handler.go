package api

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/redis/go-redis/v9"
)

// ── 企业 Webhook 注册与事件通知 ──────────────────────────────────────────

// EntWebhookHandler 提供企业 Webhook 注册/管理 API 与事件入流。
// 事件持久化与可靠投递由 WebhookDispatcher（Redis 消费组，多实例共享）负责，
// 不再使用进程内 chan（实例崩溃不丢事件）。
type EntWebhookHandler struct {
	secretKey []byte // AES-256 key derived from APP_SECRET
}

// NewEntWebhookHandler 创建 Webhook handler（注册/管理 + 事件入流）。
func NewEntWebhookHandler() *EntWebhookHandler {
	// P0-S4: 从 APP_SECRET 派生 webhook 加密密钥，避免 secret 明文存储
	return &EntWebhookHandler{secretKey: deriveWebhookKey()}
}

// deriveWebhookKey 从 APP_SECRET 派生 32-byte AES-256 key。
// 若 APP_SECRET 未设置，使用 HMAC-SHA256 派生（保证密钥熵而非直接返回固定值）。
func deriveWebhookKey() []byte {
	secret := os.Getenv("APP_SECRET")
	length := 32
	if secret == "" {
		// APP_SECRET 为空时使用 HMAC-SHA256 派生，确保密钥非空且有熵
		slog.Warn("APP_SECRET not set, webhook encryption uses fallback key (not safe for production)")
		mac := hmac.New(sha256.New, []byte("chiron-webhook-encryption-fallback"))
		mac.Write([]byte("chiron-webhook-key-derivation"))
		return mac.Sum(nil)[:length]
	}
	if len(secret) >= length {
		return []byte(secret)[:length]
	}
	mac := hmac.New(sha256.New, []byte("chiron-webhook-encryption"))
	mac.Write([]byte(secret))
	return mac.Sum(nil)
}

// RegisterRoutes 挂载 Webhook 管理路由（authMW + RequireEntPerm("webhook:manage")）。
func (h *EntWebhookHandler) RegisterRoutes(
	mux *http.ServeMux,
	authMW func(http.Handler) http.Handler,
	rlMW func(http.Handler) http.Handler,
	cfg *config.Config,
) {
	permMW := RequireEntPerm("webhook:manage")
	mux.Handle("GET /v1/ent/webhooks", authMW(permMW(http.HandlerFunc(h.List))))
	mux.Handle("POST /v1/ent/webhooks", authMW(permMW(http.HandlerFunc(h.Create))))
	mux.Handle("GET /v1/ent/webhooks/{id}", authMW(permMW(http.HandlerFunc(h.Get))))
	mux.Handle("PUT /v1/ent/webhooks/{id}", authMW(permMW(http.HandlerFunc(h.Update))))
	mux.Handle("DELETE /v1/ent/webhooks/{id}", authMW(permMW(http.HandlerFunc(h.Delete))))
	// 内部端点：Python 引擎推送事件。
	//
	// ⚠️ 此前是 `internalTokenMW(nil, …)` —— 而 internalTokenMW 首行即读
	// `cfg.InternalToken`，于是**每个请求都 nil 解引用 panic**（未认证即可无限触发，
	// 只被 Recover 兜成 500）；且该端点漏了 rlMW（对比 routes_public.go 的 internal
	// 端点全部是 `rlMW(internalTokenMW(cfg, …))`）。两者叠加使 IngestEvent **永不执行**
	// —— 企业 webhook 事件注入功能整体失效。
	mux.Handle("POST /v1/internal/webhook-event",
		rlMW(internalTokenMW(cfg, http.HandlerFunc(h.IngestEvent))))
}

// ── 数据模型 ──

// Webhook 数据库行
type webhookRow struct {
	ID          string    `json:"id"`
	TenantID    string    `json:"tenant_id"`
	Name        string    `json:"name"`
	EventTypes  []string  `json:"event_types"`
	URL         string    `json:"url"`
	Secret      string    `json:"-"`
	Enabled     bool      `json:"enabled"`
	RetryPolicy string    `json:"retry_policy"`
	CreatedAt   time.Time `json:"created_at"`
	UpdatedAt   time.Time `json:"updated_at"`
}

// WebhookEvent Python 引擎推送的事件。
// ID 为引擎生成的事件幂等键（透传 X-Webhook-Event-Id，接收方据此去重）。
type WebhookEvent struct {
	ID        string                 `json:"id,omitempty"`
	TenantID  string                 `json:"tenant_id"`
	Type      string                 `json:"type"`
	Payload   map[string]interface{} `json:"payload"`
	Timestamp time.Time              `json:"timestamp"`
}

// 加密 webhook secret（AES-256-GCM）
func encryptWebhookSecret(plaintext, key string) (string, error) {
	if key == "" {
		return "", fmt.Errorf("encryption key is empty, cannot encrypt webhook secret")
	}
	block, err := aes.NewCipher([]byte(key))
	if err != nil {
		return "", err
	}
	aesGCM, err := cipher.NewGCM(block)
	if err != nil {
		return "", err
	}
	nonce := make([]byte, aesGCM.NonceSize())
	if _, err := io.ReadFull(rand.Reader, nonce); err != nil {
		return "", err
	}
	ciphertext := aesGCM.Seal(nonce, nonce, []byte(plaintext), nil)
	return base64.StdEncoding.EncodeToString(ciphertext), nil
}

func decryptWebhookSecret(cipherB64, key string) (string, error) {
	if key == "" {
		return cipherB64, nil
	}
	block, err := aes.NewCipher([]byte(key))
	if err != nil {
		return "", err
	}
	aesGCM, err := cipher.NewGCM(block)
	if err != nil {
		return "", err
	}
	data, err := base64.StdEncoding.DecodeString(cipherB64)
	if err != nil {
		return "", err
	}
	nonceSize := aesGCM.NonceSize()
	if len(data) < nonceSize {
		return "", fmt.Errorf("ciphertext too short")
	}
	nonce, ciphertext := data[:nonceSize], data[nonceSize:]
	plaintext, err := aesGCM.Open(nil, nonce, ciphertext, nil)
	if err != nil {
		return "", err
	}
	return string(plaintext), nil
}

// 查询列
const webhookColumns = `id::text, tenant_id::text, name, event_types::text, url, secret,
	enabled, retry_policy::text, created_at, updated_at`

// ── CRUD ──

func (h *EntWebhookHandler) List(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenantID := entPolicyTenantID(claims)

	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT `+webhookColumns+` FROM ent_webhooks WHERE tenant_id = $1 ORDER BY created_at DESC`,
		tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "webhook list failed")
		return
	}
	defer rows.Close()

	out := []map[string]interface{}{}
	for rows.Next() {
		out = append(out, scanWebhookRow(rows))
	}
	OK(w, map[string]interface{}{"webhooks": out, "total": len(out)})
}

func (h *EntWebhookHandler) Create(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenantID := entPolicyTenantID(claims)

	var body struct {
		Name       string   `json:"name"`
		EventTypes []string `json:"event_types"`
		URL        string   `json:"url"`
		Secret     string   `json:"secret"`
		Enabled    bool     `json:"enabled"`
	}
	if err := DecodeJSON(w, r, &body); err != nil || body.URL == "" || len(body.EventTypes) == 0 {
		BadRequest(w, "url and event_types are required")
		return
	}

	encrypted, err := encryptWebhookSecret(body.Secret, string(h.secretKey))
	if err != nil {
		InternalError(w, "secret encryption failed")
		return
	}

	retryRaw, _ := json.Marshal(map[string]interface{}{
		"max_retries":     3,
		"backoff_seconds": 5,
	})
	eventTypesRaw, _ := json.Marshal(body.EventTypes)
	id := newUUID()

	_, err = db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO ent_webhooks (id, tenant_id, name, event_types, url, secret, enabled, retry_policy)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
		id, tenantID, body.Name, eventTypesRaw, body.URL, encrypted, body.Enabled, retryRaw)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create webhook failed")
		return
	}
	OK(w, map[string]string{"id": id, "status": "created"})
}

func (h *EntWebhookHandler) Get(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenantID := entPolicyTenantID(claims)
	id := r.PathValue("id")

	row := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT `+webhookColumns+` FROM ent_webhooks WHERE id = $1 AND tenant_id = $2`,
		id, tenantID)
	out := scanWebhookRow(row)
	if out == nil {
		NotFound(w, "webhook not found")
		return
	}
	OK(w, out)
}

func (h *EntWebhookHandler) Update(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenantID := entPolicyTenantID(claims)
	id := r.PathValue("id")

	var body struct {
		Name       *string  `json:"name"`
		EventTypes []string `json:"event_types"`
		URL        *string  `json:"url"`
		Secret     *string  `json:"secret"`
		Enabled    *bool    `json:"enabled"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}

	sets := []string{}
	args := []interface{}{}
	idx := 1

	if body.Name != nil {
		sets = append(sets, fmt.Sprintf("name = $%d", idx))
		args = append(args, *body.Name)
		idx++
	}
	if body.EventTypes != nil {
		raw, _ := json.Marshal(body.EventTypes)
		sets = append(sets, fmt.Sprintf("event_types = $%d", idx))
		args = append(args, raw)
		idx++
	}
	if body.URL != nil {
		sets = append(sets, fmt.Sprintf("url = $%d", idx))
		args = append(args, *body.URL)
		idx++
	}
	if body.Secret != nil {
		encrypted, err := encryptWebhookSecret(*body.Secret, string(h.secretKey))
		if err != nil {
			InternalError(w, "secret encryption failed")
			return
		}
		sets = append(sets, fmt.Sprintf("secret = $%d", idx))
		args = append(args, encrypted)
		idx++
	}
	if body.Enabled != nil {
		sets = append(sets, fmt.Sprintf("enabled = $%d", idx))
		args = append(args, *body.Enabled)
		idx++
	}
	if len(sets) == 0 {
		BadRequest(w, "nothing to update")
		return
	}
	sets = append(sets, "updated_at = NOW()")
	args = append(args, id, tenantID)

	_, err := db.GlobalDBManager.Exec(r.Context(),
		`UPDATE ent_webhooks SET `+joinComma(sets)+` WHERE id = $`+itoa(idx)+` AND tenant_id = $`+itoa(idx+1),
		args...)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "update webhook failed")
		return
	}
	OK(w, map[string]string{"status": "updated"})
}

func (h *EntWebhookHandler) Delete(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenantID := entPolicyTenantID(claims)
	id := r.PathValue("id")

	tag, err := db.GlobalDBManager.Exec(r.Context(),
		`DELETE FROM ent_webhooks WHERE id = $1 AND tenant_id = $2`, id, tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "delete webhook failed")
		return
	}
	if tag.RowsAffected() == 0 {
		NotFound(w, "webhook not found")
		return
	}
	OK(w, map[string]string{"status": "deleted"})
}

// ── 事件投递 ──

// IngestEvent POST /v1/internal/webhook-event — Python 引擎推送事件入口。
// 事件先持久化到 Redis Stream webhook:events（MAXLEN 限界），由 WebhookDispatcher
// 消费组跨实例投递（at-least-once + event_id 幂等）；入口立即 202，投递不依赖本实例存活。
func (h *EntWebhookHandler) IngestEvent(w http.ResponseWriter, r *http.Request) {
	var evt WebhookEvent
	if err := DecodeJSON(w, r, &evt); err != nil {
		BadRequest(w, "invalid event")
		return
	}
	if evt.TenantID == "" || evt.Type == "" {
		BadRequest(w, "tenant_id and type are required")
		return
	}
	if evt.ID == "" {
		evt.ID = newUUID() // 兼容旧负载：缺省生成幂等键
	}
	if evt.Timestamp.IsZero() {
		evt.Timestamp = time.Now()
	}
	if db.Redis == nil {
		ServiceUnavailable(w, "redis unavailable")
		return
	}
	payloadJSON, _ := json.Marshal(evt.Payload)
	_, err := db.Redis.XAdd(r.Context(), &redis.XAddArgs{
		Stream: db.RedisKey("webhook:events"),
		MaxLen: webhookEventsMaxLen,
		Approx: true,
		Values: map[string]interface{}{
			"event_id":    evt.ID,
			"tenant_id":   evt.TenantID,
			"type":        evt.Type,
			"payload":     string(payloadJSON),
			"timestamp":   evt.Timestamp.UTC().Format(time.RFC3339Nano),
			"retry_count": "0",
		},
	}).Result()
	if err != nil {
		slog.Warn("webhook event xadd failed", "type", evt.Type, "tenant", evt.TenantID, "error", err)
		InternalError(w, "event persist failed")
		return
	}
	OK(w, map[string]string{"status": "queued", "event_id": evt.ID})
}

// ── 辅助 ──

func scanWebhookRow(row interface{}) map[string]interface{} {
	// 从 *db.Row 或 *db.Rows 扫描
	type scanner interface {
		Scan(dest ...interface{}) error
	}
	s, ok := row.(scanner)
	if !ok {
		return nil
	}
	var id, tenantID, name, eventTypesRaw, url, secret, retryRaw string
	var enabled bool
	var createdAt, updatedAt time.Time
	if err := s.Scan(&id, &tenantID, &name, &eventTypesRaw, &url, &secret, &enabled, &retryRaw, &createdAt, &updatedAt); err != nil {
		return nil
	}
	var eventTypes []string
	_ = json.Unmarshal([]byte(eventTypesRaw), &eventTypes)
	var retryPolicy map[string]interface{}
	_ = json.Unmarshal([]byte(retryRaw), &retryPolicy)
	return map[string]interface{}{
		"id":           id,
		"tenant_id":    tenantID,
		"name":         name,
		"event_types":  eventTypes,
		"url":          url,
		"enabled":      enabled,
		"retry_policy": retryPolicy,
		"created_at":   createdAt,
		"updated_at":   updatedAt,
	}
}
