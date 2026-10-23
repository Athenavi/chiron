package db

import (
	"context"

	"github.com/jackc/pgx/v5/pgxpool"
)

// DefaultTenantID 是单租户部署的默认租户 ID。
// 与 internal/api/auth.go、internal/session/manager.go 中定义的常量保持一致，
// 注册与会话落库均硬编码引用该 ID。
//
// 类型为 int16 以匹配 tenants.id 的 smallint（见 0001 基线迁移）。
const DefaultTenantID int16 = 1

// EnsureDefaultTenant 幂等确保默认租户存在。
//
// users/sessions 等表均有 tenants(id) 外键（users_tenant_id_fkey 等），
// 而 tenants 表本身没有任何种子数据；若默认租户缺失，注册会直接违反
// 外键约束（SQLSTATE 23503）。该函数不依赖迁移状态（schema_migrations），
// 对通过任何方式初始化的库都能生效。
func EnsureDefaultTenant(ctx context.Context, pool *pgxpool.Pool) error {
	if _, err := pool.Exec(ctx,
		`INSERT INTO tenants (id, name) VALUES ($1, 'default') ON CONFLICT (id) DO NOTHING`,
		DefaultTenantID); err != nil {
		return err
	}
	// tenants.id 是 smallserial：显式插入 id=1 不会推进其序列。不校正的话，
	// 后续 CreateTenant 取 nextval 仍从 1 开始，会撞上默认租户的主键。
	_, err := pool.Exec(ctx,
		`SELECT setval(pg_get_serial_sequence('tenants', 'id'), GREATEST((SELECT COALESCE(MAX(id), 1) FROM tenants), 1))`)
	return err
}
