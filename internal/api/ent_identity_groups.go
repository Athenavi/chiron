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

// ent_identity_groups.go —— 企业身份的**群组**子域。
//
// 由 ent_identity_handler.go 按子域拆出（L3-4 批次 A，同包纯移动，逐字未改）。
// 群组把「角色」批量授给「成员」，是角色与用户之间的中间层。
// ── 群组 CRUD ───────────────────────────────────────────

type entGroupResponse struct {
	ID          string    `json:"id"`
	Name        string    `json:"name"`
	Description string    `json:"description"`
	MemberCount int       `json:"member_count"`
	RoleIDs     []string  `json:"role_ids,omitempty"`
	CreatedAt   time.Time `json:"created_at"`
}

func (h *EntIdentityHandler) scanGroup(ctx context.Context, id string, scope string) (*entGroupResponse, error) {
	var group entGroupResponse
	var description *string
	err := h.db.QueryRow(ctx,
		`SELECT g.id, g.name, g.description, g.created_at,
		        (SELECT COUNT(*)::int FROM ent_group_members gm WHERE gm.group_id = g.id)
		 FROM ent_groups g WHERE g.id = $1 AND ($2 = '' OR g.tenant_id::text = $2)`, id, scope).
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
	scope, isOwner := entTenantScope(auth.GetClaims(r.Context()))
	if !isOwner && scope == "" {
		Forbidden(w, "tenant context missing")
		return
	}
	rows, err := h.db.Query(r.Context(),
		`SELECT g.id, g.name, g.description, g.created_at, COUNT(gm.user_id)::int AS member_count
		 FROM ent_groups g
		 LEFT JOIN ent_group_members gm ON gm.group_id = g.id
		 WHERE ($1 = '' OR g.tenant_id::text = $1)
		 GROUP BY g.id ORDER BY g.name`,
		scope)
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
	scope, isOwner := entTenantScope(auth.GetClaims(r.Context()))
	if !isOwner && scope == "" {
		Forbidden(w, "tenant context missing")
		return
	}
	id := r.PathValue("id")
	if !isValidUUID(id) {
		BadRequest(w, "invalid group id")
		return
	}
	group, err := h.scanGroup(r.Context(), id, scope)
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
	if claims != nil && claims.TenantID != 0 {
		tenantID = claims.TenantID
	}
	scope, isOwner := entTenantScope(claims)
	if !isOwner && scope == "" {
		Forbidden(w, "tenant context missing")
		return
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
	group, err := h.scanGroup(r.Context(), id, scope)
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
	scope, isOwner := entTenantScope(auth.GetClaims(r.Context()))
	if !isOwner && scope == "" {
		Forbidden(w, "tenant context missing")
		return
	}
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

	existing, err := h.scanGroup(ctx, id, scope)
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
		`UPDATE ent_groups SET name = $2, description = $3 WHERE id = $1 AND ($4 = '' OR tenant_id::text = $4)`,
		id, newName, newDesc, scope); err != nil {
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
	scope, isOwner := entTenantScope(auth.GetClaims(r.Context()))
	if !isOwner && scope == "" {
		Forbidden(w, "tenant context missing")
		return
	}
	id := r.PathValue("id")
	if !isValidUUID(id) {
		BadRequest(w, "invalid group id")
		return
	}
	ctx := r.Context()

	// 成员/角色关联随群组级联删除；先收集成员以便失效其权限缓存
	members := h.groupMemberIDs(ctx, id)
	tag, err := h.db.Exec(ctx, `DELETE FROM ent_groups WHERE id = $1 AND ($2 = '' OR tenant_id::text = $2)`, id, scope)
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
	scope, isOwner := entTenantScope(auth.GetClaims(ctx))
	if !isOwner && scope == "" {
		Forbidden(w, "tenant context missing")
		return
	}

	if !h.groupExists(ctx, groupID, scope) {
		NotFound(w, ErrNotFound)
		return
	}
	if len(req.UserIDs) > 0 && !h.allExist(ctx, `SELECT COUNT(DISTINCT id)::int FROM users WHERE id = ANY($1::uuid[]) AND ($2 = '' OR tenant_id::text = $2)`, req.UserIDs, scope) {
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
	scope, isOwner := entTenantScope(auth.GetClaims(ctx))
	if !isOwner && scope == "" {
		Forbidden(w, "tenant context missing")
		return
	}

	if !h.groupExists(ctx, groupID, scope) {
		NotFound(w, ErrNotFound)
		return
	}
	if len(req.RoleIDs) > 0 && !h.allExist(ctx, `SELECT COUNT(DISTINCT id)::int FROM ent_roles WHERE id = ANY($1::uuid[]) AND ($2 = '' OR tenant_id::text = $2)`, req.RoleIDs, scope) {
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
