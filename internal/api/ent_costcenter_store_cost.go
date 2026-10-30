package api

import (
	"context"
	"fmt"
	"sort"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

// ent_costcenter_store_cost.go —— 成本中心的**成本汇总**存储层。
//
// 由 ent_costcenter_handler.go 按职责拆出（L3-4 批次 A，同包纯移动，逐字未改）。
// 与 ent_costcenter_store_quota.go 的分工：那个文件管配额（池/分配/用量上限），
// 这个文件管**花了多少**（billing_records + credit_transactions + payments 跨表汇总）。
//
// 两个方法都是只读聚合查询，没有写路径；跨租户维度由 groupBy 决定。

// ── CostSummary：billing_records + credit_transactions + payments 跨租户汇总 ──

func (s *pgEntCostStore) CostSummary(ctx context.Context, from, to time.Time, groupBy, tenantID string) (*entCostSummary, error) {
	// 白名单：仅允许 "day" 或 "tenant"
	var keyExpr, groupExpr string
	switch groupBy {
	case "day":
		keyExpr, groupExpr = "TO_CHAR(DATE(created_at), 'YYYY-MM-DD')", "DATE(created_at)"
	default:
		groupBy = "tenant"
		keyExpr, groupExpr = "tenant_id::text", "tenant_id"
	}

	summary := &entCostSummary{
		From: from.Format(time.RFC3339), To: to.Format(time.RFC3339), GroupBy: groupBy,
		Rows: []entCostSummaryRow{},
	}
	rowsBy := map[string]*entCostSummaryRow{}
	get := func(k string) *entCostSummaryRow {
		r, ok := rowsBy[k]
		if !ok {
			r = &entCostSummaryRow{Key: k}
			if groupBy == "day" {
				r.Day = k
			} else {
				r.TenantID = k
			}
			rowsBy[k] = r
		}
		return r
	}

	// 1) billing_records：成本与 token 明细（SQL 聚合）
	// 租户过滤加在 WHERE 里而不是事后过滤：groupBy=day 时聚合结果无法拆回单租户。
	// tenantID 为空 ⇒ $3 = '' ⇒ 条件恒真 ⇒ 全部租户（平台 owner 视角）。
	qBilling := fmt.Sprintf(
		`SELECT %s AS k, COALESCE(SUM(cost_cents),0), COALESCE(SUM(input_tokens),0),
		        COALESCE(SUM(output_tokens),0), COUNT(*)
		 FROM billing_records
		 WHERE created_at >= $1 AND created_at < $2
		   AND ($3 = '' OR tenant_id::text = $3)
		 GROUP BY %s`, keyExpr, groupExpr)
	rows, err := db.GlobalDBManager.Query(ctx, qBilling, from, to, tenantID)
	if err != nil {
		return nil, fmt.Errorf("summary billing_records: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var k string
		r := entCostSummaryRow{}
		if err := rows.Scan(&k, &r.CostCents, &r.InputTokens, &r.OutputTokens, &r.Records); err != nil {
			return nil, fmt.Errorf("summary billing scan: %w", err)
		}
		dst := get(k)
		dst.CostCents, dst.InputTokens, dst.OutputTokens, dst.Records =
			r.CostCents, r.InputTokens, r.OutputTokens, r.Records
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("summary billing iterate: %w", err)
	}

	// 2) credit_transactions：credits 消耗（负向流水求和；经 users 关联租户）
	ctKey := "u.tenant_id::text"
	ctGroup := "u.tenant_id"
	if groupBy == "day" {
		ctKey, ctGroup = "TO_CHAR(DATE(ct.created_at), 'YYYY-MM-DD')", "DATE(ct.created_at)"
	}
	qCredits := fmt.Sprintf(
		`SELECT %s AS k, COALESCE(-SUM(ct.amount),0)
		 FROM credit_transactions ct JOIN users u ON u.id::text = ct.user_id::text
		 WHERE ct.amount < 0 AND ct.created_at >= $1 AND ct.created_at < $2
		   AND ($3 = '' OR u.tenant_id::text = $3)
		 GROUP BY %s`, ctKey, ctGroup)
	rows, err = db.GlobalDBManager.Query(ctx, qCredits, from, to, tenantID)
	if err != nil {
		return nil, fmt.Errorf("summary credit_transactions: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var k string
		var spent int64
		if err := rows.Scan(&k, &spent); err != nil {
			return nil, fmt.Errorf("summary credits scan: %w", err)
		}
		get(k).CreditsSpent = spent
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("summary credits iterate: %w", err)
	}

	// 3) payments：充值收入（已支付订单；经 users 关联租户）
	pKey := "u.tenant_id::text"
	pGroup := "u.tenant_id"
	if groupBy == "day" {
		pKey, pGroup = "TO_CHAR(DATE(COALESCE(p.paid_at, p.created_at)), 'YYYY-MM-DD')",
			"DATE(COALESCE(p.paid_at, p.created_at))"
	}
	qPayments := fmt.Sprintf(
		`SELECT %s AS k, COALESCE(SUM(p.amount_cents),0)
		 FROM payments p JOIN users u ON u.id::text = p.user_id
		 WHERE p.status = 'paid'
		   AND COALESCE(p.paid_at, p.created_at) >= $1 AND COALESCE(p.paid_at, p.created_at) < $2
		   AND ($3 = '' OR u.tenant_id::text = $3)
		 GROUP BY %s`, pKey, pGroup)
	rows, err = db.GlobalDBManager.Query(ctx, qPayments, from, to, tenantID)
	if err != nil {
		return nil, fmt.Errorf("summary payments: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var k string
		var revenue int64
		if err := rows.Scan(&k, &revenue); err != nil {
			return nil, fmt.Errorf("summary payments scan: %w", err)
		}
		get(k).RevenueCents = revenue
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("summary payments iterate: %w", err)
	}

	for _, r := range rowsBy {
		summary.Rows = append(summary.Rows, *r)
		summary.Totals.CostCents += r.CostCents
		summary.Totals.InputTokens += r.InputTokens
		summary.Totals.OutputTokens += r.OutputTokens
		summary.Totals.Records += r.Records
		summary.Totals.CreditsSpent += r.CreditsSpent
		summary.Totals.RevenueCents += r.RevenueCents
	}
	sort.Slice(summary.Rows, func(i, j int) bool { return summary.Rows[i].Key < summary.Rows[j].Key })
	return summary, nil
}

// ── GroupCost：群组计费归集明细 + 合计 ──

func (s *pgEntCostStore) GroupCost(ctx context.Context, groupID string, from, to time.Time, tenantID string) (*entGroupCost, error) {
	out := &entGroupCost{GroupID: groupID, Records: []entGroupCostRow{}}

	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COUNT(*), COALESCE(SUM(cost_cents),0), COALESCE(SUM(input_tokens),0),
		        COALESCE(SUM(output_tokens),0)
		 FROM billing_records
		 WHERE group_id = $1 AND created_at >= $2 AND created_at < $3
		   AND ($4 = '' OR tenant_id::text = $4)`,
		groupID, from, to, tenantID).
		Scan(&out.Total.Records, &out.Total.CostCents, &out.Total.InputTokens, &out.Total.OutputTokens)
	if err != nil {
		return nil, fmt.Errorf("group cost totals: %w", err)
	}

	rows, err := db.GlobalDBManager.Query(ctx,
		// A5：`input_tokens` / `output_tokens` 现在可能为 NULL（企业归集记录只带金额，
		// 不带 token 明细）。列可空是刻意的 —— NULL 表示"这条记录不含 token 信息"，
		// 与"真的用了 0 token"区分开。读侧 COALESCE 成 0 以保持 API 契约不变。
		`SELECT id, user_id, session_id, COALESCE(input_tokens,0), COALESCE(output_tokens,0),
		        cost_cents, created_at
		 FROM billing_records
		 WHERE group_id = $1 AND created_at >= $2 AND created_at < $3
		   AND ($4 = '' OR tenant_id::text = $4)
		 ORDER BY created_at DESC LIMIT 500`, groupID, from, to, tenantID)
	if err != nil {
		return nil, fmt.Errorf("group cost records: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var r entGroupCostRow
		if err := rows.Scan(&r.ID, &r.UserID, &r.SessionID, &r.InputTokens, &r.OutputTokens,
			&r.CostCents, &r.CreatedAt); err != nil {
			return nil, fmt.Errorf("group cost scan: %w", err)
		}
		out.Records = append(out.Records, r)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("group cost iterate: %w", err)
	}
	return out, nil
}
