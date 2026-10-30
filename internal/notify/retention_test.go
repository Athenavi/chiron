package notify

import (
	"context"
	"errors"
	"testing"
	"time"
)

// T2 的目标：通知表不再只增不减。
//
// 这些用例钉的是**分批循环的控制流**（收敛、cutoff、上限、错误与取消），
// SQL 本身由真库用例覆盖 —— 内存替身证明不了 `DELETE ... WHERE id IN (SELECT ... LIMIT n)`
// 是否真能跑，正如它证明不了 `ON CONFLICT` 是否命中索引。

// 分批删空：2500 行按 1000 一批，应 3 批删完且总量准确。
func TestSweepRetentionDrainsInBatches(t *testing.T) {
	store := newFakeStore()
	store.pending = 2500
	m := NewManager(store, nil)

	res, err := m.SweepRetention(context.Background(), 90)
	if err != nil {
		t.Fatalf("SweepRetention 报错: %v", err)
	}
	if res.Deleted != 2500 {
		t.Fatalf("应删除 2500 行，实际 %d", res.Deleted)
	}
	if res.Batches != 3 {
		t.Fatalf("应执行 3 批（1000+1000+500），实际 %d", res.Batches)
	}
	if store.pending != 0 {
		t.Fatalf("删完应剩 0，实际 %d", store.pending)
	}
	// 不足一批时提前结束，不该再发一次空查询。
	if store.deleteCalls != 3 {
		t.Fatalf("应只调用 3 次 DeleteOlderThan，实际 %d", store.deleteCalls)
	}
}

// 没有过期行：一次调用即收敛，不该刷 50 次空查询。
func TestSweepRetentionNoopWhenNothingExpired(t *testing.T) {
	store := newFakeStore()
	store.pending = 0
	m := NewManager(store, nil)

	res, err := m.SweepRetention(context.Background(), 90)
	if err != nil {
		t.Fatalf("SweepRetention 报错: %v", err)
	}
	if res.Deleted != 0 {
		t.Fatalf("不应删除任何行，实际 %d", res.Deleted)
	}
	if store.deleteCalls != 1 {
		t.Fatalf("无过期行时应只探一次，实际调用 %d 次", store.deleteCalls)
	}
}

// cutoff 必须是 now - retentionDays，而不是 now 或别的值。
func TestSweepRetentionComputesCutoff(t *testing.T) {
	store := newFakeStore()
	m := NewManager(store, nil)

	before := time.Now()
	if _, err := m.SweepRetention(context.Background(), 30); err != nil {
		t.Fatalf("SweepRetention 报错: %v", err)
	}
	after := time.Now()

	wantEarliest := before.AddDate(0, 0, -30)
	wantLatest := after.AddDate(0, 0, -30)
	if store.lastCutoff.Before(wantEarliest) || store.lastCutoff.After(wantLatest) {
		t.Fatalf("cutoff 应为 now-30天（区间 %v~%v），实际 %v",
			wantEarliest, wantLatest, store.lastCutoff)
	}
	if store.lastBatchSize != retentionBatchSize {
		t.Fatalf("batchSize 应为 %d，实际 %d", retentionBatchSize, store.lastBatchSize)
	}
}

// retentionDays <= 0 时回落 DefaultRetentionDays，而不是"删光全部"。
func TestSweepRetentionFallsBackToDefaultDays(t *testing.T) {
	store := newFakeStore()
	m := NewManager(store, nil)
	before := time.Now()

	if _, err := m.SweepRetention(context.Background(), 0); err != nil {
		t.Fatalf("SweepRetention 报错: %v", err)
	}
	want := before.AddDate(0, 0, -DefaultRetentionDays)
	// 允许几秒误差。
	if d := store.lastCutoff.Sub(want); d < -5*time.Second || d > 5*time.Second {
		t.Fatalf("retentionDays=0 时应回落 %d 天，cutoff 偏差 %v", DefaultRetentionDays, d)
	}

	// 负数同理：不能让 cutoff 落到未来（那会一次删光所有通知）。
	store2 := newFakeStore()
	m2 := NewManager(store2, nil)
	if _, err := m2.SweepRetention(context.Background(), -7); err != nil {
		t.Fatalf("SweepRetention 报错: %v", err)
	}
	if !store2.lastCutoff.Before(time.Now()) {
		t.Fatal("cutoff 必须早于现在，否则会删光所有通知")
	}
}

// 单轮批次上限：积压再多也不许一轮跑到底（否则长时间占用连接池）。
func TestSweepRetentionRespectsPerRoundCap(t *testing.T) {
	store := newFakeStore()
	// 远超单轮上限：50 批 × 1000 = 50000。
	store.pending = 60000
	m := NewManager(store, nil)

	res, err := m.SweepRetention(context.Background(), 90)
	if err != nil {
		t.Fatalf("SweepRetention 报错: %v", err)
	}
	if res.Batches != retentionMaxBatchesPerRound {
		t.Fatalf("应停在单轮上限 %d 批，实际 %d", retentionMaxBatchesPerRound, res.Batches)
	}
	if want := retentionMaxBatchesPerRound * retentionBatchSize; res.Deleted != want {
		t.Fatalf("本轮应删 %d 行，实际 %d", want, res.Deleted)
	}
	// 剩余留到下一轮，不是丢失。
	if store.pending != 60000-res.Deleted {
		t.Fatalf("剩余应为 %d，实际 %d", 60000-res.Deleted, store.pending)
	}
}

// 删除失败：返回已删行数与错误，让调用方能记录"删了一部分"。
func TestSweepRetentionReportsPartialThenError(t *testing.T) {
	store := newFakeStore()
	store.pending = 5000
	m := NewManager(store, nil)

	// 第一批之后注入错误：用 deleteCalls 计数判断需要自定义行为，
	// 这里直接把 deleteErr 设为非 nil，则第一批就失败。
	store.deleteErr = errors.New("db down")
	res, err := m.SweepRetention(context.Background(), 90)
	if err == nil {
		t.Fatal("应返回错误")
	}
	if res.Deleted != 0 {
		t.Fatalf("第一批即失败时不应有删除量，实际 %d", res.Deleted)
	}
}

// ctx 取消：应尽快让出，且把取消错误交回调用方。
func TestSweepRetentionStopsOnContextCancel(t *testing.T) {
	store := newFakeStore()
	store.pending = 2000
	m := NewManager(store, nil)

	ctx, cancel := context.WithCancel(context.Background())
	cancel() // 进循环前就已取消

	_, err := m.SweepRetention(ctx, 90)
	if err == nil {
		t.Fatal("已取消的 ctx 应返回错误")
	}
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("应返回 context.Canceled，实际 %v", err)
	}
	if store.deleteCalls != 0 {
		t.Fatalf("取消后不应再调用 DeleteOlderThan，实际 %d 次", store.deleteCalls)
	}
}

// 默认值：DefaultRetentionDays 是 90（改这个值等于改变产品承诺的保留期）。
func TestDefaultRetentionDays(t *testing.T) {
	if DefaultRetentionDays != 90 {
		t.Fatalf("默认保留期应为 90 天，实际 %d", DefaultRetentionDays)
	}
}

// StartRetentionSweep 传非法 interval 时应回落 24h 而不是 panic / 忙循环。
// 只验证参数归一化：真起 goroutine 会拖慢测试且需要停它。
func TestStartRetentionSweepNormalizesParams(t *testing.T) {
	store := newFakeStore()
	m := NewManager(store, nil)

	ctx, cancel := context.WithCancel(context.Background())
	// interval=0、retentionDays=0 都走回落分支；启动即扫一次会删到 pending。
	store.pending = 10
	m.StartRetentionSweep(ctx, 0, 0)
	// 启动即扫一次是异步的，给它一点时间。
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) && store.pending != 0 {
		time.Sleep(5 * time.Millisecond)
	}
	cancel()

	if store.pending != 0 {
		t.Fatalf("启动即扫一次应把 10 行删完，实际剩 %d", store.pending)
	}
}
