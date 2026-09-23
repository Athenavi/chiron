package api

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/monitor"
	"github.com/athenavi/chiron/internal/settings"
	"github.com/athenavi/chiron/internal/storage"
)

var validDBName = regexp.MustCompile(`^[a-zA-Z_][a-zA-Z0-9_]*$`)

// Version 是构建时注入的版本号，通过 -ldflags 注入
var Version = "dev"

// AdminHandler provides admin-only management endpoints.
type AdminHandler struct {
	cfg           *config.Config
	authenticator *auth.Authenticator
	store         *storage.AtomicStore
	redis         *db.AtomicRedis
	pythonClient  *engine.PythonClient
	rateLimiter   *DistributedRateLimiter
	appSecret     string
	settingsStore *settings.Store
	// billingHandler 供后台「支付配置」读取生效快照并触发热重载。
	// 与 rateLimiter 同样采用构造后赋值（见 gateway_router.go）。
	billingHandler *BillingHandler
}

func NewAdminHandler(cfg *config.Config, a *auth.Authenticator, store *storage.AtomicStore, redis *db.AtomicRedis, pythonClient *engine.PythonClient) *AdminHandler {
	return &AdminHandler{cfg: cfg, authenticator: a, store: store, redis: redis, pythonClient: pythonClient}
}

// RegisterRoutes adds admin endpoints to the given router under /v1/admin.
// Caller is responsible for auth middleware.
func (h *AdminHandler) RegisterRoutes(r *http.ServeMux) {
	// 原有端点
	r.HandleFunc("GET /metrics", h.Metrics)
	r.HandleFunc("GET /users", h.ListUsers)
	r.HandleFunc("GET /users/{id}", h.GetUser)
	r.HandleFunc("PUT /users/{id}", h.UpdateUser)
	r.HandleFunc("DELETE /users/{id}", h.DeleteUser)
	r.HandleFunc("GET /system", h.SystemInfo)
	r.HandleFunc("POST /maintenance", h.TriggerMaintenance)
	r.HandleFunc("POST /backup", h.CreateBackup)
	r.HandleFunc("POST /restore", h.RestoreBackup)
	r.HandleFunc("GET /storage", h.GetStorage)
	r.HandleFunc("PUT /storage", h.UpdateStorage)
	r.HandleFunc("POST /storage/test", h.TestStorage)
	r.HandleFunc("GET /redis", h.GetRedis)
	r.HandleFunc("PUT /redis", h.UpdateRedis)
	r.HandleFunc("POST /redis/test", h.TestRedis)

	// 新增端点：队列管理
	r.HandleFunc("GET /queue", h.GetQueueStats)
	r.HandleFunc("POST /queue/flush", h.FlushQueue)
	r.HandleFunc("POST /queue/pause", h.PauseQueue)

	// 新增端点：缓存监控
	r.HandleFunc("GET /cache/stats", h.GetCacheStats)

	// 新增端点：性能监控
	r.HandleFunc("GET /performance", h.GetPerformance)

	// 新增端点：API Key 管理
	r.HandleFunc("GET /api-keys", h.ListApiKeys)
	// 运维类端点（租户/域名/数据库/Redis/模型/定时任务）—— /admin 全栈实装
	h.registerOpsRoutes(r)
	r.HandleFunc("POST /api-keys", h.AddApiKey)
	r.HandleFunc("PUT /api-keys/{id}", h.UpdateApiKey)
	r.HandleFunc("DELETE /api-keys/{id}", h.DeleteApiKey)

	// 新增端点：系统设置
	r.HandleFunc("PUT /settings", h.SaveSettings)
	r.HandleFunc("GET /settings", h.GetSettings)

	// 支付渠道配置（后台「支付配置」页面）：
	// 读为生效快照（DB 覆盖 env）、写为校验后落库 + 热重载渠道客户端。
	r.HandleFunc("GET /payments", h.GetPaymentConfig)
	r.HandleFunc("PUT /payments", h.SavePaymentConfig)
}

func (h *AdminHandler) Metrics(w http.ResponseWriter, r *http.Request) {
	snap := monitor.Snapshot()
	// Map internal metric names to dashboard-expected field names
	if v, ok := snap["requests_active"]; ok {
		snap["concurrent_connections"] = v
	}
	// queue_backlog：真实的事件队列积压（Redis Stream webhook:events）。
	// 此前误赋为 requests_total（累计请求数、单调递增），使看板把"累计请求数"
	// 显示成"队列积压"并恒定越过告警阈值。
	snap["queue_backlog"] = queueBacklog(r.Context())
	// cache_hit_rate：引擎侧多级缓存总命中率（%）。此前该字段从未返回，前端恒为 0。
	snap["cache_hit_rate"] = h.cacheHitRate(r.Context())
	// api_latency_p99：请求耗时的 P99（毫秒），采样自 MonitoringMiddleware。
	// 此前该字段从未返回（RequestHistogram 无写入点）。
	snap["api_latency_p99"] = requestLatencyP99Ms()
	OK(w, snap)
}

type AdminUser struct {
	ID        string `json:"id"`
	Email     string `json:"email"`
	Name      string `json:"name"`
	Role      string `json:"role"`
	CreatedAt string `json:"created_at"`
	UpdatedAt string `json:"updated_at"`
}

func (h *AdminHandler) ListUsers(w http.ResponseWriter, r *http.Request) {
	tenantID := GetTenantID(r)
	if tenantID == "" {
		Unauthorized(w, "missing tenant context")
		return
	}

	// 分页参数
	page := 1
	perPage := 20
	if p := r.URL.Query().Get("page"); p != "" {
		if v, err := strconv.Atoi(p); err == nil && v >= 1 {
			page = v
		}
	}
	if pp := r.URL.Query().Get("per_page"); pp != "" {
		if v, err := strconv.Atoi(pp); err == nil && v >= 1 && v <= 100 {
			perPage = v
		}
	}
	offset := (page - 1) * perPage

	// 查询总数
	var total int
	err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT COUNT(*) FROM users WHERE tenant_id = $1`, tenantID).Scan(&total)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "count users failed")
		return
	}

	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT id, email, name, role, created_at, updated_at
		 FROM users WHERE tenant_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3`,
		tenantID, perPage, offset)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "query users failed")
		return
	}
	defer rows.Close()

	users := make([]AdminUser, 0)
	for rows.Next() {
		var u AdminUser
		var createdAt, updatedAt time.Time
		if err := rows.Scan(&u.ID, &u.Email, &u.Name, &u.Role, &createdAt, &updatedAt); err != nil {
			continue
		}
		u.CreatedAt = createdAt.Format(time.RFC3339)
		u.UpdatedAt = updatedAt.Format(time.RFC3339)
		users = append(users, u)
	}
	if err := rows.Err(); err != nil {
		InternalError(w, "failed to iterate users")
		return
	}

	OK(w, map[string]interface{}{
		"users":    users,
		"total":    total,
		"page":     page,
		"per_page": perPage,
	})
}

func (h *AdminHandler) GetUser(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "" {
		BadRequest(w, "id is required")
		return
	}
	tenantID := GetTenantID(r)
	if tenantID == "" {
		Unauthorized(w, "missing tenant context")
		return
	}

	var u AdminUser
	var createdAt, updatedAt time.Time
	err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT id, email, name, role, created_at, updated_at
		 FROM users WHERE id = $1 AND tenant_id = $2`, id, tenantID).
		Scan(&u.ID, &u.Email, &u.Name, &u.Role, &createdAt, &updatedAt)
	if err != nil {
		NotFound(w, "user not found")
		return
	}
	u.CreatedAt = createdAt.Format(time.RFC3339)
	u.UpdatedAt = updatedAt.Format(time.RFC3339)

	OK(w, u)
}

func (h *AdminHandler) UpdateUser(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "" {
		BadRequest(w, "id is required")
		return
	}
	tenantID := GetTenantID(r)
	if tenantID == "" {
		Unauthorized(w, "missing tenant context")
		return
	}

	var body struct {
		Email string `json:"email"`
		Name  string `json:"name"`
		Role  string `json:"role"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}

	// Validate role
	if body.Role != "" && body.Role != "owner" && body.Role != "admin" && body.Role != "user" {
		BadRequest(w, "invalid role: must be owner, admin, or user")
		return
	}
	claims := auth.GetClaims(r.Context())
	if body.Role == "owner" && (claims == nil || claims.Role != "owner") {
		BadRequest(w, "only owner can assign owner role")
		return
	}

	// Build dynamic UPDATE with column name whitelist —— tenant_id 作为额外 WHERE 条件防越权
	// S 安全修复：列名必须来自白名单，防止 SQL 注入
	userColumnMap := map[string]string{
		"email": "email",
		"name":  "name",
		"role":  "role",
	}
	setClauses := ""
	args := []interface{}{}
	argIdx := 1

	fieldValues := []struct {
		field string
		value string
	}{
		{"email", body.Email},
		{"name", body.Name},
		{"role", body.Role},
	}
	for _, fv := range fieldValues {
		if fv.value != "" {
			col, ok := userColumnMap[fv.field]
			if !ok {
				continue
			}
			setClauses += fmt.Sprintf("%s = $%d, ", col, argIdx)
			args = append(args, fv.value)
			argIdx++
		}
	}

	if setClauses == "" {
		BadRequest(w, "no fields to update")
		return
	}

	setClauses += fmt.Sprintf("updated_at = NOW()")
	args = append(args, id, tenantID)

	query := fmt.Sprintf("UPDATE users SET %s WHERE id = $%d AND tenant_id = $%d", setClauses, argIdx, argIdx+1)
	result, err := db.GlobalDBManager.Exec(r.Context(), query, args...)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "update user failed")
		return
	}
	if result.RowsAffected() == 0 {
		NotFound(w, "user not found")
		return
	}

	OK(w, map[string]string{"status": "updated"})
}

func (h *AdminHandler) DeleteUser(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "" {
		BadRequest(w, "id is required")
		return
	}
	tenantID := GetTenantID(r)
	if tenantID == "" {
		Unauthorized(w, "missing tenant context")
		return
	}

	// Prevent deleting yourself
	claims := auth.GetClaims(r.Context())
	if claims != nil && claims.UserID == id {
		BadRequest(w, "cannot delete your own account")
		return
	}

	_, err := db.GlobalDBManager.Exec(r.Context(),
		`DELETE FROM users WHERE id = $1 AND tenant_id = $2`, id, tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "delete user failed")
		return
	}

	OK(w, map[string]string{"status": "deleted"})
}

func (h *AdminHandler) SystemInfo(w http.ResponseWriter, r *http.Request) {
	info := map[string]interface{}{
		"version": Version,
		"uptime":  time.Since(monitor.Global.StartTime).String(),
		"db": map[string]interface{}{
			"postgres": true,
			"redis":    db.Redis != nil,
		},
	}
	OK(w, info)
}

func (h *AdminHandler) TriggerMaintenance(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Action string `json:"action"` // vacuum | reindex | analyze | flush_cache
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, "action is required (vacuum, reindex, analyze, flush_cache)")
		return
	}

	switch body.Action {
	case "vacuum":
		if _, err := db.GlobalDBManager.Exec(r.Context(), "VACUUM ANALYZE"); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "vacuum failed")
			return
		}
	case "reindex":
		dbName := dbNameFromDSN()
		if !validDBName.MatchString(dbName) {
			InternalError(w, "invalid database name")
			return
		}
		if _, err := db.GlobalDBManager.Exec(r.Context(), "REINDEX DATABASE "+dbName); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "reindex failed")
			return
		}
	case "analyze":
		if _, err := db.GlobalDBManager.Exec(r.Context(), "ANALYZE"); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "analyze failed")
			return
		}
	case "flush_cache":
		if db.Redis != nil {
			const prefix = "chiron_cache:*"
			// 跨节点扫描：Cluster 下 SCAN 只覆盖单个节点，直接用 Lua/Scan 会漏清其它 master 的缓存。
			keys, err := db.Redis.ScanAll(r.Context(), prefix, 500)
			if err != nil {
				logAndRespond(w, err, http.StatusInternalServerError, "flush_cache failed")
				return
			}
			// 分批 UNLINK（非阻塞释放，避免 DEL 大 key 时卡住服务端，也避免单命令过大）
			deleted := 0
			const batch = 500
			for i := 0; i < len(keys); i += batch {
				end := i + batch
				if end > len(keys) {
					end = len(keys)
				}
				args := make([]interface{}, 0, end-i+1)
				args = append(args, "UNLINK")
				for _, k := range keys[i:end] {
					args = append(args, k)
				}
				if n, err := db.Redis.Do(r.Context(), args...).Int(); err == nil {
					deleted += n
				}
			}
			slog.Info("cache flushed", "prefix", prefix, "matched", len(keys), "deleted", deleted)
		}
	default:
		BadRequest(w, fmt.Sprintf("unknown action: %s", body.Action))
		return
	}

	OK(w, map[string]string{
		"status": "completed",
		"action": body.Action,
	})
}

// dbNameFromDSN extracts the database name from POSTGRES_DSN environment variable.
func dbNameFromDSN() string {
	dsn := os.Getenv("POSTGRES_DSN")
	if dsn == "" {
		return "chiron" // fallback
	}
	// Parse URL format: postgres://user:pass@host:port/dbname?params
	u, err := url.Parse(dsn)
	if err != nil {
		return "chiron"
	}
	if u.Path != "" && u.Path != "/" {
		// Path is /dbname — trim leading slash
		return u.Path[1:]
	}
	return "chiron"
}

// 鈹€鈹€ Backup & Restore 鈹€鈹€

func (h *AdminHandler) CreateBackup(w http.ResponseWriter, r *http.Request) {
	dsn := extractDSN()
	if dsn == "" {
		InternalError(w, "POSTGRES_DSN not configured")
		return
	}
	host, port, user, dbname, password := parseDSNComponents(dsn)
	cmd := exec.CommandContext(r.Context(), "pg_dump",
		"--host", host,
		"--port", port,
		"--username", user,
		"--dbname", dbname,
	)
	cmd.Env = os.Environ()
	cmd.Env = append(cmd.Env, "PGPASSWORD="+password)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "backup failed")
		return
	}
	if err := cmd.Start(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "backup failed")
		return
	}
	w.Header().Set("Content-Type", "application/sql")
	w.Header().Set("Content-Disposition", fmt.Sprintf("attachment; filename=chiron_backup_%s.sql", time.Now().Format("20060102_150405")))
	if _, err := io.Copy(w, stdout); err != nil {
		slog.Warn("backup stream failed", "error", err)
	}
	if err := cmd.Wait(); err != nil {
		slog.Warn("pg_dump failed", "error", err)
	}
}

func (h *AdminHandler) RestoreBackup(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, 500<<20) // 500MB 上传上限，防止 OOM
	file, _, err := r.FormFile("file")
	if err != nil {
		BadRequest(w, "file is required")
		return
	}
	defer file.Close()

	// P0-P4 防护：流式写入临时文件后用 psql 恢复，避免 OOM
	const maxSize int64 = 512 << 20 // 512MB
	tmpDir := h.cfg.DataDir
	if tmpDir == "" {
		tmpDir = config.GetDefaultDataDir()
	}
	tmpFile, err := os.CreateTemp(tmpDir, "chiron_restore_*.sql")
	if err != nil {
		InternalError(w, "cannot create temp file")
		return
	}
	tmpPath := tmpFile.Name()
	defer os.Remove(tmpPath)

	written, err := io.Copy(tmpFile, io.LimitReader(file, maxSize))
	if err != nil {
		tmpFile.Close()
		logAndRespond(w, err, http.StatusInternalServerError, "write temp file failed")
		return
	}
	tmpFile.Close()
	if written >= maxSize {
		// 已读满限流器，可能有更多数据被截断
		BadRequest(w, "backup file too large (max 512MB)")
		return
	}

	// 使用 psql 执行恢复（密码通过 PGPASSWORD 环境变量传递，避免出现在命令行参数中）
	restoreCtx, cancel := context.WithTimeout(r.Context(), 30*time.Minute)
	defer cancel()
	psqlDSN := extractDSN()
	if psqlDSN == "" {
		InternalError(w, "POSTGRES_DSN not configured")
		return
	}
	host, port, user, dbname, password := parseDSNComponents(psqlDSN)
	cmd := exec.CommandContext(restoreCtx, "psql",
		"--host", host,
		"--port", port,
		"--username", user,
		"--dbname", dbname,
		"-f", tmpPath,
		"-v", "ON_ERROR_STOP=1",
	)
	cmd.Env = os.Environ()
	cmd.Env = append(cmd.Env, "PGPASSWORD="+password)
	output, err := cmd.CombinedOutput()
	if err != nil {
		slog.Error("restore failed", "error", err, "output", string(output))
		logAndRespond(w, err, http.StatusInternalServerError, "restore failed")
		return
	}
	OK(w, map[string]string{"message": "Database restored successfully"})
}

func extractDSN() string {
	return os.Getenv("POSTGRES_DSN")
}

// parseDSNComponents 解析 PostgreSQL DSN 返回 host, port, user, dbname, password
// P0 安全修复：密码用 PGPASSWORD 环境变量，避免出现在命令行参数中
// DSN 格式: postgres://user:pass@host:port/dbname?params
func parseDSNComponents(dsn string) (host, port, user, dbname, password string) {
	host = "localhost"
	port = "5432"
	user = "postgres"
	dbname = "chiron"
	password = ""

	u, err := url.Parse(dsn)
	if err != nil {
		return
	}
	if u.User != nil {
		user = u.User.Username()
		password, _ = u.User.Password()
	}
	if h := u.Hostname(); h != "" {
		host = h
	}
	if p := u.Port(); p != "" {
		port = p
	}
	if u.Path != "" && u.Path != "/" {
		dbname = u.Path[1:]
	}
	return
}

// ─── Storage Management ────────────────────────────────────────────

type StorageConfig struct {
	Backend     string `json:"backend"`
	StorageRoot string `json:"storage_root,omitempty"`
	S3Endpoint  string `json:"s3_endpoint,omitempty"`
	S3Bucket    string `json:"s3_bucket,omitempty"`
	S3UseSSL    bool   `json:"s3_use_ssl"`
}

type StorageUpdateRequest struct {
	Backend     string `json:"backend"`
	S3Endpoint  string `json:"s3_endpoint,omitempty"`
	S3Bucket    string `json:"s3_bucket,omitempty"`
	S3AccessKey string `json:"s3_access_key,omitempty"`
	S3SecretKey string `json:"s3_secret_key,omitempty"`
	S3UseSSL    bool   `json:"s3_use_ssl,omitempty"`
}

func (h *AdminHandler) GetStorage(w http.ResponseWriter, r *http.Request) {
	if h.store == nil {
		OK(w, map[string]interface{}{
			"backend": "none",
			"config":  StorageConfig{},
		})
		return
	}
	OK(w, map[string]interface{}{
		"backend": h.store.Backend(),
		"config":  StorageConfig{},
	})
}

func (h *AdminHandler) UpdateStorage(w http.ResponseWriter, r *http.Request) {
	if h.store == nil {
		InternalError(w, "storage not initialized")
		return
	}

	var body StorageUpdateRequest
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}

	if body.Backend != "local" && body.Backend != "s3" {
		BadRequest(w, "backend must be 'local' or 's3'")
		return
	}

	previous := h.store.Backend()

	var newStore storage.FileStore
	var err error
	switch body.Backend {
	case "local":
		root := config.LoadAllowUnconfigured().StorageRoot
		if ls, ok := h.store.LoadRaw().(*storage.LocalStore); ok {
			root = ls.Root
		}
		newStore, err = storage.NewStore("local", root, "", "", "", "", false)
	case "s3":
		if body.S3Endpoint == "" || body.S3Bucket == "" || body.S3AccessKey == "" || body.S3SecretKey == "" {
			BadRequest(w, "s3_endpoint, s3_bucket, s3_access_key, s3_secret_key are required for S3 backend")
			return
		}
		newStore, err = storage.NewStore("s3", "", body.S3Endpoint, body.S3Bucket, body.S3AccessKey, body.S3SecretKey, body.S3UseSSL)
	}
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "failed to create storage backend")
		return
	}

	h.store.Swap(newStore)

	warning := ""
	if previous != body.Backend {
		if previous == "local" {
			warning = "存储后端已从 local 切换到 s3。旧后端中的文件不会自动迁移。"
		} else {
			warning = "存储后端已从 s3 切换到 local。旧后端中的文件不会自动迁移。"
		}
	}

	OK(w, map[string]interface{}{
		"status":   "switched",
		"warning":  warning,
		"previous": previous,
		"current":  body.Backend,
	})

	slog.Info("storage backend switched", "from", previous, "to", body.Backend)
}

func (h *AdminHandler) TestStorage(w http.ResponseWriter, r *http.Request) {
	if h.store == nil {
		InternalError(w, "storage not initialized")
		return
	}

	var body StorageUpdateRequest
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}

	switch body.Backend {
	case "local":
		OK(w, map[string]interface{}{
			"status":  "ok",
			"message": "本地存储可用",
		})
	case "s3":
		if body.S3Endpoint == "" || body.S3Bucket == "" || body.S3AccessKey == "" || body.S3SecretKey == "" {
			BadRequest(w, "s3_endpoint, s3_bucket, s3_access_key, s3_secret_key are required")
			return
		}
		testStore, err := storage.NewS3Store(body.S3Endpoint, body.S3Bucket, "", body.S3AccessKey, body.S3SecretKey, "", body.S3UseSSL)
		if err != nil {
			OK(w, map[string]interface{}{
				"status":  "error",
				"message": "S3 connection failed",
			})
			return
		}
		ctx := r.Context()
		_, err = testStore.List(ctx, "")
		if err != nil {
			OK(w, map[string]interface{}{
				"status":  "error",
				"message": "S3 bucket access failed",
			})
			return
		}
		OK(w, map[string]interface{}{
			"status":  "ok",
			"message": fmt.Sprintf("S3 连接成功，bucket '%s' 可访问", body.S3Bucket),
		})
	default:
		BadRequest(w, "backend must be 'local' or 's3'")
	}
}

// ─── Redis Management ────────────────────────────────────────────

func (h *AdminHandler) GetRedis(w http.ResponseWriter, r *http.Request) {
	if h.redis == nil {
		OK(w, map[string]interface{}{
			"status": "disconnected",
			"mode":   "none",
		})
		return
	}
	stats := h.redis.Stats()
	OK(w, map[string]interface{}{
		"status": "connected",
		"mode":   h.redis.Mode(),
		"pool": map[string]interface{}{
			"hits":        stats.Hits,
			"misses":      stats.Misses,
			"timeouts":    stats.Timeouts,
			"total_conns": stats.TotalConns,
			"idle_conns":  stats.IdleConns,
			"stale_conns": stats.StaleConns,
		},
	})
}

func (h *AdminHandler) UpdateRedis(w http.ResponseWriter, r *http.Request) {
	if h.redis == nil {
		InternalError(w, "redis not initialized")
		return
	}

	var body db.RedisConfig
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}

	if body.Mode == "" {
		body.Mode = "single"
	}

	switch body.Mode {
	case "single":
		if body.Addr == "" {
			BadRequest(w, "addr is required for single mode")
			return
		}
	case "cluster":
		if len(body.Addrs) == 0 {
			BadRequest(w, "addrs is required for cluster mode")
			return
		}
	case "sentinel":
		if body.MasterName == "" {
			BadRequest(w, "master_name is required for sentinel mode")
			return
		}
		if len(body.SentinelAddrs) == 0 {
			BadRequest(w, "sentinel_addrs is required for sentinel mode")
			return
		}
	default:
		BadRequest(w, "mode must be 'single', 'cluster', or 'sentinel'")
		return
	}

	newClient, err := db.NewRedisClient(body)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "failed to connect to redis")
		return
	}

	oldClient := h.redis.LoadRaw()
	h.redis.Swap(newClient)
	if oldClient != nil {
		oldClient.Close()
	}

	OK(w, map[string]interface{}{
		"status":  "switched",
		"mode":    body.Mode,
		"warning": "Redis connection switched. Cached data from the previous instance is not migrated.",
	})

	slog.Info("redis backend switched", "mode", body.Mode)
}

func (h *AdminHandler) TestRedis(w http.ResponseWriter, r *http.Request) {
	var body db.RedisConfig
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}

	if body.Mode == "" {
		body.Mode = "single"
	}

	newClient, err := db.NewRedisClient(body)
	if err != nil {
		OK(w, map[string]interface{}{
			"status":  "error",
			"message": "Redis connection failed",
		})
		return
	}
	newClient.Close()

	OK(w, map[string]interface{}{
		"status":  "ok",
		"message": fmt.Sprintf("Redis %s connection successful", body.Mode),
	})
}
