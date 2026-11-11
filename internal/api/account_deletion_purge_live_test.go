package api

import (
	"context"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/storage"
)

// ── 到期清除（真库 + 本地存储）──
//
// 这条路径最危险的地方不是"删不干净"，而是**删了但没归档** ——
// 那是不可逆的数据丢失。所以两条硬约束必须用测试钉住：
//  1. 没有存储后端时**一个字节都不删**；
//  2. 归档成功后才删，且归档内容确实落在对象存储里。
//
// 另外验证匿名化：email 置 NULL（不能置空串 —— `ux_users_tenant_email`
// 是唯一索引，多个注销账号写空串会互相冲突）。

// expireNow 把账号的到期时间推到过去，模拟宽限期已过。
func expireNow(t *testing.T, userID string) {
	t.Helper()
	if _, err := withP12Pool(t).Exec(context.Background(),
		`UPDATE users SET deletion_due_at = NOW() - interval '1 hour' WHERE id = $1`,
		userID); err != nil {
		t.Fatalf("设置到期时间失败: %v", err)
	}
}

// 没有存储后端 → 不删任何数据。
//
// 这是"先归档后才删"的硬约束：存储没配好时宁可留着数据，也不能裸删。
func TestLivePurgeSkipsWithoutStorage(t *testing.T) {
	pool := withP12Pool(t)
	_, userID := liveSmokeFixture(t, pool)
	p12Cleanup(t, pool, userID)
	h := mfaTestHandler(t)

	// 造一条会话（可验证"是否被删"）。
	sid := p12Session(t, pool, 1, userID)
	if rec := deletionRequestWithPwd(t, h, userID); rec.Code != 200 {
		// owner 之外的普通用户、无密码时 confirm 即可。
		t.Logf("申请返回 %d: %s", rec.Code, rec.Body.String())
	}
	expireNow(t, userID)

	// store = nil：必须跳过。
	if n := purgeDueAccounts(context.Background(), nil); n != 0 {
		t.Fatalf("无存储后端时不应处理任何账号，实际处理了 %d 个", n)
	}

	var sessions int
	if err := pool.QueryRow(context.Background(),
		`SELECT count(*) FROM sessions WHERE id = $1`, sid).Scan(&sessions); err != nil {
		t.Fatalf("统计会话失败: %v", err)
	}
	if sessions != 1 {
		t.Fatal("无存储后端时数据被删了：归档是删除的前提，不能跳过")
	}
}

// 归档成功 → 数据被删 + user 匿名化 + 状态变 purged。
func TestLivePurgeArchivesThenRemoves(t *testing.T) {
	pool := withP12Pool(t)
	_, userID := liveSmokeFixture(t, pool)
	p12Cleanup(t, pool, userID)
	h := mfaTestHandler(t)
	ctx := context.Background()

	// 给账号填上可识别的身份信息，验证匿名化确实清掉了它们。
	if _, err := pool.Exec(ctx,
		`UPDATE users SET email = $2, name = 'Real Name', phone = '13800000000' WHERE id = $1`,
		userID, userID+"@example.com"); err != nil {
		t.Fatalf("设置身份信息失败: %v", err)
	}
	sid := p12Session(t, pool, 1, userID)

	deletionRequestWithPwd(t, h, userID)
	expireNow(t, userID)

	dir := t.TempDir()
	store := storage.NewAtomicStore(storage.NewLocalStore(dir))
	if n := purgeDueAccounts(ctx, store); n != 1 {
		t.Fatalf("应处理 1 个账号，实际 %d", n)
	}

	// ① 归档文件必须存在且含该用户邮箱（证明确实是这份数据）。
	archivePath := filepath.Join(dir, "account-archive", userID)
	entries, err := os.ReadDir(archivePath)
	if err != nil {
		t.Fatalf("归档目录不存在（归档失败却仍删了数据？）: %v", err)
	}
	if len(entries) == 0 {
		t.Fatal("归档目录为空")
	}
	raw, err := os.ReadFile(filepath.Join(archivePath, entries[0].Name()))
	if err != nil {
		t.Fatalf("读归档文件失败: %v", err)
	}
	if !strings.Contains(string(raw), userID+"@example.com") {
		t.Fatal("归档内容里没有该账号的邮箱：归档的可能不是这份数据")
	}

	// ② 会话已删
	var sessions int
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM sessions WHERE id = $1`, sid).Scan(&sessions); err != nil {
		t.Fatalf("统计会话失败: %v", err)
	}
	if sessions != 0 {
		t.Fatal("会话未被删除")
	}

	// ③ user 行仍在（audit_logs 外键引用它）但已匿名化
	var (
		status string
		email  *string
		name   *string
		phone  *string
		hash   *string
	)
	if err := pool.QueryRow(ctx,
		`SELECT account_status, email, name, phone, password_hash
		   FROM users WHERE id = $1`, userID).
		Scan(&status, &email, &name, &phone, &hash); err != nil {
		t.Fatalf("读回用户失败（行不该被删：审计外键仍指向它）: %v", err)
	}
	if status != accountStatusPurged {
		t.Fatalf("状态应为 purged，得到 %q", status)
	}
	if email != nil {
		t.Fatalf("email 应置 NULL（置空串会撞唯一索引），得到 %q", *email)
	}
	if name != nil || phone != nil || hash != nil {
		t.Fatalf("身份字段未清空：name=%v phone=%v hash=%v", name, phone, hash)
	}
}

// 未到期的账号不能被处理（宽限期必须真的生效）。
func TestLivePurgeSkipsNotYetDue(t *testing.T) {
	pool := withP12Pool(t)
	_, userID := liveSmokeFixture(t, pool)
	p12Cleanup(t, pool, userID)
	h := mfaTestHandler(t)

	deletionRequestWithPwd(t, h, userID)
	// 不推到过去：到期时间仍在 7 天后。

	dir := t.TempDir()
	store := storage.NewAtomicStore(storage.NewLocalStore(dir))
	if n := purgeDueAccounts(context.Background(), store); n != 0 {
		t.Fatalf("未到期账号不该被处理，实际处理了 %d 个", n)
	}
	var status string
	if err := pool.QueryRow(context.Background(),
		`SELECT account_status FROM users WHERE id = $1`, userID).Scan(&status); err != nil {
		t.Fatalf("读回状态失败: %v", err)
	}
	if status != accountStatusPendingDeletion {
		t.Fatalf("状态应仍为 pending_deletion，得到 %q", status)
	}
}

// deletionRequestWithPwd 申请注销（这些用例的账号没设密码，confirm 即可）。
func deletionRequestWithPwd(t *testing.T, h *AuthHandler, userID string) *httptest.ResponseRecorder {
	t.Helper()
	return deletionRequest(t, h, userID, "")
}
