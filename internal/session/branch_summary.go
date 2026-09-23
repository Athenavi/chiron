package session

import (
	"context"
	"errors"
	"fmt"
	"strings"
)

// ── 分支压缩摘要的落库（P2）──
//
// 职责边界：引擎只负责"读历史 → 调模型 → 返回摘要"，**落库在 Go**
// （与 app/queue/worker.py 里 `_handle_agent_followup` 的注释一致：
// messages 落库、SSE 推送、turn 状态都在 Go）。所以摘要消息与 branch_state 的推进都放在这里。

// AppendBranchSummary 把分支压缩摘要落成新会话里的一条 system 消息，
// 并把 branch_state 从 pending 推进到 ready。
//
// 为什么先 CAS 再插入：压缩任务可能被重复触发（用户重试、网关重启后重投、引擎重复回调），
// 只有**当前仍是 pending** 的那一次才允许写摘要 —— 否则同一会话会出现两份摘要消息。
// 返回 wrote=false 表示"这次不需要写"（已被别的执行者写好，或状态不是 pending）。
//
// 为什么 updated_at 不推进：摘要不是用户的活动（与置顶/标签的处理一致），
// 否则会话会莫名其妙跳到会话列表顶部。
func (m *Manager) AppendBranchSummary(ctx context.Context, sessionID, summary string) (bool, error) {
	if m.pool == nil {
		return false, errors.New("database unavailable")
	}
	summary = strings.TrimSpace(summary)
	if summary == "" {
		return false, errors.New("summary is empty")
	}

	tx, err := m.pool.Begin(ctx)
	if err != nil {
		return false, fmt.Errorf("begin branch summary tx: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	// 抢占式推进状态：只有 pending 能被推进，抢不到就说明别人已经在写（或状态不符）
	claim, err := tx.Exec(ctx,
		`UPDATE sessions SET branch_state = 'ready'
		  WHERE id::text = $1 AND branch_state = 'pending'`, sessionID)
	if err != nil {
		return false, fmt.Errorf("claim branch summary: %w", err)
	}
	if claim.RowsAffected() == 0 {
		return false, nil
	}

	if _, err := tx.Exec(ctx,
		`INSERT INTO messages (id, session_id, role, content, source, created_at)
		 VALUES (gen_random_uuid()::text, $1, 'system', $2, 'branch_summary', NOW())`,
		sessionID, summary); err != nil {
		return false, fmt.Errorf("insert branch summary: %w", err)
	}

	if err := tx.Commit(ctx); err != nil {
		return false, fmt.Errorf("commit branch summary: %w", err)
	}
	return true, nil
}

// MarkBranchState 推进分支状态（ready / failed），同样是 CAS（只改 pending 的行）。
//
// 失败也要落状态：否则会话永远停在 pending，前端只能显示"压缩中…"，
// 用户无法知道"其实已经失败了、可以重试"。
func (m *Manager) MarkBranchState(ctx context.Context, sessionID, state string) (bool, error) {
	switch state {
	case "ready", "failed", "pending":
	default:
		return false, fmt.Errorf("unsupported branch state %q", state)
	}
	if m.pool == nil {
		return false, errors.New("database unavailable")
	}
	tag, err := m.pool.Exec(ctx,
		`UPDATE sessions SET branch_state = $2
		  WHERE id::text = $1 AND branch_state = 'pending'`, sessionID, state)
	if err != nil {
		return false, fmt.Errorf("mark branch state: %w", err)
	}
	return tag.RowsAffected() > 0, nil
}

// ResetBranchStateForRetry 把一个已结束的分支压缩重新置回 pending，用于"重试压缩"。
//
// 与 AppendBranchSummary 的 CAS 方向相反：这里要显式允许 pending 之外的状态回到 pending
// （ready 是"已完成"，重试意味着用户不认可现有摘要；failed 是"失败了要再来一次"）。
// 已存在的摘要消息不会被删除 —— 重试成功后写的是**新的一条**摘要，
// 便于对比两次压缩的结果；这也避免"重试时把用户能看到的旧摘要弄丢"。
func (m *Manager) ResetBranchStateForRetry(ctx context.Context, sessionID string) (bool, error) {
	if m.pool == nil {
		return false, errors.New("database unavailable")
	}
	tag, err := m.pool.Exec(ctx,
		`UPDATE sessions SET branch_state = 'pending'
		  WHERE id::text = $1
		    AND branch_mode = 'condense'
		    AND branch_state IS DISTINCT FROM 'pending'`, sessionID)
	if err != nil {
		return false, fmt.Errorf("reset branch state: %w", err)
	}
	return tag.RowsAffected() > 0, nil
}
