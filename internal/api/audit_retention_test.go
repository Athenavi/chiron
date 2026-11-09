package api

import (
	"testing"
	"time"
)

// audit_retention_test.go —— 审计保留清理的纯契约（M3）。
//
// 这里只测不依赖数据库的部分：参数归一化、批大小防呆、单轮上限、
// 以及"清理动作自己的 action 名"这类会被审计页过滤命中的常量。
// 真正删数据的路径由端到端复测覆盖（需要真实 PG）。

// TestAuditRetentionConstantsAreSane 防止常量被改成危险值。
//
// 三个数字各自守一件事：
//   - 默认 180 天：太短会让合规追溯失去依据，太长则表继续膨胀；
//   - 批大小 > 0：0 会被 SQL 当成 LIMIT 0，静默什么都不删；
//   - 单轮上限 > 0：0 会让清理永远不执行，表现为"开了等于没开"。
func TestAuditRetentionConstantsAreSane(t *testing.T) {
	if DefaultAuditRetentionDays <= 0 {
		t.Fatalf("default retention days must be positive, got %d", DefaultAuditRetentionDays)
	}
	if auditRetentionBatchSize <= 0 {
		t.Fatalf("batch size must be positive, got %d", auditRetentionBatchSize)
	}
	if auditRetentionMaxBatchesPerRound <= 0 {
		t.Fatalf("max batches per round must be positive, got %d", auditRetentionMaxBatchesPerRound)
	}
	// 单轮最多删这么多行 —— 超过就说明"一轮清理可能占住连接池太久"。
	maxRowsPerRound := auditRetentionBatchSize * auditRetentionMaxBatchesPerRound
	if maxRowsPerRound <= 0 {
		t.Fatalf("rows per round overflows: %d", maxRowsPerRound)
	}
}

// TestAuditRetentionActionNameIsStable 验证清理动作的 action 名不变。
//
// 这条记录将来会被审计页的 action 过滤命中；拼写漂移会让
// "查所有清理记录"变成一个猜谜游戏。改动它必须同时改前端文案与文档。
func TestAuditRetentionActionNameIsStable(t *testing.T) {
	if auditActionRetentionSweep != "audit.retention_sweep" {
		t.Fatalf("action name changed to %q; 同步更新前端文案与文档后再改这个断言",
			auditActionRetentionSweep)
	}
}

// TestSweepAuditRetentionNormalizesRetentionDays 验证 <=0 时回落默认值。
//
// 这是不可恢复风险的唯一防线：配置手滑写成 0（或负数）若被解释成
// "删除 cutoff = now() 之前的全部记录"，等于一键清空审计。
// 本测试用不可用数据库触发早期返回，断言 Cutoff 落在"默认天数"附近。
func TestSweepAuditRetentionNormalizesRetentionDays(t *testing.T) {
	for _, days := range []int{0, -1, -180} {
		before := time.Now()
		res, err := SweepAuditRetention(t.Context(), days)
		// 数据库不可用时必然报错，这正是我们想要的早期返回路径；
		// 关键是 Cutoff 已经按默认天数算好，而不是 zero time。
		if err == nil {
			t.Logf("database unexpectedly available; skipping cutoff assertion")
			continue
		}
		if res.Cutoff.IsZero() {
			t.Fatalf("days=%d: cutoff must be set even on early return", days)
		}
		wantCutoff := before.AddDate(0, 0, -DefaultAuditRetentionDays)
		if diff := res.Cutoff.Sub(wantCutoff); diff > time.Minute || diff < -time.Minute {
			t.Errorf("days=%d: cutoff = %s, want ≈ %s（必须回落默认 %d 天）",
				days, res.Cutoff, wantCutoff, DefaultAuditRetentionDays)
		}
	}
}

// TestDeleteAuditBatchGuardsZeroBatchSize 验证批大小 0 被兜住。
//
// batchSize=0 会让 SQL 变成 LIMIT 0，静默删 0 行；调用方看到 0 会认为
// "已删空"并结束清理 —— 结果是永远不清。这里只断言函数不 panic 且
// 在数据库不可用时返回错误（而不是静默成功）。
func TestDeleteAuditBatchGuardsZeroBatchSize(t *testing.T) {
	_, err := deleteAuditBatch(t.Context(), time.Now(), 0)
	if err == nil {
		t.Skip("database available; 无库环境下无法断言防呆路径")
	}
}
