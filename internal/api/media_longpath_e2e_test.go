package api

import (
	"encoding/json"
	"net/http"
	"testing"

	"github.com/athenavi/chiron/internal/db"
)

// 长路径的**端到端**用例：驱动真实的 `InternalCreateAsset` handler，
// 断言最终落库的层级 —— 而不是像 `media_path_live_test.go` 那样手工调中间函数。
//
// 为什么必须端到端：中间函数各自通过、拼起来仍然会错。真实的失败模式恰恰是
// "解析对了、建链对了、但 handler 仍然把 parent_id 写成空串"（这正是本次修复前
// `InternalCreateAsset` 里硬编码 `''` 的样子）。只有把 HTTP 入口跑一遍才能钉住它。
//
// 依赖：`CHIRON_TEST_POSTGRES_DSN`（未设置则 skip）。真实 PG + 真实 LocalStore。
func TestInternalCreateAssetLongPathEndToEndLive(t *testing.T) {
	pool := withLivePool(t)
	tenantID, userID := liveMediaFixture(t, pool)

	// handler 走的是全局池（db.GlobalDBManager），临时装上去、用例结束还原。
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

	rec := postInternalAsset(t, handler, map[string]any{
		"tenant_id": itoa(int(tenantID)),
		"user_id":   userID,
		"name":      "reports/2024/q3.md",
		"content":   "# Q3 report",
		"type":      "text",
		"mime_type": "text/markdown",
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("创建失败: status=%d body=%s", rec.Code, rec.Body.String())
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
	if payload.Data.Name != "q3.md" {
		t.Errorf("响应 name = %q, 期望 q3.md（只剩文件名）", payload.Data.Name)
	}
	if payload.Data.Path != "reports/2024/q3.md" {
		t.Errorf("响应 path = %q, 期望 reports/2024/q3.md", payload.Data.Path)
	}
	if payload.Data.ParentID == "" {
		t.Fatal("响应 parent_id 为空：文件落在了根目录，层级被压平")
	}

	ctx := t.Context()

	// 断言 1：文件挂在 `2024` 下，且 `2024` 挂在 `reports` 下（两级都真实存在）。
	var fileName, fileParent string
	if err := pool.QueryRow(ctx,
		`SELECT name, COALESCE(parent_id,'') FROM media_assets
		  WHERE id = $1 AND tenant_id = $2 AND user_id = $3`,
		payload.Data.ID, tenantID, userID).Scan(&fileName, &fileParent); err != nil {
		t.Fatalf("资产未落库: %v", err)
	}
	if fileName != "q3.md" {
		t.Errorf("落库 name = %q, 期望 q3.md", fileName)
	}
	if fileParent != payload.Data.ParentID {
		t.Errorf("落库 parent_id = %q, 响应说 %q", fileParent, payload.Data.ParentID)
	}

	var parentName, grandParentID string
	if err := pool.QueryRow(ctx,
		`SELECT name, COALESCE(parent_id,'') FROM media_assets
		  WHERE id = $1 AND type = 'folder'`, fileParent).Scan(&parentName, &grandParentID); err != nil {
		t.Fatalf("末级文件夹不存在: %v", err)
	}
	if parentName != "2024" {
		t.Errorf("末级文件夹 name = %q, 期望 2024", parentName)
	}
	if grandParentID == "" {
		t.Error("2024 挂在根目录下：第二级文件夹没有建出来")
	}

	var grandName string
	if err := pool.QueryRow(ctx,
		`SELECT name FROM media_assets WHERE id = $1 AND type = 'folder'`, grandParentID).Scan(&grandName); err != nil {
		t.Fatalf("一级文件夹不存在: %v", err)
	}
	if grandName != "reports" {
		t.Errorf("一级文件夹 name = %q, 期望 reports", grandName)
	}

	// 断言 2：根目录列表里**不应**出现这个文件（它不在根下）。
	var rootCount int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM media_assets
		  WHERE tenant_id=$1 AND user_id=$2 AND COALESCE(parent_id,'')='' AND name='q3.md'`,
		tenantID, userID).Scan(&rootCount); err != nil {
		t.Fatalf("count root: %v", err)
	}
	if rootCount != 0 {
		t.Error("文件出现在了根目录：层级被压平")
	}

	// 断言 3：同一路径再发布一次 → 复用文件夹，不产生重复目录，且文件是新的一份。
	rec2 := postInternalAsset(t, handler, map[string]any{
		"tenant_id": itoa(int(tenantID)),
		"user_id":   userID,
		"name":      "reports/2024/q4.md",
		"content":   "# Q4",
		"type":      "text",
		"mime_type": "text/markdown",
	})
	if rec2.Code != http.StatusOK {
		t.Fatalf("第二次创建失败: status=%d body=%s", rec2.Code, rec2.Body.String())
	}
	var folderCount, reportCount int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FILTER (WHERE type='folder'),
		        count(*) FILTER (WHERE type='folder' AND name='reports')
		   FROM media_assets WHERE tenant_id=$1 AND user_id=$2`,
		tenantID, userID).Scan(&folderCount, &reportCount); err != nil {
		t.Fatalf("count folders: %v", err)
	}
	if folderCount != 2 {
		t.Errorf("文件夹总数 = %d, 期望 2（reports + 2024）；同一路径重发造出了重复目录", folderCount)
	}
	if reportCount != 1 {
		t.Errorf("reports 出现了 %d 次，期望 1 次", reportCount)
	}
}

// 短名字（无路径）必须仍然落在根目录：修复长路径不能破坏既有行为。
func TestInternalCreateAssetPlainNameStaysAtRootLive(t *testing.T) {
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
	rec := postInternalAsset(t, handler, map[string]any{
		"tenant_id": itoa(int(tenantID)),
		"user_id":   userID,
		"name":      "README.md",
		"content":   "hello",
		"type":      "text",
		"mime_type": "text/markdown",
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("创建失败: status=%d body=%s", rec.Code, rec.Body.String())
	}

	var parentID string
	if err := pool.QueryRow(t.Context(),
		`SELECT COALESCE(parent_id,'') FROM media_assets
		  WHERE tenant_id=$1 AND user_id=$2 AND name='README.md'`,
		tenantID, userID).Scan(&parentID); err != nil {
		t.Fatalf("资产未落库: %v", err)
	}
	if parentID != "" {
		t.Errorf("根目录文件的 parent_id = %q, 期望空串", parentID)
	}

	// 不应因为一个短名字就凭空造出文件夹。
	var folders int
	if err := pool.QueryRow(t.Context(),
		`SELECT count(*) FROM media_assets
		  WHERE tenant_id=$1 AND user_id=$2 AND type='folder'`, tenantID, userID).Scan(&folders); err != nil {
		t.Fatalf("count folders: %v", err)
	}
	if folders != 0 {
		t.Errorf("短名字创建了 %d 个文件夹，期望 0", folders)
	}
}

// 非法路径必须在落任何一行之前被拒（不能建了一半文件夹再报错）。
func TestInternalCreateAssetRejectsBadPathBeforeWritingLive(t *testing.T) {
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

	for _, bad := range []string{"../secret.md", "a//b.md", "/abs.md", "dir/", "a\\b.md"} {
		rec := postInternalAsset(t, handler, map[string]any{
			"tenant_id": itoa(int(tenantID)),
			"user_id":   userID,
			"name":      bad,
			"content":   "x",
		})
		if rec.Code != http.StatusBadRequest {
			t.Errorf("路径 %q 应为 400，得到 %d（body=%s）", bad, rec.Code, rec.Body.String())
		}
	}

	var total int
	if err := pool.QueryRow(t.Context(),
		`SELECT count(*) FROM media_assets WHERE tenant_id=$1 AND user_id=$2`,
		tenantID, userID).Scan(&total); err != nil {
		t.Fatalf("count: %v", err)
	}
	if total != 0 {
		t.Errorf("非法路径留下了 %d 行（含文件夹），应在写入前就拒绝", total)
	}
}
