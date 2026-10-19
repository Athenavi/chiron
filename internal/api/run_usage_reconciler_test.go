package api

import (
	"encoding/json"
	"strings"
	"testing"
)

// M19：对账 job 的纯判据。真实 SQL 由 `integration`/real-stack 覆盖，这里钉住三件
// 最容易写错、且错了会**静默多收钱**的事：
//   1. 哪些快照可以参与对账（旧快照 / 截断 / 无网关回合一律跳过）；
//   2. 差额怎么算（已入账取 max，不做增量累加）；
//   3. 幂等键必须装得进 varchar(36) 的两张 turn_id 列。

func snapshotJSON(t *testing.T, snap map[string]any) []byte {
	t.Helper()
	raw, err := json.Marshal(snap)
	if err != nil {
		t.Fatalf("marshal snapshot: %v", err)
	}
	return raw
}

func TestParseRunUsageCheckpointAcceptsSettlementShape(t *testing.T) {
	raw := snapshotJSON(t, map[string]any{
		"usage":     map[string]any{"input_tokens": 1200, "output_tokens": 340, "cached_tokens": 50},
		"turn_ids":  []string{"turn-1", "turn-2"},
		"task_meta": map[string]any{"user_id": "u1"},
	})

	snap, skip := parseRunUsageCheckpoint(raw)

	if skip != "" {
		t.Fatalf("unexpected skip: %s", skip)
	}
	if snap.Usage.InputTokens != 1200 || snap.Usage.OutputTokens != 340 || snap.Usage.CachedTokens != 50 {
		t.Fatalf("usage not parsed: %+v", snap.Usage)
	}
	if len(snap.TurnIDs) != 2 || snap.TurnIDs[1] != "turn-2" {
		t.Fatalf("turn ids not parsed: %+v", snap.TurnIDs)
	}
	if snap.TaskMeta.UserID != "u1" {
		t.Fatalf("user id not parsed: %q", snap.TaskMeta.UserID)
	}
}

func TestParseRunUsageCheckpointSkipsLegacySnapshot(t *testing.T) {
	// 本字段引入前的快照：不能按"0 已计费"处理 —— 那会把所有存量 run 补一遍。
	raw := snapshotJSON(t, map[string]any{
		"usage":     map[string]any{"input_tokens": 10_000, "output_tokens": 2_000},
		"task_meta": map[string]any{"user_id": "u1"},
	})

	_, skip := parseRunUsageCheckpoint(raw)

	if skip != "legacy_snapshot_without_turn_ids" {
		t.Fatalf("want legacy skip, got %q", skip)
	}
}

func TestParseRunUsageCheckpointSkipsTruncated(t *testing.T) {
	raw := snapshotJSON(t, map[string]any{
		"usage":              map[string]any{"input_tokens": 10},
		"turn_ids":           []string{"turn-1"},
		"turn_ids_truncated": true,
		"task_meta":          map[string]any{"user_id": "u1"},
	})

	_, skip := parseRunUsageCheckpoint(raw)

	if skip != "turn_ids_truncated" {
		t.Fatalf("want truncated skip, got %q", skip)
	}
}

func TestParseRunUsageCheckpointSkipsNoGatewayTurn(t *testing.T) {
	raw := snapshotJSON(t, map[string]any{
		"usage":     map[string]any{"input_tokens": 10},
		"turn_ids":  []string{},
		"task_meta": map[string]any{"user_id": "u1"},
	})

	_, skip := parseRunUsageCheckpoint(raw)

	if skip != "no_gateway_turn" {
		t.Fatalf("want no_gateway_turn skip, got %q", skip)
	}
}

func TestParseRunUsageCheckpointSkipsMissingUser(t *testing.T) {
	raw := snapshotJSON(t, map[string]any{
		"usage":     map[string]any{"input_tokens": 10},
		"turn_ids":  []string{"turn-1"},
		"task_meta": map[string]any{},
	})

	_, skip := parseRunUsageCheckpoint(raw)

	if skip != "missing_user_id" {
		t.Fatalf("want missing_user_id skip, got %q", skip)
	}
}

func TestParseRunUsageCheckpointSkipsNegativeUsage(t *testing.T) {
	raw := snapshotJSON(t, map[string]any{
		"usage":     map[string]any{"input_tokens": -1, "output_tokens": 10},
		"turn_ids":  []string{"turn-1"},
		"task_meta": map[string]any{"user_id": "u1"},
	})

	_, skip := parseRunUsageCheckpoint(raw)

	if skip != "invalid_usage" {
		t.Fatalf("want invalid_usage skip, got %q", skip)
	}
}

func TestParseRunUsageCheckpointSkipsInvalidJSON(t *testing.T) {
	_, skip := parseRunUsageCheckpoint([]byte("{not json"))

	if skip != "invalid_checkpoint" {
		t.Fatalf("want invalid_checkpoint skip, got %q", skip)
	}
}

func TestRunUsageDeltaUsesMaxOfBilledAndWatermark(t *testing.T) {
	var snap runUsageSnapshot
	snap.Usage.InputTokens = 1000
	snap.Usage.OutputTokens = 200

	// 网关已计 400/100，水位已结算 600/50 ⇒ 已处理的是两者较大值（不是相加）。
	in, out := runUsageDelta(snap, 400, 100, 600, 50)
	if in != 400 || out != 100 {
		t.Fatalf("want (400,100), got (%d,%d)", in, out)
	}

	// 网关补写到 900/300（超过快照的 output）⇒ output 夹到 0，不产生负数补偿。
	in, out = runUsageDelta(snap, 900, 300, 0, 0)
	if in != 100 || out != 0 {
		t.Fatalf("want (100,0), got (%d,%d)", in, out)
	}

	// 完全没记账 ⇒ 全额补。
	in, out = runUsageDelta(snap, 0, 0, 0, 0)
	if in != 1000 || out != 200 {
		t.Fatalf("want full delta, got (%d,%d)", in, out)
	}
}

func TestRunUsageSettlementKeyFitsTurnColumn(t *testing.T) {
	// credit_transactions.turn_id 与 billing_records.turn_id 都是 varchar(36)，
	// 且各有唯一索引。键超长会让扣费直接失败（表现为"对账永远补不上"）。
	key := runUsageSettlementKey("8f14e45f-ea0c-4a6b-9c1d-2b3a4c5d6e7f", 1234, 567)

	if len(key) != 36 {
		t.Fatalf("settlement key must be exactly 36 chars, got %d: %q", len(key), key)
	}
	if !strings.HasPrefix(key, "rsm_") {
		t.Fatalf("settlement key should be recognizable: %q", key)
	}
}

func TestRunUsageSettlementKeyIsStableAndWatermarkSensitive(t *testing.T) {
	const runID = "run-1"

	first := runUsageSettlementKey(runID, 100, 20)
	again := runUsageSettlementKey(runID, 100, 20)
	next := runUsageSettlementKey(runID, 120, 20)

	if first != again {
		t.Fatalf("same watermark must produce the same idempotency key")
	}
	if first == next {
		t.Fatalf("a higher watermark must produce a new key, otherwise the retry is swallowed")
	}
}

func TestRunUsageReconcileModeDefaultsToShadow(t *testing.T) {
	t.Setenv("RUN_USAGE_RECONCILE_MODE", "")

	if got := runUsageReconcileMode(); got != RunUsageModeShadow {
		t.Fatalf("want shadow default, got %q", got)
	}
}

func TestRunUsageReconcileModeHonoursExplicitValues(t *testing.T) {
	for _, mode := range []string{RunUsageModeOff, RunUsageModeShadow, RunUsageModeEnforce} {
		t.Setenv("RUN_USAGE_RECONCILE_MODE", strings.ToUpper(mode))
		if got := runUsageReconcileMode(); got != mode {
			t.Fatalf("want %q, got %q", mode, got)
		}
	}
}

func TestRunUsageReconcileModeFallsBackOnDirtyValue(t *testing.T) {
	t.Setenv("RUN_USAGE_RECONCILE_MODE", "enforced!")

	if got := runUsageReconcileMode(); got != RunUsageModeShadow {
		t.Fatalf("dirty value must fall back to shadow, got %q", got)
	}
}
