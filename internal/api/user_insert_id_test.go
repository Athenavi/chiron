package api

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// usersInsertRe 抓出 `INSERT INTO users (列清单)` 的列清单部分（允许跨行）。
var usersInsertRe = regexp.MustCompile(`(?s)INSERT INTO users\s*\(([^)]*)\)`)

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
// 这条断言不去校验 `gen_random_uuid()`（那是"怎么给"的实现细节），只校验"给了" ——
// 后者才是这个 bug 的形状。
func TestAllUserInsertsProvideID(t *testing.T) {
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatalf("read package dir: %v", err)
	}

	checked := 0
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		raw, err := os.ReadFile(filepath.Clean(name))
		if err != nil {
			t.Fatalf("read %s: %v", name, err)
		}

		for _, match := range usersInsertRe.FindAllStringSubmatch(string(raw), -1) {
			checked++
			columns := match[1]
			// 列清单可能写成 "id, tenant_id, ..." 或跨行；取第一个标识符判断
			first := strings.TrimSpace(columns)
			if idx := strings.IndexAny(first, ",\n\t "); idx >= 0 {
				first = first[:idx]
			}
			if !strings.EqualFold(first, "id") {
				t.Errorf(
					"%s: `INSERT INTO users (%s…)` 没有把 `id` 作为第一列。\n"+
						"users.id 是 NOT NULL 且无默认值 —— 漏掉它会在该登录路径**首次建号**时 500。\n"+
						"照 internal/api/auth.go 的写法：INSERT INTO users (id, …) VALUES (gen_random_uuid(), …)",
					name, first,
				)
			}
		}
	}

	if checked == 0 {
		t.Fatal("没有扫到任何 `INSERT INTO users` —— 正则或包结构变了，这张回归网已失效")
	}
}
