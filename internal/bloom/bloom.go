// Package bloom 提供一个零依赖、并发安全的布隆过滤器实现。
//
// 语义（使用前务必理解）：布隆过滤器**只保证否定**。
//
//	Test(x) == false  ⇒ x **一定**没被 Add 过（无假阴性）
//	Test(x) == true   ⇒ x 可能被 Add 过（可能是假阳性）
//
// 正因如此，它**不能作为唯一性的真相源**：调用方只允许在 Test 返回 false 时
// 跳过权威存储的检查，返回 true 时必须回落查询。本项目用它加速注册时的
// 「邮箱是否已存在」判重，真相源始终是 users 表上的唯一索引。
//
// 位数组长度取 2 的幂，因此取模退化为位与；第二个哈希强制为奇数，
// 从而与 2^k 互质，满足 double hashing（Kirsch–Mitzenmacher）成立的前提。
package bloom

import (
	"crypto/sha256"
	"encoding/binary"
	"math"
	"math/bits"
	"sync"
)

// 默认与容量相关的边界值。
const (
	minBits    = 64 // 位数组最小长度（1 个 uint64）
	maxHashes  = 30 // 哈希个数上限：再多收益递减而成本线性上升
	minHashes  = 1
	defaultFPR = 0.01
)

// Filter 是一个并发安全的布隆过滤器。
//
// 读（Test）取共享读锁，写（Add）取排他写锁。零值不可用，必须经 New 构造。
type Filter struct {
	mu sync.RWMutex

	bits []uint64 // 位数组
	m    uint64   // 位数，恒为 2 的幂
	k    uint64   // 每个元素使用的哈希个数

	capacity uint64 // 构造时的预期容量（仅供观测）
	fpRate   float64

	// added 记录 Add 时"此前未曾出现"的元素个数（近似：并发下可能有极小偏差）。
	added uint64
}

// New 按预期容量 capacity 与目标误报率 fpRate 构造过滤器。
//
// capacity <= 0 视为 1；fpRate 不在 (0,1) 开区间时回退到 defaultFPR。
// 位数组长度与哈希个数由经典公式推导：
//
//	m = ceil(-n·ln p / (ln 2)²)   k = round((m/n)·ln 2)
func New(capacity uint64, fpRate float64) *Filter {
	if capacity == 0 {
		capacity = 1
	}
	if !(fpRate > 0 && fpRate < 1) {
		fpRate = defaultFPR
	}

	ln2 := math.Ln2
	mF := -float64(capacity) * math.Log(fpRate) / (ln2 * ln2)
	m := uint64(math.Ceil(mF))
	if m < minBits {
		m = minBits
	}
	m = nextPow2(m)

	k := uint64(math.Round(float64(m) / float64(capacity) * ln2))
	if k < minHashes {
		k = minHashes
	}
	if k > maxHashes {
		k = maxHashes
	}

	return &Filter{
		bits:     make([]uint64, m/64),
		m:        m,
		k:        k,
		capacity: capacity,
		fpRate:   fpRate,
	}
}

// Add 把 data 加入过滤器；若此前不在过滤器中则计数加一。
func (f *Filter) Add(data []byte) {
	h1, h2 := hashPair(data)
	mask := f.m - 1

	f.mu.Lock()
	defer f.mu.Unlock()

	isNew := false
	for i := uint64(0); i < f.k; i++ {
		pos := (h1 + i*h2) & mask
		word, bit := pos>>6, uint64(1)<<(pos&63)
		if f.bits[word]&bit == 0 {
			isNew = true
			f.bits[word] |= bit
		}
	}
	if isNew {
		f.added++
	}
}

// Test 判断 data 是否**可能**存在。返回 false 表示一定不存在。
func (f *Filter) Test(data []byte) bool {
	h1, h2 := hashPair(data)
	mask := f.m - 1

	f.mu.RLock()
	defer f.mu.RUnlock()

	for i := uint64(0); i < f.k; i++ {
		pos := (h1 + i*h2) & mask
		if f.bits[pos>>6]&(uint64(1)<<(pos&63)) == 0 {
			return false
		}
	}
	return true
}

// TestAndAdd 在同一个写锁内完成 Test 与 Add，返回该元素此前是否**已可能存在**。
//
// 用于「原来没有 ⇒ 判定为新元素并登记」这类需要原子性的场景：
// 分开调用 Test/Add 会在两者之间留出竞态窗口。
func (f *Filter) TestAndAdd(data []byte) (existed bool) {
	h1, h2 := hashPair(data)
	mask := f.m - 1

	f.mu.Lock()
	defer f.mu.Unlock()

	missing := false
	for i := uint64(0); i < f.k; i++ {
		pos := (h1 + i*h2) & mask
		word, bit := pos>>6, uint64(1)<<(pos&63)
		if f.bits[word]&bit == 0 {
			missing = true
			f.bits[word] |= bit
		}
	}
	if missing {
		f.added++
		return false
	}
	return true
}

// Bits 返回位数组长度（位）。
func (f *Filter) Bits() uint64 { return f.m }

// Hashes 返回每个元素使用的哈希个数。
func (f *Filter) Hashes() uint64 { return f.k }

// Capacity 返回构造时的预期容量。
func (f *Filter) Capacity() uint64 { return f.capacity }

// FPRate 返回构造时使用的目标误报率。
func (f *Filter) FPRate() float64 { return f.fpRate }

// Added 返回累计登记的非重复元素个数（近似值，并发写入下可能有极小偏差）。
func (f *Filter) Added() uint64 {
	f.mu.RLock()
	defer f.mu.RUnlock()
	return f.added
}

// FillRatio 返回位数组中已置位比特的比例，用于容量规划与饱和度告警。
func (f *Filter) FillRatio() float64 {
	f.mu.RLock()
	defer f.mu.RUnlock()
	return float64(f.setBits()) / float64(f.m)
}

// EstimateCount 依据已置位比特数反推元素数量（n ≈ -(m/k)·ln(1 - X/m)）。
// 当位数组接近充满时该估计会明显高估——它是饱和度信号，不是精确计数。
func (f *Filter) EstimateCount() float64 {
	f.mu.RLock()
	defer f.mu.RUnlock()

	x := float64(f.setBits())
	m := float64(f.m)
	if x <= 0 {
		return 0
	}
	if x >= m {
		return math.Inf(1)
	}
	return -(m / float64(f.k)) * math.Log(1-x/m)
}

// setBits 统计已置位的比特数；调用方必须持有锁。
func (f *Filter) setBits() uint64 {
	var n uint64
	for _, w := range f.bits {
		n += uint64(bits.OnesCount64(w))
	}
	return n
}

// hashPair 由一次 SHA-256 导出两个 64 位哈希。
//
// 用 SHA-256 而非 maphash：后者带进程随机种子，跨实例不可复现——而注册判重
// 需要每个副本对同一邮箱得到相同结论。h2 强制为奇数且非零，以保证与 2 的幂位长互质。
func hashPair(data []byte) (h1, h2 uint64) {
	sum := sha256.Sum256(data)
	h1 = binary.LittleEndian.Uint64(sum[0:8])
	h2 = binary.LittleEndian.Uint64(sum[8:16])
	if h2 == 0 {
		h2 = 0x9e3779b97f4a7c15 // 任意非零常量
	}
	h2 |= 1 // 奇数
	return h1, h2
}

// nextPow2 返回 >= v 的最小 2 的幂。
func nextPow2(v uint64) uint64 {
	if v <= 1 {
		return 1
	}
	v--
	v |= v >> 1
	v |= v >> 2
	v |= v >> 4
	v |= v >> 8
	v |= v >> 16
	v |= v >> 32
	return v + 1
}
