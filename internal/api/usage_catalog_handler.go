package api

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// usage_catalog_handler.go —— **用量目录**（D3）。
//
// GET /v1/admin/usage/catalog?from=<RFC3339>&to=<RFC3339>&group_by=day|model|user
//
// 与既有的配额用量（`CostCenterView` 的 pool allocation）**不是一回事**：那个回答"这个池子
// 还剩多少额度"，本端点回答"**用量的构成**"—— 按天/模型/用户看 token 与成本各花了多少。
// 前者用于限额，后者用于对账与成本归因（"这个月贵在哪"）。
//
// 数据来源是 `billing_records`（真实扣费发生时写入）。**不读引擎事件**：那些是运行期信号，
// 不是账。按模型分组时 join `turns`（model 记在回合上）。
//
// 租户隔离沿用本项目一贯口径：**tenant 来自 JWT claims，绝不来自查询参数**。

// usageBucket 是聚合结果的一行。
type usageBucket struct {
	Bucket       string `json:"bucket"`
	InputTokens  int64  `json:"input_tokens"`
	OutputTokens int64  `json:"output_tokens"`
	CostCents    int64  `json:"cost_cents"`
	Records      int64  `json:"records"`
}

// GetUsageCatalog 返回按维度的用量构成。
func (h *AdminHandler) GetUsageCatalog(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenantID := tenantStr(claims.TenantID)
	if tenantID == "" {
		tenantID = claims.UserID
	}
	if db.Pool == nil {
		InternalError(w, "database unavailable")
		return
	}

	groupBy := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("group_by")))
	switch groupBy {
	case "day", "model", "user":
	default:
		BadRequest(w, "group_by must be one of: day, model, user")
		return
	}

	// 时间窗：默认最近 30 天。上限 366 天，避免一次拉出全表。
	now := time.Now().UTC()
	from := now.AddDate(0, 0, -30)
	to := now
	if v := r.URL.Query().Get("from"); v != "" {
		parsed, err := time.Parse(time.RFC3339, v)
		if err != nil {
			BadRequest(w, "from must be RFC3339")
			return
		}
		from = parsed
	}
	if v := r.URL.Query().Get("to"); v != "" {
		parsed, err := time.Parse(time.RFC3339, v)
		if err != nil {
			BadRequest(w, "to must be RFC3339")
			return
		}
		to = parsed
	}
	if to.Before(from) {
		BadRequest(w, "to must be after from")
		return
	}
	if to.Sub(from) > 366*24*time.Hour {
		BadRequest(w, "range must not exceed 366 days")
		return
	}

	// db.Pool 无法参数化列名/表达式，所以这里用**白名单 switch 选语句**（不是拼字符串）——
	// groupBy 已在上面收敛为三个字面量之一。
	var query string
	switch groupBy {
	case "day":
		query = `SELECT to_char(date_trunc('day', created_at), 'YYYY-MM-DD') AS bucket,
		                COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),
		                COALESCE(SUM(cost_cents), 0), COUNT(*)
		           FROM billing_records
		          WHERE tenant_id = $1 AND created_at >= $2 AND created_at < $3
		          GROUP BY bucket ORDER BY bucket`
	case "model":
		// model 记在 turns 上；billing_records 通过 turn_id 指过去。
		// 没有 turn_id（历史记录）的归入 "(unknown)"，而不是丢弃 —— 否则总量对不上账。
		query = `SELECT COALESCE(t.model, '(unknown)') AS bucket,
		                COALESCE(SUM(b.input_tokens), 0), COALESCE(SUM(b.output_tokens), 0),
		                COALESCE(SUM(b.cost_cents), 0), COUNT(*)
		           FROM billing_records b
		           LEFT JOIN turns t ON t.id = b.turn_id
		          WHERE b.tenant_id = $1 AND b.created_at >= $2 AND b.created_at < $3
		          GROUP BY bucket ORDER BY bucket`
	case "user":
		query = `SELECT COALESCE(user_id, '(unknown)') AS bucket,
		                COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),
		                COALESCE(SUM(cost_cents), 0), COUNT(*)
		           FROM billing_records
		          WHERE tenant_id = $1 AND created_at >= $2 AND created_at < $3
		          GROUP BY bucket ORDER BY bucket`
	}

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	rows, err := db.Pool.Query(ctx, query, tenantID, from, to)
	if err != nil {
		InternalError(w, "usage query failed")
		return
	}
	defer rows.Close()

	buckets := make([]usageBucket, 0, 32)
	var total usageBucket
	for rows.Next() {
		var b usageBucket
		if err := rows.Scan(&b.Bucket, &b.InputTokens, &b.OutputTokens, &b.CostCents, &b.Records); err != nil {
			InternalError(w, "usage scan failed")
			return
		}
		buckets = append(buckets, b)
		total.InputTokens += b.InputTokens
		total.OutputTokens += b.OutputTokens
		total.CostCents += b.CostCents
		total.Records += b.Records
	}
	if err := rows.Err(); err != nil {
		InternalError(w, "usage iteration failed")
		return
	}

	OK(w, map[string]any{
		"group_by":   groupBy,
		"from":       from.Format(time.RFC3339),
		"to":         to.Format(time.RFC3339),
		"buckets":    buckets,
		"total":      total,
		"bucket_cnt": len(buckets),
		"note": fmt.Sprintf(
			"按 %s 聚合自 billing_records（真实扣费记录）；token 为 input+output 之和的拆分。", groupBy),
	})
}
