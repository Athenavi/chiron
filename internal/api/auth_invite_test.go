package api

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5"
)

// ── 测试替身 ──
//
// 仓库没有 pgxmock/sqlmock，单元测试也不许连真实数据库。`inviteStore` 只有一个
// 方法，用一个记录调用的假实现就够把"单语句语义"钉住。

type fakeInviteRow struct {
	scan func(dest ...any) error
}

func (r fakeInviteRow) Scan(dest ...any) error {
	return r.scan(dest...)
}

type fakeInviteStore struct {
	calls []string
	args  [][]any
	row   fakeInviteRow
}

func (s *fakeInviteStore) QueryRow(_ context.Context, sql string, args ...any) pgx.Row {
	s.calls = append(s.calls, sql)
	s.args = append(s.args, args)
	return s.row
}

// ── 语句形态 ──

// TestConsumeInviteQuery_IsSingleAtomicStatement 是这次修复的核心回归点。
//
// 旧实现是「SELECT 校验 → UPDATE 自增」两步，`max_uses=1` 的码在并发下能被用两次。
// 一旦有人把它改回去（或把 `uses < max_uses` 从 WHERE 里拿掉），下面几条断言会红。
func TestConsumeInviteQuery_IsSingleAtomicStatement(t *testing.T) {
	q := strings.ToLower(consumeInviteQuery)

	// 必须是 UPDATE，且带 RETURNING —— 用影响行数 + 返回值一次拿到结果
	if !strings.HasPrefix(strings.TrimSpace(q), "update tenant_invites") {
		t.Fatalf("consumeInviteQuery 必须以 UPDATE 开头，实际：%q", consumeInviteQuery)
	}
	if !strings.Contains(q, "returning tenant_id") {
		t.Error("consumeInviteQuery 必须 RETURNING tenant_id，否则拿不到目标租户")
	}

	// 自增与三个校验条件必须在**同一条语句**里
	for _, want := range []string{"uses = uses + 1", "uses < max_uses", "max_uses = 0", "expires_at"} {
		if !strings.Contains(q, want) {
			t.Errorf("consumeInviteQuery 缺少 %q —— 校验与自增必须原子完成", want)
		}
	}

	// 不允许出现独立的 SELECT：那意味着又退回了"先查后写"
	if strings.Contains(q, "select") {
		t.Error("consumeInviteQuery 不应包含 SELECT —— 一步 SELECT 一步 UPDATE 就是并发超发的来源")
	}

	// 只能有一个占位符（$1 = code）；多出来的条件若靠应用层传值，就又有了窗口
	if got := strings.Count(q, "$1"); got == 0 {
		t.Error("consumeInviteQuery 应使用 $1 传邀请码")
	}
	if strings.Contains(q, "$2") {
		t.Error("consumeInviteQuery 不应有 $2 —— 上限/过期判断应由数据库在同一语句内完成")
	}
}

// ── 行为 ──

func TestConsumeInvite_ReturnsTenantOnSuccess(t *testing.T) {
	store := &fakeInviteStore{row: fakeInviteRow{scan: func(dest ...any) error {
		*(dest[0].(*int16)) = 7
		return nil
	}}}

	tenantID, err := consumeInvite(context.Background(), store, "INV-1")
	if err != nil {
		t.Fatalf("期望成功，得到错误：%v", err)
	}
	if tenantID != 7 {
		t.Errorf("tenantID = %d, want 7", tenantID)
	}
}

// TestConsumeInvite_SendsExactlyOneStatement：一条语句是原子性的前提。
// 若有人加回第二条（例如补一个 UPDATE last_used），这里会立刻失败。
func TestConsumeInvite_SendsExactlyOneStatement(t *testing.T) {
	store := &fakeInviteStore{row: fakeInviteRow{scan: func(dest ...any) error {
		*(dest[0].(*int16)) = 1
		return nil
	}}}

	if _, err := consumeInvite(context.Background(), store, "INV-1"); err != nil {
		t.Fatalf("期望成功，得到错误：%v", err)
	}
	if len(store.calls) != 1 {
		t.Fatalf("应只发一条语句，实际发了 %d 条：%v", len(store.calls), store.calls)
	}
}

// TestConsumeInvite_PassesCodeAsOnlyArgument：邀请码是唯一参数。
func TestConsumeInvite_PassesCodeAsOnlyArgument(t *testing.T) {
	store := &fakeInviteStore{row: fakeInviteRow{scan: func(dest ...any) error {
		*(dest[0].(*int16)) = 1
		return nil
	}}}

	if _, err := consumeInvite(context.Background(), store, "INV-XYZ"); err != nil {
		t.Fatalf("期望成功，得到错误：%v", err)
	}
	args := store.args[0]
	if len(args) != 1 {
		t.Fatalf("应只传 1 个参数，实际 %d 个：%v", len(args), args)
	}
	if args[0] != "INV-XYZ" {
		t.Errorf("参数应为邀请码，实际 %v", args[0])
	}
}

// TestConsumeInvite_NoRowsMeansExhaustedOrInvalid：影响 0 行（不存在/过期/用满）
// 必须合并成同一条文案 —— 分开报会让攻击者能枚举出"这个码存在且已用满"。
func TestConsumeInvite_NoRowsMeansExhaustedOrInvalid(t *testing.T) {
	store := &fakeInviteStore{row: fakeInviteRow{scan: func(dest ...any) error {
		return pgx.ErrNoRows
	}}}

	tenantID, err := consumeInvite(context.Background(), store, "INV-USED-UP")
	if err == nil {
		t.Fatal("期望报错，实际成功")
	}
	if tenantID != 0 {
		t.Errorf("失败时不应返回租户 ID，实际 %d", tenantID)
	}
	if want := "邀请码无效、已过期或已达使用上限"; err.Error() != want {
		t.Errorf("错误文案 = %q, want %q", err.Error(), want)
	}
	// 不能把三种原因拆开暴露给调用方
	for _, leak := range []string{"已被使用", "用满", "不存在于"} {
		if strings.Contains(err.Error(), leak) {
			t.Errorf("错误文案不应区分具体原因（会泄漏码的存在性）：%q", err.Error())
		}
	}
}

// TestConsumeInvite_DatabaseErrorIsNotReportedAsInvalid：数据库故障必须与
// "码无效"区分开 —— 否则用户会以为自己的码坏了，运营也会误判成业务问题。
func TestConsumeInvite_DatabaseErrorIsNotReportedAsInvalid(t *testing.T) {
	store := &fakeInviteStore{row: fakeInviteRow{scan: func(dest ...any) error {
		return errors.New("connection reset by peer")
	}}}

	_, err := consumeInvite(context.Background(), store, "INV-1")
	if err == nil {
		t.Fatal("期望报错，实际成功")
	}
	if err.Error() != "邀请码校验失败" {
		t.Errorf("错误文案 = %q, want %q", err.Error(), "邀请码校验失败")
	}
	if strings.Contains(err.Error(), "邀请码无效") {
		t.Error("数据库故障不应报成『邀请码无效』—— 用户会以为自己的码坏了")
	}
}

// TestConsumeInvite_UnlimitedUsesStillWorks：max_uses = 0 表示不限次，
// 这条语义完全靠 SQL 里的 `max_uses = 0 OR` 表达，假替身只能验证语句里确实有它。
func TestConsumeInvite_UnlimitedUsesStillWorks(t *testing.T) {
	q := strings.ToLower(consumeInviteQuery)
	if !strings.Contains(q, "max_uses = 0 or") {
		t.Fatal("consumeInviteQuery 必须保留 `max_uses = 0 OR uses < max_uses`，否则不限次邀请码会被误判为已满")
	}
}
