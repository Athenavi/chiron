package api

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/athenavi/chiron/internal/db"
)

// gdpr.go —— 个人信息导出（数据可携带权，个保法第 45 条 / GDPR Art.20）。
//
// 交付形态是**单个 JSON** 而不是像账单那样出 CSV：
//   - 导出的是"关于我的全部数据"，跨多张表、结构不一，CSV 只能表达二维表；
//   - JSON 可被程序直接消费（用户有权把它交给另一个服务商）。
//
// 三件刻意不做的事：
//  1. **不导出任何认证凭据**：password_hash、totp_secret_enc、
//     totp_recovery_hashes、api_keys.key_hash 一律排除。它们不是"关于用户的
//     数据"而是凭据 —— 导出给用户，等于把攻击面从"数据库泄露"扩大到
//     "用户邮箱/下载目录泄露"。
//  2. **不导出他人数据**：所有查询按 user_id 过滤。
//  3. **不做静默截断**：这是合规交付物，半份数据会误导用户。改为有上限，
//     达到上限时明确告警（而不是让用户以为导全了）。

const (
	// gdprExportMaxRows 是单类数据的上限。达到上限说明该账号体量已不适合
	// 同步接口交付，应走人工/离线性导出。
	gdprExportMaxRows = 20000
	// gdprExportVersion 写进文件，便于支持人员确认口径版本。
	gdprExportVersion = "1"
)

// gdprExport 是导出文件的顶层结构。
type gdprExport struct {
	Version    string                   `json:"version"`
	ExportedAt time.Time                `json:"exported_at"`
	Note       string                   `json:"note"`
	Account    map[string]interface{}   `json:"account"`
	Sessions   []map[string]interface{} `json:"sessions"`
	Credits    []map[string]interface{} `json:"credits"`
	Payments   []map[string]interface{} `json:"payments"`
	// Truncated 列出达到上限而被截断的数据类。
	// 不用静默截断：用户拿到少一半的流水却以为完整，比直接告诉他更糟。
	Truncated []string `json:"truncated,omitempty"`
}

// ExportMyData GET /v1/me/export —— 导出当前登录用户的个人数据。
//
// 身份**只**来自会话（GetUserID），不接受任何 query/body 指定 userID ——
// 否则这个端点就是"下载任意用户全部数据"的入口。
func (h *AuthHandler) ExportMyData(w http.ResponseWriter, r *http.Request) {
	userID := GetUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	ctx := r.Context()
	if db.GlobalDBManager == nil {
		InternalError(w, ErrDBUnavailable)
		return
	}

	out, tenantID, err := buildUserDataExport(ctx, userID)
	if err != nil {
		if errors.Is(err, errExportUserNotFound) {
			Unauthorized(w, ErrAuthRequired)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "export failed")
		return
	}

	// 导出是访问个人数据的敏感操作，必须留痕。
	db.AuditLog(ctx, userID, tenantID, "privacy.data_export", "/v1/me/export",
		"", r.RemoteAddr, map[string]interface{}{
			"sessions":  len(out.Sessions),
			"credits":   len(out.Credits),
			"payments":  len(out.Payments),
			"truncated": out.Truncated,
		})

	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Content-Disposition",
		`attachment; filename="chiron-personal-data-`+shortID(userID)+`.json"`)
	w.WriteHeader(http.StatusOK)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	if err := enc.Encode(out); err != nil {
		// 状态码已发出，只能记日志。
		slog.Error("gdpr export: encode failed", "user_id", userID, "error", err)
	}
}

// errExportUserNotFound 表示账号不存在。
var errExportUserNotFound = errors.New("export: user not found")

// buildUserDataExport 构建个人数据快照。
//
// 抽成独立函数是为了让**归档**（到期清除前生成副本）与用户主动导出共用
// 同一份口径：两份实现会随时间漂移，而漂移的表现是"用户导出的和系统归档的
// 内容不一致"—— 那在合规场景下是硬伤。
func buildUserDataExport(ctx context.Context, userID string) (*gdprExport, int16, error) {
	if db.GlobalDBManager == nil {
		return nil, 0, errors.New("export: database unavailable")
	}

	out := &gdprExport{
		Version:    gdprExportVersion,
		ExportedAt: time.Now().UTC(),
		Note: "本文件包含与你的账号相关的个人数据。出于安全考虑，不包含任何" +
			"认证凭据（密码、双因子密钥、API Key 哈希）。",
	}

	// ── 账户信息 ──
	// 显式列出字段而不是 `SELECT *`：将来新增敏感列时不会因为忘记排除而泄漏。
	var (
		email, name, role string
		tenantID          int16
		createdAt         *time.Time
		phone             *string
		totpEnabled       bool
	)
	if err := db.GlobalDBManager.QueryRow(ctx, `
		SELECT COALESCE(email, ''), COALESCE(name, ''), COALESCE(role, ''),
		       tenant_id, created_at, phone, totp_enabled
		  FROM users WHERE id = $1`, userID).
		Scan(&email, &name, &role, &tenantID, &createdAt, &phone, &totpEnabled); err != nil {
		if isNoRows(err) {
			return nil, 0, errExportUserNotFound
		}
		return nil, 0, err
	}
	out.Account = map[string]interface{}{
		"id":          userID,
		"email":       email, // 用户自己的邮箱：导出就是要给他完整数据，不脱敏
		"name":        name,
		"role":        role,
		"tenant_id":   tenantID,
		"phone":       strPtrOrEmpty(phone),
		"created_at":  timePtrOrNil(createdAt),
		"mfa_enabled": totpEnabled,
	}

	// ── 会话 ──
	sessions, truncSessions, err := gdprSessions(ctx, userID)
	if err != nil {
		return nil, 0, err
	}
	out.Sessions = sessions

	// ── 积分流水 ──
	credits, truncCredits, err := gdprCredits(ctx, userID)
	if err != nil {
		return nil, 0, err
	}
	out.Credits = credits

	// ── 支付记录 ──
	// 不含 qr_code / provider_order_id / trade_no：渠道侧凭据，对用户无意义，
	// 泄漏反而增加被用于客服诈骗的风险。
	payments, truncPayments, err := gdprPayments(ctx, userID)
	if err != nil {
		return nil, 0, err
	}
	out.Payments = payments

	for name, trunc := range map[string]bool{
		"sessions": truncSessions, "credits": truncCredits, "payments": truncPayments,
	} {
		if trunc {
			out.Truncated = append(out.Truncated, name)
		}
	}
	return out, tenantID, nil
}

// gdprQueryRows 执行查询并把每行转成 map。
//
// scan 由调用方提供而不是靠反射：列与字段的对应关系写死，改列名时编译期
// 就能发现，而不是运行期静默错位（那会导出空值，用户看不出异常）。
func gdprQueryRows(ctx context.Context, sql string, args []interface{},
	scan func(pgx.Rows) (map[string]interface{}, error)) ([]map[string]interface{}, bool, error) {
	rows, err := db.GlobalDBManager.Query(ctx, sql, args...)
	if err != nil {
		return nil, false, err
	}
	defer rows.Close()

	out := make([]map[string]interface{}, 0, 16)
	for rows.Next() {
		m, scanErr := scan(rows)
		if scanErr != nil {
			return nil, false, scanErr
		}
		out = append(out, m)
	}
	if err := rows.Err(); err != nil {
		return nil, false, err
	}
	// 多取一行判断是否被截断：行数恰好等于上限时，可能正好导完，
	// 也可能还有更多 —— 用 LIMIT n+1 才能分辨。
	return out, len(out) > gdprExportMaxRows, nil
}

func gdprSessions(ctx context.Context, userID string) ([]map[string]interface{}, bool, error) {
	rows, truncated, err := gdprQueryRows(ctx, `
		SELECT id, COALESCE(title, ''), COALESCE(status, ''), created_at, updated_at
		  FROM sessions WHERE user_id = $1
		 ORDER BY created_at
		 LIMIT $2`, []interface{}{userID, gdprExportMaxRows + 1},
		func(r pgx.Rows) (map[string]interface{}, error) {
			var id, title, status string
			var createdAt, updatedAt *time.Time
			if err := r.Scan(&id, &title, &status, &createdAt, &updatedAt); err != nil {
				return nil, err
			}
			return map[string]interface{}{
				"id":         id,
				"title":      title,
				"status":     status,
				"created_at": timePtrOrNil(createdAt),
				"updated_at": timePtrOrNil(updatedAt),
			}, nil
		})
	if truncated {
		rows = rows[:gdprExportMaxRows]
	}
	return rows, truncated, err
}

func gdprCredits(ctx context.Context, userID string) ([]map[string]interface{}, bool, error) {
	rows, truncated, err := gdprQueryRows(ctx, `
		SELECT id, amount, balance, COALESCE(reason, ''), created_at
		  FROM credit_transactions WHERE user_id = $1
		 ORDER BY created_at
		 LIMIT $2`, []interface{}{userID, gdprExportMaxRows + 1},
		func(r pgx.Rows) (map[string]interface{}, error) {
			var id, reason string
			var amount, balance int
			var createdAt *time.Time
			if err := r.Scan(&id, &amount, &balance, &reason, &createdAt); err != nil {
				return nil, err
			}
			return map[string]interface{}{
				"id":         id,
				"amount":     amount,
				"balance":    balance,
				"reason":     reason,
				"created_at": timePtrOrNil(createdAt),
			}, nil
		})
	if truncated {
		rows = rows[:gdprExportMaxRows]
	}
	return rows, truncated, err
}

func gdprPayments(ctx context.Context, userID string) ([]map[string]interface{}, bool, error) {
	rows, truncated, err := gdprQueryRows(ctx, `
		SELECT id, COALESCE(channel, ''), credits, amount_cents, COALESCE(currency, ''),
		       COALESCE(status, ''), created_at, paid_at
		  FROM payments WHERE user_id = $1
		 ORDER BY created_at
		 LIMIT $2`, []interface{}{userID, gdprExportMaxRows + 1},
		func(r pgx.Rows) (map[string]interface{}, error) {
			var id, channel, currency, status string
			var credits int
			var amountCents int64
			var createdAt, paidAt *time.Time
			if err := r.Scan(&id, &channel, &credits, &amountCents, &currency,
				&status, &createdAt, &paidAt); err != nil {
				return nil, err
			}
			return map[string]interface{}{
				"id":           id,
				"channel":      channel,
				"credits":      credits,
				"amount_cents": amountCents,
				"currency":     currency,
				"status":       status,
				"created_at":   timePtrOrNil(createdAt),
				"paid_at":      timePtrOrNil(paidAt),
			}, nil
		})
	if truncated {
		rows = rows[:gdprExportMaxRows]
	}
	return rows, truncated, err
}

// ── 小工具 ──

func strPtrOrEmpty(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

func timePtrOrNil(p *time.Time) interface{} {
	if p == nil {
		return nil
	}
	return *p
}

// shortID 取 id 前 8 位做文件名后缀（便于用户区分多个账号的导出）。
func shortID(id string) string {
	if len(id) > 8 {
		return id[:8]
	}
	return id
}
