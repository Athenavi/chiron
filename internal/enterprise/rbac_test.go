package enterprise

import (
	"testing"
)

// 这一组钉的是**防越权的语义区分**（见 LoadEffectivePerms 的注释）：
//   - nil 切片 = "用户没有任何 ent 角色" ⇒ 调用方回退旧权限体系，且**不写缓存**；
//   - 非 nil 空切片 = "明确无权限" ⇒ 调用方**禁止**回退，且以 "[]" 缓存。
// 如果 encode/decode 把这两者混为一谈，缓存往返就会把"明确无权限"降级成"回退旧体系"（= 越权）。
// 本包此前零测试。

func TestUnionPermsDedupesAndKeepsFirstSeenOrder(t *testing.T) {
	got := unionPerms([]string{"b", "a", "b", "c", "a"})
	want := []string{"b", "a", "c"}
	if len(got) != len(want) {
		t.Fatalf("去重后长度应为 %d，实际 %v", len(want), got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("应保持首次出现顺序：got %v want %v", got, want)
		}
	}
}

func TestUnionPermsEmptyIsNonNilEmpty(t *testing.T) {
	for name, in := range map[string][]string{"nil": nil, "empty": {}} {
		got := unionPerms(in)
		if got == nil {
			t.Fatalf("%s 入参应返回**非 nil** 空切片（区分「明确无权限」），实际 nil", name)
		}
		if len(got) != 0 {
			t.Fatalf("%s 入参应返回空切片，实际 %v", name, got)
		}
	}
}

func TestPermsCacheRoundTripKeepsExplicitEmpty(t *testing.T) {
	// 关键不变量：显式空权限经缓存往返后仍是"非 nil 空切片 + ok"
	encoded, err := encodePerms([]string{})
	if err != nil {
		t.Fatalf("encodePerms: %v", err)
	}
	if encoded != "[]" {
		t.Fatalf("空权限应序列化为 []，实际 %q", encoded)
	}
	decoded, ok := decodePerms(encoded)
	if !ok {
		t.Fatal("[] 必须被认作有效缓存值")
	}
	if decoded == nil {
		t.Fatal("[] 解码后必须是**非 nil** 空切片，否则会被误判成「用户没有角色」而回退旧体系")
	}
	if len(decoded) != 0 {
		t.Fatalf("应解码为空，实际 %v", decoded)
	}
}

func TestPermsCacheRoundTripKeepsValues(t *testing.T) {
	in := []string{"ent:read", "ent:admin"}
	encoded, err := encodePerms(in)
	if err != nil {
		t.Fatalf("encodePerms: %v", err)
	}
	decoded, ok := decodePerms(encoded)
	if !ok || len(decoded) != len(in) {
		t.Fatalf("往返不一致：ok=%v decoded=%v", ok, decoded)
	}
	for i := range in {
		if decoded[i] != in[i] {
			t.Fatalf("往返顺序/内容变化：%v vs %v", decoded, in)
		}
	}
}

func TestEncodePermsTreatsNilAsExplicitEmpty(t *testing.T) {
	encoded, err := encodePerms(nil)
	if err != nil {
		t.Fatalf("encodePerms(nil): %v", err)
	}
	if encoded != "[]" {
		t.Fatalf("nil 不应产出 null/空串（那会污染缓存语义），实际 %q", encoded)
	}
}

func TestDecodePermsRejectsCorruptValues(t *testing.T) {
	for _, raw := range []string{"", "not json", "null", "{}", `"abc"`, "1", "[1,2]"} {
		if perms, ok := decodePerms(raw); ok || perms != nil {
			t.Fatalf("损坏缓存值 %q 应判为未命中（nil,false），实际 %v,%v", raw, perms, ok)
		}
	}
}
