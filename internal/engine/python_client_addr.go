package engine

// 本文件由 python_client.go 按职责外移而来（地址路由与故障转移）。
// 代码逐字未改：只搬位置，不改逻辑。

import (
	"context"
	"hash/fnv"
	"log/slog"
	"sort"
	"strings"
	"sync/atomic"
	"time"
)

// pyAddrEntry 单个引擎地址及其熔断冷却状态（Unix 秒，0=正常）。
type pyAddrEntry struct {
	url           string
	cooldownUntil int64
}
// normalizeURLs 清洗地址列表：去空白、补 http:// 前缀、去重保序。
func normalizeURLs(in []string) []string {
	seen := make(map[string]struct{}, len(in))
	out := make([]string, 0, len(in))
	for _, raw := range in {
		a := strings.TrimSpace(raw)
		if a == "" {
			continue
		}
		if !strings.HasPrefix(a, "http://") && !strings.HasPrefix(a, "https://") {
			a = "http://" + a
		}
		if _, dup := seen[a]; dup {
			continue
		}
		seen[a] = struct{}{}
		out = append(out, a)
	}
	return out
}
// snapshot 返回地址表当前快照（锁内复制）。
func (c *PythonClient) snapshot() []pyAddrEntry {
	c.mu.RLock()
	defer c.mu.RUnlock()
	out := make([]pyAddrEntry, len(c.addrs))
	copy(out, c.addrs)
	return out
}
// StaticAddresses 返回构造时的静态地址列表（发现回退用）。
func (c *PythonClient) StaticAddresses() []string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return append([]string(nil), c.staticAddrs...)
}
// SetAddresses 全量替换引擎地址表（StartEngineDiscovery 调用）。
// 保留仍存在地址的冷却状态；地址顺序即优先级顺序。返回是否发生变化。
func (c *PythonClient) SetAddresses(urls []string) bool {
	list := normalizeURLs(urls)
	c.mu.Lock()
	defer c.mu.Unlock()

	// 与当前表比较
	if len(list) == len(c.addrs) {
		same := true
		for i := range list {
			if c.addrs[i].url != list[i] {
				same = false
				break
			}
		}
		if same {
			return false
		}
	}

	old := make(map[string]int64, len(c.addrs))
	for _, e := range c.addrs {
		old[e.url] = e.cooldownUntil
	}
	next := make([]pyAddrEntry, len(list))
	for i, u := range list {
		next[i] = pyAddrEntry{url: u, cooldownUntil: old[u]}
	}
	c.addrs = next
	return true
}
// markFailure 记录地址失败，进入冷却
func (c *PythonClient) markFailure(addr string) {
	until := time.Now().Add(pythonCooldown).Unix()
	c.mu.Lock()
	defer c.mu.Unlock()
	for i := range c.addrs {
		if c.addrs[i].url == addr {
			c.addrs[i].cooldownUntil = until
			return
		}
	}
}
// markSuccess 清除地址冷却
func (c *PythonClient) markSuccess(addr string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for i := range c.addrs {
		if c.addrs[i].url == addr {
			c.addrs[i].cooldownUntil = 0
			return
		}
	}
}
// pickAddress returns the next address using round-robin.
func (c *PythonClient) pickAddress() string {
	entries := c.snapshot()
	if len(entries) == 0 {
		return "http://localhost:8000"
	}
	now := time.Now().Unix()
	start := int(atomic.AddUint64(&c.counter, 1)) % len(entries)
	for i := 0; i < len(entries); i++ {
		idx := (start + i) % len(entries)
		if entries[idx].cooldownUntil <= now {
			return entries[idx].url
		}
	}
	// 全部地址冷却中：选择最快冷却完成的地址，避免选到不可用地址
	earliestIdx := 0
	for i := 1; i < len(entries); i++ {
		if entries[i].cooldownUntil < entries[earliestIdx].cooldownUntil {
			earliestIdx = i
		}
	}
	return entries[earliestIdx].url
}
// addressFor 选择本次请求的引擎地址。
// ctx 携带 session（WithSession）→ 一致性哈希固定实例（确定性、跨网关一致）；
// 无 session → round-robin（pickAddress）。目标实例冷却中顺序探测下一健康实例
// （故障漂移，记录日志）；全部冷却回退最快冷却完成地址。
func (c *PythonClient) addressFor(ctx context.Context) string {
	entries := c.snapshot()
	if len(entries) == 0 {
		return c.pickAddress()
	}
	key := sessionFromCtx(ctx)
	if key == "" {
		return c.pickAddress()
	}
	// 归属优先（批次 4），**仅对显式要求归属的请求**（审批/取消，见 WithRunAffinity）：
	// 引擎把 session 的 run owner 写进 Redis（engine:run:{session}，TTL 300s），
	// 命中且该实例在当前地址表中健康时直连——一致性哈希在扩缩容/副本上下线后会漂移，
	// 会把审批与取消送到并不持有该 run 的实例。
	// 普通对话请求不查 Redis（A1）：归属查询在请求路径上，不能让 Redis 往返成为
	// 每个请求的固定成本；查询失败一律走下方哈希回退。
	if runAffinityFromCtx(ctx) {
		if url, rec, ok := RunOwnerURL(ctx, key); ok {
			if c.isKnownHealthy(url) {
				return url
			}
			slog.Info("python engine: run owner instance unavailable, falling back to hash",
				"session", key[:min(len(key), 16)],
				"owner_instance", rec.InstanceID, "owner_url", url)
		}
	}
	now := time.Now().Unix()
	h := fnv.New32a()
	_, _ = h.Write([]byte(key))
	start := int(h.Sum32()) % len(entries)
	for i := 0; i < len(entries); i++ {
		idx := (start + i) % len(entries)
		if entries[idx].cooldownUntil <= now {
			if i > 0 {
				slog.Info("python engine: session affinity drifted",
					"session", key[:min(len(key), 16)], "from", entries[start].url, "to", entries[idx].url)
			}
			return entries[idx].url
		}
	}
	// 全部冷却中：取最快冷却完成的地址
	earliestIdx := 0
	for i := 1; i < len(entries); i++ {
		if entries[i].cooldownUntil < entries[earliestIdx].cooldownUntil {
			earliestIdx = i
		}
	}
	return entries[earliestIdx].url
}
// sortStrings 排序去重辅助（discovery 用）。
func sortUnique(in []string) []string {
	if len(in) == 0 {
		return nil
	}
	sort.Strings(in)
	out := in[:1]
	for _, s := range in[1:] {
		if s != out[len(out)-1] {
			out = append(out, s)
		}
	}
	return out
}
