package api

import (
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// usersInsertRe 抓出 `INSERT INTO users (列清单)` 的列清单部分（允许跨行）。
var usersInsertRe = regexp.MustCompile(`(?s)INSERT INTO users\s*\(([^)]*)\)`)

// moduleRoot 从当前包目录向上找到 go.mod（守卫要扫**整个模块**，不只是本包）。
func moduleRoot(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	for {
		if _, statErr := os.Stat(filepath.Join(dir, "go.mod")); statErr == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatal("向上找不到 go.mod —— 守卫无法确定模块根")
		}
		dir = parent
	}
}

// firstIdentifier 取列清单里的第一个标识符（可能是 "id, tenant_id…" 或跨行写法）。
func firstIdentifier(columns string) string {
	first := strings.TrimSpace(columns)
	if idx := strings.IndexAny(first, ",\n\t "); idx >= 0 {
		first = first[:idx]
	}
	return first
}

// TestAllUserInsertsProvideID 是一张**回归网**：所有建 user 的 INSERT 必须显式给 `id`。
//
// 为什么值得这么测：
//
//	users.id 是 varchar(36) NOT NULL 且**没有默认值**（见 baseline 迁移
//	0001_authoritative_baseline），所以"漏掉 id 列"是一个**静态可判**的 SQL 缺陷 ——
//	但它只在**运行时**才炸，且只在"该路径首次建号"时暴露：
//
//	  邮箱验证码登录 + 自动注册 → 500 "auto register failed"
//	  （真实错误：null value in column "id" of relation "users" violates not-null constraint）
//
//	实测 internal/api 下曾有**三处**都漏了这一列（mail / sms / sso），只有认证注册
//	（auth.go）写对了。扫描源码能在编译期之前就拦住同一个坑，代价是零（不碰数据库）。
//
// **作用域（2026-10-09 扩大）**：原先只扫**本包目录**，于是同一个缺陷形状换一个包就漏掉 ——
// 已实测证明：在 `internal/auth/local.go` 里塞一处 `INSERT INTO users (email)`，
// 旧版守卫**照样通过**（exit 0）。现在改为从 go.mod 所在目录**扫整个模块**
// （跳过 `vendor/` / `node_modules` / `.git` 等，以及 `_test.go`），因为建 user 的代码
// 本来就不止一个包（当时就有 `internal/auth/local.go` 一处）。
//
// 这条断言不去校验 `gen_random_uuid()`（那是"怎么给"的实现细节），只校验"给了" ——
// 后者才是这个 bug 的形状。
func TestAllUserInsertsProvideID(t *testing.T) {
	root := moduleRoot(t)

	checked := 0
	walkErr := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			switch d.Name() {
			case "vendor", "node_modules", ".git", "dist", "__pycache__":
				return fs.SkipDir
			}
			return nil
		}
		name := d.Name()
		if !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			return nil
		}
		raw, readErr := os.ReadFile(filepath.Clean(path))
		if readErr != nil {
			return readErr
		}
		rel, relErr := filepath.Rel(root, path)
		if relErr != nil {
			rel = path
		}
		rel = filepath.ToSlash(rel)

		for _, match := range usersInsertRe.FindAllStringSubmatch(string(raw), -1) {
			checked++
			if first := firstIdentifier(match[1]); !strings.EqualFold(first, "id") {
				t.Errorf(
					"%s: `INSERT INTO users (%s…)` 没有把 `id` 作为第一列。\n"+
						"users.id 是 NOT NULL 且无默认值 —— 漏掉它会在该登录路径**首次建号**时 500。\n"+
						"照 internal/api/auth.go 的写法：INSERT INTO users (id, …) VALUES (gen_random_uuid(), …)",
					rel, first,
				)
			}
		}
		return nil
	})
	if walkErr != nil {
		t.Fatalf("遍历模块失败: %v", walkErr)
	}

	if checked == 0 {
		t.Fatal("没有扫到任何 `INSERT INTO users` —— 正则、模块根或目录结构变了，这张回归网已失效")
	}
}
