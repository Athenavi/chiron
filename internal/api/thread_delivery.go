package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"

	"github.com/google/uuid"
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
	// threadDeliverDuplicate 表示这条事件**之前已经落过行**（多副本重投 / 引擎重试）。
	//
	// 与 queued 分开是必需的：入队方据此知道"不该再为它起一个回合"。
	// 混在 queued 里的话，同一事件重投会让队列里那条已经跑过的事件被再跑一次 ——
	// 用户看到两条同样的"子任务完成"，还白花一份 token。
	threadDeliverDuplicate threadDeliveryDecision = "duplicate"
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
	// Payload 是事件到达那一刻的输入快照（`thread_deliveries.payload`）。
	//
	// 必须随行一起取回：引擎回投是一次性 HTTP 请求，`status` / `profile` / `summary`
	// 不会在别处留底，光凭键拼不出投递正文 —— 那会变成"能入队但投递内容为空"。
	Payload json.RawMessage

	// 以下字段来自 `threads`（JOIN 出来），让认领方不必再查一次就能重建投递。
	//
	// 认领方要做的是"把这个事件当成一次 HandleSubmit"：它需要知道投给谁
	// （user/tenant）、投到哪个会话、要不要用 `job` 那条通道（source_kind 决定
	// 正文拼法与 workbenchCtx 标记键）、以及事件自身的标识（external_key 是
	// 幂等键的一部分，也是 `read_subagent_result` / `job_output` 认的那个 id）。
	UserID      string
	TenantID    int16
	SessionID   string
	SourceKind  string
	ExternalKey string
}

// errNoPendingDelivery 表示该会话队列已空。**不是**错误情况，调用方照常收尾即可。
var errNoPendingDelivery = errors.New("no pending thread delivery")

// threadDeliveryRoutedAudit 是每次入站都会写的一条路由埋点（roadmap §6 的那组对照数据）。
//
// 记在**每一次**投递上而不是只在"被订阅改投"时记：要对比"有订阅 / 无订阅两种配置下的
// 决定数"，缺了无订阅那一半就没有分母，只剩一个百分比而没有总量。
const threadDeliveryRoutedAudit = "superagent.thread.routed"

// resolveThreadTarget 按订阅决定这条事件投给哪个会话。
//
// 返回 `(最终会话, 是否由订阅决定)`。
//
// 语义按 2026-11-04 的兼容取舍：**有 active 订阅则改投订阅指向的会话；无订阅则维持
// 入口给的会话**。roadmap 投递路径第 2 步写的是"无订阅则该事件到此为止"，但库里
// 此刻一条订阅都没有，照字面做等于让现存的子 Agent / job 回投全部静默停摆 ——
// 那不是安全的默认值，是停机。等订阅在真实使用中出现、对照埋点也拿到了数据，
// 再单独一批收紧成"无订阅即止"。
//
// 多条 active 订阅时取最早创建的一条：同一 thread 对多个会话同时订阅意味着一条事件
// 要开多个回合，而执行侧是**串行**的（`agent:run-lock:<session>` 唯一）。此刻挑一条
// 确定性地投，比按 map 顺序随机选一条可解释得多。
//
// 查询失败按"无订阅"处理（fail-open 到今日行为）并记日志：订阅是**新增**的路由层，
// 它自己坏掉不该把一条本来能投的事件变成投不出去。这里与"读不到配置当未开启"的
// fail-closed 不冲突 —— 那条是权限开关，这条是路由；路由失败退回旧路径不放大权限。
func resolveThreadTarget(ctx context.Context, tx pgx.Tx, threadID, entrySession string) (string, bool) {
	var target string
	err := tx.QueryRow(ctx, `
		SELECT session_id FROM thread_subscriptions
		 WHERE thread_id = $1 AND status = 'active'
		 ORDER BY created_at ASC, id ASC
		 LIMIT 1`, threadID).Scan(&target)
	if errors.Is(err, pgx.ErrNoRows) {
		return entrySession, false
	}
	if err != nil {
		slog.Warn("thread delivery: subscription lookup failed, falling back to entry session",
			"thread", threadID, "session", entrySession, "error", err)
		return entrySession, false
	}
	if target == "" {
		// 空值只可能来自坏数据，仍按未订阅处理。
		return entrySession, false
	}
	return target, true
}

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
	// 用 CTE 先把行改成 running，再 JOIN `threads` 取回重建投递所需的字段。
	// 不能把 JOIN 塞进 UPDATE 的 FROM 里再 RETURNING：那样返回的是 JOIN 后的列，
	// 但「哪一行被更新」与「返回哪一行」在多表 UPDATE 下不保证一一对应；
	// 先 RETURNING 再 JOIN 是确定的一对一。
	const claimSQL = `
		WITH claimed AS (
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
		    RETURNING d.thread_id, d.event_key, d.payload, d.session_id
		)
		SELECT c.thread_id, c.event_key, c.payload, c.session_id,
		       t.user_id, t.tenant_id, t.source_kind, t.external_key
		  FROM claimed c
		  JOIN threads t ON t.id = c.thread_id`

	// 必须走写池：`QueryRow` 用的是只读池，在读写分离部署下 UPDATE 会直接失败。
	tx, err := db.GlobalDBManager.Begin(ctx)
	if err != nil {
		return threadDelivery{}, err
	}
	// 未 Commit 时 Rollback 是无害的，这条 defer 是失败路径的唯一兜底。
	defer func() { _ = tx.Rollback(ctx) }()

	var d threadDelivery
	if scanErr := tx.QueryRow(ctx, claimSQL, sessionID).Scan(
		&d.ThreadID, &d.EventKey, &d.Payload, &d.SessionID,
		&d.UserID, &d.TenantID, &d.SourceKind, &d.ExternalKey,
	); scanErr != nil {
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
	if db.GlobalDBManager == nil {
		// 与 requeueThreadDelivery 同一处置：收尾链跑在没有数据库的环境里（单测、
		// 只读网关）不该 panic。把一条已经跑完的投递标成 done 失败不改变任何结论。
		return nil
	}
	_, err := db.GlobalDBManager.Exec(ctx,
		`UPDATE thread_deliveries SET status = $1
		  WHERE thread_id = $2 AND event_key = $3 AND status = 'running'`,
		status, d.ThreadID, d.EventKey)
	return err
}

// payloadArg 把可能为空的快照转成列可接受的参数。
//
// 列是 `jsonb NOT NULL`，直接传 nil 会撞约束 —— 但"调用方没给额外输入"是正常情形
// （迁移里默认值就是 `{}`）。这里统一成空对象，让调用方不必为这一列单独判空。
func payloadArg(p json.RawMessage) json.RawMessage {
	if len(bytes.TrimSpace(p)) == 0 {
		return json.RawMessage(`{}`)
	}
	return p
}

// threadEnqueueParams 是一次入站事件要落的全部信息。
//
// 存的是**快照**而不是"投递时再按 kind 重拼"：引擎回投是一次性 HTTP 请求，
// 调用方手里的 `status` / `profile` / `summary` 除了这条请求之外不在别处存在。
// 早先那版设计假设正文可以在投递时重算，实际到 drain 时根本拼不出来，
// 所以这里必须落输入快照。
type threadEnqueueParams struct {
	TenantID    int16
	UserID      string
	SourceKind  string // 必须 ∈ followupKinds（subagent / job），刻意不含 webhook
	ExternalKey string // 上游幂等键；空串表示内部触发，不参与去重
	SessionID   string
	EventKey    string // 同一 thread 下这条事件的幂等键
	// Payload 是事件到达那一刻的输入快照，投递时用来拼正文。
	Payload json.RawMessage
}

// followupSnapshot 把一份事件载荷压成投递行里的快照。
//
// 快照类型直接复用 `agentFollowupPayload`，不另立一个等价类型：投递时拼正文的
// `followupKinds.build` 要的就是它，多一层只会多一处要同步的字段。
//
// 编解码放在投递层而不是入口 handler 里，是为了让"存进去的"与"取出来的"挨着写 ——
// 两边的字段一旦漂移，只有并排看这两个函数时才发现得了。
func followupSnapshot(p agentFollowupPayload) (json.RawMessage, error) {
	raw, err := json.Marshal(p)
	if err != nil {
		return nil, err
	}
	return raw, nil
}

// followupFromSnapshot 是 followupSnapshot 的逆运算，并用 thread 行的权威字段覆盖身份。
//
// 覆盖而不是"空则补"：`user_id` / `tenant_id` / `session_id` 是运行锁、配额与计费的键，
// 必须以行为准 —— 快照是历史，行是现在的所有者。两者分歧时信行，否则一条老快照
// 能把额度记到已经搬走的租户头上。
//
// `Kind` 同样以 `source_kind` 为准：入队时它已对过 `followupKinds`，而快照里的 Kind
// 可能是空串（滚动升级期间旧引擎不带这个字段）。
func followupFromSnapshot(d threadDelivery) (agentFollowupPayload, error) {
	var p agentFollowupPayload
	if raw := bytes.TrimSpace(d.Payload); len(raw) > 0 {
		if err := json.Unmarshal(raw, &p); err != nil {
			return agentFollowupPayload{}, err
		}
	}
	p.SessionID = d.SessionID
	p.UserID = d.UserID
	p.TenantID = tenantStr(d.TenantID)
	p.Kind = d.SourceKind
	return p, nil
}

// errUnknownThreadSource 表示 source_kind 没在 followupKinds 里注册。
//
// 单独一个哨兵而不是复用"未知 kind 就 dropped"：调用方传错来源是**编程错误**，
// 应当让它炸在测试里；而"未知来源但已上线"是运行时输入问题，那条走 dropped + 审计。
var errUnknownThreadSource = errors.New("thread: source_kind not registered in followupKinds")

// enqueueThreadDelivery 把一条事件写进队列，并按准入判据决定它的初始状态。
//
// 返回的 `threadID` 在丢弃时也要返回：审计要指明"是哪条 thread 的第几个事件被丢了"，
// 只有动作名而定位不到具体事件的话，事后追问时答不上来。
//
// 幂等分两层：`threads_idem` 让同一 (租户, 来源, external_key) 只对应一条 thread；
// `thread_deliveries` 的主键让同一 (thread, event_key) 只落一行。第二次入站走
// `DO NOTHING` 且**不复活**已 dropped 的行 —— 丢弃是终态，重投不该把它捞回来。
func enqueueThreadDelivery(ctx context.Context, p threadEnqueueParams) (threadDeliveryDecision, string, error) {
	if _, known := followupKinds[p.SourceKind]; !known {
		return threadDeliverDropped, "", errUnknownThreadSource
	}

	tx, err := db.GlobalDBManager.Begin(ctx)
	if err != nil {
		return threadDeliverDropped, "", err
	}
	defer func() { _ = tx.Rollback(ctx) }()

	// 取或建 thread 必须**先于**待处理计数：订阅是挂在 thread 上的，而"投给哪个会话"
	// 决定了要数谁的队列。原来的顺序是先数再建 thread，一旦按订阅改投目标，
	// 就会数错会话的 pending —— 上限按会话计，数错等于上限失效。
	//
	// `DO UPDATE SET user_id = threads.user_id` 是刻意的空更新：只有带了 DO UPDATE，
	// 冲突时 RETURNING 才会返回**已存在**的那行 id；换成 DO NOTHING 时冲突不返回任何行，
	// 调用方还得再查一次。谓词必须与 `threads_idem` 的定义逐字一致，
	// 否则 Postgres 推断不到那条部分索引。
	const upsertThreadSQL = `
		INSERT INTO threads (id, tenant_id, user_id, source_kind, external_key)
		VALUES ($1, $2, $3, $4, $5)
		ON CONFLICT (tenant_id, source_kind, external_key) WHERE external_key <> ''
		DO UPDATE SET user_id = threads.user_id
		RETURNING id`
	var threadID string
	if scanErr := tx.QueryRow(ctx, upsertThreadSQL,
		uuid.NewString(), p.TenantID, p.UserID, p.SourceKind, p.ExternalKey,
	).Scan(&threadID); scanErr != nil {
		return threadDeliverDropped, "", scanErr
	}

	// 订阅解析（roadmap 投递路径第 2 步，按 2026-11-04 的兼容取舍落在 A 方案）：
	// 有 active 订阅则按订阅改投目标会话；无订阅**维持今日行为**（投给入口给的会话），
	// 并打一条 no-subscription 埋点。严格照文档做"无订阅即止"会让现存的子 Agent /
	// job 回投全部静默停摆 —— 库里此刻一条订阅都没有，那不是安全的默认值，是停机。
	//
	// 埋点必须先于"投不投"的结论产生：§6 要的对照数据是"有订阅 / 无订阅两种配置下的
	// 决定数"，如果只在有订阅时记，无订阅的那一半永远没有分母。
	sessionID, subscribed := resolveThreadTarget(ctx, tx, threadID, p.SessionID)
	db.AuditLog(ctx, p.UserID, p.TenantID, threadDeliveryRoutedAudit,
		"thread:"+threadID, "thread delivery routed", "",
		map[string]interface{}{
			"subscribed":     subscribed,
			"event_key":      p.EventKey,
			"source_kind":    p.SourceKind,
			"entry_session":  p.SessionID,
			"target_session": sessionID,
		})
	if sessionID != p.SessionID {
		slog.Info("thread delivery: routed by subscription",
			"thread", threadID, "from", p.SessionID, "to", sessionID)
	}

	// 计数与插入必须在同一个临界区里。两个副本各自读到 pending=19 然后双双插入，
	// 上限就从 20 变成 21 —— 而这种竞态只在高负载时出现，靠测试很难复现，只能从
	// 结构上消掉。advisory lock 用 hashtext 压成 32 位，键里带**最终**会话 ID
	// （订阅改投之后的那一个），否则两个不同入口投到同一会话时会各拿一把锁，
	// 计数照样能同时读到 19。
	// 与 superagent.go 的 startTx 同一手法（那里按用户 ID 加锁）。
	if _, lockErr := tx.Exec(ctx,
		`SELECT pg_advisory_xact_lock(hashtext($1))`, "thread-enqueue:"+sessionID); lockErr != nil {
		return threadDeliverDropped, "", lockErr
	}

	var pending int
	if countErr := tx.QueryRow(ctx,
		`SELECT count(*) FROM thread_deliveries
		  WHERE session_id = $1 AND status = 'queued'`, sessionID).Scan(&pending); countErr != nil {
		return threadDeliverDropped, "", countErr
	}

	decision := decideThreadDelivery(pending)
	status := threadDeliveryQueued
	if decision == threadDeliverDropped {
		// 丢弃也落一行，而不是只写审计：投递表是事实源，缺行就分不清
		// "没发生"和"发生了但被丢"，而这两件事的处理方式完全不同。
		status = threadDeliveryDropped
	}

	// 主键冲突 = 这条事件已经投过（多副本重投），DO NOTHING 让它幂等返回。
	var gotThreadID string
	insertErr := tx.QueryRow(ctx,
		`INSERT INTO thread_deliveries (thread_id, event_key, session_id, status, payload)
		 VALUES ($1, $2, $3, $4, $5)
		 ON CONFLICT (thread_id, event_key) DO NOTHING
		 RETURNING thread_id`,
		threadID, p.EventKey, sessionID, status, payloadArg(p.Payload)).Scan(&gotThreadID)
	switch {
	case errors.Is(insertErr, pgx.ErrNoRows):
		// 已投过：保持既有状态，不因为重投把它从 dropped 复活或从 done 退回 queued。
		// 调用方必须知道这一点 —— 否则它会为一条**早已跑过**的事件再起一个回合。
		decision = threadDeliverDuplicate
	case insertErr != nil:
		return threadDeliverDropped, threadID, insertErr
	}

	if commitErr := tx.Commit(ctx); commitErr != nil {
		return threadDeliverDropped, threadID, commitErr
	}

	if decision == threadDeliverDropped {
		// 审计放在事务外：写审计失败不该回滚"这条被丢了"这个事实
		// （AuditLog 本身也是 fail-soft），否则会变成"丢了但没记录"。
		db.AuditLog(ctx, p.UserID, p.TenantID, threadDeliveryDroppedAudit,
			"thread:"+threadID, "thread delivery dropped: queue full", "",
			map[string]interface{}{
				"event_key":     p.EventKey,
				"session":       sessionID,
				"entry_session": p.SessionID,
				"subscribed":    subscribed,
				"pending":       pending,
				"limit":         maxPendingThreadEvents,
			})
	}
	return decision, threadID, nil
}
