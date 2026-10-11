package api

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/enterprise"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// ── DB 访问抽象 ──────────────────────────────────────────
//
// 生产实现 pgEntStore 委托 db.ReadPool()/db.Pool（沿用 rbac.go 的读写分离模式）；
// 抽象成接口是为了单元测试可以注入 fake（覆盖内置角色 409 保护等分支）。

type entQuerier interface {
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
}

var errEntDBUnavailable = errors.New("ent: database unavailable")

// pgEntStore 是 entQuerier 的生产实现：读走 ReadPool，写走主 Pool。
type pgEntStore struct{}

func (pgEntStore) Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error) {
	pool := db.ReadPool()
	if pool == nil {
		return nil, errEntDBUnavailable
	}
	return pool.Query(ctx, sql, args...)
}

func (pgEntStore) QueryRow(ctx context.Context, sql string, args ...any) pgx.Row {
	pool := db.ReadPool()
	if pool == nil {
		return deadRow{err: errEntDBUnavailable}
	}
	return pool.QueryRow(ctx, sql, args...)
}

func (pgEntStore) Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	if db.Pool == nil {
		return pgconn.CommandTag{}, errEntDBUnavailable
	}
	return db.GlobalDBManager.Exec(ctx, sql, args...)
}

// deadRow 在连接池缺失时返回确定性错误（避免 nil pgx.Row panic）。
type deadRow struct{ err error }

func (d deadRow) Scan(dest ...any) error { return d.err }

// ── Handler ─────────────────────────────────────────────

// EntIdentityHandler 企业版账号身份管理（用户角色/群组/租户查询）。
// 所有端点由 authMW + RequireEntPerm("ent:manage") 保护（见 RegisterRoutes）。
type EntIdentityHandler struct {
	db entQuerier
}

func NewEntIdentityHandler() *EntIdentityHandler {
	return &EntIdentityHandler{db: pgEntStore{}}
}

// RegisterRoutes 挂载身份管理路由。authMW 为网关 JWT 认证中间件，
// 每个端点再叠加 RequireEntPerm("ent:manage") 企业权限校验。
// 注意：本方法供 Phase 7 集成任务调用，当前不在 gateway_router.go 注册。
func (h *EntIdentityHandler) RegisterRoutes(mux *http.ServeMux, authMW func(http.Handler) http.Handler) {
	ent := func(hf http.HandlerFunc) http.Handler {
		return authMW(RequireEntPerm("ent:manage")(hf))
	}
	mux.Handle("GET /v1/ent/users", ent(h.ListUsers))
	mux.Handle("PUT /v1/ent/users/{id}/roles", ent(h.SetUserRoles))
	mux.Handle("PUT /v1/ent/users/{id}/groups", ent(h.SetUserGroups))

	mux.Handle("GET /v1/ent/roles", ent(h.ListRoles))
	mux.Handle("POST /v1/ent/roles", ent(h.CreateRole))
	mux.Handle("GET /v1/ent/roles/{id}", ent(h.GetRole))
	mux.Handle("PUT /v1/ent/roles/{id}", ent(h.UpdateRole))
	mux.Handle("DELETE /v1/ent/roles/{id}", ent(h.DeleteRole))

	mux.Handle("GET /v1/ent/groups", ent(h.ListGroups))
	mux.Handle("POST /v1/ent/groups", ent(h.CreateGroup))
	mux.Handle("GET /v1/ent/groups/{id}", ent(h.GetGroup))
	mux.Handle("PUT /v1/ent/groups/{id}", ent(h.UpdateGroup))
	mux.Handle("DELETE /v1/ent/groups/{id}", ent(h.DeleteGroup))
	mux.Handle("PUT /v1/ent/groups/{id}/members", ent(h.SetGroupMembers))
	mux.Handle("PUT /v1/ent/groups/{id}/roles", ent(h.SetGroupRoles))

	mux.Handle("GET /v1/ent/tenants", ent(h.ListTenants))
	// 租户功能开关（SSO 灰度载体）：读走 ent:manage；**写仅平台 owner**（handler 内判定，
	// 因为"是否允许该租户用 SSO"是平台侧的信任边界，不该由租户 admin 自行放开）。
	mux.Handle("GET /v1/ent/tenants/{id}/features", ent(h.GetTenantFeatures))
	mux.Handle("PUT /v1/ent/tenants/{id}/features", ent(h.PutTenantFeatures))
}

// ── 用户 ────────────────────────────────────────────────

type entUserItem struct {
	ID        string          `json:"id"`
	Email     string          `json:"email"`
	Name      string          `json:"name"`
	Role      string          `json:"role"`
	CreatedAt time.Time       `json:"created_at"`
	Roles     json.RawMessage `json:"roles"`
	Groups    json.RawMessage `json:"groups"`
}

// ListUsers GET /v1/ent/users?search=&page=&page_size=
// users LEFT JOIN ent_user_roles/ent_roles + 群组信息，email/姓名模糊搜索 + 分页。
func (h *EntIdentityHandler) ListUsers(w http.ResponseWriter, r *http.Request) {
	search := r.URL.Query().Get("search")
	if len(search) > 200 {
		search = search[:200]
	}
	page, pageSize := parsePageQuery(r)
	ctx := r.Context()

	var total int
	err := h.db.QueryRow(ctx,
		`SELECT COUNT(*)::int FROM users u
		 WHERE ($1::text = '' OR u.email ILIKE '%' || $1 || '%' OR u.name ILIKE '%' || $1 || '%')`,
		search).Scan(&total)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	rows, err := h.db.Query(ctx,
		`SELECT u.id, u.email, u.name, u.role, u.created_at,
		        COALESCE((SELECT json_agg(jsonb_build_object('id', r.id, 'name', r.name, 'display_name', COALESCE(r.display_name, '')))
		                  FROM ent_user_roles ur JOIN ent_roles r ON r.id = ur.role_id
		                  WHERE ur.user_id = u.id), '[]'::json) AS roles,
		        COALESCE((SELECT json_agg(jsonb_build_object('id', g.id, 'name', g.name))
		                  FROM ent_group_members gm JOIN ent_groups g ON g.id = gm.group_id
		                  WHERE gm.user_id = u.id), '[]'::json) AS groups
		 FROM users u
		 WHERE ($1::text = '' OR u.email ILIKE '%' || $1 || '%' OR u.name ILIKE '%' || $1 || '%')
		 ORDER BY u.created_at DESC
		 LIMIT $2 OFFSET $3`,
		search, pageSize, (page-1)*pageSize)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	defer rows.Close()

	items := make([]entUserItem, 0)
	for rows.Next() {
		var item entUserItem
		if err := rows.Scan(&item.ID, &item.Email, &item.Name, &item.Role, &item.CreatedAt,
			&item.Roles, &item.Groups); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
			return
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	JSON(w, http.StatusOK, APIResponse{
		Success: true,
		Data:    items,
		Meta:    &Meta{Total: total, Page: page, PerPage: pageSize},
	})
}

type setUserRolesRequest struct {
	RoleIDs []string `json:"role_ids"`
}

// SetUserRoles PUT /v1/ent/users/{id}/roles 全量替换用户角色。
func (h *EntIdentityHandler) SetUserRoles(w http.ResponseWriter, r *http.Request) {
	userID := r.PathValue("id")
	if !isValidUUID(userID) {
		BadRequest(w, "invalid user id")
		return
	}
	var req setUserRolesRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if err := validateUUIDList(req.RoleIDs); err != nil {
		BadRequest(w, "invalid role_ids")
		return
	}
	ctx := r.Context()

	if !h.userExists(ctx, userID) {
		NotFound(w, ErrNotFound)
		return
	}
	if len(req.RoleIDs) > 0 && !h.allExist(ctx, `SELECT COUNT(DISTINCT id)::int FROM ent_roles WHERE id = ANY($1::uuid[])`, req.RoleIDs) {
		BadRequest(w, "one or more role_ids do not exist")
		return
	}

	// 全量替换 = 删除不在新集合的绑定 + 幂等插入新绑定
	if _, err := h.db.Exec(ctx,
		`DELETE FROM ent_user_roles WHERE user_id = $1 AND NOT (role_id = ANY($2::uuid[]))`,
		userID, req.RoleIDs); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if _, err := h.db.Exec(ctx,
		`INSERT INTO ent_user_roles (user_id, role_id)
		 SELECT $1, unnest($2::uuid[]) ON CONFLICT DO NOTHING`,
		userID, req.RoleIDs); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	enterprise.InvalidateUserPerms(ctx, userID)
	OK(w, map[string]string{"status": "updated"})
}

type setUserGroupsRequest struct {
	GroupIDs []string `json:"group_ids"`
}

// SetUserGroups PUT /v1/ent/users/{id}/groups 全量替换用户群组。
func (h *EntIdentityHandler) SetUserGroups(w http.ResponseWriter, r *http.Request) {
	userID := r.PathValue("id")
	if !isValidUUID(userID) {
		BadRequest(w, "invalid user id")
		return
	}
	var req setUserGroupsRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if err := validateUUIDList(req.GroupIDs); err != nil {
		BadRequest(w, "invalid group_ids")
		return
	}
	ctx := r.Context()

	if !h.userExists(ctx, userID) {
		NotFound(w, ErrNotFound)
		return
	}
	if len(req.GroupIDs) > 0 && !h.allExist(ctx, `SELECT COUNT(DISTINCT id)::int FROM ent_groups WHERE id = ANY($1::uuid[])`, req.GroupIDs) {
		BadRequest(w, "one or more group_ids do not exist")
		return
	}

	oldGroups := h.userGroupIDs(ctx, userID)

	if _, err := h.db.Exec(ctx,
		`DELETE FROM ent_group_members WHERE user_id = $1 AND NOT (group_id = ANY($2::uuid[]))`,
		userID, req.GroupIDs); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if _, err := h.db.Exec(ctx,
		`INSERT INTO ent_group_members (group_id, user_id)
		 SELECT unnest($1::uuid[]), $2 ON CONFLICT DO NOTHING`,
		req.GroupIDs, userID); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	// 受影响群组 = 旧 ∪ 新；同时失效用户自身缓存
	for _, gid := range unionStringSet(oldGroups, req.GroupIDs) {
		enterprise.InvalidateGroupMembersPerms(ctx, gid)
	}
	enterprise.InvalidateUserPerms(ctx, userID)
	OK(w, map[string]string{"status": "updated"})
}

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

func (h *EntIdentityHandler) scanRole(ctx context.Context, id string, withUserCount bool) (*entRoleResponse, error) {
	userCountExpr := `0`
	if withUserCount {
		userCountExpr = `(SELECT COUNT(*)::int FROM ent_user_roles ur WHERE ur.role_id = r.id)`
	}
	var role entRoleResponse
	var displayName *string
	err := h.db.QueryRow(ctx,
		`SELECT r.id, r.name, r.display_name, r.is_builtin, COALESCE(r.permissions, '{}'),
		        `+userCountExpr+`, r.created_at, r.updated_at
		 FROM ent_roles r WHERE r.id = $1`, id).
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
	rows, err := h.db.Query(r.Context(),
		`SELECT r.id FROM ent_roles r ORDER BY r.is_builtin DESC, r.name`)
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
		role, err := h.scanRole(r.Context(), id, true)
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
	id := r.PathValue("id")
	if !isValidUUID(id) {
		BadRequest(w, "invalid role id")
		return
	}
	role, err := h.scanRole(r.Context(), id, true)
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
	if claims != nil && claims.TenantID != "" {
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
		return h.scanRole(r.Context(), id, true)
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

	existing, err := h.scanRole(ctx, id, false)
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
		`UPDATE ent_roles SET name = $2, display_name = $3, permissions = $4, updated_at = NOW() WHERE id = $1`,
		id, newName, newDisplay, newPerms); err != nil {
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
	id := r.PathValue("id")
	if !isValidUUID(id) {
		BadRequest(w, "invalid role id")
		return
	}
	ctx := r.Context()

	existing, err := h.scanRole(ctx, id, false)
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
	tag, err := h.db.Exec(ctx, `DELETE FROM ent_roles WHERE id = $1`, id)
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

// ── 群组 CRUD ───────────────────────────────────────────

type entGroupResponse struct {
	ID          string    `json:"id"`
	Name        string    `json:"name"`
	Description string    `json:"description"`
	MemberCount int       `json:"member_count"`
	RoleIDs     []string  `json:"role_ids,omitempty"`
	CreatedAt   time.Time `json:"created_at"`
}

func (h *EntIdentityHandler) scanGroup(ctx context.Context, id string) (*entGroupResponse, error) {
	var group entGroupResponse
	var description *string
	err := h.db.QueryRow(ctx,
		`SELECT g.id, g.name, g.description, g.created_at,
		        (SELECT COUNT(*)::int FROM ent_group_members gm WHERE gm.group_id = g.id)
		 FROM ent_groups g WHERE g.id = $1`, id).
		Scan(&group.ID, &group.Name, &description, &group.CreatedAt, &group.MemberCount)
	if err != nil {
		return nil, err
	}
	if description != nil {
		group.Description = *description
	}
	group.RoleIDs = h.groupRoleIDs(ctx, id)
	if group.RoleIDs == nil {
		group.RoleIDs = []string{}
	}
	return &group, nil
}

// ListGroups GET /v1/ent/groups
func (h *EntIdentityHandler) ListGroups(w http.ResponseWriter, r *http.Request) {
	rows, err := h.db.Query(r.Context(),
		`SELECT g.id, g.name, g.description, g.created_at, COUNT(gm.user_id)::int AS member_count
		 FROM ent_groups g
		 LEFT JOIN ent_group_members gm ON gm.group_id = g.id
		 GROUP BY g.id ORDER BY g.name`)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	defer rows.Close()

	items := make([]entGroupResponse, 0)
	for rows.Next() {
		var item entGroupResponse
		var description *string
		if err := rows.Scan(&item.ID, &item.Name, &description, &item.CreatedAt, &item.MemberCount); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
			return
		}
		if description != nil {
			item.Description = *description
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	OK(w, items)
}

// GetGroup GET /v1/ent/groups/{id}
func (h *EntIdentityHandler) GetGroup(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !isValidUUID(id) {
		BadRequest(w, "invalid group id")
		return
	}
	group, err := h.scanGroup(r.Context(), id)
	if errors.Is(err, pgx.ErrNoRows) {
		NotFound(w, ErrNotFound)
		return
	}
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	OK(w, group)
}

type createGroupRequest struct {
	Name        string `json:"name"`
	Description string `json:"description"`
}

// CreateGroup POST /v1/ent/groups
func (h *EntIdentityHandler) CreateGroup(w http.ResponseWriter, r *http.Request) {
	var req createGroupRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if req.Name == "" || len(req.Name) > 128 {
		BadRequest(w, "name is required (max 128 chars)")
		return
	}

	claims := auth.GetClaims(r.Context())
	tenantID := db.DefaultTenantID
	if claims != nil && claims.TenantID != "" {
		tenantID = claims.TenantID
	}

	var id string
	err := h.db.QueryRow(r.Context(),
		`INSERT INTO ent_groups (tenant_id, name, description) VALUES ($1, $2, $3) RETURNING id`,
		tenantID, req.Name, req.Description).Scan(&id)
	if err != nil {
		if isUniqueViolation(err) {
			logAndRespond(w, err, http.StatusConflict, "group name already exists")
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	group, err := h.scanGroup(r.Context(), id)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	Created(w, group)
}

type updateGroupRequest struct {
	Name        *string `json:"name"`
	Description *string `json:"description"`
}

// UpdateGroup PUT /v1/ent/groups/{id}
func (h *EntIdentityHandler) UpdateGroup(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !isValidUUID(id) {
		BadRequest(w, "invalid group id")
		return
	}
	var req updateGroupRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if req.Name == nil && req.Description == nil {
		BadRequest(w, "no fields to update")
		return
	}
	ctx := r.Context()

	existing, err := h.scanGroup(ctx, id)
	if errors.Is(err, pgx.ErrNoRows) {
		NotFound(w, ErrNotFound)
		return
	}
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	newName, newDesc := existing.Name, existing.Description
	if req.Name != nil {
		if *req.Name == "" || len(*req.Name) > 128 {
			BadRequest(w, "invalid name")
			return
		}
		newName = *req.Name
	}
	if req.Description != nil {
		newDesc = *req.Description
	}

	if _, err := h.db.Exec(ctx,
		`UPDATE ent_groups SET name = $2, description = $3 WHERE id = $1`,
		id, newName, newDesc); err != nil {
		if isUniqueViolation(err) {
			logAndRespond(w, err, http.StatusConflict, "group name already exists")
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	OK(w, map[string]string{"status": "updated"})
}

// DeleteGroup DELETE /v1/ent/groups/{id}
func (h *EntIdentityHandler) DeleteGroup(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !isValidUUID(id) {
		BadRequest(w, "invalid group id")
		return
	}
	ctx := r.Context()

	// 成员/角色关联随群组级联删除；先收集成员以便失效其权限缓存
	members := h.groupMemberIDs(ctx, id)
	tag, err := h.db.Exec(ctx, `DELETE FROM ent_groups WHERE id = $1`, id)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if tag.RowsAffected() == 0 {
		NotFound(w, ErrNotFound)
		return
	}
	for _, uid := range members {
		enterprise.InvalidateUserPerms(ctx, uid)
	}
	OK(w, map[string]string{"status": "deleted"})
}

type setGroupMembersRequest struct {
	UserIDs []string `json:"user_ids"`
}

// SetGroupMembers PUT /v1/ent/groups/{id}/members 全量替换群组成员。
func (h *EntIdentityHandler) SetGroupMembers(w http.ResponseWriter, r *http.Request) {
	groupID := r.PathValue("id")
	if !isValidUUID(groupID) {
		BadRequest(w, "invalid group id")
		return
	}
	var req setGroupMembersRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if err := validateUUIDList(req.UserIDs); err != nil {
		BadRequest(w, "invalid user_ids")
		return
	}
	ctx := r.Context()

	if !h.groupExists(ctx, groupID) {
		NotFound(w, ErrNotFound)
		return
	}
	if len(req.UserIDs) > 0 && !h.allExist(ctx, `SELECT COUNT(DISTINCT id)::int FROM users WHERE id = ANY($1::uuid[])`, req.UserIDs) {
		BadRequest(w, "one or more user_ids do not exist")
		return
	}

	oldMembers := h.groupMemberIDs(ctx, groupID)

	if _, err := h.db.Exec(ctx,
		`DELETE FROM ent_group_members WHERE group_id = $1 AND NOT (user_id = ANY($2::uuid[]))`,
		groupID, req.UserIDs); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if _, err := h.db.Exec(ctx,
		`INSERT INTO ent_group_members (group_id, user_id)
		 SELECT $1, unnest($2::uuid[]) ON CONFLICT DO NOTHING`,
		groupID, req.UserIDs); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	// 被移除的成员单独失效；仍/新在组内的成员经群组批量失效
	newSet := make(map[string]struct{}, len(req.UserIDs))
	for _, uid := range req.UserIDs {
		newSet[uid] = struct{}{}
	}
	for _, uid := range oldMembers {
		if _, ok := newSet[uid]; !ok {
			enterprise.InvalidateUserPerms(ctx, uid)
		}
	}
	enterprise.InvalidateGroupMembersPerms(ctx, groupID)
	OK(w, map[string]string{"status": "updated"})
}

type setGroupRolesRequest struct {
	RoleIDs []string `json:"role_ids"`
}

// SetGroupRoles PUT /v1/ent/groups/{id}/roles 全量替换群组角色绑定。
func (h *EntIdentityHandler) SetGroupRoles(w http.ResponseWriter, r *http.Request) {
	groupID := r.PathValue("id")
	if !isValidUUID(groupID) {
		BadRequest(w, "invalid group id")
		return
	}
	var req setGroupRolesRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if err := validateUUIDList(req.RoleIDs); err != nil {
		BadRequest(w, "invalid role_ids")
		return
	}
	ctx := r.Context()

	if !h.groupExists(ctx, groupID) {
		NotFound(w, ErrNotFound)
		return
	}
	if len(req.RoleIDs) > 0 && !h.allExist(ctx, `SELECT COUNT(DISTINCT id)::int FROM ent_roles WHERE id = ANY($1::uuid[])`, req.RoleIDs) {
		BadRequest(w, "one or more role_ids do not exist")
		return
	}

	if _, err := h.db.Exec(ctx,
		`DELETE FROM ent_group_roles WHERE group_id = $1 AND NOT (role_id = ANY($2::uuid[]))`,
		groupID, req.RoleIDs); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if _, err := h.db.Exec(ctx,
		`INSERT INTO ent_group_roles (group_id, role_id)
		 SELECT $1, unnest($2::uuid[]) ON CONFLICT DO NOTHING`,
		groupID, req.RoleIDs); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	enterprise.InvalidateGroupMembersPerms(ctx, groupID)
	OK(w, map[string]string{"status": "updated"})
}

// ── 租户 ────────────────────────────────────────────────

type entTenantItem struct {
	ID        string          `json:"id"`
	Name      string          `json:"name"`
	Status    string          `json:"status"` // 真实 tenants 表无状态列，恒为 "active"
	CreatedAt time.Time       `json:"created_at"`
	UserCount int             `json:"user_count"`
	Quotas    json.RawMessage `json:"quotas"`
}

// ListTenants GET /v1/ent/tenants?page=&page_size=
// 读真实 tenants 表 + LEFT JOIN ent_quota_pools 汇总（不引用影子表 admin_tenants）。
// GetTenantFeatures GET /v1/ent/tenants/{id}/features —— 读租户功能开关。
func (h *EntIdentityHandler) GetTenantFeatures(w http.ResponseWriter, r *http.Request) {
	tenantID := r.PathValue("id")
	if !isValidUUID(tenantID) {
		BadRequest(w, "invalid tenant id")
		return
	}
	OK(w, loadTenantFeatures(r.Context(), h.db, tenantID))
}

// PutTenantFeatures PUT /v1/ent/tenants/{id}/features —— **仅平台 owner**。
//
// SSO 的灰度就是在这里放开：租户 admin 即便持有 `sso:manage` 也无法自行开启，
// 因为"该租户能否用 SSO"是平台侧决定的事（见 tenant_features.go 的说明）。
// 请求体字段可只给其一（未给的保持原值）。
func (h *EntIdentityHandler) PutTenantFeatures(w http.ResponseWriter, r *http.Request) {
	if !isPlatformOwner(auth.GetClaims(r.Context())) {
		Forbidden(w, "only the platform owner can change tenant features")
		return
	}
	tenantID := r.PathValue("id")
	if !isValidUUID(tenantID) {
		BadRequest(w, "invalid tenant id")
		return
	}
	var body struct {
		SSOEnabled     *bool `json:"sso_enabled"`
		SSOSelfService *bool `json:"sso_self_service"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.SSOEnabled == nil && body.SSOSelfService == nil {
		BadRequest(w, "no fields to update")
		return
	}
	cur := loadTenantFeatures(r.Context(), h.db, tenantID)
	if body.SSOEnabled != nil {
		cur.SSOEnabled = *body.SSOEnabled
	}
	if body.SSOSelfService != nil {
		cur.SSOSelfService = *body.SSOSelfService
	}
	if err := saveTenantFeatures(r.Context(), h.db, tenantID, cur); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "save tenant features failed")
		return
	}
	OK(w, cur)
}

func (h *EntIdentityHandler) ListTenants(w http.ResponseWriter, r *http.Request) {
	page, pageSize := parsePageQuery(r)
	ctx := r.Context()

	var total int
	if err := h.db.QueryRow(ctx, `SELECT COUNT(*)::int FROM tenants`).Scan(&total); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	rows, err := h.db.Query(ctx,
		`SELECT t.id, t.name, t.created_at,
		        (SELECT COUNT(*)::int FROM users u WHERE u.tenant_id = t.id) AS user_count,
		        COALESCE((SELECT json_agg(jsonb_build_object(
		                    'resource_type', q.resource_type,
		                    'total_amount', q.total_amount,
		                    'period', q.period))
		                  FROM ent_quota_pools q WHERE q.tenant_id = t.id), '[]'::json) AS quotas
		 FROM tenants t
		 ORDER BY t.created_at
		 LIMIT $1 OFFSET $2`,
		pageSize, (page-1)*pageSize)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	defer rows.Close()

	items := make([]entTenantItem, 0)
	for rows.Next() {
		var item entTenantItem
		if err := rows.Scan(&item.ID, &item.Name, &item.CreatedAt, &item.UserCount, &item.Quotas); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
			return
		}
		item.Status = "active"
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	JSON(w, http.StatusOK, APIResponse{
		Success: true,
		Data:    items,
		Meta:    &Meta{Total: total, Page: page, PerPage: pageSize},
	})
}

// ── 内部辅助 ────────────────────────────────────────────

func (h *EntIdentityHandler) userExists(ctx context.Context, id string) bool {
	var one int
	return h.db.QueryRow(ctx, `SELECT 1 FROM users WHERE id = $1`, id).Scan(&one) == nil
}

func (h *EntIdentityHandler) groupExists(ctx context.Context, id string) bool {
	var one int
	return h.db.QueryRow(ctx, `SELECT 1 FROM ent_groups WHERE id = $1`, id).Scan(&one) == nil
}

// allExist 校验 ids 全部存在于目标表（countSQL 需返回 DISTINCT 计数，$1 为 uuid[]）。
func (h *EntIdentityHandler) allExist(ctx context.Context, countSQL string, ids []string) bool {
	var count int
	if err := h.db.QueryRow(ctx, countSQL, ids).Scan(&count); err != nil {
		return false
	}
	return count == len(ids)
}

func (h *EntIdentityHandler) userGroupIDs(ctx context.Context, userID string) []string {
	return h.queryIDList(ctx, `SELECT group_id FROM ent_group_members WHERE user_id = $1`, userID)
}

func (h *EntIdentityHandler) groupMemberIDs(ctx context.Context, groupID string) []string {
	return h.queryIDList(ctx, `SELECT user_id FROM ent_group_members WHERE group_id = $1`, groupID)
}

func (h *EntIdentityHandler) groupRoleIDs(ctx context.Context, groupID string) []string {
	return h.queryIDList(ctx, `SELECT role_id FROM ent_group_roles WHERE group_id = $1`, groupID)
}

func (h *EntIdentityHandler) queryIDList(ctx context.Context, sql string, arg any) []string {
	rows, err := h.db.Query(ctx, sql, arg)
	if err != nil {
		return nil
	}
	defer rows.Close()
	ids := make([]string, 0)
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err == nil {
			ids = append(ids, id)
		}
	}
	return ids
}

// usersAffectedByRole 返回角色影响到的全部用户（直接绑定 + 经群组间接绑定）。
func (h *EntIdentityHandler) usersAffectedByRole(ctx context.Context, roleID string) []string {
	return h.queryIDList(ctx,
		`SELECT user_id FROM ent_user_roles WHERE role_id = $1
		 UNION
		 SELECT gm.user_id FROM ent_group_roles gr
		 JOIN ent_group_members gm ON gm.group_id = gr.group_id
		 WHERE gr.role_id = $1`, roleID)
}

func isValidUUID(id string) bool {
	_, err := uuid.Parse(id)
	return err == nil
}

func validateUUIDList(ids []string) error {
	for _, id := range ids {
		if !isValidUUID(id) {
			return errors.New("invalid uuid: " + id)
		}
	}
	return nil
}

// parsePageQuery 解析分页参数：page ≥ 1（默认 1），page_size 1..100（默认 20）。
func parsePageQuery(r *http.Request) (int, int) {
	page := 1
	if v, err := strconv.Atoi(r.URL.Query().Get("page")); err == nil && v > 0 {
		page = v
	}
	pageSize := 20
	if v, err := strconv.Atoi(r.URL.Query().Get("page_size")); err == nil && v > 0 {
		if v > 100 {
			v = 100
		}
		pageSize = v
	}
	return page, pageSize
}

// isUniqueViolation 复用 ent_costcenter_handler.go 中的定义（SQLSTATE 23505 判定）。

// equalStringSet 以集合语义比较两个字符串切片（权限数组顺序不敏感）。
func equalStringSet(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	set := make(map[string]struct{}, len(a))
	for _, s := range a {
		set[s] = struct{}{}
	}
	for _, s := range b {
		if _, ok := set[s]; !ok {
			return false
		}
	}
	return true
}

// unionStringSet 返回两个字符串切片的去重并集（保持首次出现顺序）。
func unionStringSet(a, b []string) []string {
	seen := make(map[string]struct{}, len(a)+len(b))
	out := make([]string, 0, len(a)+len(b))
	for _, s := range append(append([]string{}, a...), b...) {
		if _, ok := seen[s]; !ok {
			seen[s] = struct{}{}
			out = append(out, s)
		}
	}
	return out
}
