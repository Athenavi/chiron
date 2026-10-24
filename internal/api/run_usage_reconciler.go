package api

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/billing"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/monitor"
)

// ── M19：按 run 的用量对账 ────────────────────────────────────────────────
//
// # 为什么需要它
//
// 崩溃 / 自动接管那一段不经过网关（`python-engine/app/main.py` 的 `_auto_resume_runner`），
// 因此没有 `DeductTokens`、没有 `billing_records`、没有 `TrueUpTenantQuota`；而网关只按
// **终态 `done` 事件**计费（`usageTotals.withEvent`），崩溃段根本没有 `done` ⇒ 那一份用量
// 永久不入账。M15e 只做到"这条 run 自己的预算不再白送"，账仍缺。
//
// # 权威账从哪来
//
// `agent_runs.checkpoint.usage` 是引擎侧**唯一**的 run 级用量真相：M15e 起每个回合末由
// "花钱的那一段"**赋值式**覆写全程累计（含跨续跑继承）。它最多陈旧到上一个回合边界 ——
// 崩溃前最后半回合的用量本来就不在里面，这一层偏差**刻意保留**（方向是少算，不会多收）。
//
// 已入账多少由网关自己回答：`turns`（每个回合无论免费/扣费都写 token）与
// `billing_records`（真正扣费的成本中心明细）按快照里的 `turn_ids` 求和，取两者较大值。
// 快照不带 `turn_ids`（本轮字段引入前的旧引擎）⇒ **跳过**，不按"0 已计费"处理：
// 那会把所有存量 run 都补一遍，是静默多收。
//
// # 三档开关（默认 shadow）
//
//	off     不启动循环
//	shadow  算出差额、打指标，**不动任何账**（切 enforce 前看规模）
//	enforce 扣余额 + 写 billing_records + 校正租户池 + 推进结算水位
//
// # 幂等
//
// 扣费键 = `rsm_` + sha256(run_id:input:output)[:16]，恰好 36 字符（`credit_transactions.turn_id`
// 与 `billing_records.turn_id` 都是 varchar(36)，且各有唯一索引）。水位存**总量**而不是
// 增量：同一行会被反复对账（续跑继续抬高 usage），增量累加在水位被人工修正或网关补写
// `turns` 之后就会重复收钱；总量比较天然收敛。
//
// 扣费失败（余额不足）**不推进水位**，下一轮重试；扣费成功后即使明细写入失败也推进
// 水位（钱已经收了，再补一次就是多收），只告警。

const (
	RunUsageModeOff     = "off"
	RunUsageModeShadow  = "shadow"
	RunUsageModeEnforce = "enforce"

	runUsageReconcileIntervalDefault = 5 * time.Minute
	runUsageReconcileMinAgeDefault   = 2 * time.Minute
	runUsageReconcileMaxAgeDefault   = 30 * 24 * time.Hour // 与 turns 保留期（30 天）一致
	runUsageReconcileBatchDefault    = 50
	runUsageReconcileTimeout         = 30 * time.Second
	runUsageReconcileLockTTL         = 10 * time.Minute

	// 与引擎 `app/agent/checkpoint.py` 的 `MAX_TRACKED_TURN_IDS` 同值。
	// 快照侧一旦截断就带 `turn_ids_truncated`，这里看到即跳过（集合不完整 ⇒ 会多收）。
	runUsageMaxTrackedTurns = 512
)

// runUsageScanSQL 找"该对账"的 run。
//
// `s.checkpoint_at < 当前快照时刻` 是**唯一**的"处理过没有"判据：
//   - shadow 不写结算行 ⇒ 每轮都会重新算（幂等、只读）。为了不重复占 batch，进程内还记
//     一份 `seen`；多实例各自重算一次无副作用。
//   - enforce 成功 ⇒ `checkpoint_at` 推到与 `agent_runs` 相同，下一轮自然排除；
//     run 又续跑抬高快照后会重新进入。
//   - 失败 / 跳过 ⇒ 失败不推进（下轮重试）；跳过推进（skipped 是"这类 run 不再处理"）。
const runUsageScanSQL = `
SELECT ar.id,
       COALESCE(ar.tenant_id::text, ''),   -- tenant_id 已经是 smallint；这里要的是「文本化后的空串兜底」，必须显式 ::text，否则 PG 会报 invalid input syntax for type smallint: ""
       COALESCE(ar.session_id, ''),
       ar.checkpoint,
       COALESCE(ar.checkpoint_at, ar.updated_at, ar.created_at) AS effective_at,
       COALESCE(s.settled_input, 0),
       COALESCE(s.settled_output, 0)
  FROM agent_runs ar
  LEFT JOIN run_usage_settlements s ON s.run_id = ar.id
 WHERE COALESCE(ar.checkpoint_at, ar.updated_at, ar.created_at)
       < now() - make_interval(secs => $1)
   AND COALESCE(ar.checkpoint_at, ar.updated_at, ar.created_at)
       > now() - make_interval(days => $2)
   AND ar.checkpoint IS NOT NULL
   AND (s.run_id IS NULL
        OR s.checkpoint_at IS NULL
        OR s.checkpoint_at < COALESCE(ar.checkpoint_at, ar.updated_at, ar.created_at))
 ORDER BY COALESCE(ar.checkpoint_at, ar.updated_at, ar.created_at) DESC
 LIMIT $3`

// runUsageBilledSQL 取"这条 run 链已经入账"的用量。
//
// 两个来源取 **GREATEST**：免费额度内的回合只在 `turns` 里有 token，扣费失败的回合也
// 只有 `turns`；真正扣过费的回合两者都有。取较大值是为了在任一侧写入失败时**少补**
// （宁可少收，不能多收）。
const runUsageBilledSQL = `
WITH billed AS (
    SELECT COALESCE(SUM(input_tokens), 0) AS in_tokens,
           COALESCE(SUM(output_tokens), 0) AS out_tokens
      FROM turns
     WHERE id = ANY($1::text[])
       AND ($2 = '' OR session_id = $2)
), recorded AS (
    SELECT COALESCE(SUM(input_tokens), 0) AS in_tokens,
           COALESCE(SUM(output_tokens), 0) AS out_tokens
      FROM billing_records
     WHERE turn_id = ANY($1::text[])
       AND ($2 = '' OR session_id = $2)
)
SELECT GREATEST(billed.in_tokens, recorded.in_tokens),
       GREATEST(billed.out_tokens, recorded.out_tokens)
  FROM billed, recorded`

const runUsageSettleSQL = `
INSERT INTO run_usage_settlements
    (run_id, tenant_id, user_id, session_id, settled_input, settled_output, settled_cached,
     checkpoint_at, status, attempts, last_error, updated_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'settled', 1, NULL, now())
ON CONFLICT (run_id) DO UPDATE
   SET tenant_id      = EXCLUDED.tenant_id,
       user_id        = EXCLUDED.user_id,
       session_id     = EXCLUDED.session_id,
       settled_input  = GREATEST(run_usage_settlements.settled_input, EXCLUDED.settled_input),
       settled_output = GREATEST(run_usage_settlements.settled_output, EXCLUDED.settled_output),
       settled_cached = GREATEST(run_usage_settlements.settled_cached, EXCLUDED.settled_cached),
       checkpoint_at  = GREATEST(COALESCE(run_usage_settlements.checkpoint_at, EXCLUDED.checkpoint_at),
                                 EXCLUDED.checkpoint_at),
       status         = 'settled',
       attempts       = run_usage_settlements.attempts + 1,
       last_error     = NULL,
       updated_at     = now()`

// runUsageStateSQL 记录失败/跳过。**不推进** `checkpoint_at` 的只有失败那一档：
// 失败要下轮重试；`skipped` 推进（同一快照不再重复算），run 续跑后快照变新会重新进入。
const runUsageStateSQL = `
INSERT INTO run_usage_settlements
    (run_id, tenant_id, user_id, session_id, settled_input, settled_output, settled_cached,
     checkpoint_at, status, attempts, last_error, updated_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 1, $10, now())
ON CONFLICT (run_id) DO UPDATE
   SET status     = EXCLUDED.status,
       attempts   = run_usage_settlements.attempts + 1,
       last_error = EXCLUDED.last_error,
       checkpoint_at = CASE
           WHEN EXCLUDED.status = 'skipped'
           THEN GREATEST(COALESCE(run_usage_settlements.checkpoint_at, EXCLUDED.checkpoint_at),
                         EXCLUDED.checkpoint_at)
           ELSE run_usage_settlements.checkpoint_at
       END,
       updated_at = now()`

type runUsageRow struct {
	RunID         string
	TenantID      string
	SessionID     string
	Checkpoint    []byte
	EffectiveAt   time.Time
	SettledInput  int64
	SettledOutput int64
}

type runUsageSnapshot struct {
	Usage struct {
		InputTokens  int64 `json:"input_tokens"`
		OutputTokens int64 `json:"output_tokens"`
		CachedTokens int64 `json:"cached_tokens"`
	} `json:"usage"`
	TurnIDs          []string `json:"turn_ids"`
	TurnIDsTruncated bool     `json:"turn_ids_truncated"`
	TaskMeta         struct {
		UserID string `json:"user_id"`
	} `json:"task_meta"`
}

type runUsageReconciler struct {
	biller   *billing.Manager
	mode     string
	interval time.Duration
	minAge   time.Duration
	maxAge   time.Duration
	batch    int
	// seen 只用于 shadow 的进程内节流：不落库，所以切 enforce 不会漏补；
	// 重启后重算一次也只是多打一条指标。
	seen map[string]time.Time
}

// StartRunUsageReconciler 启动按-run 用量对账（受 lifecycleCtx 控制）。
//
// 多实例：Redis 锁只让一个实例执行一轮（照 retention 的做法）；Redis 不可用时每个实例
// 都跑 —— 补偿路径本身幂等（结算水位 + 扣费幂等键），安全只是多算几次。
func StartRunUsageReconciler(ctx context.Context, biller *billing.Manager) {
	if biller == nil || db.Pool == nil {
		return
	}
	mode := runUsageReconcileMode()
	if mode == RunUsageModeOff {
		slog.Info("run usage reconciler disabled (RUN_USAGE_RECONCILE_MODE=off)")
		return
	}
	r := &runUsageReconciler{
		biller:   biller,
		mode:     mode,
		interval: runUsageReconcileInterval(),
		minAge:   runUsageReconcileMinAge(),
		maxAge:   runUsageReconcileMaxAge(),
		batch:    runUsageReconcileBatch(),
		seen:     map[string]time.Time{},
	}
	go func() {
		// 启动后先对一次账（不等第一个 tick）：崩溃后的用量越早补越好。
		// 锁与 ticker 内的那一轮同形；抢不到就等下一轮。
		if release, ok := tryAcquireRunUsageLock(ctx); ok {
			r.sweep(ctx)
			release()
		}
		ticker := time.NewTicker(r.interval)
		defer ticker.Stop()
		slog.Info("run usage reconciler started",
			"mode", mode, "interval", r.interval.String(),
			"min_age", r.minAge.String(), "max_age", r.maxAge.String(), "batch", r.batch)
		for {
			select {
			case <-ctx.Done():
				slog.Info("run usage reconciler stopped")
				return
			case <-ticker.C:
				release, ok := tryAcquireRunUsageLock(ctx)
				if !ok {
					slog.Debug("run usage reconcile skipped: another instance is running it")
					continue
				}
				r.sweep(ctx)
				release()
			}
		}
	}()
}

func (r *runUsageReconciler) sweep(ctx context.Context) {
	sctx, cancel := context.WithTimeout(ctx, runUsageReconcileTimeout)
	defer cancel()

	rows, err := db.Pool.Query(sctx, runUsageScanSQL,
		int(r.minAge.Seconds()), int(r.maxAge.Hours()/24), r.batch)
	if err != nil {
		slog.Warn("run usage reconcile scan failed", "error", err)
		return
	}
	defer rows.Close()

	stats := map[string]int{}
	for rows.Next() {
		var row runUsageRow
		if err := rows.Scan(&row.RunID, &row.TenantID, &row.SessionID,
			&row.Checkpoint, &row.EffectiveAt, &row.SettledInput, &row.SettledOutput); err != nil {
			slog.Warn("run usage reconcile scan row failed", "error", err)
			continue
		}
		stats["scanned"]++
		outcome := r.settleOnce(sctx, row)
		stats[outcome]++
	}
	if err := rows.Err(); err != nil {
		slog.Warn("run usage reconcile rows failed", "error", err)
	}
	if stats["scanned"] > 0 {
		slog.Info("run usage reconcile sweep", "mode", r.mode, "stats", stats)
	}
}

// settleOnce 处理一行，返回结果标签（只用于本轮日志）。
func (r *runUsageReconciler) settleOnce(ctx context.Context, row runUsageRow) string {
	if seenAt, ok := r.seen[row.RunID]; ok && !row.EffectiveAt.After(seenAt) {
		return "deduped"
	}

	snap, skip := parseRunUsageCheckpoint(row.Checkpoint)
	if skip != "" {
		monitor.IncRunUsageReconcileSkipped()
		r.markState(ctx, row, "skipped", skip)
		return skip
	}

	billedIn, billedOut, err := r.billedUsage(ctx, row.SessionID, snap.TurnIDs)
	if err != nil {
		monitor.IncRunUsageReconcileFailed()
		slog.Warn("run usage reconcile: billed lookup failed",
			"run", row.RunID, "error", err)
		r.markState(ctx, row, "failed", "billed_lookup_failed")
		return "failed"
	}

	// 已入账 = max(网关记录, 上次结算水位)。水位是**总量**，见文件头。
	deltaIn, deltaOut := runUsageDelta(snap, billedIn, billedOut, row.SettledInput, row.SettledOutput)
	delta := deltaIn + deltaOut
	if delta == 0 {
		// 已结清：记进 seen，避免 shadow（不落库）每轮重复打同一条日志/指标。
		r.seen[row.RunID] = row.EffectiveAt
		return "noop"
	}

	if r.mode == RunUsageModeShadow {
		monitor.IncRunUsageReconcileShadowed(delta)
		r.seen[row.RunID] = row.EffectiveAt
		slog.Debug("run usage reconcile (shadow): would settle",
			"run", row.RunID, "input", deltaIn, "output", deltaOut)
		return "shadowed"
	}

	userID := strings.TrimSpace(snap.TaskMeta.UserID)
	key := runUsageSettlementKey(row.RunID, snap.Usage.InputTokens, snap.Usage.OutputTokens)
	if _, err := r.biller.DeductTokens(userID, int(deltaIn), int(deltaOut), key); err != nil {
		// 余额不足 / 用户不存在：**不推进水位**，下一轮重试。用量已经真实发生，
		// 不因为一次扣不动就把它从账上抹掉（方向仍然是"账不平就说出来"）。
		monitor.IncRunUsageReconcileFailed()
		slog.Warn("run usage reconcile: deduct failed (will retry)",
			"run", row.RunID, "user", userID, "input", deltaIn, "output", deltaOut, "error", err)
		r.markState(ctx, row, "failed", "deduct_failed")
		return "failed"
	}

	// 钱已经扣了：从这里开始**不能**让水位倒退，否则下一轮会再扣一次。
	if err := r.biller.RecordTokenUsage(ctx, userID, row.SessionID,
		int(deltaIn), int(deltaOut), key); err != nil {
		slog.Warn("run usage reconcile: billing record failed (credit already deducted)",
			"run", row.RunID, "user", userID, "error", err)
	}
	// 租户池与用户余额是两本账：用量发生了就该进池，哪怕扣费失败（这里已经成功）。
	TrueUpTenantQuota(ctx, userID, parseTenantID(row.TenantID), delta, 0)

	if err := r.markSettled(ctx, row, snap); err != nil {
		// 扣费已成功、水位没落库 ⇒ 下一轮会再算一次。扣费幂等键（含水位总量）会把
		// 重复扣挡住，所以这里只告警，不回滚任何东西。
		slog.Warn("run usage reconcile: settlement watermark write failed",
			"run", row.RunID, "error", err)
	}
	r.seen[row.RunID] = row.EffectiveAt
	monitor.IncRunUsageReconciled(delta)
	slog.Info("run usage reconciled",
		"run", row.RunID, "user", userID, "input", deltaIn, "output", deltaOut)
	return "settled"
}

func (r *runUsageReconciler) billedUsage(ctx context.Context, sessionID string, turnIDs []string) (int64, int64, error) {
	var in, out int64
	err := db.Pool.QueryRow(ctx, runUsageBilledSQL, turnIDs, sessionID).Scan(&in, &out)
	if err != nil {
		return 0, 0, err
	}
	return in, out, nil
}

func (r *runUsageReconciler) markSettled(ctx context.Context, row runUsageRow, snap runUsageSnapshot) error {
	_, err := db.Pool.Exec(ctx, runUsageSettleSQL,
		row.RunID, nullableString(row.TenantID), snap.TaskMeta.UserID,
		nullableString(row.SessionID),
		snap.Usage.InputTokens, snap.Usage.OutputTokens, snap.Usage.CachedTokens,
		row.EffectiveAt)
	return err
}

func (r *runUsageReconciler) markState(ctx context.Context, row runUsageRow, status, reason string) {
	_, err := db.Pool.Exec(ctx, runUsageStateSQL,
		row.RunID, nullableString(row.TenantID), "", nullableString(row.SessionID),
		row.SettledInput, row.SettledOutput, 0,
		row.EffectiveAt, status, reason)
	if err != nil {
		slog.Debug("run usage reconcile: state write failed",
			"run", row.RunID, "status", status, "error", err)
	}
	if status == "skipped" {
		r.seen[row.RunID] = row.EffectiveAt
	}
}

// parseRunUsageCheckpoint 解析快照，返回 `(snapshot, 跳过原因)`；原因为空 = 可以继续。
//
// 三道闸门都指向同一个方向 —— **宁可少补，不能多收**：
//  1. 没有 `turn_ids` 字段 = 旧引擎写的快照，无法知道哪些回合已计费 ⇒ 跳过；
//  2. `turn_ids_truncated` = 集合不完整 ⇒ 跳过；
//  3. 集合为空 / 缺 `user_id` ⇒ 无法结算（直连引擎的 run 没有网关回合）⇒ 跳过。
func parseRunUsageCheckpoint(raw []byte) (runUsageSnapshot, string) {
	var snap runUsageSnapshot
	if len(raw) == 0 {
		return snap, "empty_checkpoint"
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		return snap, "invalid_checkpoint"
	}
	if _, ok := fields["turn_ids"]; !ok {
		return snap, "legacy_snapshot_without_turn_ids"
	}
	if err := json.Unmarshal(raw, &snap); err != nil {
		return snap, "invalid_checkpoint"
	}
	if snap.TurnIDsTruncated || len(snap.TurnIDs) > runUsageMaxTrackedTurns {
		return snap, "turn_ids_truncated"
	}
	if len(snap.TurnIDs) == 0 {
		return snap, "no_gateway_turn"
	}
	if strings.TrimSpace(snap.TaskMeta.UserID) == "" {
		return snap, "missing_user_id"
	}
	if snap.Usage.InputTokens < 0 || snap.Usage.OutputTokens < 0 || snap.Usage.CachedTokens < 0 {
		// 快照是 JSONB，人工修数据可能写歪。负的用量不是"少算"，是坏数据。
		return snap, "invalid_usage"
	}
	return snap, ""
}

// runUsageSettlementKey 生成扣费/明细的幂等键，长度**恰好 36**（两个 turn_id 列都是 varchar(36)）。
//
// 键里带"水位总量"而不是增量：同一 run 的第二次补偿水位必然不同 ⇒ 新键；两次对账算出
// 同一总量 ⇒ 同一个键 ⇒ 唯一索引挡住重复扣费。
func runUsageSettlementKey(runID string, input, output int64) string {
	sum := sha256.Sum256([]byte(runID + ":" + strconv.FormatInt(input, 10) + ":" + strconv.FormatInt(output, 10)))
	return "rsm_" + hex.EncodeToString(sum[:16]) // 4 + 32 = 36
}

func nonNegative(v int64) int64 {
	if v < 0 {
		return 0
	}
	return v
}

// runUsageDelta 算"还没入账"的增量。
//
// 两个减数取 max 而不是相加：`turns`/`billing_records` 记的是网关**已经处理**的回合，
// 结算水位记的是本 job **已经补偿**的总量。两者覆盖的可能是同一段用量（正常路径先由
// 网关记账，崩溃段再由本 job 补），相加会把同一段扣两遍。
//
// 结果夹到非负：水位被人工调高、或网关后来补写了 `turns` 之后，`usage` 可能小于
// 已入账量 —— 那不是"该退钱"，而是"这条链已经结清"，不能生成负数补偿。
func runUsageDelta(snap runUsageSnapshot, billedIn, billedOut, settledIn, settledOut int64) (int64, int64) {
	baseIn := maxInt64(billedIn, settledIn)
	baseOut := maxInt64(billedOut, settledOut)
	return nonNegative(snap.Usage.InputTokens - baseIn),
		nonNegative(snap.Usage.OutputTokens - baseOut)
}

func maxInt64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}

func nullableString(v string) any {
	if v == "" {
		return nil
	}
	return v
}

func runUsageReconcileMode() string {
	v := strings.ToLower(strings.TrimSpace(os.Getenv("RUN_USAGE_RECONCILE_MODE")))
	switch v {
	case RunUsageModeOff, RunUsageModeShadow, RunUsageModeEnforce:
		return v
	case "":
		return RunUsageModeShadow
	default:
		slog.Warn("unknown RUN_USAGE_RECONCILE_MODE, falling back to shadow",
			"value", v)
		return RunUsageModeShadow
	}
}

func runUsageReconcileInterval() time.Duration {
	return envDurationSeconds("RUN_USAGE_RECONCILE_INTERVAL_SECONDS",
		runUsageReconcileIntervalDefault)
}

func runUsageReconcileMinAge() time.Duration {
	return envDurationSeconds("RUN_USAGE_RECONCILE_MIN_AGE_SECONDS",
		runUsageReconcileMinAgeDefault)
}

func runUsageReconcileMaxAge() time.Duration {
	days := envPositiveInt("RUN_USAGE_RECONCILE_MAX_AGE_DAYS",
		int(runUsageReconcileMaxAgeDefault.Hours()/24))
	return time.Duration(days) * 24 * time.Hour
}

func runUsageReconcileBatch() int {
	return envPositiveInt("RUN_USAGE_RECONCILE_BATCH", runUsageReconcileBatchDefault)
}

func envPositiveInt(name string, fallback int) int {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback
	}
	v, err := strconv.Atoi(raw)
	if err != nil || v <= 0 {
		slog.Warn("invalid integer env, using default", "name", name, "value", raw, "default", fallback)
		return fallback
	}
	return v
}

func envDurationSeconds(name string, fallback time.Duration) time.Duration {
	return time.Duration(envPositiveInt(name, int(fallback.Seconds()))) * time.Second
}

// runUsageLockKey 跨实例互斥键：同一时刻只让一个实例对账。
var runUsageLockKey = db.RedisKey("run-usage-reconcile:lock")

const runUsageLockAcquireLua = `
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) then
  return 1
end
return 0
`

// tryAcquireRunUsageLock 与 retention 的锁同形。Redis 不可用 ⇒ ok=true（每个实例都跑）。
// 补偿路径幂等（结算水位 + 扣费幂等键），所以退回多实例只是浪费一次扫描。
func tryAcquireRunUsageLock(ctx context.Context) (release func(), ok bool) {
	if db.Redis == nil {
		return func() {}, true
	}
	res := db.Redis.Eval(ctx, runUsageLockAcquireLua,
		[]string{runUsageLockKey}, "1", int(runUsageReconcileLockTTL.Seconds()))
	if err := res.Err(); err != nil {
		slog.Debug("run usage reconcile lock acquire failed, skipping this round", "error", err)
		return nil, false
	}
	if n, err := res.Int(); err != nil || n != 1 {
		return nil, false
	}
	return func() {
		// 与 retention 一致：不做 CAS 释放。对账幂等，最坏是锁 TTL 到期后重复跑一轮。
		_ = db.Redis.Del(context.Background(), runUsageLockKey).Err()
	}, true
}
