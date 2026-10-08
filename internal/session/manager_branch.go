package session

import (
	"context"
	"errors"
	"fmt"
	"strings"
)

// ── 分支（裁剪 + 压缩）────────────────────────────────────────────────────

const (
	// defaultBranchKeepTail：默认保留最近 4 条原文 —— 与引擎侧 ContextManager 的
	// keep_tail 下限一致（它至少保留 4 条，否则新会话会丢掉"最近一轮"的逐字上下文）。
	defaultBranchKeepTail = 4
	// minCondenseMessages：压缩区至少这么多条才值得调一次模型。
	// 低于它 → 不压缩（整段复制），但仍按 condense 记账 + state=ready。
	minCondenseMessages = 3
)

// BranchPlan 把用户参数翻译成"复制窗口 + 初始状态"。
//
// 为什么抽成纯函数：分支语义最容易错的就是窗口起点与状态初值（例如把 condense
// 也按"复制前 N 条"处理，就会复制一大堆冗余原文、却什么也没压缩）。纯函数可脱离 DB 单测。
type BranchPlan struct {
	// Mode 归一化后的模式："truncate" | "condense"
	Mode string
	// Offset / Limit：复制窗口（0 基，相对源会话的消息顺序）
	Offset int
	Limit  int
	// State 是 branch_state 的初值：""（truncate）| "pending" | "ready"
	State string
	// Condensed 表示本次是否真的会压缩（false 时前端显示"无需压缩"）
	Condensed bool
}

// planBranch 校验参数并给出裁剪计划。
//
// 语义（docs/session-map-branch-design.md 2.3）：
//
//	源会话  m1 ... m(N-K) | m(N-K+1) ... mN | m(N+1) ...
//	        └─ 压缩区 ──┘ └─ 原文保留 ──┘ └─ 被裁掉（丢弃）─┘
//	新会话  [ 模型压缩摘要 ] + m(N-K+1) ... mN
func planBranch(mode string, fromIndex, keepTail int) (BranchPlan, error) {
	if fromIndex <= 0 {
		return BranchPlan{}, errors.New("from_index must be greater than 0")
	}
	switch mode {
	case "", "condense":
		if keepTail < 0 {
			return BranchPlan{}, errors.New("keep_tail must not be negative")
		}
		keep := keepTail
		if keep == 0 {
			keep = defaultBranchKeepTail
		}
		if keep > fromIndex {
			keep = fromIndex
		}
		compressible := fromIndex - keep
		if compressible < minCondenseMessages {
			// 压缩区太短：不值得为它花一次模型调用 → 整段复制，状态直接 ready
			return BranchPlan{Mode: "condense", Offset: 0, Limit: fromIndex, State: "ready"}, nil
		}
		return BranchPlan{
			Mode:      "condense",
			Offset:    compressible,
			Limit:     keep,
			State:     "pending",
			Condensed: true,
		}, nil
	case "truncate":
		// 旧行为：逐字复制前 N 条，不带压缩状态（branch_state 留 NULL）
		return BranchPlan{Mode: "truncate", Offset: 0, Limit: fromIndex}, nil
	default:
		return BranchPlan{}, fmt.Errorf("unsupported branch mode %q", mode)
	}
}

// BranchOptions 是一次分支请求的参数。
type BranchOptions struct {
	Title     string
	FromIndex int
	KeepTail  int
	// Mode："condense"（默认）或 "truncate"
	Mode string
}

// BranchResult 是分支结果：血缘 + 状态，前端可直接拿它渲染徽标与"压缩中"提示。
type BranchResult struct {
	SessionID       string
	ParentSessionID string
	BranchFromSeq   int
	BranchMode      string
	BranchState     string
	Copied          int
	Condensed       bool
	// KeepTail 是**归一化后**的原文保留条数（用户没传时是默认值）。
	// 调用方随后要把它交给引擎做压缩：传原始参数（0 表示"用默认"）会被引擎理解成
	// "一条原文都不保留"，语义正好相反。
	KeepTail int
}

// BranchSession 从 srcSessionID 裁出一段上下文，落到一个**带血缘标记**的新会话。
//
// 参照实现 `vendor/dsh-synapse` 按 `session.header.parentSession` 画边、用 `seedLength`
// 记分叉点；我们此前只有"复制前 N 条"的 fork，没有父子关系与裁剪语义。
//
// 两种模式（见 docs/session-map-branch-design.md 2.2）：
//   - truncate：复制前 N 条原文（等价旧行为，无压缩、无 pending 状态）；
//   - condense：只复制**尾部 K 条原文**，其余保留区交给引擎压成"核心上下文摘要"
//     （摘要由引擎异步写入，因此这里先把 branch_state 置为 pending）。
//
// 为什么仍然一个事务：新会话与它复制出的消息必须同时可见 —— 否则地图会出现
// "空卡"或"有消息却没有会话"的中间态。
func (m *Manager) BranchSession(ctx context.Context, srcSessionID, userID string, opts BranchOptions) (BranchResult, error) {
	plan, err := planBranch(opts.Mode, opts.FromIndex, opts.KeepTail)
	if err != nil {
		return BranchResult{}, err
	}
	if m.pool == nil {
		return BranchResult{}, errors.New("database unavailable")
	}
	newID, err := genID()
	if err != nil {
		return BranchResult{}, err
	}
	tx, err := m.pool.Begin(ctx)
	if err != nil {
		return BranchResult{}, fmt.Errorf("begin branch tx: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	// 源会话：校验归属（同租户语义由 tenant 过滤保证），并继承 agent/tenant
	var srcTitle string
	if err := tx.QueryRow(ctx,
		`SELECT COALESCE(title, '') FROM sessions
		  WHERE id = $1 AND (user_id = NULLIF($2, '')::uuid OR user_id IS NULL)`,
		srcSessionID, userID).Scan(&srcTitle); err != nil {
		return BranchResult{}, fmt.Errorf("source session not found: %w", err)
	}
	title := strings.TrimSpace(opts.Title)
	if title == "" {
		title = strings.TrimSpace(srcTitle) + " · 分支"
	}

	// branch_keep_tail 只在 condense 下有意义（审计/复现用）；truncate 写 NULL
	var keepTailArg interface{}
	if plan.Mode == "condense" {
		keepTailArg = plan.Limit
	}
	if _, err := tx.Exec(ctx,
		`INSERT INTO sessions (id, tenant_id, user_id, agent_id, title, status, created_at, updated_at,
		                       parent_session_id, branch_from_seq, branch_mode, branch_state, branch_keep_tail)
		 SELECT $1, tenant_id, user_id, agent_id, $2, 'active', NOW(), NOW(), id, $3::int, $4::text, NULLIF($5::text, ''), $6::int
		   FROM sessions WHERE id = $7`,
		newID, title, opts.FromIndex, plan.Mode, plan.State, keepTailArg, srcSessionID); err != nil {
		return BranchResult{}, fmt.Errorf("insert branched session: %w", err)
	}

	// 复制窗口内的消息：source（用户输入 / 子 Agent 自动轮）一并复制，前端靠它区分渲染。
	//
	// 时间戳按窗口内的**全表序号**重排：`NOW() - ((N - rn) ms)` —— 窗口最后一条正好是
	// NOW()，既保持与源会话一致的相对间距，又保证新会话之后的真实消息时间必然更大
	// （否则历史列表排序会乱）。窗口函数在 LIMIT/OFFSET 之前求值，所以 rn 是源会话里的
	// 绝对序号（不是窗口内序号）。
	tag, err := tx.Exec(ctx,
		`INSERT INTO messages (id, session_id, role, content, tool_calls, turn_id, source, created_at)
		 SELECT gen_random_uuid()::text, $1, g.role, g.content, g.tool_calls, g.turn_id, g.source,
		        NOW() - (($2::int - g.rn) * INTERVAL '1 millisecond')
		   FROM (SELECT role, content, tool_calls, turn_id, source,
		                ROW_NUMBER() OVER (ORDER BY created_at, id) AS rn
		           FROM messages
		          WHERE session_id = $3
		          ORDER BY created_at, id
		          OFFSET $4::int LIMIT $5::int) g`,
		newID, opts.FromIndex, srcSessionID, plan.Offset, plan.Limit)
	if err != nil {
		return BranchResult{}, fmt.Errorf("copy messages for branch: %w", err)
	}
	copied := int(tag.RowsAffected())
	if err := tx.Commit(ctx); err != nil {
		return BranchResult{}, fmt.Errorf("commit branch tx: %w", err)
	}
	return BranchResult{
		SessionID:       newID,
		ParentSessionID: srcSessionID,
		BranchFromSeq:   opts.FromIndex,
		BranchMode:      plan.Mode,
		BranchState:     plan.State,
		Copied:          copied,
		Condensed:       plan.Condensed,
		KeepTail:        plan.Limit,
	}, nil
}

// ForkSession 保留旧签名：等价于 mode='truncate' 的 BranchSession。
// `POST /v1/conversations/{id}/fork` 与既有调用方（地图的按真实分支连线）继续可用。
func (m *Manager) ForkSession(ctx context.Context, srcSessionID, userID, title string, fromIndex int) (string, int, error) {
	res, err := m.BranchSession(ctx, srcSessionID, userID, BranchOptions{
		Title:     title,
		FromIndex: fromIndex,
		Mode:      "truncate",
	})
	if err != nil {
		return "", 0, err
	}
	return res.SessionID, res.Copied, nil
}
