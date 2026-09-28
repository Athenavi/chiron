package main

import (
	"context"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/db"
	"github.com/spf13/cobra"
)

var dbCmd = &cobra.Command{
	Use:   "db",
	Short: "Database diagnostics (read-only)",
	Long: `Read-only database diagnostics.

Chiron never runs migrations from this CLI: Alembic is the single migration
entry point. Run it from the repository root (see requirements-migrate.txt):

    alembic upgrade head     # fresh database
    alembic stamp head       # existing database
`,
}

var dbStatusCmd = &cobra.Command{
	Use:   "status",
	Short: "Show database status and the applied Alembic revision",
	Long: `Connect with POSTGRES_DSN and report reachability plus the applied migration
revision, read from the alembic_version table (the single source of truth for
schema state). This command never writes schema.`,
	RunE: runDBStatus,
}

func init() {
	dbCmd.AddCommand(dbStatusCmd)
}

// getDSN 读取 POSTGRES_DSN，优先从环境变量获取
func getDSN() string {
	if dsn := os.Getenv("POSTGRES_DSN"); dsn != "" {
		return dsn
	}
	// 从 config 加载（config 会读取 .env 和环境变量）
	cfg := config.LoadAllowUnconfigured()
	if cfg != nil && cfg.PostgresDSN != "" {
		return cfg.PostgresDSN
	}
	return ""
}

// sanitizeDSN 隐藏连接串中的密码，避免打印泄漏
func sanitizeDSN(dsn string) string {
	const marker = "://"
	i := 0
	if idx := strings.Index(dsn, marker); idx >= 0 {
		i = idx + len(marker)
	}
	rest := dsn[i:]
	// 密码可能含 @，host 前的最后一个 @ 才是 userinfo 分隔
	at := strings.LastIndex(rest, "@")
	if at < 0 {
		return dsn
	}
	userinfo := rest[:at]
	if colon := strings.Index(userinfo, ":"); colon >= 0 {
		userinfo = userinfo[:colon] + ":*****"
	}
	return dsn[:i] + userinfo + rest[at:]
}

func runDBStatus(cmd *cobra.Command, args []string) error {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	dsn := getDSN()
	if err := db.ConnectPostgres(ctx, dsn, 2, 1); err != nil {
		return fmt.Errorf("连接数据库失败: %w", err)
	}
	defer db.ClosePostgres()

	if err := db.Pool.Ping(ctx); err != nil {
		return fmt.Errorf("数据库不可达: %w", err)
	}

	fmt.Println("Database Status")
	fmt.Println("===============")
	fmt.Printf("DSN:       %s\n", sanitizeDSN(dsn))
	fmt.Printf("Connected: yes\n")

	// 迁移版本以 alembic_version 为唯一事实源（Go 侧旧的 schema_migrations 已废弃）
	var revision string
	if err := db.Pool.QueryRow(ctx, `SELECT version_num FROM alembic_version`).Scan(&revision); err != nil {
		fmt.Println("Migrations: (alembic_version 不存在或为空 —— 数据库尚未迁移)")
		fmt.Println("            执行: alembic upgrade head（全新库）/ alembic stamp head（已有库）")
		return nil
	}
	fmt.Printf("\nApplied migration: %s\n", revision)
	return nil
}
