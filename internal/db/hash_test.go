package db

import "testing"

// TestParseHashReply 锁死跨 go-redis 版本的 HGETALL 解析。
//
// 回归背景（真实故障）：go-redis **v9** 的 `Do(ctx, "HGETALL", key)` 返回
// `map[interface{}]interface{}`，而 `internal/api/model_discovery.go` 的
// keysetActiveProviders 一直按 v8 的扁平 `[]interface{}` 断言 → `ok == false`
// 且不报错 → 解析出 **0 个 provider** → 模型发现循环一次都没进过，
// `/v1/models` 永远返回空列表（前端模型下拉"暂无数据"），日志里连一条 warn 都没有。
func TestParseHashReply(t *testing.T) {
	v9Payload := `{"k":"sk-x","s":"active"}`

	// 1) v9 真实形态：map[interface{}]interface{}
	v9 := map[interface{}]interface{}{"abc123": v9Payload}
	if got := ParseHashReply(v9); got["abc123"] != v9Payload {
		t.Fatalf("v9 map 解析失败: %#v", got)
	}

	// 2) RESP3 形态：map[string]interface{}
	if got := ParseHashReply(map[string]interface{}{"f": "v"}); got["f"] != "v" {
		t.Fatalf("RESP3 map 解析失败: %#v", got)
	}

	// 3) v8 扁平数组（保留兼容，避免回退版本时再炸一次）
	got := ParseHashReply([]interface{}{"f1", "v1", "f2", "v2"})
	if got["f1"] != "v1" || got["f2"] != "v2" {
		t.Fatalf("扁平数组解析失败: %#v", got)
	}

	// 4) map[string]string / []string 形态
	if got := ParseHashReply(map[string]string{"a": "b"}); got["a"] != "b" {
		t.Fatalf("map[string]string 解析失败: %#v", got)
	}
	if got := ParseHashReply([]string{"a", "b"}); got["a"] != "b" {
		t.Fatalf("[]string 解析失败: %#v", got)
	}

	// 5) 空/未知形态 → 空 map，绝不 panic（调用方按"没数据"降级）
	if len(ParseHashReply(nil)) != 0 {
		t.Fatal("nil 应返回空 map")
	}
	if len(ParseHashReply(42)) != 0 {
		t.Fatal("未知形态应返回空 map")
	}
	// 6) 奇数长度数组：只解析完整对，不越界
	if got := ParseHashReply([]interface{}{"only"}); len(got) != 0 {
		t.Fatalf("奇数长度应解析出空 map: %#v", got)
	}
}

// TestHashAllNilClient 保证 rdb 为 nil 时静默降级（调用方的"Redis 不可用也不崩"约定）。
func TestHashAllNilClient(t *testing.T) {
	got, err := HashAll(nil, nil, "any")
	if err != nil {
		t.Fatalf("nil client 不应报错: %v", err)
	}
	if len(got) != 0 {
		t.Fatalf("nil client 应返回空 map: %#v", got)
	}
}
