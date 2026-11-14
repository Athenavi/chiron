package engine

import (
	"context"
	"encoding/json"
	"log/slog"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

// ── 引擎实例自动发现──
//
// 背景：网关的引擎地址来自静态 PYTHON_ENGINE_ADDRESS，扩容/缩容需人工同步并滚动重启。
// 本模块每 15s 从 Redis 引擎注册表（python-engine 启动后写入
// {REDIS_KEY_PREFIX}engine:instance:{instanceID}，TTL 60s + 心跳续期）拉取存活引擎，
// 动态更新 PythonClient 地址表：
//   - 注册表非空 → 动态优先（全量替换，扩容缩容自动生效）；
//   - 注册表为空 / Redis 不可用 → 回退构造时的静态地址（PYTHON_ENGINE_ADDRESS），
//     与未启用发现时的行为一致。

const (
	engineDiscoveryInterval = 15 * time.Second
	engineDiscoveryTimeout  = 3 * time.Second
)

// engineInstanceRecord 与 python-engine/app/engine_registry.py 写入的 JSON 对应。
type engineInstanceRecord struct {
	URL      string `json:"url"`
	Version  string `json:"version,omitempty"`
	LastSeen string `json:"last_seen,omitempty"`
}

// StartEngineDiscovery 启动引擎发现循环（每网关实例一次；Redis/client 为空则跳过）。
func StartEngineDiscovery(ctx context.Context, rdb db.RedisClient, client *PythonClient) {
	// 顺带注入 session→实例归属映射（engine:run:*，批次 4）读取所用的同一 Redis 客户端。
	// Redis 不可用时为 nil，RunOwner 直接返回 false，路由回退一致性哈希。
	SetAffinityRedis(rdb)
	if rdb == nil || client == nil {
		return
	}
	go func() {
		ticker := time.NewTicker(engineDiscoveryInterval)
		defer ticker.Stop()
		slog.Info("engine discovery started",
			"interval_seconds", int(engineDiscoveryInterval.Seconds()))
		for {
			select {
			case <-ctx.Done():
				slog.Info("engine discovery stopped")
				return
			case <-ticker.C:
				applyDiscovery(ctx, rdb, client)
			}
		}
	}()
}

// applyDiscovery 扫描注册表并应用动态/静态地址。
func applyDiscovery(ctx context.Context, rdb db.RedisClient, client *PythonClient) {
	dctx, cancel := context.WithTimeout(ctx, engineDiscoveryTimeout)
	defer cancel()

	prefix := db.RedisKey("engine:instance:")
	urls := make([]string, 0, 4)
	var cursor uint64
	for {
		keys, next, err := rdb.Scan(dctx, cursor, prefix+"*", 100).Result()
		if err != nil {
			slog.Debug("engine discovery scan failed", "error", err)
			break
		}
		for _, k := range keys {
			data, err := rdb.Get(dctx, k).Bytes()
			if err != nil {
				continue // TTL 过期/被删
			}
			var rec engineInstanceRecord
			if err := json.Unmarshal(data, &rec); err != nil || rec.URL == "" {
				continue
			}
			urls = append(urls, rec.URL)
		}
		cursor = next
		if cursor == 0 {
			break
		}
	}

	urls = sortUnique(urls)
	if len(urls) > 0 {
		if client.SetAddresses(urls) {
			slog.Info("engine addresses updated via discovery", "instances", len(urls))
		}
		return
	}
	// 注册表为空（引擎未启用注册/全部下线）：回退静态地址
	static := client.StaticAddresses()
	if len(static) > 0 && client.SetAddresses(static) {
		slog.Warn("engine registry empty; falling back to static addresses", "instances", len(static))
	}
}
