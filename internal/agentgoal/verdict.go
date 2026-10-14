// Package agentgoal 从一个回合的 **host 证据** 判定"这一轮到底做完没有"。
//
// 这是 P1（host 证据判定）的判定层，对应 Reasonix 的 internal/runtime/verdict。
// 三条不变量（与 Reasonix verdict 的 I2 约束同源）：
//
//  1. **Evaluate 不读取任何由模型写下的状态** —— 它只看 internal/evidence 采到的证据
//     与网关自身观测到的信号（是否有 error 事件、是否产出了正文、是否被截断）。
//     模型"说完成了"不构成证据；模型的 claim 至多触发验证，永不满足义务。
//  2. **相同输入给相同结果** —— 纯函数、无副作用、无并发、无时间依赖。
//  3. **fail-closed** —— 证据缺失/无法解析时判 owed 或 unverifiable，绝不判 satisfied。
//
// 为什么需要它：此前"完成"完全等于"模型不再调工具"（python-engine runtime.py 的
// `break`）。这一层把"完成"改成 host 可复核的断言：有哪些义务、各自有无证据。
package agentgoal

import (
	"fmt"
	"sort"
	"strings"

	"github.com/athenavi/chiron/internal/evidence"
)

// Verdict 是**单个义务**的状态。
type Verdict string

const (
	// Satisfied：有 host 证据支持该义务已满足。
	Satisfied Verdict = "satisfied"
	// Unsatisfied：有 host 证据**反对**该义务（例如回合以错误结束）。
	Unsatisfied Verdict = "unsatisfied"
	// Owed：义务尚未完成，但也没有反证 —— 是"还欠着"，可以被后续工作满足。
	Owed Verdict = "owed"
	// Unverifiable：host 无法判定（证据不足且不构成"欠"），例如工具不可识别。
	Unverifiable Verdict = "unverifiable"
	// Stale：证据在相关事件之后已过期（本轮先不产生，保留给 P5/P6 的会话级证据）。
	Stale Verdict = "stale"
)

// Outcome 是整个回合的结论。
type Outcome string

const (
	// Completed：所有必需义务都 satisfied。
	Completed Outcome = "completed"
	// Incomplete：有义务 owed / unsatisfied，但没有硬失败 —— 可继续跑。
	Incomplete Outcome = "incomplete"
	// Failed：出现硬失败（回合以错误结束）。
	Failed Outcome = "failed"
	// Blocked：回合不是自然结束（被取消 / 截断），结论不适用。
	Blocked Outcome = "blocked"
	// None：本回合没有可判定的义务（例如纯问答）。
	None Outcome = "none"
)

// class 是义务的类别，决定它由哪段判定逻辑处理。
type class string

const (
	classNoError    class = "no-error"          // 回合不得以错误结束
	classAnswered   class = "answered"          // 回合应产出正文
	classVerified   class = "verified-changes"  // 成功的改动应有验证跟随
	classModelClaim class = "model-claim"       // 模型声明的判据（P3 起接入；判定仍只看证据）
)

// Obligation 是一条需要被满足/证明的义务。
type Obligation struct {
	ID          string `json:"id"`
	Description string `json:"description"`
	// Required=false 的义务只出现在报告里，**不影响** Outcome。
	Required bool  `json:"required"`
	Class    class `json:"-"`
}

// Finding 是一条义务的判定结果。
type Finding struct {
	Obligation Obligation `json:"obligation"`
	Verdict    Verdict    `json:"verdict"`
	Reason     string     `json:"reason"`
	// Evidence 指向支撑该判定的工具调用 id（可追溯）。
	Evidence []string `json:"evidence,omitempty"`
}

// Signals 是证据之外的、网关自身观测到的回合信号。
type Signals struct {
	HasError     bool   // 引擎发过 error 事件
	ErrorMessage string // 该错误的内容（用于报告）
	HasAnswer    bool   // 回合产出了正文（非空文本）
	Cancelled    bool   // 用户取消 / 会话取消
	Truncated    bool   // 被 deadline 截断
}

// Report 是一次判定的输出。
type Report struct {
	Outcome  Outcome   `json:"outcome"`
	Findings []Finding `json:"findings"`
	// Reasons 是给用户看的简短说明（Outcome 非 completed 时非空）。
	Reasons []string `json:"reasons,omitempty"`
}

// Complete 是给调用方的便捷判定：是否可以正常收尾（不被强制继续）。
func (r Report) Complete() bool {
	return r.Outcome == Completed || r.Outcome == None
}

// Evaluate 用 host 证据与信号判定本回合。纯函数：不读模型写的任何状态。
//
// 注意 `e` 可以为 nil（采集器未接入 / 空回合），此时按"无证据"处理。
func Evaluate(e *evidence.Collector, sig Signals) Report {
	obligations := derive(e, sig)
	if len(obligations) == 0 {
		return Report{Outcome: None}
	}

	findings := make([]Finding, 0, len(obligations))
	for _, ob := range obligations {
		findings = append(findings, judge(ob, e, sig))
	}
	// 稳定排序：必需义务在前，其次按 id —— 报告可读且可测。
	sort.SliceStable(findings, func(i, j int) bool {
		if findings[i].Obligation.Required != findings[j].Obligation.Required {
			return findings[i].Obligation.Required
		}
		return findings[i].Obligation.ID < findings[j].Obligation.ID
	})

	outcome := summarize(findings)
	var reasons []string
	if outcome != Completed && outcome != None {
		for _, f := range findings {
			if f.Obligation.Required && f.Verdict != Satisfied {
				reasons = append(reasons, fmt.Sprintf("%s: %s", f.Obligation.ID, f.Reason))
			}
		}
	}
	return Report{Outcome: outcome, Findings: findings, Reasons: reasons}
}

// derive 由 host **自己**派生出义务集 —— 这是"host 拥有型"的关键：
// 模型不能通过少声明/多声明来改变判定的严格性（P3 的模型声明判据会被当作
// classModelClaim 加入，但**只是被复核的对象**，不是判定依据）。
func derive(e *evidence.Collector, sig Signals) []Obligation {
	obs := []Obligation{{
		ID:          "no-error",
		Description: "The turn must not end on an engine error.",
		Required:    true,
		Class:       classNoError,
	}}

	// 只有出现过**成功的改动**时，才派生"改动需被验证"的义务 —— 否则纯问答回合
	// 会被迫"验证"一件根本没发生的事（假警报会把这条机制变成噪声）。
	if e != nil && len(e.Mutations()) > 0 {
		obs = append(obs, Obligation{
			ID:          "verified-changes",
			Description: "Every change to the workspace must be followed by a successful verification.",
			Required:    true,
			Class:       classVerified,
		})
	}

	// 回合应产出正文（除非被取消/截断 —— 那属于 Blocked，不算欠）。
	if !sig.Cancelled && !sig.Truncated {
		obs = append(obs, Obligation{
			ID:          "answered",
			Description: "The turn must produce a non-empty answer.",
			Required:    true,
			Class:       classAnswered,
		})
	}
	return obs
}

// judge 判定单条义务。
func judge(ob Obligation, e *evidence.Collector, sig Signals) Finding {
	f := Finding{Obligation: ob}
	switch ob.Class {
	case classNoError:
		if sig.HasError {
			f.Verdict = Unsatisfied
			f.Reason = nonEmpty(sig.ErrorMessage, "engine reported an error")
		} else {
			f.Verdict = Satisfied
			f.Reason = "no engine error observed"
		}
	case classAnswered:
		if sig.HasAnswer {
			f.Verdict = Satisfied
			f.Reason = "a non-empty answer was produced"
		} else {
			f.Verdict = Owed
			f.Reason = "no answer was produced"
		}
	case classVerified:
		if e == nil {
			f.Verdict = Unverifiable
			f.Reason = "no evidence collector attached"
			break
		}
		unverified := e.UnverifiedMutations()
		if len(unverified) == 0 {
			f.Verdict = Satisfied
			f.Reason = "every change was followed by a successful verification"
			for _, v := range e.Verifications() {
				f.Evidence = append(f.Evidence, v.ToolCallID)
			}
			break
		}
		f.Verdict = Owed
		f.Reason = fmt.Sprintf("%d change(s) were not followed by a successful verification", len(unverified))
		for _, m := range unverified {
			if m.ToolCallID != "" {
				f.Evidence = append(f.Evidence, m.ToolCallID)
			}
		}
	case classModelClaim:
		// 模型声明的判据：**只按 host 证据复核**。无证据即 owed（claim 自身永不满足义务）。
		f.Verdict = Owed
		f.Reason = "model-declared criterion has no host evidence yet"
	default:
		f.Verdict = Unverifiable
		f.Reason = "unknown obligation class"
	}
	return f
}

// summarize 汇总义务结论。
func summarize(findings []Finding) Outcome {
	var anyUnsatisfied, anyOwed, anyUnverifiable bool
	for _, f := range findings {
		if !f.Obligation.Required {
			continue
		}
		switch f.Verdict {
		case Unsatisfied:
			anyUnsatisfied = true
		case Owed:
			anyOwed = true
		case Unverifiable, Stale:
			anyUnverifiable = true
		}
	}
	switch {
	case anyUnsatisfied:
		return Failed
	case anyOwed:
		// 有"欠"就还能继续跑（这是"强制继续"的触发点）。
		return Incomplete
	case anyUnverifiable:
		// 无法判定：不硬判失败，但也不宣称完成。
		return Incomplete
	default:
		return Completed
	}
}

// BlockedReport 是"回合非自然结束"（取消/截断）时的报告 —— 结论不适用。
func BlockedReport(reason string) Report {
	return Report{
		Outcome: Blocked,
		Reasons: []string{nonEmpty(reason, "turn did not end naturally")},
	}
}

func nonEmpty(s, fallback string) string {
	if strings.TrimSpace(s) == "" {
		return fallback
	}
	return strings.TrimSpace(s)
}
