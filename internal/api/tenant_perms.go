package api

import (
	"context"
	"errors"
	"log/slog"

	"github.com/jackc/pgx/v5"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// ── 权限判定的读路径（C1-4 / roadmap §12.4 第 4 步）──
//
// ## 为什么要切
//
// 在此之前，登录签发的 token 里 `perms` 一律来自 `auth.RolePermissions[role]`，
// 而那个 map 只有 `owner` / `admin` / `user` 三档、且是**硬编码在代码里**的。
// 团队能力落地后，"这个人在这个租户里是什么角色"的唯一真相是
// `tenant_members.role_id` → `ent_roles.permissions`，硬编码表反而成了第二套真相。
//
// ## 灰度与可回退（这是本条最关键的性质）
//
// 读 `tenant_members` **优先**，但**查不到就回退**旧表：
//
//	tenant_members 有行 → 用 ent_roles.permissions
//	查不到 / 查询失败   → 用 auth.RolePermissions[users.role]
//
// 于是任何一步出问题都能退回读 `users.tenant_id` 那一套 —— 这是 §12.4 明写的
// 可回退要求。注意查询失败时**不是**拒绝登录：拒绝会让一次 DB 抖动把所有用户
// 锁在门外，而回退到旧权限是既有的、可用的行为。
//
// ## 为什么不复用 enterprise.LoadEffectivePerms
//
// 那个函数聚合 `ent_user_roles` / `ent_group_members`（旧 RBAC），与
// `tenant_members` 是两套表。此处刻意只读 tenant_members，是为了让"团队角色"
// 这一条链路单独可验证；两套的合并留到旧 RBAC 真正退役时再谈。

const userTenantPermsQuery = `SELECT COALESCE(r.permissions, '{}')::text[]
  FROM tenant_members tm
  JOIN ent_roles r ON r.id = tm.role_id
 WHERE tm.user_id = $1`

// permQuerier 收窄到一条 QueryRow，便于单测注入假实现（仓库无 pgxmock）。
type permQuerier interface {
	QueryRow(ctx context.Context, sql string, args ...interface{}) pgx.Row
}

// resolveUserPerms 解析用户的有效权限：优先 tenant_members，查不到回退旧表。
//
// 返回的切片**永不为 nil**：`auth.HasPermission` 用 `Perms != nil` 区分
// "显式授予（含空集）" 与 "未设置（回退 RolePermissions）"，返回 nil 会让
// 调用方的回退逻辑意外生效，把权限放大。
func resolveUserPerms(ctx context.Context, q permQuerier, userID, fallbackRole string) []string {
	if userID != "" {
		var perms []string
		err := q.QueryRow(ctx, userTenantPermsQuery, userID).Scan(&perms)
		if err == nil {
			if perms == nil {
				perms = []string{}
			}
			return perms
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			// 查询失败：回退旧表而不是拒绝登录（见文件头注释）。
			slog.Warn("resolve tenant perms failed, falling back to role defaults",
				"user_id", userID, "error", err)
		}
	}
	return fallbackPerms(fallbackRole)
}

// fallbackPerms 是旧口径：`auth.RolePermissions[role]`，但**保证非 nil**。
// map 未命中原先返回 nil（触发 HasPermission 的二次回退），这里显式归一成
// 空切片，让"未知角色 = 无权限"成为确定结论而不是隐式回退。
func fallbackPerms(role string) []string {
	perms, ok := auth.RolePermissions[role]
	if !ok || perms == nil {
		return []string{}
	}
	// 复制一份：避免调用方持有 map 内部切片后被意外修改。
	out := make([]string, len(perms))
	copy(out, perms)
	return out
}

// resolveUserPermsDefault 是 resolveUserPerms 绑定到全局连接池的便捷入口。
func resolveUserPermsDefault(ctx context.Context, userID, fallbackRole string) []string {
	return resolveUserPerms(ctx, db.GlobalDBManager, userID, fallbackRole)
}
