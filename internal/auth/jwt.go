package auth

import (
	"context"
	"fmt"
	"os"
	"time"

	"github.com/athenavi/chiron/internal/id"
	"github.com/golang-jwt/jwt/v5"
)

type Claims struct {
	UserID   string   `json:"uid"`
	Email    string   `json:"email"`
	Role     string   `json:"role"` // owner / admin / user
	TenantID string   `json:"tenant_id,omitempty"`
	Perms    []string `json:"perms,omitempty"`
	jwt.RegisteredClaims
}

type Authenticator struct {
	secret     []byte
	expiration time.Duration
}

// 注意：这里原先有一个 `SigningSecret()`，被媒体签名 URL 复用来做 HMAC。
// 复用 JWT 签名密钥会让「媒体 URL 的 HMAC」与「会话令牌签名」共享同一把钥匙，
// 已移除。媒体签名改用独立派生密钥，见 internal/api/media_sign.go 的 mediaSignSecret()。

func NewAuthenticator(secret string, expiration time.Duration) *Authenticator {
	return &Authenticator{
		secret:     []byte(secret),
		expiration: expiration,
	}
}

// GenerateToken 签发 JWT。tenantID 必须由调用方从用户记录中传入（多租户隔离键）。
func (a *Authenticator) GenerateToken(userID, email, role, tenantID string, perms []string) (string, error) {
	now := time.Now()
	claims := &Claims{
		UserID:   userID,
		Email:    email,
		Role:     role,
		TenantID: tenantID,
		Perms:    perms,
		RegisteredClaims: jwt.RegisteredClaims{
			ExpiresAt: jwt.NewNumericDate(now.Add(a.expiration)),
			IssuedAt:  jwt.NewNumericDate(now),
			NotBefore: jwt.NewNumericDate(now),
			Issuer:    "chiron",
			ID:        generateID(),
		},
	}

	token := jwt.NewWithClaims(jwt.SigningMethodHS256, claims)
	return token.SignedString(a.secret)
}

func (a *Authenticator) ValidateToken(tokenStr string) (*Claims, error) {
	token, err := jwt.ParseWithClaims(tokenStr, &Claims{}, func(t *jwt.Token) (interface{}, error) {
		if _, ok := t.Method.(*jwt.SigningMethodHMAC); !ok {
			return nil, fmt.Errorf("unexpected signing method: %v", t.Header["alg"])
		}
		return a.secret, nil
	})
	if err != nil {
		return nil, fmt.Errorf("token validation failed")
	}

	claims, ok := token.Claims.(*Claims)
	if !ok || !token.Valid {
		return nil, fmt.Errorf("invalid token claims")
	}

	return claims, nil
}

// 注意：这里原先有一个 `RefreshToken(tokenStr)`，语义是「校验旧 token 后原样
// 复制其 claims 重新签发」。它正是「管理员降权后刷新仍保留旧权限」的根因，
// 已删除。刷新改由 api 层从 users 表重取 role/tenant 后重新签发
// （见 internal/api/auth.go 的 AuthHandler.Refresh）。

type contextKey string

const ClaimsKey contextKey = "claims"

func WithClaims(ctx context.Context, claims *Claims) context.Context {
	return context.WithValue(ctx, ClaimsKey, claims)
}

func GetClaims(ctx context.Context) *Claims {
	if ctx == nil {
		return nil
	}
	claims, _ := ctx.Value(ClaimsKey).(*Claims)
	return claims
}

func generateID() string {
	return id.NextID()
}

// Global JWT authenticator instance for use in middleware.
var jwtAuth *Authenticator

// InitJWTAuth initializes the global JWT authenticator from environment variables.
// Returns an error if JWT_SECRET is missing or too short, instead of calling os.Exit.
func InitJWTAuth() error {
	secret := os.Getenv("JWT_SECRET")
	if secret == "" {
		return fmt.Errorf("JWT_SECRET environment variable is required and must be at least 32 characters")
	}
	if len(secret) < 32 {
		return fmt.Errorf("JWT_SECRET must be at least 32 characters long, got %d", len(secret))
	}
	expiration := 24 * time.Hour
	if v := os.Getenv("JWT_EXPIRATION"); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			expiration = d
		}
	}
	jwtAuth = NewAuthenticator(secret, expiration)
	return nil
}

// ParseJWT is a convenience function that parses and validates a JWT token string.
func ParseJWT(token string) (*Claims, error) {
	if jwtAuth == nil {
		return nil, fmt.Errorf("JWT authenticator not initialized, call InitJWTAuth() first")
	}
	return jwtAuth.ValidateToken(token)
}

// Built-in permissions
var (
	PermChatWrite   = "chat:write"
	PermChatRead    = "chat:read"
	PermAdminRead   = "admin:read"
	PermAdminWrite  = "admin:write"
	PermToolsExec   = "tools:execute"
	PermUsersManage = "users:manage"
	// 企业功能权限点（owner/admin 默认拥有）
	PermAuditRead    = "audit:read"
	PermEntManage    = "ent:manage"
	PermPolicyManage = "policy:manage"
	PermMarketManage = "market:manage"
	PermSSOManage    = "sso:manage"
)

// RolePermissions maps roles to permission sets
var RolePermissions = map[string][]string{
	"owner": {PermChatWrite, PermChatRead, PermAdminRead, PermAdminWrite, PermToolsExec, PermUsersManage, PermAuditRead, PermEntManage, PermPolicyManage, PermMarketManage, PermSSOManage},
	"admin": {PermChatWrite, PermChatRead, PermAdminRead, PermAdminWrite, PermToolsExec, PermAuditRead, PermEntManage, PermPolicyManage, PermMarketManage, PermSSOManage},
	"user":  {PermChatWrite, PermChatRead, PermToolsExec},
}

func HasPermission(claims *Claims, perm string) bool {
	if claims == nil {
		return false
	}
	// 如果 claims.Perms 被显式设置（含空数组），以它为唯一权限白名单。
	// 用 `!= nil` 而非 `len(...) > 0`：后者会让「显式授予空权限」被回退到角色
	// 默认权限而放大（P1）。仅当 Perms 为 nil（未设置）才回退到 RolePermissions。
	if claims.Perms != nil {
		for _, p := range claims.Perms {
			if p == perm {
				return true
			}
		}
		return false
	}
	// Fallback 到 RolePermissions（仅当 Perms 为空时）
	perms, ok := RolePermissions[claims.Role]
	if !ok {
		return false
	}
	for _, p := range perms {
		if p == perm {
			return true
		}
	}
	return false
}
