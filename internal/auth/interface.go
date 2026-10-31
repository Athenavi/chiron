package auth

import "context"

// User represents a user in the system.
type User struct {
	ID       string `json:"id"`
	Email    string `json:"email"`
	Name     string `json:"name"`
	Role     string `json:"role"`
	TenantID int16  `json:"tenant_id"`
}

// AuthService defines the authentication service interface.
type AuthService interface {
	// GetUser retrieves a user by ID.
	GetUser(ctx context.Context, userID string) (*User, error)

	// GetUserByEmail retrieves a user by email within the given tenant.
	// tenantID 防止跨租户同 email 误登录。
	GetUserByEmail(ctx context.Context, email string, tenantID int16) (*User, error)

	// ValidateToken validates a JWT token and returns claims.
	ValidateToken(ctx context.Context, token string) (*Claims, error)

	// UpdateUser updates an existing user.
	UpdateUser(ctx context.Context, user *User) error

	// DeleteUser deletes a user by ID.
	DeleteUser(ctx context.Context, userID string) error

	// ListUsers lists users in the given tenant (admin only).
	ListUsers(ctx context.Context, tenantID int16) ([]User, error)
}
