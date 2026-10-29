package api

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// 用户级套餐权益与租户级策略的合并口径：**取更严者**（模型范围取交集）。
//
// 为什么不是"权益覆盖租户策略"：付费不该突破租户管理员的管控。租户只允许模型 A
// 时，用户买的套餐含 A/B 也只该能用 A —— 否则任何成员都能靠买最低档绕过白名单，
// 这是权限设计上的越权，不是产品上的灵活。
//
// 为什么不是"只取租户策略"：那套餐就毫无作用，用户付了钱却什么都没得到。
//
// 空数组的语义（两处一致）：租户策略不存在 / 权益不存在 / 权益 allowed_models
// 为空，都表示**该层不施加模型限制**，不参与取交集。

// effectiveAllowedModels 合并租户策略白名单与套餐权益白名单，返回生效清单。
//
// 返回 (models, restricted)：
//   - restricted=false 表示两侧都不限制，调用方直接放行；
//   - restricted=true 时 models 是最终允许的模型集合（可能为空，表示哪个都不许用）。
func effectiveAllowedModels(
	policyModels []string, hasPolicy bool,
	entModels []string, hasEntitlement bool,
) ([]string, bool) {
	// 权益存在但 allowed_models 为空 = 该套餐不限制模型，视同没有权益限制。
	entRestricts := hasEntitlement && len(entModels) > 0

	switch {
	case !hasPolicy && !entRestricts:
		return nil, false
	case hasPolicy && !entRestricts:
		return policyModels, true
	case !hasPolicy && entRestricts:
		return entModels, true
	default:
		// 两侧都限制 → 交集。用 policyModels 遍历（通常更小），
		// 保持租户策略的原始顺序，便于前端展示与排查。
		set := make(map[string]struct{}, len(entModels))
		for _, m := range entModels {
			set[m] = struct{}{}
		}
		out := make([]string, 0, len(policyModels))
		for _, m := range policyModels {
			if _, ok := set[m]; ok {
				out = append(out, m)
			}
		}
		return out, true
	}
}

// defaultResolveEntitledModels 查用户当前生效的套餐权益允许的模型。
//
// 返回 (models, hasEntitlement, err)：
//   - hasEntitlement=false：用户没有任何生效权益（未购买或已过期），不构成限制；
//   - models 为空：生效权益是"不限模型"套餐，同样不构成限制。
//
// 多份同时生效的权益取**并集**：用户买了两个包，两个包各自允许的模型都能用。
// 这不会放宽容权限 —— 与租户策略的交集仍在上层做。
func defaultResolveEntitledModels(ctx context.Context, userID string) ([]string, bool, error) {
	if userID == "" {
		return nil, false, nil
	}
	pool := db.ReadPool()
	if pool == nil {
		return nil, false, errors.New("entitlement: postgres pool unavailable")
	}

	// 生效与否只由时间决定（见 0009 迁移文件头）：无 status 列，过期即失配。
	rows, err := pool.Query(ctx,
		`SELECT COALESCE(allowed_models, ARRAY[]::text[])
		   FROM user_entitlements
		  WHERE user_id = $1
		    AND starts_at <= now()
		    AND expires_at > now()`, userID)
	if err != nil {
		return nil, false, fmt.Errorf("entitlement: query active: %w", err)
	}
	defer rows.Close()

	var (
		has  bool
		seen = map[string]struct{}{}
		out  []string
	)
	for rows.Next() {
		var models []string
		if err := rows.Scan(&models); err != nil {
			return nil, false, fmt.Errorf("entitlement: scan active: %w", err)
		}
		has = true
		for _, m := range models {
			if _, dup := seen[m]; dup {
				continue
			}
			seen[m] = struct{}{}
			out = append(out, m)
		}
	}
	if err := rows.Err(); err != nil {
		return nil, false, fmt.Errorf("entitlement: iterate active: %w", err)
	}
	return out, has, nil
}

// effectiveModelSet 返回请求路径可用的模型集合，以及"是否真的施加了限制"。
//
// 与 ResolveAllowedModels 的区别（**不要相互替代**）：
//   - ResolveAllowedModels 在"不限制"时要返回一份全量清单，实现是查
//     admin_model_configs.model_id。该表是后台模型路由配置，可以为空；
//   - 本函数把"不限制"如实表达为 restricted=false，让调用方**跳过过滤**。
//
// 为什么这个区别是致命的：真库里 admin_model_configs 可能 0 行而 llm_models 有
// 40 行。若拿"不限制时的全量清单"去过滤 /v1/models，结果是**一个模型都不返回**
// ——用户看到空下拉，却被当成"权限正常"。
//
// 失败一律 fail-open（restricted=false + 告警），与既有策略解析一致。
func (h *EntPolicyHandler) effectiveModelSet(
	ctx context.Context, tenantID int16, userID string,
) (map[string]struct{}, bool) {
	allowed, hasPolicy, err := h.resolveAllowed(ctx, tenantID, userID)
	if err != nil {
		slog.Warn("ent policy: resolve allow-list failed, fail-open",
			"user", userID, "tenant", tenantID, "error", err)
		return nil, false
	}
	entModels, hasEnt := h.resolveEntitledModels(ctx, userID)
	effective, restricted := effectiveAllowedModels(allowed, hasPolicy, entModels, hasEnt)
	if !restricted {
		return nil, false
	}
	set := make(map[string]struct{}, len(effective))
	for _, m := range effective {
		set[m] = struct{}{}
	}
	return set, true
}

// EffectiveModelSet 包级入口，供 GET /v1/models 等请求路径调用。
func EffectiveModelSet(ctx context.Context, tenantID int16, userID string) (map[string]struct{}, bool) {
	return defaultPolicyHandler.effectiveModelSet(ctx, tenantID, userID)
}

// 模型准入被拒的原因标识（放在响应 params.reason 里）。
//
// 为什么要分开：两种拒绝**给用户的建议完全不同**。
//   - ReasonEntitlement：租户允许这个模型，只是用户手上的套餐不覆盖 →
//     买/续套餐有用，文案应指向「套餐包」；
//   - ReasonPolicy：租户白名单里根本没有它 → 买套餐也没用，文案应指向
//     「联系租户管理员」。
//
// 合成一句「没有权限」会让前者白买套餐、后者反复找客服。
const (
	ReasonEntitlement = "entitlement"
	ReasonPolicy      = "policy"
)

// modelAccessReason 判定 modelID 对某用户是否可用；不可用时返回原因。
//
// 与 effectiveAllowedModels 保持同一口径（允许 ⟺ 两层都放行），但额外回答
// 「是哪一层拦的」，用于给用户可执行动作。
// 解析失败 fail-open（返回允许），与既有策略解析一致。
func (h *EntPolicyHandler) modelAccessReason(
	ctx context.Context, tenantID int16, userID, modelID string,
) (string, bool) {
	allowed, hasPolicy, err := h.resolveAllowed(ctx, tenantID, userID)
	if err != nil {
		slog.Warn("ent policy: resolve allow-list failed, fail-open",
			"user", userID, "tenant", tenantID, "model", modelID, "error", err)
		return "", false
	}
	entModels, hasEnt := h.resolveEntitledModels(ctx, userID)

	// 租户策略存在但白名单为空 = 一个都不允许（沿用 effectiveAllowedModels 的语义）。
	policyAllows := !hasPolicy || containsString(allowed, modelID)
	// 权益只在其确实限制了模型范围时才参与判定（空 allowed_models = 不限模型）。
	entRestricts := hasEnt && len(entModels) > 0
	entAllows := !entRestricts || containsString(entModels, modelID)

	switch {
	case policyAllows && entAllows:
		return "", false
	case !policyAllows:
		// 先判 policy：即使权益也不含，买套餐同样解决不了，别把用户引向付费。
		return ReasonPolicy, true
	default:
		return ReasonEntitlement, true
	}
}

func containsString(list []string, want string) bool {
	for _, s := range list {
		if s == want {
			return true
		}
	}
	return false
}

// resolveModelAccess 是包级可替换点（测试注入 fake，避免依赖真实 PG）。
var resolveModelAccess = func(ctx context.Context, tenantID int16, userID, modelID string) (string, bool) {
	return defaultPolicyHandler.modelAccessReason(ctx, tenantID, userID, modelID)
}

// GuardChatModelAccess 是对话提交链路的模型准入闸门（P2-2b）。
//
// 返回 true 表示放行；false 表示**已写出 403 响应**，调用方必须中止转发。
//
// 调用时机很关键：必须排在 InjectSessionRuntime 之后。后者会把会话运行时
// 解析出的最终 model 写回 body —— 若在它之前校验，只会看到前端随手传的字段，
// 用户切换模型后实际生效的可能完全是另一个（既有 bug 的根因就是 Go 侧不看
// body 的最终值）。
//
// 无 model 字段时放行：交给引擎按默认模型处理，与既有行为一致。
func GuardChatModelAccess(w http.ResponseWriter, r *http.Request, body map[string]interface{}) bool {
	modelID := chatBodyModel(body)
	if modelID == "" {
		return true
	}
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		return true // 鉴权由上游中间件负责
	}
	tenant := claims.TenantID
	if tenant == 0 {
		tenant = parseTenantID(claims.UserID)
	}
	reason, denied := resolveModelAccess(r.Context(), tenant, claims.UserID, modelID)
	if !denied {
		return true
	}
	slog.Info("chat model denied",
		"user", claims.UserID, "tenant", tenant, "model", modelID, "reason", reason)
	// 显式 retryable=false：模型范围不会因为"再点一次"而变化，给重试按钮
	// 只会把用户推进必然失败的循环（与 quota_exceeded 同一口径）。
	JSONWithRetryable(w, http.StatusForbidden, CodeModelNotAllowed,
		map[string]interface{}{"model": modelID, "reason": reason},
		ErrModelNotAllowed.Error(), false)
	return false
}

// chatBodyModel 取请求体里最终生效的模型名。
//
// 优先 `llm_config.model`（统一链路经 InjectSessionRuntime 补齐后的值），
// 回退顶层 `model`（兼容直接调用 /v1/chat/completions 形态的客户端）。
func chatBodyModel(body map[string]interface{}) string {
	if cfg, ok := body["llm_config"].(map[string]interface{}); ok {
		if v, ok := cfg["model"].(string); ok && v != "" {
			return v
		}
	}
	if v, ok := body["model"].(string); ok && v != "" {
		return v
	}
	return ""
}

// resolveEntitledModels 调注入的实现；未注入时视为"无权益限制"。
//
// 失败一律 fail-open（与既有策略解析一致）：权益基础设施故障不该阻断业务链路，
// 但要留下告警，否则"权益静默失效"会长期无人发现。
func (h *EntPolicyHandler) resolveEntitledModels(ctx context.Context, userID string) ([]string, bool) {
	if h.resolveEntitled == nil {
		return nil, false
	}
	models, has, err := h.resolveEntitled(ctx, userID)
	if err != nil {
		slog.Warn("ent entitlement: resolve failed, fail-open",
			"user", userID, "error", err)
		return nil, false
	}
	return models, has
}
