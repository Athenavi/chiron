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
// P1-9 统一后的口径：
//   - 数据来源是 `usage_events`（引擎每轮回写的真实 token），**不是** billing_records；
//     billing_records 只在企业计费观察者装配时写入，本地/演示环境为空表。
//   - `credits_charged` = 这一轮实际扣减的用户余额（免费额度内为空，按 0 计）；
//   - `cost_cents` = 企业成本（分），来自引擎侧计价；当前可能为空，按 0 计。
//     两者**不可互相替代**：前者是用户扣费，后者是成本。
//
// 租户隔离沿用本项目一贯口径：**tenant 来自 JWT claims，绝不来自查询参数**。

// usageBucket 是聚合结果的一行。
type usageBucket struct {
	Bucket string `json:"bucket"`
	// Label 是给人看的名称（用户姓名 / 租户名）。Bucket 仍是稳定的 ID / 键：
	// 同名用户、重命名租户都不该让两行合并或让前端猜哪一个是谁。
	Label        string `json:"label,omitempty"`
	InputTokens  int64  `json:"input_tokens"`
	OutputTokens int64  `json:"output_tokens"`
	// CreditsCharged 是用户余额扣减；CostCents 是企业成本（分）。两者口径不同。
	CreditsCharged int64 `json:"credits_charged"`
	CostCents      int64 `json:"cost_cents"`
	Records        int64 `json:"records"`
}

// GetUsageCatalog 返回按维度的用量构成。
//
// 平台后台**只能看租户级**：user 分组仅租户管理员在 /tenant/usage 使用，
// 平台侧拿到成员级消费明细属于超出"平台运维"范围的越权读。
func (h *AdminHandler) GetUsageCatalog(w http.ResponseWriter, r *http.Request) {
	h.serveUsageCatalog(w, r, "admin")
}

// GetTenantUsageCatalog 返回**本租户**的用量构成（租户管理员）。
//
// 允许 day / model / user；tenant（跨租户）在租户侧没有意义，直接 403。
func (h *AdminHandler) GetTenantUsageCatalog(w http.ResponseWriter, r *http.Request) {
	h.serveUsageCatalog(w, r, "tenant")
}

func (h *AdminHandler) serveUsageCatalog(w http.ResponseWriter, r *http.Request, scope string) {
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
	case "day", "model":
	case "user":
		// 成员级明细只给租户管理员（/tenant/usage）。
		if scope == "admin" {
			Forbidden(w, "平台侧只能查看租户级用量；成员明细请在租户后台查看")
			return
		}
	case "tenant":
		// 跨租户视图只给平台 owner。
		if scope == "tenant" {
			Forbidden(w, "租户后台不能查看跨租户用量")
			return
		}
		if claims.Role != "owner" {
			Forbidden(w, "only platform owner can read cross-tenant usage")
			return
		}
	default:
		allowed := "day, model, tenant"
		if scope == "tenant" {
			allowed = "day, model, user"
		}
		BadRequest(w, "group_by must be one of: "+allowed)
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

	// 数据源是 usage_events（引擎每轮结束时写入真实 token 与模型），不是 billing_records：
	// 后者只有企业计费观察者装配时才写，本地/演示环境里是空表 —— 用它做统计，
	// 「团队与租户用量」永远是 0。成本列在 usage_events 上可空，一律 COALESCE。
	//
	// 每条查询都返回 (bucket, label, ...)：bucket 是稳定键（日期 / 模型名 / user_id /
	// tenant_id），label 是给人看的名字（用户姓名 / 租户名），没有就留空由前端回退。
	var query string
	switch groupBy {
	case "day":
		query = `SELECT to_char(date_trunc('day', created_at), 'YYYY-MM-DD') AS bucket,
		                '' AS label,
		                COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),
		                COALESCE(SUM(credits_charged), 0), COALESCE(SUM(cost_cents), 0), COUNT(*)
		           FROM usage_events
		          WHERE tenant_id = $1 AND created_at >= $2 AND created_at < $3
		          GROUP BY bucket ORDER BY bucket`
	case "model":
		query = `SELECT COALESCE(NULLIF(model, ''), '(unknown)') AS bucket,
		                '' AS label,
		                COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),
		                COALESCE(SUM(credits_charged), 0), COALESCE(SUM(cost_cents), 0), COUNT(*)
		           FROM usage_events
		          WHERE tenant_id = $1 AND created_at >= $2 AND created_at < $3
		          GROUP BY bucket ORDER BY bucket`
	case "user":
		query = `SELECT COALESCE(NULLIF(e.user_id, ''), '(unknown)') AS bucket,
		                COALESCE(u.name, '') AS label,
		                COALESCE(SUM(e.input_tokens), 0), COALESCE(SUM(e.output_tokens), 0),
		                COALESCE(SUM(e.credits_charged), 0), COALESCE(SUM(e.cost_cents), 0), COUNT(*)
		           FROM usage_events e
		           LEFT JOIN users u ON u.id = e.user_id
		          WHERE e.tenant_id = $1 AND e.created_at >= $2 AND e.created_at < $3
		          GROUP BY bucket, label ORDER BY bucket`
	case "tenant":
		// 跨租户：不带 tenant_id 谓词（权限已在上面收敛为 owner）。
		query = `SELECT e.tenant_id::text AS bucket,
		                COALESCE(tn.name, '') AS label,
		                COALESCE(SUM(e.input_tokens), 0), COALESCE(SUM(e.output_tokens), 0),
		                COALESCE(SUM(e.credits_charged), 0), COALESCE(SUM(e.cost_cents), 0), COUNT(*)
		           FROM usage_events e
		           LEFT JOIN tenants tn ON tn.id = e.tenant_id
		          WHERE e.created_at >= $1 AND e.created_at < $2
		          GROUP BY bucket, label ORDER BY bucket`
	}

	args := []any{tenantID, from, to}
	if groupBy == "tenant" {
		args = []any{from, to}
	}

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	rows, err := db.Pool.Query(ctx, query, args...)
	if err != nil {
		InternalError(w, "usage query failed")
		return
	}
	defer rows.Close()

	buckets := make([]usageBucket, 0, 32)
	var total usageBucket
	for rows.Next() {
		var b usageBucket
		if err := rows.Scan(&b.Bucket, &b.Label, &b.InputTokens, &b.OutputTokens, &b.CreditsCharged, &b.CostCents, &b.Records); err != nil {
			InternalError(w, "usage scan failed")
			return
		}
		buckets = append(buckets, b)
		total.InputTokens += b.InputTokens
		total.OutputTokens += b.OutputTokens
		total.CreditsCharged += b.CreditsCharged
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
			"按 %s 聚合自 usage_events（真实 token 用量）；credits_charged 为用户余额扣减，cost_cents 为企业成本（分，缺失按 0 计）。", groupBy),
	})
}
