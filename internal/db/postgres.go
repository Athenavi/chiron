package db

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/athenavi/chiron/internal/monitor"
)

// PoolMu guards Pool for concurrent access (e.g. hot-reload reconnection).
var PoolMu sync.RWMutex

// Pool is the global PostgreSQL connection pool.
// Protected by PoolMu for concurrent read/write access.
var Pool *pgxpool.Pool

func ConnectPostgres(ctx context.Context, dsn string, maxConn, minConn int) error {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return fmt.Errorf("pgx parse config: %w", err)
	}

	cfg.MaxConns = int32(maxConn)
	cfg.MinConns = int32(minConn)
	cfg.MaxConnLifetime = 30 * time.Minute
	cfg.MaxConnIdleTime = 5 * time.Minute
	cfg.HealthCheckPeriod = 30 * time.Second

	// 设置默认事务隔离级别为 READ COMMITTED（平衡一致性和性能）
	cfg.ConnConfig.RuntimeParams["default_transaction_isolation"] = "read committed"

	// P 性能/稳定：statement_timeout 防慢查询长期占用连接耗尽池
	// 长查询（迁移/批量）应走独立连接，不复用业务池
	cfg.AfterConnect = func(ctx context.Context, conn *pgx.Conn) error {
		_, err := conn.Exec(ctx, "SET statement_timeout = 30000")
		return err
	}

	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return fmt.Errorf("pgx new pool: %w", err)
	}

	// Verify connection
	pingCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if err := pool.Ping(pingCtx); err != nil {
		pool.Close()
		return fmt.Errorf("pgx ping: %w", err)
	}

	PoolMu.Lock()
	Pool = pool
	PoolMu.Unlock()
	slog.Info("postgres connected", "max_conns", maxConn, "min_conns", minConn)

	// 连接池容量与 PG 上限的关系（企业化扩容的关键约束）：
	// 网关与引擎各自持有连接池、都连同一个 PG，因此
	//     N_网关 × MaxConns + M_引擎 × DB_POOL_MAX_SIZE ≤ PG max_connections
	// 应用无法知道集群里跑着多少个实例，但可以把自己这一份与 PG 上限一起打出来，
	// 让运维扩容时能直接算，而不是等到"连接被拒"才发现。
	logPoolCapacity(ctx, pool, maxConn)

	// 注册连接池监控到全局 metrics
	monitor.RegisterExtraStats(PoolStats)
	return nil
}

// pgMaxConnections 读 PG 的 max_connections；读不到时返回 0（不阻断启动）。
func pgMaxConnections(ctx context.Context, pool *pgxpool.Pool) int {
	qctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	var n int
	if err := pool.QueryRow(qctx,
		"SELECT current_setting('max_connections')::int").Scan(&n); err != nil {
		slog.Debug("read pg max_connections failed", "error", err)
		return 0
	}
	return n
}

// logPoolCapacity 打出"本实例池上限 / PG 上限 / 按此池大小能容纳多少实例"。
// 实例数偏少时额外告警 —— 扩容到该数量以上就会撞 PG 的 max_connections。
func logPoolCapacity(ctx context.Context, pool *pgxpool.Pool, maxConn int) {
	if maxConn <= 0 {
		return
	}
	pgMax := pgMaxConnections(ctx, pool)
	if pgMax <= 0 {
		return
	}
	instances := pgMax / maxConn
	slog.Info("postgres pool capacity",
		"pool_max_conns", maxConn,
		"pg_max_connections", pgMax,
		"instances_supported", instances)
	if instances < 4 {
		slog.Warn("postgres pool headroom is thin for horizontal scaling: "+
			"gateway and engine each hold their own pool against the same PG "+
			"(engine side is DB_POOL_MAX_SIZE)",
			"pool_max_conns", maxConn,
			"pg_max_connections", pgMax,
			"instances_supported", instances)
	}
}

func ClosePostgres() {
	PoolMu.Lock()
	p := Pool
	Pool = nil
	PoolMu.Unlock()
	if p != nil {
		p.Close()
		slog.Info("postgres disconnected")
	}
}

// ReadPool returns the best available pool for read operations.
// If a DatabaseRouter with read replicas is configured, returns a healthy replica.
// Otherwise falls back to the primary Pool.
//
// ⚠️ 只读副本一致性：
// 安全/钱包关键读（RBAC 权限、支付订单、余额）必须走主库 db.Pool，
// 禁止使用本函数——副本延迟窗口会造成降权不即时 / "unknown order" / 余额回旧。
// 仅统计/历史/展示类读允许走副本。
func ReadPool() *pgxpool.Pool {
	PoolMu.RLock()
	p := Pool
	PoolMu.RUnlock()
	if Router != nil {
		return Router.Read()
	}
	return p
}

// PoolStats returns current PostgreSQL connection pool statistics for monitoring.
func PoolStats() map[string]interface{} {
	PoolMu.RLock()
	p := Pool
	PoolMu.RUnlock()
	if p == nil {
		return nil
	}
	s := p.Stat()
	return map[string]interface{}{
		"total_conns":         s.TotalConns(),
		"idle_conns":          s.IdleConns(),
		"acquired_conns":      s.AcquiredConns(),
		"empty_acquire":       s.EmptyAcquireCount(),
		"acquire_duration_ms": s.AcquireDuration().Milliseconds(),
	}
}
