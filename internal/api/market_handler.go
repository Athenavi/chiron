package api

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"regexp"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// ── 能力市场：类型与常量 ─────────────────────────────────────────────────

// MarketItem 对应 ent_catalog_items 表（市场目录条目：plugin/skill）。
type MarketItem struct {
	ID        string          `json:"id"`
	Type      string          `json:"type"` // plugin / skill
	Name      string          `json:"name"`
	Version   string          `json:"version"`
	Manifest  json.RawMessage `json:"manifest"`
	Status    string          `json:"status"` // draft / published / retired
	CreatedBy *string         `json:"created_by,omitempty"`
	CreatedAt time.Time       `json:"created_at"`
	UpdatedAt time.Time       `json:"updated_at"`
}

// MarketGrant 对应 ent_catalog_installs 表（租户安装/启用记录）。
type MarketGrant struct {
	ItemID      string    `json:"item_id"`
	TenantID    string    `json:"tenant_id"`
	Enabled     bool      `json:"enabled"`
	InstalledAt time.Time `json:"installed_at"`
}

// 合法枚举（与迁移 CHECK 约束一致）
var (
	validMarketItemTypes = map[string]bool{"plugin": true, "skill": true, "agent": true, "mcp": true}
)

var validMarketItemName = regexp.MustCompile(`^[a-zA-Z0-9_.-]{1,128}$`)

// canCatalogTransition 状态机校验：仅允许 draft→published→retired，
// retired 为终态不可回 published。
func canCatalogTransition(from, to string) bool {
	switch {
	case from == "draft" && to == "published":
		return true
	case from == "published" && to == "retired":
		return true
	default:
		return false
	}
}

// ── Handler ──────────────────────────────────────────────────────────────

// MarketHandler 提供企业能力市场 API（目录条目 CRUD / 发布状态机 / 租户授权）。
// 路由注册由集成任务统一接入（本任务不注册）：
//
//	marketHandler := api.NewMarketHandler()
//	marketHandler.RegisterRoutes(mux, authMW)
type MarketHandler struct{}

// NewMarketHandler 创建市场 handler。
func NewMarketHandler() *MarketHandler { return &MarketHandler{} }

// RegisterRoutes 挂载市场路由（authMW + RequireEntPerm("market:manage")）。
func (h *MarketHandler) RegisterRoutes(mux *http.ServeMux, authMW func(http.Handler) http.Handler) {
	permMW := RequireEntPerm("market:manage")
	handle := func(pattern string, hf http.HandlerFunc) {
		mux.Handle(pattern, authMW(permMW(http.HandlerFunc(hf))))
	}
	handle("GET /v1/ent/market/items", h.ListItems)
	handle("POST /v1/ent/market/items", h.CreateItem)
	handle("GET /v1/ent/market/items/{id}", h.GetItem)
	handle("PUT /v1/ent/market/items/{id}", h.UpdateItem)
	handle("DELETE /v1/ent/market/items/{id}", h.DeleteItem)
	handle("POST /v1/ent/market/items/{id}/publish", h.PublishItem)
	handle("POST /v1/ent/market/items/{id}/retire", h.RetireItem)
	handle("GET /v1/ent/market/grants", h.ListGrants)
	handle("POST /v1/ent/market/grants", h.GrantItem)
	handle("PUT /v1/ent/market/grants/{itemID}/{tenantID}", h.UpdateGrant)
	handle("DELETE /v1/ent/market/grants/{itemID}/{tenantID}", h.DeleteGrant)
}

// ── 门控函数（导出给 plugin/skill 集成点，独立可测） ─────────────────────

// marketItemLookup 查询市场条目状态；测试可整体替换。
// 返回 (是否存在同名 published 条目, 该租户是否已安装且启用, error)。
var marketItemLookup = func(ctx context.Context, itemType, itemName, tenantID string) (bool, bool, error) {
	pool := db.ReadPool()
	if pool == nil {
		return false, false, errors.New("market: postgres pool unavailable")
	}
	var published bool
	if err := pool.QueryRow(ctx,
		`SELECT EXISTS(SELECT 1 FROM ent_catalog_items
		 WHERE type = $1 AND name = $2 AND status = 'published')`,
		itemType, itemName).Scan(&published); err != nil {
		return false, false, err
	}
	if !published {
		return false, false, nil
	}
	var enabled bool
	err := pool.QueryRow(ctx,
		`SELECT EXISTS(SELECT 1 FROM ent_catalog_installs i
		 JOIN ent_catalog_items c ON c.id = i.item_id
		 WHERE c.type = $1 AND c.name = $2 AND c.status = 'published'
		   AND i.tenant_id = $3 AND i.enabled)`,
		itemType, itemName, tenantID).Scan(&enabled)
	if err != nil {
		return false, false, err
	}
	return true, enabled, nil
}

// IsItemEnabledForTenant 判断租户是否可使用指定市场能力（plugin/skill 门控）：
//   - 市场无任何同名 published 条目 → true（未上架能力不受市场管控影响）；
//   - 有同名 published 条目 → 该租户已安装且 enabled 才 true；
//   - 查询失败 → fail-open（true + slog.Warn），保证市场基础设施故障不阻断能力使用。
func IsItemEnabledForTenant(ctx context.Context, itemType, itemName, tenantID string) (bool, error) {
	published, enabled, err := marketItemLookup(ctx, itemType, itemName, tenantID)
	if err != nil {
		slog.Warn("market gate: lookup failed, fail-open",
			"type", itemType, "name", itemName, "tenant", tenantID, "error", err)
		return true, nil
	}
	if !published {
		return true, nil
	}
	return enabled, nil
}

// ListEnabledMarketItems 返回租户已安装且启用的 published 市场条目
// （供插件/技能列表叠加市场已授权项）。
func ListEnabledMarketItems(ctx context.Context, itemType, tenantID string) ([]MarketItem, error) {
	pool := db.ReadPool()
	if pool == nil {
		return nil, errors.New("market: postgres pool unavailable")
	}
	rows, err := pool.Query(ctx,
		`SELECT c.id, c.type, c.name, c.version, c.manifest, c.status, c.created_by, c.created_at, c.updated_at
		 FROM ent_catalog_items c
		 JOIN ent_catalog_installs i ON i.item_id = c.id
		 WHERE c.type = $1 AND c.status = 'published' AND i.tenant_id = $2 AND i.enabled
		 ORDER BY c.name`, itemType, tenantID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := []MarketItem{}
	for rows.Next() {
		it, err := scanMarketItem(rows)
		if err != nil {
			return nil, err
		}
		items = append(items, *it)
	}
	return items, rows.Err()
}

// ── 内部辅助 ─────────────────────────────────────────────────────────────

const catalogItemColumns = `id, type, name, version, manifest, status, created_by, created_at, updated_at`

func scanMarketItem(row interface{ Scan(...any) error }) (*MarketItem, error) {
	var it MarketItem
	if err := row.Scan(&it.ID, &it.Type, &it.Name, &it.Version, &it.Manifest,
		&it.Status, &it.CreatedBy, &it.CreatedAt, &it.UpdatedAt); err != nil {
		return nil, err
	}
	return &it, nil
}

// ── 目录条目 CRUD ────────────────────────────────────────────────────────

// ListItems GET /v1/ent/market/items?type=&status=
func (h *MarketHandler) ListItems(w http.ResponseWriter, r *http.Request) {
	if claims := auth.GetClaims(r.Context()); claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	itemType := r.URL.Query().Get("type")
	status := r.URL.Query().Get("status")
	if itemType != "" && !validMarketItemTypes[itemType] {
		BadRequest(w, "type must be plugin or skill")
		return
	}
	query := `SELECT ` + catalogItemColumns + ` FROM ent_catalog_items WHERE TRUE`
	args := []any{}
	if itemType != "" {
		args = append(args, itemType)
		query += ` AND type = $` + string(rune('0'+len(args)))
	}
	if status != "" {
		args = append(args, status)
		query += ` AND status = $` + string(rune('0'+len(args)))
	}
	query += ` ORDER BY created_at DESC LIMIT 500`

	pool := db.ReadPool()
	if pool == nil {
		ServiceUnavailable(w, ErrDBUnavailable)
		return
	}
	rows, err := pool.Query(r.Context(), query, args...)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list market items failed")
		return
	}
	defer rows.Close()
	items := []MarketItem{}
	for rows.Next() {
		it, err := scanMarketItem(rows)
		if err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "list market items failed")
			return
		}
		items = append(items, *it)
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list market items failed")
		return
	}
	OK(w, items)
}

// CreateItem POST /v1/ent/market/items（初始状态 draft）。
func (h *MarketHandler) CreateItem(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	var body struct {
		Type     string          `json:"type"`
		Name     string          `json:"name"`
		Version  string          `json:"version"`
		Manifest json.RawMessage `json:"manifest"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if !validMarketItemTypes[body.Type] {
		BadRequest(w, "type must be plugin or skill")
		return
	}
	if !validMarketItemName.MatchString(body.Name) {
		BadRequest(w, "invalid item name")
		return
	}
	if body.Version == "" {
		body.Version = "1.0.0"
	}
	if len(body.Manifest) == 0 {
		body.Manifest = json.RawMessage(`{}`)
	}
	if !json.Valid(body.Manifest) {
		BadRequest(w, "manifest must be valid JSON")
		return
	}
	if db.Pool == nil {
		ServiceUnavailable(w, ErrDBUnavailable)
		return
	}
	created, err := scanMarketItem(db.GlobalDBManager.QueryRow(r.Context(),
		`INSERT INTO ent_catalog_items (type, name, version, manifest, status, created_by)
		 VALUES ($1, $2, $3, $4, 'draft', $5) RETURNING `+catalogItemColumns,
		body.Type, body.Name, body.Version, string(body.Manifest), claims.UserID))
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create market item failed")
		return
	}
	Created(w, created)
}

// GetItem GET /v1/ent/market/items/{id}
func (h *MarketHandler) GetItem(w http.ResponseWriter, r *http.Request) {
	if claims := auth.GetClaims(r.Context()); claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	id := r.PathValue("id")
	if _, err := uuid.Parse(id); err != nil {
		BadRequest(w, "invalid item id")
		return
	}
	pool := db.ReadPool()
	if pool == nil {
		ServiceUnavailable(w, ErrDBUnavailable)
		return
	}
	it, err := scanMarketItem(pool.QueryRow(r.Context(),
		`SELECT `+catalogItemColumns+` FROM ent_catalog_items WHERE id = $1`, id))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			NotFound(w, ErrNotFound)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "get market item failed")
		return
	}
	OK(w, it)
}

// UpdateItem PUT /v1/ent/market/items/{id}（仅 name/version/manifest；
// 状态流转只经 publish/retire 端点）。
func (h *MarketHandler) UpdateItem(w http.ResponseWriter, r *http.Request) {
	if claims := auth.GetClaims(r.Context()); claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	id := r.PathValue("id")
	if _, err := uuid.Parse(id); err != nil {
		BadRequest(w, "invalid item id")
		return
	}
	var body struct {
		Name     *string          `json:"name"`
		Version  *string          `json:"version"`
		Manifest *json.RawMessage `json:"manifest"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.Name == nil && body.Version == nil && body.Manifest == nil {
		BadRequest(w, "nothing to update")
		return
	}
	if body.Name != nil && !validMarketItemName.MatchString(*body.Name) {
		BadRequest(w, "invalid item name")
		return
	}
	if body.Manifest != nil && !json.Valid(*body.Manifest) {
		BadRequest(w, "manifest must be valid JSON")
		return
	}
	// jsonb 列参数以 string 传递（[]byte 会被 pgx 编码为 bytea）；nil → NULL
	var manifest any
	if body.Manifest != nil {
		manifest = string(*body.Manifest)
	}
	if db.Pool == nil {
		ServiceUnavailable(w, ErrDBUnavailable)
		return
	}
	updated, err := scanMarketItem(db.GlobalDBManager.QueryRow(r.Context(),
		`UPDATE ent_catalog_items SET
		     name = COALESCE($2, name),
		     version = COALESCE($3, version),
		     manifest = COALESCE($4, manifest),
		     updated_at = NOW()
		 WHERE id = $1 RETURNING `+catalogItemColumns,
		id, body.Name, body.Version, manifest))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			NotFound(w, ErrNotFound)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "update market item failed")
		return
	}
	OK(w, updated)
}

// DeleteItem DELETE /v1/ent/market/items/{id}（级联删除安装记录）。
func (h *MarketHandler) DeleteItem(w http.ResponseWriter, r *http.Request) {
	if claims := auth.GetClaims(r.Context()); claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	id := r.PathValue("id")
	if _, err := uuid.Parse(id); err != nil {
		BadRequest(w, "invalid item id")
		return
	}
	if db.Pool == nil {
		ServiceUnavailable(w, ErrDBUnavailable)
		return
	}
	tag, err := db.GlobalDBManager.Exec(r.Context(), `DELETE FROM ent_catalog_items WHERE id = $1`, id)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "delete market item failed")
		return
	}
	if tag.RowsAffected() == 0 {
		NotFound(w, ErrNotFound)
		return
	}
	NoContent(w)
}

// transitionItem 读取当前状态并校验状态机迁移，通过后原子 UPDATE。
func (h *MarketHandler) transitionItem(w http.ResponseWriter, r *http.Request, to string) {
	if claims := auth.GetClaims(r.Context()); claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	id := r.PathValue("id")
	if _, err := uuid.Parse(id); err != nil {
		BadRequest(w, "invalid item id")
		return
	}
	if db.Pool == nil {
		ServiceUnavailable(w, ErrDBUnavailable)
		return
	}
	var from string
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT status FROM ent_catalog_items WHERE id = $1`, id).Scan(&from); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			NotFound(w, ErrNotFound)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "transition market item failed")
		return
	}
	if !canCatalogTransition(from, to) {
		JSON(w, http.StatusConflict, APIResponse{
			Success: false,
			Error:   "invalid status transition: " + from + " -> " + to,
		})
		return
	}
	updated, err := scanMarketItem(db.GlobalDBManager.QueryRow(r.Context(),
		`UPDATE ent_catalog_items SET status = $2, updated_at = NOW()
		 WHERE id = $1 AND status = $3 RETURNING `+catalogItemColumns, id, to, from))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			// 并发下状态已被其他请求变更
			JSON(w, http.StatusConflict, APIResponse{
				Success: false,
				Error:   "invalid status transition (concurrent update)",
			})
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "transition market item failed")
		return
	}
	OK(w, updated)
}

// PublishItem POST /v1/ent/market/items/{id}/publish（draft → published）。
func (h *MarketHandler) PublishItem(w http.ResponseWriter, r *http.Request) {
	h.transitionItem(w, r, "published")
}

// RetireItem POST /v1/ent/market/items/{id}/retire（published → retired）。
func (h *MarketHandler) RetireItem(w http.ResponseWriter, r *http.Request) {
	h.transitionItem(w, r, "retired")
}

// ── 租户授权（安装记录） ─────────────────────────────────────────────────

func scanMarketGrant(row interface{ Scan(...any) error }) (*MarketGrant, error) {
	var g MarketGrant
	if err := row.Scan(&g.ItemID, &g.TenantID, &g.Enabled, &g.InstalledAt); err != nil {
		return nil, err
	}
	return &g, nil
}

// ListGrants GET /v1/ent/market/grants?item_id=&tenant_id=
func (h *MarketHandler) ListGrants(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	// 跨租户读防护：非 owner 强制锚定自身租户（ent_catalog_installs.tenant_id 是 smallint）。
	// 此前是裸 `WHERE TRUE` + 可选的 query 过滤 ⇒ 任一持有 market:manage 的租户管理员
	// 都能列出**所有租户**的能力授权。
	scoped := scopedTenantID(claims, r.URL.Query().Get("tenant_id"))
	query := `SELECT item_id, tenant_id, enabled, installed_at FROM ent_catalog_installs WHERE TRUE`
	args := []any{}
	if itemID := r.URL.Query().Get("item_id"); itemID != "" {
		args = append(args, itemID)
		query += ` AND item_id = $` + string(rune('0'+len(args)))
	}
	if scoped != "" {
		args = append(args, scoped)
		query += ` AND tenant_id = $` + string(rune('0'+len(args)))
	}
	query += ` ORDER BY installed_at DESC LIMIT 500`

	pool := db.ReadPool()
	if pool == nil {
		ServiceUnavailable(w, ErrDBUnavailable)
		return
	}
	rows, err := pool.Query(r.Context(), query, args...)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list market grants failed")
		return
	}
	defer rows.Close()
	grants := []MarketGrant{}
	for rows.Next() {
		g, err := scanMarketGrant(rows)
		if err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "list market grants failed")
			return
		}
		grants = append(grants, *g)
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list market grants failed")
		return
	}
	OK(w, grants)
}

// GrantItem POST /v1/ent/market/grants（UPSERT：item_id + tenant_id 主键）。
func (h *MarketHandler) GrantItem(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	var body struct {
		ItemID   string `json:"item_id"`
		TenantID string `json:"tenant_id"`
		Enabled  *bool  `json:"enabled"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if _, err := uuid.Parse(body.ItemID); err != nil {
		BadRequest(w, "valid item_id (uuid) required")
		return
	}
	// 跨租户写防护：tenant_id 列是 smallint（ent_catalog_installs，见 0001 baseline），
	// 此前按 uuid 校验 ⇒ 合法请求一律 400，且完全没有归属锚定。现按 smallint 语义校验，
	// 并用 scopedTenantID 锚定 —— 非 owner 只能给自己租户授权。
	body.TenantID = scopedTenantID(claims, body.TenantID)
	if parseTenantID(body.TenantID) == 0 {
		BadRequest(w, "valid tenant_id required")
		return
	}
	enabled := true
	if body.Enabled != nil {
		enabled = *body.Enabled
	}
	if db.Pool == nil {
		ServiceUnavailable(w, ErrDBUnavailable)
		return
	}
	// 条目必须存在（外键兜底，提前给出友好 404）
	var exists bool
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT EXISTS(SELECT 1 FROM ent_catalog_items WHERE id = $1)`, body.ItemID).Scan(&exists); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "grant market item failed")
		return
	}
	if !exists {
		NotFound(w, ErrNotFound)
		return
	}
	g, err := scanMarketGrant(db.GlobalDBManager.QueryRow(r.Context(),
		`INSERT INTO ent_catalog_installs (item_id, tenant_id, enabled)
		 VALUES ($1, $2, $3)
		 ON CONFLICT (item_id, tenant_id) DO UPDATE SET enabled = EXCLUDED.enabled
		 RETURNING item_id, tenant_id, enabled, installed_at`,
		body.ItemID, body.TenantID, enabled))
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "grant market item failed")
		return
	}
	Created(w, g)
}

// UpdateGrant PUT /v1/ent/market/grants/{itemID}/{tenantID}（仅切换 enabled）。
func (h *MarketHandler) UpdateGrant(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	itemID := r.PathValue("itemID")
	tenantID := r.PathValue("tenantID")
	if _, err := uuid.Parse(itemID); err != nil || parseTenantID(tenantID) == 0 {
		BadRequest(w, "invalid item or tenant id")
		return
	}
	// 跨租户写防护：非 owner 只能改自己租户的授权。
	if scopedTenantID(claims, tenantID) != tenantID {
		Forbidden(w, "access denied")
		return
	}
	var body struct {
		Enabled bool `json:"enabled"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if db.Pool == nil {
		ServiceUnavailable(w, ErrDBUnavailable)
		return
	}
	g, err := scanMarketGrant(db.GlobalDBManager.QueryRow(r.Context(),
		`UPDATE ent_catalog_installs SET enabled = $3
		 WHERE item_id = $1 AND tenant_id = $2
		 RETURNING item_id, tenant_id, enabled, installed_at`, itemID, tenantID, body.Enabled))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			NotFound(w, ErrNotFound)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "update market grant failed")
		return
	}
	OK(w, g)
}

// DeleteGrant DELETE /v1/ent/market/grants/{itemID}/{tenantID}
func (h *MarketHandler) DeleteGrant(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	itemID := r.PathValue("itemID")
	tenantID := r.PathValue("tenantID")
	if _, err := uuid.Parse(itemID); err != nil || parseTenantID(tenantID) == 0 {
		BadRequest(w, "invalid item or tenant id")
		return
	}
	// 跨租户写防护：非 owner 只能删自己租户的授权。
	if scopedTenantID(claims, tenantID) != tenantID {
		Forbidden(w, "access denied")
		return
	}
	if db.Pool == nil {
		ServiceUnavailable(w, ErrDBUnavailable)
		return
	}
	tag, err := db.GlobalDBManager.Exec(r.Context(),
		`DELETE FROM ent_catalog_installs WHERE item_id = $1 AND tenant_id = $2`, itemID, tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "delete market grant failed")
		return
	}
	if tag.RowsAffected() == 0 {
		NotFound(w, ErrNotFound)
		return
	}
	NoContent(w)
}
