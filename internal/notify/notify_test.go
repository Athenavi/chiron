package notify

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"
)

// fakeStore 是 Store 的内存替身。
//
// 手写而不是引 mock 库：本仓库没有 pgxmock/sqlmock，且这些用例要断言的正是
// "重复键被吞掉""查偏好失败要降级"这类控制流，手写替身比断言调用次数更直观。
type fakeStore struct {
	mu sync.Mutex

	// seen 模拟 ux_notifications_user_dedupe：key = userID + "\x00" + dedupeKey。
	seen map[string]bool

	// emailPrefs 显式设置过的开关；缺省即默认开启。
	emailPrefs map[string]bool
	// prefErr 非 nil 时 EmailEnabled 返回错误，用于验证降级路径。
	prefErr error

	insertCalls int

	// ── 保留期清理（T2）──
	// pending 模拟"过期行数"：每次 DeleteOlderThan 删掉不超 batchSize 行。
	pending int
	// deleteErr 非 nil 时 DeleteOlderThan 返回错误。
	deleteErr error
	// deleteCalls / lastCutoff / lastBatchSize 供断言参数是否被正确传入。
	deleteCalls   int
	lastCutoff    time.Time
	lastBatchSize int
}

func newFakeStore() *fakeStore {
	return &fakeStore{
		seen:       make(map[string]bool),
		emailPrefs: make(map[string]bool),
	}
}

func prefKey(userID, kind string) string { return userID + "\x00" + kind }

func (f *fakeStore) Insert(_ context.Context, n *Notification) (bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.insertCalls++
	k := n.UserID + "\x00" + n.DedupeKey
	if f.seen[k] {
		return false, nil
	}
	f.seen[k] = true
	return true, nil
}

func (f *fakeStore) List(context.Context, string, int, bool) ([]Notification, error) {
	return nil, nil
}

func (f *fakeStore) UnreadCount(context.Context, string) (int, error) { return 0, nil }

func (f *fakeStore) MarkRead(context.Context, string, []string) (int, error) { return 0, nil }

func (f *fakeStore) MarkAllRead(context.Context, string) (int, error) { return 0, nil }

func (f *fakeStore) EmailEnabled(_ context.Context, userID, kind string) (bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.prefErr != nil {
		return false, f.prefErr
	}
	// 缺行即默认开启 —— 与 PG 实现的 COALESCE(..., TRUE) 同语义。
	if v, ok := f.emailPrefs[prefKey(userID, kind)]; ok {
		return v, nil
	}
	return true, nil
}

func (f *fakeStore) SetEmailEnabled(_ context.Context, userID, kind string, enabled bool) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.emailPrefs[prefKey(userID, kind)] = enabled
	return nil
}

func (f *fakeStore) ListPrefs(_ context.Context, userID string) (map[string]bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make(map[string]bool)
	for k, v := range f.emailPrefs {
		// k = userID + "\x00" + kind
		if len(k) > len(userID) && k[:len(userID)] == userID {
			out[k[len(userID)+1:]] = v
		}
	}
	return out, nil
}

// DeleteOlderThan 模拟分批删除。
//
// 用 pending 计数器而不是真存行：这些用例要断言的是「分批循环是否收敛」与
// 「cutoff/batchSize 是否被正确传入」，不是 SQL 本身（后者由真库用例覆盖）。
func (f *fakeStore) DeleteOlderThan(_ context.Context, cutoff time.Time, batchSize int) (int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.deleteCalls++
	f.lastCutoff = cutoff
	f.lastBatchSize = batchSize
	if f.deleteErr != nil {
		return 0, f.deleteErr
	}
	if f.pending <= 0 {
		return 0, nil
	}
	n := batchSize
	if n > f.pending {
		n = f.pending
	}
	f.pending -= n
	return n, nil
}

// fakeMailer 记录发信次数，可注入错误。
type fakeMailer struct {
	mu    sync.Mutex
	sent  []string // 收件 userID 列表
	failOn error
}

func (f *fakeMailer) SendNotification(_ context.Context, userID, _, _ string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.failOn != nil {
		return f.failOn
	}
	f.sent = append(f.sent, userID)
	return nil
}

func (f *fakeMailer) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.sent)
}

func newNote(userID, key string) *Notification {
	return &Notification{
		TenantID:  1,
		UserID:    userID,
		Kind:      KindPaymentSucceeded,
		DedupeKey: key,
		Title:     "支付成功",
		Body:      "已到账 1000 credits",
	}
}

// 同一事件对同一用户只推一次：重复回调不能重复发信。
func TestNotifyDedupesPerUserAndKey(t *testing.T) {
	store := newFakeStore()
	mail := &fakeMailer{}
	m := NewManager(store, mail)
	ctx := context.Background()

	first, err := m.Notify(ctx, newNote("u-1", "payment_succeeded:pay_1"))
	if err != nil {
		t.Fatalf("首次 Notify 报错: %v", err)
	}
	if !first {
		t.Fatal("首次 Notify 应返回 inserted=true")
	}

	second, err := m.Notify(ctx, newNote("u-1", "payment_succeeded:pay_1"))
	if err != nil {
		t.Fatalf("重复 Notify 不应报错: %v", err)
	}
	if second {
		t.Fatal("重复键应返回 inserted=false")
	}
	if got := mail.count(); got != 1 {
		t.Fatalf("重复事件只应发 1 封邮件，实际 %d", got)
	}
}

// 去重键带 user_id：同一事件通知不同人时，第二个人不能被静默吞掉。
func TestNotifySameEventDifferentUsersBothDelivered(t *testing.T) {
	store := newFakeStore()
	mail := &fakeMailer{}
	m := NewManager(store, mail)
	ctx := context.Background()

	const key = "quota_changed:pool_1:monthly"
	for _, uid := range []string{"u-1", "u-2"} {
		inserted, err := m.Notify(ctx, newNote(uid, key))
		if err != nil {
			t.Fatalf("%s Notify 报错: %v", uid, err)
		}
		if !inserted {
			t.Fatalf("%s 的同一事件被误判为重复", uid)
		}
	}
	if got := mail.count(); got != 2 {
		t.Fatalf("两个收件人应各发 1 封，实际 %d", got)
	}
}

// 用户关掉该类邮件后：站内信照写，邮件不发。
func TestNotifySkipsMailWhenEmailDisabled(t *testing.T) {
	store := newFakeStore()
	mail := &fakeMailer{}
	m := NewManager(store, mail)
	ctx := context.Background()

	if err := m.SetEmailEnabled(ctx, "u-1", KindPaymentSucceeded, false); err != nil {
		t.Fatalf("关邮件失败: %v", err)
	}

	inserted, err := m.Notify(ctx, newNote("u-1", "payment_succeeded:pay_2"))
	if err != nil {
		t.Fatalf("Notify 报错: %v", err)
	}
	if !inserted {
		t.Fatal("站内信必须照写，不因关邮件而跳过")
	}
	if got := mail.count(); got != 0 {
		t.Fatalf("关掉邮件后不应发信，实际 %d 封", got)
	}
}

// 查偏好失败时按"默认开启"继续发：宁可多发，也不能因一次读失败错过到期预警。
func TestNotifyFallsBackToSendWhenPrefReadFails(t *testing.T) {
	store := newFakeStore()
	store.prefErr = errors.New("db down")
	mail := &fakeMailer{}
	m := NewManager(store, mail)

	if _, err := m.Notify(context.Background(), newNote("u-1", "payment_succeeded:pay_3")); err != nil {
		t.Fatalf("Notify 报错: %v", err)
	}
	if got := mail.count(); got != 1 {
		t.Fatalf("偏好读失败应按默认开启发信，实际 %d 封", got)
	}
}

// 邮件发送失败不影响 Notify 的成功语义：站内信已落地。
func TestNotifyEmailFailureDoesNotFailNotify(t *testing.T) {
	store := newFakeStore()
	mail := &fakeMailer{failOn: errors.New("smtp refused")}
	m := NewManager(store, mail)

	inserted, err := m.Notify(context.Background(), newNote("u-1", "payment_succeeded:pay_4"))
	if err != nil {
		t.Fatalf("邮件失败不应让 Notify 报错: %v", err)
	}
	if !inserted {
		t.Fatal("站内信应已写入")
	}
}

// 没有配置邮件时不应 panic。
func TestNotifyWithoutMailer(t *testing.T) {
	store := newFakeStore()
	m := NewManager(store, nil)

	inserted, err := m.Notify(context.Background(), newNote("u-1", "payment_succeeded:pay_5"))
	if err != nil {
		t.Fatalf("无 mailer 时报错: %v", err)
	}
	if !inserted {
		t.Fatal("站内信应已写入")
	}
}

// Prefs 必须把未设置过的类型补成"开启"，接口才能给出实际生效值。
func TestPrefsFillsDefaults(t *testing.T) {
	store := newFakeStore()
	m := NewManager(store, nil)
	ctx := context.Background()

	if err := m.SetEmailEnabled(ctx, "u-1", KindQuotaChanged, false); err != nil {
		t.Fatalf("设置偏好失败: %v", err)
	}

	prefs, err := m.Prefs(ctx, "u-1")
	if err != nil {
		t.Fatalf("Prefs 报错: %v", err)
	}
	if len(prefs) != len(AllKinds) {
		t.Fatalf("应返回全部 %d 类开关，实际 %d", len(AllKinds), len(prefs))
	}
	if prefs[KindQuotaChanged] {
		t.Fatal("显式关掉的 quota_changed 应为 false")
	}
	for _, k := range AllKinds {
		if k == KindQuotaChanged {
			continue
		}
		if !prefs[k] {
			t.Fatalf("未设置过的 %s 应默认开启", k)
		}
	}
}

// 未知事件类型必须在写入偏好前就被拒掉，避免垃圾行。
func TestSetEmailEnabledRejectsUnknownKind(t *testing.T) {
	store := newFakeStore()
	m := NewManager(store, nil)

	err := m.SetEmailEnabled(context.Background(), "u-1", "made_up_kind", true)
	if !errors.Is(err, ErrUnknownKind) {
		t.Fatalf("未知类型应返回 ErrUnknownKind，实际 %v", err)
	}
	if len(store.emailPrefs) != 0 {
		t.Fatal("被拒的请求不应写入任何偏好行")
	}
}

// nil 通知直接忽略，不应打到存储层。
func TestNotifyNilIsNoop(t *testing.T) {
	store := newFakeStore()
	m := NewManager(store, nil)

	inserted, err := m.Notify(context.Background(), nil)
	if err != nil || inserted {
		t.Fatalf("nil 通知应为 (false, nil)，实际 (%v, %v)", inserted, err)
	}
	if store.insertCalls != 0 {
		t.Fatalf("nil 通知不应调用 Insert，实际调用 %d 次", store.insertCalls)
	}
}
