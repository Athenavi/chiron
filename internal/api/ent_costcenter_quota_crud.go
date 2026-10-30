package api

// 本文件由 ent_costcenter_handler.go 按职责外移而来（配额池的增删改查 HTTP 入口）。
// 代码逐字未改：只搬位置，不改逻辑。

import (
	"errors"
	"net/http"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/google/uuid"
)

// ListQuotas GET /v1/ent/quotas?tenant_id=
func (h *EntCostCenterHandler) ListQuotas(w http.ResponseWriter, r *http.Request) {
	claims := requirePerm(w, r, auth.PermAdminRead)
	if claims == nil {
		return
	}
	tenantID := scopedTenantID(claims, r.URL.Query().Get("tenant_id"))
	if claims.Role != "owner" && tenantID == "" {
		Forbidden(w, "tenant context missing")
		return
	}
	pools, err := h.store.ListQuotaPools(r.Context(), tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list quotas failed")
		return
	}
	type poolWithAllocated struct {
		EntQuotaPool
		Allocated int64 `json:"allocated"`
	}
	out := []poolWithAllocated{}
	for _, p := range pools {
		allocated, aErr := h.store.SumAllocated(r.Context(), p.ID)
		if aErr != nil {
			allocated = 0
		}
		out = append(out, poolWithAllocated{EntQuotaPool: p, Allocated: allocated})
	}
	OK(w, map[string]interface{}{"pools": out, "total": len(out)})
}
// CreateQuota POST /v1/ent/quotas
func (h *EntCostCenterHandler) CreateQuota(w http.ResponseWriter, r *http.Request) {
	claims := requirePerm(w, r, auth.PermAdminWrite)
	if claims == nil {
		return
	}
	var body struct {
		TenantID     string `json:"tenant_id"`
		ResourceType string `json:"resource_type"`
		TotalAmount  int64  `json:"total_amount"`
		Period       string `json:"period"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	// 跨租户写防护：非 owner 只能给自己租户建配额池 —— 此前直接采信请求体里的
	// tenant_id，任一持有 admin:write 的租户管理员都能给**别的租户**设配额（越权写）。
	body.TenantID = scopedTenantID(claims, body.TenantID)
	if _, err := uuid.Parse(body.TenantID); err != nil {
		BadRequest(w, "valid tenant_id (uuid) required")
		return
	}
	if !validResourceTypes[body.ResourceType] {
		BadRequest(w, "resource_type must be token/storage_mb/concurrency/credits")
		return
	}
	if body.TotalAmount < 0 {
		BadRequest(w, "total_amount must be >= 0")
		return
	}
	if body.Period == "" {
		body.Period = "monthly"
	}
	if !validPeriods[body.Period] {
		BadRequest(w, "period must be daily or monthly")
		return
	}

	pool := EntQuotaPool{
		TenantID:     body.TenantID,
		ResourceType: body.ResourceType,
		TotalAmount:  body.TotalAmount,
		Period:       body.Period,
	}
	if err := h.store.CreateQuotaPool(r.Context(), &pool); err != nil {
		if errors.Is(err, errQuotaConflict) {
			JSON(w, http.StatusConflict, APIResponse{
				Success: false,
				Error:   "quota pool already exists for this tenant/resource_type/period",
			})
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "create quota failed")
		return
	}
	h.notifyQuotaChanged(r.Context(), &pool, "created")
	Created(w, pool)
}
// GetQuota GET /v1/ent/quotas/{id}
func (h *EntCostCenterHandler) GetQuota(w http.ResponseWriter, r *http.Request) {
	claims := requirePerm(w, r, auth.PermAdminRead)
	if claims == nil {
		return
	}
	id := r.PathValue("id")
	if _, err := uuid.Parse(id); err != nil {
		BadRequest(w, "invalid quota id")
		return
	}
	pool, err := h.store.GetQuotaPool(r.Context(), id)
	if err != nil {
		if errors.Is(err, errQuotaNotFound) {
			NotFound(w, ErrNotFound)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "get quota failed")
		return
	}
	// 跨租户读防护：非 owner 只能看自己租户的配额池（与 UpdateQuota/DeleteQuota 同口径）。
	// 此前缺这一步，任一持有 admin:read 的租户管理员可读他租户的配额池与分配明细。
	if scopedTenantID(claims, pool.TenantID) != pool.TenantID {
		NotFound(w, ErrNotFound)
		return
	}
	allocs, err := h.store.ListAllocations(r.Context(), id)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "get allocations failed")
		return
	}
	allocated, err := h.store.SumAllocated(r.Context(), id)
	if err != nil {
		allocated = 0
	}
	OK(w, map[string]interface{}{
		"pool":        pool,
		"allocations": allocs,
		"allocated":   allocated,
	})
}
// UpdateQuota PUT /v1/ent/quotas/{id}
func (h *EntCostCenterHandler) UpdateQuota(w http.ResponseWriter, r *http.Request) {
	claims := requirePerm(w, r, auth.PermAdminWrite)
	if claims == nil {
		return
	}
	id := r.PathValue("id")
	if _, err := uuid.Parse(id); err != nil {
		BadRequest(w, "invalid quota id")
		return
	}
	var body struct {
		ResourceType *string `json:"resource_type"`
		TotalAmount  *int64  `json:"total_amount"`
		Period       *string `json:"period"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}

	pool, err := h.store.GetQuotaPool(r.Context(), id)
	if err != nil {
		if errors.Is(err, errQuotaNotFound) {
			NotFound(w, ErrNotFound)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "get quota failed")
		return
	}
	// 跨租户写防护：非 owner 不得改**别的租户**的配额池。这里返回 404 而非 403，
	// 避免用状态码差异探测他租户的 pool id 是否存在。
	if scopedTenantID(claims, pool.TenantID) != pool.TenantID {
		NotFound(w, ErrNotFound)
		return
	}
	if body.ResourceType != nil {
		if !validResourceTypes[*body.ResourceType] {
			BadRequest(w, "resource_type must be token/storage_mb/concurrency/credits")
			return
		}
		pool.ResourceType = *body.ResourceType
	}
	if body.TotalAmount != nil {
		if *body.TotalAmount < 0 {
			BadRequest(w, "total_amount must be >= 0")
			return
		}
		pool.TotalAmount = *body.TotalAmount
	}
	if body.Period != nil {
		if !validPeriods[*body.Period] {
			BadRequest(w, "period must be daily or monthly")
			return
		}
		pool.Period = *body.Period
	}

	if err := h.store.UpdateQuotaPool(r.Context(), pool); err != nil {
		if errors.Is(err, errQuotaConflict) {
			JSON(w, http.StatusConflict, APIResponse{
				Success: false,
				Error:   "quota pool already exists for this tenant/resource_type/period",
			})
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "update quota failed")
		return
	}
	h.notifyQuotaChanged(r.Context(), pool, "updated")
	OK(w, pool)
}
// DeleteQuota DELETE /v1/ent/quotas/{id}（级联删除分配）
func (h *EntCostCenterHandler) DeleteQuota(w http.ResponseWriter, r *http.Request) {
	claims := requirePerm(w, r, auth.PermAdminWrite)
	if claims == nil {
		return
	}
	id := r.PathValue("id")
	if _, err := uuid.Parse(id); err != nil {
		BadRequest(w, "invalid quota id")
		return
	}
	// 跨租户写防护：先确认该池属于调用者租户 —— 越权删除他租户配额同样是破坏性写。
	// 返回 404 以免用状态码差异探测他租户的 pool id 是否存在。
	pool, err := h.store.GetQuotaPool(r.Context(), id)
	if err != nil {
		if errors.Is(err, errQuotaNotFound) {
			NotFound(w, ErrNotFound)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "get quota failed")
		return
	}
	if scopedTenantID(claims, pool.TenantID) != pool.TenantID {
		NotFound(w, ErrNotFound)
		return
	}
	if err := h.store.DeleteQuotaPool(r.Context(), id); err != nil {
		if errors.Is(err, errQuotaNotFound) {
			NotFound(w, ErrNotFound)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "delete quota failed")
		return
	}
	NoContent(w)
}
