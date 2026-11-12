package api

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/google/uuid"

	"github.com/athenavi/chiron/internal/storage"
)

// ── 归档下载通道 ──
//
// 这条通道把「已注销用户的完整个人数据快照」交出去，风险集中在两点：
//  1. **谁**能读（权限必须比普通 admin 读更严，否则租户 admin 能跨租户窥探）；
//  2. **读哪个文件**（user_id 与文件名都拼进对象键，不校验就是路径穿越）。
//
// 权限本身由路由层的 platformReadMW 保证（此处无法单测中间件组合），
// 所以这里钉的是 handler 侧的输入校验：非法输入必须在碰对象存储之前被拒。

func newArchiveTestHandler(t *testing.T) (*AdminHandler, string) {
	t.Helper()
	dir := t.TempDir()
	return NewAdminHandler(nil, nil, storage.NewAtomicStore(storage.NewLocalStore(dir)), nil, nil), dir
}

func TestListAccountArchivesRejectsInvalidUserID(t *testing.T) {
	h, _ := newArchiveTestHandler(t)

	// 这些都会拼进对象键：不拦就是一个路径穿越入口。
	for _, bad := range []string{"", "../../etc", "not-a-uuid", "1/2/3"} {
		req := httptest.NewRequest(http.MethodGet, "/v1/admin/account-archives/x", nil)
		req.SetPathValue("user_id", bad)
		rec := httptest.NewRecorder()
		h.ListAccountArchives(rec, req)
		if rec.Code != http.StatusBadRequest {
			t.Errorf("user_id=%q 应 400，得到 %d", bad, rec.Code)
		}
	}
}

func TestDownloadAccountArchiveRejectsTraversal(t *testing.T) {
	h, _ := newArchiveTestHandler(t)
	validID := uuid.NewString()

	// 文件名同样拼进对象键：`../` 与子目录都必须在 handler 层被拒。
	for _, bad := range []string{"../secret.json", "sub/dir.json", "a.txt", "", "x.json/../../y"} {
		req := httptest.NewRequest(http.MethodGet, "/v1/admin/account-archives/x/y", nil)
		req.SetPathValue("user_id", validID)
		req.SetPathValue("file", bad)
		rec := httptest.NewRecorder()
		h.DownloadAccountArchive(rec, req)
		if rec.Code != http.StatusBadRequest {
			t.Errorf("file=%q 应 400，得到 %d", bad, rec.Code)
		}
	}
}

// 正常路径：列出与下载都必须能读到 purge 写下的归档。
func TestArchiveListAndDownload(t *testing.T) {
	h, _ := newArchiveTestHandler(t)
	ctx := context.Background()
	userID := uuid.NewString()

	// 直接按 purge 的键格式写一份归档。
	key := accountArchivePrefix + userID + "/20260101T000000Z.json"
	payload := []byte(`{"version":"1","account":{"id":"` + userID + `"}}`)
	if err := h.store.Write(ctx, key, payload); err != nil {
		t.Fatalf("写归档失败: %v", err)
	}

	// ① 列表
	listReq := httptest.NewRequest(http.MethodGet, "/v1/admin/account-archives/x", nil)
	listReq.SetPathValue("user_id", userID)
	listRec := httptest.NewRecorder()
	h.ListAccountArchives(listRec, listReq)
	if listRec.Code != http.StatusOK {
		t.Fatalf("列出归档失败: %d body=%s", listRec.Code, listRec.Body.String())
	}
	if !strings.Contains(listRec.Body.String(), "20260101T000000Z.json") {
		t.Fatalf("列表里没有该归档: %s", listRec.Body.String())
	}

	// ② 下载
	dlReq := httptest.NewRequest(http.MethodGet, "/v1/admin/account-archives/x/y", nil)
	dlReq.SetPathValue("user_id", userID)
	dlReq.SetPathValue("file", "20260101T000000Z.json")
	dlRec := httptest.NewRecorder()
	h.DownloadAccountArchive(dlRec, dlReq)
	if dlRec.Code != http.StatusOK {
		t.Fatalf("下载失败: %d body=%s", dlRec.Code, dlRec.Body.String())
	}
	if !strings.Contains(dlRec.Body.String(), userID) {
		t.Fatal("下载内容与归档不符")
	}
	// 必须标记为附件：归档是 JSON，内联渲染会让"分享链接"变成数据泄漏。
	if cd := dlRec.Header().Get("Content-Disposition"); !strings.Contains(cd, "attachment") {
		t.Fatalf("Content-Disposition 未标记 attachment: %q", cd)
	}
}

// 不存在的归档给 404，且不泄漏存储层错误细节。
func TestDownloadAccountArchiveNotFound(t *testing.T) {
	h, _ := newArchiveTestHandler(t)
	req := httptest.NewRequest(http.MethodGet, "/v1/admin/account-archives/x/y", nil)
	req.SetPathValue("user_id", uuid.NewString())
	req.SetPathValue("file", "missing.json")
	rec := httptest.NewRecorder()
	h.DownloadAccountArchive(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("应 404，得到 %d", rec.Code)
	}
}
