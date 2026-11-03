package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// `POST /v1/media/folders` 的长路径端到端用例。
//
// 这是用户报告「没有正确处理**文件夹创建**」的**直接**回归点：此前 handler 把
// `a/b/c` 整串当成一个 name 直接 INSERT，媒体库里于是出现一个名字带斜杠的怪目录，
// 点进去空空如也，用户以为自己建好了三层结构。
//
// 与 `media_longpath_e2e_test.go` 一样驱动真实 handler + 真实 PG：中间函数各自通过、
// 拼起来仍然会错，只有把 HTTP 入口跑一遍才算验证。
func postCreateFolder(t *testing.T, h *MediaHandler, tenantID int16, userID, name, parentID string) *httptest.ResponseRecorder {
	t.Helper()
	payload := map[string]any{"name": name}
	if parentID != "" {
		payload["parent_id"] = parentID
	}
	body, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	req := httptest.NewRequest(http.MethodPost, "/v1/media/folders", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{
		TenantID: tenantID,
		UserID:   userID,
	}))
	rec := httptest.NewRecorder()
	h.CreateFolder(rec, req)
	return rec
}

func TestCreateFolderLongPathEndToEndLive(t *testing.T) {
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

	rec := postCreateFolder(t, handler, tenantID, userID, "reports/2024/q3", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("建目录失败: status=%d body=%s", rec.Code, rec.Body.String())
	}

	var payload struct {
		Data struct {
			ID       string `json:"id"`
			Name     string `json:"name"`
			ParentID string `json:"parent_id"`
			Path     string `json:"path"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
		t.Fatalf("解析响应: %v (body=%s)", err, rec.Body.String())
	}
	if payload.Data.Name != "q3" {
		t.Errorf("响应 name = %q, 期望 q3（末段是这级目录名）", payload.Data.Name)
	}
	if payload.Data.Path != "reports/2024/q3" {
		t.Errorf("响应 path = %q, 期望 reports/2024/q3", payload.Data.Path)
	}

	ctx := t.Context()

	// 断言：三层目录都真实存在，且逐级嵌套（不是三个都在根下）。
	var leafParent string
	if err := pool.QueryRow(ctx,
		`SELECT COALESCE(parent_id,'') FROM media_assets
		  WHERE id=$1 AND type='folder'`, payload.Data.ID).Scan(&leafParent); err != nil {
		t.Fatalf("末级目录不存在: %v", err)
	}
	if leafParent == "" {
		t.Fatal("q3 挂在根目录下：多级路径被压平成一层")
	}

	var midName, topID string
	if err := pool.QueryRow(ctx,
		`SELECT name, COALESCE(parent_id,'') FROM media_assets WHERE id=$1 AND type='folder'`,
		leafParent).Scan(&midName, &topID); err != nil {
		t.Fatalf("中间目录不存在: %v", err)
	}
	if midName != "2024" {
		t.Errorf("中间目录 name = %q, 期望 2024", midName)
	}
	if topID == "" {
		t.Error("2024 挂在根下：第二级没有嵌套到 reports 里")
	}

	var topName string
	if err := pool.QueryRow(ctx,
		`SELECT name FROM media_assets WHERE id=$1 AND type='folder'`, topID).Scan(&topName); err != nil {
		t.Fatalf("一级目录不存在: %v", err)
	}
	if topName != "reports" {
		t.Errorf("一级目录 name = %q, 期望 reports", topName)
	}

	// 断言：**不能**出现名字含斜杠的条目（这正是修复前的错误形态）。
	var dirty int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM media_assets
		  WHERE tenant_id=$1 AND user_id=$2 AND (name LIKE '%/%' OR name LIKE '%\\%')`,
		tenantID, userID).Scan(&dirty); err != nil {
		t.Fatalf("count dirty: %v", err)
	}
	if dirty != 0 {
		t.Errorf("库里有 %d 个名字含路径分隔符的条目（整串被当成名字）", dirty)
	}

	// 断言：同一个多级路径再来一次 → 末级必须报"已存在"，而不是造出第二个 q3。
	// （前两级可复用是设计，末级必须冲突。）
	again := postCreateFolder(t, handler, tenantID, userID, "reports/2024/q3", "")
	if again.Code != http.StatusBadRequest {
		t.Errorf("重复建同一目录应 400，得到 %d（body=%s）", again.Code, again.Body.String())
	}
	var q3Count int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM media_assets
		  WHERE tenant_id=$1 AND user_id=$2 AND type='folder' AND name='q3'`,
		tenantID, userID).Scan(&q3Count); err != nil {
		t.Fatalf("count q3: %v", err)
	}
	if q3Count != 1 {
		t.Errorf("q3 有 %d 个，期望 1（重复建目录造出了副本）", q3Count)
	}
}

// 在**已存在的目录下**再建多级目录：起点必须是那个目录，而不是根。
//
// 这条容易漏：缺了 `baseParent` 传递，用户在高亮的子目录里新建 `x/y`，会得到
// 根下的 `x/y` —— 与它眼前的目录对不上，用户只会觉得"建错地方了"。
func TestCreateFolderUnderExistingParentLive(t *testing.T) {
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

	// 先建 reports
	first := postCreateFolder(t, handler, tenantID, userID, "reports", "")
	if first.Code != http.StatusOK {
		t.Fatalf("建 reports 失败: %s", first.Body.String())
	}
	var rootPayload struct {
		Data struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	if err := json.Unmarshal(first.Body.Bytes(), &rootPayload); err != nil {
		t.Fatalf("解析: %v", err)
	}
	reportsID := rootPayload.Data.ID

	// 在 reports 下建 a/b
	second := postCreateFolder(t, handler, tenantID, userID, "a/b", reportsID)
	if second.Code != http.StatusOK {
		t.Fatalf("在 reports 下建 a/b 失败: %s", second.Body.String())
	}
	var p2 struct {
		Data struct {
			ParentID string `json:"parent_id"`
		} `json:"data"`
	}
	if err := json.Unmarshal(second.Body.Bytes(), &p2); err != nil {
		t.Fatalf("解析: %v", err)
	}

	ctx := t.Context()
	// b 的父应是 a；a 的父应是 reports（而不是根）。
	var aID string
	if err := pool.QueryRow(ctx,
		`SELECT COALESCE(parent_id,'') FROM media_assets
		  WHERE tenant_id=$1 AND user_id=$2 AND type='folder' AND name='b'`,
		tenantID, userID).Scan(&aID); err != nil {
		t.Fatalf("b 不存在: %v", err)
	}
	if aID == "" {
		t.Fatal("b 挂在根下：baseParent 没有传递")
	}
	var aParent string
	if err := pool.QueryRow(ctx,
		`SELECT COALESCE(parent_id,'') FROM media_assets WHERE id=$1`, aID).Scan(&aParent); err != nil {
		t.Fatalf("a 不存在: %v", err)
	}
	if aParent != reportsID {
		t.Errorf("a 的 parent = %q, 期望 %q（reports）", aParent, reportsID)
	}

	// 根下**不应**多出 a（它应该在 reports 里）。
	var rootA int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM media_assets
		  WHERE tenant_id=$1 AND user_id=$2 AND type='folder' AND name='a' AND COALESCE(parent_id,'')=''`,
		tenantID, userID).Scan(&rootA); err != nil {
		t.Fatalf("count root a: %v", err)
	}
	if rootA != 0 {
		t.Error("a 出现在了根目录：起点没有用 parent_id")
	}
}

// 非法的文件夹路径必须在写入之前被拒。
func TestCreateFolderRejectsBadPathLive(t *testing.T) {
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

	for _, bad := range []string{"../x", "/abs", "a//b", "a\\b", "a/\x01b"} {
		rec := postCreateFolder(t, handler, tenantID, userID, bad, "")
		if rec.Code != http.StatusBadRequest {
			t.Errorf("路径 %q 应 400，得到 %d（body=%s）", bad, rec.Code, rec.Body.String())
		}
	}

	var total int
	if err := pool.QueryRow(t.Context(),
		`SELECT count(*) FROM media_assets WHERE tenant_id=$1 AND user_id=$2`,
		tenantID, userID).Scan(&total); err != nil {
		t.Fatalf("count: %v", err)
	}
	if total != 0 {
		t.Errorf("非法路径留下了 %d 行", total)
	}
}
