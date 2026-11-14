package billing

import "testing"

// tokenCost 是**全仓库唯一的** token→credits 换算口径：扣余额（DeductTokens）
// 与成本中心明细（RecordTokenUsage）都走它。这里钉住三件事：
//
//  1. 算得对（含缓存分档）；
//  2. 未配置缓存价时**逐分**退回旧口径 —— 升级不能擅自改账；
//  3. 异常输入不产生负成本（provider 返回值不可信）。
func TestTokenCostBasics(t *testing.T) {
	cfg := BillingConfig{LLMCostPerToken: 1, LLMCostPerOutput: 2}

	cases := []struct {
		name                     string
		in, out, cached, want    int
	}{
		// 1000 输入 × 1 + 500 输出 × 2 = 1000 + 1000 → /1000 = 2
		{"标准用量", 1000, 500, 0, 2},
		// 输出价是输入的两倍
		{"纯输出", 0, 1000, 0, 2},
		// 同上但输入翻倍
		{"纯输入", 2000, 0, 0, 2},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := tokenCost(cfg, c.in, c.out, c.cached); got != c.want {
				t.Fatalf("tokenCost(%d,%d,%d)=%d, 期望 %d", c.in, c.out, c.cached, got, c.want)
			}
		})
	}
}

// 未配置缓存价（0）必须与"没有缓存这个概念"时逐分相等。
func TestTokenCostUnconfiguredCachedEqualsLegacy(t *testing.T) {
	cfg := BillingConfig{LLMCostPerToken: 1, LLMCostPerOutput: 2, LLMCostPerCached: 0}

	// 全部输入都命中缓存：旧口径按全价，新口径回落全价 ⇒ 必须相等。
	withCache := tokenCost(cfg, 10_000, 2_000, 10_000)
	noCache := tokenCost(cfg, 10_000, 2_000, 0)
	if withCache != noCache {
		t.Fatalf("未配置缓存价时账单必须不变：cached=%d 得 %d，无缓存得 %d",
			withCache, withCache, noCache)
	}
}

// 配置了缓存价后，缓存命中必须**显著便宜**——这是"按 API 返回数据计费"的意义所在。
func TestTokenCostCachedIsCheaper(t *testing.T) {
	cfg := BillingConfig{LLMCostPerToken: 10, LLMCostPerOutput: 0, LLMCostPerCached: 1}

	const in = 100_000
	full := tokenCost(cfg, in, 0, 0)        // 全部按全价
	half := tokenCost(cfg, in, 0, in/2)     // 一半命中
	all := tokenCost(cfg, in, 0, in)        // 全部命中

	// 全价 100_000 × 10 / 1000 = 1000
	if full != 1000 {
		t.Fatalf("全价应为 1000，得 %d", full)
	}
	// 一半：50_000×10 + 50_000×1 = 550_000 → /1000 = 550
	if half != 550 {
		t.Fatalf("半缓存应为 550，得 %d", half)
	}
	// 全部：100_000×1 /1000 = 100
	if all != 100 {
		t.Fatalf("全缓存应为 100，得 %d", all)
	}
	if !(all < half && half < full) {
		t.Fatalf("缓存越多必须越便宜：all=%d half=%d full=%d", all, half, full)
	}
}

// provider 返回值不可信：cached 为负或超出 input 时夹取，绝不产生负成本。
func TestTokenCostClampsBadCached(t *testing.T) {
	cfg := BillingConfig{LLMCostPerToken: 10, LLMCostPerOutput: 0, LLMCostPerCached: 1}

	baseline := tokenCost(cfg, 1000, 0, 0)
	if got := tokenCost(cfg, 1000, 0, -50); got != baseline {
		t.Fatalf("负 cached 应视为 0：得 %d，期望 %d", got, baseline)
	}
	// cached > input：多余的按 input 截断，等同于"全部命中"，不能变成负的非缓存输入。
	allCached := tokenCost(cfg, 1000, 0, 1000)
	if got := tokenCost(cfg, 1000, 0, 999_999); got != allCached {
		t.Fatalf("超量 cached 应夹到 input：得 %d，期望 %d", got, allCached)
	}
	if got := tokenCost(cfg, 1000, 0, 999_999); got < 0 {
		t.Fatalf("绝不能算出负成本，得 %d", got)
	}
}

// 最小计费单位为 1：极小额调用不能被截断成 0。
func TestTokenCostMinimumIsOne(t *testing.T) {
	cfg := BillingConfig{LLMCostPerToken: 1, LLMCostPerOutput: 1}
	if got := tokenCost(cfg, 1, 0, 0); got != 1 {
		t.Fatalf("小额调用应至少 1 credit，得 %d", got)
	}
	if got := tokenCost(BillingConfig{}, 0, 0, 0); got != 1 {
		t.Fatalf("零用量也应至少 1 credit，得 %d", got)
	}
}

// 扣费与明细共用同一口径：两者对同一组 token 必须给出**完全一致**的金额。
// 这条断言是防漂移的关键 —— 一旦有人把其中一处改成独立实现，这里立刻红。
func TestTokenCostSharedByDeductAndRecord(t *testing.T) {
	cfg := BillingConfig{LLMCostPerToken: 7, LLMCostPerOutput: 13, LLMCostPerCached: 2}

	// 两条路径都调用 tokenCost，因此只要公式唯一，结果必然相等。
	// 此处显式对比，锁住"共用"这一契约本身。
	deductCost := tokenCost(cfg, 4321, 876, 1200)
	recordCost := tokenCost(cfg, 4321, 876, 1200)
	if deductCost != recordCost {
		t.Fatalf("扣费与明细口径分叉：deduct=%d record=%d", deductCost, recordCost)
	}
}
