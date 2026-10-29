package api

import (
	"sync"
	"testing"
	"time"
)

// fakeClock 是可控时钟：节流逻辑依赖时间流逝，真等一分钟的测试没人会跑。
type fakeClock struct {
	mu sync.Mutex
	t  time.Time
}

func newFakeClock() *fakeClock {
	return &fakeClock{t: time.Date(2026, 10, 29, 12, 0, 0, 0, time.UTC)}
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *fakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
}

func newTestTracker(minGap time.Duration) (*apiKeyUsageTracker, *fakeClock) {
	clock := newFakeClock()
	tr := newAPIKeyUsageTracker(minGap)
	tr.now = clock.Now
	return tr, clock
}

// 同一把 key 在间隔内只写一次 —— 这是节流存在的全部意义。
func TestAPIKeyUsageTracker_ThrottlesWithinGap(t *testing.T) {
	tr, clock := newTestTracker(time.Minute)

	if !tr.allow("key-1") {
		t.Fatal("第一次调用应当允许写库")
	}
	// 间隔内连续 9 次都不该写
	for i := 0; i < 9; i++ {
		clock.Advance(5 * time.Second)
		if tr.allow("key-1") {
			t.Fatalf("第 %d 次调用仍在 %v 间隔内，不应允许写库", i+2, time.Minute)
		}
	}
}

// 间隔一过就要重新允许 —— 否则 last_used_at 会永久停在第一次的时间。
func TestAPIKeyUsageTracker_AllowsAfterGap(t *testing.T) {
	tr, clock := newTestTracker(time.Minute)

	if !tr.allow("key-1") {
		t.Fatal("第一次调用应当允许写库")
	}
	clock.Advance(time.Minute - time.Millisecond)
	if tr.allow("key-1") {
		t.Fatal("差 1ms 未满间隔，不应允许")
	}
	clock.Advance(time.Millisecond)
	if !tr.allow("key-1") {
		t.Fatal("正好满间隔，应当允许写库")
	}
}

// 不同 key 之间互不影响：一把高频 key 不该把另一把的写入吃掉。
func TestAPIKeyUsageTracker_IsolatesKeys(t *testing.T) {
	tr, _ := newTestTracker(time.Minute)

	if !tr.allow("key-1") {
		t.Fatal("key-1 第一次应允许")
	}
	if !tr.allow("key-2") {
		t.Fatal("key-2 是另一把 key，不应被 key-1 节流")
	}
}

// 空 keyID 一律不写：调用方没拿到 id 时不该往库里写 NULL。
func TestAPIKeyUsageTracker_RejectsEmptyID(t *testing.T) {
	tr, _ := newTestTracker(time.Minute)
	if tr.allow("") {
		t.Fatal("空 keyID 不应允许写库")
	}
}

// 条目数到上限时要清理过期条目，否则进程长跑会累积成内存泄漏。
func TestAPIKeyUsageTracker_EvictsWhenFull(t *testing.T) {
	tr, clock := newTestTracker(time.Minute)

	// 填满到上限
	for i := 0; i < apiKeyUsageTrackerMaxEntries; i++ {
		if !tr.allow(string(rune('a')) + string(rune(i))) {
			t.Fatalf("第 %d 个 key 第一次应允许", i)
		}
	}
	if len(tr.last) < apiKeyUsageTrackerMaxEntries {
		t.Fatalf("期望填满到 %d 条，实际 %d", apiKeyUsageTrackerMaxEntries, len(tr.last))
	}

	// 全部过期后，再来一个新 key 应触发清理
	clock.Advance(2 * time.Minute)
	if !tr.allow("brand-new") {
		t.Fatal("新 key 第一次应允许")
	}
	if len(tr.last) > 1 {
		t.Fatalf("过期条目应被清理，期望只剩 1 条，实际 %d", len(tr.last))
	}
}

// 并发安全：同一把 key 同时到达多个请求时，只能有一个写库。
func TestAPIKeyUsageTracker_ConcurrentSameKey(t *testing.T) {
	tr, _ := newTestTracker(time.Minute)

	const goroutines = 50
	var wg sync.WaitGroup
	var mu sync.Mutex
	allowed := 0
	wg.Add(goroutines)
	for i := 0; i < goroutines; i++ {
		go func() {
			defer wg.Done()
			if tr.allow("shared-key") {
				mu.Lock()
				allowed++
				mu.Unlock()
			}
		}()
	}
	wg.Wait()

	if allowed != 1 {
		t.Fatalf("同一把 key 的 %d 个并发请求只能有 1 个写库，实际 %d", goroutines, allowed)
	}
}
