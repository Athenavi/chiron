package auth

import (
	"context"
	"fmt"

	"github.com/athenavi/chiron/internal/db"
	"github.com/jackc/pgx/v5/pgxpool"
)

// LocalAuthService implements AuthService using local database.
type LocalAuthService struct {
	authenticator *Authenticator
	db            *pgxpool.Pool
}

// NewLocalAuthService creates a new local auth service.
func NewLocalAuthService(authenticator *Authenticator, db *pgxpool.Pool) *LocalAuthService {
	return &LocalAuthService{
		authenticator: authenticator,
		db:            db,
	}
}

func (s *LocalAuthService) GetUser(ctx context.Context, userID string) (*User, error) {
	if s.db == nil {
		return nil, fmt.Errorf("database not available")
	}

	var user User
	err := s.db.QueryRow(ctx,
		`SELECT id, COALESCE(email, ''), COALESCE(name, ''), COALESCE(role, 'user'), COALESCE(tenant_id, 0)
		 FROM users WHERE id = $1`, userID).
		Scan(&user.ID, &user.Email, &user.Name, &user.Role, &user.TenantID)
	if err != nil {
		return nil, fmt.Errorf("get user: %w", err)
	}
	return &user, nil
}

// GetUserByEmail 多租户查询：email+tenant_id 双键定位，防止跨租户登录。
// tenantID 为空时回退 DefaultTenantID（单租户兼容，未来收紧）。
func (s *LocalAuthService) GetUserByEmail(ctx context.Context, email string, tenantID int16) (*User, error) {
	if s.db == nil {
		return nil, fmt.Errorf("database not available")
	}
	if tenantID == 0 {
		tenantID = db.DefaultTenantID
	}

	var user User
	err := s.db.QueryRow(ctx,
		`SELECT id, COALESCE(email, ''), COALESCE(name, ''), COALESCE(role, 'user'), COALESCE(tenant_id, 0)
		 FROM users WHERE email = $1 AND tenant_id = $2`, email, tenantID).
		Scan(&user.ID, &user.Email, &user.Name, &user.Role, &user.TenantID)
	if err != nil {
		return nil, fmt.Errorf("get user by email: %w", err)
	}
	return &user, nil
}

func (s *LocalAuthService) ValidateToken(ctx context.Context, token string) (*Claims, error) {
	if s.authenticator == nil {
		return nil, fmt.Errorf("authenticator not available")
	}

	claims, err := s.authenticator.ValidateToken(token)
	if err != nil {
		return nil, err
	}

	return &Claims{
		UserID:   claims.UserID,
		Email:    claims.Email,
		Role:     claims.Role,
		TenantID: claims.TenantID,
		Perms:    claims.Perms,
	}, nil
}

func (s *LocalAuthService) CreateUser(ctx context.Context, user *User) error {
	if s.db == nil {
		return fmt.Errorf("database not available")
	}

	_, err := s.db.Exec(ctx,
		`INSERT INTO users (id, email, name, role, tenant_id, created_at, updated_at)
		 VALUES ($1, $2, $3, $4, $5, NOW(), NOW())`,
		user.ID, user.Email, user.Name, user.Role, user.TenantID)
	if err != nil {
		return fmt.Errorf("create user: %w", err)
	}
	return nil
}

func (s *LocalAuthService) UpdateUser(ctx context.Context, user *User) error {
	if s.db == nil {
		return fmt.Errorf("database not available")
	}

	_, err := s.db.Exec(ctx,
		`UPDATE users SET email = $2, name = $3, role = $4, tenant_id = $5, updated_at = NOW() WHERE id = $1`,
		user.ID, user.Email, user.Name, user.Role, user.TenantID)
	if err != nil {
		return fmt.Errorf("update user: %w", err)
	}
	return nil
}

func (s *LocalAuthService) DeleteUser(ctx context.Context, userID string) error {
	if s.db == nil {
		return fmt.Errorf("database not available")
	}

	_, err := s.db.Exec(ctx, `DELETE FROM users WHERE id = $1`, userID)
	if err != nil {
		return fmt.Errorf("delete user: %w", err)
	}
	return nil
}

// ListUsers 多租户隔离：仅返回指定租户的用户。
func (s *LocalAuthService) ListUsers(ctx context.Context, tenantID string) ([]User, error) {
	if s.db == nil {
		return nil, fmt.Errorf("database not available")
	}
	if tenantID == "" {
		return nil, fmt.Errorf("tenant_id is required for ListUsers")
	}

	rows, err := s.db.Query(ctx,
		`SELECT id, COALESCE(email, ''), COALESCE(name, ''), COALESCE(role, 'user'), COALESCE(tenant_id, 0)
		 FROM users WHERE tenant_id = $1 ORDER BY created_at DESC LIMIT 100`,
		tenantID)
	if err != nil {
		return nil, fmt.Errorf("list users: %w", err)
	}
	defer rows.Close()

	var users []User
	for rows.Next() {
		var user User
		if err := rows.Scan(&user.ID, &user.Email, &user.Name, &user.Role, &user.TenantID); err != nil {
			continue
		}
		users = append(users, user)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate users: %w", err)
	}

	if users == nil {
		users = []User{}
	}
	return users, nil
}
