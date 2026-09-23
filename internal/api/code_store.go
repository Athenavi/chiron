package api

import (
	"context"
	"errors"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

// ── 验证码存储（Redis）──────────────────────────────────
//
// 短信验证码与邮箱验证码共用同一套语义：
//
//	一次性验证码 + 尝试计数 + 发送冷却 + 每日上限
//
// 唯一的差异是 Redis 键前缀，因此把它参数化成一个实现，而不是每个通道各抄一份。
// 键名一律经 db.RedisKey 加环境前缀（多套环境共用同一 Redis 时隔离键空间）。

// 验证码存储的统一时间窗口。
const (
	// codeTriesTTL 尝试计数的存活时间：与验证码有效期上限一致，
	// 过期后计数归零，避免"旧计数"永久锁死一个标识。
	codeTriesTTL = 15 * time.Minute
	// codeDailyWindow 每日发送计数的滚动窗口。
	codeDailyWindow = 24 * time.Hour
)

// codeStore 抽象验证码存取（生产 Redis，测试内存 fake）。
// id 为标识本身（手机号 / 邮箱）。
type codeStore interface {
	SetCode(ctx context.Context, id, code string, ttl time.Duration) error
	GetCode(ctx context.Context, id string) (string, error)
	DelCode(ctx context.Context, id string) error
	IncrTries(ctx context.Context, id string) (int, error)
	ResetTries(ctx context.Context, id string) error
	MarkCooldown(ctx context.Context, id string, ttl time.Duration) error
	InCooldown(ctx context.Context, id string) (bool, error)
	IncrDaily(ctx context.Context, id string) (int, error)
}

// redisCodeStore 是 codeStore 的 Redis 实现。
type redisCodeStore struct {
	rdb         db.RedisClient
	codePrefix  string
	triesPrefix string
	coolPrefix  string
	dailyPrefix string
}

// newRedisCodeStore 按命名空间（如 "sms:" / "mail:"）构造存取器。
func newRedisCodeStore(rdb db.RedisClient, namespace string) redisCodeStore {
	return redisCodeStore{
		rdb:         rdb,
		codePrefix:  db.RedisKey(namespace + "code:"),
		triesPrefix: db.RedisKey(namespace + "tries:"),
		coolPrefix:  db.RedisKey(namespace + "cool:"),
		dailyPrefix: db.RedisKey(namespace + "day:"),
	}
}

func (s redisCodeStore) SetCode(ctx context.Context, id, code string, ttl time.Duration) error {
	if s.rdb == nil {
		return errors.New("code store: redis unavailable")
	}
	return s.rdb.Set(ctx, s.codePrefix+id, code, ttl).Err()
}

func (s redisCodeStore) GetCode(ctx context.Context, id string) (string, error) {
	if s.rdb == nil {
		return "", errors.New("code store: redis unavailable")
	}
	v, err := s.rdb.Get(ctx, s.codePrefix+id).Result()
	if err != nil {
		// 过期/不存在视为空码
		return "", nil
	}
	return v, nil
}

func (s redisCodeStore) DelCode(ctx context.Context, id string) error {
	if s.rdb == nil {
		return errors.New("code store: redis unavailable")
	}
	return s.rdb.Del(ctx, s.codePrefix+id).Err()
}

func (s redisCodeStore) IncrTries(ctx context.Context, id string) (int, error) {
	if s.rdb == nil {
		return 0, errors.New("code store: redis unavailable")
	}
	key := s.triesPrefix + id
	n, err := s.rdb.Incr(ctx, key).Result()
	if err != nil {
		return 0, err
	}
	if n == 1 {
		s.rdb.Expire(ctx, key, codeTriesTTL)
	}
	return int(n), nil
}

func (s redisCodeStore) ResetTries(ctx context.Context, id string) error {
	if s.rdb == nil {
		return errors.New("code store: redis unavailable")
	}
	return s.rdb.Del(ctx, s.triesPrefix+id).Err()
}

func (s redisCodeStore) MarkCooldown(ctx context.Context, id string, ttl time.Duration) error {
	if s.rdb == nil {
		return errors.New("code store: redis unavailable")
	}
	return s.rdb.Set(ctx, s.coolPrefix+id, "1", ttl).Err()
}

func (s redisCodeStore) InCooldown(ctx context.Context, id string) (bool, error) {
	if s.rdb == nil {
		return false, errors.New("code store: redis unavailable")
	}
	n, err := s.rdb.Exists(ctx, s.coolPrefix+id).Result()
	if err != nil {
		return false, err
	}
	return n > 0, nil
}

func (s redisCodeStore) IncrDaily(ctx context.Context, id string) (int, error) {
	if s.rdb == nil {
		return 0, errors.New("code store: redis unavailable")
	}
	key := s.dailyPrefix + id
	n, err := s.rdb.Incr(ctx, key).Result()
	if err != nil {
		return 0, err
	}
	if n == 1 {
		s.rdb.Expire(ctx, key, codeDailyWindow)
	}
	return int(n), nil
}
