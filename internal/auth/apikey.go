package auth

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"strings"
	"time"
)

type APIKey struct {
	ID        string    `json:"id"`
	UserID    string    `json:"user_id"`
	Name      string    `json:"name"`
	Key       string    `json:"key,omitempty"` // only shown on creation
	LastUsed  time.Time `json:"last_used"`
	CreatedAt time.Time `json:"created_at"`
	ExpiresAt time.Time `json:"expires_at,omitempty"`
}

type APIKeyStore interface {
	Create(ctx *APIKey) error
	GetByKey(key string) (*APIKey, error)
	ListByUser(userID string) ([]*APIKey, error)
	Delete(id string) error
	UpdateLastUsed(id string) error
}

func GenerateAPIKey() (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", fmt.Errorf("crypto/rand.Read failed: %w", err)
	}
	return "mcc_" + hex.EncodeToString(b), nil
}

func ValidateAPIKeyFormat(key string) error {
	if !strings.HasPrefix(key, "mcc_") {
		return fmt.Errorf("api key must start with 'mcc_'")
	}
	hexPart := key[4:]
	if len(hexPart) != 64 {
		return fmt.Errorf("api key hex part must be 64 characters, got %d", len(hexPart))
	}
	if _, err := hex.DecodeString(hexPart); err != nil {
		return fmt.Errorf("api key hex part is not valid hex: %w", err)
	}
	return nil
}

// apiKeyDeniedPerms 是**不通过 API Key 授予**的权限：管理类操作要求交互式登录。
//
// 理由：API Key 是长期凭据（不过期或长过期），通常被脚本/CI 保存，泄漏面远大于短期
// JWT。此前 API Key 直接继承 `RolePermissions[role]`，等于"一把 key = 该用户全部权限"，
// 包括改配额、管用户、改策略、读审计等。这里把管理面剔出去，业务面（chat/tools）保留。
var apiKeyDeniedPerms = map[string]bool{
	PermAdminRead:   true,
	PermAdminWrite:  true,
	PermUsersManage: true,
	PermPolicyManage: true,
	PermEntManage:   true,
	PermMarketManage: true,
	PermAuditRead:   true,
}

// APIKeyPermissions 返回 API Key 可用的权限集：角色权限里剔除管理类权限。
func APIKeyPermissions(role string) []string {
	all := RolePermissions[role]
	out := make([]string, 0, len(all))
	for _, p := range all {
		if !apiKeyDeniedPerms[p] {
			out = append(out, p)
		}
	}
	return out
}
