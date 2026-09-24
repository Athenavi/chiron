package main

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/db"
	"github.com/spf13/cobra"
)

var dbCmd = &cobra.Command{
	Use:   "db",
	Short: "Database management",
	Long:  `Manage Chiron database.`,
}

var dbStatusCmd = &cobra.Command{
	Use:   "status",
	Short: "Show database status",
	RunE:  runDBStatus,
}

var dbMigrateCmd = &cobra.Command{
	Use:  "migrate",
	RunE: runDBMigrate,
}

func init() {
	dbCmd.AddCommand(dbStatusCmd)
	dbCmd.AddCommand(dbMigrateCmd)
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

func runDBMigrate(cmd *cobra.Command, args []string) error {
	dsn := getDSN()
	fmt.Printf("Migrating database: %s\n", sanitizeDSN(dsn))

	// 如果指定了 --dry-run 参数，使用 --sql 输出 SQL 而不实际执行
	for _, a := range args {
		if a == "--dry-run" || a == "--sql" {
			os.Setenv("DATABASE_DSN", dsn)
			python := "python"
			if v := os.Getenv("PYTHON"); v != "" {
				python = v
			} else if v := os.Getenv("CHIRON_PYTHON"); v != "" {
				python = v
			}
			runCmd := exec.Command(python, "-m", "alembic", "--config", "alembic.ini", "upgrade", "head", "--sql")
			runCmd.Dir = "."
			runCmd.Stdout = os.Stdout
			runCmd.Stderr = os.Stderr
			return runCmd.Run()
		}
	}

	fmt.Println("Running: alembic upgrade head")
	if err := db.RunMigrations(dsn); err != nil {
		return fmt.Errorf("database migration failed: %w", err)
	}

	fmt.Println("Database migrations completed successfully")
	return nil
}

// hasInternalMigrationFiles 检测目录下是否存在内部迁移器格式（.up.sql/.down.sql）文件
func hasInternalMigrationFiles(dir string) bool {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return false
	}
	for _, e := range entries {
		name := e.Name()
		if strings.HasSuffix(name, ".up.sql") || strings.HasSuffix(name, ".down.sql") {
			return true
		}
	}
	return false
}
