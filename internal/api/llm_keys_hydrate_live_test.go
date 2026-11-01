package api

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/settings"
)

// 真实 Redis + PostgreSQL 用例：M52 启动回填的**镜像语义 + 失效通知**。
//
// 现场（打包桌面态，2026-11-01）：管理端保存 key 只在那一次请求里镜像到 Redis，网关启动
// 不回填；而打包态的 Redis 是 `--appendonly no --save ""` 的每启每空。结果引擎 lifespan
// 里扫不到 keyset ⇒ provider 注册表为空 ⇒ 每条回合立刻 `no LLM provider available`。
// 纯函数单测证明不了这条链：只有真库真缓存能证明"重启后运行层里真的有这把 key"，
// 以及"运行中的引擎真的被告知要重读"。
//
// 为什么用例只喂 hydrateKeysetsForProviders 而不直接跑 HydrateProviderKeysets：后者会
// 对**全表**每个 provider 重解重镜像，测试用的 APP_SECRET 解不开开发者本机已有的密文，
// 按"解不开就不进缓存"的语义会把它们清空 —— 用例不该有这种副作用。
// 全表那一步（DISTINCT + 缺表跳过）由 TestLiveDistinctLLMKeyProviders 与
// llm_keys_hydrate_test.go 的闸门用例分别覆盖。

const hydrateTestSecret = "m52-live-test-app-secret"

func seedLLMKeyRow(t *testing.T, ctx context.Context, provider, plain, status string, weight int) string {
	t.Helper()
	cipher, err := settings.New(db.Pool, hydrateTestSecret).EncryptString(plain)
	if err != nil {
		t.Fatalf("加密测试 key 失败: %v", err)
	}
	keyHash := llmKeyHash(provider, plain)
	if _, err := db.Pool.Exec(ctx,
		`INSERT INTO llm_provider_keys (id, provider, encrypted_key, key_hash, status, weight, created_at, updated_at)
		 VALUES (gen_random_uuid()::text, $1, $2, $3, $4, $5, NOW(), NOW())`,
		provider, cipher, keyHash, status, weight); err != nil {
		t.Fatalf("播种 llm_provider_keys 失败（provider=%s status=%s）: %v", provider, status, err)
	}
	return keyHash
}

func TestLiveHydrateKeysetsMirrorsAuthorityAndNotifies(t *testing.T) {
	withLiveRedis(t)
	withLiveModelPool(t)
	ctx := context.Background()

	provider := "test-hydrate-" + uuid.NewString()[:8]
	hashKey := llmKeysHashPrefix + provider
	cfg := &config.Config{AppSecret: hydrateTestSecret}

	cleanup := func() {
		bg := context.Background()
		if _, err := db.Pool.Exec(bg, `DELETE FROM llm_provider_keys WHERE provider = $1`, provider); err != nil {
			t.Logf("清理 llm_provider_keys(%s) 失败: %v", provider, err)
		}
		_ = db.Redis.Del(bg, hashKey).Err()
	}
	cleanup()
	t.Cleanup(cleanup)

	// 两条权威记录：active 带权重 3、circuit_open 权重 0。
	// 非 active 也必须被镜像 —— 状态是 keyset 里的 `s` 字段，过滤由引擎 KeyRing 做；
	// 只镜像 active 等于"禁用一把 key 后重启就复活"。
	activeHash := seedLLMKeyRow(t, ctx, provider, "sk-hydrate-active", "active", 3)
	openHash := seedLLMKeyRow(t, ctx, provider, "sk-hydrate-open", "circuit_open", 0)

	// 幽灵字段：上一份运行残留（或已从库里删掉的那把 key）。
	if err := db.Redis.Do(ctx, "HSET", hashKey, "ghost00000000", `{"k":"sk-revoked","s":"active","w":1}`).Err(); err != nil {
		t.Fatalf("预置幽灵字段失败: %v", err)
	}

	// 先订阅再回填：回填必须**同时**告诉运行中的引擎重读，否则云端多实例态下
	// 引擎的 provider 表还停留在旧内容（本里程碑 M52b 依赖这条通知）。
	pubsub := db.Redis.Subscribe(ctx, llmKeysChangedChannel)
	if pubsub == nil {
		t.Fatal("Subscribe 返回 nil")
	}
	defer func() { _ = pubsub.Close() }()
	// 吃掉订阅确认，确保订阅已生效再触发 publish。
	confirmCtx, confirmCancel := context.WithTimeout(ctx, 5*time.Second)
	_, err := pubsub.Receive(confirmCtx)
	confirmCancel()
	if err != nil {
		t.Fatalf("订阅 %s 失败: %v", llmKeysChangedChannel, err)
	}

	verBefore, _ := db.Redis.Get(ctx, llmKeysVerKey).Int64()

	n := hydrateKeysetsForProviders(ctx, cfg, []string{provider})
	if n != 1 {
		t.Fatalf("单个 provider 回填应计数 1，实际 %d", n)
	}

	notifyCtx, notifyCancel := context.WithTimeout(ctx, 10*time.Second)
	defer notifyCancel()
	msg, err := pubsub.ReceiveMessage(notifyCtx)
	if err != nil {
		t.Fatalf("回填后必须有 llm:keys:changed 通知（引擎靠它失效旧 provider 表）: %v", err)
	}
	if msg.Payload != provider {
		t.Fatalf("通知内容应为 provider %q，实际 %q", provider, msg.Payload)
	}

	verAfter, _ := db.Redis.Get(ctx, llmKeysVerKey).Int64()
	if verAfter <= verBefore {
		t.Fatalf("回填必须推进版本号：%d → %d", verBefore, verAfter)
	}

	got := hGetAllFields(t, hashKey)
	if len(got) != 2 {
		t.Fatalf("回填后应只有权威层的 2 条，实际 %+v", got)
	}
	if _, bad := got["ghost00000000"]; bad {
		t.Fatal("幽灵字段必须被整体替换清除：已撤销的 key 不得继续被服务")
	}
	for _, tc := range []struct {
		hash, wantKey, wantStatus string
		wantWeight                int
	}{
		{activeHash, "sk-hydrate-active", "active", 3},
		{openHash, "sk-hydrate-open", "circuit_open", 0},
	} {
		field := tc.hash[:12]
		raw, ok := got[field]
		if !ok {
			t.Fatalf("keyset 缺少字段 %s（%s），实际 %+v", field, tc.wantKey, got)
		}
		var payload struct {
			K string `json:"k"`
			S string `json:"s"`
			W int    `json:"w"`
		}
		if err := json.Unmarshal([]byte(raw), &payload); err != nil {
			t.Fatalf("字段 %s 不是预期 JSON: %q (%v)", field, raw, err)
		}
		if payload.K != tc.wantKey || payload.S != tc.wantStatus || payload.W != tc.wantWeight {
			t.Fatalf("字段 %s 内容不对: 期望 {k:%s s:%s w:%d}，实际 %+v",
				field, tc.wantKey, tc.wantStatus, tc.wantWeight, payload)
		}
	}

	// 权威层清空 ⇒ 镜像必须清空：否则"删掉的 key"会在下一次重启后被空 Redis 反证为"还在"。
	if _, err := db.Pool.Exec(ctx, `DELETE FROM llm_provider_keys WHERE provider = $1`, provider); err != nil {
		t.Fatalf("清空权威层失败: %v", err)
	}
	if n := hydrateKeysetsForProviders(ctx, cfg, []string{provider}); n != 1 {
		t.Fatalf("第二次回填应计数 1，实际 %d", n)
	}
	if remaining := hGetAllFields(t, hashKey); len(remaining) != 0 {
		t.Fatalf("权威层已无 key，镜像必须为空，实际残留 %+v", remaining)
	}
}

func TestLiveDistinctLLMKeyProvidersDedupsAndKeepsInactive(t *testing.T) {
	withLiveModelPool(t)
	ctx := context.Background()

	// 只读用例：不接 Redis，因此不会镜像任何东西，只证明"回填名单从权威层来、
	// 且同一 provider 只进一次（含非 active 行）"。
	provider := "test-distinct-" + uuid.NewString()[:8]
	cleanup := func() {
		if _, err := db.Pool.Exec(context.Background(),
			`DELETE FROM llm_provider_keys WHERE provider = $1`, provider); err != nil {
			t.Logf("清理 llm_provider_keys(%s) 失败: %v", provider, err)
		}
	}
	t.Cleanup(cleanup)
	seedLLMKeyRow(t, ctx, provider, "sk-d1", "active", 1)
	seedLLMKeyRow(t, ctx, provider, "sk-d2", "rate_limited", 1)

	providers, err := distinctLLMKeyProviders(ctx, db.Pool)
	if err != nil {
		t.Fatalf("distinctLLMKeyProviders 失败: %v", err)
	}
	seen := 0
	for _, p := range providers {
		if p == provider {
			seen++
		}
	}
	if seen != 1 {
		t.Fatalf("同一 provider 的多行应折叠成一次回填，实际出现 %d 次（共 %d 项）", seen, len(providers))
	}
}
