package api

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/db"
	"github.com/jackc/pgx/v5/pgconn"
)

// 启动回填（M52）的**无外部依赖**用例：只钉住闸门与错误分类，真镜像语义见
// llm_keys_live_test.go 的 TestLiveHydrate*。
//
// 为什么这几条值得单独存在：打包桌面态每次换一份空 Redis 起来，回填失败的表现是
// 「网关起来了、引擎没有 provider、每条回合立刻失败」。所以"什么时候必须报错"和
// "什么时候如实跳过"这两个边界，比镜像本身更容易被后来的改动写错。

// stubRedisClient 只为满足"db.Redis 非 nil"这个前置；被测路径在触到它之前就返回。
type stubRedisClient struct{ db.RedisClient }

func withGlobals(t *testing.T, redis db.RedisClient) {
	t.Helper()
	prevRedis, prevPool := db.Redis, db.Pool
	db.Redis = redis
	t.Cleanup(func() { db.Redis, db.Pool = prevRedis, prevPool })
}

func TestHydrateProviderKeysetsTableMissingOnlyMatches42P01(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want bool
	}{
		{"undefined_table", &pgconn.PgError{Code: "42P01", Message: `relation "llm_provider_keys" does not exist`}, true},
		{"wrapped 42P01", fmt.Errorf("query distinct providers: %w", &pgconn.PgError{Code: "42P01"}), true},
		{"undefined_column 不是缺表", &pgconn.PgError{Code: "42703"}, false},
		{"权限错误不是缺表", &pgconn.PgError{Code: "42501"}, false},
		{"普通错误", errors.New("dial tcp: connection refused"), false},
		{"nil", nil, false},
	}
	for _, tc := range cases {
		if got := llmProviderKeysTableMissing(tc.err); got != tc.want {
			t.Errorf("%s: llmProviderKeysTableMissing = %v, want %v", tc.name, got, tc.want)
		}
	}
}

func TestHydrateProviderKeysetsFailSoftWhenRedisAbsent(t *testing.T) {
	prevRedis, prevPool := db.Redis, db.Pool
	defer func() { db.Redis, db.Pool = prevRedis, prevPool }()
	db.Redis, db.Pool = nil, nil

	// 降级态：回填无事可做，但**不能报错**——报错会让启动日志看起来像"密钥丢了"，
	// 而引擎此时按 env 种子走，与 syncProviderKeyset 同一口径。
	n, err := HydrateProviderKeysets(context.Background(), &config.Config{AppSecret: "test-secret"})
	if err != nil || n != 0 {
		t.Fatalf("Redis 缺失时应如实跳过，得到 n=%d err=%v", n, err)
	}
}

func TestHydrateProviderKeysetsFailSoftWhenConfigNil(t *testing.T) {
	withGlobals(t, stubRedisClient{})
	db.Pool = nil

	// cfg 为 nil ⇒ 没有主密钥，解不开任何密文；若继续往下走就会把每个 keyset 都"镜像成空"，
	// 那等于用一次启动抹掉运行层。宁可不做事，也不做坏事。
	n, err := HydrateProviderKeysets(context.Background(), nil)
	if err != nil || n != 0 {
		t.Fatalf("cfg 为 nil 时应返回 (0,nil)，得到 n=%d err=%v", n, err)
	}
}

func TestHydrateProviderKeysetsErrorsWhenPoolMissing(t *testing.T) {
	withGlobals(t, stubRedisClient{})
	db.Pool = nil

	// Redis 在、PG 不在：权威层读不到。这里必须**报错**而不是 (0,nil)，
	// 否则调用方的日志只剩"hydrated 0"，分不清"库里真没 key"和"根本没接库"。
	_, err := HydrateProviderKeysets(context.Background(), &config.Config{AppSecret: "test-secret"})
	if err == nil || !strings.Contains(err.Error(), "PostgreSQL pool") {
		t.Fatalf("PG 缺失应报错并点名 pool，得到 %v", err)
	}
}
