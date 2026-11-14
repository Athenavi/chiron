package api

import (
	"context"
	"encoding/json"
	"errors"
	"strconv"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
)

// P1-1 投递层队列侧的验收。
//
// 这一层有三件事只有跑一次才知道真假：
//   - `FOR UPDATE SKIP LOCKED` 写在 UPDATE … WHERE (…) = (SELECT …) 这种形态里到底
//     能不能编译执行（子查询里带 LIMIT + 锁定子句，语法位置很容易写错）；
//   - 排序是不是真的"最早优先"，而且同 created_at 时结果稳定；
//   - 收尾的 UPDATE 是不是幂等 —— 它会被重复调用（回合结束 + 进程退出清理）。
//
// 取值口径与既有 `*_live_test.go` 一致：`CHIRON_TEST_POSTGRES_DSN` 未设置即 skip。
// 跑法：`go test -mod=mod -run 'LiveThreadDelivery' ./internal/api/`。

func TestLiveClaimThreadDeliveryTakesOldestFirst(t *testing.T) {
	pool := withP12Pool(t)
	tenantID, userID := liveSmokeFixture(t, pool)
	ctx := context.Background()
	sessionID := p12Session(t, pool, tenantID, userID)

	// threads 引用 users，必须在 fixture 删 users **之前**清掉。
	// t.Cleanup 是 LIFO：这里后注册，所以先执行。
	// 两条 SQL 的过滤列不同（投递行按 session_id，thread 按 user_id），
	// 不能塞进一个循环里共用同一个参数 —— 那样 where 永远不匹配，行会留着不删。
	t.Cleanup(func() {
		// 顺序必须由引用方到被引用方：投递行 → threads → sessions。
		// 漏掉 sessions 会让 fixture 删 users 时撞 `sessions_user_id_fkey`，
		// 表现为一堆"清理失败"告警 + 库里留下残留行。
		for _, c := range []struct {
			sql string
			arg string
		}{
			{`DELETE FROM thread_deliveries WHERE session_id = $1`, sessionID},
			{`DELETE FROM threads WHERE user_id = $1`, userID},
			{`DELETE FROM sessions WHERE id = $1`, sessionID},
		} {
			if _, err := pool.Exec(context.Background(), c.sql, c.arg); err != nil {
				t.Logf("清理失败 %q: %v", c.sql, err)
			}
		}
	})

	seed := func(createdAt string, eventKey string) string {
		t.Helper()
		threadID := uuid.NewString()
		if _, err := pool.Exec(ctx,
			`INSERT INTO threads (id, tenant_id, user_id, source_kind, external_key, created_at)
			 VALUES ($1, $2, $3, 'job', $4, $5)`,
			threadID, tenantID, userID, eventKey, createdAt); err != nil {
			t.Fatalf("建 thread 失败: %v", err)
		}
		if _, err := pool.Exec(ctx,
			`INSERT INTO thread_deliveries (thread_id, event_key, session_id, status, created_at)
			 VALUES ($1, $2, $3, 'queued', $4)`,
			threadID, eventKey, sessionID, createdAt); err != nil {
			t.Fatalf("建投递行失败: %v", err)
		}
		return threadID
	}

	// 故意乱序插入：先晚后早，"最早优先"才不是插入顺序的巧合。
	lateKey := "evt-late"
	lateThread := seed("2026-01-02 00:00:00+00", lateKey)
	earlyKey := "evt-early"
	earlyThread := seed("2026-01-01 00:00:00+00", earlyKey)

	got, err := claimNextThreadDelivery(ctx, sessionID)
	if err != nil {
		t.Fatalf("领队失败: %v", err)
	}
	if got.EventKey != earlyKey || got.ThreadID != earlyThread {
		t.Fatalf("应领最早的一条 (%s/%s)，实际领到 (%s/%s)",
			earlyKey, earlyThread, got.EventKey, got.ThreadID)
	}

	// 领走即置 running：再领一次必须拿到第二条，而不是同一条。
	second, err := claimNextThreadDelivery(ctx, sessionID)
	if err != nil {
		t.Fatalf("第二次领队失败: %v", err)
	}
	if second.EventKey != lateKey || second.ThreadID != lateThread {
		t.Fatalf("应领第二条 (%s/%s)，实际领到 (%s/%s)",
			lateKey, lateThread, second.EventKey, second.ThreadID)
	}

	// 队列空了要给出可判别的哨兵错误，而不是 nil, nil —— 后者会让调用方
	// 拿着零值结构体去开一个 thread_id 为空的回合。
	if _, emptyErr := claimNextThreadDelivery(ctx, sessionID); emptyErr != errNoPendingDelivery {
		t.Fatalf("空队列应返回 errNoPendingDelivery，实际 %v", emptyErr)
	}

	// running 的行不该被再次领走（状态机单向）。
	var running int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM thread_deliveries WHERE session_id = $1 AND status = 'running'`,
		sessionID).Scan(&running); err != nil {
		t.Fatalf("统计 running 失败: %v", err)
	}
	if running != 2 {
		t.Fatalf("应有 2 行 running，实际 %d", running)
	}
}

func TestLiveFinishThreadDeliveryIsIdempotent(t *testing.T) {
	pool := withP12Pool(t)
	tenantID, userID := liveSmokeFixture(t, pool)
	ctx := context.Background()
	sessionID := p12Session(t, pool, tenantID, userID)

	t.Cleanup(func() {
		// 与上一条用例同因：漏删 sessions 会让 fixture 删 users 时撞
		// `sessions_user_id_fkey`，留下残留行并打印一串"清理失败"。
		for _, c := range []struct {
			sql string
			arg string
		}{
			{`DELETE FROM thread_deliveries WHERE session_id = $1`, sessionID},
			{`DELETE FROM threads WHERE user_id = $1`, userID},
			{`DELETE FROM sessions WHERE id = $1`, sessionID},
		} {
			if _, err := pool.Exec(context.Background(), c.sql, c.arg); err != nil {
				t.Logf("清理失败 %q: %v", c.sql, err)
			}
		}
	})

	threadID := uuid.NewString()
	if _, err := pool.Exec(ctx,
		`INSERT INTO threads (id, tenant_id, user_id, source_kind, external_key)
		 VALUES ($1, $2, $3, 'subagent', $4)`, threadID, tenantID, userID, "evt-1"); err != nil {
		t.Fatalf("建 thread 失败: %v", err)
	}
	if _, err := pool.Exec(ctx,
		`INSERT INTO thread_deliveries (thread_id, event_key, session_id, status)
		 VALUES ($1, 'evt-1', $2, 'queued')`, threadID, sessionID); err != nil {
		t.Fatalf("建投递行失败: %v", err)
	}

	d, err := claimNextThreadDelivery(ctx, sessionID)
	if err != nil {
		t.Fatalf("领队失败: %v", err)
	}
	if err := finishThreadDelivery(ctx, d, threadDeliveryDone); err != nil {
		t.Fatalf("收尾失败: %v", err)
	}

	// 重复收尾必须**不生效**：`WHERE status = 'running'` 让它自然幂等。
	// 没有这个条件的话，第二次调用会把已 done 的行改写成 dropped —— 审计上
	// 表现为"跑完了又变成没跑"，而这正是这套状态机最该避免的自相矛盾。
	if err := finishThreadDelivery(ctx, d, threadDeliveryDropped); err != nil {
		t.Fatalf("重复收尾应静默成功，实际 %v", err)
	}
	var status string
	if err := pool.QueryRow(ctx,
		`SELECT status FROM thread_deliveries WHERE thread_id = $1 AND event_key = $2`,
		threadID, "evt-1").Scan(&status); err != nil {
		t.Fatalf("读状态失败: %v", err)
	}
	if status != threadDeliveryDone {
		t.Fatalf("重复收尾后状态应仍为 done，实际 %q", status)
	}

	// 非法终态直接报错，而不是悄悄写成 queued/running 这种会被人重投的值。
	if err := finishThreadDelivery(ctx, d, threadDeliveryQueued); err == nil {
		t.Fatal("finishThreadDelivery 应拒绝非终态")
	}
}

// p11EnqueueFixture 建会话并注册按 FK 逆序的清理。
//
// 清理必须覆盖 sessions：漏掉它时 fixture 删 users 会撞 `sessions_user_id_fkey`，
// 结果是残留行 + 一串"清理失败"告警（这种失败容易被当成噪音忽略掉）。
func p11EnqueueFixture(t *testing.T, pool *pgxpool.Pool, tenantID int16, userID string) string {
	t.Helper()
	sessionID := p12Session(t, pool, tenantID, userID)
	t.Cleanup(func() {
		for _, c := range []struct {
			sql string
			arg string
		}{
			{`DELETE FROM thread_deliveries WHERE session_id = $1`, sessionID},
			{`DELETE FROM threads WHERE user_id = $1`, userID},
			{`DELETE FROM sessions WHERE id = $1`, sessionID},
		} {
			if _, err := pool.Exec(context.Background(), c.sql, c.arg); err != nil {
				t.Logf("清理失败 %q: %v", c.sql, err)
			}
		}
	})
	return sessionID
}

// TestLiveEnqueueThreadDeliveryIsIdempotent 钉住"多副本重投只落一行"。
//
// 幂等靠两层唯一约束：`threads_idem`（同一 external_key 一条 thread）与投递表主键
// （同一 (thread, event_key) 一行）。这两层必须**都**生效 —— 只靠第一层时，同一事件
// 重投会落成同一条 thread 下的两行投递，用户就会看两遍同样的结果。
func TestLiveEnqueueThreadDeliveryIsIdempotent(t *testing.T) {
	pool := withP12Pool(t)
	tenantID, userID := liveSmokeFixture(t, pool)
	sessionID := p11EnqueueFixture(t, pool, tenantID, userID)
	ctx := context.Background()

	p := threadEnqueueParams{
		TenantID:    tenantID,
		UserID:      userID,
		SourceKind:  "subagent",
		ExternalKey: "run-abc",
		SessionID:   sessionID,
		EventKey:    "evt-1",
	}

	decision, threadID, _, err := enqueueThreadDelivery(ctx, p)
	if err != nil {
		t.Fatalf("首次入队失败: %v", err)
	}
	if decision != threadDeliverQueued {
		t.Fatalf("空队列应 queued，实际 %q", decision)
	}

	// 同一条事件重投：必须判 duplicate 并复用同一 thread，且不新增行。
	// 判 duplicate（而不是 queued）是**语义要求**：调用方据此知道不必再起一个回合，
	// 否则同一事件会被再跑一遍，用户看到两条同样的结果还白花一份 token。
	decision2, threadID2, _, err := enqueueThreadDelivery(ctx, p)
	if err != nil {
		t.Fatalf("重投应幂等成功，实际 %v", err)
	}
	if decision2 != threadDeliverDuplicate || threadID2 != threadID {
		t.Fatalf("重投应判 duplicate 并复用同一 thread（%s），实际 (%q, %s)", threadID, decision2, threadID2)
	}

	var threads, deliveries int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM threads WHERE user_id = $1`, userID).Scan(&threads); err != nil {
		t.Fatalf("统计 thread 失败: %v", err)
	}
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM thread_deliveries WHERE session_id = $1`, sessionID).Scan(&deliveries); err != nil {
		t.Fatalf("统计投递失败: %v", err)
	}
	if threads != 1 || deliveries != 1 {
		t.Fatalf("重投后应各 1 行，实际 threads=%d deliveries=%d", threads, deliveries)
	}
}

// TestLiveEnqueueThreadDeliveryDropsOnOverflow 验超限丢弃：**落 dropped 行 + 不复活**。
//
// 丢弃写成"只写审计、不落行"会让投递表分不清"没发生"和"发生了但被丢"；而重投把
// dropped 捞回 queued 则是另一种错 —— 丢弃是终态，否则队列满时会来回抖动。
func TestLiveEnqueueThreadDeliveryDropsOnOverflow(t *testing.T) {
	pool := withP12Pool(t)
	tenantID, userID := liveSmokeFixture(t, pool)
	sessionID := p11EnqueueFixture(t, pool, tenantID, userID)
	ctx := context.Background()

	base := threadEnqueueParams{
		TenantID:    tenantID,
		UserID:      userID,
		SourceKind:  "job",
		ExternalKey: "job-42",
		SessionID:   sessionID,
	}
	// 先精确填满上限，一条都不能被丢 —— 这一步也顺带验了"上限不含正在跑的"。
	for i := 0; i < maxPendingThreadEvents; i++ {
		p := base
		p.EventKey = "evt-" + strconv.Itoa(i)
		decision, _, _, err := enqueueThreadDelivery(ctx, p)
		if err != nil {
			t.Fatalf("第 %d 条入队失败: %v", i, err)
		}
		if decision != threadDeliverQueued {
			t.Fatalf("第 %d 条不该被丢（上限 %d），实际 %q", i, maxPendingThreadEvents, decision)
		}
	}

	// 第 21 条落 dropped。
	overflow := base
	overflow.EventKey = "evt-overflow"
	decision, _, _, err := enqueueThreadDelivery(ctx, overflow)
	if err != nil {
		t.Fatalf("超限入队失败: %v", err)
	}
	if decision != threadDeliverDropped {
		t.Fatalf("超限应 dropped，实际 %q", decision)
	}

	var status string
	if err := pool.QueryRow(ctx,
		`SELECT status FROM thread_deliveries WHERE session_id = $1 AND event_key = $2`,
		sessionID, "evt-overflow").Scan(&status); err != nil {
		t.Fatalf("丢弃也必须落行，读不到: %v", err)
	}
	if status != threadDeliveryDropped {
		t.Fatalf("丢弃行状态应为 dropped，实际 %q", status)
	}

	// 重投同一条：判 duplicate（该事件早已落过行），且**状态仍是 dropped** ——
	// 关键在于不能被捞回队列，否则队列满时事件会反复进出。
	// duplicate 与 dropped 在这里表达的是两件不同的事：前者是"这条来过了"，
	// 后者是"这条被丢弃了"；调用方按前者 ACK、按 status 列判断投不投。
	again, _, _, err := enqueueThreadDelivery(ctx, overflow)
	if err != nil {
		t.Fatalf("丢弃后重投不应报错: %v", err)
	}
	if again != threadDeliverDuplicate {
		t.Fatalf("重投丢弃行应判 duplicate，实际 %q", again)
	}
	if err := pool.QueryRow(ctx,
		`SELECT status FROM thread_deliveries WHERE session_id = $1 AND event_key = $2`,
		sessionID, "evt-overflow").Scan(&status); err != nil {
		t.Fatalf("重投后读状态失败: %v", err)
	}
	if status != threadDeliveryDropped {
		t.Fatalf("重投后状态应仍为 dropped，实际 %q", status)
	}
}

// TestLiveEnqueueThreadDeliveryRejectsUnknownSource 验未注册来源直接报错。
//
// `webhook` 刻意不在 `followupKinds` 里，所以它必须炸，
// 而不是悄悄按某个默认分组入队 —— 那等于绕过信任边界。
func TestLiveEnqueueThreadDeliveryRejectsUnknownSource(t *testing.T) {
	pool := withP12Pool(t)
	tenantID, userID := liveSmokeFixture(t, pool)
	sessionID := p11EnqueueFixture(t, pool, tenantID, userID)

	_, _, _, err := enqueueThreadDelivery(context.Background(), threadEnqueueParams{
		TenantID:   tenantID,
		UserID:     userID,
		SourceKind: "webhook",
		SessionID:  sessionID,
		EventKey:   "evt-x",
	})
	if !errors.Is(err, errUnknownThreadSource) {
		t.Fatalf("未注册来源应返回 errUnknownThreadSource，实际 %v", err)
	}
}

// TestLiveClaimThreadDeliveryCarriesPayload 验认领时能拿回投递所需的全部上下文。
//
// 这条钉住的是"接线先决"：引擎回投是一次性 HTTP 请求，`status`/`profile`/`summary`
// 不在别处留底。若 payload 没随行返回、或 threads 的字段没 JOIN 回来，认领方就拼不出
// 正文也找不到收件人 —— 那种缺陷在单测里看不出来（各函数单独都对），只有真正跑一次
// 认领才暴露。
func TestLiveClaimThreadDeliveryCarriesPayload(t *testing.T) {
	pool := withP12Pool(t)
	tenantID, userID := liveSmokeFixture(t, pool)
	sessionID := p11EnqueueFixture(t, pool, tenantID, userID)
	ctx := context.Background()

	// 快照刻意带 `profile`（只有 subagent 用得上）与中文摘要，覆盖 jsonb 编解码。
	payload := json.RawMessage(`{"status":"completed","profile":"researcher","summary":"子任务已完成"}`)

	decision, threadID, _, err := enqueueThreadDelivery(ctx, threadEnqueueParams{
		TenantID:    tenantID,
		UserID:      userID,
		SourceKind:  "subagent",
		ExternalKey: "run-payload",
		SessionID:   sessionID,
		EventKey:    "evt-1",
		Payload:     payload,
	})
	if err != nil {
		t.Fatalf("入队失败: %v", err)
	}
	if decision != threadDeliverQueued {
		t.Fatalf("应 queued，实际 %q", decision)
	}

	d, err := claimNextThreadDelivery(ctx, sessionID)
	if err != nil {
		t.Fatalf("认领失败: %v", err)
	}

	if d.ThreadID != threadID || d.EventKey != "evt-1" {
		t.Fatalf("认领到错误的事件: (%s, %s)", d.ThreadID, d.EventKey)
	}
	// JOIN 出来的收件人信息：缺任何一项，认领方都无从发起 handleSubmit。
	if d.SessionID != sessionID || d.UserID != userID || d.TenantID != tenantID {
		t.Fatalf("收件人字段不对: session=%q user=%q tenant=%d", d.SessionID, d.UserID, d.TenantID)
	}
	if d.SourceKind != "subagent" || d.ExternalKey != "run-payload" {
		t.Fatalf("来源字段不对: kind=%q external=%q", d.SourceKind, d.ExternalKey)
	}

	// payload 必须逐字还原：拼正文时用的是它，不是"再查一次"。
	var got struct {
		Status  string `json:"status"`
		Profile string `json:"profile"`
		Summary string `json:"summary"`
	}
	if err := json.Unmarshal(d.Payload, &got); err != nil {
		t.Fatalf("payload 解不开: %v（原文 %s）", err, string(d.Payload))
	}
	if got.Status != "completed" || got.Profile != "researcher" || got.Summary != "子任务已完成" {
		t.Fatalf("payload 往返失真: %+v", got)
	}

	// 认领即置 running：同一条不会被同副本再领一次（否则事件会被投两遍）。
	if _, err := claimNextThreadDelivery(ctx, sessionID); !errors.Is(err, errNoPendingDelivery) {
		t.Fatalf("已认领的行不该再被领到，实际 err=%v", err)
	}
}
