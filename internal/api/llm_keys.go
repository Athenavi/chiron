package api

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"log/slog"
	"net/http"
	"strings"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/settings"
)

// ── LLM Provider 密钥管理（集中派，DR：docs/llm-provider-key-management-dr.md）──
//
// 权威层:llm_provider_keys 表(密文,internal/settings AES-256-GCM 加密)。
// 运行时层:Redis keyset(llm:keys:{provider} hash: field=key_hash[:12] →
// JSON{key 明文,status}) + 版本号 + 变更 PUBLISH,供引擎 KeyRing 同步。
// 本文件为网关侧实现;/v1/admin/api-keys 在此本地处理(不再转发引擎)。

var (
	llmKeysHashPrefix     = db.RedisKey("llm:keys:")
	llmKeysVerKey         = db.RedisKey("llm:keys:ver")
	llmKeysChangedChannel = db.RedisKey("llm:keys:changed")
)

const llmKeysStatuses = "active rate_limited circuit_open"

func llmKeyHash(provider, key string) string {
	d := sha256.Sum256([]byte(provider + ":" + key))
	return hex.EncodeToString(d[:])
}

// llmEnc 网关侧加解密入口(懒构造,复用 internal/settings 的 APP_SECRET 派生 AEAD)。
func llmEnc(cfg *config.Config) *settings.Store {
	return settings.New(db.Pool, cfg.AppSecret)
}

// syncProviderKeyset 在写库后重建该 provider 的 Redis keyset 并广播变更。
// Redis 不可用时仅返回 nil(仅 DB 权威;引擎退化为 env 种子),调用方按需告警。
func syncProviderKeyset(ctx context.Context, cfg *config.Config, provider string) error {
	if db.Redis == nil {
		return nil
	}
	type row struct {
		encrypted string
		keyHash   string
		status    string
	}
	var rows []row
	if db.Pool != nil {
		rs, err := db.Pool.Query(ctx,
			`SELECT encrypted_key, key_hash, status FROM llm_provider_keys WHERE provider = $1`,
			provider)
		if err != nil {
			return err
		}
		defer rs.Close()
		for rs.Next() {
			var r row
			if err := rs.Scan(&r.encrypted, &r.keyHash, &r.status); err != nil {
				continue
			}
			rows = append(rows, r)
		}
		if err := rs.Err(); err != nil {
			return err
		}
	}

	hashKey := llmKeysHashPrefix + provider
	enc := llmEnc(cfg)
	if len(rows) == 0 {
		if err := db.Redis.Del(ctx, hashKey).Err(); err != nil {
			slog.Warn("llm keyset del failed", "provider", provider, "error", err)
		}
	} else {
		args := make([]interface{}, 0, 1+2*len(rows))
		args = append(args, hashKey)
		for _, r := range rows {
			plain, err := enc.DecryptString(r.encrypted)
			if err != nil {
				slog.Warn("llm keyset decrypt failed, skipping key", "provider", provider, "error", err)
				continue
			}
			payload, _ := json.Marshal(map[string]string{"k": plain, "s": r.status})
			args = append(args, r.keyHash[:12], string(payload))
		}
		if len(args) > 1 {
			cmdArgs := append([]interface{}{"HMSET"}, args...)
			if err := db.Redis.Do(ctx, cmdArgs...).Err(); err != nil {
				slog.Warn("llm keyset hmset failed", "provider", provider, "error", err)
			}
		}
	}
	_, _ = db.Redis.Incr(ctx, llmKeysVerKey).Result()
	_ = db.Redis.Publish(ctx, llmKeysChangedChannel, provider).Err()
	return nil
}

// ── HTTP handlers(注册于 /v1/admin/api-keys,鉴权由路由中间件完成)──

func (h *AdminHandler) ListLLMKeys(w http.ResponseWriter, r *http.Request) {
	if db.Pool == nil {
		InternalError(w, "database unavailable")
		return
	}
	rows, err := db.Pool.Query(r.Context(),
		`SELECT id, provider, key_hash, status, COALESCE(remark, '') FROM llm_provider_keys
		 ORDER BY provider, created_at`)
	if err != nil {
		slog.Error("list llm keys", "error", err)
		InternalError(w, "failed to list api keys")
		return
	}
	defer rows.Close()

	keys := make([]map[string]interface{}, 0)
	stats := map[string]interface{}{
		"total": 0, "active": 0, "rate_limited": 0, "circuit_open": 0, "providers": map[string]interface{}{},
	}
	for rows.Next() {
		var id, provider, keyHash, status, remark string
		if err := rows.Scan(&id, &provider, &keyHash, &status, &remark); err != nil {
			continue
		}
		keys = append(keys, map[string]interface{}{
			"id":       keyHash[:12],
			"provider": provider,
			"key":      "•••••" + keyHash[:8], // 仅指纹，不触碰明文
			"status":   status,
			"remark":   remark,
		})
		stats["total"] = stats["total"].(int) + 1
		stats[status] = stats[status].(int) + 1
	}
	OK(w, map[string]interface{}{"keys": keys, "stats": stats})
}

func (h *AdminHandler) AddLLMKey(w http.ResponseWriter, r *http.Request) {
	if db.Pool == nil {
		InternalError(w, "database unavailable")
		return
	}
	var body struct {
		Provider string `json:"provider"`
		Key      string `json:"key"`
		Remark   string `json:"remark"`
		// BaseURL 可选：与 key 一并保存该 provider 的端点覆盖（system_settings python 分类）
		BaseURL string `json:"base_url"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	body.Provider = strings.ToLower(strings.TrimSpace(body.Provider))
	body.Key = strings.TrimSpace(body.Key)
	body.BaseURL = strings.TrimSpace(body.BaseURL)
	if body.Provider == "" || body.Key == "" {
		BadRequest(w, "provider and key are required")
		return
	}
	// provider 名会作为 system_settings 键前缀与 Redis keyset 后缀，限定字符集避免注入。
	if !validLLMProviderName(body.Provider) {
		BadRequest(w, "invalid provider: only a-z 0-9 . _ - are allowed (max 64 chars)")
		return
	}
	if body.BaseURL != "" && !validProviderBaseURL(body.BaseURL) {
		BadRequest(w, "invalid base_url: must be an http(s) URL with a host")
		return
	}

	enc := llmEnc(h.cfg)
	cipher, err := enc.EncryptString(body.Key)
	if err != nil {
		slog.Error("llm key encrypt failed", "error", err)
		InternalError(w, "encryption unavailable (check APP_SECRET)")
		return
	}
	keyHash := llmKeyHash(body.Provider, body.Key)

	var exists int
	if err := db.Pool.QueryRow(r.Context(),
		`SELECT COUNT(*) FROM llm_provider_keys WHERE key_hash = $1`, keyHash).Scan(&exists); err == nil && exists > 0 {
		BadRequest(w, "api key already exists for this provider")
		return
	}

	if _, err := db.Pool.Exec(r.Context(),
		`INSERT INTO llm_provider_keys (id, provider, encrypted_key, key_hash, status, remark, created_at, updated_at)
		 VALUES (gen_random_uuid()::text, $1, $2, $3, 'active', $4, NOW(), NOW())`,
		body.Provider, cipher, keyHash, nullIfEmpty(body.Remark)); err != nil {
		slog.Error("insert llm key", "error", err)
		InternalError(w, "failed to add api key")
		return
	}
	if err := syncProviderKeyset(r.Context(), h.cfg, body.Provider); err != nil {
		slog.Warn("sync keyset after add", "provider", body.Provider, "error", err)
	}
	// 端点覆盖随 key 一并保存（可选）：写入后引擎侧 base_url 优先取 DB 覆盖。
	if body.BaseURL != "" {
		if store := h.ensureSettingsStore(); store != nil {
			userID := ""
			if claims := auth.GetClaims(r.Context()); claims != nil {
				userID = claims.ID
			}
			if err := store.SaveConfig(r.Context(), "python",
				map[string]interface{}{llmProviderBaseURLKey(body.Provider): strings.TrimRight(body.BaseURL, "/")},
				userID); err != nil {
				slog.Warn("save provider base url failed", "provider", body.Provider, "error", err)
			}
		} else {
			slog.Warn("settings store unavailable, provider base url not saved", "provider", body.Provider)
		}
	}
	OK(w, map[string]string{"status": "added", "provider": body.Provider})
}

func (h *AdminHandler) UpdateLLMKeyStatus(w http.ResponseWriter, r *http.Request) {
	if db.Pool == nil {
		InternalError(w, "database unavailable")
		return
	}
	digest := strings.ToLower(r.PathValue("id"))
	if len(digest) != 12 {
		NotFound(w, "api key not found")
		return
	}
	var body struct {
		Status string `json:"status"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if !strings.Contains(llmKeysStatuses, body.Status) {
		BadRequest(w, "invalid status: "+body.Status)
		return
	}
	tag, err := db.Pool.Exec(r.Context(),
		`UPDATE llm_provider_keys SET status = $1, updated_at = NOW() WHERE left(key_hash, 12) = $2`,
		body.Status, digest)
	if err != nil {
		slog.Error("update llm key status", "error", err)
		InternalError(w, "failed to update api key")
		return
	}
	if tag.RowsAffected() == 0 {
		NotFound(w, "api key not found")
		return
	}
	var provider string
	_ = db.Pool.QueryRow(r.Context(),
		`SELECT provider FROM llm_provider_keys WHERE left(key_hash, 12) = $1`, digest).Scan(&provider)
	if provider != "" {
		_ = syncProviderKeyset(r.Context(), h.cfg, provider)
	}
	OK(w, map[string]interface{}{"status": "updated", "id": digest, "key_status": body.Status})
}

func (h *AdminHandler) DeleteLLMKey(w http.ResponseWriter, r *http.Request) {
	if db.Pool == nil {
		InternalError(w, "database unavailable")
		return
	}
	digest := strings.ToLower(r.PathValue("id"))
	if len(digest) != 12 {
		NotFound(w, "api key not found")
		return
	}
	var provider string
	_ = db.Pool.QueryRow(r.Context(),
		`SELECT provider FROM llm_provider_keys WHERE left(key_hash, 12) = $1`, digest).Scan(&provider)
	tag, err := db.Pool.Exec(r.Context(),
		`DELETE FROM llm_provider_keys WHERE left(key_hash, 12) = $1`, digest)
	if err != nil {
		slog.Error("delete llm key", "error", err)
		InternalError(w, "failed to delete api key")
		return
	}
	if tag.RowsAffected() == 0 {
		NotFound(w, "api key not found")
		return
	}
	if provider != "" {
		_ = syncProviderKeyset(r.Context(), h.cfg, provider)
	}
	OK(w, map[string]interface{}{"status": "deleted", "id": digest})
}

func nullIfEmpty(s string) interface{} {
	if strings.TrimSpace(s) == "" {
		return nil
	}
	return s
}
