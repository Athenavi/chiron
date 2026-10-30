package api

import (
	"context"
	"errors"
	"net/http"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/enterprise"
	"github.com/jackc/pgx/v5"
)

// ent_identity_roles.go —— 企业身份的**角色**子域。
//
// 由 ent_identity_handler.go 按子域拆出（L3-4 批次 A，同包纯移动，逐字未改）。
// 与另外三个子域（用户 / 群组 / 租户）共享同一个 EntIdentityHandler 类型与
// 内部辅助函数，因此本文件不定义类型，只挂方法。
// ── 角色 CRUD ───────────────────────────────────────────

type entRoleResponse struct {
	ID          string    `json:"id"`
	Name        string    `json:"name"`
	DisplayName string    `json:"display_name"`
	IsBuiltin   bool      `json:"is_builtin"`
	Permissions []string  `json:"permissions"`
	UserCount   int       `json:"user_count,omitempty"`
	CreatedAt   time.Time `json:"created_at"`
	UpdatedAt   time.Time `json:"updated_at"`
}

func (h *EntIdentityHandler) scanRole(ctx context.Context, id string, withUserCount bool, scope string) (*entRoleResponse, error) {
	userCountExpr := `0`
	if withUserCount {
		userCountExpr = `(SELECT COUNT(*)::int FROM ent_user_roles ur WHERE ur.role_id = r.id)`
	}
	var role entRoleResponse
	var displayName *string
	err := h.db.QueryRow(ctx,
		`SELECT r.id, r.name, r.display_name, r.is_builtin, COALESCE(r.permissions, '{}'),
		        `+userCountExpr+`, r.created_at, r.updated_at
		 FROM ent_roles r WHERE r.id = $1 AND ($2 = '' OR r.tenant_id::text = $2)`, id, scope).
		Scan(&role.ID, &role.Name, &displayName, &role.IsBuiltin, &role.Permissions,
			&role.UserCount, &role.CreatedAt, &role.UpdatedAt)
	if err != nil {
		return nil, err
	}
	if displayName != nil {
		role.DisplayName = *displayName
	}
	if role.Permissions == nil {
		role.Permissions = []string{}
	}
	return &role, nil
}

// ListRoles GET /v1/ent/roles
func (h *EntIdentityHandler) ListRoles(w http.ResponseWriter, r *http.Request) {
	scope, isOwner := entTenantScope(auth.GetClaims(r.Context()))
	if !isOwner && scope == "" {
		Forbidden(w, "tenant context missing")
		return
	}
	rows, err := h.db.Query(r.Context(),
		`SELECT r.id FROM ent_roles r WHERE ($1 = '' OR r.tenant_id::text = $1) ORDER BY r.is_builtin DESC, r.name`,
		scope)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	defer rows.Close()

	items := make([]entRoleResponse, 0)
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
			return
		}
		role, err := h.scanRole(r.Context(), id, true, scope)
		if err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
			return
		}
		items = append(items, *role)
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	OK(w, items)
}

// GetRole GET /v1/ent/roles/{id}
func (h *EntIdentityHandler) GetRole(w http.ResponseWriter, r *http.Request) {
	scope, isOwner := entTenantScope(auth.GetClaims(r.Context()))
	if !isOwner && scope == "" {
		Forbidden(w, "tenant context missing")
		return
	}
	id := r.PathValue("id")
	if !isValidUUID(id) {
		BadRequest(w, "invalid role id")
		return
	}
	role, err := h.scanRole(r.Context(), id, true, scope)
	if errors.Is(err, pgx.ErrNoRows) {
		NotFound(w, ErrNotFound)
		return
	}
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	OK(w, role)
}

type createRoleRequest struct {
	Name        string   `json:"name"`
	DisplayName string   `json:"display_name"`
	Permissions []string `json:"permissions"`
}

// CreateRole POST /v1/ent/roles（新建角色一律 is_builtin=false）。
func (h *EntIdentityHandler) CreateRole(w http.ResponseWriter, r *http.Request) {
	var req createRoleRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if req.Name == "" || len(req.Name) > 64 {
		BadRequest(w, "name is required (max 64 chars)")
		return
	}
	if req.Permissions == nil {
		req.Permissions = []string{}
	}

	claims := auth.GetClaims(r.Context())
	tenantID := db.DefaultTenantID
	if claims != nil && claims.TenantID != 0 {
		tenantID = claims.TenantID
	}
	role, err := func() (*entRoleResponse, error) {
		var id string
		err := h.db.QueryRow(r.Context(),
			`INSERT INTO ent_roles (tenant_id, name, display_name, is_builtin, permissions)
			 VALUES ($1, $2, $3, FALSE, $4) RETURNING id`,
			tenantID, req.Name, req.DisplayName, req.Permissions).Scan(&id)
		if err != nil {
			return nil, err
		}
		return h.scanRole(r.Context(), id, true, tenantStr(tenantID))
	}()
	if err != nil {
		if isUniqueViolation(err) {
			logAndRespond(w, err, http.StatusConflict, "role name already exists")
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	Created(w, role)
}

type updateRoleRequest struct {
	Name        *string   `json:"name"`
	DisplayName *string   `json:"display_name"`
	Permissions *[]string `json:"permissions"`
}

// UpdateRole PUT /v1/ent/roles/{id}。
// 内置角色禁止修改 name/permissions，违反返回 409。
func (h *EntIdentityHandler) UpdateRole(w http.ResponseWriter, r *http.Request) {
	scope, isOwner := entTenantScope(auth.GetClaims(r.Context()))
	if !isOwner && scope == "" {
		Forbidden(w, "tenant context missing")
		return
	}
	id := r.PathValue("id")
	if !isValidUUID(id) {
		BadRequest(w, "invalid role id")
		return
	}
	var req updateRoleRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	ctx := r.Context()

	existing, err := h.scanRole(ctx, id, false, scope)
	if errors.Is(err, pgx.ErrNoRows) {
		NotFound(w, ErrNotFound)
		return
	}
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	newName, newDisplay, newPerms := existing.Name, existing.DisplayName, existing.Permissions
	if req.Name != nil {
		if *req.Name == "" || len(*req.Name) > 64 {
			BadRequest(w, "invalid name")
			return
		}
		newName = *req.Name
	}
	if req.DisplayName != nil {
		newDisplay = *req.DisplayName
	}
	if req.Permissions != nil {
		newPerms = *req.Permissions
		if newPerms == nil {
			newPerms = []string{}
		}
	}

	if existing.IsBuiltin &&
		(newName != existing.Name || (req.Permissions != nil && !equalStringSet(newPerms, existing.Permissions))) {
		logAndRespond(w, errors.New("builtin role is immutable (name/permissions)"),
			http.StatusConflict, "builtin role cannot be deleted or have permissions changed")
		return
	}

	if _, err := h.db.Exec(ctx,
		`UPDATE ent_roles SET name = $2, display_name = $3, permissions = $4, updated_at = NOW() WHERE id = $1 AND ($5 = '' OR tenant_id::text = $5)`,
		id, newName, newDisplay, newPerms, scope); err != nil {
		if isUniqueViolation(err) {
			logAndRespond(w, err, http.StatusConflict, "role name already exists")
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	// 权限变更后失效受影响用户缓存（直接绑定 + 经群组间接绑定）
	if req.Permissions != nil {
		for _, uid := range h.usersAffectedByRole(ctx, id) {
			enterprise.InvalidateUserPerms(ctx, uid)
		}
	}
	OK(w, map[string]string{"status": "updated"})
}

// DeleteRole DELETE /v1/ent/roles/{id}。内置角色禁止删除（409）。
func (h *EntIdentityHandler) DeleteRole(w http.ResponseWriter, r *http.Request) {
	scope, isOwner := entTenantScope(auth.GetClaims(r.Context()))
	if !isOwner && scope == "" {
		Forbidden(w, "tenant context missing")
		return
	}
	id := r.PathValue("id")
	if !isValidUUID(id) {
		BadRequest(w, "invalid role id")
		return
	}
	ctx := r.Context()

	existing, err := h.scanRole(ctx, id, false, scope)
	if errors.Is(err, pgx.ErrNoRows) {
		NotFound(w, ErrNotFound)
		return
	}
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if existing.IsBuiltin {
		logAndRespond(w, errors.New("builtin role cannot be deleted"),
			http.StatusConflict, "builtin role cannot be deleted or have permissions changed")
		return
	}

	// 先收集受影响用户（级联删除后关联即消失），再执行删除
	affected := h.usersAffectedByRole(ctx, id)
	tag, err := h.db.Exec(ctx, `DELETE FROM ent_roles WHERE id = $1 AND ($2 = '' OR tenant_id::text = $2)`, id, scope)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if tag.RowsAffected() == 0 {
		NotFound(w, ErrNotFound)
		return
	}
	for _, uid := range affected {
		enterprise.InvalidateUserPerms(ctx, uid)
	}
	OK(w, map[string]string{"status": "deleted"})
}
