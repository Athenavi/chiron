package api

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/id"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// api_keys_handler.go —— 用户级 API Key 自助（roadmap §11.3 / P1-3）。
//
// 背景（已核实）：`api_keys` 表早已存在，且**已经在鉴权链路里**（`middleware.go`
// 用 `X-API-Key` → sha256 → JOIN users 校验 revoked/expires_at/tenant_id）。但全仓
// 没有任何写路径：既没有 `INSERT INTO api_keys`，也没有地方更新 `last_used_at`。
// 也就是说，用户此前**只能手工往表里插行**才能拿到一把 key。
//
// 这个 handler 补的就是这段路：用户自己创建 / 查看 / 吊销，管理员保留全局视图
// （管理端那两个 `/v1/admin/api-keys` 是**平台的 LLM 提供商密钥**，另一回事，
// 见 `internal/api/llm_keys.go`）。
//
// 三条不可动摇的约定：
//  1. **明文只返回一次**：库里只存 sha256 hex，与 middleware 的校验方式完全一致。
//     换算法会让存量 key 全部失效。
//  2. **永不回 key_hash**：它不是密码，但也没有任何让客户端看见的理由。
//  3. **越权一律 404**：403 会确认"这个 id 存在"，是可枚举性泄漏。

const (
	// apiKeyPrefix 是明文前缀，让用户一眼认出这是本系统的 key（也能被密钥扫描
	// 类工具识别）。
	apiKeyPrefix = "sk-"

	// apiKeyEntropyBytes 是随机部分的长度。32 字节 = 256 bit，远超暴力枚举可行性。
	apiKeyEntropyBytes = 32

	// apiKeyNameMaxLen 与 `api_keys.name` 的 varchar(128) 对齐。
	// 按**字符**（rune）计数而非字节：PostgreSQL 的 varchar(n) 也是按字符计的，
	// 用 len() 会把一个中文名算成 3 个字符，导致明明能存的name被前端拒掉。
	apiKeyNameMaxLen = 128

	// apiKeyMaxActivePerUser 是每个用户同时有效的 key 上限。
	//
	// 这不是产品限制，而是防御性的：创建 key 只需一次 INSERT + 一次 sha256，
	// 认证用户可以无限刷，把表撑大。50 这个数字足够宽松，正常用户不会碰到，
	// 但能挡住明显的滥用。
	apiKeyMaxActivePerUser = 50
)

// ── SQL 语句 ──
//
// 提成包级常量是为了让真实 PostgreSQL 冒烟用例（`*_live_test.go`）能直接引用
// **生产语句本身**。若测试里另抄一份，两边一旦漂移就会出现"测试全绿、上线才炸"
// —— 而 SQL 漂移正是这组用例要防的东西。

// apiKeysListQuery 列出某用户的全部 key（含已吊销，供审计与"我建过哪些"）。
// **不 SELECT key_hash**：类型层面已经不暴露，这里再少读一列。
const apiKeysListQuery = `SELECT id, name, created_at, last_used_at, expires_at, COALESCE(revoked, false)
		   FROM api_keys
		  WHERE user_id = $1
		  ORDER BY created_at DESC NULLS LAST, id DESC`

// apiKeyCountActiveQuery 数当前有效 key，用于上限校验。
const apiKeyCountActiveQuery = `SELECT COUNT(*) FROM api_keys
		  WHERE user_id = $1 AND COALESCE(revoked, false) = false`

// apiKeyInsertQuery 写入一把新 key。`RETURNING created_at` 省一次回查。
const apiKeyInsertQuery = `INSERT INTO api_keys (id, user_id, name, key_hash, created_at, revoked)
		 VALUES ($1, $2, $3, $4, now(), false)
		 RETURNING created_at`

// apiKeyRevokeQuery 软删除（`revoked = true`）保留审计。
// `AND COALESCE(revoked, false) = false` 让重复吊销影响 0 行 → handler 回 404，
// 并发双请求时只有第一个生效。
const apiKeyRevokeQuery = `UPDATE api_keys SET revoked = true
		  WHERE id = $1 AND user_id = $2 AND COALESCE(revoked, false) = false`

// apiKeyAuthQuery 是 `X-API-Key` 鉴权的唯一查询（`AuthMiddleware` 使用）。
//
// 一次 JOIN 完成四件事：key 存在、未吊销、未过期、所属用户绑定了租户。
// `expires_at IS NULL` 表示永不过期 —— 漏掉这一支会让"永久 key"变成永久无效。
//
// 提成常量是为了让真实 PostgreSQL 冒烟用例能直接跑**生产语句本身**：这条 SQL
// 决定了每个 API 请求的身份，列数或扫描顺序错了，单测（只断言字符串包含）
// 完全发现不了。
const apiKeyAuthQuery = `SELECT ak.id, u.id, u.role, COALESCE(u.tenant_id::text, '') AS tenant_id
					 FROM users u
					 JOIN api_keys ak ON ak.user_id = u.id
					 WHERE ak.key_hash = $1
					   AND COALESCE(ak.revoked, false) = false
					   AND (ak.expires_at IS NULL OR ak.expires_at > NOW())`

// apiKeyMarkUsedQuery 回写 `last_used_at`（由 `markAPIKeyUsed` 按节流调用）。
const apiKeyMarkUsedQuery = `UPDATE api_keys SET last_used_at = now() WHERE id = $1`

// apiKeyStore 是该 handler 需要的最小数据库能力。
//
// 与 `inviteStore` 同一个理由：收窄成接口后，单测可以用假实现覆盖"越权 404""tenant
// 为空拦截"这类**分支**，而不需要连真实数据库（仓库没有 pgxmock/sqlmock，
// 单元测试也不允许联网）。`db.DBManager` 天然满足这个接口。
type apiKeyStore interface {
	Query(ctx context.Context, sql string, args ...interface{}) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...interface{}) pgx.Row
	Exec(ctx context.Context, sql string, args ...interface{}) (pgconn.CommandTag, error)
}

// APIKeyRow 是列表里的一行。
//
// **不含 `key_hash`**，这是类型层面的保证 —— 只要不往这个结构体加字段，
// 序列化出来就漏不出去。
type APIKeyRow struct {
	ID        string     `json:"id"`
	Name      string     `json:"name"`
	CreatedAt *time.Time `json:"created_at,omitempty"`
	// LastUsedAt 目前**永远是空**：鉴权链路虽然读 `api_keys`，但从不回写这一列。
	// 如实返回而不是隐藏，是为了将来补上写入后前端无需改动就能显示。
	LastUsedAt *time.Time `json:"last_used_at,omitempty"`
	ExpiresAt  *time.Time `json:"expires_at,omitempty"`
	Revoked    bool       `json:"revoked"`
}

// createdAPIKey 是创建响应：在列表字段之外多一个**明文** key。
// 这是整个接口里唯一会返回明文的地方。
type createdAPIKey struct {
	APIKeyRow
	Key string `json:"key"`
}

type APIKeyHandler struct {
	store apiKeyStore
}

func NewAPIKeyHandler() *APIKeyHandler {
	return &APIKeyHandler{store: db.GlobalDBManager}
}

func (h *APIKeyHandler) RegisterRoutes(mux *http.ServeMux, authMW, rlMW routeMiddleware) {
	// 用户侧路径，**不带** adminReadMW —— 越权靠 SQL 里的 `user_id = $1` 挡住，
	// 而不是靠管理员中间件（普通用户本来就该能用）。
	mux.Handle("GET /v1/api-keys", authMW(rlMW(http.HandlerFunc(h.List))))
	mux.Handle("POST /v1/api-keys", authMW(rlMW(http.HandlerFunc(h.Create))))
	mux.Handle("DELETE /v1/api-keys/{id}", authMW(rlMW(http.HandlerFunc(h.Revoke))))
}

// ── 纯函数（可直接单测）──

// generateAPIKey 生成一对（明文, sha256 hex）。
//
// hash 必须与 `middleware.go` 的校验完全一致：那边是
// `sha256.Sum256([]byte(key))` 再 `hex.EncodeToString`。任何偏移都会让新 key
// 拿到手就 401，且现象很迷惑（"刚创建的 key 说无效"）。
func generateAPIKey() (plain, hash string, err error) {
	var b [apiKeyEntropyBytes]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", "", fmt.Errorf("api key: crypto/rand unavailable: %w", err)
	}
	plain = apiKeyPrefix + hex.EncodeToString(b[:])
	sum := sha256.Sum256([]byte(plain))
	return plain, hex.EncodeToString(sum[:]), nil
}

// normalizeAPIKeyName 校验并规范化用户给的名称。
//
// 空名一律拒绝而不是自动填 "default"：key 列表里一排同样的名字，用户根本分不清
// 该吊销哪一个 —— 那正是"我这把 key 泄露了要立刻停用"时最需要分清的时刻。
func normalizeAPIKeyName(raw string) (string, error) {
	name := strings.TrimSpace(raw)
	if name == "" {
		return "", errors.New("name is required")
	}
	if n := len([]rune(name)); n > apiKeyNameMaxLen {
		return "", fmt.Errorf("name must be at most %d characters", apiKeyNameMaxLen)
	}
	return name, nil
}

// ── HTTP ──

// List 返回当前用户的全部 key（含已吊销的）。
//
// 已吊销的**保留展示**：用户需要看到"这把 key 是我上周停掉的"，否则会以为
// 自己从没创建过，进而重复创建。
func (h *APIKeyHandler) List(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	rows, err := h.store.Query(r.Context(),
		apiKeysListQuery,
		claims.UserID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list api keys failed")
		return
	}
	defer rows.Close()

	// 空列表返回 [] 而不是 null：前端 `v-for` 遇到 null 会报错。
	out := make([]APIKeyRow, 0, 8)
	for rows.Next() {
		var row APIKeyRow
		if err := rows.Scan(&row.ID, &row.Name, &row.CreatedAt,
			&row.LastUsedAt, &row.ExpiresAt, &row.Revoked); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "scan api key failed")
			return
		}
		out = append(out, row)
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list api keys failed")
		return
	}

	OK(w, out)
}

// Create 生成一把新 key，**明文只在这一个响应里出现**。
func (h *APIKeyHandler) Create(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	var body struct {
		Name string `json:"name"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	name, err := normalizeAPIKeyName(body.Name)
	if err != nil {
		BadRequest(w, err.Error())
		return
	}

	// 先确认用户已绑租户。`middleware.go` 对 `tenant_id` 为空的用户直接拒绝，
	// 所以在这里放行一把 key，会让用户拿到"key 生成成功但调用永远 401"的结果 ——
	// 那种失败最难自查。明确拦下来并说清楚原因，比让他自己去试要省事得多。
	var tenantID *int16
	if err := h.store.QueryRow(r.Context(),
		`SELECT tenant_id FROM users WHERE id = $1`, claims.UserID).Scan(&tenantID); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			// 有合法 token 却查不到用户：账号已被删除，返回 404 而不是 500。
			NotFound(w, ErrNotFound)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "load user failed")
		return
	}
	if tenantID == nil {
		BadRequest(w, "api keys require a tenant; ask an administrator to assign one")
		return
	}

	var active int
	if err := h.store.QueryRow(r.Context(),
		apiKeyCountActiveQuery,
		claims.UserID).Scan(&active); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "count api keys failed")
		return
	}
	if active >= apiKeyMaxActivePerUser {
		BadRequest(w, fmt.Sprintf(
			"too many active api keys (limit %d); revoke one before creating another",
			apiKeyMaxActivePerUser))
		return
	}

	plain, hash, err := generateAPIKey()
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "generate api key failed")
		return
	}
	keyID, err := id.UUID()
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "generate api key id failed")
		return
	}

	// RETURNING created_at：响应里要带上创建时间，省一次回查。
	var createdAt *time.Time
	if err := h.store.QueryRow(r.Context(),
		apiKeyInsertQuery,
		keyID, claims.UserID, name, hash).Scan(&createdAt); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create api key failed")
		return
	}

	slog.Info("api key created", "user_id", claims.UserID, "key_id", keyID, "name", name)
	Created(w, createdAPIKey{
		APIKeyRow: APIKeyRow{ID: keyID, Name: name, CreatedAt: createdAt},
		Key:       plain,
	})
}

// Revoke 吊销一把 key。
//
// 用 DELETE 表达"用户视角的删除"，但底层是 `revoked = true`：物理删除会让审计
// 断掉（"这把 key 什么时候建的、什么时候停的"），而排查泄露时正需要这段历史。
//
// **影响 0 行一律 404**，不区分"不存在"与"不是你的" —— 区分就是可枚举性泄漏。
func (h *APIKeyHandler) Revoke(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	keyID := strings.TrimSpace(r.PathValue("id"))
	if keyID == "" {
		NotFound(w, ErrNotFound)
		return
	}

	tag, err := h.store.Exec(r.Context(),
		apiKeyRevokeQuery,
		keyID, claims.UserID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "revoke api key failed")
		return
	}
	if tag.RowsAffected() == 0 {
		NotFound(w, ErrNotFound)
		return
	}

	slog.Info("api key revoked", "user_id", claims.UserID, "key_id", keyID)
	OK(w, map[string]interface{}{"id": keyID, "revoked": true})
}
