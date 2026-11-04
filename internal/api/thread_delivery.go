package api

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"

	"github.com/athenavi/chiron/internal/db"
)

// P1-1 投递层（roadmap P1-1.1）。
//
// 裁决是**串行排队**：同一会话短时间收到多个 thread 事件时进队列，跑完一个接着下一个。
// 执行侧一行不改 —— `agent:run-lock:<session_id>` 仍是唯一的并发护栏，所以这一层
// 只做两件事：入站时决定能不能进队（`decideThreadDelivery`），收尾时领走下一条
// （`claimNextThreadDelivery`）。
//
// 两半缺一不可：只有入队没有认领，队列只会越长越长，因为清空它的唯一时机
// （锁已自由 + 会话空闲）只有收尾方知道。

// maxPendingThreadEvents 是每会话待处理上限。
//
// 排队必须有个显式上限，否则它会在没人注意的地方变成无限队列 —— 事件早已发生、
// 很久之后才生效，用户看到的是系统反应迟钝。20 这个量级的依据是：一个 run 可能
// 积压的事件就在这个数量级，再多说明这个会话根本消化不完，继续攒只会把延迟推高。
//
// 超限时**落 dropped 并写审计**，不静默丢。静默丢是最糟的一种：事件消失了，
// 队列长度看着正常，没人知道少了什么 —— 而审计能让"少了一条"这件事可被追问。
const (
	maxPendingThreadEvents = 20

	// threadDeliveryDroppedAudit 是超限丢弃的审计动作名。
	// 与 `superagent.thread.*` 同一命名空间：都是"系统替用户决定了一件事"。
	threadDeliveryDroppedAudit = "superagent.thread.dropped_overflow"
)

// threadDeliveryDecision 是一次入站事件该落到哪个状态。
type threadDeliveryDecision string

const (
	threadDeliverQueued  threadDeliveryDecision = "queued"
	threadDeliverDropped threadDeliveryDecision = "dropped"
)

// decideThreadDelivery 判断新事件能不能进队。
//
// `pending` 是**该会话**当前待处理数（`status='queued'`），不含正在跑的那一条：
// running 已经占着运行锁，它不需要排队位，把它算进来会让上限随并发数漂移。
//
// 判据是纯函数而不是内嵌在 SQL 里，理由与 `budgetExhausted` 相同：
// "20 到底能不能进"必须有一个唯一、可测的答案。写成 `>=` 而不是 `>` 也是有意的 ——
// 上限 20 意味着最多 20 条在等，第 21 条就该被拒；写成 `>` 会变成 21 条。
func decideThreadDelivery(pending int) threadDeliveryDecision {
	if pending < 0 {
		// 负数是调用方算错了（多半是并发读到了不一致的快照）。此时**不丢**
		// 是更安全的一侧：丢事件不可逆，多排一条只是延迟。
		return threadDeliverQueued
	}
	if pending >= maxPendingThreadEvents {
		return threadDeliverDropped
	}
	return threadDeliverQueued
}

// threadDeliveryStatus 是投递行（`thread_deliveries.status`）的取值。
//
// 四个值构成一条单向链：queued → running → done | dropped。没有 running → queued
// 的回退：认领即开始，跑失败就把那条落到 dropped 并写明原因，而不是放回去重来 ——
// 重来会撞上同一条坏数据，无限重投。
const (
	threadDeliveryQueued  = "queued"
	threadDeliveryRunning = "running"
	threadDeliveryDone    = "done"
	threadDeliveryDropped = "dropped"
)

// threadDelivery 是从队列里领到的一条待处理事件。
//
// 只带投递层自己要用的两个键：内容怎么组装由事件入口决定（`followupKinds.build`），
// 投递层不解析正文 —— 存正文就意味着存一份会过期的副本。
type threadDelivery struct {
	ThreadID string
	EventKey string
}

// errNoPendingDelivery 表示该会话队列已空。**不是**错误情况，调用方照常收尾即可。
var errNoPendingDelivery = errors.New("no pending thread delivery")

// claimNextThreadDelivery 原子地领走该会话最早的一条待处理事件，并置为 running。
//
// 并发安全靠 `FOR UPDATE SKIP LOCKED`：多副本同时收尾同一个会话时（理论上运行锁让这
// 不该发生，但锁可能刚过期），第二个副本会跳过已被人锁住的那行而不是阻塞等它 ——
// 等锁会把一次收尾拖成一次超时。
//
// 排序用 `(created_at, thread_id, event_key)` 而不是只按 `created_at`：`now()` 在
// 同一事务里是同一个值，同一批入队的事件时间戳会完全相同，只按时间排序的结果是
// 不确定的 —— 事后看"为什么这条先跑"就没有答案。补上主键两列让它成为全序。
//
// 调用前提：调用方**已持有** `agent:run-lock:<session_id>`。本函数不抢锁 —— 抢锁失败
// 时要保留队列（留在 queued），那是入口侧的事，不在这里叠一层。
func claimNextThreadDelivery(ctx context.Context, sessionID string) (threadDelivery, error) {
	const claimSQL = `
		UPDATE thread_deliveries d
		   SET status = 'running'
		 WHERE (d.thread_id, d.event_key) = (
		        SELECT td.thread_id, td.event_key
		          FROM thread_deliveries td
		         WHERE td.session_id = $1
		           AND td.status = 'queued'
		         ORDER BY td.created_at, td.thread_id, td.event_key
		         LIMIT 1
		           FOR UPDATE SKIP LOCKED)
		   AND d.status = 'queued'
		RETURNING d.thread_id, d.event_key`

	// 必须走写池：`QueryRow` 用的是只读池，在读写分离部署下 UPDATE 会直接失败。
	tx, err := db.GlobalDBManager.Begin(ctx)
	if err != nil {
		return threadDelivery{}, err
	}
	// 未 Commit 时 Rollback 是无害的，这条 defer 是失败路径的唯一兜底。
	defer func() { _ = tx.Rollback(ctx) }()

	var d threadDelivery
	if scanErr := tx.QueryRow(ctx, claimSQL, sessionID).Scan(&d.ThreadID, &d.EventKey); scanErr != nil {
		if errors.Is(scanErr, pgx.ErrNoRows) {
			return threadDelivery{}, errNoPendingDelivery
		}
		return threadDelivery{}, scanErr
	}
	if commitErr := tx.Commit(ctx); commitErr != nil {
		return threadDelivery{}, commitErr
	}
	return d, nil
}

// finishThreadDelivery 把一条已认领的投递落到终态。
//
// `WHERE status = 'running'` 是有意的：收尾可能被重复调用（回合结束 + 进程退出清理），
// 没有这个条件时第二次调用会把已经 `done` 的行改回去，审计上就出现"跑完了又变成
// dropped"这种自相矛盾的记录。带上条件的 UPDATE 天然幂等。
//
// 会话被删的情况也用 dropped 表达：`session_id` 故意没有外键（见 0021 迁移），
// 悬空行靠 status 说明，投递历史本身要留着当证据。
func finishThreadDelivery(ctx context.Context, d threadDelivery, status string) error {
	if status != threadDeliveryDone && status != threadDeliveryDropped {
		// 静默接受非法终态会让"跑完了"和"没跑"混成一列，宁可让调用方在测试里炸掉。
		return errors.New("finishThreadDelivery: status must be done or dropped")
	}
	_, err := db.GlobalDBManager.Exec(ctx,
		`UPDATE thread_deliveries SET status = $1
		  WHERE thread_id = $2 AND event_key = $3 AND status = 'running'`,
		status, d.ThreadID, d.EventKey)
	return err
}
