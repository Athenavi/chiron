package api

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/billing"
	"github.com/athenavi/chiron/internal/db"
)

// P1-1 接线的执行半边：投递回合怎么起、收尾时怎么续下一条。
//
// 与 `thread_delivery.go`（入队/认领/收尾三件事）分开：那一层只跟表打交道，
// 这一层只跟回合打交道。两者通过 `threadDelivery` 这一个值交接。
//
// 三个硬约束（roadmap P1-1.2 已记）在这里各落一处：
//  ① 入口处已持锁 ⇒ 第一条**原地直跑**（`launchThreadTurn` 接受现成资源），
//     不先入队再 drain 一轮。
//  ② 续跑必须发生在**释放运行锁之后**，且用**未取消的父 ctx** —— 见 `launchThreadTurn`
//     的 defer 顺序与其中每一处 `context.WithoutCancel`。
//  ③ 一次只续一条，靠下一条自己的收尾再续 ⇒ 不写循环（回合是异步的，循环等不到它）。

// threadTurnDeps 是起一个投递回合所需的全部外部依赖。
//
// 显式传值而不是包级变量：续跑会从多个入口的后台 goroutine 里跑，靠全局可变状态
// 交接依赖会让测试之间互相污染，也让"这个回合用的是哪套信号量"变成读代码时答不上来的问题。
type threadTurnDeps struct {
	submit        *SubmitHandler
	billingMgr    *billing.Manager
	agentSem      *SharedSemaphore
	tenantResMgr  *TenantResourceManager
	submitTimeout time.Duration
}

// threadTurnResources 是一个投递回合已占用的资源，由起跑方负责归还。
//
// 单独成型是因为资源有两个来源：`startThreadTurn` 自己抢，或事件入口**已经**抢好了
// （那里为了判断"会话是否忙"必须先拿锁）。后者的正确做法是把持有转交出去，
// 而不是先还掉再抢一次 —— 那一瞬间别的请求能挤进来，排队语义就断了。
type threadTurnResources struct {
	runToken      string
	releaseRun    func()
	releaseSem    func()
	releaseTenant func()
	// reserved 是已预留的租户 token 量；0 表示还没预留，由起跑方自己算。
	reserved int64
}

// preparedThreadTurn 是通过校验、可以直接起跑的一条投递。
type preparedThreadTurn struct {
	body    agentFollowupPayload
	kind    followupKind
	content string
}

// prepareThreadTurn 做所有"起跑前就能判死"的检查，并把正文拼好。
//
// 单独拆出来是为了让调用方**在占资源之前**就能否掉坏数据：一条解不开的投递
// 不该先抢锁、抢信号量、扣配额再失败，那会让队列里的坏行反复消耗全局资源。
// 任何一条被判死时这里就已把投递落到终态（dropped 或 done），调用方不必再收尾。
func prepareThreadTurn(parent context.Context, deps threadTurnDeps, d threadDelivery) (preparedThreadTurn, bool) {
	// 收尾/写库一律用未取消的 ctx：调用点常在回合的收尾 defer 上，那里的 ctx 已结束。
	finalCtx := context.WithoutCancel(parent)

	body, decErr := followupFromSnapshot(d)
	if decErr != nil {
		slog.Warn("thread delivery: payload undecodable, dropped",
			"thread", d.ThreadID, "event", d.EventKey, "error", decErr)
		finishThreadDelivery(finalCtx, d, threadDeliveryDropped)
		return preparedThreadTurn{}, false
	}
	kind, known := lookupFollowupKind(d.SourceKind)
	if !known {
		// 入队时已校验过 source_kind ∈ followupKinds，这里再查一次是因为表里可能
		// 留着更早版本写入的别的值。查不到就别跑：`kind.build` 为 nil 会 panic。
		slog.Warn("thread delivery: unknown source kind, dropped",
			"thread", d.ThreadID, "source_kind", d.SourceKind)
		finishThreadDelivery(finalCtx, d, threadDeliveryDropped)
		return preparedThreadTurn{}, false
	}
	// 开关在投递时**再判一次**：入队时用户可能开着，等到真跑时已经关了。
	// 开关的意义是"别在我不要的时候自己烧钱"，所以以跑之前的这一刻为准。
	if kind.gate != nil && !kind.gate(finalCtx, body.UserID) {
		finishThreadDelivery(finalCtx, d, threadDeliveryDone)
		return preparedThreadTurn{}, false
	}
	if body.SessionID == "" || body.UserID == "" {
		// 会话或用户没了（会话被删 / 行被搬走）：投不进去，但**不是**要重试的情况。
		slog.Warn("thread delivery: row without session or user, dropped",
			"thread", d.ThreadID, "event", d.EventKey)
		finishThreadDelivery(finalCtx, d, threadDeliveryDropped)
		return preparedThreadTurn{}, false
	}
	// 计费闸门放在这里而不是只在事件入口：队列里的条目可能**晚很久**才被续跑，
	// 那时用户的余额早就不一样了。只在入口判一次，续跑就成了绕过余额检查的后门。
	if !threadTurnAffordable(finalCtx, deps.billingMgr, body.UserID) {
		slog.Info("thread delivery: insufficient credits, skipped",
			"thread", d.ThreadID, "user_id", body.UserID)
		finishThreadDelivery(finalCtx, d, threadDeliveryDone)
		return preparedThreadTurn{}, false
	}
	return preparedThreadTurn{body: body, kind: kind, content: kind.build(body)}, true
}

// threadTurnAffordable 判"这个用户还付得起一轮吗"。
//
// 与 `/v1/agent/followup` 原先那段判断同源（免费额度用完 + 余额为零 ⇒ 别唤起），
// 抽成函数是为了让入口与续跑用同一份口径 —— 两处各写一份，迟早一边改了另一边没改。
// fail-open 而非 fail-closed：读不到用量时不该因此停掉整条通道，
// 真正防止烧穿账单的是租户配额那一层（它是硬闸门）。
func threadTurnAffordable(ctx context.Context, mgr *billing.Manager, userID string) bool {
	if mgr == nil {
		return true
	}
	count, err := mgr.DailyFreeCount(ctx, userID)
	if err != nil || count >= billing.DailyFreeLimit {
		if balance, balErr := mgr.GetBalance(userID); balErr == nil && balance <= 0 {
			return false
		}
	}
	return true
}

// reserveThreadTurn 为一条投递回合预留租户 token 额度，返回预留量。
//
// 与 `/submit` 同一套语义：按估算预留，回合末由 submit_handler 校正成真实用量。
// 自动轮此前完全绕过租户池，这里补上 —— 子 Agent 的自动总结同样烧租户的钱。
func reserveThreadTurn(ctx context.Context, d threadDelivery, prep preparedThreadTurn) (int64, error) {
	return EnforceTenantQuota(ctx,
		&auth.Claims{UserID: prep.body.UserID, TenantID: d.TenantID},
		EstimateRequestTokens(prep.content, nil))
}

// startThreadTurn 自行抢占运行锁与并发资源后起跑一条投递，收尾时接着续队列。
//
// 返回 false 表示这次没跑起来（忙 / 无额度 / 载荷坏）。除了"锁被占"与"配额耗尽"
// 这两种**等一会儿再来就有意义**的情形，其余都已把投递落到终态。
func startThreadTurn(parent context.Context, deps threadTurnDeps, d threadDelivery) bool {
	base := context.WithoutCancel(parent)
	// 先判死再抢锁：一条解不开的投递不该先占住锁与信号量再失败，
	// 那会让坏行反复消耗全局资源，还把同会话的用户请求挡在锁外面。
	prep, ok := prepareThreadTurn(base, deps, d)
	if !ok {
		return false
	}

	runToken := newThreadRunToken()
	releaseRun, locked, lockErr := AcquireSessionRunLock(base, prep.body.SessionID, runToken)
	if lockErr != nil {
		// 锁不可用（Redis 挂）：事件**留在 queued**，等下一个收尾方或进程恢复再试。
		// 判 dropped 会把一件完全没试过的事记成终态，那是不可逆的信息损失。
		slog.Warn("thread delivery: run lock unavailable",
			"session", prep.body.SessionID, "error", lockErr)
		return false
	}
	if !locked {
		// 会话正忙：留在队里，由当前持有者在收尾时 drain。这与用户提交被拒要立刻
		// 告诉人不同 —— 后台事件本来就没人盯，排队才是对的（roadmap P1-1.1 第 4 条）。
		slog.Debug("thread delivery: session busy, event stays queued",
			"session", prep.body.SessionID)
		return false
	}
	return acquireAndLaunch(base, deps, d, prep, runToken, releaseRun)
}

// startThreadTurnWithLock 是约束Ⅰ的落点：事件入口**已经**抢到运行锁时的起跑路径。
//
// 入口必须先拿锁才能判断"会话是否忙"，所以它手里那把锁是判断的副产物。正确做法是把
// 持有**转交**给这里，而不是先还掉再抢一次 —— 那一瞬间别的请求能挤进来，
// "先到先排"的语义就断了，而且多一次抢锁就多一次失败窗口。
//
// `releaseRun` 的所有权在调用本函数后归本函数：无论成败都由它归还，
// 调用方不得再碰（重复释放会 panic 或提前解锁）。
func startThreadTurnWithLock(parent context.Context, deps threadTurnDeps, d threadDelivery,
	runToken string, releaseRun func()) bool {
	base := context.WithoutCancel(parent)
	prep, ok := prepareThreadTurn(base, deps, d)
	if !ok {
		// 判死也要还锁：否则本会话被一把空锁挡到 TTL（5min），后续请求一律 409。
		releaseRun()
		return false
	}
	return acquireAndLaunch(base, deps, d, prep, runToken, releaseRun)
}

// acquireAndLaunch 在**已持有运行锁**的前提下抢并发资源与配额，然后起跑。
//
// 任何一步失败都会把锁还掉：这把锁的持有者必须是"真要跑的那一刻"，悬空持有会让
// 同会话的用户请求在 5 分钟里一直撞 409。
func acquireAndLaunch(base context.Context, deps threadTurnDeps, d threadDelivery,
	prep preparedThreadTurn, runToken string, releaseRun func()) bool {
	releaseSem, semOK := deps.agentSem.TryAcquire(base)
	if !semOK {
		releaseRun()
		return false
	}
	var releaseTenant func()
	if deps.tenantResMgr != nil {
		var tenantOK bool
		releaseTenant, tenantOK = deps.tenantResMgr.Acquire(tenantStr(d.TenantID))
		if !tenantOK {
			releaseSem()
			releaseRun()
			return false
		}
	}

	reserved, quotaErr := reserveThreadTurn(base, d, prep)
	if quotaErr != nil {
		slog.Warn("thread delivery: tenant token quota exceeded",
			"session", prep.body.SessionID, "thread", d.ThreadID)
		if releaseTenant != nil {
			releaseTenant()
		}
		releaseSem()
		releaseRun()
		// 配额耗尽时**不丢**：配额恢复后这条还该被投递。留在 running 会卡住队列，
		// 所以退回 queued 等下一次收尾重试（堆积由 maxPendingThreadEvents 兜住）。
		requeueThreadDelivery(base, d)
		return false
	}

	return launchThreadTurn(base, deps, d, prep, threadTurnResources{
		runToken:      runToken,
		releaseRun:    releaseRun,
		releaseSem:    releaseSem,
		releaseTenant: releaseTenant,
		reserved:      reserved,
	})
}

// launchThreadTurn 在资源已就位的前提下起跑，并排好收尾链。
//
// 调用前提：`res` 里的锁/信号量/租户槽位**已由调用方持有**，且所有权随之转交到这里 ——
// 本函数负责归还，调用方不得再碰。这样约束Ⅰ（入口原地直跑）与约束②（收尾后 drain）
// 共用同一条收尾链，不会出现"入口那条忘了 drain"或"归还顺序两处不同"。
func launchThreadTurn(parent context.Context, deps threadTurnDeps, d threadDelivery,
	prep preparedThreadTurn, res threadTurnResources) bool {
	base := context.WithoutCancel(parent)

	// 外层只是**兜底**，不是"这一轮该跑多久"的判据。
	//
	// 判据在 `submitOnce` 的空闲看门狗：只要引擎还在产出事件就续命，连续无事件
	// 才判定卡死。这里若用 `DefaultAgentTimeout`（300s，那是**空闲窗口**）当总时长，
	// 正常的长任务（几十轮工具调用本就要跑十几分钟）必然被拦腰砍断 —— 队列里的
	// 后续投递也跟着一起死。
	timeout := deps.submitTimeout
	if timeout < LongTurnCeiling {
		timeout = LongTurnCeiling
	}
	runCtx, cancel := context.WithTimeout(base, timeout)
	runCtx = WithQuotaReserve(runCtx, res.reserved)
	stopHeartbeat := StartSessionRunLockHeartbeat(runCtx, prep.body.SessionID, res.runToken)
	markThreadTurn(prep.body.SessionID, d.EventKey)
	stopTurnBeat := startThreadTurnHeartbeat(runCtx, prep.body.SessionID, d.EventKey)

	go func() {
		// defer 是后进先出，这里按"谁先该停"排：先落终态与停心跳，再还资源，
		// 最后才续队列 —— 续队列必须看到锁已经还掉，否则它抢锁必然失败。
		// 约束②的落点就是这一行：`drainNextThreadDelivery` 声明在 `releaseAll` **之前**，
		// 所以它执行在归还之后；且用 `context.WithoutCancel(parent)` 而不是 `runCtx`。
		defer func() {
			if rec := recover(); rec != nil {
				slog.Error("thread delivery turn panicked",
					"thread", d.ThreadID, "event", d.EventKey, "panic", rec)
			}
		}()
		defer drainNextThreadDelivery(base, deps, prep.body.SessionID)
		defer clearThreadTurn(prep.body.SessionID, d.EventKey)
		defer cancel()
		defer stopTurnBeat()
		defer stopHeartbeat()
		defer func() {
			if res.releaseTenant != nil {
				res.releaseTenant()
			}
			res.releaseSem()
			res.releaseRun()
		}()
		defer func() {
			// 回合无论成败都落终态：留在 running 会让队列永远卡在这条上。
			finishThreadDelivery(base, d, threadDeliveryDone)
		}()

		deps.submit.HandleSubmit(runCtx, prep.body.UserID, prep.body.SessionID, prep.content,
			map[string]interface{}{}, map[string]interface{}{prep.kind.ctxKey: prep.body.RunID}, nil)
	}()
	return true
}

// requeueThreadDelivery 把一条已认领（running）但没跑成的投递退回 queued。
//
// 只有"等一会儿再来就有意义"的原因才该退回来（配额耗尽、锁不可用）；坏数据退回来
// 只会无限重投同一条，那种要 dropped 并写原因。
func requeueThreadDelivery(ctx context.Context, d threadDelivery) {
	if db.GlobalDBManager == nil {
		return
	}
	if _, err := db.GlobalDBManager.Exec(ctx,
		`UPDATE thread_deliveries SET status = 'queued'
		  WHERE thread_id = $1 AND event_key = $2 AND status = 'running'`,
		d.ThreadID, d.EventKey); err != nil {
		slog.Warn("thread delivery: requeue failed",
			"thread", d.ThreadID, "event", d.EventKey, "error", err)
	}
}

// drainNextThreadDelivery 在会话空闲时领走下一条待处理事件并起跑。
//
// 只领**一条**：跑完那条的收尾会再调一次本函数，队列因此自然清空。写成循环会要求
// "等这个回合结束再取下一轮"，而回合是异步的 —— 循环等不到它，只能忙等或提前开跑。
//
// 多个回合同时收尾时靠 `threadTurnsInFlight` 收敛：集合非空说明还有别人的回合没结束，
// 这一拍不推进；等那个回合收尾时自然会轮到它。这样即使两处同时收尾，
// 也只会有一条走到底（另一条会因集合非空直接返回）。
func drainNextThreadDelivery(ctx context.Context, deps threadTurnDeps, sessionID string) {
	if sessionID == "" || deps.submit == nil || db.GlobalDBManager == nil {
		return
	}
	owners, err := threadTurnsInFlight(ctx, sessionID)
	if err != nil {
		// 读不到就不推进：宁可晚一拍，也不要在"不知道还有谁在跑"的时候起新回合。
		slog.Warn("thread delivery: in-flight set unreadable, drain skipped",
			"session", sessionID, "error", err)
		return
	}
	if len(owners) > 0 {
		return
	}

	d, claimErr := claimNextThreadDelivery(ctx, sessionID)
	if claimErr != nil {
		if errors.Is(claimErr, errNoPendingDelivery) {
			return // 队列空了：最常见的一条路径，不是错误
		}
		slog.Warn("thread delivery: claim failed", "session", sessionID, "error", claimErr)
		return
	}
	startThreadTurn(ctx, deps, d)
}

// newThreadRunToken 生成本次投递回合的运行锁归属 token。
//
// 前缀与其它入口区分开：排查"谁持有这把锁"时，前缀就是唯一线索。
// 计数器保证同一纳秒内两次调用也不撞（rand 失败时仍有区分度）。
func newThreadRunToken() string {
	var rnd [12]byte
	_, _ = rand.Read(rnd[:])
	threadRunTokenSeq++
	return fmt.Sprintf("thread-%s-%d", hex.EncodeToString(rnd[:]), threadRunTokenSeq)
}

var threadRunTokenSeq uint64
