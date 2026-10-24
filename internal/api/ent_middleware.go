package api

import (
	"context"
	"errors"
	"log/slog"
	"net/http"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/enterprise"
)

// RequireEntPerm 企业版权限中间件，签名与 RequirePermission 保持一致。
//
// 决策流程（实现见 AllowedByEntOrLegacy）：
//  1. 从 auth.GetClaims 取 UserID，调用 enterprise.LoadEffectivePerms 聚合
//     用户的企业级有效权限（直接角色 ∪ 群组成员角色，带 Redis 缓存）。
//  2. 返回 nil（用户无 ent 角色配置）→ 回退旧权限体系 auth.HasPermission。
//  3. 返回非 nil（含空切片）→ 仅当 perm 在切片内放行，否则 403；
//     空切片表示"明确无权限"，禁止回退旧体系（防越权）。
//  4. LoadEffectivePerms 出错（ent 基础设施故障）→ fail-open 回退
//     auth.HasPermission + slog.Warn，保证故障不阻断管理面。
//
// 挂载现状：各企业模块的 RegisterRoutes 均以此为 guard，见 gateway_router.go 的
// ent 注册区（SSO / 短信 / 人机验证 / 身份 / 市场 / 策略 / 模型路由 / Webhook /
// 评估 / 混沌 / 审计 / 成本中心）。
func RequireEntPerm(perm string) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			claims := auth.GetClaims(r.Context())
			if claims == nil || claims.UserID == "" {
				logAndRespond(w, errors.New("missing auth claims"),
					http.StatusUnauthorized, ErrAuthRequired)
				return
			}
			if !AllowedByEntOrLegacy(r.Context(), claims, perm) {
				logAndRespond(w, errors.New("insufficient enterprise permissions"),
					http.StatusForbidden, "insufficient permissions")
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

// AllowedByEntOrLegacy 企业 RBAC 优先、legacy 回退的权限判定。
//
// 与 RequireEntPerm 共用同一实现，避免"路由中间件放行、handler 内二次判定又拒绝"
// 的不一致（成本中心等需要自行判定权限的 handler 直接调用本函数）：
//   - 用户无 ent 角色配置（LoadEffectivePerms 返回 nil）→ 回退 auth.HasPermission；
//   - 用户有 ent 配置（含空切片 = 明确无权限）→ 仅以聚合权限为准，禁止回退；
//   - ent 基础设施故障（如 PG 查询失败）→ fail-open 回退 auth.HasPermission + 告警。
func AllowedByEntOrLegacy(ctx context.Context, claims *auth.Claims, perm string) bool {
	if claims == nil || claims.UserID == "" {
		return false
	}
	perms, err := enterprise.LoadEffectivePerms(ctx, claims.UserID)
	if err != nil {
		// fail-close（P0 修复）：此前回退到 legacy 角色权限 —— 但用户可能已被 ent 角色
		// **明确剥夺**了某权限（perms 为空切片），PG/Redis 抖动期间就会被 legacy 角色权限
		// 放行，构成越权窗口。RBAC 是授权判定：读不到就应当**拒绝**，而不是放宽。
		slog.Warn("ent rbac: load effective perms failed, denying (fail-close)",
			"user_id", claims.UserID, "perm", perm, "error", err)
		return false
	}
	if perms == nil {
		return auth.HasPermission(claims, perm)
	}
	for _, p := range perms {
		if p == perm {
			return true
		}
	}
	return false
}

// entTenantScope 解析企业版管理端点的租户作用域（M1 跨租户越权修复）。
//
// 这些端点挂 RequireEntPerm("ent:manage")，而该权限**租户级管理员也可能持有**
// （见 CreateRole/CreateGroup 会按 claims.TenantID 锚定写入租户）—— 因此必须做租户
// 隔离，否则持有该权限的租户 A 管理员可读/改/删租户 B 的角色、群组、用户、策略、
// 配额与 SSO Provider。
//
// 语义（与 ent_costcenter 的 scopedTenantID 对齐）：
//   - owner：平台级视角 → ("", true)，调用方**不加**租户过滤；
//   - 非 owner：→ (自身租户数字串, false)，调用方按该值过滤；
//   - 非 owner 且无租户绑定：→ ("", false)，调用方须 fail-close（拒绝）。
func entTenantScope(claims *auth.Claims) (string, bool) {
	if claims == nil {
		return "", false
	}
	if claims.Role == "owner" {
		return "", true
	}
	if claims.TenantID != 0 {
		return tenantStr(claims.TenantID), false
	}
	return "", false
}
