package api

import "strings"

// 引擎事件的**用量通道**：`done` 给**整轮累计**，`usage` 给**按次增量**（C3）。
//
// 为什么值得单独一个类型与单测：这里的输出直接喂 `DeductTokens` —— 多算一次就是
// **多扣用户的钱**；而"按字段非零就累加"这种写法在新增事件类型时会**静默**重复计。

// usageEventType 是引擎按**每次 LLM 调用**下发的用量事件
// （`python-engine/app/agent/runtime.py` 的 C3 通道，字段为本次调用的增量）。
const usageEventType = "usage"

// subagentEventPrefix 是子 Agent 进度事件的类型前缀（见 python-engine/app/agent/event_sink.py）。
const subagentEventPrefix = "subagent."

// usageTotals 是一轮对话的**累计**用量。
type usageTotals struct {
	input  int
	output int
	cached int
}

// withEvent 把一条事件里的用量并入累计。
//
// ⚠ 按**事件类型**分流，而不是"字段非零就累加"：
//   - `done`（引擎终态事件）携带的是**整轮累计**，是唯一该并入的来源；
//   - `usage` 携带的是**本次调用**的增量，它自己就是一条独立通道；若也并进来，
//     就会与 `done` 的累计**重复计一遍**，并顺着 `DeductTokens` 放大成多扣费。
//   - `subagent.*` 一律**不并入**（M15d/2B 的口径）：子 Agent 的 token 只进引擎侧的
//     任务线账（`subagent_runs` + `run_lineage_ledger`），由线账闸门与预算待办来约束，
//     不进租户额度池。引擎今天只把它的用量放在 `usage` 这个**嵌套字段**里（父帧的
//     扁平 `input_tokens/output_tokens` 为 0），所以此处本来就不会误计 —— 但那个"不会"
//     依赖引擎的序列化细节，一旦哪天有人把嵌套 usage 摊平、或给子 Agent 帧补上扁平字段，
//     这里就会**静默地**把子 Agent 的花费算进父 turn 的账单并扣到租户头上。宁可在唯一
//     的入账入口把它挡死，也不要留一个"靠对端不出错"的约定。
func (t usageTotals) withEvent(eventType string, input, output, cached int) usageTotals {
	if eventType == usageEventType || strings.HasPrefix(eventType, subagentEventPrefix) {
		return t
	}
	if input > 0 {
		t.input += input
	}
	if output > 0 {
		t.output += output
	}
	if cached > 0 {
		t.cached += cached
	}
	return t
}
