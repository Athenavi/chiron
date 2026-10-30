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
	// ExpiringEntitlements 返回在 (now, now+withinDays] 内到期、且当前仍有效的权益。
	// 供到期预警扫描使用（P2-3）：**跨全部用户**，不做 user 过滤。
	ExpiringEntitlements(ctx context.Context, now time.Time, withinDays int) ([]Entitlement, error)
	// ListPackages 返回后台已上架的套餐目录（按 sort_order 排序）。
	ListPackages(ctx context.Context) ([]Package, error)
	// UserEntitlements 返回用户的全部权益（**含已过期**），供"我的套餐"页展示到期状态。
	// 只返回生效中的会让"已过期"和"从未购买"在界面上无法区分。
	UserEntitlements(ctx context.Context, userID string) ([]Entitlement, error)

	// ── 管理端（P2-2c）──

	// AdminListPackages 返回**全部**套餐（含已下架），供后台管理页使用。
	// 与 ListPackages 刻意分开：后者是用户侧视图（enabled=true 才有意义），
	// 后台必须看得到下架项，否则"下架后它还在列表里吗"无从确认。
	AdminListPackages(ctx context.Context) ([]Package, error)
	// CreatePackage 新建套餐。code 重复返回 ErrPackageCodeExists。
	CreatePackage(ctx context.Context, p *Package) error
	// UpdatePackage 按 id 更新可编辑字段；返回 false 表示该 id 不存在。
	//
	// **code 不在可编辑字段内**：它是 `payments.package_code` 的引用值，改了会让
	// 未确认的订单在 `PackageByCode` 里查不到套餐 —— 于是钱付了、权益发不出来。
	UpdatePackage(ctx context.Context, p *Package) (bool, error)
	// SetPackageEnabled 上架/下架；返回 false 表示该 id 不存在。
	// 下架而非删除：已售权益是快照不受影响，但历史订单要靠 code 反查套餐名。
	SetPackageEnabled(ctx context.Context, id string, enabled bool) (bool, error)
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

// ExpiringEntitlements 返回在 (now, now+withinDays] 内到期、且当前仍有效的权益。
//
// 跨全部用户扫描，供到期预警（P2-3）使用。三条边界都要写清：
//   - `expires_at > now`：已过期的不再预警。过期后权益已失效，再发"即将到期"
//     是错误信息；过期提示由"我的套餐"页与模型准入的 403 承担。
//   - `expires_at <= now + withinDays`：只取窗口内的。
//   - `starts_at <= now`：尚未生效的权益（理论上不会有，start 恒为购买时刻）
//     不发预警。
//
// 走 idx_user_entitlements_user_expiry 的 expires_at 部分；尽管该索引以 user_id
// 打头，这里的过滤条件只用到 expires_at，全表扫描在小数据量下可接受，而扫描
// 周期是小时级、不是请求路径。
func (s *PGStore) ExpiringEntitlements(ctx context.Context, now time.Time, withinDays int) ([]Entitlement, error) {
	if withinDays <= 0 {
		withinDays = 1
	}
	rows, err := db.GlobalDBManager.Query(ctx,
		`SELECT id, user_id, package_code, package_name,
		        COALESCE(allowed_models, ARRAY[]::text[]), payment_id,
		        starts_at, expires_at, created_at
		   FROM user_entitlements
		  WHERE starts_at <= $1
		    AND expires_at > $1
		    AND expires_at <= $1 + make_interval(days => $2)
		  ORDER BY expires_at ASC`, now, withinDays)
	if err != nil {
		return nil, fmt.Errorf("query expiring entitlements: %w", err)
	}
	defer rows.Close()

	out := []Entitlement{}
	for rows.Next() {
		var e Entitlement
		if err := rows.Scan(&e.ID, &e.UserID, &e.PackageCode, &e.PackageName,
			&e.AllowedModels, &e.PaymentID, &e.StartsAt, &e.ExpiresAt, &e.CreatedAt); err != nil {
			return nil, fmt.Errorf("scan expiring entitlement: %w", err)
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
//
// 返回成功发放的权益（未发放/幂等重放/失败都返回 nil）：调用方据此拼通知内容，
// 免得为了拿套餐名与到期时间再查一次库。**错误仍然不上抛**，原因见上。
func (m *Manager) grantEntitlementForPayment(ctx context.Context, p *Payment) *Entitlement {
	if p == nil || p.PackageCode == "" {
		return nil
	}
	pkg, err := m.store.PackageByCode(ctx, p.PackageCode)
	if err != nil {
		slog.Error("load package for entitlement failed; credits credited but entitlement missing",
			"order", p.ID, "user", p.UserID, "package", p.PackageCode, "error", err)
		return nil
	}
	if pkg == nil {
		slog.Error("package not found for paid order; credits credited but entitlement missing",
			"order", p.ID, "user", p.UserID, "package", p.PackageCode)
		return nil
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
		return nil
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
		return nil
	}
	if !granted {
		slog.Info("entitlement already granted (idempotent replay)",
			"order", p.ID, "user", p.UserID, "package", pkg.Code)
		return nil
	}
	// 刻意**不** publish CreditEvent：那是"余额变更"通道，
	// EnterpriseBillingObserver 会把每条事件写成 billing_records 的一行成本记录。
	// 权益发放没有改变余额（credits 已在 AddCredits 里入账并单独 publish 过），
	// 再发一条 Amount=0 的事件只会在成本报表里造出一行金额为 0 的假记录。
	slog.Info("entitlement granted",
		"order", p.ID, "user", p.UserID, "package", pkg.Code,
		"expires_at", e.ExpiresAt)
	return e
}

// ── 套餐目录管理端（P2-2c）────────────────────────────────────────────────

// ErrPackageCodeExists 表示套餐 code 已被占用（撞 `ux_billing_packages_code`）。
//
// 单独定义错误而不是让调用方判 23505：handler 需要把"code 冲突"与其它写失败
// 分成 409 / 500 两种响应，靠 SQLSTATE 字符串在 api 层做分支既脆又难测。
var ErrPackageCodeExists = errors.New("package code already exists")

// 套餐目录的统一列清单，避免四个查询各写一份导致扫描错位。
const packageColumns = `id, code, name, credits, price_cents, currency, duration_days,
	        COALESCE(allowed_models, ARRAY[]::text[]), enabled, sort_order`

func scanPackageRow(row interface{ Scan(...any) error }) (*Package, error) {
	var p Package
	if err := row.Scan(&p.ID, &p.Code, &p.Name, &p.Credits, &p.PriceCents, &p.Currency,
		&p.DurationDays, &p.AllowedModels, &p.Enabled, &p.SortOrder); err != nil {
		return nil, err
	}
	return &p, nil
}

// AdminListPackages 返回**全部**套餐（含已下架），供后台管理页。
//
// 排序把启用项排在前面：运营打开页面最常做的是"改还在卖的"，下架项通常只是
// 留着看历史，压在列表底部更顺手。
func (s *PGStore) AdminListPackages(ctx context.Context) ([]Package, error) {
	rows, err := db.GlobalDBManager.Query(ctx,
		`SELECT `+packageColumns+`
		   FROM billing_packages
		  ORDER BY enabled DESC, sort_order ASC, price_cents ASC, code ASC`)
	if err != nil {
		return nil, fmt.Errorf("admin list packages: %w", err)
	}
	defer rows.Close()

	out := []Package{}
	for rows.Next() {
		p, err := scanPackageRow(rows)
		if err != nil {
			return nil, fmt.Errorf("scan admin package: %w", err)
		}
		out = append(out, *p)
	}
	return out, rows.Err()
}

// CreatePackage 新建套餐；主键在 store 内生成，调用方不必关心。
//
// 主键用 id.UUID()：`billing_packages.id` 是 varchar(36)，与 user_entitlements
// 保持一致（雪花 ID 加前缀会超长）。
func (s *PGStore) CreatePackage(ctx context.Context, p *Package) error {
	if p == nil {
		return errors.New("nil package")
	}
	pkgID, err := id.UUID()
	if err != nil {
		return fmt.Errorf("generate package id: %w", err)
	}
	p.ID = pkgID

	models := p.AllowedModels
	if models == nil {
		models = []string{}
	}
	_, err = db.GlobalDBManager.Exec(ctx,
		`INSERT INTO billing_packages
		     (id, code, name, credits, price_cents, currency, duration_days,
		      allowed_models, enabled, sort_order, created_at, updated_at)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW(), NOW())`,
		p.ID, p.Code, p.Name, p.Credits, p.PriceCents, p.Currency, p.DurationDays,
		models, p.Enabled, p.SortOrder)
	if err != nil {
		if isUniqueViolation(err) {
			return ErrPackageCodeExists
		}
		return fmt.Errorf("insert package: %w", err)
	}
	return nil
}

// UpdatePackage 按 id 更新可编辑字段；返回 false 表示该 id 不存在。
//
// **刻意不更新 code**：`payments.package_code` 存的就是它，改掉会让还没确认的
// 订单在 `PackageByCode` 里查不到套餐 —— 钱付了、权益发不出来。code 只能新建。
func (s *PGStore) UpdatePackage(ctx context.Context, p *Package) (bool, error) {
	if p == nil {
		return false, errors.New("nil package")
	}
	models := p.AllowedModels
	if models == nil {
		models = []string{}
	}
	tag, err := db.GlobalDBManager.Exec(ctx,
		`UPDATE billing_packages
		    SET name = $2, credits = $3, price_cents = $4, currency = $5,
		        duration_days = $6, allowed_models = $7, sort_order = $8,
		        updated_at = NOW()
		  WHERE id = $1`,
		p.ID, p.Name, p.Credits, p.PriceCents, p.Currency, p.DurationDays,
		models, p.SortOrder)
	if err != nil {
		return false, fmt.Errorf("update package: %w", err)
	}
	return tag.RowsAffected() > 0, nil
}

// SetPackageEnabled 上架/下架；返回 false 表示该 id 不存在。
//
// 下架而不是删除：已售权益是快照、不受影响，但历史订单要靠 code 反查套餐名，
// 物理删除会让"我当初买的到底是什么"永久失联。
func (s *PGStore) SetPackageEnabled(ctx context.Context, id string, enabled bool) (bool, error) {
	tag, err := db.GlobalDBManager.Exec(ctx,
		`UPDATE billing_packages SET enabled = $2, updated_at = NOW() WHERE id = $1`,
		id, enabled)
	if err != nil {
		return false, fmt.Errorf("set package enabled: %w", err)
	}
	return tag.RowsAffected() > 0, nil
}
