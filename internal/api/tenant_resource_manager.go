package api

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

// ── TenantResourceManager 租户资源隔离管理器 ──────────────────────────
//
// 集中管理每个租户的并发执行上限与存储配额，补全现有限流体系：
//   - 并发限流：TenantRateLimiter（QPS）+ DistributedRateLimiter（RPM）
//     → 新增 per-tenant 并发上限（SharedSemaphore，Redis 跨实例共享计数）
//   - 存储配额：按租户限制存储使用量（基于 media 表聚合）
//
// **配置来源（多副本一致性的关键）**：配额不再是各副本各自的进程内状态，而是以 PG
// 表 ent_quota_pools 为权威（resource_type = 'concurrency' / 'storage_mb'，与成本
// 中心的配额池同表），由 StartSync 周期性重载到本实例缓存。因此任一副本改配额，
// 其它副本在下一个同步周期内生效，不会出现"同一租户在不同副本上并发上限不同"的裂脑。
//
// 设计原则：默认不限制（未配置或 0 = 不限制），仅配置了配额才激活。

// tenantSemaphoreAcquireTimeout 单次租户信号量获取的 Redis 操作上限。
// Redis 抖动时不应让 /submit 长时间阻塞（正常一次 Lua 往返是亚毫秒级）。
const tenantSemaphoreAcquireTimeout = 1 * time.Second

// tenantResourceSyncDefault 配额同步默认周期（多副本一致性窗口的上界）。
const tenantResourceSyncDefault = 5 * time.Minute

// maxInt 用于 int64 → int 转换的上界校验（防止 32 位平台溢出）。
const maxInt = int64(^uint(0) >> 1)

// TenantResourceConfig 租户资源配置（来自 ent_quota_pools 或显式 SetQuota）
type TenantResourceConfig struct {
	TenantID        string
	MaxConcurrency  int   // 并发 agent/worker 上限（0 = 不限制）
	MaxStorageBytes int64 // 存储配额（0 = 不限制）
	MaxStorageFiles int   // 文件数量上限（0 = 不限制）
}

// TenantResourceManager 租户资源管理器
type TenantResourceManager struct {
	mu         sync.RWMutex
	rdb        db.RedisClient // 可为 nil（纯本地兜底）
	quotas     map[string]*TenantResourceConfig
	semaphores map[string]*SharedSemaphore // tenantID → 并发信号量（Redis 共享计数）
}

// NewTenantResourceManager 创建租户资源管理器。
//
// 全局并发闸门由 /submit 的 agentSem 负责（SharedSemaphore "agent"），此处只负责
// 租户维度，因此不再做第二个全局信号量回退。历史签名中的 globalConcurrency 参数已
// 废弃，仅保留以兼容既有调用点（传入值被忽略）。
func NewTenantResourceManager(rdb db.RedisClient, _ ...int) *TenantResourceManager {
	return &TenantResourceManager{
		rdb:        rdb,
		quotas:     make(map[string]*TenantResourceConfig),
		semaphores: make(map[string]*SharedSemaphore),
	}
}

// StartCleanup 旧名兼容：语义已从"清空信号量"改为"周期性同步 ent_quota_pools 配额"，
// 故更名为 StartSync 以名实相符；保留本别名以免破坏既有调用点。
//
// Deprecated: use StartSync.
func (trm *TenantResourceManager) StartCleanup(ctx context.Context, interval time.Duration) {
	trm.StartSync(ctx, interval)
}

// StartSync 启动配额同步循环：周期性从 ent_quota_pools 重载各租户配额，并按最新配额
// 重建/淘汰租户信号量（受 ctx 控制，随网关生命周期结束）。
//
// 取代原先的 StartCleanup —— 原实现每个周期把 semaphores 整体清空，会让已配置的租户
// 并发上限被静默丢弃（退化到全局闸门），是"租户隔离运行一段时间后失效"的根因。
func (trm *TenantResourceManager) StartSync(ctx context.Context, interval time.Duration) {
	if interval <= 0 {
		interval = tenantResourceSyncDefault
	}
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		trm.SyncFromDB(ctx) // 启动即同步一次，避免"等一个周期才有配额"
		slog.Info("tenant resource sync started", "interval", interval.String())
		for {
			select {
			case <-ctx.Done():
				slog.Info("tenant resource sync stopped")
				return
			case <-ticker.C:
				trm.SyncFromDB(ctx)
			}
		}
	}()
}

// SyncFromDB 从 ent_quota_pools 重载配额并同步信号量（跨实例权威来源）。
//
// 查询失败时保留现有配额（不清空），仅记录告警：配额属于"限制"类配置，暂时读不到时
// 应维持上一次的有效值，而不是放开限制。
func (trm *TenantResourceManager) SyncFromDB(ctx context.Context) {
	if db.GlobalDBManager == nil {
		return
	}
	cctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
	defer cancel()
	rows, err := db.GlobalDBManager.Query(cctx,
		`SELECT tenant_id::text, resource_type, COALESCE(total_amount, 0)
		   FROM ent_quota_pools
		  WHERE resource_type IN ('concurrency', 'storage_mb')`)
	if err != nil {
		slog.Warn("tenant resource: sync from ent_quota_pools failed, keeping previous quotas", "error", err)
		return
	}
	defer rows.Close()

	loaded := map[string]*TenantResourceConfig{}
	for rows.Next() {
		var tenantID, resourceType string
		var amount int64
		if err := rows.Scan(&tenantID, &resourceType, &amount); err != nil {
			slog.Warn("tenant resource: scan quota row failed", "error", err)
			continue
		}
		cfg, ok := loaded[tenantID]
		if !ok {
			cfg = &TenantResourceConfig{TenantID: tenantID}
			loaded[tenantID] = cfg
		}
		switch resourceType {
		case "concurrency":
			if amount > 0 && amount <= maxInt {
				cfg.MaxConcurrency = int(amount)
			}
		case "storage_mb":
			if amount > 0 {
				cfg.MaxStorageBytes = amount * 1024 * 1024
			}
		}
	}
	if err := rows.Err(); err != nil {
		slog.Warn("tenant resource: iterate quota rows failed, keeping previous quotas", "error", err)
		return
	}

	trm.mu.Lock()
	defer trm.mu.Unlock()
	trm.quotas = loaded
	// 同步信号量：配额变更则重建、配额删除则淘汰。绝不整体清空 —— 那等于静默放开
	// 所有租户的并发上限。
	for tenantID, sem := range trm.semaphores {
		cfg, ok := loaded[tenantID]
		if !ok || cfg.MaxConcurrency <= 0 || sem.limit != int64(cfg.MaxConcurrency) {
			delete(trm.semaphores, tenantID)
		}
	}
	for tenantID, cfg := range loaded {
		if cfg.MaxConcurrency <= 0 {
			continue
		}
		if _, ok := trm.semaphores[tenantID]; !ok {
			trm.semaphores[tenantID] = NewSharedSemaphore(trm.rdb, "tenant:"+tenantID, cfg.MaxConcurrency)
		}
	}
	slog.Debug("tenant resource: quotas synced", "tenants", len(loaded))
}

// SetQuota 显式设置租户资源配置。
// 多副本生产环境以 ent_quota_pools 为权威（由 SyncFromDB 覆盖），本方法供单测与
// 本地覆盖使用。
func (trm *TenantResourceManager) SetQuota(cfg TenantResourceConfig) {
	trm.mu.Lock()
	defer trm.mu.Unlock()
	c := cfg
	trm.quotas[c.TenantID] = &c
	// 并发上限变更时重建信号量（Redis 共享计数）
	if c.MaxConcurrency > 0 {
		trm.semaphores[c.TenantID] = NewSharedSemaphore(trm.rdb, "tenant:"+c.TenantID, c.MaxConcurrency)
	} else {
		delete(trm.semaphores, c.TenantID)
	}
	slog.Info("tenant resource quota set",
		"tenant", c.TenantID,
		"max_concurrency", c.MaxConcurrency,
		"max_storage_bytes", c.MaxStorageBytes,
	)
}

// GetQuota 获取租户资源配置
func (trm *TenantResourceManager) GetQuota(tenantID string) *TenantResourceConfig {
	trm.mu.RLock()
	defer trm.mu.RUnlock()
	return trm.quotas[tenantID]
}

// RemoveQuota 移除租户资源配置
func (trm *TenantResourceManager) RemoveQuota(tenantID string) {
	trm.mu.Lock()
	defer trm.mu.Unlock()
	delete(trm.quotas, tenantID)
	delete(trm.semaphores, tenantID)
}

// Acquire 尝试获取租户并发执行许可（非阻塞）。返回释放函数（必须调用）。
//
// 未配置并发上限的租户恒放行：全局并发闸门已由 /submit 的 agentSem 负责，此处不再
// 回退第二个全局信号量（那会与 agentSem 形成重复的全局限制）。
func (trm *TenantResourceManager) Acquire(tenantID string) (release func(), acquired bool) {
	trm.mu.RLock()
	sem := trm.semaphores[tenantID]
	trm.mu.RUnlock()
	if sem == nil {
		return func() {}, true
	}
	ctx, cancel := context.WithTimeout(context.Background(), tenantSemaphoreAcquireTimeout)
	defer cancel()
	return sem.TryAcquire(ctx)
}

// CheckStorageQuota 检查租户存储配额是否允许添加指定大小的文件
// 需要 DB 查询当前用量，仅在配额配置 > 0 时生效
func (trm *TenantResourceManager) CheckStorageQuota(ctx context.Context, tenantID string, fileSize int64) (allowed bool, used int64, limit int64) {
	trm.mu.RLock()
	cfg, ok := trm.quotas[tenantID]
	trm.mu.RUnlock()

	if !ok || cfg.MaxStorageBytes <= 0 {
		return true, 0, 0 // 未配置存储配额
	}

	// 查询当前存储用量
	var currentBytes int64
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COALESCE(SUM(file_size), 0) FROM media WHERE tenant_id = $1`, tenantID).Scan(&currentBytes)
	if err != nil {
		slog.Warn("tenant resource: check storage quota failed", "tenant", tenantID, "error", err)
		return true, 0, cfg.MaxStorageBytes // fail-open
	}

	if currentBytes+fileSize > cfg.MaxStorageBytes {
		return false, currentBytes, cfg.MaxStorageBytes
	}
	return true, currentBytes, cfg.MaxStorageBytes
}

// CheckFileCountQuota 检查租户文件数量是否超限
func (trm *TenantResourceManager) CheckFileCountQuota(ctx context.Context, tenantID string) (allowed bool, current int, limit int) {
	trm.mu.RLock()
	cfg, ok := trm.quotas[tenantID]
	trm.mu.RUnlock()

	if !ok || cfg.MaxStorageFiles <= 0 {
		return true, 0, 0
	}

	var currentCount int
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COUNT(*) FROM media WHERE tenant_id = $1`, tenantID).Scan(&currentCount)
	if err != nil {
		slog.Warn("tenant resource: check file count quota failed", "tenant", tenantID, "error", err)
		return true, 0, cfg.MaxStorageFiles
	}

	if currentCount >= cfg.MaxStorageFiles {
		return false, currentCount, cfg.MaxStorageFiles
	}
	return true, currentCount, cfg.MaxStorageFiles
}

// ── 上传路径的配额门禁 ──────────────────────────────────────────────
//
// 背景：CheckStorageQuota / CheckFileCountQuota 此前**没有任何调用点**，租户存储配额
// 形同虚设（只写进配置表，从不生效）。这里给上传 handler 提供统一入口。
//
// 为什么用包级变量而非穿透构造函数：配额检查散布在 MediaHandler / UploadHandler 的
// 多个方法里，显式注入要改一批构造签名；包级单例与既有的 db.GlobalDBManager 做法一致。
// 未注入时（单测 / CLI）视为"无配额管理器" ⇒ 放行。
var tenantResMgrForQuota *TenantResourceManager

// SetTenantResourceManager 由网关启动时注入（见 gateway_router.go）。
func SetTenantResourceManager(m *TenantResourceManager) { tenantResMgrForQuota = m }

// CheckUploadAllowed 是上传路径的统一配额门禁：同时检查存储容量与文件数量。
// 返回 (是否放行, 拒绝原因)；未配置管理器或租户为空时放行。
func CheckUploadAllowed(ctx context.Context, tenantID string, fileSize int64) (bool, string) {
	m := tenantResMgrForQuota
	if m == nil || tenantID == "" {
		return true, ""
	}
	if allowed, used, limit := m.CheckStorageQuota(ctx, tenantID, fileSize); !allowed {
		return false, fmt.Sprintf("租户存储配额不足：已用 %d 字节，上限 %d 字节，本次上传 %d 字节", used, limit, fileSize)
	}
	if allowed, current, limit := m.CheckFileCountQuota(ctx, tenantID); !allowed {
		return false, fmt.Sprintf("租户文件数量已达上限：%d / %d", current, limit)
	}
	return true, ""
}
