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
