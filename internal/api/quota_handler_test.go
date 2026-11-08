package api

import (
	"strings"
	"testing"
	"time"
)

// quota_handler_test.go —— 配额管理的纯函数契约。
//
// 这里只测不依赖数据库的部分：资源白名单、周期边界（必须与执行侧逐字一致）、
// 金额校验、SQL 口径与用量来源标签。数据库路径（upsert 的表达式唯一索引、
// 状态推进）由迁移与端到端复测覆盖 —— 与 invoice_export_test.go 同款分层。

// TestNormalizeQuotaResource_AllowsOnlyExecutedTypes 验证只接受执行侧真正会读的四类。
//
// `ent_quota_pools.resource_type` 是 varchar(20) 且**没有 CHECK 约束**，放任任意
// 字符串会让执行侧永远匹配不到 —— 管理员以为配了限额，实际完全不生效。
func TestNormalizeQuotaResource_AllowsOnlyExecutedTypes(t *testing.T) {
	for _, in := range []string{"token", "credits", "concurrency", "storage_mb"} {
		got, err := normalizeQuotaResource(in)
		if err != nil {
			t.Errorf("%q should be allowed, got error %v", in, err)
		}
		if got != in {
			t.Errorf("normalizeQuotaResource(%q) = %q, want %q", in, got, in)
		}
	}

	// 大小写与空白应被归一化，而不是被判非法。
	if got, err := normalizeQuotaResource("  TOKEN  "); err != nil || got != "token" {
		t.Errorf("whitespace/case should normalise, got (%q, %v)", got, err)
	}

	for _, in := range []string{"", "storage", "tokens", "models", "unknown"} {
		if _, err := normalizeQuotaResource(in); err == nil {
			t.Errorf("%q must be rejected (执行侧不会读它)", in)
		}
	}
}

// TestNormalizeQuotaPeriod_DefaultsToMonthly 验证周期默认与白名单。
//
// 执行侧 quotaPeriodKey 只把 "daily" 当特例，其余全按月度处理。
// 若允许写进 "weekly" 这类值，周期起点会静默按月度算 ——
// 管理员看到一个自己没选过的周期，且用量数字对不上。
func TestNormalizeQuotaPeriod_DefaultsToMonthly(t *testing.T) {
	for _, tc := range []struct{ in, want string }{
		{"", quotaPeriodMonthly},
		{"daily", quotaPeriodDaily},
		{"monthly", quotaPeriodMonthly},
		{" DAILY ", quotaPeriodDaily},
	} {
		got, err := normalizeQuotaPeriod(tc.in)
		if err != nil {
			t.Errorf("normalizeQuotaPeriod(%q) returned error: %v", tc.in, err)
			continue
		}
		if got != tc.want {
			t.Errorf("normalizeQuotaPeriod(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}

	for _, in := range []string{"weekly", "yearly", "hourly"} {
		if _, err := normalizeQuotaPeriod(in); err == nil {
			t.Errorf("%q must be rejected (执行侧只认 daily/monthly)", in)
		}
	}
}

// TestNormalizeQuotaStatusFilter_DefaultsToPending 验证审批队列默认待审。
//
// 审核队列的第一屏必须是"等我处理的事"；默认给全量会让待审条目被历史冲散。
func TestNormalizeQuotaStatusFilter_DefaultsToPending(t *testing.T) {
	got, err := normalizeQuotaStatusFilter("")
	if err != nil || got != quotaRequestPending {
		t.Fatalf("empty filter should default to pending, got (%q, %v)", got, err)
	}
	// "all" 表示不过滤（空串），与默认待审必须区分开。
	got, err = normalizeQuotaStatusFilter("all")
	if err != nil || got != "" {
		t.Fatalf(`"all" should mean no filter, got (%q, %v)`, got, err)
	}
	if _, err := normalizeQuotaStatusFilter("weird"); err == nil {
		t.Fatal("unknown status must be rejected")
	}
}

// TestValidateQuotaAmount_RejectsNegative 验证负数被拒绝。
//
// 负数在执行侧会被 `TotalAmount > 0` 判为"不限额"—— 管理员以为自己设了个
// 极小值，实际是完全放开，这是最危险的方向。0 则是合法的"取消限制"。
func TestValidateQuotaAmount_RejectsNegative(t *testing.T) {
	if err := validateQuotaAmount(0); err != nil {
		t.Errorf("0 means unlimited and must be allowed, got %v", err)
	}
	if err := validateQuotaAmount(1_000_000); err != nil {
		t.Errorf("positive amount must be allowed, got %v", err)
	}
	if err := validateQuotaAmount(-1); err == nil {
		t.Error("negative must be rejected (执行侧会当作不限额)")
	}
	if err := validateQuotaAmount(maxQuotaAmount + 1); err == nil {
		t.Error("amount above cap must be rejected (超出 JS 安全整数范围)")
	}
}

// TestQuotaPeriodStart_MatchesEnforceSide 验证周期起点与执行侧逐字一致。
//
// 这是面板与强制侧最容易漂移的地方：如果面板按本地时区算、强制按 UTC 算，
// 每月 1 号凌晨会出现"面板清零了但请求仍被拦"的窗口。
func TestQuotaPeriodStart_MatchesEnforceSide(t *testing.T) {
	shanghai := time.FixedZone("CST", 8*3600)
	// 上海 2026-11-01 03:00 == UTC 2026-10-31 19:00 ⇒ 月度起点应落在 **10 月**。
	now := time.Date(2026, 11, 1, 3, 0, 0, 0, shanghai)

	monthStart := quotaPeriodStart(quotaPeriodMonthly, now)
	want := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	if !monthStart.Equal(want) {
		t.Errorf("monthly start = %s, want %s (必须按 UTC 算)", monthStart, want)
	}

	// 日度起点同理：UTC 的当天零点。
	dayStart := quotaPeriodStart(quotaPeriodDaily, now)
	wantDay := time.Date(2026, 10, 31, 0, 0, 0, 0, time.UTC)
	if !dayStart.Equal(wantDay) {
		t.Errorf("daily start = %s, want %s", dayStart, wantDay)
	}
}

// TestQuotaUsageSQL_SourcesMatchEnforcement 验证用量来源与强制侧同源。
//
// token/credits 超限时网关是真的在拦人，所以面板必须读**同一张表**：
// 否则会出现"页面显示 40%、请求却被拒"这种无法解释的状态。
func TestQuotaUsageSQL_SourcesMatchEnforcement(t *testing.T) {
	tokenSQL, tokenBytes := quotaUsageSQL(quotaResourceToken)
	if tokenSQL == "" || tokenBytes {
		t.Fatal("token usage must come from usage_events and not be byte-based")
	}
	if !strings.Contains(tokenSQL, "usage_events") {
		t.Error("token usage must read usage_events (与强制侧计数器同源)")
	}

	creditSQL, creditBytes := quotaUsageSQL(quotaResourceCredits)
	if creditSQL == "" || creditBytes {
		t.Fatal("credits usage must not be byte-based")
	}
	// credits 的权威口径是 credit_transactions 的扣减量（与 CreditUsageSQL 相同）。
	if !strings.Contains(creditSQL, "credit_transactions") || !strings.Contains(creditSQL, "amount < 0") {
		t.Error("credits usage must sum negative credit_transactions amounts")
	}

	storageSQL, isBytes := quotaUsageSQL(quotaResourceStorageMB)
	if storageSQL == "" || !isBytes {
		t.Fatal("storage usage must be byte-based (表里存字节，配额以 MB 计)")
	}
	if !strings.Contains(storageSQL, "media_assets") {
		t.Error("storage usage must read media_assets (与 storageUsageQuery 同源)")
	}

	// 并发是"上限"而不是累计配额，没有已用量可算。
	if sql, _ := quotaUsageSQL(quotaResourceConcurrency); sql != "" {
		t.Error("concurrency has no usage; must return empty query")
	}
}

// TestQuotaUsageSource_LabelsAreStable 验证排障标签。
//
// "为什么显示 0"必须能一眼看出是没有数据还是查询失败 ——
// 后者返回 none，前者返回具体的表名。
func TestQuotaUsageSource_LabelsAreStable(t *testing.T) {
	cases := map[string]string{
		quotaResourceToken:       "usage_events",
		quotaResourceCredits:     "credit_transactions",
		quotaResourceStorageMB:   "media_assets",
		quotaResourceConcurrency: "none",
	}
	for resource, want := range cases {
		if got := quotaUsageSource(resource); got != want {
			t.Errorf("quotaUsageSource(%q) = %q, want %q", resource, got, want)
		}
	}
}
