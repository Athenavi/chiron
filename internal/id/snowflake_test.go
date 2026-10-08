package id

import (
	"sync"
	"testing"
	"time"
)

// 为什么这些用例值钱：雪花 ID 一旦重复，就是 JWT jti / 会话 ID / 事件 ID 全链路碰撞。
// 代码注释里自己记着一次真实事故（"修复前默认恒为 0：多实例同一毫秒各自递增 seq 会产出相同 ID"），
// 而本包此前**零测试**。这里把"唯一性"与"位布局"钉成机械断言。

func TestNewRejectsOutOfRangeWorkerID(t *testing.T) {
	for _, wid := range []int64{-1, workerMax + 1, 1 << 20} {
		g, err := New(wid)
		if err == nil || g != nil {
			t.Fatalf("New(%d) 应报错并返回 nil，实际 g=%v err=%v", wid, g, err)
		}
	}
	for _, wid := range []int64{0, 1, workerMax} {
		g, err := New(wid)
		if err != nil || g == nil {
			t.Fatalf("New(%d) 应成功，实际 g=%v err=%v", wid, g, err)
		}
	}
}

func TestNextIsUniqueUnderConcurrency(t *testing.T) {
	g, err := New(7)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	const workers, per = 16, 500
	results := make(chan string, workers*per)
	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < per; j++ {
				results <- g.Next()
			}
		}()
	}
	wg.Wait()
	close(results)

	seen := make(map[string]struct{}, workers*per)
	for id := range results {
		if _, dup := seen[id]; dup {
			t.Fatalf("并发下出现重复 ID：%q", id)
		}
		seen[id] = struct{}{}
	}
	if len(seen) != workers*per {
		t.Fatalf("期望 %d 个唯一 ID，实际 %d", workers*per, len(seen))
	}
}

func TestWorkerIDOccupiesWorkerBits(t *testing.T) {
	g1, _ := New(1)
	g2, _ := New(2)
	// 把 lastTime 推到"未来"⇒ 强制走同毫秒分支，等价于"两个实例同一毫秒各发一个 ID"（原事故场景）
	future := time.Now().UnixMilli() - epochMillis + 10_000
	g1.lastTime, g2.lastTime = future, future

	a1, a2 := g1.nextInt64(), g2.nextInt64()
	if a1 == a2 {
		t.Fatalf("不同 worker 在同一毫秒产出了相同 ID：%d", a1)
	}
	if got := (a1 >> workerShift) & workerMax; got != 1 {
		t.Fatalf("worker 位应为 1，实际 %d", got)
	}
	if got := (a2 >> workerShift) & workerMax; got != 2 {
		t.Fatalf("worker 位应为 2，实际 %d", got)
	}
	if a1>>timeShift != a2>>timeShift {
		t.Fatalf("同毫秒的时间位应相同：%d vs %d", a1>>timeShift, a2>>timeShift)
	}
}

func TestClockRegressionKeepsUniqueAndMonotonic(t *testing.T) {
	g, _ := New(5)
	future := time.Now().UnixMilli() - epochMillis + 5_000 // 模拟时钟回退：lastTime 在未来
	g.lastTime = future

	seen := make(map[int64]struct{}, 200)
	var prev int64 = -1
	for i := 0; i < 200; i++ {
		v := g.nextInt64()
		if _, dup := seen[v]; dup {
			t.Fatalf("时钟回退后出现重复 ID：%d", v)
		}
		seen[v] = struct{}{}
		if v>>timeShift != future {
			t.Fatalf("回退期间时间位应固定在 lastTime=%d，实际 %d", future, v>>timeShift)
		}
		if v <= prev {
			t.Fatalf("ID 必须严格递增（seq 兜底）：%d <= %d", v, prev)
		}
		prev = v
	}
}

func TestSequenceExhaustionWaitsForRealClock(t *testing.T) {
	g, _ := New(3)
	now := time.Now().UnixMilli() - epochMillis
	g.lastTime = now
	g.seq = seqMax // 下一次 +1 回绕到 0 ⇒ 触发"等真实时钟前进"

	v := g.nextInt64()
	if v>>timeShift <= now {
		t.Fatalf("序列耗尽后应等到时钟前进：时间位 %d 未超过 %d", v>>timeShift, now)
	}
	if v&seqMax != 0 {
		t.Fatalf("回绕后序列应为 0，实际 %d", v&seqMax)
	}
}

func TestBase62EncodeIsInjectiveOnSample(t *testing.T) {
	cases := map[int64]string{0: "0", 1: "1", 9: "9", 10: "A", 61: "z", 62: "10"}
	for n, want := range cases {
		if got := base62Encode(n); got != want {
			t.Fatalf("base62Encode(%d) = %q，期望 %q", n, got, want)
		}
	}
	seen := make(map[string]int64, 20000)
	for n := int64(0); n < 20000; n++ {
		s := base62Encode(n)
		if prev, dup := seen[s]; dup {
			t.Fatalf("base62 编码冲突：%d 与 %d 都编成 %q", prev, n, s)
		}
		seen[s] = n
	}
}

func TestResolveWorkerIDPrefersEnvAndStaysInRange(t *testing.T) {
	// 把 data 目录指到临时目录：避免在开发者真实 data 目录里写 worker.id
	t.Setenv("CHIRON_DATA_DIR", t.TempDir())

	t.Setenv("WORKER_ID", "37")
	if got := resolveWorkerID(); got != 37 {
		t.Fatalf("应优先读 WORKER_ID=37，实际 %d", got)
	}

	t.Setenv("WORKER_ID", "99999") // 越界 ⇒ 落到"持久化/随机"分支，但结果必须仍在合法区间
	if got := resolveWorkerID(); got < 0 || got > workerMax {
		t.Fatalf("越界 WORKER_ID 后回退值超出区间：%d", got)
	}
}

func TestNextIDStaysUnique(t *testing.T) {
	t.Setenv("CHIRON_DATA_DIR", t.TempDir())
	seen := make(map[string]struct{}, 2000)
	for i := 0; i < 2000; i++ {
		got := NextID()
		if got == "" {
			t.Fatal("NextID 返回空串")
		}
		if _, dup := seen[got]; dup {
			t.Fatalf("NextID 重复：%q", got)
		}
		seen[got] = struct{}{}
	}
}
