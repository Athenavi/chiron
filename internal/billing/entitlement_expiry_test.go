package billing

import (
	"context"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/notify"
)

// expiryFakeStore 只实现扫描需要的那一个方法。
//
// 嵌入 nil 的 Store 接口：其余方法不会被调到，调到即 panic —— 这正好能在测试
// 里暴露"扫描意外触碰了别的存储调用"。完整实现 10 多个方法只会淹没意图。
type expiryFakeStore struct {
	Store
	items []Entitlement
	err   error
}

func (f *expiryFakeStore) ExpiringEntitlements(context.Context, time.Time, int) ([]Entitlement, error) {
	return f.items, f.err
}

// dedupeNotifier 模拟 (user_id, dedupe_key) 唯一索引的去重行为。
//
// recordingNotifier 每次都记，无法验证"第二轮扫描不再推送"，所以这里按
// userID+key 去重，inserted 如实反映是否首次。
type dedupeNotifier struct {
	seen     map[string]bool
	inserted []*notify.Notification
}

func newDedupeNotifier() *dedupeNotifier {
	return &dedupeNotifier{seen: map[string]bool{}}
}

func (d *dedupeNotifier) Notify(_ context.Context, n *notify.Notification) (bool, error) {
	k := n.UserID + "\x00" + n.DedupeKey
	if d.seen[k] {
		return false, nil
	}
	d.seen[k] = true
	cp := *n
	d.inserted = append(d.inserted, &cp)
	return true, nil
}

// 档位判定：向上取整，只有恰好落在 3 或 1 时才发。
func TestExpiryWarnBucket(t *testing.T) {
	now := time.Date(2026, 10, 30, 12, 0, 0, 0, time.UTC)
	cases := []struct {
		name    string
		expires time.Duration
		wantDay int
		wantOK  bool
	}{
		{"整 3 天", 72 * time.Hour, 3, true},
		{"2.4 天归入 3 天档", 58 * time.Hour, 3, true},
		{"3.9 天不在档位", 94 * time.Hour, 0, false},
		{"整 1 天", 24 * time.Hour, 1, true},
		{"1.4 天不在档位", 34 * time.Hour, 0, false},
		{"0.5 天归入 1 天档", 12 * time.Hour, 1, true},
		{"刚好到期", 0, 0, false},
		{"已过期", -time.Hour, 0, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			day, ok := expiryWarnBucket(now, now.Add(tc.expires))
			if ok != tc.wantOK || day != tc.wantDay {
				t.Fatalf("剩余 %v：期望 (%d,%v)，实际 (%d,%v)",
					tc.expires, tc.wantDay, tc.wantOK, day, ok)
			}
		})
	}
}

// 一轮扫描：3 天档与 1 天档各发一条，各自带自己的去重键。
func TestScanEntitlementExpirySendsBothBuckets(t *testing.T) {
	now := time.Date(2026, 10, 30, 12, 0, 0, 0, time.UTC)
	store := &expiryFakeStore{items: []Entitlement{
		{ID: "ent-3d", UserID: "u-1", PackageName: "入门包", PackageCode: "starter",
			ExpiresAt: now.Add(72 * time.Hour)},
		{ID: "ent-1d", UserID: "u-2", PackageName: "专业包", PackageCode: "pro",
			ExpiresAt: now.Add(24 * time.Hour)},
	}}
	m := NewManager(store)
	dn := newDedupeNotifier()
	m.SetNotifier(dn)

	res, err := m.ScanEntitlementExpiry(context.Background(), now)
	if err != nil {
		t.Fatalf("扫描报错: %v", err)
	}
	if res.Scanned != 2 || res.Notified != 2 {
		t.Fatalf("期望 Scanned=2 Notified=2，实际 %+v", res)
	}
	if len(dn.inserted) != 2 {
		t.Fatalf("应发 2 条通知，实际 %d", len(dn.inserted))
	}

	byKey := map[string]*notify.Notification{}
	for _, n := range dn.inserted {
		byKey[n.DedupeKey] = n
		if n.Kind != notify.KindEntitlementExpiring {
			t.Errorf("kind 应为 %s，实际 %s", notify.KindEntitlementExpiring, n.Kind)
		}
	}
	if _, ok := byKey["entitlement_expiring:ent-3d:3"]; !ok {
		t.Errorf("缺少 3 天档去重键，实际键集 %v", keysOf(byKey))
	}
	if _, ok := byKey["entitlement_expiring:ent-1d:1"]; !ok {
		t.Errorf("缺少 1 天档去重键，实际键集 %v", keysOf(byKey))
	}
	// 文案必须含套餐名与到期日：只说"快到期了"用户无从判断该续哪个。
	n3 := byKey["entitlement_expiring:ent-3d:3"]
	if !contains(n3.Title, "3") || !contains(n3.Body, "入门包") {
		t.Errorf("3 天档文案不符: title=%q body=%q", n3.Title, n3.Body)
	}
	if !contains(n3.Body, "2026-11-02") {
		t.Errorf("正文应含到期日 2026-11-02，实际 %q", n3.Body)
	}
}

// 第二轮扫描：同一档位撞去重键即跳过 —— 这是"每小时扫一次"不产生重复推送的关键。
func TestScanEntitlementExpiryIsIdempotent(t *testing.T) {
	now := time.Date(2026, 10, 30, 12, 0, 0, 0, time.UTC)
	store := &expiryFakeStore{items: []Entitlement{
		{ID: "ent-1", UserID: "u-1", PackageName: "入门包", ExpiresAt: now.Add(72 * time.Hour)},
	}}
	m := NewManager(store)
	dn := newDedupeNotifier()
	m.SetNotifier(dn)
	ctx := context.Background()

	first, err := m.ScanEntitlementExpiry(ctx, now)
	if err != nil {
		t.Fatalf("首轮扫描报错: %v", err)
	}
	if first.Notified != 1 {
		t.Fatalf("首轮应发 1 条，实际 %d", first.Notified)
	}

	// 一小时后重新扫（仍在 3 天档，ceil(71h/24)=3）。
	second, err := m.ScanEntitlementExpiry(ctx, now.Add(time.Hour))
	if err != nil {
		t.Fatalf("第二轮扫描报错: %v", err)
	}
	if second.Notified != 0 {
		t.Fatalf("同一档位不应重复推送，实际发了 %d 条", second.Notified)
	}
	if len(dn.inserted) != 1 {
		t.Fatalf("总共应只有 1 条通知，实际 %d", len(dn.inserted))
	}
}

// 同一份权益从 3 天档走到 1 天档时必须**再发一条** —— 两个档位是两件事。
func TestScanEntitlementExpiryBothBucketsForSameEntitlement(t *testing.T) {
	start := time.Date(2026, 10, 30, 12, 0, 0, 0, time.UTC)
	expires := start.Add(72 * time.Hour)
	store := &expiryFakeStore{items: []Entitlement{
		{ID: "ent-1", UserID: "u-1", PackageName: "入门包", ExpiresAt: expires},
	}}
	m := NewManager(store)
	dn := newDedupeNotifier()
	m.SetNotifier(dn)
	ctx := context.Background()

	if _, err := m.ScanEntitlementExpiry(ctx, start); err != nil {
		t.Fatalf("3 天档扫描报错: %v", err)
	}
	// 两天后再扫：剩 24 小时 → 1 天档。
	if _, err := m.ScanEntitlementExpiry(ctx, start.Add(48*time.Hour)); err != nil {
		t.Fatalf("1 天档扫描报错: %v", err)
	}
	if len(dn.inserted) != 2 {
		t.Fatalf("两个档位应各发一条（共 2 条），实际 %d", len(dn.inserted))
	}
	if dn.inserted[1].DedupeKey != "entitlement_expiring:ent-1:1" {
		t.Errorf("第二档去重键不符: %s", dn.inserted[1].DedupeKey)
	}
}

// 不在档位上的剩余时间不发通知，但仍计入 Scanned。
func TestScanEntitlementExpirySkipsOffBucket(t *testing.T) {
	now := time.Date(2026, 10, 30, 12, 0, 0, 0, time.UTC)
	store := &expiryFakeStore{items: []Entitlement{
		{ID: "ent-1", UserID: "u-1", ExpiresAt: now.Add(34 * time.Hour)}, // ceil=2
		{ID: "ent-2", UserID: "u-2", ExpiresAt: now.Add(94 * time.Hour)}, // ceil=4
	}}
	m := NewManager(store)
	dn := newDedupeNotifier()
	m.SetNotifier(dn)

	res, err := m.ScanEntitlementExpiry(context.Background(), now)
	if err != nil {
		t.Fatalf("扫描报错: %v", err)
	}
	if res.Scanned != 2 {
		t.Fatalf("Scanned 应为 2，实际 %d", res.Scanned)
	}
	if res.Notified != 0 || len(dn.inserted) != 0 {
		t.Fatalf("不在档位时不应发通知，实际 %d 条", len(dn.inserted))
	}
}

// 未装配通知中心时应直接返回，且不触碰存储（Store 为 nil 也不 panic）。
func TestScanEntitlementExpiryWithoutNotifierIsNoop(t *testing.T) {
	m := NewManager(nil) // store 为 nil：一旦查询就会 panic
	res, err := m.ScanEntitlementExpiry(context.Background(), time.Now())
	if err != nil {
		t.Fatalf("未装配时不应报错: %v", err)
	}
	if res.Scanned != 0 || res.Notified != 0 {
		t.Fatalf("未装配时应为全零，实际 %+v", res)
	}
}

func keysOf(m map[string]*notify.Notification) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
