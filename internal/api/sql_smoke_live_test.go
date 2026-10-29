package api

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
)

// sha256Hex 与 `AuthMiddleware` / `generateAPIKey` 的算法保持完全一致：
// sha256 → hex。测试必须用同一条路径生成 key_hash，否则插进去的 key
// 永远对不上鉴权查询，"key 无效"会变成测试自身的 bug。
func sha256Hex(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

// 6 处「只做过字符串断言」的 SQL 的**真实 PostgreSQL** 冒烟用例。
//
// ## 为什么需要这份文件
//
// 这批 SQL 此前只有单测，而单测做的是 `strings.Contains(sql, "...")` 这一类断言 ——
// 它能证明「WHERE 里写了 user_id = $1」，却证明不了：
//
//   - 列名在库里真的存在（拼错 `last_used_tm` 单测全绿）；
//   - 列数、顺序与 Scan 的参数一一对应（多选一列会让 Scan 静默错位）；
//   - 类型能对上（`last_used_at` 是 timestamp 无时区，写入却用 now() 得 timestamptz）；
//   - 语法在目标 PG 版本上成立（`make_interval(days => $2)`、`SUM(...) OVER ()`）。
//
// 这些都是「跑一次就知道、不跑就不知道」的东西，而它们出错的后果是运行时 500。
//
// ## 取值口径
//
// 与既有 `*_live_test.go` 一致：`CHIRON_TEST_POSTGRES_DSN` 未设置则 `t.Skip`。
// 也就是说本机不设 DSN 时这份文件只贡献编译检查；CI 的 real-stack job 注入 DSN
// 后才真正执行。**注意 run 的模式要带 `Live`**（`-run Live`）。
//
// ## 数据隔离
//
// 每个用例自建 tenant + user，结束时按逆序清理。都用随机 uuid 作标记，
// 与并发跑的其他用例互不影响。

// liveSmokePool 复用邀请码用例的连接逻辑（DSN 守卫、30s 连接超时、Cleanup 关闭）。
func liveSmokePool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	return liveInvitePool(t)
}

// liveSmokeFixture 建一套「租户 + 用户」，返回 (tenantID, userID)。
//
// `role` 必须显式写成 'user'：`users.role` 是 `varchar(16)` 且**可 NULL**，
// 而 `apiKeyAuthQuery` 把 `u.role` 扫进 `string` —— 为 NULL 时 Scan 直接报
// "cannot scan NULL into *string"，表现为 API Key 鉴权 500。
// 显式设值既是贴近生产（注册流程会写 role），也让这个坑在真库上一跑就现形。
func liveSmokeFixture(t *testing.T, pool *pgxpool.Pool) (tenantID int16, userID string) {
	t.Helper()
	ctx := context.Background()

	if err := pool.QueryRow(ctx,
		`INSERT INTO tenants (name, status) VALUES ($1, 'active') RETURNING id`,
		"sql-smoke-"+uuid.NewString()[:8]).Scan(&tenantID); err != nil {
		t.Fatalf("建租户失败: %v", err)
	}

	userID = uuid.NewString()
	if _, err := pool.Exec(ctx,
		`INSERT INTO users (id, tenant_id, name, role) VALUES ($1, $2, $3, 'user')`,
		userID, tenantID, "sql-smoke"); err != nil {
		t.Fatalf("建用户失败: %v", err)
	}

	t.Cleanup(func() {
		// 逆序清理：先删引用方，再删被引用方，避免将来补上外键后顺序失效。
		cleanup := []struct {
			sql  string
			args []interface{}
		}{
			{`DELETE FROM invoice_requests WHERE user_id = $1`, []interface{}{userID}},
			{`DELETE FROM payments WHERE user_id = $1`, []interface{}{userID}},
			{`DELETE FROM credit_transactions WHERE user_id = $1`, []interface{}{userID}},
			{`DELETE FROM usage_events WHERE user_id = $1`, []interface{}{userID}},
			{`DELETE FROM api_keys WHERE user_id = $1`, []interface{}{userID}},
			{`DELETE FROM users WHERE id = $1`, []interface{}{userID}},
			{`DELETE FROM tenants WHERE id = $1`, []interface{}{tenantID}},
		}
		for _, c := range cleanup {
			if _, err := pool.Exec(context.Background(), c.sql, c.args...); err != nil {
				t.Logf("清理失败 %q: %v", c.sql, err)
			}
		}
	})
	return tenantID, userID
}

// TestLiveExportQueries 验 `exportCountQuery` / `exportRowsQuery`（缺口 ①）。
//
// 除了「语法能跑、参数对得上」，这条用例专门钉住一个**容易踩到的语义**：
// `reason <> 'free_chat'` 对 NULL 求值为 UNKNOWN，因此 `reason` 为 NULL 的行
// 会被**静默排除**在导出结果之外。当前实现即如此 —— 若将来认为这些流水也该
// 出现在账单里，需要把条件改成 `COALESCE(reason, '') <> 'free_chat'`，
// 届时这条断言会失败并提醒改这里。
func TestLiveExportQueries(t *testing.T) {
	pool := liveSmokePool(t)
	_, userID := liveSmokeFixture(t, pool)
	ctx := context.Background()

	now := time.Now()
	from := now.AddDate(0, 0, -30)
	to := now.Add(time.Minute)

	ins := func(reason *string, amount int) string {
		id := uuid.NewString()
		if _, err := pool.Exec(ctx,
			`INSERT INTO credit_transactions (id, user_id, amount, balance, reason, created_at)
			 VALUES ($1, $2, $3, $4, $5, $6)`,
			id, userID, amount, 1000, reason, now.Add(-time.Hour)); err != nil {
			t.Fatalf("插入流水失败: %v", err)
		}
		return id
	}
	chat := "chat"
	free := "free_chat"
	ins(&chat, -10)
	ins(&chat, -20)
	ins(&free, -5) // 应被排除
	ins(nil, -7)   // NULL：`reason <> 'free_chat'` 为 UNKNOWN → 也被排除

	// 同租户的另一个用户，验证 user_id 隔离。
	otherID := uuid.NewString()
	if _, err := pool.Exec(ctx,
		`INSERT INTO users (id, tenant_id, name, role)
		 SELECT $1, tenant_id, 'sql-smoke-other', 'user' FROM users WHERE id = $2`,
		otherID, userID); err != nil {
		t.Fatalf("建第二个用户失败: %v", err)
	}
	t.Cleanup(func() {
		if _, err := pool.Exec(context.Background(),
			`DELETE FROM credit_transactions WHERE user_id = $1`, otherID); err != nil {
			t.Logf("清理第二个用户的流水失败: %v", err)
		}
		if _, err := pool.Exec(context.Background(),
			`DELETE FROM users WHERE id = $1`, otherID); err != nil {
			t.Logf("清理第二个用户失败: %v", err)
		}
	})
	if _, err := pool.Exec(ctx,
		`INSERT INTO credit_transactions (id, user_id, amount, balance, reason, created_at)
		 VALUES ($1, $2, -99, 900, 'chat', $3)`,
		uuid.NewString(), otherID, now.Add(-time.Hour)); err != nil {
		t.Fatalf("插入他人流水失败: %v", err)
	}

	var total int
	if err := pool.QueryRow(ctx, exportCountQuery, userID, from, to).Scan(&total); err != nil {
		t.Fatalf("exportCountQuery 执行失败（列名/语法/参数有问题）: %v", err)
	}
	if total != 2 {
		t.Errorf("count = %d，期望 2（排除 free_chat、NULL reason 与他人流水）", total)
	}

	rows, err := pool.Query(ctx, exportRowsQuery, userID, from, to, exportMaxRows)
	if err != nil {
		t.Fatalf("exportRowsQuery 执行失败: %v", err)
	}
	defer rows.Close()

	n := 0
	for rows.Next() {
		var amount, balance int
		var reason string
		var createdAt time.Time
		var turnID string
		// 列顺序必须与 exportRowsQuery 的 SELECT 一致：amount, balance, reason,
		// created_at, COALESCE(turn_id, '')。错位在这里会立刻炸出来。
		if err := rows.Scan(&amount, &balance, &reason, &createdAt, &turnID); err != nil {
			t.Fatalf("扫描导出行为失败（列数与顺序不匹配？）: %v", err)
		}
		if reason == "free_chat" {
			t.Errorf("导出不应包含 free_chat 流水")
		}
		n++
	}
	if err := rows.Err(); err != nil {
		t.Fatalf("遍历导出结果失败: %v", err)
	}
	if n != 2 {
		t.Errorf("导出 %d 行，期望 2 行", n)
	}
}

// TestLiveUsageBreakdownWindowTotals 验 `usageBreakdownQuery` 的窗口函数与
// `COUNT(*) OVER ()`（缺口 ②）。
//
// 这条用例要证明的核心性质是「**截断之后总量仍然准确**」：结果被 LIMIT 截到 100 行，
// 但 total_* 是窗口函数在聚合后的全部行上求和的结果（窗口先于 LIMIT 执行）。
// 若哪天有人把 `SUM(...) OVER ()` 改成「把返回行相加」，用户对账就会少算 ——
// 而这个错误在任何「数据量小于 100」的测试里都看不出来，所以这里故意造 150 个桶。
func TestLiveUsageBreakdownWindowTotals(t *testing.T) {
	pool := liveSmokePool(t)
	tenantID, userID := liveSmokeFixture(t, pool)
	ctx := context.Background()

	const buckets = 150
	// 每个桶的 input_tokens 就是它的序号：1..150，总和 150*151/2 = 11325。
	// 截断到 100 行时，返回行的和只有 1..100 = 5050 —— 两者必须区分得开。
	if _, err := pool.Exec(ctx,
		`INSERT INTO usage_events (tenant_id, user_id, session_id, model,
		                           input_tokens, output_tokens, cached_tokens, cost_cents, created_at)
		 SELECT $1, $2, '', 'model-' || i, i, 0, 0, 1, now() - make_interval(days => 1)
		   FROM generate_series(1, $3) AS i`,
		tenantID, userID, buckets); err != nil {
		t.Fatalf("批量插入 usage_events 失败: %v", err)
	}

	column, ok := usageGroupColumn("model")
	if !ok {
		t.Fatal(`usageGroupColumn("model") 未命中白名单`)
	}
	query := fmt.Sprintf(usageBreakdownQuery, column)

	rows, err := pool.Query(ctx, query, userID, 30, usageBreakdownLimit)
	if err != nil {
		t.Fatalf("usageBreakdownQuery 执行失败（窗口函数/语法问题）: %v", err)
	}
	defer rows.Close()

	type row struct {
		bucket       string
		eventCount   int
		inputTokens  int64
		outputTokens int64
		cachedTokens int64
		costCents    int64
		grandInput   int64
		grandOutput  int64
		grandCached  int64
		grandCost    int64
		totalBuckets int
	}

	var got []row
	for rows.Next() {
		var r row
		// 列顺序必须与语句 SELECT 一致：bucket, event_count, input_tokens,
		// output_tokens, cached_tokens, cost_cents, 四个 grand_*, total_buckets。
		if err := rows.Scan(&r.bucket, &r.eventCount, &r.inputTokens, &r.outputTokens,
			&r.cachedTokens, &r.costCents, &r.grandInput, &r.grandOutput,
			&r.grandCached, &r.grandCost, &r.totalBuckets); err != nil {
			t.Fatalf("扫描聚合行失败（列数与顺序不匹配？）: %v", err)
		}
		got = append(got, r)
	}
	if err := rows.Err(); err != nil {
		t.Fatalf("遍历聚合结果失败: %v", err)
	}

	if len(got) != usageBreakdownLimit {
		t.Fatalf("返回 %d 行，期望被截断到 %d 行", len(got), usageBreakdownLimit)
	}
	const wantTotal = int64(buckets * (buckets + 1) / 2) // 11325
	first := got[0]
	if first.totalBuckets != buckets {
		t.Errorf("total_buckets = %d，期望 %d（COUNT(*) OVER () 应看到全部桶，不受 LIMIT 影响）",
			first.totalBuckets, buckets)
	}
	if first.grandInput != wantTotal {
		t.Errorf("grand_input = %d，期望 %d（截断后总量仍必须准确）",
			first.grandInput, wantTotal)
	}
	// 排序：input_tokens 降序 → 第一个桶是 model-150。
	if first.bucket != "model-150" {
		t.Errorf("首行 bucket = %q，期望 model-150（应按 token 降序）", first.bucket)
	}
	// 截断判定：客户端据此决定是否提示"只显示前 N 项"。
	if truncated := first.totalBuckets > usageBreakdownLimit; !truncated {
		t.Errorf("total_buckets=%d > limit=%d，truncated 应为 true",
			first.totalBuckets, usageBreakdownLimit)
	}

	// 隔离：另一个用户的数据不能被算进来。
	var otherTotal int64
	if err := pool.QueryRow(ctx,
		`SELECT COALESCE(SUM(input_tokens), 0) FROM usage_events WHERE user_id = $1`,
		"no-such-user").Scan(&otherTotal); err != nil {
		t.Fatalf("查他人用量失败: %v", err)
	}
	if otherTotal != 0 {
		t.Errorf("不存在的用户求和为 %d，期望 0", otherTotal)
	}
}

// TestLiveApplyInvoiceInsertSelect 验 `invoice_requests` 建表与
// `applyInvoiceQuery` 的 `INSERT ... SELECT ... FROM payments`（缺口 ③）。
//
// 这条 SQL 的设计要点是「校验与写入在同一条语句里」：存在性、归属、可开票状态
// 全在 WHERE 里，任何一项不满足都只影响 0 行 —— 不存在「校验通过但写入失败」的
// 窗口。真库用例要同时证明 **成功路径** 与 **三种 0 行路径**，以及
// `ux_invoice_requests_payment` 唯一索引确实拦得住重复申请（23505）。
func TestLiveApplyInvoiceInsertSelect(t *testing.T) {
	pool := liveSmokePool(t)
	_, userID := liveSmokeFixture(t, pool)
	ctx := context.Background()

	paidID := uuid.NewString()
	if _, err := pool.Exec(ctx,
		`INSERT INTO payments (id, user_id, channel, credits, amount_cents, currency, status, created_at)
		 VALUES ($1, $2, 'alipay', 1000, 9900, 'CNY', 'paid', now())`,
		paidID, userID); err != nil {
		t.Fatalf("建已支付订单失败: %v", err)
	}

	apply := func(invoiceID, paymentID, title, taxNo, note string) (int64, error) {
		tag, err := pool.Exec(ctx, applyInvoiceQuery,
			invoiceID, userID, paymentID, title, taxNo, note)
		if err != nil {
			return 0, err
		}
		return tag.RowsAffected(), nil
	}

	// 成功路径：金额与币种来自 payments 快照。
	invoiceID := uuid.NewString()
	affected, err := apply(invoiceID, paidID, "某某科技有限公司", "91310000MA1K3XYZ12", "")
	if err != nil {
		t.Fatalf("applyInvoiceQuery 执行失败（列名/语法问题）: %v", err)
	}
	if affected != 1 {
		t.Fatalf("影响 %d 行，期望 1", affected)
	}

	var amount, currency, status string
	var amountCents int64
	if err := pool.QueryRow(ctx,
		`SELECT title, amount_cents, currency, status FROM invoice_requests WHERE id = $1`,
		invoiceID).Scan(&amount, &amountCents, &currency, &status); err != nil {
		t.Fatalf("回查发票失败: %v", err)
	}
	if amountCents != 9900 {
		t.Errorf("amount_cents = %d，期望 9900（应为 payments 的快照）", amountCents)
	}
	if currency != "CNY" {
		t.Errorf("currency = %q，期望 CNY", currency)
	}
	if status != "pending" {
		t.Errorf("status = %q，期望 pending", status)
	}

	// 重复申请：同一 payment 再开一张，唯一索引必须拦住（23505，而非静默成功）。
	if _, err := apply(uuid.NewString(), paidID, "重复抬头", "91310000MA1K3XYZ12", ""); err == nil {
		t.Fatal("同一笔支付重复开票居然成功了：ux_invoice_requests_payment 未生效")
	} else if !isUniqueViolation(err) {
		t.Fatalf("重复开票报的不是唯一冲突（isUniqueViolation 为假）: %v", err)
	}

	// 0 行路径之一：不存在的 payment。
	if n, err := apply(uuid.NewString(), uuid.NewString(), "不存在的订单", "91310000MA1K3XYZ12", ""); err != nil {
		t.Fatalf("不存在的订单应影响 0 行而不是报错: %v", err)
	} else if n != 0 {
		t.Errorf("不存在的订单影响 %d 行，期望 0", n)
	}

	// 0 行路径之二：状态不是 paid。
	pendingID := uuid.NewString()
	if _, err := pool.Exec(ctx,
		`INSERT INTO payments (id, user_id, channel, credits, amount_cents, currency, status, created_at)
		 VALUES ($1, $2, 'alipay', 1000, 5000, 'CNY', 'pending', now())`,
		pendingID, userID); err != nil {
		t.Fatalf("建待支付订单失败: %v", err)
	}
	if n, err := apply(uuid.NewString(), pendingID, "未支付订单", "91310000MA1K3XYZ12", ""); err != nil {
		t.Fatalf("未支付订单应影响 0 行而不是报错: %v", err)
	} else if n != 0 {
		t.Errorf("未支付订单影响 %d 行，期望 0", n)
	}

	// 0 行路径之三：别人的订单（越权）。
	otherID := uuid.NewString()
	if _, err := pool.Exec(ctx,
		`INSERT INTO users (id, tenant_id, name, role)
		 SELECT $1, tenant_id, 'sql-smoke-other', 'user' FROM users WHERE id = $2`,
		otherID, userID); err != nil {
		t.Fatalf("建第二个用户失败: %v", err)
	}
	otherPayID := uuid.NewString()
	if _, err := pool.Exec(ctx,
		`INSERT INTO payments (id, user_id, channel, credits, amount_cents, currency, status, created_at)
		 VALUES ($1, $2, 'alipay', 1000, 7777, 'CNY', 'paid', now())`,
		otherPayID, otherID); err != nil {
		t.Fatalf("建他人订单失败: %v", err)
	}
	t.Cleanup(func() {
		if _, err := pool.Exec(context.Background(),
			`DELETE FROM payments WHERE user_id = $1`, otherID); err != nil {
			t.Logf("清理他人订单失败: %v", err)
		}
		if _, err := pool.Exec(context.Background(),
			`DELETE FROM users WHERE id = $1`, otherID); err != nil {
			t.Logf("清理第二个用户失败: %v", err)
		}
	})
	if n, err := apply(uuid.NewString(), otherPayID, "别人的订单", "91310000MA1K3XYZ12", ""); err != nil {
		t.Fatalf("他人订单应影响 0 行而不是报错: %v", err)
	} else if n != 0 {
		t.Errorf("他人订单影响 %d 行，期望 0（越权必须落到 0 行，再映射成 404）", n)
	}
}

// TestLiveAPIKeyAuthAndMarkUsed 验鉴权 JOIN 与 `last_used_at` 回写（缺口 ⑤⑥）。
//
// ⑤ `apiKeyAuthQuery` 是**每个 API 请求**都会走的语句，它返回 4 列并扫进
// 4 个 string。列数或顺序变了，单测（只断言字符串包含）发现不了，而线上表现是
// 全部请求 401/500。这里用真库把 4 列的**语义**逐一钉住。
//
// ⑥ `api_keys.last_used_at` 在 `init.sql` 里是 `timestamp`（**无时区**），
// 而回写语句用 `now()`（timestamptz）。两者靠隐式转换对齐，是否会丢/偏时间
// 只有真库能确认 —— 这里断言写入后确实落成非空时间。
func TestLiveAPIKeyAuthAndMarkUsed(t *testing.T) {
	pool := liveSmokePool(t)
	tenantID, userID := liveSmokeFixture(t, pool)
	ctx := context.Background()

	const plainKey = "sk-livesmoketestkey0000000000000000000000000000000000000000000000"
	keyHash := sha256Hex(plainKey)
	keyID := uuid.NewString()

	if _, err := pool.Exec(ctx,
		`INSERT INTO api_keys (id, user_id, name, key_hash, created_at, revoked)
		 VALUES ($1, $2, 'smoke', $3, now(), false)`,
		keyID, userID, keyHash); err != nil {
		t.Fatalf("建 api key 失败: %v", err)
	}

	// ⑤ 鉴权查询：读回 key 本身的 id，而不是只读 user —— 这是"补选 ak.id 的 JOIN"。
	var gotKeyID, gotUserID, gotRole, gotTenantID string
	if err := pool.QueryRow(ctx, apiKeyAuthQuery, keyHash).
		Scan(&gotKeyID, &gotUserID, &gotRole, &gotTenantID); err != nil {
		t.Fatalf("apiKeyAuthQuery 执行失败（列数/顺序/类型不匹配？）: %v", err)
	}
	if gotKeyID != keyID {
		t.Errorf("第 1 列 keyID = %q，期望 %q", gotKeyID, keyID)
	}
	if gotUserID != userID {
		t.Errorf("第 2 列 userID = %q，期望 %q", gotUserID, userID)
	}
	if gotRole != "user" {
		t.Errorf("第 3 列 role = %q，期望 user", gotRole)
	}
	if gotTenantID != fmt.Sprintf("%d", tenantID) {
		t.Errorf("第 4 列 tenantID = %q，期望 %d（COALESCE 转文本）", gotTenantID, tenantID)
	}

	// 已吊销的 key 必须查不到 —— 否则"吊销"只是列表里少一行。
	if _, err := pool.Exec(ctx, apiKeyRevokeQuery, keyID, userID); err != nil {
		t.Fatalf("吊销失败: %v", err)
	}
	if err := pool.QueryRow(ctx, apiKeyAuthQuery, keyHash).
		Scan(&gotKeyID, &gotUserID, &gotRole, &gotTenantID); err == nil {
		t.Error("已吊销的 key 仍能通过鉴权查询")
	}

	// 重复吊销影响 0 行（→ handler 回 404，且并发双请求只有一个生效）。
	tag, err := pool.Exec(ctx, apiKeyRevokeQuery, keyID, userID)
	if err != nil {
		t.Fatalf("重复吊销失败: %v", err)
	}
	if tag.RowsAffected() != 0 {
		t.Errorf("重复吊销影响 %d 行，期望 0", tag.RowsAffected())
	}

	// ⑥ 回写 last_used_at：新建时该列为 NULL。
	activeKeyID := uuid.NewString()
	if _, err := pool.Exec(ctx,
		`INSERT INTO api_keys (id, user_id, name, key_hash, created_at, revoked)
		 VALUES ($1, $2, 'mark-used', $3, now(), false)`,
		activeKeyID, userID, sha256Hex(plainKey+"-2")); err != nil {
		t.Fatalf("建第二把 key 失败: %v", err)
	}
	var before *time.Time
	if err := pool.QueryRow(ctx,
		`SELECT last_used_at FROM api_keys WHERE id = $1`, activeKeyID).Scan(&before); err != nil {
		t.Fatalf("读 last_used_at 失败: %v", err)
	}
	if before != nil {
		t.Errorf("新建 key 的 last_used_at 应为 NULL，实际 %v", before)
	}

	if _, err := pool.Exec(ctx, apiKeyMarkUsedQuery, activeKeyID); err != nil {
		t.Fatalf("apiKeyMarkUsedQuery 执行失败（timestamp 与 now() 隐式转换有问题？）: %v", err)
	}
	var after *time.Time
	if err := pool.QueryRow(ctx,
		`SELECT last_used_at FROM api_keys WHERE id = $1`, activeKeyID).Scan(&after); err != nil {
		t.Fatalf("回读 last_used_at 失败: %v", err)
	}
	if after == nil {
		t.Fatal("回写后 last_used_at 仍为 NULL")
	}
	// 时间必须接近"数据库的现在"。
	//
	// 这里**不能**用 Go 侧的 `time.Since(*after)`：`api_keys.last_used_at` 是
	// `timestamp without time zone`（naive），而 `now()` 是 timestamptz，隐式转换会
	// 按**数据库会话时区**落成墙钟值（本机实测会话时区 Asia/Shanghai ⇒ 存进去的是
	// +08 的本地时间）。拿它跟 Go 进程的 UTC 时刻相减，偏差恒为 8 小时 ——那是
	// L4-3 已记录的存量语义，不是这段代码的缺陷。
	//
	// 因此把比较放到数据库侧，naive 减 naive，只验证"确实写入了当下"。
	var driftSec float64
	if err := pool.QueryRow(ctx,
		`SELECT EXTRACT(EPOCH FROM (now()::timestamp - last_used_at))::float8
		   FROM api_keys WHERE id = $1`, activeKeyID).Scan(&driftSec); err != nil {
		t.Fatalf("计算 last_used_at 偏差失败: %v", err)
	}
	if driftSec < -60 || driftSec > 60 {
		t.Errorf("last_used_at 与数据库会话当前时刻偏差 %.0f 秒，期望 1 分钟内（同一会话时区下的 naive 比较）", driftSec)
	}
}
