// Goal FSM（P6）—— 目标级自动续跑。
//
// 对照实现：Reasonix `internal/session/control/goal.go` 的 `goalMachine` +
// `turn_orchestrator.go` 的 `advanceGoalAfterTurn`（run_loop 注释："In Goal mode the FSM
// auto-continues"）。Chiron 在 P1c 已实现**单次提交内**的有限续跑
// （internal/api/submit_handler.go 的 maxGoalEnforceRounds）；本文件补的是**跨轮**的那一半：
// 目标的状态被**记下来**（跨用户请求），于是"上一次没做完"不会因为用户没再说话就消失。
//
// 设计纪律（与 P1 判定层一致）：
//
//  1. **Advance 是纯函数** —— 不读时钟、不读网络；时间戳由调用方写入（便于单测）。
//  2. **有界** —— 回合数（RoundsUsed vs MaxRounds）是硬闸门，防止"永不收敛的目标"烧 token。
//     到顶即 blocked（**不是** completed）—— 如实说"没做完"，绝不谎报。
//  3. **失败不伪装成继续** —— Failed 直接 blocked（要人介入），而不是无限续跑到预算耗尽。
package agentgoal

// GoalStatus 是目标的生命周期状态。
type GoalStatus string

const (
	// GoalActive：目标进行中（可以继续推进）。
	GoalActive GoalStatus = "active"
	// GoalCompleted：目标已达成（host 证据支持）。
	GoalCompleted GoalStatus = "completed"
	// GoalBlocked：目标卡住 / 到顶 —— **没有达成**，需要人或新的输入。
	GoalBlocked GoalStatus = "blocked"
	// GoalAbandoned：回合非自然结束（取消 / 超时），目标结论不适用。
	GoalAbandoned GoalStatus = "abandoned"
)

// Goal 是一个会话级的目标。
type Goal struct {
	ID          string     `json:"id"`
	SessionID   string     `json:"session_id"`
	Description string     `json:"description"`
	Status      GoalStatus `json:"status"`
	// RoundsUsed 是已消耗的续跑回合数（**由 host 计数**，模型不能改）。
	RoundsUsed int `json:"rounds_used"`
	// MaxRounds 是续跑回合上限（0 表示用 DefaultGoalRounds）。
	MaxRounds int   `json:"max_rounds"`
	UpdatedAt int64 `json:"updated_at"`
	// LastReason 是最近一次推进的原因（给用户/日志）。
	LastReason string `json:"last_reason,omitempty"`
}

// DefaultGoalRounds 是默认的目标级续跑上限。刻意小：目标是"多走一两步收尾"，
// 不是"无限自治"——后者需要额度、审批与可观测性齐备才谈得上。
const DefaultGoalRounds = 3

// EffectiveMaxRounds 返回生效的回合上限。
func (g Goal) EffectiveMaxRounds() int {
	if g.MaxRounds > 0 {
		return g.MaxRounds
	}
	return DefaultGoalRounds
}

// NewGoal 建一个 active 目标。
func NewGoal(id, sessionID, description string, now int64) Goal {
	return Goal{
		ID:          id,
		SessionID:   sessionID,
		Description: description,
		Status:      GoalActive,
		UpdatedAt:   now,
	}
}

// Advance 依据**一次回合的判定结果**推进目标，返回更新后的目标与"是否应继续跑".
//
// 这是 goalMachine 的核心转移（纯函数）。调用方负责把返回的目标持久化。
func Advance(g Goal, report Report, now int64) (Goal, bool) {
	g.UpdatedAt = now
	switch report.Outcome {
	case Completed, None:
		g.Status = GoalCompleted
		g.LastReason = "host evidence satisfied every obligation"
		return g, false

	case Failed:
		// 硬失败：交给用户/运维，不自动续跑（继续跑只会在同一堵墙上再撞几次）。
		g.Status = GoalBlocked
		g.LastReason = "turn failed: " + firstReason(report)
		return g, false

	case Blocked:
		// 取消 / 超时：结论不适用，目标不推进也不判死。
		g.Status = GoalAbandoned
		g.LastReason = "turn ended without a conclusion (cancelled or truncated)"
		return g, false

	case Incomplete:
		g.RoundsUsed++
		if g.RoundsUsed >= g.EffectiveMaxRounds() {
			g.Status = GoalBlocked
			g.LastReason = "continuation budget exhausted: " + firstReason(report)
			return g, false
		}
		g.Status = GoalActive
		g.LastReason = "continuing: " + firstReason(report)
		return g, true
	}

	// 未知 outcome：不猜 —— 保持原状且不续跑（fail-closed）。
	g.LastReason = "unknown turn outcome: " + string(report.Outcome)
	return g, false
}

// Terminal 判断目标是否已到终态（不再推进）。
func (g Goal) Terminal() bool {
	return g.Status == GoalCompleted || g.Status == GoalBlocked || g.Status == GoalAbandoned
}

func firstReason(report Report) string {
	if len(report.Reasons) > 0 {
		return report.Reasons[0]
	}
	return string(report.Outcome)
}
