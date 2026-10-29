package api

import (
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/billing"
)

// ── 引导步骤白名单与状态折叠（P0-3，纯逻辑，不需要 DB）──────────────
//
// 这几个函数是引导接口的全部判断逻辑；handler 本身只做"读库 → 调它们 → 写回"。
// 本仓没有浏览器 e2e，所以"该不该弹、哪一步算完成"必须在这里被钉住。

func TestIsOnboardingStep_AcceptsOnlyWhitelist(t *testing.T) {
	for _, step := range []string{
		OnboardingStepTrialCredit,
		OnboardingStepDefaultModel,
		OnboardingStepFirstMessage,
	} {
		if !isOnboardingStep(step) {
			t.Fatalf("%q 是合法步骤，却被拒了", step)
		}
	}
	// 任意字符串必须被拒：否则表里会混进噪音，按 step 聚合完成率就不可信了
	for _, step := range []string{"", "unknown", "trial", "TRIAL_CREDIT", "first-message"} {
		if isOnboardingStep(step) {
			t.Fatalf("%q 不该被当成合法步骤", step)
		}
	}
}

func TestOnboardingStatus_CompletedWinsOverSkipped(t *testing.T) {
	now := time.Now()
	earlier := now.Add(-time.Hour)

	if got := onboardingStatus(nil, nil); got != "pending" {
		t.Fatalf("两个时间戳都为空应为 pending，得到 %q", got)
	}
	if got := onboardingStatus(&now, nil); got != "completed" {
		t.Fatalf("有完成时间应为 completed，得到 %q", got)
	}
	if got := onboardingStatus(nil, &now); got != "skipped" {
		t.Fatalf("只有跳过时间应为 skipped，得到 %q", got)
	}
	// 先跳过、后来真的做了：算完成。把跳过算成完成会让完成率虚高，
	// 但反过来把"做过"算成跳过同样是错的方向。
	if got := onboardingStatus(&now, &earlier); got != "completed" {
		t.Fatalf("既跳过又完成时应判为 completed，得到 %q", got)
	}
}

func TestBuildPayload_FillsEveryStepInOrder(t *testing.T) {
	h := &OnboardingHandler{}
	// 只有第一步有行：其余两步必须被补成 pending，否则前端会少渲染步骤
	payload := h.buildPayload("u1", map[string]OnboardingStepState{
		OnboardingStepTrialCredit: {Step: OnboardingStepTrialCredit, Status: "completed"},
	})

	steps, ok := payload["steps"].([]OnboardingStepState)
	if !ok {
		t.Fatalf("steps 类型不对：%T", payload["steps"])
	}
	if len(steps) != len(onboardingSteps) {
		t.Fatalf("steps 应补齐为 %d 步，得到 %d", len(onboardingSteps), len(steps))
	}
	for i, name := range onboardingSteps {
		if steps[i].Step != name {
			t.Fatalf("第 %d 步应为 %q，得到 %q（顺序即展示顺序）", i, name, steps[i].Step)
		}
	}
	if steps[1].Status != "pending" || steps[2].Status != "pending" {
		t.Fatalf("缺失的步骤应补成 pending，得到 %q / %q", steps[1].Status, steps[2].Status)
	}
	if payload["finished"] != false {
		t.Fatal("还有 pending 步骤时 finished 必须为 false")
	}
}

func TestBuildPayload_EligibleOnlyWhenUserHasRows(t *testing.T) {
	h := &OnboardingHandler{}

	// 没有行 = 存量用户：不弹引导（roadmap：不打断已有用户）
	if got := h.buildPayload("u1", map[string]OnboardingStepState{}); got["eligible"] != false {
		t.Fatalf("没有行的用户 eligible 应为 false，得到 %v", got["eligible"])
	}

	// 有行 = 走过新注册流程
	states := map[string]OnboardingStepState{}
	for _, s := range onboardingSteps {
		states[s] = OnboardingStepState{Step: s, Status: "completed"}
	}
	got := h.buildPayload("u1", states)
	if got["eligible"] != true {
		t.Fatalf("有行的用户 eligible 应为 true，得到 %v", got["eligible"])
	}
	if got["finished"] != true {
		t.Fatalf("全部非 pending 时 finished 应为 true，得到 %v", got["finished"])
	}
}

func TestBuildPayload_TrialExposesDailyFreeLimit(t *testing.T) {
	h := &OnboardingHandler{}
	payload := h.buildPayload("u1", map[string]OnboardingStepState{})

	trial, ok := payload["trial"].(map[string]interface{})
	if !ok {
		t.Fatalf("trial 类型不对：%T", payload["trial"])
	}
	// 第一步是「展示试用额度」而不是「再发钱」：必须如实告诉用户他每天有几次免费。
	if trial["daily_free_limit"] != billing.DailyFreeLimit {
		t.Fatalf("daily_free_limit = %v, want %d", trial["daily_free_limit"], billing.DailyFreeLimit)
	}
	// 不在发放额度 —— 这个字段是"已经有了"，别让前端误以为可以再领一次
	if trial["already_granted"] != true {
		t.Fatalf("already_granted 应为 true，得到 %v", trial["already_granted"])
	}
	// balance 为 nil 时不该出现 credits 键（读不到就不编一个 0 出来）
	if _, has := trial["credits"]; has {
		t.Fatal("没有余额来源时不该返回 credits")
	}
}
