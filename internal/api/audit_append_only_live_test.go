package api

import (
	"context"
	"strings"
	"testing"

	"github.com/google/uuid"
)

// ── 审计表 append-only（真库）──
//
// 这条迁移的价值全在"PG 真的拦住了"：读 SQL 能看到 `CREATE TRIGGER ... BEFORE UPDATE`，
// 但触发器没挂在表上、函数体写错、或者被后续迁移 DROP 掉，静态检查都看不出来。
//
// 三条断言缺一不可：
//  1. UPDATE 必须被拒（这是本迁移的全部目的）；
//  2. INSERT 必须仍可用（否则整个审计写入链路全断）；
//  3. DELETE 必须仍可用（保留期清理 SweepAuditRetention 依赖它 ——
//     如果为了"防篡改"把 DELETE 也堵死，审计表会无限增长撑爆磁盘）。

// insertAuditRow 插入一条测试审计记录并返回 id；用例结束自动清理。
//
// 这个辅助本身就在验证「INSERT 仍可用」—— 若触发器写错把 INSERT 也拦了，
// 所有用到它的用例会在第一步就失败，而不是悄悄跳过。
//
// ⚠ 必须建真实的 tenant + user：`audit_logs` 对两者都有外键
// （audit_logs_tenant_id_fkey / audit_logs_user_id_fkey），
// 用凭空编的 id 会直接撞 23503，看起来像"INSERT 坏了"，实际是测试造数不对。
func insertAuditRow(t *testing.T, marker string) string {
	t.Helper()
	pool := withP12Pool(t)
	tenantID, userID := liveSmokeFixture(t, pool)
	ctx := context.Background()

	id := uuid.NewString()
	// 清理顺序：先删审计行再删 user/tenant（外键方向）。
	// liveSmokeFixture 自己的 cleanup 在本函数之后注册，LIFO 下会先跑这里，
	// 因此不会出现"users 已删、audit_logs 还在引用"的冲突。
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), `DELETE FROM audit_logs WHERE id = $1`, id)
	})

	if _, err := pool.Exec(ctx, `
		INSERT INTO audit_logs (id, tenant_id, user_id, action, resource_type, resource_id, details, ip_address, created_at)
		VALUES ($1, $2, $3, $4, 'test', $5, '{}'::json, '127.0.0.1', now())`,
		id, tenantID, userID, marker, marker); err != nil {
		t.Fatalf("插入审计记录失败（INSERT 必须保持可用）: %v", err)
	}
	return id
}

// 核心断言：UPDATE 必须被触发器拒绝。
func TestLiveAuditLogsRejectUpdate(t *testing.T) {
	pool := withP12Pool(t)
	id := insertAuditRow(t, "audit-append-only-update")

	_, err := pool.Exec(context.Background(),
		`UPDATE audit_logs SET action = 'tampered' WHERE id = $1`, id)
	if err == nil {
		t.Fatal("UPDATE 竟然成功了：审计记录可被篡改，append-only 承诺不成立")
	}
	// 确认是触发器拦的，而不是别的原因（比如约束冲突）。
	msg := err.Error()
	if !strings.Contains(msg, "append-only") {
		t.Fatalf("期望被 append-only 触发器拒绝，实际错误：%v", err)
	}

	// 而且记录必须保持原样 —— 不能出现"报错了但改了一半"。
	var action string
	if qerr := pool.QueryRow(context.Background(),
		`SELECT COALESCE(action, '') FROM audit_logs WHERE id = $1`, id).Scan(&action); qerr != nil {
		t.Fatalf("读回记录失败: %v", qerr)
	}
	if action != "audit-append-only-update" {
		t.Fatalf("记录被改动了：action = %q", action)
	}
}

// INSERT 必须保持可用：这是审计写入链路的唯一路径。
func TestLiveAuditLogsAllowInsert(t *testing.T) {
	// insertAuditRow 内部断言插入成功并在失败时 t.Fatalf，取得 id 即证明可用。
	if id := insertAuditRow(t, "audit-append-only-insert"); id == "" {
		t.Fatal("没有拿到新插入记录的 id")
	}
}

// DELETE 必须保持可用：保留期清理（SweepAuditRetention）依赖它。
// 若为了防篡改把 DELETE 也堵死，审计表会无限增长。
func TestLiveAuditLogsAllowDelete(t *testing.T) {
	pool := withP12Pool(t)
	id := insertAuditRow(t, "audit-append-only-delete")

	if _, err := pool.Exec(context.Background(),
		`DELETE FROM audit_logs WHERE id = $1`, id); err != nil {
		t.Fatalf("DELETE 被拒绝：保留期清理会失效，审计表将无限增长 —— %v", err)
	}
	var n int
	if err := pool.QueryRow(context.Background(),
		`SELECT count(*) FROM audit_logs WHERE id = $1`, id).Scan(&n); err != nil {
		t.Fatalf("统计失败: %v", err)
	}
	if n != 0 {
		t.Fatalf("删除后仍有 %d 行", n)
	}
}

// 触发器必须真的挂在表上 —— 防止"迁移写了但没生效"（例如被后续迁移 DROP）。
func TestLiveAuditLogsTriggerExists(t *testing.T) {
	pool := withP12Pool(t)

	var count int
	if err := pool.QueryRow(context.Background(), `
		SELECT count(*) FROM pg_trigger tg
		  JOIN pg_class c ON c.oid = tg.tgrelid
		 WHERE c.relname = 'audit_logs'
		   AND tg.tgname = 'audit_logs_no_update'
		   AND NOT tg.tgisinternal`).Scan(&count); err != nil {
		t.Fatalf("查询触发器失败: %v", err)
	}
	if count != 1 {
		t.Fatalf("audit_logs_no_update 触发器不存在（count=%d）：迁移可能未应用或被后续迁移删除", count)
	}
}
