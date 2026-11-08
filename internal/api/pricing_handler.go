package api

import (
	"encoding/json"
	"log/slog"
	"net/http"
	"strings"

	"github.com/athenavi/chiron/internal/db"
)

// pricing_handler.go —— **参考价格目录**（只读）。
//
// GET /v1/pricing/models?q=&provider=&sort=
//
// 数据来自 `model_prices`，由引擎侧 `app/pricing/sync.py` 定时拉取多个公开价格源后写入。
// 每行带 `verified`：是否**≥2 个源在容差（相对差 5%）内一致**。单源模型与源间有分歧的
// 模型同样入库（`verified=false`），并保留每个源各自的报价（`source_details`）供页面展开 ——
// 只显示"已印证"的模型会让目录只剩零星几条，用户看不到完整市场价。
//
// ⚠ **它不参与计费**：计费权威是 `BillingConfig`（管理员可改，`billing_records` 按它记账）。
// 本端点只回答"公开市场价大概是多少"，供选型与成本估算参考。把两者混起来会让
// "这个数字谁说了算"不可追溯 —— 而用户是照着账单付钱的。
//
// 只读、无写路径：价格由同步任务维护，任何人（含管理员）都不能通过 API 改它。

// modelPriceSource 是某个价格源对同一个模型的报价（与 sync.py 的 source_details 对齐）。
type modelPriceSource struct {
	Source      string  `json:"source"`
	InputPer1k  float64 `json:"input_per_1k"`
	OutputPer1k float64 `json:"output_per_1k"`
}

// modelPriceRow 是一行参考价格。
type modelPriceRow struct {
	Model       string   `json:"model"`
	Provider    string   `json:"provider"`
	InputPer1k  float64  `json:"input_per_1k"`
	OutputPer1k float64  `json:"output_per_1k"`
	Currency    string   `json:"currency"`
	Sources     []string `json:"sources"`
	SourceCount int      `json:"source_count"`
	// Verified 表示该价格通过多源印证（≥2 源在容差内一致）。
	Verified bool `json:"verified"`
	// SourceDetails 是每个源各自的报价，供"印证来源"一栏展开查看。
	SourceDetails []modelPriceSource `json:"source_details"`
	VerifiedAt    string             `json:"verified_at"`
	// MixedPer1k 是"混合成本"（输入:输出 = 3:1）—— 只作为同一量纲下的排序键，
	// 不代表任何具体用量假设。用户看到的一直是 input/output 两列原文。
	MixedPer1k float64 `json:"mixed_per_1k"`
}

// 排序白名单。列名绝不能来自请求参数：那是标识符，拼进 SQL 就是注入面。
var pricingSortClauses = map[string]string{
	"model":    "model ASC, provider ASC",
	"input":    "input_per_1k ASC, model ASC",
	"output":   "output_per_1k ASC, model ASC",
	"mixed":    "mixed_per_1k ASC, model ASC",
	"verified": "verified_at DESC, model ASC",
	// 默认排序：**多源已验证的排前面**，再按最近同步时间。
	// 价格页首先要回答"这个数字可信吗"，把单源参考价混在同样位置没有意义。
	"confidence": "verified DESC, verified_at DESC, model ASC",
}

const pricingDefaultLimit = 500

// ListModelPrices 列出已交叉验证的参考价格。
func (h *BillingHandler) ListModelPrices(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	sortKey := strings.ToLower(strings.TrimSpace(q.Get("sort")))
	if sortKey == "" {
		// 默认"可信度优先"：已验证（≥2 源一致）的价格排在前面，
		// 单源/分歧的参考价在后 —— 这是这张表最该先传达的信息。
		sortKey = "confidence"
	}
	orderBy, ok := pricingSortClauses[sortKey]
	if !ok {
		BadRequest(w, "invalid sort: must be one of model, input, output, mixed, verified, confidence")
		return
	}

	if db.GlobalDBManager == nil || !db.GlobalDBManager.IsAvailable() {
		// 与列表端点同口径：库不可用时返回空集而不是 500 —— 价格目录是只读参考，
		// 它挂了不该让整个页面报错。
		// ⚠ 必须在参数校验**之后**：否则非法 sort 在库不可用时会被静默放行，
		// 同一份请求的行为随环境漂移。
		OK(w, map[string]any{"models": []modelPriceRow{}, "count": 0, "note": "database unavailable"})
		return
	}

	models, err := db.GlobalDBManager.Query(r.Context(), `
		SELECT model,
		       provider,
		       input_per_1k::float8   AS input_per_1k,
		       output_per_1k::float8  AS output_per_1k,
		       input_per_1k * 0.75 + output_per_1k * 0.25 AS mixed_per_1k,
		       currency,
		       sources,
		       source_count,
		       verified,
		       source_details,
		       verified_at
		  FROM model_prices
		 WHERE ($1 = '' OR provider = $1)
		   AND ($2 = '' OR model ILIKE '%' || $2 || '%')
		 ORDER BY `+orderBy+`
		 LIMIT $3`,
		strings.TrimSpace(q.Get("provider")),
		strings.TrimSpace(q.Get("q")),
		pricingDefaultLimit)
	if err != nil {
		slog.Error("list model prices failed", "error", err)
		InternalError(w, "failed to list model prices")
		return
	}
	defer models.Close()

	rows := make([]modelPriceRow, 0, 64)
	for models.Next() {
		var (
			row         modelPriceRow
			srcRaw      string
			detailsRaw  []byte
			verifiedRaw any
		)
		if err := models.Scan(&row.Model, &row.Provider, &row.InputPer1k, &row.OutputPer1k,
			&row.MixedPer1k, &row.Currency, &srcRaw, &row.SourceCount, &row.Verified,
			&detailsRaw, &verifiedRaw); err != nil {
			slog.Warn("scan model price row failed", "error", err)
			continue
		}
		// sources 是逗号分隔的源名（写入方 `sync.py` 的口径）。
		row.Sources = splitNonEmpty(srcRaw, ",")
		row.SourceDetails = make([]modelPriceSource, 0, len(row.Sources))
		if len(detailsRaw) > 0 {
			// 每源明细是同步任务写的 jsonb；解析失败不该让整行消失（页面仍能显示汇总）。
			if err := json.Unmarshal(detailsRaw, &row.SourceDetails); err != nil {
				slog.Warn("parse model price source_details failed", "model", row.Model, "error", err)
			}
		}
		row.VerifiedAt = stringOf(verifiedRaw)
		rows = append(rows, row)
	}
	if err := models.Err(); err != nil {
		InternalError(w, "failed to iterate model prices")
		return
	}

	OK(w, map[string]any{
		"models":        rows,
		"count":         len(rows),
		"sort":          sortKey,
		"limit":         pricingDefaultLimit,
		"unit":          "USD per 1k tokens",
		"verified_only": false,
		"note": "目录包含多源印证（verified=true，≥2 源在 5% 容差内一致）与单源参考价" +
			"（verified=false）；展开「印证来源」可看每个源各自的报价。仅供选型参考，不参与计费。",
	})
}

// splitNonEmpty 按分隔符切分并丢掉空段（"a,,b" → ["a","b"]）。
func splitNonEmpty(raw, sep string) []string {
	out := make([]string, 0, 2)
	for _, part := range strings.Split(raw, sep) {
		if p := strings.TrimSpace(part); p != "" {
			out = append(out, p)
		}
	}
	return out
}
