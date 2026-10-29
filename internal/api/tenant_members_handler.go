package api

import (
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/id"
)

// ── 团队与成员（C1 / roadmap §12.4）──
//
// ## 口径（产品 2026-10-29 答复）
//
// * **一期一人一租户**：`tenant_members` 有 `UNIQUE (user_id)`，本组接口不提供
//   跨租户操作，也**不接受请求体里的 tenant_id** —— 租户一律取当前登录者的
//   租户（`GetTenantID`）。这是本文件最重要的一条安全约束：一旦允许调用方
//   指定 tenant_id，任何持有 admin 权限的人都能改别的租户的成员。
// * **角色复用 `ent_roles`**：`tenant_members.role_id` 外键指向它，不存字符串。
// * **移除成员 = 切断访问，不转移资产**：公开资产归团队、私有资产随人走。
//
// ## 移除为什么必须同时吊销 key 与 session
//
// 删掉 `tenant_members` 行只是让「成员关系」消失，但该用户的 JWT 里仍带着旧
// `tenant_id`、API key 也仍然有效 —— 也就是说**不吊销就等于没移除**。两者都
// 走已有的批量撤销机制（`revokeUserSessions` 的会话纪元 / `UPDATE api_keys`），
// 不新造一套。
//
// ## 两套角色真相的处置
//
// `ent_user_roles`（旧 RBAC）与 `tenant_members.role_id`（新）目前并存。在读取
// 路径尚未切换之前（C1-4），若移除成员时不清 `ent_user_roles`，该用户仍会通过
// `LoadEffectivePerms` 拿回权限 —— 移除就成了假的。因此移除时一并清理。

var (
	// errLastOwner 是「租户里必须至少留一个 owner」的业务拒绝，不是系统故障 ——
	// 调用方应回 400 而不是 500。
	errLastOwner = errors.New("租户至少需要一个 owner，请先指定另一位 owner")
)

type tenantMemberRow struct {
	UserID      string    `json:"user_id"`
	Email       string    `json:"email"`
	Name        string    `json:"name"`
	Role        string    `json:"role"`      // users.role，系统级
	MemberRole  string    `json:"member_role"` // ent_roles.name，租户内
	RoleLabel   string    `json:"role_label"`
	JoinedAt    time.Time `json:"joined_at"`
}

type tenantRoleRow struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	DisplayName string `json:"display_name"`
}

const tenantMembersListQuery = `SELECT u.id, COALESCE(u.email, ''), COALESCE(u.name, ''),
       COALESCE(u.role, ''), COALESCE(r.name, ''), COALESCE(r.display_name, ''),
       tm.joined_at
  FROM tenant_members tm
  JOIN users u ON u.id = tm.user_id
  LEFT JOIN ent_roles r ON r.id = tm.role_id
 WHERE tm.tenant_id = $1
 ORDER BY tm.joined_at ASC, u.id ASC`

const tenantRolesListQuery = `SELECT id, name, display_name
  FROM ent_roles
 WHERE tenant_id = $1 AND is_builtin = TRUE
 ORDER BY name`

const tenantMemberRoleQuery = `SELECT COALESCE(r.name, '')
  FROM tenant_members tm
  LEFT JOIN ent_roles r ON r.id = tm.role_id
 WHERE tm.tenant_id = $1 AND tm.user_id = $2`

const tenantOwnerCountQuery = `SELECT COUNT(*)
  FROM tenant_members tm
  JOIN ent_roles r ON r.id = tm.role_id
 WHERE tm.tenant_id = $1 AND r.name = 'owner'`

// tenantMemberSetRoleQuery 同时约束「成员属于该租户」与「角色属于该租户」，
// 后者不能用请求体里的 role_id 直接信任 —— 否则可以把别的租户的角色挂过来。
const tenantMemberSetRoleQuery = `UPDATE tenant_members tm
   SET role_id = r.id
  FROM ent_roles r
 WHERE tm.tenant_id = $1 AND tm.user_id = $2
   AND r.tenant_id = $1 AND r.name = $3`

const tenantMemberDeleteQuery = `DELETE FROM tenant_members WHERE tenant_id = $1 AND user_id = $2`

// userAPIKeysRevokeAllQuery 吊销该用户所有未吊销的 key（长期凭据，移除成员时必须一并作废）。
const userAPIKeysRevokeAllQuery = `UPDATE api_keys SET revoked = true
 WHERE user_id = $1 AND COALESCE(revoked, false) = false`

const userLegacyRolesDeleteQuery = `DELETE FROM ent_user_roles WHERE user_id = $1`

const tenantInvitesListQuery = `SELECT code, max_uses, uses, expires_at, created_at
  FROM tenant_invites WHERE tenant_id = $1 ORDER BY created_at DESC`

const tenantInviteInsertQuery = `INSERT INTO tenant_invites (code, tenant_id, created_by, max_uses, expires_at)
 VALUES ($1, $2, NULLIF($3, ''), $4, $5)`

// tenantInviteDeleteQuery 带 tenant_id 条件：撤销只能撤销本租户的码，
// 不能凭一个 code 删掉别的租户的邀请。
const tenantInviteDeleteQuery = `DELETE FROM tenant_invites WHERE code = $1 AND tenant_id = $2`

// ListTenantMembers 列出当前租户的成员，并附上可分配的内置角色。
func (h *AdminHandler) ListTenantMembers(w http.ResponseWriter, r *http.Request) {
	tenantID := GetTenantID(r)
	if tenantID == 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	rows, err := db.GlobalDBManager.Query(r.Context(), tenantMembersListQuery, tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list tenant members failed")
		return
	}
	defer rows.Close()

	members := []tenantMemberRow{}
	for rows.Next() {
		var m tenantMemberRow
		if err := rows.Scan(&m.UserID, &m.Email, &m.Name, &m.Role,
			&m.MemberRole, &m.RoleLabel, &m.JoinedAt); err != nil {
			// 不 continue 静默跳过：成员列表少一行会让人以为某人已被移除。
			logAndRespond(w, err, http.StatusInternalServerError, "list tenant members failed")
			return
		}
		members = append(members, m)
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list tenant members failed")
		return
	}

	roles, err := listTenantBuiltinRoles(r, tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list tenant roles failed")
		return
	}
	OK(w, map[string]interface{}{"members": members, "roles": roles, "total": len(members)})
}

func listTenantBuiltinRoles(r *http.Request, tenantID int16) ([]tenantRoleRow, error) {
	rows, err := db.GlobalDBManager.Query(r.Context(), tenantRolesListQuery, tenantID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []tenantRoleRow{}
	for rows.Next() {
		var role tenantRoleRow
		if err := rows.Scan(&role.ID, &role.Name, &role.DisplayName); err != nil {
			return nil, err
		}
		out = append(out, role)
	}
	return out, rows.Err()
}

// SetTenantMemberRole 调整成员在租户内的角色。
//
// 关键约束：不能把**最后一个 owner** 降级 —— 否则这个租户再没有人能管理成员，
// 只能靠手工改库救回来。
func (h *AdminHandler) SetTenantMemberRole(w http.ResponseWriter, r *http.Request) {
	tenantID := GetTenantID(r)
	if tenantID == 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	userID := r.PathValue("user_id")
	if userID == "" {
		BadRequest(w, "user_id is required")
		return
	}
	var body struct {
		Role string `json:"role"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	role := strings.TrimSpace(body.Role)
	if role == "" {
		BadRequest(w, "role is required")
		return
	}

	current, err := tenantMemberRole(r, tenantID, userID)
	if err != nil {
		respondMemberLookupError(w, err)
		return
	}
	if current == "owner" && role != "owner" {
		if err := guardLastOwner(r, tenantID); err != nil {
			respondOwnerGuardError(w, err)
			return
		}
	}

	res, err := db.GlobalDBManager.Exec(r.Context(), tenantMemberSetRoleQuery, tenantID, userID, role)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "set member role failed")
		return
	}
	if res.RowsAffected() == 0 {
		// 要么成员不在本租户，要么 role 不是本租户的内置角色。
		NotFound(w, "member or role not found")
		return
	}
	db.AuditLog(r.Context(), claimsUserID(r), tenantID, "tenant_member_role_set",
		"/v1/admin/tenant/members/"+userID, "role="+role, r.RemoteAddr, nil)
	OK(w, map[string]string{"status": "updated", "user_id": userID, "role": role})
}

// RemoveTenantMember 移除成员：切断访问，不转移资产。
//
// 三件事缺一不可，否则"移除"是假的：
//  1. 删除 tenant_members 行；
//  2. 吊销其全部 API key（长期凭据，不过期）；
//  3. 吊销其全部登录会话（JWT 里还带着旧 tenant_id）+ 清掉旧 RBAC 角色行。
func (h *AdminHandler) RemoveTenantMember(w http.ResponseWriter, r *http.Request) {
	tenantID := GetTenantID(r)
	if tenantID == 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	userID := r.PathValue("user_id")
	if userID == "" {
		BadRequest(w, "user_id is required")
		return
	}
	// 不能移除自己：否则一次误点就把自己的管理权限弄没了，且没有第二个入口恢复。
	if claims := auth.GetClaims(r.Context()); claims != nil && claims.UserID == userID {
		BadRequest(w, "cannot remove yourself")
		return
	}

	current, err := tenantMemberRole(r, tenantID, userID)
	if err != nil {
		respondMemberLookupError(w, err)
		return
	}
	if current == "owner" {
		if err := guardLastOwner(r, tenantID); err != nil {
			respondOwnerGuardError(w, err)
			return
		}
	}

	res, err := db.GlobalDBManager.Exec(r.Context(), tenantMemberDeleteQuery, tenantID, userID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "remove member failed")
		return
	}
	if res.RowsAffected() == 0 {
		NotFound(w, "member not found")
		return
	}

	// 后续两步失败只告警：成员关系已删，不该因为吊销失败而回滚成"还能访问"。
	// 但必须记日志 —— 否则会出现"以为移除了、其实 token 还能用"。
	if _, err := db.GlobalDBManager.Exec(r.Context(), userAPIKeysRevokeAllQuery, userID); err != nil {
		slog.Warn("revoke member api keys failed", "user_id", userID, "error", err)
	}
	if _, err := db.GlobalDBManager.Exec(r.Context(), userLegacyRolesDeleteQuery, userID); err != nil {
		slog.Warn("clear member legacy roles failed", "user_id", userID, "error", err)
	}
	if err := revokeUserSessions(r.Context(), userID); err != nil {
		slog.Warn("revoke member sessions failed", "user_id", userID, "error", err)
	}

	db.AuditLog(r.Context(), claimsUserID(r), tenantID, "tenant_member_removed",
		"/v1/admin/tenant/members/"+userID, "", r.RemoteAddr, nil)
	OK(w, map[string]string{"status": "removed", "user_id": userID})
}

// ListTenantInvites 列出本租户的邀请码。
func (h *AdminHandler) ListTenantInvites(w http.ResponseWriter, r *http.Request) {
	tenantID := GetTenantID(r)
	if tenantID == 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	rows, err := db.GlobalDBManager.Query(r.Context(), tenantInvitesListQuery, tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list invites failed")
		return
	}
	defer rows.Close()
	out := []inviteRow{}
	for rows.Next() {
		var it inviteRow
		it.TenantID = tenantStr(tenantID)
		if err := rows.Scan(&it.Code, &it.MaxUses, &it.Uses, &it.ExpiresAt, &it.CreatedAt); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "list invites failed")
			return
		}
		out = append(out, it)
	}
	OK(w, map[string]interface{}{"invites": out, "total": len(out)})
}

// CreateTenantInvite 为本租户生成邀请码。
//
// 不接收 tenant_id：邀请码天然只能邀请**自己所在的租户**，这比"校验调用者有没有
// 别的租户的权限"更不容易出错。过期与次数上限沿用 `tenant_invites` 的既有语义
// （`consumeInviteQuery` 已经在注册时校验，无需另写判定）。
func (h *AdminHandler) CreateTenantInvite(w http.ResponseWriter, r *http.Request) {
	tenantID := GetTenantID(r)
	if tenantID == 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	var body struct {
		MaxUses       int `json:"max_uses"`
		ExpiresInDays int `json:"expires_in_days"`
	}
	// 允许空请求体：默认 max_uses=1、7 天过期。
	if r.ContentLength > 0 {
		if err := DecodeJSON(w, r, &body); err != nil {
			BadRequest(w, ErrInvalidReq)
			return
		}
	}
	maxUses, expiresInDays := normalizeInviteParams(body.MaxUses, body.ExpiresInDays)

	raw, err := id.UUID()
	if err != nil {
		InternalError(w, "generate code failed")
		return
	}
	code := strings.ReplaceAll(raw, "-", "")[:16]
	expiresAt := time.Now().AddDate(0, 0, expiresInDays)

	if _, err := db.GlobalDBManager.Exec(r.Context(), tenantInviteInsertQuery,
		code, tenantID, claimsUserID(r), maxUses, expiresAt); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create invite failed")
		return
	}
	db.AuditLog(r.Context(), claimsUserID(r), tenantID, "tenant_invite_created",
		"/v1/admin/tenant/members/invites", "code="+code, r.RemoteAddr, nil)
	OK(w, map[string]interface{}{
		"code":       code,
		"tenant_id":  tenantStr(tenantID),
		"max_uses":   maxUses,
		"expires_at": expiresAt,
	})
}

// DeleteTenantInvite 撤销本租户的邀请码。撤销即删除该行（与既有 admin 端点一致）。
func (h *AdminHandler) DeleteTenantInvite(w http.ResponseWriter, r *http.Request) {
	tenantID := GetTenantID(r)
	if tenantID == 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	code := r.PathValue("code")
	if code == "" {
		BadRequest(w, "code is required")
		return
	}
	res, err := db.GlobalDBManager.Exec(r.Context(), tenantInviteDeleteQuery, code, tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "delete invite failed")
		return
	}
	if res.RowsAffected() == 0 {
		NotFound(w, "invite not found")
		return
	}
	db.AuditLog(r.Context(), claimsUserID(r), tenantID, "tenant_invite_revoked",
		"/v1/admin/tenant/members/invites/"+code, "", r.RemoteAddr, nil)
	OK(w, map[string]string{"status": "deleted"})
}

// ── 内部小工具 ──

// tenantMemberRole 取成员在租户内的角色名。
//
// 返回 `pgx.ErrNoRows` 表示**不是该租户的成员**（调用方回 404）；其它错误是
// 系统故障（回 500）。这两种情况此前都被归成 404，会让"数据库挂了"表现为
// "成员不存在" —— 运维排查时会一直往数据方向找。
func tenantMemberRole(r *http.Request, tenantID int16, userID string) (string, error) {
	var name string
	err := db.GlobalDBManager.QueryRow(r.Context(), tenantMemberRoleQuery,
		tenantID, userID).Scan(&name)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", pgx.ErrNoRows
	}
	return name, err
}

// respondMemberLookupError 把成员查询失败映射成恰当的状态码。
func respondMemberLookupError(w http.ResponseWriter, err error) {
	if errors.Is(err, pgx.ErrNoRows) {
		NotFound(w, "member not found")
		return
	}
	logAndRespond(w, err, http.StatusInternalServerError, "lookup member failed")
}

// guardLastOwner 保证租户里至少还剩一个 owner。
//
// 两种返回要分开：`errLastOwner` 是业务拒绝（400），其它是查询失败（500）。
// 查询失败时**拒绝**这次操作而不是放行 —— 否则恰好在"数不清 owner"的瞬间
// 把最后一个 owner 降级/移除，租户就再没人能管理成员了。
func guardLastOwner(r *http.Request, tenantID int16) error {
	var owners int
	if err := db.GlobalDBManager.QueryRow(r.Context(), tenantOwnerCountQuery,
		tenantID).Scan(&owners); err != nil {
		slog.Error("count tenant owners failed", "tenant", tenantID, "error", err)
		return err
	}
	if owners <= 1 {
		return errLastOwner
	}
	return nil
}

// respondOwnerGuardError 把守卫结果映射成状态码：业务拒绝 400，查询失败 500。
func respondOwnerGuardError(w http.ResponseWriter, err error) {
	if errors.Is(err, errLastOwner) {
		BadRequest(w, err.Error())
		return
	}
	logAndRespond(w, err, http.StatusInternalServerError, "check tenant owners failed")
}

func claimsUserID(r *http.Request) string {
	if claims := auth.GetClaims(r.Context()); claims != nil {
		return claims.UserID
	}
	return ""
}

// normalizeInviteParams 给邀请码参数补默认值：不限次（0）会被替换成 1，
// 是因为 `tenant_invites.max_uses = 0` 在 `consumeInviteQuery` 里被解释成
// **不限次数**。邀请场景下"没填次数"显然不是"永久有效"，所以这里必须显式
// 落成 1，而不是把 0 直接写进库。
func normalizeInviteParams(maxUses, expiresInDays int) (int, int) {
	if maxUses <= 0 {
		maxUses = 1
	}
	if expiresInDays <= 0 {
		expiresInDays = 7
	}
	return maxUses, expiresInDays
}
