package api

import "testing"

// `normalizeSlowLog` 的测试。
//
// 为什么值得单测：它存在的唯一理由是**前端表格按第一条的键生成列**，
// 所以"键是否正确"就是功能本身（错了表头就变成 0/1/2… 或单位错）。
// 两种协议形状都要覆盖：RESP2 的位置数组、RESP3 / Redis ≥ 7 的映射。
func TestNormalizeSlowLogPositionalArray(t *testing.T) {
	// RESP2 形状：`SLOWLOG GET` 每条形如 [id, ts, duration_us, [args…], client, name]
	raw := []interface{}{
		[]interface{}{
			int64(7), int64(1759999999), int64(12345),
			[]interface{}{"GET", "some:key"},
			"127.0.0.1:54321", "worker-1",
		},
		// Redis < 7：没有 client/name 两栏（只有 4 个元素）也不该丢行
		[]interface{}{int64(6), int64(1759999998), int64(99), []interface{}{"PING"}},
	}
	got := normalizeSlowLog(raw)

	if len(got) != 2 {
		t.Fatalf("应得到 2 条，实得 %d：%+v", len(got), got)
	}
	if got[0]["id"] != int64(7) || got[0]["timestamp"] != int64(1759999999) || got[0]["duration_us"] != int64(12345) {
		t.Fatalf("id/timestamp/duration_us 解析错误：%+v", got[0])
	}
	if got[0]["command"] != "GET" || got[0]["args"] != "some:key" {
		t.Fatalf("命令与参数应拆开（表格两列）：%+v", got[0])
	}
	if got[0]["client"] != "127.0.0.1:54321" || got[0]["name"] != "worker-1" {
		t.Fatalf("client/name 应带上：%+v", got[0])
	}
	// 4 元素的老形状：不能因此丢行，也不能带 client/name
	if _, ok := got[1]["client"]; ok {
		t.Fatalf("没有 client 栏时不该凭空造一个：%+v", got[1])
	}
	if got[1]["command"] != "PING" {
		t.Fatalf("无参命令应仍能解析：%+v", got[1])
	}
}

func TestNormalizeSlowLogMapShape(t *testing.T) {
	// RESP3 / Redis ≥ 7：每条是映射，键为 duration / command / client-addr / client-name
	raw := []interface{}{
		map[string]interface{}{
			"id": int64(11), "timestamp": int64(1759999900),
			"duration": int64(4200), "command": "SET foo bar",
			"client-addr": "10.0.0.5:61000", "client-name": "",
		},
		// 键类型为 interface{} 的映射也要能吃（go-redis 在某些路径下就这么给）
		map[interface{}]interface{}{
			"id": int64(10), "timestamp": int64(1759999899),
			"duration": int64(1), "command": "DEL k1 k2",
		},
	}
	got := normalizeSlowLog(raw)

	if len(got) != 2 {
		t.Fatalf("应得到 2 条，实得 %d：%+v", len(got), got)
	}
	if got[0]["duration_us"] != int64(4200) {
		t.Fatalf("映射形状的 duration（本身即微秒）应映射到 duration_us：%+v", got[0])
	}
	if got[0]["command"] != "SET" || got[0]["args"] != "foo bar" {
		t.Fatalf("命令行字符串应拆成 command + args：%+v", got[0])
	}
	if got[0]["client"] != "10.0.0.5:61000" {
		t.Fatalf("client-addr 应映射到 client：%+v", got[0])
	}
	if got[1]["name"] != nil {
		t.Fatalf("缺 client-name 时不该造键：%+v", got[1])
	}
}

func TestNormalizeSlowLogDegenerate(t *testing.T) {
	// 非数组 / 空 / 畸形条目：一律返回空表而不是 panic（前端据此显示"暂无数据"）
	for name, raw := range map[string]interface{}{
		"nil":       nil,
		"string":    "OK",
		"emptyList": []interface{}{},
		"shortRow":  []interface{}{[]interface{}{int64(1), int64(2)}}, // 只有 2 列 ⇒ 跳过
		"badRow":    []interface{}{"not-an-entry"},
	} {
		if got := normalizeSlowLog(raw); len(got) != 0 {
			t.Errorf("%s：应返回空表，实得 %+v", name, got)
		}
	}
}
