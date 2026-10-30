package api

// 本文件由 sso_handler.go 按职责外移而来（身份绑定与用户自助：
// 三方身份 ↔ 本地账号的绑定/建号、绑定列表、解绑、首设/改密码）。
// 代码逐字未改：只搬位置，不改逻辑。

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"log/slog"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"
	"golang.org/x/crypto/bcrypt"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// handleBindCallback bind 模式回调：该三方身份 → state.uid 本地账号。
// 冲突（已绑定他人）→ 409；已绑定本人 → 幂等成功。
func (h *SSOHandler) handleBindCallback(w http.ResponseWriter, r *http.Request, provider *ssoProvider, idToken *auth.IDTokenResult, payload *auth.StatePayload) {
	ctx := r.Context()

	existing, err := h.findIdentityUser(ctx, provider.ID, idToken.Subject)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if existing != nil {
		if existing.ID != payload.UID {
			db.AuditLog(ctx, payload.UID, parseTenantID(provider.TenantID), "sso_bind_conflict", "/v1/auth/sso/callback",
				"provider="+provider.Name, r.RemoteAddr, nil)
			JSON(w, http.StatusConflict, APIResponse{
				Success: false,
				Error:   "该三方账号已绑定其他用户，请改用其登录",
			})
			return
		}
		// 已绑定本人 → 幂等
		http.Redirect(w, r, h.bindURL, http.StatusFound)
		return
	}

	if _, err := h.db.Exec(ctx,
		`INSERT INTO ent_user_identities (user_id, provider_id, subject, email)
		 VALUES ($1, $2, $3, $4)`,
		payload.UID, provider.ID, idToken.Subject, idToken.Email); err != nil {
		if isUniqueViolation(err) {
			// 并发绑定冲突
			JSON(w, http.StatusConflict, APIResponse{
				Success: false,
				Error:   "该三方账号已绑定其他用户，请改用其登录",
			})
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	db.AuditLog(ctx, payload.UID, parseTenantID(provider.TenantID), "sso_bind", "/v1/auth/sso/callback",
		"provider="+provider.Name, r.RemoteAddr, nil)
	http.Redirect(w, r, h.bindURL, http.StatusFound)
}
// ListIdentities GET /v1/auth/sso/identities（authMW）
func (h *SSOHandler) ListIdentities(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	rows, err := h.db.Query(r.Context(),
		`SELECT ui.id, p.name, COALESCE(p.display_name, p.name), p.provider_type,
		        ui.subject, ui.email, ui.created_at
		 FROM ent_user_identities ui
		 JOIN ent_oidc_providers p ON p.id = ui.provider_id
		 WHERE ui.user_id = $1
		 ORDER BY ui.created_at`, claims.UserID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	defer rows.Close()

	items := make([]map[string]any, 0)
	for rows.Next() {
		var id, name, display, providerType, subject string
		var email *string
		var createdAt time.Time
		if err := rows.Scan(&id, &name, &display, &providerType, &subject, &email, &createdAt); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
			return
		}
		emailStr := ""
		if email != nil {
			emailStr = *email
		}
		items = append(items, map[string]any{
			"id":            id,
			"provider_name": display,
			"provider_type": providerType,
			"subject":       subject,
			"email":         emailStr,
			"created_at":    createdAt,
		})
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	OK(w, items)
}
// DeleteIdentity DELETE /v1/auth/sso/identities/{id}（authMW）
// 守卫：用户无可口令密码（password_set=false）且这是最后一个三方身份 → 403。
func (h *SSOHandler) DeleteIdentity(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	id := r.PathValue("id")
	if !isValidUUID(id) {
		BadRequest(w, "invalid identity id")
		return
	}
	ctx := r.Context()

	// 守卫：确保解绑后至少保留一种登录方式
	var passwordSet bool
	var count int
	err := h.db.QueryRow(ctx,
		`SELECT u.password_set,
		        (SELECT COUNT(*) FROM ent_user_identities ui WHERE ui.user_id = u.id)
		 FROM users u WHERE u.id = $1`, claims.UserID).Scan(&passwordSet, &count)
	if errors.Is(err, pgx.ErrNoRows) {
		NotFound(w, ErrNotFound)
		return
	}
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	// 目标身份属于本人？
	var subject string
	err = h.db.QueryRow(ctx,
		`SELECT subject FROM ent_user_identities WHERE id = $1 AND user_id = $2`,
		id, claims.UserID).Scan(&subject)
	if errors.Is(err, pgx.ErrNoRows) {
		NotFound(w, ErrNotFound)
		return
	}
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	if !passwordSet && count <= 1 {
		Forbidden(w, "无法解绑：请先设置密码，保证至少保留一种登录方式")
		return
	}

	tag, err := h.db.Exec(ctx,
		`DELETE FROM ent_user_identities WHERE id = $1 AND user_id = $2`, id, claims.UserID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	if tag.RowsAffected() == 0 {
		NotFound(w, ErrNotFound)
		return
	}
	db.AuditLog(ctx, claims.UserID, claims.TenantID, "sso_unbind", "/v1/auth/sso/identities/"+id,
		"subject="+subject, r.RemoteAddr, nil)
	OK(w, map[string]string{"status": "deleted"})
}
type setPasswordRequest struct {
	CurrentPassword string `json:"current_password"`
	NewPassword     string `json:"new_password"`
}
// SetPassword POST /v1/auth/password（authMW）
// SSO 建号用户（password_set=false）首设密码免旧密码；已设置者必须校验旧密码。
func (h *SSOHandler) SetPassword(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	var req setPasswordRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if ok, msg := auth.ValidatePasswordComplexity(req.NewPassword); !ok {
		BadRequest(w, msg)
		return
	}
	ctx := r.Context()

	var passwordHash string
	var passwordSet bool
	err := h.db.QueryRow(ctx,
		`SELECT password_hash, password_set FROM users WHERE id = $1`, claims.UserID).
		Scan(&passwordHash, &passwordSet)
	if errors.Is(err, pgx.ErrNoRows) {
		NotFound(w, ErrNotFound)
		return
	}
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}

	if passwordSet {
		if req.CurrentPassword == "" {
			BadRequest(w, "current_password is required")
			return
		}
		if err := bcrypt.CompareHashAndPassword([]byte(passwordHash), []byte(req.CurrentPassword)); err != nil {
			Unauthorized(w, "invalid current password")
			return
		}
	}

	hash, err := bcrypt.GenerateFromPassword([]byte(req.NewPassword), auth.BcryptCost)
	if err != nil {
		InternalError(w, "set password failed")
		return
	}
	if _, err := h.db.Exec(ctx,
		`UPDATE users SET password_hash = $2, password_set = TRUE, updated_at = NOW() WHERE id = $1`,
		claims.UserID, string(hash)); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, ErrDBUnavailable)
		return
	}
	// 会话撤销：改密后**所有**旧 token 立即失效（含发起本次改密的那个 —— 前端会拿到
	// 401 并跳登录页，这是"改密即登出所有设备"的常见语义，安全优先）。
	// 撤销失败不回滚改密，但记日志（否则"以为踢下线了、其实没有"）。
	if rerr := revokeUserSessions(ctx, claims.UserID); rerr != nil {
		slog.Error("set password: session revoke failed", "user_id", claims.UserID, "error", rerr)
	}
	db.AuditLog(ctx, claims.UserID, claims.TenantID, "password_set", "/v1/auth/password", "", r.RemoteAddr, nil)
	OK(w, map[string]string{"status": "updated"})
}
// findIdentityUser 按 (provider_id, subject) 查绑定用户；未绑定返回 pgx.ErrNoRows。
func (h *SSOHandler) findIdentityUser(ctx context.Context, providerID, subject string) (*ssoUser, error) {
	var user ssoUser
	err := h.db.QueryRow(ctx,
		`SELECT u.id, u.email, u.name, u.role
		 FROM ent_user_identities ui JOIN users u ON u.id = ui.user_id
		 WHERE ui.provider_id = $1 AND ui.subject = $2`, providerID, subject).
		Scan(&user.ID, &user.Email, &user.Name, &user.Role)
	if err != nil {
		return nil, err
	}
	return &user, nil
}
// provisionAndBind 自动建号并写入外部身份绑定。
// 租户取 provider 配置；email 取身份信息（缺失时以 subject 兜底）；
// password_hash 写入随机不可碰撞 bcrypt 且 password_set=FALSE（不可口令登录）；
// role 按 role_mapping 匹配、缺省 "user"；携带手机号时回填 users.phone。
func (h *SSOHandler) provisionAndBind(r *http.Request, provider *ssoProvider, idToken *auth.IDTokenResult) (*ssoUser, error) {
	ctx := r.Context()

	email := idToken.Email
	if email == "" {
		email = idToken.Subject + "@sso.local"
	}
	name := idToken.Name
	if name == "" {
		name = email
	}
	role := resolveRole(provider.RoleMapping, idToken.Roles)

	// 随机 32 字节密码 → bcrypt；SSO 建号用户不可用口令登录，直到主动设置密码
	randomPassword := make([]byte, 32)
	if _, err := rand.Read(randomPassword); err != nil {
		return nil, err
	}
	passwordHash, err := bcrypt.GenerateFromPassword([]byte(hex.EncodeToString(randomPassword)), auth.BcryptCost)
	if err != nil {
		return nil, err
	}

	// 同租户同 email 已有本地账号时直接绑定，避免重复建号
	var user ssoUser
	err = h.db.QueryRow(ctx,
		`SELECT id, email, name, role FROM users WHERE tenant_id = $1 AND email = $2`,
		provider.TenantID, email).Scan(&user.ID, &user.Email, &user.Name, &user.Role)
	if errors.Is(err, pgx.ErrNoRows) {
		err = h.db.QueryRow(ctx,
			`INSERT INTO users (id, tenant_id, email, name, password_hash, role, password_set)
			 -- id 必须**显式**给：users.id 是 varchar(36) NOT NULL 且没有默认值（见 baseline 迁移）。
			 -- 与邮箱/短信自动注册同一个坑：漏这一列 ⇒ null value in column "id"。
			 VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, FALSE) RETURNING id`,
			provider.TenantID, email, name, string(passwordHash), role).Scan(&user.ID)
		if err != nil {
			return nil, err
		}
		user.Email, user.Name, user.Role = email, name, role
		// 新建号的 SSO 用户同样要成员记录（理由同 provisionSmsUser）。
		// 只在 INSERT 分支补：已存在的账号在它自己建号时就写过。
		// provider.TenantID 是字符串，与本文件其他地方一致走 parseTenantID。
		if merr := addTenantMember(ctx, h.db, parseTenantID(provider.TenantID), user.ID, role); merr != nil {
			slog.Warn("add tenant member failed (sso provision)", "user_id", user.ID, "error", merr)
		}
	} else if err != nil {
		return nil, err
	}

	// 携带手机号（微信/钉钉等）时回填；失败仅记录不阻断
	if idToken.Phone != "" {
		if _, err := h.db.Exec(ctx,
			`UPDATE users SET phone = $2, updated_at = NOW() WHERE id = $1 AND (phone IS NULL OR phone = '')`,
			user.ID, idToken.Phone); err != nil {
			// 手机号可能与他人冲突（唯一索引），不阻断登录主流程
			_ = err
		}
	}

	if _, err := h.db.Exec(ctx,
		`INSERT INTO ent_user_identities (user_id, provider_id, subject, email)
		 VALUES ($1, $2, $3, $4)`,
		user.ID, provider.ID, idToken.Subject, email); err != nil {
		if isUniqueViolation(err) {
			// 并发场景：另一请求已完成绑定，直接复用既有绑定用户
			if bound, lookupErr := h.findIdentityUser(ctx, provider.ID, idToken.Subject); lookupErr == nil {
				return bound, nil
			}
		}
		return nil, err
	}
	db.AuditLog(ctx, user.ID, parseTenantID(provider.TenantID), "sso_provision", "/v1/auth/sso/callback",
		"provider="+provider.Name+" email="+email, r.RemoteAddr, nil)
	return &user, nil
}
// resolveRole 按 provider.role_mapping 将 IdP "roles" claim 映射为本地角色；
// 无命中时缺省 "user"。纯函数，便于单元测试。
func resolveRole(mapping map[string]string, idpRoles []string) string {
	for _, r := range idpRoles {
		if mapped, ok := mapping[r]; ok && mapped != "" {
			return mapped
		}
	}
	return "user"
}
