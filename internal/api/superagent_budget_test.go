package api

import (
	"context"
	"testing"
)

// 预算硬闸门的判据单测。
//
// 这个闸门此前**只读不写**：`token_spent` 永远是 0，于是任何 `token_cap` 都不会触发，
// 面板上的"已花 x / 上限 y"永远停在 0 —— 对用户是"承诺了会停，实际永不生效"。
// 现在累加与判定都落在 recordTakeoverUsage 上，而判定被提成纯函数，
// 就是为了让"0 到底是不限还是上限 0"这件事有唯一的、可测的答案。
func TestBudgetExhausted(t *testing.T) {
	cases := []struct {
		name  string
		spent int64
		cap   int64
		want  bool
	}{
		// cap == 0 是**不限**，不是"上限 0"。写反了会让每一个沿用默认配置
		// （token_cap 缺省 0）的接管在第一拍结束时就停 —— 与"不改配置等于今天的行为"冲突。
		{"cap 0 means unlimited", 1_000_000, 0, false},
		{"under cap keeps running", 999, 1000, false},
		// 正好花到上限也算用尽：用户写的就是"最多这么多"。
		{"exactly at cap stops", 1000, 1000, true},
		// 一拍越过去是常态（用量在回合结束后才已知），必须照样停。
		{"over cap stops", 1001, 1000, true},
		{"zero spent under cap", 0, 1000, false},
	}
	for _, c := range cases {
		if got := budgetExhausted(c.spent, c.cap); got != c.want {
			t.Errorf("%s: budgetExhausted(%d, %d) = %v, want %v", c.name, c.spent, c.cap, got, c.want)
		}
	}
}

// 用量收集器要跨**续跑轮**累加，并对"没有挂收集器的普通提交"保持零副作用。
func TestTakeoverUsageSinkAccumulatesAcrossRounds(t *testing.T) {
	ctx := withTakeoverUsageSink(context.Background(), &takeoverUsageSink{})
	sink := takeoverUsageFromCtx(ctx)
	if sink == nil {
		t.Fatal("挂上去的收集器取不回来")
	}

	// 目标续跑会把同一个 HandleSubmit 跑成多轮，每轮各加一次。
	sink.Add(120)
	sink.Add(80)
	sink.Add(0)   // 无用量的一轮（例如引擎不可达）：不计
	sink.Add(-5)  // 防御性：负值不进累计
	if got := sink.Total(); got != 200 {
		t.Fatalf("累计应为 200，得到 %d", got)
	}
}

// 普通提交（非接管）的 ctx 上不该有收集器；nil 收集器的方法也必须可调用，
// 否则每一个非接管回合都会在收尾处 panic。
func TestTakeoverUsageSinkAbsentIsSafe(t *testing.T) {
	if s := takeoverUsageFromCtx(context.Background()); s != nil {
		t.Fatalf("普通提交不应带回收集器，得到 %+v", s)
	}
	var s *takeoverUsageSink
	s.Add(100) // 不得 panic
	if s.Total() != 0 {
		t.Fatalf("nil 收集器总用量应为 0")
	}
	// WithQuotaReserve 的同类约定：0 值不注入无用键。
	ctx := withTakeoverUsageSink(context.Background(), nil)
	if ctx.Value(takeoverUsageKey{}) != nil {
		t.Fatal("nil 收集器不应写进上下文")
	}
}
