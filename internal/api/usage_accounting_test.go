package api

import "testing"

// 计费口径的回归测试：`usage`（按次增量）**不得**并入整轮累计。
//
// 背景：引擎 C3 起按**每次 LLM 调用**下发 `usage`（增量），而终态 `done` 仍给整轮累计。
// 若累计器按"token 字段非零就相加"实现，同一次调用会被计两遍，而它直接进
// `DeductTokens` / `FinishTurn` —— 表现为**用户被多扣费**、缓存命中率也被算错。
func TestUsageEventIsNotCountedIntoRunTotals(t *testing.T) {
	var got usageTotals

	// ① 按次增量：必须被忽略（否则与 done 的累计重复）。
	got = got.withEvent(usageEventType, 100, 50, 10)
	if got != (usageTotals{}) {
		t.Fatalf("usage 是按次增量，不得并入整轮累计（会重复计费），得到 %+v", got)
	}

	// ② 终态事件的累计值：必须并入。
	got = got.withEvent("done", 1000, 200, 30)
	if got.input != 1000 || got.output != 200 || got.cached != 30 {
		t.Fatalf("done 的累计值必须并入，得到 %+v", got)
	}

	// ③ 其它事件若带用量字段（历史或未来新增）仍照常按累计处理：只针对 usage 特判，
	//    避免"顺手把累加整体关掉"这种过度修正。
	got = got.withEvent("text", 5, 7, 0)
	if got.input != 1005 || got.output != 207 || got.cached != 30 {
		t.Fatalf("非 usage 事件应照常并入，得到 %+v", got)
	}
}

// M15d（方案 2B）：子 Agent 的 token 只进引擎侧的任务线账，**不进租户额度池**。
//
// 引擎今天把子 Agent 的用量放在 `subagent.done` 的**嵌套** `usage` 字段里，父帧的扁平
// `input_tokens/output_tokens` 是 0 —— 所以"不并入"过去只是一个巧合。这一条把它变成契约：
// 只要事件类型带 `subagent.` 前缀，即便带上了扁平 token 也不许进入这里的累计，
// 因为它下游就是 `DeductTokens` / `RecordTokenUsage` / `TrueUpTenantQuota`。
func TestSubagentEventsNeverChargeTheTenantPool(t *testing.T) {
	for _, et := range []string{"subagent.done", "subagent.text", "subagent.status", "subagent.notice"} {
		var got usageTotals
		got = got.withEvent(et, 4000, 900, 120)
		if got != (usageTotals{}) {
			t.Fatalf("%s 的用量不得并入租户累计（子 Agent 只记线账），得到 %+v", et, got)
		}
	}

	// 前缀判定不能顺手把主对话的终态也挡掉：done 仍要计。
	var got usageTotals
	got = got.withEvent("subagent.done", 4000, 900, 0).withEvent("done", 10, 2, 1)
	if got.input != 10 || got.output != 2 || got.cached != 1 {
		t.Fatalf("父 turn 的 done 必须照常计入，得到 %+v", got)
	}
}
