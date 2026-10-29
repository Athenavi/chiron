package api

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"

	"github.com/athenavi/chiron/internal/billing"
	"github.com/athenavi/chiron/internal/db"
)

// billing_packages_handler.go —— 套餐目录的后台管理端（P2-2c）。
//
// 为什么要这一层：P2-2 只做到「能买到」，`billing_packages` 里有什么只能手工
// INSERT。运营改一次价格要连数据库，而价格直接影响真实收款 —— 这是最容易
// 出错且最不该走手工 SQL 的一类操作。
//
// 权限：整组挂 `PermBillingManage`（见 auth.PermBillingManage 注释：定价属
// "经营决策"而非"日常运维"，独立成点后可只给运营）。读也用它而不是
// PermAdminRead —— 能看到成本与定价的人应该是同一批。

// billingPackageRequest 是新建/更新套餐的请求体。
//
// Code 只在新建时生效：它同时是 `payments.package_code` 的引用值，改动会让
// 尚未确认的订单在 `PackageByCode` 里查不到套餐 —— 钱收了、权益发不出来。
// 因此更新时该字段被静默忽略，而不是返回错误（前端复用同一个表单）。
type billingPackageRequest struct {
	Code          string   `json:"code"`
	Name          string   `json:"name"`
	Credits       int      `json:"credits"`
	PriceCents    int64    `json:"price_cents"`
	Currency      string   `json:"currency"`
	DurationDays  int      `json:"duration_days"`
	AllowedModels []string `json:"allowed_models"`
	SortOrder     int      `json:"sort_order"`
}

// RegisterAdminRoutes 挂载套餐目录 CRUD。
//
// 与发票审核同一写法：注册在 adminMux 内部，外层 mux 已做
// `StripPrefix("/v1/admin", adminMux)`，只有在这里注册才能拿到 {id} 路径参数。
func (h *BillingHandler) RegisterAdminRoutes(adminMux *http.ServeMux) {
	adminMux.HandleFunc("GET /billing/packages", h.AdminListPackages)
	adminMux.HandleFunc("POST /billing/packages", h.AdminCreatePackage)
	adminMux.HandleFunc("PUT /billing/packages/{id}", h.AdminUpdatePackage)
	// DELETE 是**软下架**（enabled=false），不是物理删除：已售权益是快照不受影响，
	// 但历史订单要靠 code 反查套餐名，删行会让"我当初买的到底是什么"永久失联。
	adminMux.HandleFunc("DELETE /billing/packages/{id}", h.AdminDisablePackage)
	adminMux.HandleFunc("POST /billing/packages/{id}/restore", h.AdminEnablePackage)
}

// AdminListPackages 返回全部套餐（含已下架）。
func (h *BillingHandler) AdminListPackages(w http.ResponseWriter, r *http.Request) {
	pkgs, err := h.mgr.AdminListPackages(r.Context())
	if err != nil {
		slog.Error("admin list packages failed", "error", err)
		InternalError(w, "failed to list packages")
		return
	}
	OK(w, pkgs)
}

// AdminCreatePackage 新建套餐。
func (h *BillingHandler) AdminCreatePackage(w http.ResponseWriter, r *http.Request) {
	var body billingPackageRequest
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	pkg, problems, err := h.buildPackage(r.Context(), body, true)
	if err != nil {
		InternalError(w, err.Error())
		return
	}
	if len(problems) > 0 {
		unprocessablePackage(w, problems)
		return
	}

	if err := h.mgr.CreatePackage(r.Context(), pkg); err != nil {
		if errors.Is(err, billing.ErrPackageCodeExists) {
			// 409 而不是 422：code 唯一是数据库层面的冲突，前端据此提示"换个 code"，
			// 与"格式不对"是可区分的两类失败。
			JSON(w, http.StatusConflict, APIResponse{
				Success: false,
				Error:   fmt.Sprintf("package code %q already exists", pkg.Code),
			})
			return
		}
		slog.Error("create package failed", "code", pkg.Code, "error", err)
		InternalError(w, "failed to create package")
		return
	}
	Created(w, pkg)
}

// AdminUpdatePackage 更新套餐可编辑字段（code 不可变）。
func (h *BillingHandler) AdminUpdatePackage(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimSpace(r.PathValue("id"))
	if id == "" {
		BadRequest(w, "package id is required")
		return
	}
	var body billingPackageRequest
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	pkg, problems, err := h.buildPackage(r.Context(), body, false)
	if err != nil {
		InternalError(w, err.Error())
		return
	}
	if len(problems) > 0 {
		unprocessablePackage(w, problems)
		return
	}
	pkg.ID = id

	found, err := h.mgr.UpdatePackage(r.Context(), pkg)
	if err != nil {
		slog.Error("update package failed", "id", id, "error", err)
		InternalError(w, "failed to update package")
		return
	}
	if !found {
		NotFound(w, "package not found")
		return
	}
	OK(w, pkg)
}

// AdminDisablePackage 下架套餐（软删除）。
func (h *BillingHandler) AdminDisablePackage(w http.ResponseWriter, r *http.Request) {
	h.setPackageEnabled(w, r, false)
}

// AdminEnablePackage 重新上架套餐。
func (h *BillingHandler) AdminEnablePackage(w http.ResponseWriter, r *http.Request) {
	h.setPackageEnabled(w, r, true)
}

func (h *BillingHandler) setPackageEnabled(w http.ResponseWriter, r *http.Request, enabled bool) {
	id := strings.TrimSpace(r.PathValue("id"))
	if id == "" {
		BadRequest(w, "package id is required")
		return
	}
	found, err := h.mgr.SetPackageEnabled(r.Context(), id, enabled)
	if err != nil {
		slog.Error("set package enabled failed", "id", id, "enabled", enabled, "error", err)
		InternalError(w, "failed to update package")
		return
	}
	if !found {
		NotFound(w, "package not found")
		return
	}
	OK(w, map[string]interface{}{"id": id, "enabled": enabled})
}

// unprocessablePackage 把参数问题一次性全部返回。
//
// 逐条只报第一个错会让人机来回好几轮（改一个、再提交、再报下一个），
// 而这里的问题都是彼此独立的字段校验。
func unprocessablePackage(w http.ResponseWriter, problems []string) {
	JSON(w, http.StatusUnprocessableEntity, APIResponse{
		Success: false,
		Error:   strings.Join(problems, "; "),
	})
}

// buildPackage 把请求体规范化成待写入的 Package，并收集字段问题。
//
// 返回 error 只表示**基础设施故障**（如模型表查不到），字段问题一律进 problems ——
// 两者混在一起会让"数据库连不上"和"价格填了负数"都变成同一种 422。
func (h *BillingHandler) buildPackage(ctx context.Context, body billingPackageRequest, requireCode bool) (*billing.Package, []string, error) {
	problems := []string{}

	code := strings.TrimSpace(body.Code)
	if requireCode {
		if code == "" {
			problems = append(problems, "code is required")
		} else if len(code) > 64 {
			// payments.package_code 是 varchar(64)：超长会在回调发权益时才炸，
			// 那时钱已经收了。必须在这里拦。
			problems = append(problems, "code must be at most 64 characters")
		}
	}
	name := strings.TrimSpace(body.Name)
	if name == "" {
		problems = append(problems, "name is required")
	} else if len([]rune(name)) > 128 {
		problems = append(problems, "name must be at most 128 characters")
	}

	// 与迁移里的 CHECK 保持一致：credits>=0、price_cents>=0、duration_days>0。
	// 在 handler 拦下来是为了给出可读原因，而不是把 23514 变成 500。
	if body.Credits < 0 {
		problems = append(problems, "credits must be >= 0")
	}
	if body.PriceCents < 0 {
		problems = append(problems, "price_cents must be >= 0")
	}
	if body.DurationDays <= 0 {
		problems = append(problems, "duration_days must be > 0")
	}

	// 币种只允许下单链路真正支持的两种：支付宝/微信收人民币，PayPal 收美元。
	// 放行其它币种会让套餐建得成、买不了（下单时按 wantCurrency 拒绝）。
	currency := strings.ToUpper(strings.TrimSpace(body.Currency))
	switch currency {
	case "":
		currency = "CNY"
	case "CNY", "USD":
	default:
		problems = append(problems, "currency must be CNY or USD")
	}

	models, err := normalizePackageModels(ctx, body.AllowedModels)
	if err != nil {
		return nil, nil, err
	}
	if len(models.problems) > 0 {
		problems = append(problems, models.problems...)
	}

	return &billing.Package{
		Code:          code,
		Name:          name,
		Credits:       body.Credits,
		PriceCents:    body.PriceCents,
		Currency:      currency,
		DurationDays:  body.DurationDays,
		AllowedModels: models.models,
		Enabled:       true,
		SortOrder:     body.SortOrder,
	}, problems, nil
}

// packageModelResult 是模型清单的规范化结果。
type packageModelResult struct {
	models   []string
	problems []string
}

// normalizePackageModels 去重去空、并校验每个模型名真实存在。
//
// **不复用 validateAllowedModels**：那个函数查的是 `admin_model_configs.model_id`，
// 而真库该表实测 0 行、`llm_models` 40 行、两者零交集。拿它校验会把所有合法
// 模型名都判成非法（P2-2b 已在 `ResolveAllowedModels` 上踩过同一个坑）。
// 套餐权益最终是拿 `llm_models.name` 去和请求体里的 model 比对，所以这里也必须
// 以 `llm_models.name` 为准，两边口径才会一致。
func normalizePackageModels(ctx context.Context, raw []string) (*packageModelResult, error) {
	seen := map[string]bool{}
	models := []string{}
	for _, m := range raw {
		v := strings.TrimSpace(m)
		if v == "" || seen[v] {
			continue
		}
		seen[v] = true
		models = append(models, v)
	}
	// 空清单 = 不限制模型，本就有意义，无需查表。
	if len(models) == 0 {
		return &packageModelResult{models: []string{}}, nil
	}

	known, err := packageModelNameSet(ctx)
	if err != nil {
		return nil, fmt.Errorf("failed to validate allowed models: %w", err)
	}
	problems := []string{}
	for _, m := range models {
		if !known[m] {
			problems = append(problems, fmt.Sprintf("unknown model: %s", m))
		}
	}
	return &packageModelResult{models: models, problems: problems}, nil
}

// packageModelNameSet 返回 `llm_models.name` 全集。
//
// 刻意不限 enabled：模型被临时停用后，套餐里已有的权益不该因此变得"编辑不了"。
// 真正的可用性判定发生在请求时刻（P2-2b 的 EffectiveModelSet）。
func packageModelNameSet(ctx context.Context) (map[string]bool, error) {
	pool := db.ReadPool()
	if pool == nil {
		return nil, errors.New("postgres pool unavailable")
	}
	rows, err := pool.Query(ctx, `SELECT name FROM llm_models`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	set := map[string]bool{}
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			return nil, err
		}
		set[name] = true
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	// 表为空说明模型清单还没同步。此时放行会让套餐写进根本不存在的模型名，
	// 用户买了才发现用不了 —— 明确拒绝，提示先同步模型。
	if len(set) == 0 {
		return nil, errors.New("no models registered yet; sync models before configuring a package")
	}
	return set, nil
}
