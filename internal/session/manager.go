package session

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/id"
	"github.com/athenavi/chiron/internal/model"
	"github.com/jackc/pgx/v5/pgxpool"
)

// redisKeyPrefix 统一前缀(N2):由 db.RedisKey 注入 REDIS_KEY_PREFIX。
var redisKeyPrefix = db.RedisKey("session:")

const (
	redisTTL = 2 * time.Hour
)

// ErrSessionNotFound 表示会话不存在（SSE 端点据此放行尚未创建的新会话连接）。
var ErrSessionNotFound = errors.New("session not found")

// ErrSessionForbidden 表示目标会话存在但**不属于当前用户**（写入被拒绝）。
// 与 ErrSessionNotFound 区分：后者是"新会话"（允许顺带创建），前者是越权尝试。
var ErrSessionForbidden = errors.New("session belongs to another user")

// Manager provides session CRUD with Redis hot cache + PostgreSQL persistence.
// All methods degrade gracefully when Redis or PG is unavailable.
type Manager struct {
	pool *pgxpool.Pool
	rdb  db.RedisClient
}

func NewManager(pool *pgxpool.Pool, rdb db.RedisClient) *Manager {
	return &Manager{pool: pool, rdb: rdb}
}

// ── Cache helpers ─────────────────────────────────────────────────────────

// sessionCacheEntry 缓存条目：带版本（updated_at 纳秒）以便写入时比较。
// 目的：并发交错下「读路径回填的旧快照」不应覆盖新值（000.md 第 13 条）。
type sessionCacheEntry struct {
	V int64          `json:"v"`
	D *model.Session `json:"d"`
}

// sessionCacheSetLua 带版本比较的写入：缓存缺失、或新值版本不早于缓存值时写入。
// 返回 1=已写入，0=拒绝（缓存中已有更新的版本）。旧格式条目（无 v 字段）视为可覆盖。
const sessionCacheSetLua = `
local cur = redis.call('GET', KEYS[1])
if cur then
  local ok, old = pcall(cjson.decode, cur)
  if ok and old['v'] then
    local oldv = tonumber(old['v'])
    local newv = tonumber(ARGV[2])
    if oldv and newv and newv < oldv then
      return 0
    end
  end
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
return 1
`

func (m *Manager) cacheSession(ctx context.Context, s *model.Session) {
	if m.rdb == nil {
		return
	}
	payload, err := json.Marshal(sessionCacheEntry{V: s.UpdatedAt.UnixNano(), D: s})
	if err != nil {
		return
	}
	res, err := m.rdb.Eval(ctx, sessionCacheSetLua,
		[]string{redisKeyPrefix + s.ID},
		payload,
		strconv.FormatInt(s.UpdatedAt.UnixNano(), 10),
		int(redisTTL.Seconds()),
	).Result()
	if err != nil {
		slog.Warn("session cache set", "error", err)
		return
	}
	if n, ok := res.(int64); ok && n == 0 {
		slog.Debug("session cache set skipped (cached version is newer)", "session", s.ID)
	}
}

func (m *Manager) evictCache(ctx context.Context, id string) {
	if m.rdb == nil {
		return
	}
	m.rdb.Del(ctx, redisKeyPrefix+id)
}

// ── Helpers ───────────────────────────────────────────────────────────────

func genID() (string, error) {
	return id.UUID()
}

func truncateTitle(s string) string {
	s = strings.TrimSpace(s)
	idx := strings.Index(s, "\n")
	if idx >= 0 {
		s = s[:idx]
	}
	if utf8.RuneCountInString(s) > 120 {
		runes := []rune(s)
		s = string(runes[:120])
	}
	if s == "" {
		s = "New Chat"
	}
	return s
}

func nullableStr(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}
