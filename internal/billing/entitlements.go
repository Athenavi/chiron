package billing

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/id"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// Entitlement 是用户已获得的套餐权益（快照），对应 user_entitlements 表。
//
// 为什么字段是"快照"而不是外键到 billing_packages：套餐被运营改价/改权益/下线后，
// 已售出的权益必须保持不变。存 package_code + package_name + allowed_models 三份拷贝，
// 而不是 JOIN 目录现读 —— 与 invoice_requests 对金额做快照同一条理由。
type Entitlement struct {
	ID            string    `json:"id"`
	UserID        string    `json:"user_id"`
	PackageCode   string    `json:"package_code"`
	PackageName   string    `json:"package_name"`
	AllowedModels []string  `json:"allowed_models"`
	PaymentID     string    `json:"payment_id"`
	StartsAt      time.Time `json:"starts_at"`
	ExpiresAt     time.Time `json:"expires_at"`
	CreatedAt     time.Time `json:"created_at"`
}

// Package 是后台可配置的套餐目录项，对应 billing_packages 表。
type Package struct {
	ID            string   `json:"id"`
	Code          string   `json:"code"`
	Name          string   `json:"name"`
	Credits       int      `json:"credits"`
	PriceCents    int64    `json:"price_cents"`
	Currency      string   `json:"currency"`
	DurationDays  int      `json:"duration_days"`
	AllowedModels []string `json:"allowed_models"`
	Enabled       bool     `json:"enabled"`
	SortOrder     int      `json:"sort_order"`
}

// EntitlementStore 是套餐目录与用户权益的持久化接口。
type EntitlementStore interface {
	// PackageByCode 读套餐目录；不存在返回 (nil, nil)。
	PackageByCode(ctx context.Context, code string) (*Package, error)
	// GrantEntitlement 发放一条权益，幂等（payment_id 唯一）。
	// 返回 granted=false 表示该支付单此前已发放过。
	GrantEntitlement(ctx context.Context, e *Entitlement) (bool, error)
	// ActiveEntitlements 返回用户在 now 时刻仍有效的权益（expires_at > now）。
	ActiveEntitlements(ctx context.Context, userID string, now time.Time) ([]Entitlement, error)
	// ListPackages 返回后台已上架的套餐目录（按 sort_order 排序）。
	ListPackages(ctx context.Context) ([]Package, error)
	// UserEntitlements 返回用户的全部权益（**含已过期**），供"我的套餐"页展示到期状态。
	// 只返回生效中的会让"已过期"和"从未购买"在界面上无法区分。
	UserEntitlements(ctx context.Context, userID string) ([]Entitlement, error)
}

// isUniqueViolation 判定 SQLSTATE 23505。
//
// 本期在这里本地实现一份，而不是复用 internal/api 的同名函数：billing 包不能被 api 包
// 反向依赖，而这段判定只有三行，跨包抽公共函数反而增加耦合。
func isUniqueViolation(err error) bool {
	var pe *pgconn.PgError
	return errors.As(err, &pe) && pe.Code == "23505"
}

// PackageByCode 读套餐目录（主库，避免副本延迟让刚上架的套餐买不到）。
func (s *PGStore) PackageByCode(ctx context.Context, code string) (*Package, error) {
	var p Package
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT id, code, name, credits, price_cents, currency, duration_days,
		        COALESCE(allowed_models, ARRAY[]::text[]), enabled, sort_order
		   FROM billing_packages WHERE code = $1`, code).
		Scan(&p.ID, &p.Code, &p.Name, &p.Credits, &p.PriceCents, &p.Currency,
			&p.DurationDays, &p.AllowedModels, &p.Enabled, &p.SortOrder)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, nil
		}
		return nil, fmt.Errorf("query package: %w", err)
	}
	return &p, nil
}

// GrantEntitlement 发放权益。
//
// 幂等靠 ux_user_entitlements_payment 唯一索引：重复回调/重放会撞 23505，
// 这里把它翻译成 granted=false 而非错误 —— 对调用方而言"已经发过"就是成功。
// 不用"先查后插"：并发下两个请求都能查到"没有"，然后都插进去。
func (s *PGStore) GrantEntitlement(ctx context.Context, e *Entitlement) (bool, error) {
	models := e.AllowedModels
	if models == nil {
		models = []string{}
	}
	_, err := db.GlobalDBManager.Exec(ctx,
		`INSERT INTO user_entitlements
		     (id, user_id, package_code, package_name, allowed_models,
		      payment_id, starts_at, expires_at, created_at)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())`,
		e.ID, e.UserID, e.PackageCode, e.PackageName, models,
		e.PaymentID, e.StartsAt, e.ExpiresAt)
	if err != nil {
		if isUniqueViolation(err) {
			return false, nil
		}
		return false, fmt.Errorf("insert entitlement: %w", err)
	}
	return true, nil
}

// ActiveEntitlements 返回 now 时刻仍在有效窗口内的权益。
//
// 生效与否**只由时间决定**（starts_at <= now < expires_at），没有 status 列 ——
// 冗余状态列会造出"状态是 active 但时间已过期"这类两个真相不一致的经典问题。
func (s *PGStore) ActiveEntitlements(ctx context.Context, userID string, now time.Time) ([]Entitlement, error) {
	rows, err := db.GlobalDBManager.Query(ctx,
		`SELECT id, user_id, package_code, package_name,
		        COALESCE(allowed_models, ARRAY[]::text[]), payment_id,
		        starts_at, expires_at, created_at
		   FROM user_entitlements
		  WHERE user_id = $1 AND starts_at <= $2 AND expires_at > $2
		  ORDER BY expires_at DESC`, userID, now)
	if err != nil {
		return nil, fmt.Errorf("query entitlements: %w", err)
	}
	defer rows.Close()

	out := []Entitlement{}
	for rows.Next() {
		var e Entitlement
		if err := rows.Scan(&e.ID, &e.UserID, &e.PackageCode, &e.PackageName,
			&e.AllowedModels, &e.PaymentID, &e.StartsAt, &e.ExpiresAt, &e.CreatedAt); err != nil {
			return nil, fmt.Errorf("scan entitlement: %w", err)
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

// ListPackages 返回已上架套餐，按 sort_order 升序（sort_order 相同再按价格）。
// 只返回 enabled = TRUE：下架套餐不应出现在用户可购买列表里。
func (s *PGStore) ListPackages(ctx context.Context) ([]Package, error) {
	rows, err := db.GlobalDBManager.Query(ctx,
		`SELECT id, code, name, credits, price_cents, currency, duration_days,
		        COALESCE(allowed_models, ARRAY[]::text[]), enabled, sort_order
		   FROM billing_packages
		  WHERE enabled = TRUE
		  ORDER BY sort_order ASC, price_cents ASC, code ASC`)
	if err != nil {
		return nil, fmt.Errorf("list packages: %w", err)
	}
	defer rows.Close()

	out := []Package{}
	for rows.Next() {
		var p Package
		if err := rows.Scan(&p.ID, &p.Code, &p.Name, &p.Credits, &p.PriceCents, &p.Currency,
			&p.DurationDays, &p.AllowedModels, &p.Enabled, &p.SortOrder); err != nil {
			return nil, fmt.Errorf("scan package: %w", err)
		}
		out = append(out, p)
	}
	return out, rows.Err()
}

// UserEntitlements 返回用户的全部权益（含已过期），按到期时间倒序。
//
// 刻意**不加** `expires_at > now()` 过滤：前端要能区分"已过期"与"从未购买"。
// 若只返回生效中的，到期后条目直接消失，用户会以为购买记录丢了 ——
// 这正是"用不了但没提示"的一种形态。
func (s *PGStore) UserEntitlements(ctx context.Context, userID string) ([]Entitlement, error) {
	rows, err := db.GlobalDBManager.Query(ctx,
		`SELECT id, user_id, package_code, package_name,
		        COALESCE(allowed_models, ARRAY[]::text[]), payment_id,
		        starts_at, expires_at, created_at
		   FROM user_entitlements
		  WHERE user_id = $1
		  ORDER BY expires_at DESC, created_at DESC`, userID)
	if err != nil {
		return nil, fmt.Errorf("query user entitlements: %w", err)
	}
	defer rows.Close()

	out := []Entitlement{}
	for rows.Next() {
		var e Entitlement
		if err := rows.Scan(&e.ID, &e.UserID, &e.PackageCode, &e.PackageName,
			&e.AllowedModels, &e.PaymentID, &e.StartsAt, &e.ExpiresAt, &e.CreatedAt); err != nil {
			return nil, fmt.Errorf("scan user entitlement: %w", err)
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

// grantEntitlementForPayment 在支付确认后为套餐订单发放权益。
//
// **必须在 credits 到账之后独立执行，且失败不返回错误**：
// `AddCredits` 已经把 credits 加进余额，此时把订单退回 pending 会让下一次回调再发一次
// credits（钱付一次、额度加两次）。所以权益失败只能显式告警，由幂等重放补发
// （`GrantEntitlement` 撞 23505 即视为已发）。
//
// 这也是为什么权益发放要单独成表而不是塞进 credit_transactions：它的失败可以重放，
// 而余额变更不能。
func (m *Manager) grantEntitlementForPayment(ctx context.Context, p *Payment) {
	if p == nil || p.PackageCode == "" {
		return
	}
	pkg, err := m.store.PackageByCode(ctx, p.PackageCode)
	if err != nil {
		slog.Error("load package for entitlement failed; credits credited but entitlement missing",
			"order", p.ID, "user", p.UserID, "package", p.PackageCode, "error", err)
		return
	}
	if pkg == nil {
		slog.Error("package not found for paid order; credits credited but entitlement missing",
			"order", p.ID, "user", p.UserID, "package", p.PackageCode)
		return
	}

	// starts_at 用 time.Now()，**不用** p.PaidAt。
	//
	// payments.paid_at 是 `timestamp without time zone`（naive 列），写入时由
	// `NOW()` 按会话时区（实测 Asia/Shanghai）落成本地墙钟，而 pgx 把它扫成
	// *time.Time 时会当作 UTC。直接拿来当时间点会整体偏移 8 小时，落到未来，
	// 于是 `starts_at <= now()` 恒不成立 —— 权益刚买完就查不到。
	// 语义上"有效期从支付成功起算"就是"从确认这一刻起算"，用 now 既正确又免于时区换算。
	starts := time.Now()
	// 主键用 id.UUID()：user_entitlements.id 是 varchar(36)，雪花 ID 加前缀会超长，
	// 且 UUID 与 invoices 等既有 varchar(36) 表的生成方式一致。
	entID, err := id.UUID()
	if err != nil {
		slog.Error("generate entitlement id failed; credits credited but entitlement missing",
			"order", p.ID, "user", p.UserID, "package", pkg.Code, "error", err)
		return
	}
	e := &Entitlement{
		ID:            entID,
		UserID:        p.UserID,
		PackageCode:   pkg.Code,
		PackageName:   pkg.Name,
		AllowedModels: pkg.AllowedModels,
		PaymentID:     p.ID,
		StartsAt:      starts,
		ExpiresAt:     starts.AddDate(0, 0, pkg.DurationDays),
	}
	granted, err := m.store.GrantEntitlement(ctx, e)
	if err != nil {
		slog.Error("grant entitlement failed; credits credited but entitlement missing",
			"order", p.ID, "user", p.UserID, "package", pkg.Code, "error", err)
		return
	}
	if !granted {
		slog.Info("entitlement already granted (idempotent replay)",
			"order", p.ID, "user", p.UserID, "package", pkg.Code)
		return
	}
	// 刻意**不** publish CreditEvent：那是"余额变更"通道，
	// EnterpriseBillingObserver 会把每条事件写成 billing_records 的一行成本记录。
	// 权益发放没有改变余额（credits 已在 AddCredits 里入账并单独 publish 过），
	// 再发一条 Amount=0 的事件只会在成本报表里造出一行金额为 0 的假记录。
	slog.Info("entitlement granted",
		"order", p.ID, "user", p.UserID, "package", pkg.Code,
		"expires_at", e.ExpiresAt)
}
