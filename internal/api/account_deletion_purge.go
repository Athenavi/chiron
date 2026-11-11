package api

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/storage"
)

// account_deletion_purge.go —— 到期清理：先归档，再清除个人数据。
//
// ── 顺序为什么必须是"先归档、后删除" ──
// 反过来的话，归档失败时数据已经没了 —— 而归档是这次删除**唯一**的留底。
// 因此本实现的每一步都是"归档成功才继续"，归档失败则整条账号跳过、留着下次重试。
//
// ── 为什么 users 行不能删，只能匿名化 ──
// `audit_logs.user_id` 有外键指向 `users`，而 0018 迁移给审计表加了
// BEFORE UPDATE 触发器（审计不可篡改）。这意味着：
//   - 删 users 行 → 外键冲突（要么删审计、要么置 NULL，两者都不可接受）；
//   - 改 audit_logs.user_id → 被触发器拒绝。
// 所以保留 users 行、把它的**身份字段**清空：行还在（审计与财务的外键仍有效），
// 但已经无法对应到任何自然人。这正是"匿名化"而不是"物理删除"的标准做法，
// 也是税务留存与删除权的交集。
//
// ── 哪些数据删、哪些留 ──
// 删（个人内容）：会话、消息、工具调用、知识库、媒体、记忆、通知、API Key、
//                  定时任务、接管、身份绑定、租户成员关系。
// 留（法定留存 + 不可篡改）：payments、credit_transactions、billing_records、
//                  usage_events、audit_logs。它们通过 user_id 仍指向那个已匿名化的行。
//
// 归档文件本身放在对象存储的 `account-archive/` 前缀下，与用户媒体分开，
// 便于运维单独设置保留期与访问策略。

const (
	// accountArchivePrefix 是归档对象键前缀。
	accountArchivePrefix = "account-archive/"
	// accountPurgeBatchSize 每轮处理的账号数上限。
	// 清理是低优先级后台工作，一次处理太多会长时间占用连接池。
	accountPurgeBatchSize = 20
)

// purgeDueAccounts 清除已过期宽限期的账号，返回成功处理的个数。
//
// store 为 nil 时**直接跳过**（不删任何数据）：没有归档能力就不该销毁数据 ——
// 这是"先归档后才删"这条原则的硬约束，不能因为存储没配好就降级成裸删。
func purgeDueAccounts(ctx context.Context, store *storage.AtomicStore) int {
	if store == nil {
		slog.Warn("account purge skipped: no storage backend for archive")
		return 0
	}
	purged := 0
	// attempted 记录本轮处理失败的账号，用于在后续取号时排除它们。
	//
	// 没有这个排除会**死循环**：nextDueAccount 每次都取"最早到期的那个"，
	// 而失败的账号仍是 pending_deletion 且已到期 ⇒ 下一轮又取到它，
	// 循环到 batch 上限为止。表现出来就是同一条错误日志刷 20 遍，
	// 而其它账号永远轮不到（实测踩过，日志里同一 user_id 重复 6 次）。
	var attempted []string
	// ⚠ 必须用 COALESCE 兜住 nil 切片：pgx 把 nil []string 编码成 NULL，
	// 而 `NOT (id = ANY(NULL))` 求值为 NULL（既非真也非假）⇒ WHERE 永不成立
	// ⇒ 一个账号都查不到。实测踩过：purgeDueAccounts 恒返回 0。
	for i := 0; i < accountPurgeBatchSize; i++ {
		if ctx.Err() != nil {
			break
		}
		userID, err := nextDueAccount(ctx, attempted)
		if err != nil {
			slog.Warn("account purge: pick due account failed", "error", err)
			break
		}
		if userID == "" {
			break // 没有待处理的
		}
		if err := purgeOneAccount(ctx, store, userID); err != nil {
			// 不标记 purged：下一轮会重试。归档失败时重试是安全的（覆盖写同名对象）。
			slog.Error("account purge failed, will retry next round",
				"user_id", userID, "error", err)
			attempted = append(attempted, userID)
			continue
		}
		purged++
	}
	return purged
}

// nextDueAccount 取一个到期且仍待清理的账号。
//
// exclude 是本轮已处理失败的账号：必须排除，否则会反复取到同一条。
// 返回空串表示"没有待处理的"（正常路径，不是错误）。
func nextDueAccount(ctx context.Context, exclude []string) (string, error) {
	var userID string
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT id FROM users
		  WHERE account_status = 'pending_deletion'
		    AND deletion_due_at IS NOT NULL
		    AND deletion_due_at <= NOW()
		    AND NOT (id = ANY(COALESCE($1::text[], '{}'::text[])))
		  ORDER BY deletion_due_at
		  LIMIT 1`, exclude).Scan(&userID)
	if err != nil {
		if isNoRows(err) {
			return "", nil
		}
		return "", err
	}
	return userID, nil
}

// purgeOneAccount 处理单个到期账号：归档 → 清除 → 匿名化 → 吊销会话。
func purgeOneAccount(ctx context.Context, store *storage.AtomicStore, userID string) error {
	// ① 归档（失败即中止，不删任何数据）。
	data, tenantID, err := buildUserDataExport(ctx, userID)
	if err != nil {
		return fmt.Errorf("build archive: %w", err)
	}
	payload, err := json.MarshalIndent(data, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal archive: %w", err)
	}
	key := fmt.Sprintf("%s%s/%s.json", accountArchivePrefix, userID, time.Now().UTC().Format("20060102T150405Z"))
	if err := store.Write(ctx, key, payload); err != nil {
		return fmt.Errorf("upload archive: %w", err)
	}
	slog.Info("account purge: archive uploaded", "user_id", userID, "key", key, "bytes", len(payload))

	// ② 清除个人内容 + ③ 匿名化（同一事务：要么都完成，要么都不动）。
	if err := purgePersonalData(ctx, userID); err != nil {
		return err
	}

	// ④ 吊销会话：数据已清，旧 token 不该再能访问（虽然也访问不到什么了）。
	if err := revokeUserSessions(ctx, userID); err != nil {
		// 吊销失败不影响清除结果（账号已是 purged，鉴权会拒绝），但要留痕。
		slog.Warn("account purge: revoke sessions failed", "user_id", userID, "error", err)
	}
	invalidateAccountStatusCache(userID)

	// 审计必须带真实 tenantID：`audit_logs.tenant_id` 有外键指向 tenants，
	// 传 0 会因 FK 失败而**静默丢弃**这条记录（AuditLog 只打 Warn）。
	db.AuditLog(ctx, userID, tenantID, "account.purged", "/v1/me/deletion",
		"", "", map[string]interface{}{"archive_key": key})
	return nil
}

// purgePersonalData 删除个人内容并匿名化 user 行。
//
// 单事务：半清状态（内容删了但身份还在，或反之）既不符合合规要求，
// 也难以在事后判定"到底删没删干净"。
func purgePersonalData(ctx context.Context, userID string) error {
	tx, err := db.GlobalDBManager.Begin(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()

	// 该用户的会话 id：多数内容表通过 session_id 关联（它们没有 user_id 列）。
	rows, err := tx.Query(ctx, `SELECT id FROM sessions WHERE user_id = $1`, userID)
	if err != nil {
		return fmt.Errorf("list sessions: %w", err)
	}
	var sessionIDs []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return err
		}
		sessionIDs = append(sessionIDs, id)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return err
	}

	// 该用户的 threads：投递表通过 thread_id 关联。
	rows, err = tx.Query(ctx, `SELECT id FROM threads WHERE user_id = $1`, userID)
	if err != nil {
		return fmt.Errorf("list threads: %w", err)
	}
	var threadIDs []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return err
		}
		threadIDs = append(threadIDs, id)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return err
	}

	// 先按 thread_id 删投递/订阅（它们引用 threads）。
	if len(threadIDs) > 0 {
		for _, sql := range []string{
			`DELETE FROM thread_deliveries WHERE thread_id = ANY($1::text[])`,
			`DELETE FROM thread_subscriptions WHERE thread_id = ANY($1::text[])`,
		} {
			if _, err := tx.Exec(ctx, sql, threadIDs); err != nil {
				return fmt.Errorf("purge threads children: %w", err)
			}
		}
	}

	// 再按 session_id 删内容。**排除财务与审计**：
	//   billing_records / usage_events 是计费依据，属法定留存；
	//   audit_logs 不可改也不可删（0018 触发器 + 它是合规产物）。
	//
	// 顺序=先子后父：这些表都引用 sessions，先删它们才能删 sessions 本身。
	if len(sessionIDs) > 0 {
		for _, sql := range []string{
			`DELETE FROM messages WHERE session_id = ANY($1::text[])`,
			`DELETE FROM message_archives WHERE session_id = ANY($1::text[])`,
			`DELETE FROM tool_calls WHERE session_id = ANY($1::text[])`,
			`DELETE FROM turns WHERE session_id = ANY($1::text[])`,
			`DELETE FROM session_folds WHERE session_id = ANY($1::text[])`,
			`DELETE FROM session_map_nodes WHERE session_id = ANY($1::text[])`,
			`DELETE FROM unified_messages WHERE session_id = ANY($1::text[])`,
			`DELETE FROM agent_runs WHERE session_id = ANY($1::text[])`,
			`DELETE FROM memory_summaries WHERE session_id = ANY($1::text[])`,
			`DELETE FROM pending_interactions WHERE session_id = ANY($1::text[])`,
			`DELETE FROM run_usage_settlements WHERE session_id = ANY($1::text[])`,
			// ⚠ subagent_runs 用的是 `root_session_id`，不是 `session_id`
			// （baseline 里写的是 session_id，真实库不是 —— 以 information_schema 为准）。
			`DELETE FROM subagent_runs WHERE root_session_id = ANY($1::text[])`,
			`DELETE FROM superagent_takeovers WHERE session_id = ANY($1::text[])`,
			`DELETE FROM conversation_shares WHERE session_id = ANY($1::text[])`,
		} {
			if _, err := tx.Exec(ctx, sql, sessionIDs); err != nil {
				return fmt.Errorf("purge session content: %w", err)
			}
		}
	}

	// 最后按 user_id 删其余个人数据（含 sessions / 知识库 / 媒体 / 身份绑定等）。
	//
	// threads 必须在 thread_deliveries 之后；sessions 必须在上面那批之后。
	for _, sql := range []string{
		`DELETE FROM threads WHERE user_id = $1`,
		`DELETE FROM sessions WHERE user_id = $1`,
		`DELETE FROM unified_sessions WHERE user_id = $1`,
		`DELETE FROM agent_sessions WHERE user_id = $1`,
		`DELETE FROM knowledge_documents WHERE user_id = $1`,
		`DELETE FROM knowledge_bases WHERE user_id = $1`,
		`DELETE FROM media_assets WHERE user_id = $1`,
		`DELETE FROM notifications WHERE user_id = $1`,
		`DELETE FROM user_notification_prefs WHERE user_id = $1`,
		`DELETE FROM user_onboarding WHERE user_id = $1`,
		`DELETE FROM user_knowledge_settings WHERE user_id = $1`,
		`DELETE FROM user_memory_entries WHERE user_id = $1`,
		`DELETE FROM user_memory_profile WHERE user_id = $1`,
		`DELETE FROM user_entitlements WHERE user_id = $1`,
		`DELETE FROM api_keys WHERE user_id = $1`,
		`DELETE FROM cron_jobs WHERE user_id = $1`,
		`DELETE FROM superagent_schedules WHERE user_id = $1`,
		`DELETE FROM superagent_takeovers WHERE user_id = $1`,
		`DELETE FROM tasks WHERE user_id = $1`,
		`DELETE FROM agents WHERE user_id = $1`,
		`DELETE FROM invoice_requests WHERE user_id = $1`,
		`DELETE FROM ent_user_identities WHERE user_id = $1`,
		`DELETE FROM ent_user_roles WHERE user_id = $1`,
		`DELETE FROM ent_group_members WHERE user_id = $1`,
		`DELETE FROM harness_profiles WHERE user_id = $1`,
		`DELETE FROM tenant_members WHERE user_id = $1`,
	} {
		if _, err := tx.Exec(ctx, sql, userID); err != nil {
			return fmt.Errorf("purge user data: %w", err)
		}
	}

	// ④ 匿名化 user 行本身。
	//
	// email 必须置 NULL 而不是空串：`ux_users_tenant_email` 是唯一索引，
	// 多个注销账号都写空串会互相冲突（第二个就插不进去/改不动）。
	// password_hash / phone / totp_* 一并清空 —— 它们是直接的身份凭据。
	if _, err := tx.Exec(ctx, `
		UPDATE users
		   SET email = NULL,
		       name = NULL,
		       phone = NULL,
		       password_hash = NULL,
		       password_set = false,
		       totp_secret_enc = NULL,
		       totp_enabled = false,
		       totp_recovery_hashes = NULL,
		       credits = 0,
		       credits_frozen = NULL,
		       deletion_freeze_state = NULL,
		       settings = NULL,
		       account_status = 'purged',
		       updated_at = NOW()
		 WHERE id = $1`, userID); err != nil {
		return fmt.Errorf("anonymize user: %w", err)
	}

	if err := tx.Commit(ctx); err != nil {
		return err
	}
	return nil
}

// StartAccountPurgeSweep 周期性清除到期账号（受 ctx 控制，随网关退出）。
//
// 与 StartAuditRetentionSweep 同形：ticker + 启动即跑一次 + 错误只记日志。
// 启动即跑一次的理由是"网关重启后尽快把到期数据清掉"——注销是用户已经
// 明确要求的动作，不能因为进程重启就无限推迟。
//
// interval 默认 1 小时：宽限期本身是 7 天，清理精度到小时已经足够；
// 更密的扫描只会增加空转的数据库负载。
func StartAccountPurgeSweep(ctx context.Context, interval time.Duration, store *storage.AtomicStore) {
	if interval <= 0 {
		interval = time.Hour
	}
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		// 第一轮用独立的超时 ctx：启动即跑时若处理很多账号，不该拖住 ticker。
		runPurgeRound(ctx, store)
		slog.Info("account purge sweep started", "interval", interval.String())
		for {
			select {
			case <-ctx.Done():
				slog.Info("account purge sweep stopped")
				return
			case <-ticker.C:
				runPurgeRound(ctx, store)
			}
		}
	}()
}

// runPurgeRound 跑一轮清理，单轮有硬超时。
//
// 单轮超时是必要的：清一个账号要删 20+ 张表并上传归档，若某张表被锁，
// 没有超时会让这个 goroutine 永久挂住，之后再也不会清理任何账号。
func runPurgeRound(parent context.Context, store *storage.AtomicStore) {
	if parent.Err() != nil {
		return
	}
	ctx, cancel := context.WithTimeout(parent, 10*time.Minute)
	defer cancel()
	n := purgeDueAccounts(ctx, store)
	if n > 0 {
		slog.Info("account purge round done", "purged", n)
	}
}
