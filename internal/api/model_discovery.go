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
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

const (
	modelSyncFreshInterval = 5 * time.Minute // 内存节流：相同 provider 该窗口内不重复外呼
	modelFetchTimeout      = 6 * time.Second

	// llmHTTPUserAgent 是出站 LLM 请求的具名 UA。部分网关前置 Cloudflare 反滥用规则，
	// 会直接拒绝 http 库的默认 UA（实测访问 opencode.ai 返回 CF Error 1010 → 403）。
	// 与引擎侧 python-engine/app/config.py 的 llm_http_user_agent 保持一致。
	llmHTTPUserAgent = "chiron/1.0"
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
		// go-redis v9 的 HGETALL 返回 map 而非 v8 的扁平数组：统一走 db.HashAll，
		// 否则这里会**静默**解析不出任何 provider —— 那正是"模型发现从未运行、
		// /v1/models 永远返回空"的根因（连一条 warn 都不会打）。
		pairs, err := db.HashAll(ctx, db.Redis, k)
		if err != nil {
			slog.Debug("llm keyset hash read failed", "provider", provider, "error", err)
			continue
		}
		for _, payload := range pairs {
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

	// 具名 UA：见 llmHTTPUserAgent 的说明（Cloudflare 反滥用会拒默认 UA）。
	req.Header.Set("User-Agent", llmHTTPUserAgent)

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

// ── 模型同步：**保住用户的开关与行的身份** ──
//
// 2026-10-09 修（用户报「/models 刷新后全部变回启用」）：
// 此前是「DELETE 该 provider 全部行 → 重新 INSERT」，而 INSERT 把 `enabled` **写死成 true**、
// `id` 每次都是新的 `gen_random_uuid()` ⇒ 管理员刚关掉的模型，**下一次模型发现就被重新启用**。
// 而 `GET /v1/models` 就会触发发现（对话页一打开就调）⇒ 表现为「刷新后全部变回启用」。
//
// 这与 P1-f（见下）修的是**同一类**问题：那次只保住了 context_window，漏了 enabled 与行的身份。
//
// 现在的口径：
//   - **已存在的行原样留着** —— 连 `enabled` 都不在 SELECT/UPDATE 里出现，从代码上保证翻不动；
//   - 只新建「新发现的」、只删除「这次没再发现的」；
//   - 窗口只在"之前不知道（<=0）"时推断补一次，**不覆盖**已有值（P1-f 的原口径）。
//
// 抽成纯函数 `planModelSync` 是为了**能单测**：本仓不能假设测试环境有真 PostgreSQL
// （DB 相关用例靠 `*_live_test.go` 的 env gating），所以决策必须与执行分离。
type existingModel struct {
	ID     string
	Name   string
	Window int
}

type windowBackfill struct {
	ID     string
	Window int
}

type modelSyncPlan struct {
	Keep        []existingModel  // 原样保留（enabled / id / display_name 都不动）
	BackfillWin []windowBackfill // 窗口未知 ⇒ 推断补一次
	Insert      []string         // 新发现 ⇒ 新建（enabled = true）
	Delete      []string         // 这次没再发现 ⇒ 删除
}

// planModelSync 给「库里的现有行」与「这次发现的模型名」，算出唯一一套动作。
// 同名重复发现只算一次（provider 的 /models 可能重复返回）。
func planModelSync(existing []existingModel, discovered []string) modelSyncPlan {
	byName := make(map[string]existingModel, len(existing))
	for _, m := range existing {
		byName[m.Name] = m
	}
	seen := make(map[string]bool, len(discovered))
	var plan modelSyncPlan
	for _, id := range discovered {
		if id == "" || seen[id] {
			continue
		}
		seen[id] = true
		prev, ok := byName[id]
		if !ok {
			plan.Insert = append(plan.Insert, id)
			continue
		}
		plan.Keep = append(plan.Keep, prev)
		if prev.Window <= 0 {
			if w := inferContextWindow(id); w > 0 {
				plan.BackfillWin = append(plan.BackfillWin, windowBackfill{ID: prev.ID, Window: w})
			}
		}
	}
	// 按库里的原顺序遍历（结果稳定，便于断言与排查）
	for _, m := range existing {
		if !seen[m.Name] {
			plan.Delete = append(plan.Delete, m.ID)
		}
	}
	return plan
}

// replaceProviderModels 用外部发现的模型同步该 provider 在 llm_models 的缓存行。
//
// P1-f：此前重建时把 context_window 写死为 0，导致前端上下文环的**分母恒为 0**
// （环永远不显示 → 用户完全无法感知上下文占用与自动压缩）。现在：
//  1. 先留住该 provider 已探测/已配置（>0）的窗口值，重建时沿用 —— 否则每次刷新模型列表
//     都会把之前的正确值清掉；
//  2. 新模型按名字约定推断一个**保守**窗口；推断不出时仍返回 0（宁可不显示环，
//     也不给一个可能严重高估的分母去误导用户）。
//
// 2026-10-09 追加：**已存在的行连 id 一起保留**（见上面 modelSyncPlan 的说明）——
// 用户设的 enabled 必须活过每一次发现。
func replaceProviderModels(ctx context.Context, provider string, ids []string) error {
	tx, err := db.Pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	// ⚠ 这里**刻意不 SELECT enabled**：不同步它，就不可能把它翻回去（比"读出来再写回去"更稳）
	var existing []existingModel
	if rows, err := tx.Query(ctx,
		`SELECT id::text, name, COALESCE(context_window, 0) FROM llm_models WHERE provider = $1`,
		provider); err == nil {
		for rows.Next() {
			var m existingModel
			if rows.Scan(&m.ID, &m.Name, &m.Window) == nil {
				existing = append(existing, m)
			}
		}
		rows.Close()
	}

	plan := planModelSync(existing, ids)

	for _, id := range plan.Delete {
		if _, err := tx.Exec(ctx, `DELETE FROM llm_models WHERE id = $1`, id); err != nil {
			return err
		}
	}
	for _, id := range plan.Insert {
		window := inferContextWindow(id)
		if _, err := tx.Exec(ctx,
			`INSERT INTO llm_models (id, provider, name, display_name, enabled, context_window, created_at, updated_at)
			 VALUES (gen_random_uuid()::text, $1, $2, $3, true, $4, NOW(), NOW())`,
			provider, id, id, window); err != nil {
			return err
		}
	}
	for _, b := range plan.BackfillWin {
		if _, err := tx.Exec(ctx,
			`UPDATE llm_models SET context_window = $1, updated_at = NOW() WHERE id = $2`,
			b.Window, b.ID); err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}

// reContextWindowSuffix 匹配模型名尾部的显式窗口声明（如 `-128k` / `-1m` / `_32k`）。
var reContextWindowSuffix = regexp.MustCompile(`[-_](\d+)(k|m)$`)

// inferContextWindow 从模型名推断上下文窗口（tokens）。名字里显式声明的优先，
// 否则按已知家族给保守估计；无法判断时返回 0（调用方据此不展示上下文环）。
func inferContextWindow(modelID string) int {
	id := strings.ToLower(strings.TrimSpace(modelID))
	if m := reContextWindowSuffix.FindStringSubmatch(id); m != nil {
		if n, err := strconv.Atoi(m[1]); err == nil && n > 0 {
			if m[2] == "m" {
				return n * 1000000
			}
			return n * 1000
		}
	}
	switch {
	case strings.Contains(id, "claude"):
		return 200000
	case strings.Contains(id, "gemini"):
		return 1000000
	case strings.Contains(id, "gpt-5"), strings.Contains(id, "gpt-4.1"),
		strings.Contains(id, "o3"), strings.Contains(id, "o4"):
		return 1000000
	case strings.Contains(id, "gpt-4"), strings.Contains(id, "o1"):
		return 128000
	case strings.Contains(id, "grok"):
		return 131072
	case strings.Contains(id, "qwen"), strings.Contains(id, "qwq"), strings.Contains(id, "llama"),
		strings.Contains(id, "mistral"), strings.Contains(id, "mixtral"):
		return 131072
	case strings.Contains(id, "deepseek"), strings.Contains(id, "kimi"),
		strings.Contains(id, "glm"), strings.Contains(id, "minimax"),
		strings.Contains(id, "doubao"), strings.Contains(id, "ernie"):
		return 128000
	}
	return 0
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
