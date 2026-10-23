package api

import (
	"context"
	"errors"
	"testing"
)

// ── fake 预热数据源 ──

// fakeCountRows 驱动预热第一遍（SELECT tenant_id, COUNT(*)）。
type fakeCountRows struct {
	rows [][2]any // {tenantID string, count int64}
	i    int
}

func (r *fakeCountRows) Next() bool {
	if r.i >= len(r.rows) {
		return false
	}
	r.i++
	return true
}

func (r *fakeCountRows) Scan(dest ...any) error {
	row := r.rows[r.i-1]
	*(dest[0].(*string)) = row[0].(string)
	*(dest[1].(*int64)) = row[1].(int64)
	return nil
}

func (r *fakeCountRows) Close()     {}
func (r *fakeCountRows) Err() error { return nil }

// fakeScanRows 驱动预热第二遍（SELECT id, tenant_id, email）。
type fakeScanRows struct {
	rows [][3]string // {id, tenantID, email}
	i    int
}

func (r *fakeScanRows) Next() bool {
	if r.i >= len(r.rows) {
		return false
	}
	r.i++
	return true
}

func (r *fakeScanRows) Scan(dest ...any) error {
	row := r.rows[r.i-1]
	for i := 0; i < 3; i++ {
		*(dest[i].(*string)) = row[i]
	}
	return nil
}

func (r *fakeScanRows) Close()     {}
func (r *fakeScanRows) Err() error { return nil }

type fakeBloomQuerier struct {
	counts  [][2]any
	scanned [][3]string
	scanErr error
}

func (q *fakeBloomQuerier) Query(_ context.Context, sql string, _ ...any) (bloomRows, error) {
	// 两遍扫描按 SQL 文本区分：分片容量统计含 COUNT(*)。
	if len(sql) > 0 && containsCountStar(sql) {
		return &fakeCountRows{rows: q.counts}, nil
	}
	if q.scanErr != nil {
		return nil, q.scanErr
	}
	return &fakeScanRows{rows: q.scanned}, nil
}

func containsCountStar(sql string) bool {
	for i := 0; i+8 <= len(sql); i++ {
		if sql[i:i+8] == "COUNT(*)" {
			return true
		}
	}
	return false
}

func TestUserEmailBloomNilSafety(t *testing.T) {
	var b *userEmailBloom
	if !b.MightExist("t1", "a@b.com") {
		t.Fatal("nil bloom must fall back to the DB (return true)")
	}
	b.Add("t1", "a@b.com") // 必须不 panic
	if b.Ready() {
		t.Fatal("nil bloom must not report ready")
	}
	if b.ShardCount() != 0 {
		t.Fatalf("nil bloom shard count = %d, want 0", b.ShardCount())
	}
	if s := b.Stats(); s.Ready || s.Shards != 0 {
		t.Fatalf("nil bloom stats = %+v, want zero value", s)
	}
}

// 预热完成前必须保守：哪怕该租户从未出现，也不能断言"一定不存在"。
func TestUserEmailBloomConservativeBeforeReady(t *testing.T) {
	b := newUserEmailBloom(1024, 0.001)
	if !b.MightExist("t1", "brand-new@b.com") {
		t.Fatal("before warmup, MustExist must return true (always fall back)")
	}
	b.Add("t1", "brand-new@b.com")
	if !b.MightExist("t1", "brand-new@b.com") {
		t.Fatal("still conservative before warmup completes")
	}
	if b.Ready() {
		t.Fatal("a freshly built bloom is not ready")
	}
}

func TestUserEmailBloomAfterReady(t *testing.T) {
	b := newUserEmailBloom(1024, 0.001)
	b.Add("t1", "known@b.com")
	b.ready.Store(true)

	if !b.MightExist("t1", "known@b.com") {
		t.Fatal("a registered email must be reported as possibly-exists")
	}
	if b.MightExist("t1", "unknown@b.com") {
		t.Fatal("an unseen email in a known tenant should be reported absent")
	}
	if b.MightExist("t-never-seen", "anything@b.com") {
		t.Fatal("a tenant with no shard must report absent")
	}
}

// 分片隔离：同一邮箱在不同租户下互不影响（这正是按租户分片的意义）。
func TestUserEmailBloomTenantIsolation(t *testing.T) {
	b := newUserEmailBloom(1024, 0.001)
	b.Add("tenantA", "shared@b.com")
	b.ready.Store(true)

	if b.MightExist("tenantB", "shared@b.com") {
		t.Fatal("an email registered in tenantA must not be reported present for tenantB")
	}
	if !b.MightExist("tenantA", "shared@b.com") {
		t.Fatal("tenantA must still report its own email as present")
	}
	// 空租户/空邮箱是无效输入：一律保守回落。
	if !b.MightExist("", "shared@b.com") {
		t.Fatal("empty tenant must fall back")
	}
	if !b.MightExist("tenantA", "") {
		t.Fatal("empty email must fall back")
	}
}

// 键拼接不能有歧义：("a","bc") 与 ("ab","c") 必须映射到不同位。
func TestEmailBloomKeyNoCollision(t *testing.T) {
	if string(emailBloomKey("a", "bc@x.com")) == string(emailBloomKey("ab", "c@x.com")) {
		t.Fatal("tenant/email key concatenation is ambiguous")
	}
}

func TestParseEmailBloomBroadcast(t *testing.T) {
	cases := []struct {
		name       string
		in         string
		wantTenant string
		wantEmail  string
		wantOK     bool
	}{
		{"normal", "t1\tuser@b.com", "t1", "user@b.com", true},
		{"empty tenant", "\tuser@b.com", "", "", false},
		{"empty email", "t1\t", "", "", false},
		{"no separator", "no-tab-here", "", "", false},
		{"empty payload", "", "", "", false},
		{"extra tabs kept in email", "t1\ta\tb", "t1", "a\tb", true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			tenant, email, ok := parseEmailBloomBroadcast(c.in)
			if ok != c.wantOK || tenant != c.wantTenant || email != c.wantEmail {
				t.Fatalf("parse(%q) = (%q,%q,%v), want (%q,%q,%v)",
					c.in, tenant, email, ok, c.wantTenant, c.wantEmail, c.wantOK)
			}
		})
	}
}

// Redis 未初始化时广播必须是 no-op 而不是 panic。
func TestBroadcastEmailBloomAddWithoutRedis(t *testing.T) {
	broadcastUserEmailBloomAdd("t1", "user@b.com")
	broadcastUserEmailBloomAdd("", "") // 空值也应安全返回
}

func TestUserEmailBloomWarmup(t *testing.T) {
	b := newUserEmailBloom(1024, 0.001)
	q := &fakeBloomQuerier{
		counts: [][2]any{{"t1", int64(2)}, {"t2", int64(1)}},
		scanned: [][3]string{
			{"id1", "t1", "a@b.com"},
			{"id2", "t1", "c@b.com"},
			{"id3", "t2", "d@b.com"},
		},
	}
	b.Warmup(context.Background(), q)

	if !b.Ready() {
		t.Fatal("successful warmup must mark ready")
	}
	if got := b.ShardCount(); got != 2 {
		t.Fatalf("shards = %d, want 2", got)
	}
	if !b.MightExist("t1", "a@b.com") || !b.MightExist("t2", "d@b.com") {
		t.Fatal("warmed emails must be reported present")
	}
	if b.MightExist("t1", "never@b.com") {
		t.Fatal("an email absent from the scan should be reported absent")
	}
}

// 预热失败必须保持保守态（不 ready），否则会带着残缺过滤器开始跳过判重。
func TestUserEmailBloomWarmupScanFailureStaysConservative(t *testing.T) {
	b := newUserEmailBloom(1024, 0.001)
	q := &fakeBloomQuerier{scanErr: errors.New("db unavailable")}
	b.Warmup(context.Background(), q)

	if b.Ready() {
		t.Fatal("failed warmup must NOT mark ready")
	}
	if !b.MightExist("t1", "x@b.com") {
		t.Fatal("must keep falling back to the DB after a failed warmup")
	}
}

func TestUserEmailBloomStatsCounters(t *testing.T) {
	b := newUserEmailBloom(1024, 0.001)
	b.Add("t1", "known@b.com")
	b.ready.Store(true)

	_ = b.MightExist("t1", "known@b.com")   // 回落
	_ = b.MightExist("t1", "unknown@b.com") // 跳过

	s := b.Stats()
	if !s.Ready || s.Shards != 1 {
		t.Fatalf("stats = %+v, want ready with 1 shard", s)
	}
	if s.FellbackDBLookups != 1 {
		t.Fatalf("fellback = %d, want 1", s.FellbackDBLookups)
	}
	if s.SkippedDBLookups != 1 {
		t.Fatalf("skipped = %d, want 1", s.SkippedDBLookups)
	}
}
