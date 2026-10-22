package api

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// `validateReadOnlySQL` 的单测 —— 锁住 B4 的这次收紧。
//
// 为什么值得单测：这个校验是三个内部端点（/v1/internal/db/query|execute|batch-execute）
// **共用**的最后一道 SQL 闸门。它们都挂在对外同一个 mux 上、只靠 X-Internal-Token
// 一道门，token 一旦泄露，闸门就是唯一拦得住 `SELECT 1; DROP TABLE users` 的东西。
//
// 本次修复的由来：DBQuery 早已升级成这套校验，而 DBExecute / DBBatchExecute 还留着
// 一份更宽松的白名单（含 SET/BEGIN/COMMIT，且没有多语句与 CTE 检查）—— 同一个文件里
// 两个端点两套口径。下面既有"必须拒绝"的用例，也有"必须仍放行"的用例：**收紧不能
// 误伤只读查询**，否则引擎的正常读路径会被打断。
func TestValidateReadOnlySQL(t *testing.T) {
	rejected := []struct {
		name string
		sql  string
	}{
		{"空语句", "   "},
		// 数据修改语句：前缀白名单直接拦下
		{"INSERT", "INSERT INTO users (id) VALUES ('x')"},
		{"UPDATE", "UPDATE users SET credits = 0"},
		{"DELETE", "DELETE FROM users"},
		{"DDL", "DROP TABLE llm_models"},
		{"TRUNCATE", "TRUNCATE TABLE audit_logs"},
		// 多语句：单条合法前缀 + 分号 + 任意破坏性语句
		{"多语句", "SELECT 1; DROP TABLE users"},
		{"多语句（带尾分号后的第二句）", "SELECT 1;DELETE FROM users;"},
		// data-modifying CTE：单语句、无分号，能穿过前缀白名单
		{"WITH 内嵌 DELETE", "WITH x AS (DELETE FROM users RETURNING id) SELECT * FROM x"},
		{"WITH 内嵌 UPDATE", "WITH x AS (UPDATE users SET credits = 0 RETURNING id) SELECT 1"},
		// 本次收紧掉的语句：此前 DBExecute 的白名单会放行它们
		{"SET（此前放行）", "SET search_path = public"},
		{"BEGIN（此前放行）", "BEGIN"},
		{"COMMIT（此前放行）", "COMMIT"},
		{"ROLLBACK（此前放行）", "ROLLBACK"},
		{"SAVEPOINT（此前放行）", "SAVEPOINT sp1"},
		{"RELEASE（此前放行）", "RELEASE SAVEPOINT sp1"},
	}
	for _, c := range rejected {
		t.Run("拒绝/"+c.name, func(t *testing.T) {
			if reason := validateReadOnlySQL(c.sql); reason == "" {
				t.Errorf("%q 应被拒绝，实际放行", c.sql)
			}
		})
	}

	allowed := []struct {
		name string
		sql  string
	}{
		{"SELECT", "SELECT id, name FROM llm_models WHERE enabled = true"},
		{"小写 select（大小写不敏感）", "select 1"},
		{"前置空白", "   \n\tSELECT 1"},
		{"EXPLAIN", "EXPLAIN SELECT 1"},
		{"SHOW", "SHOW max_connections"},
		{"TABLE", "TABLE llm_models"},
		{"VALUES", "VALUES (1), (2)"},
		{"WITH（纯读 CTE）", "WITH x AS (SELECT 1 AS n) SELECT n FROM x"},
		{"尾随分号（单语句）", "SELECT 1;"},
	}
	for _, c := range allowed {
		t.Run("放行/"+c.name, func(t *testing.T) {
			if reason := validateReadOnlySQL(c.sql); reason != "" {
				t.Errorf("%q 应被放行，实际被拒：%s", c.sql, reason)
			}
		})
	}
}

// handler 层：两个"执行"端点必须在**触库之前**就拒掉非只读语句。
//
// 为什么这条比纯函数测试更值得写：纯函数测试只证明"校验本身对"，证明不了
// "端点真的接了它"。B4 修的正是后者 —— DBExecute 当年自己写了一份更宽松的白名单，
// 校验函数存在却没被调用。这里用 403 作为证据（校验在 db 调用之前返回，
// 所以这些用例不需要数据库）。
func TestDBExecuteRejectsNonReadOnlyBeforeTouchingDB(t *testing.T) {
	h := &SystemHandler{}
	cases := []struct {
		name string
		sql  string
	}{
		{"多语句", "SELECT 1; DROP TABLE users"},
		{"事务控制（本次收紧）", "SET search_path = public"},
		{"WITH 内嵌 DELETE", "WITH x AS (DELETE FROM users RETURNING id) SELECT * FROM x"},
		{"INSERT", "INSERT INTO users (id) VALUES ('x')"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			body := fmt.Sprintf(`{"sql":%q}`, c.sql)
			req := httptest.NewRequest(http.MethodPost, "/v1/internal/db/execute", strings.NewReader(body))
			w := httptest.NewRecorder()
			h.DBExecute(w, req)
			if w.Code != http.StatusForbidden {
				t.Fatalf("期望 403，实得 %d：%s", w.Code, w.Body.String())
			}
			// 文案必须来自共享校验（而不是各端点自己硬编码的那句）
			if !strings.Contains(w.Body.String(), "read-only") &&
				!strings.Contains(w.Body.String(), "multiple statements") &&
				!strings.Contains(w.Body.String(), "data-modifying CTE") {
				t.Fatalf("拒绝理由不是共享校验给出的：%s", w.Body.String())
			}
		})
	}
}

// 批量端点：逐条校验，一条越界就整批拒绝。
func TestDBBatchExecuteRejectsIfAnyStatementIsNotReadOnly(t *testing.T) {
	h := &SystemHandler{}
	body := `{"queries":["SELECT 1","SELECT id FROM users; DROP TABLE users"]}`
	req := httptest.NewRequest(http.MethodPost, "/v1/internal/db/batch-execute", strings.NewReader(body))
	w := httptest.NewRecorder()
	h.DBBatchExecute(w, req)
	if w.Code != http.StatusForbidden {
		t.Fatalf("批量里混入非只读语句应 403，实得 %d：%s", w.Code, w.Body.String())
	}
	// 空批量仍是 400（保持原有参数校验语义）
	req = httptest.NewRequest(http.MethodPost, "/v1/internal/db/batch-execute", strings.NewReader(`{"queries":[]}`))
	w = httptest.NewRecorder()
	h.DBBatchExecute(w, req)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("空 queries 应 400，实得 %d", w.Code)
	}
}
