package api

// ── 可用模型动态发现（不写死模型/种子）──
//
// 对话页 /v1/models 不应依赖静态 llm_models 种子。本模块按“已配置 provider”
// 动态拉取各 provider 的 OpenAI 兼容 /models 端点（DeepSeek / OpenAI / 自定义网关
// 如 claudeagent.com.cn），结果写回 llm_models 作为 DB 缓存供查询与旧端点兼容。
//
// provider → API base URL（base 会拼 "/models"）来源（优先级降序）：
//   1. DB「系统设置」python 分类的 {provider}_base_url / llm_base_url（敏感项加密列）;
//   2. 服务提供商目录（internal/api/llm_providers.go）的默认端点，支持任意已收录 provider
//      （Anthropic 原生协议默认不做发现，需显式配置端点）。
// 与引擎侧 provider 使用同一套 base_url 语义（服务提供商面板 / 后台 python 分类或 .env 的 *_BASE_URL）。

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

const (
	modelSyncFreshInterval = 5 * time.Minute // 内存节流：相同 provider 该窗口内不重复外呼
	modelFetchTimeout      = 6 * time.Second
)

var (
	modelSyncMu   sync.Mutex
	modelSyncLast = map[string]time.Time{}
)

// llmKeysetProvider 是 Redis llm:keys:{provider} 中 active 的记录。
type llmKeysetProvider struct {
	Name string
	Key  string
}

// keysetActiveProviders 返回 Redis keyset 中存在 active key 的 provider 列表。
// keyset 结构：HASH llm:keys:{provider}，field=digest12，value=JSON {"k":明文,"s":status}。
func keysetActiveProviders(ctx context.Context) []llmKeysetProvider {
	if db.Redis == nil {
		return nil
	}
	out := []llmKeysetProvider{}
	prefix := db.RedisKey("llm:keys:")
	// 跨节点扫描：Cluster 下 SCAN 只覆盖被路由到的单个节点，会漏读其它 master 上的
	// provider key（llm:keys:{provider} 按 slot 分散在各 master），导致可用模型列表不全。
	keys, err := db.Redis.ScanAll(ctx, prefix+"*", 100)
	if err != nil {
		slog.Debug("llm keyset scan failed", "error", err)
		return out
	}
	for _, k := range keys {
		provider := strings.TrimPrefix(k, prefix)
		if provider == "" || provider == "ver" {
			continue
		}
		res := db.Redis.Do(ctx, "HGETALL", k)
		if res.Err() != nil {
			continue
		}
		rawPairs, ok := res.Val().([]interface{})
		if !ok {
			continue
		}
		for i := 0; i+1 < len(rawPairs); i += 2 {
			payload, ok := rawPairs[i+1].(string)
			if !ok {
				continue
			}
			var item struct {
				K string `json:"k"`
				S string `json:"s"`
			}
			if json.Unmarshal([]byte(payload), &item) != nil || item.K == "" {
				continue
			}
			if item.S != "" && item.S != "active" {
				continue
			}
			out = append(out, llmKeysetProvider{Name: provider, Key: item.K})
			break // 一个 provider 取一个 key 即可发现模型列表
		}
	}
	return out
}

// providerModelsBaseURL 解析 provider 的 OpenAI 兼容 base（不含 "/models"）。
// 优先级（降序）：
//  1. DB「系统设置」python 分类的 {provider}_base_url —— 任意 provider（不限于内建三项），
//     由管理端「服务提供商」面板 / 系统设置写入；openai 兼容网关额外回退 llm_base_url；
//  2. 服务提供商目录（llm_providers.go）的默认端点，且该 provider 支持模型发现
//     （Anthropic 原生协议不做发现，除非上面的 DB 覆盖显式给出端点）。
func providerModelsBaseURL(ctx context.Context, provider string) string {
	// 1) DB 覆盖
	if db.Pool != nil {
		keys := []string{llmProviderBaseURLKey(provider)}
		if provider == "openai" {
			keys = append(keys, "llm_base_url")
		}
		for _, key := range keys {
			var valText string
			err := db.Pool.QueryRow(ctx,
				`SELECT value::text FROM system_settings WHERE category = 'python' AND key = $1`, key).
				Scan(&valText)
			if err == nil && valText != "" && valText != "null" {
				if v := strings.TrimRight(llmSettingsString(valText), "/"); v != "" {
					return v
				}
			}
		}
	}
	// 2) 目录默认端点
	if !llmProviderSupportsModelDiscovery(provider) {
		// anthropic 等：无默认 base 时不做发现（避免写死第三方地址）
		return ""
	}
	return llmProviderDefaultBaseURL(provider)
}

// fetchProviderModels 调用 {base}/models（OpenAI 兼容）返回模型 id 列表。
func fetchProviderModels(ctx context.Context, base, apiKey string) ([]string, error) {
	base = strings.TrimRight(strings.TrimSpace(base), "/")
	url := base + "/models"
	reqCtx, cancel := context.WithTimeout(ctx, modelFetchTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+apiKey)
	req.Header.Set("Accept", "application/json")

	client := &http.Client{}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return nil, fmt.Errorf("models api %s: HTTP %d: %s", url, resp.StatusCode, strings.TrimSpace(string(body)))
	}
	var payload struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 4<<20)).Decode(&payload); err != nil {
		return nil, fmt.Errorf("models api decode: %w", err)
	}
	ids := make([]string, 0, len(payload.Data))
	for _, m := range payload.Data {
		if strings.TrimSpace(m.ID) != "" {
			ids = append(ids, strings.TrimSpace(m.ID))
		}
	}
	return ids, nil
}

// replaceProviderModels 用外部发现的模型重建该 provider 在 llm_models 的缓存行。
func replaceProviderModels(ctx context.Context, provider string, ids []string) error {
	tx, err := db.Pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if _, err := tx.Exec(ctx, `DELETE FROM llm_models WHERE provider = $1`, provider); err != nil {
		return err
	}
	for _, id := range ids {
		if _, err := tx.Exec(ctx,
			`INSERT INTO llm_models (id, provider, name, display_name, enabled, context_window, created_at, updated_at)
			 VALUES (gen_random_uuid()::text, $1, $2, $3, true, 0, NOW(), NOW())`,
			provider, id, id); err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}

// ListModelsForUser 是 GET /v1/models 的实现：
// 先对“Redis keyset 有 active key 且本地节流已过期”的 provider 动态拉取模型并回写
// llm_models（DB 缓存），再返回 enabled 模型列表。任何 provider 拉取失败仅告警，不回滚旧缓存。
func ListModelsForUser(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	pool := db.ReadPool()
	if pool == nil {
		ServiceUnavailable(w, "database not available")
		return
	}

	for _, p := range keysetActiveProviders(ctx) {
		if modelsRecentlySynced(p.Name) {
			continue
		}
		base := providerModelsBaseURL(ctx, p.Name)
		if base == "" {
			continue
		}
		ids, err := fetchProviderModels(ctx, base, p.Key)
		if err != nil {
			slog.Warn("dynamic models fetch failed", "provider", p.Name, "error", err)
			continue
		}
		if len(ids) == 0 {
			slog.Warn("dynamic models returned empty list", "provider", p.Name)
			continue
		}
		if err := replaceProviderModels(ctx, p.Name, ids); err != nil {
			slog.Warn("dynamic models sync failed", "provider", p.Name, "error", err)
			continue
		}
		markModelsSynced(p.Name)
		slog.Info("dynamic models synced", "provider", p.Name, "count", len(ids))
	}

	rows, err := pool.Query(ctx,
		`SELECT provider, name, display_name, context_window FROM llm_models
		 WHERE enabled = true ORDER BY provider, name`)
	if err != nil {
		slog.Error("list models", "error", err)
		InternalError(w, "failed to list models")
		return
	}
	defer rows.Close()
	type model struct {
		Provider      string `json:"provider"`
		Name          string `json:"name"`
		DisplayName   string `json:"display_name"`
		ContextWindow int    `json:"context_window"`
	}
	out := []model{}
	for rows.Next() {
		var m model
		if rows.Scan(&m.Provider, &m.Name, &m.DisplayName, &m.ContextWindow) == nil {
			out = append(out, m)
		}
	}
	OK(w, map[string]interface{}{"models": out})
}

func modelsRecentlySynced(provider string) bool {
	modelSyncMu.Lock()
	defer modelSyncMu.Unlock()
	t, ok := modelSyncLast[provider]
	return ok && time.Since(t) < modelSyncFreshInterval
}

func markModelsSynced(provider string) {
	modelSyncMu.Lock()
	modelSyncLast[provider] = time.Now()
	modelSyncMu.Unlock()
}
