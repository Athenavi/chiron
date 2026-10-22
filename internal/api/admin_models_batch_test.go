package api

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// `normalizeBatchModelIDs` 的单测。
//
// 为什么值得单测：这个端点是 /models 页"整组开关"的落点，而两级分组下跨组全选
// 很容易把同一个 id 选两次（同一行既在其 provider 组里、又在某个前缀子组里）。
// 重复不影响 UPDATE 的结果，却会让返回的 updated 计数虚高 —— 前端正是拿它提示
// "已更新 N 个"，计数错了就是功能错了。
func TestNormalizeBatchModelIDs(t *testing.T) {
	t.Run("去重且保持原顺序", func(t *testing.T) {
		got, err := normalizeBatchModelIDs([]string{"b", "a", "b", " a ", "c", ""})
		if err != nil {
			t.Fatalf("不该报错：%v", err)
		}
		want := []string{"b", "a", "c"}
		if fmt.Sprint(got) != fmt.Sprint(want) {
			t.Fatalf("期望 %v（去重、去空白、保序），实得 %v", want, got)
		}
	})

	t.Run("空输入一律报错", func(t *testing.T) {
		for _, in := range [][]string{nil, {}, {""}, {"   ", ""}} {
			if _, err := normalizeBatchModelIDs(in); err == nil {
				t.Errorf("输入 %#v 应报错（ids is empty）", in)
			}
		}
	})

	t.Run("上限之内放行、超出报错", func(t *testing.T) {
		ids := make([]string, 0, maxBatchModelIDs+1)
		for i := 0; i < maxBatchModelIDs; i++ {
			ids = append(ids, fmt.Sprintf("id-%d", i))
		}
		if _, err := normalizeBatchModelIDs(ids); err != nil {
			t.Fatalf("%d 个 id 在上限内，不该报错：%v", maxBatchModelIDs, err)
		}
		ids = append(ids, "id-over-limit")
		if _, err := normalizeBatchModelIDs(ids); err == nil {
			t.Fatalf("%d 个 id 超出上限，应报错", maxBatchModelIDs+1)
		}
	})

	// 上限判定必须在**去重之后**：否则前端把同一批 id 重复送两次就会被误拒。
	t.Run("超限的输入若去重后落在上限内则应放行", func(t *testing.T) {
		ids := make([]string, 0, maxBatchModelIDs*2)
		for i := 0; i < maxBatchModelIDs; i++ {
			ids = append(ids, fmt.Sprintf("id-%d", i))
		}
		ids = append(ids, ids...) // 每个 id 各来两次
		got, err := normalizeBatchModelIDs(ids)
		if err != nil {
			t.Fatalf("去重后正好 %d 个，应放行：%v", maxBatchModelIDs, err)
		}
		if len(got) != maxBatchModelIDs {
			t.Fatalf("期望去重到 %d 个，实得 %d", maxBatchModelIDs, len(got))
		}
	})
}

// 请求体校验：这些分支都在触库之前返回，所以无需数据库即可断言。
//
// 要紧的是 `enabled: false`（"禁用选中"）**不能**被判成"没传 enabled" ——
// 那会让最常用的操作静默 400。
func TestBatchUpdateModelsRequestBodyValidation(t *testing.T) {
	h := &AdminHandler{}
	cases := []struct {
		name string
		body string
	}{
		{"缺 enabled", `{"ids":["11111111-1111-1111-1111-111111111111"]}`},
		{"enabled 为 null", `{"ids":["11111111-1111-1111-1111-111111111111"],"enabled":null}`},
		{"ids 为空数组", `{"ids":[],"enabled":false}`},
		{"ids 全是空白", `{"ids":["","  "],"enabled":true}`},
		{"请求体不是 JSON", `not-json`},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodPut, "/models/batch", strings.NewReader(c.body))
			w := httptest.NewRecorder()
			h.BatchUpdateModels(w, req)
			if w.Code != http.StatusBadRequest {
				t.Fatalf("期望 400，实得 %d（body=%s）", w.Code, w.Body.String())
			}
		})
	}
}
