package api

import (
	"context"
	"log/slog"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/broadcast"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/session"
)

// TakeoverKey 标记「接管巡检回合」注入的系统指令来源，前端据此渲染成系统卡片。
const TakeoverKey = "superagent_takeover"

// TakeoverSource 是接管回合落进 messages.source 的取值。
const TakeoverSource = "superagent_takeover"

const (
	takeoverScanInterval = 30 * time.Second
	// takeoverLeaseStale 决定"持有者多久没续租视为已崩溃"。必须明显大于扫描间隔，
	// 否则一个副本自己的合法租约会在两拍之间被判为陈旧，另一副本随即插进来。
	takeoverLeaseStale = 300
	takeoverHeartbeat  = 60 * time.Second
	takeoverScanLimit  = 100
)

// takeoverTickPrompt 是每一拍注入的系统指令。
//
// 必须显式声明"不是用户提问"：否则模型会把它当成新需求，用户回来看到的是
// 一堆没人点过的任务在跑。
const takeoverTickPrompt = "[Super-agent 接管] 这是系统定时触发的巡检回合，不是用户的新提问。\n" +
	"请检查本会话的任务板与未完成的工作，挑其中一个推进到可验证的进展，然后给出简短结论。\n" +
	"若确实没有可推进的事项，直接回答\"当前无待办\"并结束 —— 不要为此新开无关工作。"

// takeoverDriver 按节拍驱动到期的接管。
//
// 所有"该不该跑"的判断都压进一条 UPDATE：多副本同时扫描时，只有条件成立的那一个
// 能拿到租约并自增计数，不需要应用层加锁。
type takeoverDriver struct {
	submit        *SubmitHandler
	sessionMgr    *session.Manager
	agentSem      *SharedSemaphore
	tenantResMgr  *TenantResourceManager
	hub           *broadcast.Hub
	submitTimeout time.Duration
}

// StartSuperAgentDriver 启动接管驱动循环（每个网关副本各一个，靠租约收敛）。
func StartSuperAgentDriver(
	ctx context.Context,
	submit *SubmitHandler,
	sessionMgr *session.Manager,
	agentSem *SharedSemaphore,
	tenantResMgr *TenantResourceManager,
	hub *broadcast.Hub,
	submitTimeout time.Duration,
) {
	d := &takeoverDriver{
		submit:        submit,
		sessionMgr:    sessionMgr,
		agentSem:      agentSem,
		tenantResMgr:  tenantResMgr,
		hub:           hub,
		submitTimeout: submitTimeout,
	}
	go d.loop(ctx)
}

func (d *takeoverDriver) loop(ctx context.Context) {
	ticker := time.NewTicker(takeoverScanInterval)
	defer ticker.Stop()
	runBackgroundCycle("superagent-driver", func() { d.tick(ctx) })
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			runBackgroundCycle("superagent-driver", func() { d.tick(ctx) })
		}
	}
}

func (d *takeoverDriver) tick(ctx context.Context) {
	if db.GlobalDBManager == nil {
		return
	}
	scanCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()

	d.expire(scanCtx)
	d.sweepMaxRounds(scanCtx)
	d.orphanStopping(scanCtx)

	// 只取"还该跑"的候选：到期的已在上面收口，非 active 的不参与驱动。
	rows, err := db.GlobalDBManager.Query(scanCtx, `
		SELECT id, session_id, user_id, tenant_id::int
		  FROM superagent_takeovers
		 WHERE status = 'active'
		   AND ends_at > now()
		   AND (max_rounds <= 0 OR rounds < max_rounds)
		   AND (last_tick_at IS NULL
		        OR last_tick_at + make_interval(secs => tick_seconds) <= now())
		 ORDER BY last_tick_at NULLS FIRST
		 LIMIT $1`, takeoverScanLimit)
	if err != nil {
		slog.Warn("superagent driver: scan failed", "error", err)
		return
	}
	defer rows.Close()

	type candidate struct {
		id, sessionID, userID string
		tenantID              int
	}
	var list []candidate
	for rows.Next() {
		var c candidate
		if err := rows.Scan(&c.id, &c.sessionID, &c.userID, &c.tenantID); err != nil {
			slog.Warn("superagent driver: scan row failed", "error", err)
			return
		}
		list = append(list, c)
	}
	for _, c := range list {
		if ctx.Err() != nil {
			return
		}
		d.driveOne(ctx, c.id, c.sessionID, c.userID, int16(c.tenantID))
	}
}

// driveOne 驱动一条接管的一拍。
//
// 顺序不能变：抢租约 → 抢会话锁 → **重读接管状态**。缺了最后一步，用户在
// "决定接管"与"抢到锁"之间点了打断，这一拍仍会把回合拉起来。
func (d *takeoverDriver) driveOne(ctx context.Context, id, sessionID, userID string, tenantID int16) {
	token := newLeaseToken()
	if !d.claimTick(ctx, id, token) {
		return
	}
	releaseLease := func() { d.releaseLease(context.WithoutCancel(ctx), id, token) }
	// 租约必须在本次回合跑完之后才归还，否则另一副本会在回合进行中插进来。

	runToken := "superagent-" + token
	releaseRun, locked, lockErr := AcquireSessionRunLock(ctx, sessionID, runToken)
	if lockErr != nil {
		slog.Warn("superagent driver: run lock unavailable", "session", sessionID, "error", lockErr)
		releaseLease()
		return
	}
	if !locked {
		// 用户正在用这个会话：本次窗口作废，不排队等（排队会让接管在用户
		// 毫不知情时突然落地）。下个节拍自然重试。
		d.markSkippedBusy(context.WithoutCancel(ctx), id)
		releaseLease()
		return
	}

	// check-then-act：锁已到手，但状态可能已被打断或到期。
	var status string
	var tokenCap, tokenSpent int64
	err := db.GlobalDBManager.QueryRow(ctx, `
		SELECT status, token_cap, token_spent
		  FROM superagent_takeovers WHERE id = $1`, id).
		Scan(&status, &tokenCap, &tokenSpent)
	if err != nil || status != takeoverStatusActive {
		releaseRun()
		releaseLease()
		return
	}
	if tokenCap > 0 && tokenSpent >= tokenCap {
		releaseRun()
		d.finish(context.WithoutCancel(ctx), id, "budget_exhausted", "token_cap")
		releaseLease()
		return
	}

	releaseSem, ok := d.agentSem.TryAcquire(ctx)
	if !ok {
		releaseRun()
		d.markSkippedBusy(context.WithoutCancel(ctx), id)
		releaseLease()
		return
	}
	releaseTenant, tenantOK := d.tenantResMgr.Acquire(tenantStr(tenantID))
	if !tenantOK {
		releaseSem()
		releaseRun()
		d.markSkippedBusy(context.WithoutCancel(ctx), id)
		releaseLease()
		return
	}

	reserved, quotaErr := EnforceTenantQuota(ctx,
		&auth.Claims{UserID: userID, TenantID: tenantID},
		EstimateRequestTokens(takeoverTickPrompt, nil))
	if quotaErr != nil {
		slog.Warn("superagent driver: tenant token quota exceeded",
			"takeover_id", id, "session", sessionID, "user_id", userID)
		releaseTenant()
		releaseSem()
		releaseRun()
		releaseLease()
		return
	}

	// 轮数在真正要起回合前才占用：前面任何一次放弃（忙、信号量、配额）都不该
	// 消耗用户设的 max_rounds，否则窗口没跑成几拍额度就没了。
	if !d.consumeRound(ctx, id) {
		releaseTenant()
		releaseSem()
		releaseRun()
		d.finish(context.WithoutCancel(ctx), id, takeoverStatusStopped, "max_rounds")
		releaseLease()
		return
	}

	timeout := d.submitTimeout
	if timeout < DefaultAgentTimeout {
		timeout = DefaultAgentTimeout
	}
	// 回合必须脱离扫描上下文：响应一返回扫描就结束，回合还在跑。
	runCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), timeout)
	runCtx = WithQuotaReserve(runCtx, reserved)
	stopHeartbeat := StartSessionRunLockHeartbeat(runCtx, sessionID, runToken)
	stopLease := d.startLeaseHeartbeat(id, token)

	go func() {
		defer releaseTenant()
		defer releaseSem()
		defer releaseRun()
		defer releaseLease()
		defer stopLease()
		defer cancel()
		defer stopHeartbeat()
		defer func() {
			if rec := recover(); rec != nil {
				slog.Error("superagent driver: turn panicked", "panic", rec, "takeover_id", id)
			}
		}()
		d.submit.HandleSubmit(runCtx, userID, sessionID, takeoverTickPrompt,
			map[string]interface{}{}, map[string]interface{}{TakeoverKey: id}, nil)
	}()
}

// claimTick 抢租约并判定节拍。
//
// 这里**不**自增 rounds：本拍随后可能因会话正忙、信号量或配额而完全放弃，
// 把"没跑成的一拍"计进轮数，会让用户设的 max_rounds 在什么都没跑的情况下耗尽。
// 轮数在真正要起回合前由 consumeRound 原子自增。
func (d *takeoverDriver) claimTick(ctx context.Context, id, token string) bool {
	tag, err := db.GlobalDBManager.Exec(ctx, `
		UPDATE superagent_takeovers
		   SET lease_token = $2, lease_at = now(), last_tick_at = now()
		 WHERE id = $1 AND status = 'active'
		   AND ends_at > now()
		   AND (max_rounds <= 0 OR rounds < max_rounds)
		   AND (last_tick_at IS NULL
		        OR last_tick_at + make_interval(secs => tick_seconds) <= now())
		   AND (lease_at IS NULL
		        OR lease_at < now() - make_interval(secs => $3))`,
		id, token, takeoverLeaseStale)
	if err != nil {
		slog.Warn("superagent driver: claim tick failed", "takeover_id", id, "error", err)
		return false
	}
	return tag.RowsAffected() == 1
}

// consumeRound 在即将起回合时原子占用一个轮数名额。
//
// 与 claimTick 分开是因为两者的语义不同：claimTick 说的是"这一拍归我"，
// consumeRound 说的是"这一拍真的跑了一个回合"。返回 false 表示轮数已用尽。
func (d *takeoverDriver) consumeRound(ctx context.Context, id string) bool {
	tag, err := db.GlobalDBManager.Exec(ctx, `
		UPDATE superagent_takeovers SET rounds = rounds + 1
		 WHERE id = $1 AND status = 'active'
		   AND (max_rounds <= 0 OR rounds < max_rounds)`, id)
	if err != nil {
		slog.Warn("superagent driver: consume round failed", "takeover_id", id, "error", err)
		return false
	}
	return tag.RowsAffected() == 1
}

// releaseLease 归还租约。必须带 token 条件：否则本次超时被接管后回来，
// 会清掉新持有者的租约，制造重复执行窗口。
func (d *takeoverDriver) releaseLease(ctx context.Context, id, token string) {
	relCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	if _, err := db.GlobalDBManager.Exec(relCtx, `
		UPDATE superagent_takeovers
		   SET lease_at = NULL, lease_token = NULL
		 WHERE id = $1 AND lease_token = $2`, id, token); err != nil {
		slog.Error("superagent driver: release lease failed; row may stay locked until stale",
			"takeover_id", id, "error", err)
	}
}

// startLeaseHeartbeat 在回合运行期间续租：回合可能长于 stale 阈值。
func (d *takeoverDriver) startLeaseHeartbeat(id, token string) func() {
	stop := make(chan struct{})
	go func() {
		t := time.NewTicker(takeoverHeartbeat)
		defer t.Stop()
		for {
			select {
			case <-stop:
				return
			case <-t.C:
				ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
				if _, err := db.GlobalDBManager.Exec(ctx, `
					UPDATE superagent_takeovers SET lease_at = now()
					 WHERE id = $1 AND lease_token = $2`, id, token); err != nil {
					slog.Warn("superagent driver: lease heartbeat failed", "takeover_id", id, "error", err)
				}
				cancel()
			}
		}
	}()
	return func() { close(stop) }
}

// expire 把到点的接管收口。先落状态再取消回合 —— 反过来驱动循环仍看到 active。
func (d *takeoverDriver) expire(ctx context.Context) {
	rows, err := db.GlobalDBManager.Query(ctx, `
		UPDATE superagent_takeovers
		   SET status = 'expired', stop_reason = 'expired'
		 WHERE status = 'active' AND ends_at <= now()
		RETURNING id, session_id, user_id`)
	if err != nil {
		slog.Warn("superagent driver: expire failed", "error", err)
		return
	}
	defer rows.Close()

	// 先收集再取消：在迭代 rows 期间发另一次查询会占着同一条连接，
	// 而取消要走网络到引擎，拖长持有时间，也会让"边读结果集边发查询"的语义含糊。
	type expired struct{ id, sessionID, userID string }
	var list []expired
	for rows.Next() {
		var e expired
		if rows.Scan(&e.id, &e.sessionID, &e.userID) != nil {
			break
		}
		list = append(list, e)
	}
	if err := rows.Err(); err != nil {
		slog.Warn("superagent driver: expire scan failed", "error", err)
		return
	}
	rows.Close()

	for _, e := range list {
		// 状态已提交为 expired，驱动不会再拉起新回合；这里只是让在跑的那一轮尽快收口。
		if _, err := cancelSessionRun(ctx, d.sessionMgr, e.sessionID, e.userID); err != nil {
			slog.Warn("superagent driver: cancel expired turn failed",
				"takeover_id", e.id, "session", e.sessionID, "error", err)
		}
		publishTakeoverEvent(d.hub, eventTakeoverEnded, e.id, e.sessionID, e.userID, "expired")
	}
}

// sweepMaxRounds 收口"轮数已用尽"的接管。
//
// 没有它，这类行会永远停在 active：driveOne 的候选查询已经把它们排除在外，
// 于是既跑不了新的回合，也没有任何路径把它置成终结 —— 前端遮罩会一直显示
// "接管中"，而实际早已停止工作。
func (d *takeoverDriver) sweepMaxRounds(ctx context.Context) {
	rows, err := db.GlobalDBManager.Query(ctx, `
		UPDATE superagent_takeovers
		   SET status = 'stopped', stop_reason = 'max_rounds'
		 WHERE status = 'active' AND max_rounds > 0 AND rounds >= max_rounds
		RETURNING id, session_id, user_id`)
	if err != nil {
		slog.Warn("superagent driver: max rounds sweep failed", "error", err)
		return
	}
	defer rows.Close()
	for rows.Next() {
		var id, sessionID, userID string
		if rows.Scan(&id, &sessionID, &userID) != nil {
			return
		}
		publishTakeoverEvent(d.hub, eventTakeoverEnded, id, sessionID, userID, takeoverStatusStopped)
	}
}

// orphanStopping 收口那些回合已经结束、却没人置 stopped 的 stopping 行。
//
// 没有它，任何一次取消失败都会让接管永久停在 stopping —— 前端遮罩一直显示
// "正在停止…"，而实际上什么都没在跑。
//
// 判据用 stopping_at 而不是 lease_at：用户点打断时这一行可能从未持有过租约
// （lease_at IS NULL），按 lease_at 判会立刻满足条件，把还在跑的回合标成已停止，
// 遮罩随即消失 —— 用户会以为停了，实际还在花钱。
func (d *takeoverDriver) orphanStopping(ctx context.Context) {
	rows, err := db.GlobalDBManager.Query(ctx, `
		UPDATE superagent_takeovers
		   SET status = 'stopped'
		 WHERE status = 'stopping'
		   AND stopping_at IS NOT NULL
		   AND stopping_at < now() - make_interval(secs => $1)
		RETURNING id, session_id, user_id`,
		takeoverLeaseStale)
	if err != nil {
		slog.Warn("superagent driver: orphan stopping sweep failed", "error", err)
		return
	}
	defer rows.Close()
	for rows.Next() {
		var id, sessionID, userID string
		if rows.Scan(&id, &sessionID, &userID) != nil {
			return
		}
		publishTakeoverEvent(d.hub, eventTakeoverEnded, id, sessionID, userID, takeoverStatusStopped)
	}
}

// finish 终结一条接管（预算用尽、轮数用尽等），不经过用户请求。
func (d *takeoverDriver) finish(ctx context.Context, id, status, reason string) {
	var sessionID, userID string
	// 用 RETURNING 取会话标识：终态事件要发到那个会话的流上，否则前端遮罩要等
	// 下一次快照轮询才发现接管已经结束。
	err := db.GlobalDBManager.QueryRow(ctx, `
		UPDATE superagent_takeovers
		   SET status = $2, stop_reason = $3
		 WHERE id = $1 AND status = 'active'
		RETURNING session_id, user_id`, id, status, reason).Scan(&sessionID, &userID)
	if err != nil {
		if !isNoRows(err) {
			slog.Warn("superagent driver: finish takeover failed", "takeover_id", id, "error", err)
		}
		return
	}
	publishTakeoverEvent(d.hub, eventTakeoverEnded, id, sessionID, userID, status)
}

// markSkippedBusy 记录"这一拍因为会话正忙而放弃"，供配置页显示。
//
// 写 last_status 而不是 stop_reason：它描述的是"上一拍的结果"，接管仍在进行中，
// 写进 stop_reason 会让人以为接管已经因为忙碌而终止了。
func (d *takeoverDriver) markSkippedBusy(ctx context.Context, id string) {
	if _, err := db.GlobalDBManager.Exec(ctx, `
		UPDATE superagent_takeovers SET last_status = 'skipped_busy'
		 WHERE id = $1 AND status = 'active'`, id); err != nil {
		slog.Warn("superagent driver: mark skipped_busy failed", "takeover_id", id, "error", err)
	}
}
