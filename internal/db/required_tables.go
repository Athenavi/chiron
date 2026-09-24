package db

import (
	"context"
	"fmt"
)

// ── 启动期的「必需表」只读校验 ──────────────────────────────────────────────
//
// 分工：DDL 的唯一权威是 Alembic（migrations/versions/0001_authoritative_baseline.py），
// 应用不建表。而 schema_version.go 校验的只是 **revision** 是否一致 —— 若有人手工删了
// 某张表，revision 仍然匹配，缺失会推迟到运行时才以 `relation "x" does not exist` 暴露
// （且往往出现在某个冷门功能路径上）。这里补一道只读的存在性校验，把它提前到启动日志。
//
// 与 Python 侧的关系：引擎有自己的一份清单（app/db.py 的 REQUIRED_TABLES），覆盖向量 /
// RAG / 记忆等**引擎专属**表；本清单只列**网关自身读写**的表。两者有意不追求完全一致 ——
// 各自校验自己的依赖，避免任一侧的清单变化牵动另一侧。

// RequiredTables 是网关启动时必须存在的表。
var RequiredTables = []string{
	"users",
	"tenants",
	"sessions",
	"messages",
	"agents",
	"api_keys",
	"system_settings",
	"audit_logs",
	"credit_transactions",
	"payments",
}

// CheckMissingTables 只读返回缺失的表名（顺序同 RequiredTables）。
// 返回空切片表示全部存在；查询本身失败则返回错误，由调用方决定告警还是阻断。
func CheckMissingTables(ctx context.Context) ([]string, error) {
	if Pool == nil {
		return nil, fmt.Errorf("database pool not initialized")
	}

	// 用 unnest 一次往返查完，避免 N 次 to_regclass 往返。
	rows, err := Pool.Query(ctx, `
		SELECT t.name
		  FROM unnest($1::text[]) AS t(name)
		 WHERE to_regclass('public.' || t.name) IS NULL`, RequiredTables)
	if err != nil {
		return nil, fmt.Errorf("check required tables: %w", err)
	}
	defer rows.Close()

	missing := make([]string, 0, len(RequiredTables))
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			return nil, fmt.Errorf("scan missing table: %w", err)
		}
		missing = append(missing, name)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate missing tables: %w", err)
	}
	return missing, nil
}
