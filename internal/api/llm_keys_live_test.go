package api

import (
	"context"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/db"
	"github.com/google/uuid"
)

// 真实 Redis 用例：keyset 的「整体替换」语义（LLM 密钥缓存陈旧 bug 的回归防线）。
//
// 引擎 KeyRing（app/gateway/key_ring.py `_refresh`）用 `HGETALL llm:keys:{provider}`
// 把整份 hash 当作「当前可用 key」轮询。此前网关写镜像用 HMSET **增量**：一条 key 从
// DB 删掉、只要该 provider 还剩别的 key，它那 12 位指纹字段就永久留在 hash 里 ——
// 于是**一把已撤销的 key 会被继续拿去打上游**。llmReplaceKeyset 改为一次原子 Lua
// DEL+逐对 HSET，正是堵住这个洞。
//
// 取 Redis 的方式与 session_coord_live_test.go 的 withLiveRedis 一致：都取不到则 skip。

func hGetAllFields(t *testing.T, key string) map[string]string {
	t.Helper()
	// RedisClient 接口不暴露 HGetAll，走 Do。go-redis 把 HGETALL 回复解码成
	// map[interface{}]interface{}（field→value），而非 flat 数组。
	res, err := db.Redis.Do(context.Background(), "HGETALL", key).Result()
	if err != nil {
		t.Fatalf("HGETALL %s 失败: %v", key, err)
	}
	raw, ok := res.(map[interface{}]interface{})
	if !ok {
		t.Fatalf("HGETALL 返回类型非预期: %T", res)
	}
	out := make(map[string]string, len(raw))
	for k, v := range raw {
		f, _ := k.(string)
		val, _ := v.(string)
		out[f] = val
	}
	return out
}

func TestLiveLLMReplaceKeysetDropsGhostField(t *testing.T) {
	withLiveRedis(t)
	provider := "pytest-" + uuid.NewString()
	key := llmKeysHashPrefix + provider
	ctx := context.Background()
	t.Cleanup(func() { _ = db.Redis.Del(ctx, key).Err() })

	// 先种一把「幽灵」字段：模拟一把早已从 DB 删除、却因旧的 HMSET 增量写残留在 hash 里的 key。
	if err := db.Redis.Do(ctx, "HSET", key, "ghostdig12ab", `{"k":"sk-should-be-gone","s":"active"}`).Err(); err != nil {
		t.Fatalf("预置幽灵字段失败: %v", err)
	}

	// 换成只含当前权威的一条：替换后幽灵字段必须消失。
	fields := []interface{}{"keeper0001", `{"k":"sk-live","s":"active"}`}
	n, err := llmReplaceKeyset(ctx, provider, fields)
	if err != nil {
		t.Fatalf("llmReplaceKeyset 失败: %v", err)
	}
	if n != 1 {
		t.Fatalf("替换后 HLEN 应为 1，实际 %d", n)
	}
	got := hGetAllFields(t, key)
	if _, bad := got["ghostdig12ab"]; bad {
		t.Fatalf("已撤销的幽灵字段必须被清除，仍在 hash 里: %+v", got)
	}
	if !strings.Contains(got["keeper0001"], "sk-live") {
		t.Fatalf("保留字段内容不对: %q", got["keeper0001"])
	}
}

func TestLiveLLMReplaceKeysetEmptyFieldsWipesHash(t *testing.T) {
	withLiveRedis(t)
	provider := "pytest-" + uuid.NewString()
	key := llmKeysHashPrefix + provider
	ctx := context.Background()
	t.Cleanup(func() { _ = db.Redis.Del(ctx, key).Err() })

	// 该 provider 曾有若干 key（或全部解密失败 ⇒ 权威内容为空）。
	if err := db.Redis.Do(ctx, "HSET", key, "a1", `{"k":"x"}`, "a2", `{"k":"y"}`).Err(); err != nil {
		t.Fatalf("预置字段失败: %v", err)
	}

	// fields 为空 ⇒ 等价于清空：陈旧明文不得继续被引擎服务。
	n, err := llmReplaceKeyset(ctx, provider, nil)
	if err != nil {
		t.Fatalf("llmReplaceKeyset(nil) 失败: %v", err)
	}
	if n != 0 {
		t.Fatalf("清空后 HLEN 应为 0，实际 %d", n)
	}
	if got := hGetAllFields(t, key); len(got) != 0 {
		t.Fatalf("清空后 hash 必须为空，实际残留: %+v", got)
	}
}
