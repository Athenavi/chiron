package api

// 本文件由 billing_handler.go 按职责外移而来（用量分摊查询：按 day/model/endpoint/session 聚合）。
// 代码逐字未改：只搬位置，不改逻辑。

import (
	"fmt"
	"log/slog"
	"net/http"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

// usageGroupColumn 把分组维度映射成 SQL 列表达式。
//
// 只处理**非 day** 的两个维度：day 走 credit_transactions 的按日聚合，不经过这里。
// 空值归入 'unknown' 而不是丢弃 —— 丢掉会让"各分组之和"小于总量，
// 用户会以为统计漏了数据，而这恰恰是最该被看见的那部分（子 Agent 未回传模型）。
func usageGroupColumn(groupBy string) (string, bool) {
	switch groupBy {
	case "model":
		return "COALESCE(NULLIF(model, ''), 'unknown')", true
	case "session":
		return "COALESCE(NULLIF(session_id, ''), 'unknown')", true
	default:
		return "", false
	}
}
// GetUsage returns aggregated usage stats for the user: daily token consumption and costs.
//
// 查询参数（都可省略，省略时行为与改造前完全一致）：
//
//	groupBy = day(默认) | model | session
//	range   = 7d | 30d(默认) | 90d
//
// 为什么默认仍走 credit_transactions 的按日聚合：已有前端依赖 `daily` 数组的形状，
// 换数据源会改变"记账流入/流出"的口径。`model` / `session` 两个维度来自
// usage_events（0004 迁移），它记的是**真实 token 与模型**，不含充值与扣费流水。
// parseUsageParams 解析并校验 GetUsage 的查询参数。
//
// 抽成纯函数是为了能**脱离数据库**测这三个行为：默认值、白名单、非法值报错。
// 返回 errMsg 非空即应回 400 —— 刻意不返回 error：调用方只需要一句给用户看的
// 文案，包一层 error 只会让错误信息被 fmt 破坏。
func parseUsageParams(groupBy, rangeKey string) (string, int, string) {
	if groupBy == "" {
		groupBy = "day"
	}
	if !usageGroupByWhitelist[groupBy] {
		return "", 0, "invalid groupBy: must be one of day, model, session"
	}
	if rangeKey == "" {
		rangeKey = "30d"
	}
	days, ok := usageRangeDays[rangeKey]
	if !ok {
		return "", 0, "invalid range: must be one of 7d, 30d, 90d"
	}
	return groupBy, days, ""
}
func (h *BillingHandler) GetUsage(w http.ResponseWriter, r *http.Request) {
	userID := h.resolveUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	q := r.URL.Query()
	groupBy, days, errMsg := parseUsageParams(q.Get("groupBy"), q.Get("range"))
	if errMsg != "" {
		// 显式 400 而不是静默回落：静默回落会让调用方以为查询生效了。
		BadRequest(w, errMsg)
		return
	}

	if groupBy != "day" {
		h.getBreakdownUsage(w, r, userID, groupBy, days)
		return
	}

	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT DATE(created_at::timestamp) as day,
			COUNT(*) as tx_count,
			SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE 0 END) as credits_spent,
			SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) as credits_added
		 FROM credit_transactions
		 WHERE user_id = $1 AND created_at::timestamp >= NOW() - make_interval(days => $2)
		 GROUP BY DATE(created_at::timestamp)
		 ORDER BY day DESC`, userID, days)
	if err != nil {
		slog.Error("get usage stats failed", "user_id", userID, "error", err, "db_available", db.GlobalDBManager.IsAvailable())
		InternalError(w, fmt.Sprintf("failed to get usage stats: %v", err))
		return
	}
	defer rows.Close()

	type dailyUsage struct {
		Day          string `json:"day"`
		TxCount      int    `json:"tx_count"`
		CreditsSpent int    `json:"credits_spent"`
		CreditsAdded int    `json:"credits_added"`
	}

	var daily []dailyUsage
	for rows.Next() {
		var d dailyUsage
		var dayTime time.Time
		if err := rows.Scan(&dayTime, &d.TxCount, &d.CreditsSpent, &d.CreditsAdded); err != nil {
			continue
		}
		d.Day = dayTime.Format("2006-01-02")
		daily = append(daily, d)
	}
	if err := rows.Err(); err != nil {
		InternalError(w, "failed to iterate daily usage")
		return
	}

	// Compute totals
	totalSpent := 0
	totalAdded := 0
	for _, d := range daily {
		totalSpent += d.CreditsSpent
		totalAdded += d.CreditsAdded
	}

	OK(w, map[string]interface{}{
		"daily":       daily,
		"total_spent": totalSpent,
		"total_added": totalAdded,
		"period_days": 30,
	})
}
func (h *BillingHandler) getBreakdownUsage(w http.ResponseWriter, r *http.Request, userID, groupBy string, days int) {
	// 列表达式来自白名单，绝不能来自请求参数 —— 参数只能是标识符，不能拼进 SQL。
	column, ok := usageGroupColumn(groupBy)
	if !ok {
		InternalError(w, "unsupported groupBy")
		return
	}

	rows, err := db.GlobalDBManager.Query(r.Context(),
		fmt.Sprintf(usageBreakdownQuery, column), userID, days, usageBreakdownLimit)
	if err != nil {
		slog.Error("get usage breakdown failed", "user_id", userID, "group_by", groupBy, "error", err)
		InternalError(w, "failed to get usage breakdown")
		return
	}
	defer rows.Close()

	type bucketUsage struct {
		Bucket       string `json:"bucket"`
		EventCount   int    `json:"event_count"`
		InputTokens  int64  `json:"input_tokens"`
		OutputTokens int64  `json:"output_tokens"`
		CachedTokens int64  `json:"cached_tokens"`
		CostCents    int64  `json:"cost_cents"`
	}

	breakdown := []bucketUsage{}
	// total_* 始终是全量合计（见函数头说明），不是 breakdown 各行之和。
	var totalInput, totalOutput, totalCached, totalCost int64
	var totalBuckets int
	for rows.Next() {
		var b bucketUsage
		var grandInput, grandOutput, grandCached, grandCost int64
		if err := rows.Scan(&b.Bucket, &b.EventCount, &b.InputTokens, &b.OutputTokens,
			&b.CachedTokens, &b.CostCents,
			&grandInput, &grandOutput, &grandCached, &grandCost, &totalBuckets); err != nil {
			slog.Warn("scan usage breakdown row failed", "user_id", userID, "error", err)
			continue
		}
		breakdown = append(breakdown, b)
		// 窗口值每行相同，直接覆盖即可（空结果集时保持 0）。
		totalInput, totalOutput, totalCached, totalCost = grandInput, grandOutput, grandCached, grandCost
	}
	if err := rows.Err(); err != nil {
		InternalError(w, "failed to iterate usage breakdown")
		return
	}

	OK(w, map[string]interface{}{
		"group_by":     groupBy,
		"period_days":  days,
		"breakdown":    breakdown,
		"total_input":  totalInput,
		"total_output": totalOutput,
		"total_cached": totalCached,
		"total_cost":   totalCost,
		// truncated 表示"还有没返回的 bucket"。前端据此提示"仅显示花费最高的 N 项"。
		"truncated":     totalBuckets > len(breakdown),
		"total_buckets": totalBuckets,
		"limit":         usageBreakdownLimit,
		"daily":         []struct{}{},
	})
}
