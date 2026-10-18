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

	enc := llmEnc(cfg)

	// 从库里**解得开**的每一条，构成这份 keyset 的新内容（Redis 只是 DB 权威层的运行时镜像）。
	// 解不开就跳过 —— 那是"这条不该进缓存"，不是"整份不刷新"（否则换 APP_SECRET 后旧明文会一直留着）。
	fields := make([]interface{}, 0, 2*len(rows))
	for _, r := range rows {
		plain, err := enc.DecryptString(r.encrypted)
		if err != nil {
			slog.Warn("llm keyset decrypt failed, dropping key from cache", "provider", provider, "error", err)
			continue
		}
		payload, _ := json.Marshal(map[string]string{"k": plain, "s": r.status})
		fields = append(fields, r.keyHash[:12], string(payload))
	}

	// **整体替换**而非 HMSET 增量写。HMSET 只增不删：一条 key 从库里删掉、只要该 provider 还剩别的 key，
	// 它那 12 位指纹字段就永久留在 hash 里 —— 而引擎 KeyRing 是 `HGETALL` 整份镜像当"当前可用 key"轮询的
	// （app/gateway/key_ring.py `_refresh`），于是**一把已撤销的 key 会被继续拿去打上游**。
	// 全部解密失败 / 该 provider 已无 key ⇒ fields 为空 ⇒ 等价于清空 ⇒ 陈旧明文不再被服务（正确）。
	if _, err := llmReplaceKeyset(ctx, provider, fields); err != nil {
		slog.Warn("llm keyset replace failed", "provider", provider, "error", err)
	}
	_, _ = db.Redis.Incr(ctx, llmKeysVerKey).Result()
	_ = db.Redis.Publish(ctx, llmKeysChangedChannel, provider).Err()
	return nil
}

// llmReplaceKeyset 用**一次原子 Lua 调用**把 `llm:keys:{provider}` 换成给定的 field/value 集合
// （先 DEL 再逐对 HSET）。之所以必须"替换"而非 HMSET 增量，见 syncProviderKeyset 的注释：
// 引擎把整份 hash 当"当前可用 key"轮询，残留字段等于把一把已删除的 key 一直服务下去。
// 返回写入后的 HLEN（便于观测与测试）；fields 为空 ⇒ 结果为空（哈希被删）。
// 单 key 脚本在 Cluster 下按该 key 路由到对应节点，安全；DEL+写入原子，读者不会看到中间态。
func llmReplaceKeyset(ctx context.Context, provider string, fields []interface{}) (int64, error) {
	if db.Redis == nil {
		return 0, nil
	}
	const script = `
redis.call('DEL', KEYS[1])
for i = 1, #ARGV, 2 do
  redis.call('HSET', KEYS[1], ARGV[i], ARGV[i + 1])
end
return redis.call('HLEN', KEYS[1])`
	n, err := db.Redis.Eval(ctx, script, []string{llmKeysHashPrefix + provider}, fields...).Int64()
	if err != nil {
		return 0, err
	}
	return n, nil
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
