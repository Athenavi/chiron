package db

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// ── schema 版本校验（只读）──
//
// 外置 PostgreSQL（托管实例 / DBA 维护）场景下，应用**不再自行迁移**：迁移由发布流程或
// DBA 用 requirements-migrate.txt 的环境执行（`alembic upgrade head`）。但应用必须在启动
// 时确认「代码期望的 schema」与「数据库当前 schema」一致，否则会以静默漂移的方式失败
// （运行时报 relation/column does not exist）。
//
// 本文件只做两件只读的事：
//  1. ParseMigrationHead：从 migrations/versions/*.py 解析出期望的 head revision；
//  2. CheckSchemaVersion：与数据库 alembic_version.version_num 比对。
//
// 不写任何 schema、不修改任何文件（Go 侧旧的 RunMigrations 会 shell 出 python 并写 .env，
// 已随 CLI 迁移入口一并删除，见 docs/db-migration-entry.md）。

var (
	migrationRevisionRe = regexp.MustCompile(`(?m)^revision:\s*str\s*=\s*['"]([^'"]+)['"]`)
	migrationDownRe     = regexp.MustCompile(`(?m)^down_revision:[^=]*=\s*(.+)$`)
	migrationQuotedRe   = regexp.MustCompile(`['"]([^'"]+)['"]`)
)

// MigrationsDir 返回迁移文件目录（可用 MIGRATIONS_DIR 覆盖；默认 migrations/versions）。
func MigrationsDir() string {
	if v := os.Getenv("MIGRATIONS_DIR"); v != "" {
		return v
	}
	return filepath.Join("migrations", "versions")
}

// ParseMigrationHead 解析目录下所有迁移，返回期望的 head revision。
// head 的判定：出现在 `revision` 中、且未被任何迁移的 `down_revision` 引用。
// 零个/多个 head 都返回错误——迁移链分叉或目录为空属于部署问题，必须显式暴露。
func ParseMigrationHead(dir string) (string, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return "", fmt.Errorf("read migrations dir %s: %w", dir, err)
	}
	revisions := map[string]bool{}
	referenced := map[string]bool{}
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".py") {
			continue
		}
		data, err := os.ReadFile(filepath.Join(dir, e.Name()))
		if err != nil {
			continue
		}
		content := string(data)
		m := migrationRevisionRe.FindStringSubmatch(content)
		if m == nil {
			continue
		}
		revisions[m[1]] = true
		if d := migrationDownRe.FindStringSubmatch(content); d != nil {
			// down_revision 可能是 None / 'abc' / ('a','b')——只取引号内的 revision
			for _, ref := range migrationQuotedRe.FindAllStringSubmatch(d[1], -1) {
				referenced[ref[1]] = true
			}
		}
	}
	if len(revisions) == 0 {
		return "", fmt.Errorf("no migration files with `revision:` found in %s", dir)
	}
	heads := make([]string, 0, 1)
	for rev := range revisions {
		if !referenced[rev] {
			heads = append(heads, rev)
		}
	}
	switch len(heads) {
	case 1:
		return heads[0], nil
	case 0:
		return "", fmt.Errorf("migration chain in %s has no head (cycle?)", dir)
	default:
		sort.Strings(heads)
		return "", fmt.Errorf("multiple migration heads in %s: %v — merge them before deploying", dir, heads)
	}
}

// CheckSchemaVersion 比对代码期望的迁移 head 与数据库实际版本（只读）。
// 返回 expected（期望 head）、actual（数据库 alembic_version）、match（是否一致）。
// 迁移表不存在或为空视为"从未迁移"（actual 为空串，match=false），不算错误；
// 只有解析失败或查询失败才返回 err，由调用方决定告警还是阻断。
func CheckSchemaVersion(ctx context.Context) (expected, actual string, match bool, err error) {
	expected, err = ParseMigrationHead(MigrationsDir())
	if err != nil {
		return "", "", false, err
	}
	if Pool == nil {
		return expected, "", false, errors.New("database pool not initialized")
	}
	if err := Pool.QueryRow(ctx, `SELECT version_num FROM alembic_version`).Scan(&actual); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return expected, "", false, nil
		}
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "42P01" { // undefined_table：从未迁移
			return expected, "", false, nil
		}
		return expected, "", false, fmt.Errorf("read alembic_version: %w", err)
	}
	return expected, actual, expected == actual, nil
}
