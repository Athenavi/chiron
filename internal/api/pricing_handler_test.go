package api

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// pricing_handler_test.go —— 固定价格目录的两条不变量。
//
// 这个端点是**只读**的参考数据出口：写路径在引擎侧（`app/pricing/sync.py`），
// 这里只负责按白名单排序、按条件过滤地把已验证价格读出来。

func TestPricingSortWhitelistCoversEveryAdvertisedKey(t *testing.T) {
	// 白名单是 SQL 片段，绝不能来自请求参数。这里反过来锁住：前端/文档里宣传的
	// 每个 sort 值都必须真的在表里，否则用户选了它只会拿到 400。
	for _, key := range []string{"model", "input", "output", "mixed", "verified"} {
		if clause, ok := pricingSortClauses[key]; !ok || clause == "" {
			t.Fatalf("sort=%q 缺少白名单映射（前端选项会直接 400）", key)
		}
	}
	// 反向：白名单里不该有未被宣称的键，否则等于留了一个没人测的口子。
	if len(pricingSortClauses) != 5 {
		t.Fatalf("排序白名单应恰好 5 项，实际 %d 项", len(pricingSortClauses))
	}
}

func TestListModelPricesRejectsUnknownSort(t *testing.T) {
	h := &BillingHandler{}
	// 用真实存在的列名 `verified_at`：白名单里的键是 `verified`。
	// 这证明拦截依据是白名单，而不是"长得不像列名"这种启发式。
	// 注意不要用分号：`url.Query()` 遇到分号会丢弃整个参数，sort 变空值 ⇒ 走默认值。
	req := httptest.NewRequest(http.MethodGet, "/v1/pricing/models?sort=verified_at", nil)
	rec := httptest.NewRecorder()

	h.ListModelPrices(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("非法 sort 必须 400，得到 %d：%s", rec.Code, rec.Body.String())
	}
}

func TestSplitNonEmptyDropsBlankSegments(t *testing.T) {
	// `sources` 是逗号分隔的源名；写入方可能留下空段，直接塞进 JSON 会让前端
	// 渲染出空 Tag。
	got := splitNonEmpty(" portkey , ,llmrates,", ",")
	want := []string{"portkey", "llmrates"}
	if len(got) != len(want) {
		t.Fatalf("期望 %v，得到 %v", want, got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("期望 %v，得到 %v", want, got)
		}
	}
}
