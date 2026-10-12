package api

import (
	"context"
	"net/http"
	"regexp"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// MaxHarnessSuffixChars 是租户级 / 用户级 harness suffix 的长度上限。
//
// ⚠️ **必须与 Python 侧 `app/agent/harness_store.py` 的 `MAX_SUFFIX_CHARS` 同构** ——
// 该不变量由 `scripts/check_tool_policy_parity.py` 断言（漂移即失败）。理由：suffix 会被
// 拼进 system prompt，这个上限是"防单个租户吃掉共享上下文预算"的唯一开关，两侧不一致即形同虚设。
const MaxHarnessSuffixChars = 2000

// HarnessProfile 是一条租户级 / 用户级的行为塑形记录。
//
// `ModelPrefix` 为空串 = 该 scope 下**所有模型**；否则按前缀匹配（更长的前缀优先）。
// `Enabled=false` **不是"未配置"而是"显式禁用"**：它仍然占位并**阻止**回退到租户级 ——
// 否则用户无法关掉租户给他加上的 suffix（引擎侧的合并口径见 `harness_store._query`）。
type HarnessProfile struct {
	ModelPrefix string `json:"model_prefix"`
	Suffix      string `json:"system_prompt_suffix"`
	Enabled     bool   `json:"enabled"`
	// Source 只在 `effective` 列表里出现：`user` / `tenant`。
	Source string `json:"source,omitempty"`
}

// harnessPrefixRe 限制模型前缀的字符集：**禁止 LIKE 通配符**（`%` / `_`）。
//
// 引擎侧按 `lower(model) LIKE model_prefix || '%'` 匹配，一个 `%` 就能匹配所有模型 ——
// 语义立刻失控（而且那是"配置说谎"：界面写 claude-，实际命中的是全部）。
var harnessPrefixRe = regexp.MustCompile(`^[a-z0-9._:-]{0,128}$`)

// HarnessProfileHandler 读写 harness 配置。
//
// **身份只来自 token**：`auth.GetClaims` 的 `TenantID` / `UserID`。请求体里若出现
// `tenant_id` / `user_id` 一律**忽略** —— 这是防越权的主开关（见
// `TestHarnessProfileIgnoresBodyIdentity`）。
type HarnessProfileHandler struct {
	db entQuerier
	// notify 是"配置已变更"的广播钩子（引擎据此清进程内缓存，实现即时生效）。
	// 为 nil 时退化为"最多滞后一个 TTL"—— 不影响正确性，只影响生效延迟。
	notify func(ctx context.Context, tenantID string)
}

// NewHarnessProfileHandler 构造 handler。`notify` 可为 nil。
func NewHarnessProfileHandler(db entQuerier, notify func(ctx context.Context, tenantID string)) *HarnessProfileHandler {
	return &HarnessProfileHandler{db: db, notify: notify}
}

// ── 存储层 ────────────────────────────────────────────────────────────────

func listHarnessProfiles(ctx context.Context, db entQuerier, tenantID, userID string) ([]HarnessProfile, error) {
	rows, err := db.Query(ctx,
		`SELECT model_prefix, system_prompt_suffix, enabled FROM harness_profiles
		 WHERE tenant_id = $1 AND user_id = $2
		 ORDER BY model_prefix`, tenantID, userID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []HarnessProfile{}
	for rows.Next() {
		var p HarnessProfile
		if err := rows.Scan(&p.ModelPrefix, &p.Suffix, &p.Enabled); err != nil {
			return nil, err
		}
		out = append(out, p)
	}
	return out, rows.Err()
}

func upsertHarnessProfile(ctx context.Context, db entQuerier, tenantID, userID string, p HarnessProfile) error {
	_, err := db.Exec(ctx,
		`INSERT INTO harness_profiles (tenant_id, user_id, model_prefix, system_prompt_suffix, enabled, updated_at)
		 VALUES ($1, $2, $3, $4, $5, NOW())
		 ON CONFLICT (tenant_id, user_id, model_prefix) DO UPDATE
		   SET system_prompt_suffix = EXCLUDED.system_prompt_suffix,
		       enabled              = EXCLUDED.enabled,
		       updated_at           = NOW()`,
		tenantID, userID, p.ModelPrefix, p.Suffix, p.Enabled)
	return err
}

func deleteHarnessProfile(ctx context.Context, db entQuerier, tenantID, userID, modelPrefix string) error {
	_, err := db.Exec(ctx,
		`DELETE FROM harness_profiles WHERE tenant_id = $1 AND user_id = $2 AND model_prefix = $3`,
		tenantID, userID, modelPrefix)
	return err
}

// ── 合并（effective）────────────────────────────────────────────────────

// effectiveProfiles 按 `model_prefix` 合并两个 scope：**用户级覆盖租户级**。
//
// 覆盖是"整条记录"级的 —— 用户把自己的 `claude-` 关掉（enabled=false）就真的关掉了，
// 不会因为租户级那一条仍 enabled=true 而复活。
func effectiveProfiles(tenantDefaults, userOverrides []HarnessProfile) []HarnessProfile {
	merged := map[string]HarnessProfile{}
	order := []string{}
	put := func(p HarnessProfile, source string) {
		if _, seen := merged[p.ModelPrefix]; !seen {
			order = append(order, p.ModelPrefix)
		}
		p.Source = source
		merged[p.ModelPrefix] = p
	}
	for _, p := range tenantDefaults {
		put(p, "tenant")
	}
	for _, p := range userOverrides {
		put(p, "user") // 后写覆盖先写
	}
	out := make([]HarnessProfile, 0, len(order))
	for _, prefix := range order {
		out = append(out, merged[prefix])
	}
	return out
}

// ── 校验 ─────────────────────────────────────────────────────────────────

// validateHarnessProfile 返回给用户看的错误文案；空串 = 通过。
//
// 超长是**拒绝**而不是截断：写入时就该失败，而不是让用户在"我配了 3000 字"与
// "实际生效 2000 字"之间对不上账。
func validateHarnessProfile(p HarnessProfile) string {
	if !harnessPrefixRe.MatchString(p.ModelPrefix) {
		return "model_prefix must be lowercase letters/digits/._:- (max 128) and must not contain LIKE wildcards"
	}
	if len([]rune(p.Suffix)) > MaxHarnessSuffixChars {
		return "system_prompt_suffix is too long (max 2000 characters)"
	}
	return ""
}

// ── HTTP 层 ──────────────────────────────────────────────────────────────

// harnessScope 从 claims 取 scope；`tenantScoped=true` 时 user_id 固定为 ""（租户级）。
func harnessScope(r *http.Request, tenantScoped bool) (tenantID, userID string, ok bool) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID == "" {
		return "", "", false
	}
	if tenantScoped {
		return claims.TenantID, "", true
	}
	if claims.UserID == "" {
		return "", "", false
	}
	return claims.TenantID, claims.UserID, true
}

func (h *HarnessProfileHandler) handleGet(w http.ResponseWriter, r *http.Request, tenantScoped bool) {
	tenantID, userID, ok := harnessScope(r, tenantScoped)
	if !ok {
		Unauthorized(w, "authentication required")
		return
	}
	tenantDefaults, err := listHarnessProfiles(r.Context(), h.db, tenantID, "")
	if err != nil {
		InternalError(w, "failed to load harness profiles")
		return
	}
	userOverrides := []HarnessProfile{}
	if !tenantScoped {
		userOverrides, err = listHarnessProfiles(r.Context(), h.db, tenantID, userID)
		if err != nil {
			InternalError(w, "failed to load harness profiles")
			return
		}
	}
	OK(w, map[string]any{
		"tenant_defaults": tenantDefaults,
		"user_overrides":  userOverrides,
		"effective":       effectiveProfiles(tenantDefaults, userOverrides),
		"limits":          map[string]any{"max_suffix_chars": MaxHarnessSuffixChars},
	})
}

func (h *HarnessProfileHandler) handlePut(w http.ResponseWriter, r *http.Request, tenantScoped bool) {
	tenantID, userID, ok := harnessScope(r, tenantScoped)
	if !ok {
		Unauthorized(w, "authentication required")
		return
	}
	var body HarnessProfile
	if err := DecodeJSON(w, r, &body); err != nil {
		return // DecodeJSON 已写响应
	}
	body.ModelPrefix = strings.ToLower(strings.TrimSpace(body.ModelPrefix))
	if msg := validateHarnessProfile(body); msg != "" {
		BadRequest(w, msg)
		return
	}
	if err := upsertHarnessProfile(r.Context(), h.db, tenantID, userID, body); err != nil {
		InternalError(w, "failed to save harness profile")
		return
	}
	if h.notify != nil {
		h.notify(r.Context(), tenantID) // 失败不影响写入结果，只影响生效延迟
	}
	OK(w, body)
}

func (h *HarnessProfileHandler) handleDelete(w http.ResponseWriter, r *http.Request, tenantScoped bool) {
	tenantID, userID, ok := harnessScope(r, tenantScoped)
	if !ok {
		Unauthorized(w, "authentication required")
		return
	}
	prefix := strings.ToLower(strings.TrimSpace(r.PathValue("prefix")))
	if !harnessPrefixRe.MatchString(prefix) {
		BadRequest(w, "invalid model_prefix")
		return
	}
	if err := deleteHarnessProfile(r.Context(), h.db, tenantID, userID, prefix); err != nil {
		InternalError(w, "failed to delete harness profile")
		return
	}
	if h.notify != nil {
		h.notify(r.Context(), tenantID)
	}
	OK(w, map[string]any{"model_prefix": prefix, "deleted": true})
}

// 用户级（写自己；鉴权只需 authMW）
func (h *HarnessProfileHandler) GetMine(w http.ResponseWriter, r *http.Request) {
	h.handleGet(w, r, false)
}
func (h *HarnessProfileHandler) PutMine(w http.ResponseWriter, r *http.Request) {
	h.handlePut(w, r, false)
}
func (h *HarnessProfileHandler) DeleteMine(w http.ResponseWriter, r *http.Request) {
	h.handleDelete(w, r, false)
}

// 租户级（user_id=”；路由侧另有 PermPolicyManage 权限闸）
func (h *HarnessProfileHandler) GetTenant(w http.ResponseWriter, r *http.Request) {
	h.handleGet(w, r, true)
}
func (h *HarnessProfileHandler) PutTenant(w http.ResponseWriter, r *http.Request) {
	h.handlePut(w, r, true)
}
func (h *HarnessProfileHandler) DeleteTenant(w http.ResponseWriter, r *http.Request) {
	h.handleDelete(w, r, true)
}

// ── 变更广播（即时生效）──────────────────────────────────────────────────

// HarnessProfileChannel 是"某租户的 harness 配置已变更"的广播频道。
//
// ⚠️ **必须与 Python 侧 `app/agent/harness_store.py` 的 `CHANGE_CHANNEL` 逐字相同** ——
// 两侧各写一份（语言不同，无法共享常量）。
const HarnessProfileChannel = "chiron:harness_profile_changed"

// notifyHarnessProfileChanged 广播变更，让引擎清掉进程内缓存（否则最多滞后一个 TTL 生效）。
//
// **旁路、绝不阻断写入**：Redis 不可用 ⇒ 什么都不做（引擎侧 TTL 兜底）。用
// `WithoutCancel` 是因为"客户端断开"不该取消这条通知 —— 与仓库既有的旁路发货纪律一致。
func notifyHarnessProfileChanged(ctx context.Context, tenantID string) {
	if db.Redis == nil {
		return
	}
	_, _ = db.Redis.Publish(context.WithoutCancel(ctx), HarnessProfileChannel, tenantID).Result()
}
