package api

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/redis/go-redis/v9"
)

// ── 测试替身 ──────────────────────────────────────────────────────

// fakeQuotaRedis 只实现 quotaRedis 的四个方法，按脚本语义模拟 INCRBY/EXPIREAT。
// 不连真 Redis：配额子系统的正确性关注点是"写进计数器的**数值**"，不是 Redis 行为。
type fakeQuotaRedis struct {
	values   map[string]int64
	seeded   map[string]bool
	failEval bool
}

func newFakeQuotaRedis() *fakeQuotaRedis {
	return &fakeQuotaRedis{values: map[string]int64{}, seeded: map[string]bool{}}
}

func (f *fakeQuotaRedis) Exists(_ context.Context, keys ...string) *redis.IntCmd {
	var n int64
	for _, k := range keys {
		if f.seeded[k] {
			n++
		}
	}
	return redis.NewIntResult(n, nil)
}

func (f *fakeQuotaRedis) Get(_ context.Context, key string) *redis.StringCmd {
	if !f.seeded[key] {
		return redis.NewStringResult("", redis.Nil)
	}
	return redis.NewStringResult(strconv.FormatInt(f.values[key], 10), nil)
}

func (f *fakeQuotaRedis) Set(_ context.Context, key string, value interface{}, _ time.Duration) *redis.StatusCmd {
	n, err := strconv.ParseInt(fmt.Sprint(value), 10, 64)
	if err != nil {
		return redis.NewStatusResult("", err)
	}
	f.values[key], f.seeded[key] = n, true
	return redis.NewStatusResult("OK", nil)
}

func (f *fakeQuotaRedis) Eval(_ context.Context, script string, keys []string, args ...interface{}) *redis.Cmd {
	cmd := redis.NewCmd(context.Background())
	if f.failEval {
		cmd.SetErr(errors.New("redis unavailable"))
		return cmd
	}
	delta, err := strconv.ParseInt(fmt.Sprint(args[0]), 10, 64)
	if err != nil {
		cmd.SetErr(err)
		return cmd
	}
	v := f.values[keys[0]] + delta
	// 负增量脚本带下界夹取（quotaDeltaScript），正增量脚本没有 —— 用脚本内容区分。
	if v < 0 && strings.Contains(script, "if v < 0") {
		v = 0
	}
	f.values[keys[0]], f.seeded[keys[0]] = v, true
	cmd.SetVal(v)
	return cmd
}

type fakeQuotaStore struct {
	tenantID string
	pools    []EntQuotaPool
	sqlUsage int64
}

func (s fakeQuotaStore) ResolveTenantID(_ context.Context, _ string) (string, error) {
	return s.tenantID, nil
}

func (s fakeQuotaStore) TenantTokenPools(_ context.Context, tenantID string) ([]EntQuotaPool, error) {
	if tenantID != s.tenantID {
		return nil, nil
	}
	return s.pools, nil
}

func (s fakeQuotaStore) TokenUsageSQL(_ context.Context, _ string, _ time.Time) (int64, error) {
	return s.sqlUsage, nil
}

func newTestEnforcer(redisClient quotaRedis, store quotaStore) *tenantQuotaEnforcer {
	frozen := time.Date(2026, 3, 5, 12, 0, 0, 0, time.UTC)
	return &tenantQuotaEnforcer{store: store, redis: redisClient, now: func() time.Time { return frozen }}
}

// ── 估算 ──────────────────────────────────────────────────────────

func TestEstimateRequestTokensCountsCJKAsOneTokenPerChar(t *testing.T) {
	// 中文按 1 token/字；纯按字符数除 4 会低估 4 倍（本仓库的主要输入是中文）
	cjk := EstimateRequestTokens("你好世界你好世界", nil)
	if cjk != 8 {
		t.Fatalf("cjk estimate = %d, want 8", cjk)
	}
	latin := EstimateRequestTokens(strings.Repeat("a", 40), nil)
	if latin != 10 {
		t.Fatalf("latin estimate = %d, want 10", latin)
	}
	// 空正文 + 附件：正文仍按最小 1 计（保持计数器热度），附件各加一份占位
	if got := EstimateRequestTokens("", []map[string]interface{}{{"id": "1"}}); got != quotaAttachmentEstimateTokens+1 {
		t.Fatalf("attachment-only estimate = %d, want %d", got, quotaAttachmentEstimateTokens+1)
	}
}

// ── 预检预留 ───────────────────────────────────────────────────────

func TestEnforceReservesEstimatedTokensNotOnePerRequest(t *testing.T) {
	rdb := newFakeQuotaRedis()
	e := newTestEnforcer(rdb, fakeQuotaStore{
		tenantID: "t1",
		pools:    []EntQuotaPool{{TenantID: "t1", ResourceType: "token", TotalAmount: 1000, Period: "monthly"}},
	})
	claims := &auth.Claims{UserID: "u1", TenantID: "t1"}

	reserved, err := e.enforce(context.Background(), claims, 500)
	if err != nil || reserved != 500 {
		t.Fatalf("enforce = (%d, %v), want (500, nil)", reserved, err)
	}
	key := quotaMonthlyKeyForTest(t, "t1")
	if got := rdb.values[key]; got != 500 {
		t.Fatalf("counter = %d, want 500（此前每请求只 +1）", got)
	}

	// 第二次 600 ⇒ 1100 > 1000：拒绝，且不返回预留（请求不会起回合，无需校正）
	reserved, err = e.enforce(context.Background(), claims, 600)
	if !errors.Is(err, ErrTenantQuotaExceeded) {
		t.Fatalf("err = %v, want ErrTenantQuotaExceeded", err)
	}
	if reserved != 0 {
		t.Fatalf("reserved = %d, want 0 on rejection", reserved)
	}
}

func TestEnforceFailOpenKeepsReservationAtZero(t *testing.T) {
	rdb := newFakeQuotaRedis()
	rdb.failEval = true
	e := newTestEnforcer(rdb, fakeQuotaStore{
		tenantID: "t1",
		pools:    []EntQuotaPool{{TenantID: "t1", ResourceType: "token", TotalAmount: 1000, Period: "monthly"}},
	})
	reserved, err := e.enforce(context.Background(), &auth.Claims{UserID: "u1", TenantID: "t1"}, 300)
	if err != nil || reserved != 0 {
		t.Fatalf("enforce = (%d, %v), want (0, nil) — Redis 抖动必须 fail-open 且不谎报预留", reserved, err)
	}
}

func TestEnforceCountsUnlimitedPools(t *testing.T) {
	rdb := newFakeQuotaRedis()
	e := newTestEnforcer(rdb, fakeQuotaStore{
		tenantID: "t1",
		pools:    []EntQuotaPool{{TenantID: "t1", ResourceType: "token", TotalAmount: 0, Period: "monthly"}},
	})
	// TotalAmount=0 是"不限额"，但**仍然要计数**（面板/告警靠它）；此前直接 continue，
	// 与代码里"仅计数不拦截"的注释相反。
	reserved, err := e.enforce(context.Background(), &auth.Claims{UserID: "u1", TenantID: "t1"}, 400)
	if err != nil || reserved != 400 {
		t.Fatalf("enforce = (%d, %v), want (400, nil)", reserved, err)
	}
	if got := rdb.values[quotaMonthlyKeyForTest(t, "t1")]; got != 400 {
		t.Fatalf("counter = %d, want 400", got)
	}
}

// ── 回合末校正 ─────────────────────────────────────────────────────

func TestTrueUpReplacesEstimateWithRealUsage(t *testing.T) {
	rdb := newFakeQuotaRedis()
	e := newTestEnforcer(rdb, fakeQuotaStore{
		tenantID: "t1",
		pools:    []EntQuotaPool{{TenantID: "t1", ResourceType: "token", TotalAmount: 1000, Period: "monthly"}},
	})
	key := quotaMonthlyKeyForTest(t, "t1")

	if _, err := e.enforce(context.Background(), &auth.Claims{UserID: "u1", TenantID: "t1"}, 500); err != nil {
		t.Fatalf("enforce: %v", err)
	}
	// 真实用量 1200、预留 500 ⇒ 补 700，池子里最终等于真实值（与 billing_records 同口径）
	e.trueUp(context.Background(), "u1", "t1", 1200-500)
	if got := rdb.values[key]; got != 1200 {
		t.Fatalf("counter = %d, want 1200", got)
	}
}

func TestTrueUpRefundClampsAtZero(t *testing.T) {
	rdb := newFakeQuotaRedis()
	e := newTestEnforcer(rdb, fakeQuotaStore{
		tenantID: "t1",
		pools:    []EntQuotaPool{{TenantID: "t1", ResourceType: "token", TotalAmount: 1000, Period: "monthly"}},
	})
	key := quotaMonthlyKeyForTest(t, "t1")

	// 预留 500 后请求被闸门挡下（一 token 未烧）⇒ 全额退还
	if _, err := e.enforce(context.Background(), &auth.Claims{UserID: "u1", TenantID: "t1"}, 500); err != nil {
		t.Fatalf("enforce: %v", err)
	}
	e.trueUp(context.Background(), "u1", "t1", -500)
	if got := rdb.values[key]; got != 0 {
		t.Fatalf("counter = %d, want 0", got)
	}

	// 退还超过已计入的量（回填偏低时可能发生）不能把计数器压成负数 ——
	// 负值会让"用量 > 上限"永不成立，配额彻底失效。
	e.trueUp(context.Background(), "u1", "t1", -500)
	if got := rdb.values[key]; got != 0 {
		t.Fatalf("counter = %d, want 0（负增量必须夹到下界）", got)
	}
}

func TestTrueUpResolvesTenantWhenEmpty(t *testing.T) {
	rdb := newFakeQuotaRedis()
	e := newTestEnforcer(rdb, fakeQuotaStore{
		tenantID: "t1",
		pools:    []EntQuotaPool{{TenantID: "t1", ResourceType: "token", TotalAmount: 1000, Period: "monthly"}},
	})
	// 子 Agent 自动轮没有用户 JWT ⇒ claims 里没有 tenantID，必须经 users 表解析后照样入账
	e.trueUp(context.Background(), "u1", "", 700)
	if got := rdb.values[quotaMonthlyKeyForTest(t, "t1")]; got != 700 {
		t.Fatalf("counter = %d, want 700", got)
	}
}

// ── 预留值的上下文传递 ─────────────────────────────────────────────

func TestQuotaReserveContextRoundTrip(t *testing.T) {
	ctx := context.Background()
	if got := QuotaReserveFromContext(ctx); got != 0 {
		t.Fatalf("empty ctx = %d, want 0", got)
	}
	ctx = WithQuotaReserve(ctx, 500)
	if got := QuotaReserveFromContext(ctx); got != 500 {
		t.Fatalf("reserved = %d, want 500", got)
	}
	// 多轮目标续跑：预留只能扣一次，第二轮起必须清零（否则凭空多退一次预估）
	ctx = ConsumeQuotaReserve(ctx)
	if got := QuotaReserveFromContext(ctx); got != 0 {
		t.Fatalf("after consume = %d, want 0", got)
	}
	if same := ConsumeQuotaReserve(context.Background()); same != context.Background() {
		t.Fatal("no reservation should not wrap the context")
	}
}

// quotaMonthlyKeyForTest 取当前测试时钟下的月度计数器键。
func quotaMonthlyKeyForTest(t *testing.T, tenantID string) string {
	t.Helper()
	now := time.Date(2026, 3, 5, 12, 0, 0, 0, time.UTC)
	key, _, _ := quotaPeriodKey(tenantID, "monthly", now)
	return key
}
