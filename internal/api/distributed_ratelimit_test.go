package api

import (
	"strings"
	"testing"
)

// 「三级桶同槽」这条不变量此前**没有任何测试**，而它的失效形态很隐蔽：
// Redis **Cluster** 下多 key `EVAL` 要求所有 key 同槽，否则 `CROSSSLOT` 直接失败 ⇒
// `Allow` 返回错误 ⇒ 限流 fail-close ⇒ **全站 503 "rate limiter unavailable"**。
// 单机/哨兵模式不会暴露（多 key EVAL 不校验同槽），所以本地跑永远看不出问题 ——
// 而部署文档恰恰推荐生产用 Sentinel/Cluster。

func hashTagOf(key string) string {
	start := strings.Index(key, "{")
	end := strings.Index(key, "}")
	if start < 0 || end <= start {
		return ""
	}
	return key[start : end+1]
}

func TestRateLimitKeysShareOneHashTag(t *testing.T) {
	global, tenant, user := rateLimitKeys("t1", "u1")

	for _, key := range []string{global, tenant, user} {
		if got := hashTagOf(key); got != rateLimitHashTag {
			t.Fatalf("key %q 的 hash tag = %q, want %q（跨槽即 CROSSSLOT ⇒ 全站 503）",
				key, got, rateLimitHashTag)
		}
	}
}

func TestRateLimitKeysAreDistinctPerScope(t *testing.T) {
	global, tenantA, _ := rateLimitKeys("tenant-a", "u1")
	_, tenantB, _ := rateLimitKeys("tenant-b", "u1")
	_, _, userX := rateLimitKeys("t", "user-x")
	_, _, userY := rateLimitKeys("t", "user-y")

	for _, pair := range [][2]string{
		{global, tenantA},
		{tenantA, tenantB},
		{userX, userY},
	} {
		if pair[0] == pair[1] {
			t.Fatalf("不同作用域的 key 必须不同，得到同一个 %q", pair[0])
		}
	}
}

func TestRateLimitKeysOmitsUserBucketWithoutIdentity(t *testing.T) {
	_, _, user := rateLimitKeys("t1", "")

	if user != "" {
		t.Fatalf("无用户身份时 user key 应为空串（Lua 据此跳过该级），得到 %q", user)
	}
}

func TestRateLimitKeysRespectEnvPrefix(t *testing.T) {
	// REDIS_KEY_PREFIX 是既有约定（多环境共用同一 Redis 时防串键）：
	// 加 hash tag **不能**把前缀挤掉，否则多环境会互相污染限流计数。
	t.Setenv("REDIS_KEY_PREFIX", "env1:")

	global, _, _ := rateLimitKeys("t1", "u1")

	if !strings.HasPrefix(global, "env1:") {
		t.Fatalf("key %q 丢失了 REDIS_KEY_PREFIX", global)
	}
	if hashTagOf(global) != rateLimitHashTag {
		t.Fatalf("加前缀后 hash tag 被破坏：%q", global)
	}
}
