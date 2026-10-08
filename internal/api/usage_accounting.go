package api

// 引擎事件的**用量通道**：`done` 给**整轮累计**，`usage` 给**按次增量**（C3）。
//
// 为什么值得单独一个类型与单测：这里的输出直接喂 `DeductTokens` —— 多算一次就是
// **多扣用户的钱**；而"按字段非零就累加"这种写法在新增事件类型时会**静默**重复计。

// usageEventType 是引擎按**每次 LLM 调用**下发的用量事件
// （`python-engine/app/agent/runtime.py` 的 C3 通道，字段为本次调用的增量）。
const usageEventType = "usage"

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
func (t usageTotals) withEvent(eventType string, input, output, cached int) usageTotals {
	if eventType == usageEventType {
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
