package db

import (
	"testing"
)

// 本文件只保留 alembic.ini / .env 的路径解析用例。
//
// 原先还有 TestVersionLockPath / TestListMigrationFiles / TestFindNewFile* /
// TestIsEmptyMigration* —— 它们测的是 autogenerate 时代的遗留
// （version.lock、迁移文件比对、空迁移判定），这些函数**没有生产调用点**，
// 已随 migrate.go 的清理一并移除，故对应用例也已删除。

func TestAlembicConfigPath_Default(t *testing.T) {
	path := alembicConfigPath()
	if path != "alembic.ini" {
		t.Errorf("expected 'alembic.ini', got %q", path)
	}
}

func TestAlembicConfigPath_Override(t *testing.T) {
	t.Setenv("ALEMBIC_CONFIG", "/custom/path/alembic.ini")
	path := alembicConfigPath()
	if path != "/custom/path/alembic.ini" {
		t.Errorf("expected '/custom/path/alembic.ini', got %q", path)
	}
}

func TestDotEnvPath_Default(t *testing.T) {
	path := dotEnvPath()
	if path != ".env" {
		t.Errorf("expected '.env', got %q", path)
	}
}

func TestDotEnvPath_Override(t *testing.T) {
	t.Setenv("DOT_ENV_PATH", "/custom/path/.env")
	path := dotEnvPath()
	if path != "/custom/path/.env" {
		t.Errorf("expected '/custom/path/.env', got %q", path)
	}
}
