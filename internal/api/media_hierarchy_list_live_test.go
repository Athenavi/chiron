package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// 用户视角的完整层级浏览：**逐层调用 `GET /v1/media`**。
//
// 之前的 live 用例全部只查数据库（"这两行的 parent_id 对不对"），那证明不了
// 用户能不能看见层级 —— 一个 `WHERE` 写错就足以让层级在库里完好、在列表里消失。
// "文件夹创建没被正确处理、文件层级嵌套不对"这句报告最终必须落到**列表能不能列出**
// 上，所以这里把每一层都真的走一遍。
func listMedia(t *testing.T, h *MediaHandler, tenantID int16, userID, parentID string) []map[string]any {
	t.Helper()
	url := "/v1/media?page=1&page_size=50"
	if parentID != "" {
		url += "&parent_id=" + parentID
	}
	req := httptest.NewRequest(http.MethodGet, url, nil)
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{
		TenantID: tenantID,
		UserID:   userID,
	}))
	rec := httptest.NewRecorder()
	h.List(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("列表失败: status=%d body=%s", rec.Code, rec.Body.String())
	}
	var payload struct {
		Data struct {
			Items []map[string]any `json:"items"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
		t.Fatalf("解析列表: %v (body=%s)", err, rec.Body.String())
	}
	return payload.Data.Items
}

// findByName 在列表结果里按名字找一项，返回其 id（找不到返回空串）。
func findByName(items []map[string]any, name string) string {
	for _, item := range items {
		if str, _ := item["name"].(string); str == name {
			if id, _ := item["id"].(string); id != "" {
				return id
			}
		}
	}
	return ""
}

func TestLongPathHierarchyVisibleThroughListLive(t *testing.T) {
	pool := withLivePool(t)
	tenantID, userID := liveMediaFixture(t, pool)

	db.PoolMu.Lock()
	prevPool := db.Pool
	db.Pool = pool
	db.PoolMu.Unlock()
	t.Cleanup(func() {
		db.PoolMu.Lock()
		db.Pool = prevPool
		db.PoolMu.Unlock()
	})

	handler := newMediaTestHandler(t, t.TempDir())

	// ① agent 发布一个长路径文件
	rec := postInternalAsset(t, handler, map[string]any{
		"tenant_id": itoa(int(tenantID)),
		"user_id":   userID,
		"name":      "reports/2024/q3.md",
		"content":   "# Q3",
		"type":      "text",
		"mime_type": "text/markdown",
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("创建失败: %s", rec.Body.String())
	}

	// ② 根目录：必须**只**看到 reports（一层文件夹），看不到 q3.md
	root := listMedia(t, handler, tenantID, userID, "")
	reportsID := findByName(root, "reports")
	if reportsID == "" {
		t.Fatalf("根目录列表里没有 reports（层级没建出来）: %v", summarizeNames(root))
	}
	if findByName(root, "q3.md") != "" {
		t.Error("根目录列表里出现了 q3.md：文件没有嵌套进目录")
	}
	if findByName(root, "2024") != "" {
		t.Error("2024 出现在了根目录：第二级没有嵌套到 reports 里")
	}

	// ③ 进入 reports：必须看到 2024（且看不到 q3.md）
	inReports := listMedia(t, handler, tenantID, userID, reportsID)
	twentyFourID := findByName(inReports, "2024")
	if twentyFourID == "" {
		t.Fatalf("reports 里没有 2024: %v", summarizeNames(inReports))
	}
	if findByName(inReports, "q3.md") != "" {
		t.Error("q3.md 出现在了 reports 下：它应该在 reports/2024 里")
	}

	// ④ 进入 2024：必须看到 q3.md（且看不到别的目录）
	in2024 := listMedia(t, handler, tenantID, userID, twentyFourID)
	if findByName(in2024, "q3.md") == "" {
		t.Fatalf("2024 里没有 q3.md: %v", summarizeNames(in2024))
	}
	for _, leaked := range []string{"reports", "2024"} {
		if findByName(in2024, leaked) != "" {
			t.Errorf("2024 里出现了目录 %q：层级串了", leaked)
		}
	}

	// ⑤ 每一层都必须是"只有该层的东西"—— 总数也要对得上
	if len(root) != 1 {
		t.Errorf("根目录应有 1 项，实际 %d: %v", len(root), summarizeNames(root))
	}
	if len(inReports) != 1 {
		t.Errorf("reports 应有 1 项，实际 %d: %v", len(inReports), summarizeNames(inReports))
	}
	if len(in2024) != 1 {
		t.Errorf("2024 应有 1 项，实际 %d: %v", len(in2024), summarizeNames(in2024))
	}
}

// 用户手动建多级文件夹后，层级同样要能逐层列出。
func TestCreateFolderHierarchyVisibleThroughListLive(t *testing.T) {
	pool := withLivePool(t)
	tenantID, userID := liveMediaFixture(t, pool)

	db.PoolMu.Lock()
	prevPool := db.Pool
	db.Pool = pool
	db.PoolMu.Unlock()
	t.Cleanup(func() {
		db.PoolMu.Lock()
		db.Pool = prevPool
		db.PoolMu.Unlock()
	})

	handler := newMediaTestHandler(t, t.TempDir())

	// 建 a/b/c（三层）
	rec := postCreateFolder(t, handler, tenantID, userID, "a/b/c", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("建目录失败: %s", rec.Body.String())
	}

	root := listMedia(t, handler, tenantID, userID, "")
	aID := findByName(root, "a")
	if aID == "" {
		t.Fatalf("根目录没有 a: %v", summarizeNames(root))
	}
	if findByName(root, "b") != "" || findByName(root, "c") != "" {
		t.Errorf("b/c 出现在了根目录（层级被压平）: %v", summarizeNames(root))
	}

	inA := listMedia(t, handler, tenantID, userID, aID)
	bID := findByName(inA, "b")
	if bID == "" {
		t.Fatalf("a 里没有 b: %v", summarizeNames(inA))
	}

	inB := listMedia(t, handler, tenantID, userID, bID)
	if findByName(inB, "c") == "" {
		t.Fatalf("b 里没有 c: %v", summarizeNames(inB))
	}
}

func summarizeNames(items []map[string]any) []string {
	names := make([]string, 0, len(items))
	for _, item := range items {
		name, _ := item["name"].(string)
		kind, _ := item["type"].(string)
		names = append(names, name+"("+kind+")")
	}
	return names
}
