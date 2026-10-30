package notify

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5/pgconn"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/id"
)

// PGStore 是 Store 的 PostgreSQL 实现（表见 migrations/versions/0011_notifications.py）。
type PGStore struct{}

// NewPGStore 构造 PG 实现。
func NewPGStore() *PGStore { return &PGStore{} }

// isUniqueViolation 判定 SQLSTATE 23505。
//
// 去重的**唯一**依据：`ux_notifications_user_dedupe` 是 (user_id, dedupe_key)
// 唯一索引，重复事件插入必然撞它。刻意不用 "SELECT ... 再 INSERT" ——
// 并发下两个请求都能查到"没有"，于是各插一条，用户收到重复推送。
func isUniqueViolation(err error) bool {
	var pe *pgconn.PgError
	return errors.As(err, &pe) && pe.Code == "23505"
}

const notificationColumns = `id, tenant_id, user_id, kind, dedupe_key, title, body,
	link, COALESCE(payload, '{}'::jsonb), read_at, created_at`

// Insert 幂等写入一条通知。
//
// 撞唯一索引时返回 (false, nil)：这是"已推送过"的正常结果，不是错误。
//
// **tenant_id 由 users 表派生，不用调用方传入的值**：它本来就是"用户当时属于
// 哪个租户"的快照，不是通知自身的属性。让调用方传会引入不一致风险 —— 传错
// 就把通知挂到别人的租户下（跨租户可见），而派生天然正确，也省掉每条通知前
// 先查一次 users 的往返。用户不存在时插入 0 行，返回 (false, nil)。
func (s *PGStore) Insert(ctx context.Context, n *Notification) (bool, error) {
	if n.ID == "" {
		newID, err := id.UUID()
		if err != nil {
			return false, fmt.Errorf("notify: generate id: %w", err)
		}
		n.ID = newID
	}
	payload := "{}"
	if len(n.Payload) > 0 {
		raw, err := json.Marshal(n.Payload)
		if err != nil {
			return false, fmt.Errorf("notify: marshal payload: %w", err)
		}
		payload = string(raw)
	}

	tag, err := db.GlobalDBManager.Exec(ctx,
		`INSERT INTO notifications
			(id, tenant_id, user_id, kind, dedupe_key, title, body, link, payload)
		 SELECT $1, u.tenant_id, u.id, $2, $3, $4, $5, $6, $7::jsonb
		   FROM users u WHERE u.id = $8
		 ON CONFLICT (user_id, dedupe_key) DO NOTHING`,
		n.ID, n.Kind, n.DedupeKey,
		n.Title, n.Body, n.Link, payload, n.UserID)
	if err != nil {
		// 不用 ON CONFLICT 的隐式跳过作为唯一手段：ON CONFLICT 要求冲突目标
		// 有唯一索引，而 23505 仍可能来自别的约束（如外键缺失导致的其它错误不算）。
		// 这里保留显式判定，让"真的是重复"与"其它写失败"可区分。
		if isUniqueViolation(err) {
			return false, nil
		}
		return false, fmt.Errorf("notify: insert: %w", err)
	}
	return tag.RowsAffected() > 0, nil
}

// List 按时间倒序返回通知。
//
// limit 由调用方夹逼（handler 负责上限），存储层只做 min(1) 防呆。
func (s *PGStore) List(ctx context.Context, userID string, limit int, unreadOnly bool) ([]Notification, error) {
	if limit <= 0 {
		limit = 50
	}
	q := `SELECT ` + notificationColumns + ` FROM notifications WHERE user_id = $1`
	if unreadOnly {
		q += ` AND read_at IS NULL`
	}
	q += ` ORDER BY created_at DESC, id DESC LIMIT $2`

	rows, err := db.GlobalDBManager.Query(ctx, q, userID, limit)
	if err != nil {
		return nil, fmt.Errorf("notify: list: %w", err)
	}
	defer rows.Close()

	out := make([]Notification, 0, limit)
	for rows.Next() {
		var n Notification
		var payload []byte
		if err := rows.Scan(&n.ID, &n.TenantID, &n.UserID, &n.Kind, &n.DedupeKey,
			&n.Title, &n.Body, &n.Link, &payload, &n.ReadAt, &n.CreatedAt); err != nil {
			return nil, fmt.Errorf("notify: scan: %w", err)
		}
		if len(payload) > 0 {
			// 解析失败不影响整条通知可用性：payload 只是附加细节。
			_ = json.Unmarshal(payload, &n.Payload)
		}
		out = append(out, n)
	}
	return out, rows.Err()
}

// UnreadCount 返回未读数，供前端角标使用。
func (s *PGStore) UnreadCount(ctx context.Context, userID string) (int, error) {
	var n int
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT count(*) FROM notifications WHERE user_id = $1 AND read_at IS NULL`,
		userID).Scan(&n)
	if err != nil {
		return 0, fmt.Errorf("notify: unread count: %w", err)
	}
	return n, nil
}

// MarkRead 把指定 id 标为已读。
//
// 条件里带 user_id：不能让 A 通过猜 id 把 B 的通知标成已读。
// 条件里带 `read_at IS NULL`：重复调用不重复计数，返回的是"这次真正改变的行数"。
func (s *PGStore) MarkRead(ctx context.Context, userID string, ids []string) (int, error) {
	if len(ids) == 0 {
		return 0, nil
	}
	tag, err := db.GlobalDBManager.Exec(ctx,
		`UPDATE notifications SET read_at = now()
		 WHERE user_id = $1 AND id = ANY($2) AND read_at IS NULL`,
		userID, ids)
	if err != nil {
		return 0, fmt.Errorf("notify: mark read: %w", err)
	}
	return int(tag.RowsAffected()), nil
}

// MarkAllRead 把该用户全部未读标为已读。
func (s *PGStore) MarkAllRead(ctx context.Context, userID string) (int, error) {
	tag, err := db.GlobalDBManager.Exec(ctx,
		`UPDATE notifications SET read_at = now() WHERE user_id = $1 AND read_at IS NULL`,
		userID)
	if err != nil {
		return 0, fmt.Errorf("notify: mark all read: %w", err)
	}
	return int(tag.RowsAffected()), nil
}

// EmailEnabled 返回该用户对该类事件的邮件开关，**缺行即默认开启**。
//
// 默认值放在读路径的 COALESCE 上，而不是给每个用户 × 每种事件预置一行：
// 后者会让新增事件类型时必须补数据迁移，且存量用户全部漏发。
func (s *PGStore) EmailEnabled(ctx context.Context, userID, kind string) (bool, error) {
	var enabled bool
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COALESCE(
			(SELECT email_enabled FROM user_notification_prefs
			  WHERE user_id = $1 AND kind = $2), TRUE)`,
		userID, kind).Scan(&enabled)
	if err != nil {
		return false, fmt.Errorf("notify: read email pref: %w", err)
	}
	return enabled, nil
}

// SetEmailEnabled 写入/更新开关（upsert 靠主键 (user_id, kind)）。
func (s *PGStore) SetEmailEnabled(ctx context.Context, userID, kind string, enabled bool) error {
	_, err := db.GlobalDBManager.Exec(ctx,
		`INSERT INTO user_notification_prefs (user_id, kind, email_enabled, updated_at)
		 VALUES ($1, $2, $3, now())
		 ON CONFLICT (user_id, kind)
		 DO UPDATE SET email_enabled = EXCLUDED.email_enabled, updated_at = now()`,
		userID, kind, enabled)
	if err != nil {
		return fmt.Errorf("notify: write email pref: %w", err)
	}
	return nil
}

// ListPrefs 返回该用户**显式设置过**的开关。
// 未出现的 kind 由 Manager.Prefs 补默认开启 —— 存储层只反映"设置过什么"。
func (s *PGStore) ListPrefs(ctx context.Context, userID string) (map[string]bool, error) {
	rows, err := db.GlobalDBManager.Query(ctx,
		`SELECT kind, email_enabled FROM user_notification_prefs WHERE user_id = $1`, userID)
	if err != nil {
		return nil, fmt.Errorf("notify: list prefs: %w", err)
	}
	defer rows.Close()

	out := make(map[string]bool)
	for rows.Next() {
		var kind string
		var enabled bool
		if err := rows.Scan(&kind, &enabled); err != nil {
			return nil, fmt.Errorf("notify: scan pref: %w", err)
		}
		out[kind] = enabled
	}
	return out, rows.Err()
}

// UserLocale 返回该用户的界面语言（`users.settings->>'locale'`）。
// 复用 `users.settings` 而不是新加一列：UpdateProfile 已有
// `settings = COALESCE(settings::jsonb,'{}') || $n::jsonb` 的局部合并写入路径
// （internal/api/auth.go），前端切语言时 PUT 一个键即可，不需要新表新列新接口。
//
// 列是 `json`（不是 jsonb），`->>` 需要先转型；用 `::jsonb` 显式转换，
// 否则在某些 PG 版本上 json 与 jsonb 的操作符不通用。
//
// 未设置返回空串（不是 zh-CN）：把"没设置过"与"明确选了中文"区分开，
// 调用方才有机会回落到浏览器语言。
func (s *PGStore) UserLocale(ctx context.Context, userID string) (string, error) {
	var loc string
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COALESCE(settings::jsonb ->> 'locale', '') FROM users WHERE id = $1`,
		userID).Scan(&loc)
	if err != nil {
		return "", fmt.Errorf("notify: read user locale: %w", err)
	}
	return loc, nil
}

// DeleteOlderThan 分批删除 created_at < cutoff 的通知，返回本批删除行数。
//
// PG 的 DELETE 不支持直接写 LIMIT，所以用「子查询选出本批 id → 按 id 删」两步
// 合在一条语句里。子查询按 created_at 排序并 LIMIT，能直接用上
// `idx_notifications_created`（0012 迁移专门为它建的）。
//
// 每批是一个独立语句（各自独立的隐式事务），不是把整轮清理包在一个长事务里：
// 后者会长时间持有大量行锁，把并发的支付回调 / 配额变更写入堵在后面。
func (s *PGStore) DeleteOlderThan(ctx context.Context, cutoff time.Time, batchSize int) (int, error) {
	if batchSize <= 0 {
		// 防呆：batchSize = 0 会被 SQL 当成 LIMIT 0 而静默什么都不删，
		// 调用方会误以为"已经删空"。给一个安全的小批量。
		batchSize = 1000
	}
	tag, err := db.GlobalDBManager.Exec(ctx,
		`DELETE FROM notifications
		  WHERE id IN (
		        SELECT id FROM notifications
		         WHERE created_at < $1
		         ORDER BY created_at
		         LIMIT $2
		  )`,
		cutoff, batchSize)
	if err != nil {
		return 0, fmt.Errorf("notify: delete old notifications: %w", err)
	}
	return int(tag.RowsAffected()), nil
}
