package auth

import (
	"context"
	"crypto/sha256"
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
	TenantID int16    `json:"tenant_id,omitempty"`
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
func (a *Authenticator) GenerateToken(userID, email, role string, tenantID int16, perms []string) (string, error) {
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

// ── MFA 两步登录的「挑战令牌」 ──
//
// 密码正确但需要 TOTP 时，不能直接发正式 token（那就等于 MFA 可以被跳过），
// 也不能只返回一个用户 id（无状态接口里客户端可随意伪造 userID 去撞验证码）。
// 做法是签一个**只能用于本次验证**的短期令牌。
//
// 关键设计：用**派生密钥**签名，而不是主密钥。
// 这样 `ValidateToken`（AuthMiddleware 用的那个）天然无法通过 —— 签名不同，
// 攻击者拿挑战令牌去访问业务接口会得到 "invalid token"，
// 而不是依赖某处记得去检查 "这是挑战令牌不是正式令牌"。
// 靠显式检查标志位来隔离，漏一处就是完整的认证绕过；靠密钥隔离则是结构性的。
const mfaChallengeTTL = 5 * time.Minute

// MFAChallengeTTLSeconds 导出挑战令牌有效期（秒），供 API 层在响应里告知客户端。
// 不在 api 包另写一个常量：两处各写一个 300 会在调整有效期时发生漂移，
// 而漂移的表现是"前端以为还有 5 分钟、实际 2 分钟就过期了"，很难排查。
func MFAChallengeTTLSeconds() int {
	return int(mfaChallengeTTL / time.Second)
}

// mfaChallengeKey 由主密钥派生，与正式令牌的签名密钥不同。
func (a *Authenticator) mfaChallengeKey() []byte {
	sum := sha256.Sum256(append(append([]byte{}, a.secret...), []byte("|mfa-challenge|v1")...))
	return sum[:]
}

// GenerateMFAChallengeToken 签发"待二次验证"令牌。
// 只能配合 ValidateMFAChallengeToken 使用，无法通过 ValidateToken。
func (a *Authenticator) GenerateMFAChallengeToken(userID string) (string, error) {
	now := time.Now()
	claims := &Claims{
		UserID: userID,
		RegisteredClaims: jwt.RegisteredClaims{
			ExpiresAt: jwt.NewNumericDate(now.Add(mfaChallengeTTL)),
			IssuedAt:  jwt.NewNumericDate(now),
			NotBefore: jwt.NewNumericDate(now),
			Issuer:    "chiron-mfa",
			Subject:   "mfa-challenge", // 双保险：即便密钥被混用，Claim 也不匹配
			ID:        generateID(),
		},
	}
	token := jwt.NewWithClaims(jwt.SigningMethodHS256, claims)
	return token.SignedString(a.mfaChallengeKey())
}

// ValidateMFAChallengeToken 校验挑战令牌，返回其携带的 userID。
func (a *Authenticator) ValidateMFAChallengeToken(tokenStr string) (string, error) {
	token, err := jwt.ParseWithClaims(tokenStr, &Claims{}, func(t *jwt.Token) (interface{}, error) {
		if _, ok := t.Method.(*jwt.SigningMethodHMAC); !ok {
			return nil, fmt.Errorf("unexpected signing method: %v", t.Header["alg"])
		}
		return a.mfaChallengeKey(), nil
	})
	if err != nil {
		return "", fmt.Errorf("mfa challenge validation failed")
	}
	claims, ok := token.Claims.(*Claims)
	if !ok || !token.Valid || claims.Subject != "mfa-challenge" || claims.UserID == "" {
		return "", fmt.Errorf("invalid mfa challenge")
	}
	return claims.UserID, nil
}

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
	// PermBillingManage 套餐目录管理（P2-2c）。
	//
	// 单独一个权限点而不是复用 PermAdminWrite：套餐价格与所含权益直接决定
	// 平台收多少钱、卖什么货，属"定价"而非"日常运维"。目前授予 owner/admin
	// 与其它企业权限点同口径，但独立成点后可按需只给运营、不给技术运维。
	PermBillingManage = "billing:manage"
)

// RolePermissions maps roles to permission sets
var RolePermissions = map[string][]string{
	"owner": {PermChatWrite, PermChatRead, PermAdminRead, PermAdminWrite, PermToolsExec, PermUsersManage, PermAuditRead, PermEntManage, PermPolicyManage, PermMarketManage, PermSSOManage, PermBillingManage},
	"admin": {PermChatWrite, PermChatRead, PermAdminRead, PermAdminWrite, PermToolsExec, PermAuditRead, PermEntManage, PermPolicyManage, PermMarketManage, PermSSOManage, PermBillingManage},
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
