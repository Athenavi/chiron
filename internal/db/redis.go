package db

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/redis/go-redis/v9"
)

var (
	redisMu sync.RWMutex
	Redis   RedisClient
)

// ErrRedisNotAvailable Redis 未初始化错误
var ErrRedisNotAvailable = errors.New("redis not available")

// RedisManager 统一 Redis 管理器，集中处理所有 Redis 操作和 nil 检查
type RedisManager struct{}

// NewRedisManager 创建 Redis 管理器实例
func NewRedisManager() *RedisManager {
	return &RedisManager{}
}

// GetClient 获取 Redis 客户端，如果未初始化则返回错误
func (m *RedisManager) GetClient() (RedisClient, error) {
	redisMu.RLock()
	client := Redis
	redisMu.RUnlock()
	if client == nil {
		return nil, ErrRedisNotAvailable
	}
	return client, nil
}

// IsAvailable 检查 Redis 是否可用
func (m *RedisManager) IsAvailable() bool {
	redisMu.RLock()
	client := Redis
	redisMu.RUnlock()
	return client != nil
}

// Get 获取键值
func (m *RedisManager) Get(ctx context.Context, key string) (string, error) {
	client, err := m.GetClient()
	if err != nil {
		return "", fmt.Errorf("redis manager: %w", err)
	}
	cmd := client.Get(ctx, key)
	if cmd.Err() != nil {
		return "", cmd.Err()
	}
	return cmd.Val(), nil
}

// Set 设置键值
func (m *RedisManager) Set(ctx context.Context, key string, value interface{}, expiration time.Duration) error {
	client, err := m.GetClient()
	if err != nil {
		return fmt.Errorf("redis manager: %w", err)
	}
	return client.Set(ctx, key, value, expiration).Err()
}



// Del 删除键
func (m *RedisManager) Del(ctx context.Context, keys ...string) error {
	client, err := m.GetClient()
	if err != nil {
		return fmt.Errorf("redis manager: %w", err)
	}
	return client.Del(ctx, keys...).Err()
}

// Exists 检查键是否存在
func (m *RedisManager) Exists(ctx context.Context, keys ...string) (int64, error) {
	client, err := m.GetClient()
	if err != nil {
		return 0, fmt.Errorf("redis manager: %w", err)
	}
	return client.Exists(ctx, keys...).Val(), nil
}


// Incr 自增
func (m *RedisManager) Incr(ctx context.Context, key string) (int64, error) {
	client, err := m.GetClient()
	if err != nil {
		return 0, fmt.Errorf("redis manager: %w", err)
	}
	return client.Incr(ctx, key).Val(), nil
}

// Ping 检查连接状态
func (m *RedisManager) Ping(ctx context.Context) error {
	client, err := m.GetClient()
	if err != nil {
		return err
	}
	return client.Ping(ctx).Err()
}

// Publish 发布消息到频道
func (m *RedisManager) Publish(ctx context.Context, channel string, message interface{}) error {
	client, err := m.GetClient()
	if err != nil {
		return fmt.Errorf("redis manager: %w", err)
	}
	return client.Publish(ctx, channel, message).Err()
}

// Subscribe 订阅频道
func (m *RedisManager) Subscribe(ctx context.Context, channels ...string) (*PubSubWrapper, error) {
	client, err := m.GetClient()
	if err != nil {
		return nil, fmt.Errorf("redis manager: %w", err)
	}
	pubsub := client.Subscribe(ctx, channels...)
	return &PubSubWrapper{pubsub: pubsub}, nil
}

// Scan 扫描匹配的键
func (m *RedisManager) Scan(ctx context.Context, match string, count int64) ([]string, error) {
	client, err := m.GetClient()
	if err != nil {
		return nil, fmt.Errorf("redis manager: %w", err)
	}

	var keys []string
	var cursor uint64
	for {
		scanCmd := client.Scan(ctx, cursor, match, count)
		if scanCmd.Err() != nil {
			return nil, scanCmd.Err()
		}

		k, nextCursor := scanCmd.Val()
		keys = append(keys, k...)
		cursor = nextCursor
		if cursor == 0 {
			break
		}
	}
	return keys, nil
}


// HealthCheck 健康检查
func (m *RedisManager) HealthCheck(ctx context.Context) map[string]interface{} {
	result := map[string]interface{}{
		"available": m.IsAvailable(),
		"timestamp": time.Now().Unix(),
	}

	if !m.IsAvailable() {
		return result
	}

	client, _ := m.GetClient()
	if client != nil {
		pingCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
		defer cancel()
		err := client.Ping(pingCtx).Err()
		result["ping_ok"] = err == nil
		if err != nil {
			result["ping_error"] = err.Error()
		}

		// 获取统计信息
		if stats := client.Stats(); stats != nil {
			result["stats"] = map[string]interface{}{
				"total_conns": stats.TotalConns,
				"idle_conns":  stats.IdleConns,
				"hits":        stats.Hits,
				"misses":      stats.Misses,
				"timeouts":    stats.Timeouts,
			}
		}
	}

	return result
}

// PubSubWrapper Redis Pub/Sub 包装器
type PubSubWrapper struct {
	pubsub *redis.PubSub
}

// Close 关闭订阅
func (p *PubSubWrapper) Close() error {
	return p.pubsub.Close()
}

// GlobalRedisManager 全局 Redis 管理器实例
var GlobalRedisManager = NewRedisManager()
