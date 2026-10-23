package bloom

import (
	"fmt"
	"math"
	"reflect"
	"sync"
	"testing"
)

// TestNoFalseNegatives 是布隆过滤器唯一不可让步的保证：Add 过的元素 Test 必须为真。
func TestNoFalseNegatives(t *testing.T) {
	f := New(1000, 0.01)
	for i := 0; i < 1000; i++ {
		f.Add([]byte(fmt.Sprintf("user%d@example.com", i)))
	}
	for i := 0; i < 1000; i++ {
		if !f.Test([]byte(fmt.Sprintf("user%d@example.com", i))) {
			t.Fatalf("false negative for user%d@example.com", i)
		}
	}
}

// TestFalsePositiveRate 校验实测误报率不超过目标值一个宽松倍数。
func TestFalsePositiveRate(t *testing.T) {
	const n = 10000
	const target = 0.01
	f := New(n, target)

	for i := 0; i < n; i++ {
		f.Add([]byte(fmt.Sprintf("member-%d", i)))
	}

	probes := 50000
	blocked := 0
	for i := n; i < n+probes; i++ {
		if f.Test([]byte(fmt.Sprintf("member-%d", i))) {
			blocked++
		}
	}
	rate := float64(blocked) / float64(probes)
	if rate > target*3 {
		t.Fatalf("false positive rate %.4f exceeds 3x target %.4f", rate, target)
	}
	t.Logf("false positive rate = %.4f (target %.4f), bits=%d hashes=%d", rate, target, f.Bits(), f.Hashes())
}

// TestTestAndAdd 校验同一个写锁内 Test+Add 的原子语义。
func TestTestAndAdd(t *testing.T) {
	f := New(100, 0.01)
	if existed := f.TestAndAdd([]byte("a@b.com")); existed {
		t.Fatal("first TestAndAdd should report not-existed")
	}
	if existed := f.TestAndAdd([]byte("a@b.com")); !existed {
		t.Fatal("second TestAndAdd should report existed")
	}
	if got := f.Added(); got != 1 {
		t.Fatalf("Added() = %d, want 1", got)
	}
	// 重复 Add 不应累加计数。
	f.Add([]byte("a@b.com"))
	if got := f.Added(); got != 1 {
		t.Fatalf("Added() after duplicate Add = %d, want 1", got)
	}
}

// TestConcurrent 并发读写，交由 -race 检测数据竞争。
func TestConcurrent(t *testing.T) {
	f := New(4096, 0.01)
	var wg sync.WaitGroup
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for i := 0; i < 500; i++ {
				key := []byte(fmt.Sprintf("g%d-i%d", g, i))
				f.Add(key)
				_ = f.Test(key)
				_ = f.EstimateCount()
				_ = f.FillRatio()
			}
		}(g)
	}
	wg.Wait()
}

// TestConstructorBoundaries 校验非法参数的回退与位长的 2 的幂不变式。
func TestConstructorBoundaries(t *testing.T) {
	cases := []struct {
		name     string
		capacity uint64
		fpRate   float64
	}{
		{"zero capacity", 0, 0.01},
		{"zero fp rate", 100, 0},
		{"fp rate >= 1", 100, 1.5},
		{"negative fp rate", 100, -0.2},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f := New(c.capacity, c.fpRate)
			if f.Bits() < minBits || f.Bits()&(f.Bits()-1) != 0 {
				t.Fatalf("bits=%d not a power of two >= %d", f.Bits(), minBits)
			}
			if f.Hashes() < minHashes || f.Hashes() > maxHashes {
				t.Fatalf("hashes=%d out of [%d,%d]", f.Hashes(), minHashes, maxHashes)
			}
			if f.FPRate() <= 0 || f.FPRate() >= 1 {
				t.Fatalf("fp rate %v not normalized into (0,1)", f.FPRate())
			}
		})
	}
}

// TestDeterministic 同一个输入必须跨实例得到相同位布局（哈希不能带随机种子），
// 这是"多副本对同一邮箱给出相同判定"的前提。
func TestDeterministic(t *testing.T) {
	data := []byte("same@example.com")

	h1a, h2a := hashPair(data)
	h1b, h2b := hashPair(data)
	if h1a != h1b || h2a != h2b {
		t.Fatal("hashPair must be deterministic across calls")
	}

	a, b := New(1000, 0.01), New(1000, 0.01)
	a.Add(data)
	b.Add(data)
	if !reflect.DeepEqual(a.bits, b.bits) {
		t.Fatal("identical inputs must produce identical bit layouts across instances")
	}
	if !a.Test(data) || !b.Test(data) {
		t.Fatal("both instances must report an added element as present")
	}
	// 未写入的实例对同一条日志未必一致，但一个全新元素被误判为"存在"的概率应很低。
	if b.Test([]byte("never-inserted@example.com")) {
		t.Log("unexpected (but legal) false positive on a nearly-empty filter")
	}
}

// TestOccupancyMetrics 校验饱和度与计数估计的单调性/量级。
func TestOccupancyMetrics(t *testing.T) {
	f := New(1000, 0.01)
	for i := 0; i < 1000; i++ {
		f.Add([]byte(fmt.Sprintf("x-%d", i)))
	}
	if r := f.FillRatio(); r <= 0 || r >= 1 {
		t.Fatalf("fill ratio %v out of (0,1)", r)
	}
	est := f.EstimateCount()
	if math.Abs(est-1000)/1000 > 0.3 {
		t.Fatalf("estimate %.0f too far from 1000", est)
	}
	if got := f.Added(); got != 1000 {
		t.Fatalf("Added() = %d, want 1000", got)
	}
}

// TestEmptyFilter 空过滤器对任何输入都返回 false（无假阴性保证的平凡推论）。
func TestEmptyFilter(t *testing.T) {
	f := New(1000, 0.01)
	if f.Test([]byte("anything")) {
		t.Fatal("empty filter must report every element as absent")
	}
	if f.EstimateCount() != 0 {
		t.Fatalf("empty filter estimate = %v, want 0", f.EstimateCount())
	}
}
