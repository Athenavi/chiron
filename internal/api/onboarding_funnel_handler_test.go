package api

import (
	"testing"
)

// TestBuildOnboardingFunnel_KeepsAllStepsInOrder 验证漏斗始终包含全部三步且顺序固定。
//
// 为什么重要：`GROUP BY step` 只会返回**有人走到过**的步骤。若直接把它当作结果，
// 一个刚上线、还没人走完第三步的实例会显示成"两步漏斗"，看板上根本看不出流失点是哪一步。
func TestBuildOnboardingFunnel_KeepsAllStepsInOrder(t *testing.T) {
	// 只给第一步的数据，后两步在表里没有行。
	counts := map[string][3]int{
		OnboardingStepTrialCredit: {3, 1, 0},
	}

	steps, _ := buildOnboardingFunnel(4, 0, counts)

	if len(steps) != len(onboardingSteps) {
		t.Fatalf("expected %d steps, got %d", len(onboardingSteps), len(steps))
	}
	for i, want := range onboardingSteps {
		if steps[i].Step != want {
			t.Errorf("step %d: expected %q, got %q", i, want, steps[i].Step)
		}
	}
	// 缺失的步骤必须补 0，而不是被丢掉。
	if steps[1].Completed != 0 || steps[2].Pending != 0 {
		t.Errorf("missing steps should be zeroed, got %+v", steps[1:])
	}
}

// TestBuildOnboardingFunnel_RatesUseEnteredAsDenominator 验证比率的分母是 entered。
//
// 分母选错是这类统计最常见的错误：若用全站用户数当分母，存量用户会把完成率
// 稀释成一个看起来"引导完全无效"的低值。
func TestBuildOnboardingFunnel_RatesUseEnteredAsDenominator(t *testing.T) {
	counts := map[string][3]int{
		OnboardingStepTrialCredit:  {2, 1, 1},
		OnboardingStepDefaultModel: {1, 0, 3},
	}

	steps, finishedRate := buildOnboardingFunnel(4, 1, counts)

	if steps[0].CompletionRate != 0.5 {
		t.Errorf("step0 completion_rate: expected 0.5, got %v", steps[0].CompletionRate)
	}
	if steps[0].SkipRate != 0.25 {
		t.Errorf("step0 skip_rate: expected 0.25, got %v", steps[0].SkipRate)
	}
	if steps[1].CompletionRate != 0.25 {
		t.Errorf("step1 completion_rate: expected 0.25, got %v", steps[1].CompletionRate)
	}
	if finishedRate != 0.25 {
		t.Errorf("finished_rate: expected 0.25, got %v", finishedRate)
	}
}

// TestBuildOnboardingFunnel_ZeroEnteredYieldsNoNaN 验证空表时比率是 0 而不是 NaN。
//
// 0/0 在 Go 里是 NaN，JSON 无法表示 NaN —— 编码会失败，整个接口 500。
// 因此空库必须是"全 0 的一份正常响应"，而不是一个错误。
func TestBuildOnboardingFunnel_ZeroEnteredYieldsNoNaN(t *testing.T) {
	steps, finishedRate := buildOnboardingFunnel(0, 0, nil)

	if finishedRate != 0 {
		t.Errorf("finished_rate: expected 0 for empty table, got %v", finishedRate)
	}
	for _, s := range steps {
		if s.CompletionRate != 0 || s.SkipRate != 0 {
			t.Errorf("step %q: expected zero rates, got %+v", s.Step, s)
		}
	}
	if len(steps) != len(onboardingSteps) {
		t.Fatalf("expected %d steps for empty table, got %d", len(onboardingSteps), len(steps))
	}
}

// TestBuildOnboardingFunnel_CountsAreMutuallyExclusive 验证三态计数来自不同的行。
//
// 三个计数必须在 SQL 里互斥（completed_at 优先），否则同一行会被算进两个桶，
// 逐步计数之和超过分母，完成率总和 > 100%。
func TestBuildOnboardingFunnel_CountsAreMutuallyExclusive(t *testing.T) {
	// 4 个用户：2 完成、1 跳过、1 待办 —— 相加正好等于分母。
	counts := map[string][3]int{
		OnboardingStepTrialCredit: {2, 1, 1},
	}

	steps, _ := buildOnboardingFunnel(4, 0, counts)
	s := steps[0]

	if sum := s.Completed + s.Skipped + s.Pending; sum != 4 {
		t.Errorf("counts must sum to entered (4), got %d", sum)
	}
	// 三个计数互斥 ⇒ 三个比率相加必须正好是 1.0。待办率不单独成字段，
	// 但它对应的 Pending/entered 必须被算进来，否则说明某一类被重复计数或被漏掉。
	pendingRate := float64(s.Pending) / 4
	if got := s.CompletionRate + s.SkipRate + pendingRate; got != 1.0 {
		t.Errorf("completion + skip + pending rates must be 1.0, got %v", got)
	}
}
