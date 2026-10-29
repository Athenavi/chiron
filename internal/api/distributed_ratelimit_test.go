package api

import (
	"errors"
	"net/http"
	"net/http/httptest"
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

// ── 429 与 503 必须分开（P0-2）────────────────────────────────────────
//
// 此前 `Allow` 对两种情况都返回普通 error，中间件一律当成"限流服务不可用" ⇒ **503**，
// 于是下面那个 429 分支永远不可达。后果不是文案难看，而是**动作给错了**：
// 用户超额时被告知"服务坏了"、客户端按 503 判定为不可重试 —— 恰恰放弃了正确的做法
// （等一会儿原样重发）。这两条断言把两类失败钉死在各自的出口上。

func TestRateLimitDecision_ExceededIsDistinguishable(t *testing.T) {
	for _, result := range []string{"global", "tenant", "user"} {
		allowed, err := rateLimitDecision(result, "t1", "u1")
		if allowed {
			t.Fatalf("%s 桶命中时不该放行", result)
		}
		if !errors.Is(err, ErrRateLimitExceeded) {
			t.Fatalf("%s 桶命中必须回 ErrRateLimitExceeded（否则中间件会当服务故障回 503），得到 %v",
				result, err)
		}
	}
}

func TestRateLimitDecision_AllowsWhenNoBucketHit(t *testing.T) {
	allowed, err := rateLimitDecision("ok", "t1", "u1")
	if !allowed || err != nil {
		t.Fatalf("未命中任何桶必须放行且无错，得到 allowed=%v err=%v", allowed, err)
	}
}

func TestWriteRateLimitRejection_ExceededGives429WithWait(t *testing.T) {
	rec := httptest.NewRecorder()
	handled := writeRateLimitRejection(rec,
		errors.Join(ErrRateLimitExceeded, errors.New("租户 t1 请求频率超限")), "u1", "/submit")

	if !handled {
		t.Fatal("超额必须由本函数写出响应")
	}
	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("超额必须是 429，得到 %d（503 等于告诉用户「服务坏了」）", rec.Code)
	}
	if got := rec.Header().Get("Retry-After"); got != "60" {
		t.Fatalf("Retry-After = %q, want 60", got)
	}
	if body := decodeBody(t, rec)["code"]; body != CodeRateLimited {
		t.Fatalf("code = %v, want %s", body, CodeRateLimited)
	}
}

func TestWriteRateLimitRejection_ServiceFailureGives503(t *testing.T) {
	rec := httptest.NewRecorder()
	handled := writeRateLimitRejection(rec,
		errors.New("限流服务暂时不可用: dial tcp: connection refused"), "u1", "/submit")

	if !handled {
		t.Fatal("服务故障必须由本函数写出响应")
	}
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("Redis 故障必须是 503，得到 %d", rec.Code)
	}
	// 不能给 Retry-After：我们并不知道什么时候能恢复，编一个秒数只会误导客户端的退避策略
	if got := rec.Header().Get("Retry-After"); got != "" {
		t.Fatalf("服务故障不该给 Retry-After，得到 %q", got)
	}
}

func TestWriteRateLimitRejection_PassesThroughOnNoError(t *testing.T) {
	rec := httptest.NewRecorder()
	if writeRateLimitRejection(rec, nil, "u1", "/submit") {
		t.Fatal("没有错误时不该写响应")
	}
	if rec.Body.Len() != 0 {
		t.Fatalf("没有错误时不该写 body，得到 %q", rec.Body.String())
	}
}
