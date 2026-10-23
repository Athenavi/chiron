package api

import (
	"context"
	"log/slog"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/athenavi/chiron/internal/bloom"
	"github.com/athenavi/chiron/internal/db"
)

// ── 注册邮箱判重的布隆过滤器（按租户分片）──
//
// 动机：注册流程里那次 `SELECT COUNT(*) FROM users WHERE email = $1 AND tenant_id = $2`
// 在大用户量下是昂贵操作（users 上 email 无索引时更是一次全表扫描），而"注册一个
// 全新邮箱"恰恰是绝大多数请求。布隆过滤器把这类请求挡在 DB 之前。
//
// 语义 —— 只做**快速否定**，这是本组件唯一的正确性契约：
//
//	MightExist == false ⇒ 该租户下这个邮箱一定没注册过 ⇒ 可安全跳过 DB 判重
//	MightExist == true  ⇒ 可能已注册（也可能是假阳性）⇒ 必须回落 DB 查证
//
// 真相源始终是 users(tenant_id, email) 唯一索引，**不是**本过滤器。任何导致过滤器
// "漏记"的情形（预热未完成、跨实例广播延迟、实例重启、Add 失败）都只会让判定退化为
// 回落 DB，或由唯一索引挡下重复写入 —— 绝不因此放进一个重复邮箱。
const (
	emailBloomChannelSuffix = "users:email:bloom:sync"
	emailBloomWarmupBatch   = 2000
	emailBloomMinCapacity   = 1024
)

// bloomRows 是预热扫描所需的最小行集接口。
// 抽象出来是为了让 Warmup 可在无数据库的单元测试中用内存 fake 驱动。
type bloomRows interface {
	Next() bool
	Scan(dest ...any) error
	Close()
	Err() error
}

// bloomQuerier 是预热扫描所需的最小查询接口（生产实现见 pgBloomQuerier）。
type bloomQuerier interface {
	Query(ctx context.Context, sql string, args ...any) (bloomRows, error)
}

// pgBloomQuerier 把 db.GlobalDBManager 适配成 bloomQuerier。
type pgBloomQuerier struct{}

func (pgBloomQuerier) Query(ctx context.Context, sql string, args ...any) (bloomRows, error) {
	rows, err := db.GlobalDBManager.Query(ctx, sql, args...)
	if err != nil {
		return nil, err
	}
	return rows, nil
}

// userEmailBloom 是按租户分片的注册邮箱布隆过滤器门面。
// 零值不可用，必须经 newUserEmailBloom 构造；方法对 nil 接收者安全（等同"未启用"）。
type userEmailBloom struct {
	mu       sync.RWMutex
	shards   map[string]*bloom.Filter
	capacity uint64 // 默认分片容量（预热前出现的租户按此建）
	fpRate   float64
	ready    atomic.Bool // 预热完成前一律保守判定为"可能存在"

	skipped  atomic.Uint64 // 因"一定不存在"而跳过的 DB 判重次数
	fellback atomic.Uint64 // 因"可能存在"而回落 DB 的次数
}

func newUserEmailBloom(capacity uint64, fpRate float64) *userEmailBloom {
	if capacity < emailBloomMinCapacity {
		capacity = emailBloomMinCapacity
	}
	return &userEmailBloom{
		shards:   make(map[string]*bloom.Filter),
		capacity: capacity,
		fpRate:   fpRate,
	}
}

// emailBloomKey 以 NUL 分隔租户与邮箱，避免 "a"+"bc@x" 与 "ab"+"c@x" 这类拼接歧义。
func emailBloomKey(tenantID, email string) []byte {
	buf := make([]byte, 0, len(tenantID)+1+len(email))
	buf = append(buf, tenantID...)
	buf = append(buf, 0)
	buf = append(buf, email...)
	return buf
}

// MightExist 判定某租户下该邮箱是否**可能**已注册。
// 返回 false 表示一定不存在，调用方可跳过 DB 判重；返回 true 表示必须回落查证。
func (b *userEmailBloom) MightExist(tenantID, email string) bool {
	if b == nil || tenantID == "" || email == "" {
		return true // 未启用或输入不足 ⇒ 一律回落，绝不冒险跳过
	}
	if !b.ready.Load() {
		return true // 预热完成前保守回落：此时过滤器只记了增量，缺失存量
	}
	f := b.shard(tenantID)
	if f == nil {
		return false // 该租户从未在过滤器中出现过 ⇒ 一定没有
	}
	if f.Test(emailBloomKey(tenantID, email)) {
		b.fellback.Add(1)
		return true
	}
	b.skipped.Add(1)
	return false
}

// Add 登记某租户下的一个邮箱（注册成功后调用，也用于预热与跨实例广播）。
func (b *userEmailBloom) Add(tenantID, email string) {
	if b == nil || tenantID == "" || email == "" {
		return
	}
	b.getOrCreate(tenantID, b.capacity).Add(emailBloomKey(tenantID, email))
}

// shard 只读地取出某租户的分片，不存在返回 nil。
func (b *userEmailBloom) shard(tenantID string) *bloom.Filter {
	b.mu.RLock()
	f := b.shards[tenantID]
	b.mu.RUnlock()
	return f
}

// getOrCreate 取出（或创建）某租户的分片。capacity <= 0 时用该租户的默认容量。
// 已存在的分片一律复用 —— 重建会把已登记的增量丢回去。
func (b *userEmailBloom) getOrCreate(tenantID string, capacity uint64) *bloom.Filter {
	if f := b.shard(tenantID); f != nil {
		return f
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if f := b.shards[tenantID]; f != nil {
		return f
	}
	if capacity <= 0 {
		capacity = b.capacity
	}
	f := bloom.New(capacity, b.fpRate)
	b.shards[tenantID] = f
	return f
}

// Ready 报告预热是否已完成。未完成时 MightExist 恒为 true。
func (b *userEmailBloom) Ready() bool { return b != nil && b.ready.Load() }

// ShardCount 返回当前分片（租户）数量。
func (b *userEmailBloom) ShardCount() int {
	if b == nil {
		return 0
	}
	b.mu.RLock()
	defer b.mu.RUnlock()
	return len(b.shards)
}

// BloomStats 是过滤器的运行观测快照。
type BloomStats struct {
	Ready             bool
	Shards            int
	SkippedDBLookups  uint64
	FellbackDBLookups uint64
}

// Stats 返回运行观测快照，便于管理端点/日志暴露命中率。
func (b *userEmailBloom) Stats() BloomStats {
	if b == nil {
		return BloomStats{}
	}
	return BloomStats{
		Ready:             b.ready.Load(),
		Shards:            b.ShardCount(),
		SkippedDBLookups:  b.skipped.Load(),
		FellbackDBLookups: b.fellback.Load(),
	}
}

// Warmup 从 users 表后台重建过滤器，完成后标记 ready。
//
// 两遍扫描：先按租户统计规模以分配恰好的分片容量（避免所有分片都按最大容量吃内存），
// 再按 id 键集分页填充（避免大表上的 OFFSET 退化）。
//
// 任何一步失败都**不**标记 ready —— 宁可保持"预热完成前一律回落 DB"的保守态，
// 也不要带着残缺的过滤器开始跳过判重。唯一索引始终兜底。
func (b *userEmailBloom) Warmup(ctx context.Context, q bloomQuerier) {
	if b == nil || q == nil {
		return
	}
	start := time.Now()

	if err := b.warmupSizing(ctx, q); err != nil {
		slog.Warn("email bloom warmup: sizing query failed, using uniform capacity", "error", err)
	}

	total := 0
	lastID := ""
	for {
		if ctx.Err() != nil {
			slog.Info("email bloom warmup aborted by context", "entries", total)
			return
		}
		rows, err := q.Query(ctx,
			`SELECT id, tenant_id, email FROM users
			  WHERE id > $1 AND email IS NOT NULL AND email <> '' AND tenant_id IS NOT NULL
			  ORDER BY id LIMIT $2`, lastID, emailBloomWarmupBatch)
		if err != nil {
			slog.Warn("email bloom warmup: scan failed, staying in conservative mode", "error", err)
			return
		}

		n := 0
		for rows.Next() {
			var id, tenant, email string
			if err := rows.Scan(&id, &tenant, &email); err != nil {
				continue
			}
			b.Add(tenant, email)
			lastID = id
			n++
			total++
		}
		rows.Close()
		if err := rows.Err(); err != nil {
			slog.Warn("email bloom warmup: row iteration failed, staying in conservative mode", "error", err)
			return
		}
		if n < emailBloomWarmupBatch {
			break
		}
	}

	b.ready.Store(true)
	slog.Info("email bloom warmup complete",
		"entries", total, "shards", b.ShardCount(), "duration", time.Since(start).String())
}

// warmupSizing 为每个租户按实际邮箱数预建分片（容量留 1.5 倍余量）。
func (b *userEmailBloom) warmupSizing(ctx context.Context, q bloomQuerier) error {
	rows, err := q.Query(ctx,
		`SELECT tenant_id, COUNT(*) FROM users
		  WHERE email IS NOT NULL AND email <> '' AND tenant_id IS NOT NULL
		  GROUP BY tenant_id`)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var tenant string
		var cnt int64
		if err := rows.Scan(&tenant, &cnt); err != nil {
			continue
		}
		if cnt < 0 {
			cnt = 0
		}
		b.getOrCreate(tenant, uint64(float64(cnt)*1.5)+emailBloomMinCapacity)
	}
	return rows.Err()
}

// broadcastUserEmailBloomAdd 向其他实例广播"某租户下新增了一个邮箱"。
func broadcastUserEmailBloomAdd(tenantID, email string) {
	if db.Redis == nil || tenantID == "" || email == "" {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	payload := tenantID + "\t" + email
	if err := db.Redis.Publish(ctx, db.RedisKey(emailBloomChannelSuffix), payload).Err(); err != nil {
		slog.Warn("failed to broadcast email bloom add", "error", err)
	}
}

// parseEmailBloomBroadcast 解析广播载荷 "tenantID\temail"。
// ok=false 表示载荷格式非法 —— 跳过即可，漏收只会让本实例回落 DB，不影响正确性。
func parseEmailBloomBroadcast(payload string) (tenantID, email string, ok bool) {
	tenantID, email, found := strings.Cut(payload, "\t")
	if !found || tenantID == "" || email == "" {
		return "", "", false
	}
	return tenantID, email, true
}

// StartUserEmailBloomSubscriber 启动 Redis Pub/Sub 监听器，把其他实例登记的邮箱
// 并入本实例的过滤器。形态与 StartBlacklistPubSub 一致：一个 goroutine + 一条订阅。
//
// 订阅本身是"尽力而为"：漏收只会让本实例判定退化为回落 DB，不影响正确性。
func StartUserEmailBloomSubscriber(ctx context.Context, b *userEmailBloom) {
	if b == nil {
		return
	}
	if db.Redis == nil {
		slog.Warn("Redis not available, email bloom cross-instance sync disabled")
		return
	}

	go func() {
		pubsub := db.Redis.Subscribe(ctx, db.RedisKey(emailBloomChannelSuffix))
		defer pubsub.Close()

		ch := pubsub.Channel()
		slog.Info("email bloom Pub/Sub listener started")

		for {
			select {
			case <-ctx.Done():
				return
			case msg, ok := <-ch:
				if !ok {
					slog.Warn("email bloom Pub/Sub channel closed")
					return
				}
				tenant, email, ok := parseEmailBloomBroadcast(msg.Payload)
				if !ok {
					continue
				}
				b.Add(tenant, email)
			}
		}
	}()
}
