package engine

import "context"

// Biller interface for credit management during LLM calls.
type Biller interface {
	Deduct(userID, reason string, amount int) (int, error)
	GetBalance(userID string) (int, error)
	DailyFreeCount(ctx context.Context, userID string) (int, error)
	MarkFreeUsage(ctx context.Context, userID, turnID string) error
	DeductTokens(userID string, inputTokens, outputTokens int, turnID string) (int, error)
	// RecordTokenUsage 在扣费成功后记录企业成本中心 token 明细（billing_records）。
	// turnID 非空时按回合幂等：同一 turn 重复调用不再重复扣费/记账（B4）。
	RecordTokenUsage(ctx context.Context, userID, sessionID string, inputTokens, outputTokens, cachedTokens int, turnID string) error
}
