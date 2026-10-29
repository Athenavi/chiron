package api

import (
	"context"
	"log/slog"

	"github.com/athenavi/chiron/internal/db"
)

// UsageEvent 是一次 LLM 调用的用量明细。
//
// 与 billing_records 的区别：那边一行一回合、靠 ON CONFLICT (turn_id) 幂等，是
// 扣费与对账的依据；这里是**只读的观测数据**，一回合可能多行（主模型 / 子 Agent /
// 摘要），供「钱花在哪个模型上」的统计使用。
//
// Model 取引擎事件里的实际模型（`evt.Model`），不是请求里的 llmConfig.model ——
// 后者只是意图，子 Agent 与 fallback 会让两者不一致，用它统计等于在统计用户想用什么。
type UsageEvent struct {
	TenantID     int16
	UserID       string
	SessionID    string
	TurnID       string
	Model        string
	Provider     string
	InputTokens  int
	OutputTokens int
	CachedTokens int
	CostCents    *int
}

// RecordUsageEvent 写一条用量明细。
//
// **绝不返回错误给调用方**：明细写入失败不能让已完成的扣费回滚，也不能让一次成功
// 的对话因统计失败而报错。失败只告警。
func RecordUsageEvent(ctx context.Context, ev UsageEvent) {
	if db.GlobalDBManager == nil {
		return
	}
	if ev.Model == "" && ev.InputTokens == 0 && ev.OutputTokens == 0 {
		return
	}
	_, err := db.GlobalDBManager.Exec(ctx,
		`INSERT INTO usage_events
		   (tenant_id, user_id, session_id, turn_id, model, provider,
		    input_tokens, output_tokens, cached_tokens, cost_cents)
		 VALUES ($1, $2, $3, $4, NULLIF($5, ''), NULLIF($6, ''), $7, $8, $9, $10)`,
		ev.TenantID, ev.UserID, ev.SessionID, ev.TurnID, ev.Model, ev.Provider,
		ev.InputTokens, ev.OutputTokens, ev.CachedTokens, ev.CostCents)
	if err != nil {
		slog.Warn("usage_events: record failed",
			"user", ev.UserID, "session", ev.SessionID, "model", ev.Model, "error", err)
	}
}
