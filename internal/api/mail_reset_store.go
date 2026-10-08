package api

import (
	"context"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

// resetTokenStore 抽象"密码重置令牌"的存取（生产 Redis，测试内存 fake）。
type resetTokenStore interface {
	// Available 报告后端是否可用（Redis 未接时密码重置直接 503，不静默失效）。
	Available() bool
	Put(ctx context.Context, tokenHash, userID string, ttl time.Duration) error
	// Take 原子地取出并删除令牌；不存在/已用过返回空串。
	// 必须原子：否则同一链接可被并发点击两次，等于多发一次改密机会。
	Take(ctx context.Context, tokenHash string) (string, error)
	Del(ctx context.Context, tokenHash string) error
}

// redisResetTokenStore 是 resetTokenStore 的 Redis 实现。
//
// 库里只存令牌的 SHA-256：即便 Redis 快照/监控泄露，也无法直接拿去改密码。
type redisResetTokenStore struct {
	rdb db.RedisClient
}

// resetTokenTakeScript 返回令牌值并删除（GET+DEL 的原子等价物）。
const resetTokenTakeScript = `local v = redis.call('GET', KEYS[1])
if v then redis.call('DEL', KEYS[1]) end
return v`

func (s redisResetTokenStore) Available() bool { return s.rdb != nil }

func (s redisResetTokenStore) Put(ctx context.Context, tokenHash, userID string, ttl time.Duration) error {
	if s.rdb == nil {
		return errCodeStoreUnavailable
	}
	return s.rdb.Set(ctx, mailResetKeyPrefix+tokenHash, userID, ttl).Err()
}

func (s redisResetTokenStore) Take(ctx context.Context, tokenHash string) (string, error) {
	if s.rdb == nil {
		return "", errCodeStoreUnavailable
	}
	raw, err := s.rdb.Eval(ctx, resetTokenTakeScript, []string{mailResetKeyPrefix + tokenHash}).Result()
	if err != nil {
		// 键不存在时 Redis 返回 nil 错误，等同于"令牌无效"
		return "", nil
	}
	userID, ok := raw.(string)
	if !ok {
		return "", nil
	}
	return userID, nil
}

func (s redisResetTokenStore) Del(ctx context.Context, tokenHash string) error {
	if s.rdb == nil {
		return errCodeStoreUnavailable
	}
	return s.rdb.Del(ctx, mailResetKeyPrefix+tokenHash).Err()
}
