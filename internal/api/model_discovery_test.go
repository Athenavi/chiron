package api

import "testing"

// TestInferContextWindow 锁死 P1-f 的行为：
// 模型发现写回 llm_models 时必须给出**可用**的上下文窗口（此前写死 0，
// 导致前端上下文环分母恒为 0、环永远不显示），且推断不出时宁可返回 0。
func TestInferContextWindow(t *testing.T) {
	cases := []struct {
		model string
		want  int
	}{
		// 名字里显式声明窗口 —— 优先级最高
		{"claude-sonnet-4-128k", 128000},
		{"qwen3-32k", 32000},
		{"some-model-1m", 1000000},
		{"openai/gpt-4o_16k", 16000},
		// 已知家族（名字未声明时的保守估计）
		{"claude-sonnet-4-20250514", 200000},
		{"gpt-4o", 128000},
		{"gpt-4.1-mini", 1000000},
		{"gemini-2.5-pro", 1000000},
		{"deepseek-chat", 128000},
		{"kimi-k2-0905-preview", 128000},
		{"glm-4.6", 128000},
		{"qwen-max", 131072},
		{"llama-3.3-70b", 131072},
		{"grok-4", 131072},
		// 无法判断：返回 0，由前端决定不显示上下文环（不给误导性的分母）
		{"opencode-zen-unknown", 0},
		{"", 0},
	}
	for _, c := range cases {
		if got := inferContextWindow(c.model); got != c.want {
			t.Errorf("inferContextWindow(%q) = %d, want %d", c.model, got, c.want)
		}
	}
}

// TestPlanModelSyncPreservesUserState 锁死 2026-10-09 的修复（用户报
// 「/models 刷新后全部变回启用」）：
//
// 旧实现是「DELETE 该 provider 全部行 → 以 `enabled = true` 重新 INSERT」，
// 而且每次 `id` 都是新的 `gen_random_uuid()` ⇒ 管理员刚关掉的模型，**下一次模型发现
// 就被重新启用**（`GET /v1/models` 会触发发现，所以表现为"刷新后全部变回启用"）。
//
// 所以这里最要紧的断言是：**已存在的模型一次都不许出现在 Insert 里** ——
// 只要它进了 Insert，`enabled` 就必然被写回 true。
func TestPlanModelSyncPreservesUserState(t *testing.T) {
	existing := []existingModel{
		{ID: "row-1", Name: "claude-sonnet-4", Window: 200000},
		{ID: "row-2", Name: "gpt-4o", Window: 128000},
	}
	plan := planModelSync(existing, []string{"claude-sonnet-4", "gpt-4o"})

	if len(plan.Insert) != 0 {
		t.Fatalf("已存在的模型不许被重新 INSERT（那会把 enabled 写回 true）：%v", plan.Insert)
	}
	if len(plan.Delete) != 0 {
		t.Fatalf("仍在发现结果里的模型不许被删：%v", plan.Delete)
	}
	if len(plan.Keep) != 2 {
		t.Fatalf("应原样保留 2 行，实得 %d", len(plan.Keep))
	}
	// 行 id 也必须保留：前端 PUT /v1/admin/models/{id} 打的就是它，
	// 换了 id 就等于用户在下一次点击时打到一个不存在的行
	if plan.Keep[0].ID != "row-1" || plan.Keep[1].ID != "row-2" {
		t.Fatalf("行 id 必须原样保留，实得 %+v", plan.Keep)
	}
}

// TestPlanModelSyncWindowBackfillOnly 保住 P1-f 的口径：窗口只在"之前不知道"时补，
// 绝不覆盖已有值（覆盖会把用户/探测到的正确分母改掉）。
func TestPlanModelSyncWindowBackfillOnly(t *testing.T) {
	existing := []existingModel{
		{ID: "row-known", Name: "claude-sonnet-4", Window: 111111}, // 已有值 ⇒ 不许动
		{ID: "row-unknown", Name: "gpt-4o", Window: 0},             // 未知 ⇒ 推断补一次
		{ID: "row-hintless", Name: "opencode-zen-unknown", Window: 0}, // 推不出来 ⇒ 不补
	}
	plan := planModelSync(existing, []string{"claude-sonnet-4", "gpt-4o", "opencode-zen-unknown"})

	if len(plan.BackfillWin) != 1 {
		t.Fatalf("只该补 1 行窗口，实得 %d：%+v", len(plan.BackfillWin), plan.BackfillWin)
	}
	got := plan.BackfillWin[0]
	if got.ID != "row-unknown" || got.Window != 128000 {
		t.Fatalf("应补 row-unknown 为 128000，实得 %+v", got)
	}
}

// TestPlanModelSyncAddAndRemove 新发现的建、消失的删，且**只删消失的那些**
// （不能像旧实现那样"先全删"）。
func TestPlanModelSyncAddAndRemove(t *testing.T) {
	existing := []existingModel{
		{ID: "row-gone", Name: "removed-model", Window: 128000},
		{ID: "row-kept", Name: "gpt-4o", Window: 128000},
	}
	plan := planModelSync(existing, []string{"gpt-4o", "brand-new-model", "gpt-4o"})

	if len(plan.Insert) != 1 || plan.Insert[0] != "brand-new-model" {
		t.Fatalf("应只新建 brand-new-model（重复发现也只算一次），实得 %v", plan.Insert)
	}
	if len(plan.Delete) != 1 || plan.Delete[0] != "row-gone" {
		t.Fatalf("应只删除消失的那一行，实得 %v", plan.Delete)
	}
	if len(plan.Keep) != 1 || plan.Keep[0].ID != "row-kept" {
		t.Fatalf("应保留 row-kept，实得 %+v", plan.Keep)
	}
}

// 空发现结果 ⇒ 与旧实现一致：清空该 provider 的缓存行（并由后续发现重建）
func TestPlanModelSyncEmptyDiscoveryClears(t *testing.T) {
	existing := []existingModel{{ID: "row-1", Name: "a", Window: 1}}
	plan := planModelSync(existing, nil)
	if len(plan.Keep) != 0 || len(plan.Insert) != 0 {
		t.Fatalf("空发现不该保留/新建任何行：%+v", plan)
	}
	if len(plan.Delete) != 1 || plan.Delete[0] != "row-1" {
		t.Fatalf("空发现应清掉现有行，实得 %v", plan.Delete)
	}
}
