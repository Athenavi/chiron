package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/storage"
)

// newStorageTestHandler 用真实 LocalStore（临时目录）构造 handler。
//
// 不用 mock：这里要验证的正是"存储键怎么拼"与"越界怎么拒"，用真后端才看得见。
func newStorageTestHandler(t *testing.T) *InternalStorageHandler {
	t.Helper()
	return NewInternalStorageHandler(
		storage.NewAtomicStore(storage.NewLocalStore(t.TempDir())),
	)
}

func doWrite(t *testing.T, h *InternalStorageHandler, tenant, user, path, content string) *httptest.ResponseRecorder {
	t.Helper()
	body, _ := json.Marshal(map[string]string{
		"tenant_id": tenant, "user_id": user, "path": path, "content": content,
	})
	req := httptest.NewRequest(http.MethodPost, "/v1/internal/storage/write", bytes.NewReader(body))
	rec := httptest.NewRecorder()
	h.StorageWrite(rec, req)
	return rec
}

func doRead(t *testing.T, h *InternalStorageHandler, tenant, user, path string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/v1/internal/storage/read?tenant_id="+tenant+
		"&user_id="+user+"&path="+path, nil)
	rec := httptest.NewRecorder()
	h.StorageRead(rec, req)
	return rec
}

func TestInternalStorageWriteReadRoundTrip(t *testing.T) {
	h := newStorageTestHandler(t)

	if rec := doWrite(t, h, "t1", "u1", "notes/a.txt", "hello"); rec.Code != http.StatusOK {
		t.Fatalf("write 期望 200，得到 %d：%s", rec.Code, rec.Body.String())
	}

	rec := doRead(t, h, "t1", "u1", "notes/a.txt")
	if rec.Code != http.StatusOK {
		t.Fatalf("read 期望 200，得到 %d：%s", rec.Code, rec.Body.String())
	}
	var payload map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
		t.Fatalf("响应不是 JSON：%v", err)
	}
	if payload["content"] != "hello" {
		t.Fatalf("content 期望 hello，得到 %v", payload["content"])
	}
	if payload["encoding"] != "utf-8" {
		t.Fatalf("encoding 期望 utf-8，得到 %v", payload["encoding"])
	}
}

// 租户/用户隔离：同名路径在不同租户下必须互不可见 —— 这是本端点最重要的安全属性。
func TestInternalStorageIsolatesTenantsAndUsers(t *testing.T) {
	h := newStorageTestHandler(t)

	if rec := doWrite(t, h, "tenant-a", "user-1", "same.txt", "A"); rec.Code != http.StatusOK {
		t.Fatalf("写入失败：%d", rec.Code)
	}

	// 另一租户读同名路径 → 必须 404（而不是读到别人的内容）
	if rec := doRead(t, h, "tenant-b", "user-1", "same.txt"); rec.Code != http.StatusNotFound {
		t.Fatalf("跨租户读取期望 404，得到 %d：%s", rec.Code, rec.Body.String())
	}
	// 同租户另一用户 → 同样 404
	if rec := doRead(t, h, "tenant-a", "user-2", "same.txt"); rec.Code != http.StatusNotFound {
		t.Fatalf("跨用户读取期望 404，得到 %d：%s", rec.Code, rec.Body.String())
	}
	// 本租户本用户 → 200
	if rec := doRead(t, h, "tenant-a", "user-1", "same.txt"); rec.Code != http.StatusOK {
		t.Fatalf("本租户读取期望 200，得到 %d", rec.Code)
	}
}

func TestInternalStorageRejectsEscapingPath(t *testing.T) {
	h := newStorageTestHandler(t)

	for _, bad := range []string{"../escape.txt", "a/../../escape.txt", ".."} {
		rec := doWrite(t, h, "t1", "u1", bad, "x")
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("路径 %q 期望 400，得到 %d", bad, rec.Code)
		}
	}
}

// 身份段含路径分隔符时被收敛（否则能借身份跳出自己的前缀）。
func TestInternalStorageSanitizesIdentitySegments(t *testing.T) {
	if got := sanitizeSegment("../../etc"); strings.Contains(got, "/") {
		t.Fatalf("身份段未收敛：%q", got)
	}
	if got := sanitizeSegment(""); got != "unknown" {
		t.Fatalf("空身份段应回落 unknown，得到 %q", got)
	}
}

func TestInternalStorageReadMissingIsNotFound(t *testing.T) {
	h := newStorageTestHandler(t)

	// 必须是 404 而不是"空内容"：调用方要能区分"没有这个文件"与"文件是空的"
	rec := doRead(t, h, "t1", "u1", "nope.txt")
	if rec.Code != http.StatusNotFound {
		t.Fatalf("期望 404，得到 %d：%s", rec.Code, rec.Body.String())
	}
}

func TestInternalStorageListStripsNamespace(t *testing.T) {
	h := newStorageTestHandler(t)
	doWrite(t, h, "t1", "u1", "docs/a.md", "a")
	doWrite(t, h, "t1", "u1", "docs/b.md", "b")

	req := httptest.NewRequest(http.MethodGet,
		"/v1/internal/storage/list?tenant_id=t1&user_id=u1&prefix=docs", nil)
	rec := httptest.NewRecorder()
	h.StorageList(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("list 期望 200，得到 %d：%s", rec.Code, rec.Body.String())
	}

	var payload struct {
		Files []map[string]any `json:"files"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
		t.Fatalf("响应不是 JSON：%v", err)
	}

	// 结果里**既有文件也有目录项**（`docs` 目录本身）—— 目录项对 `ls` 有用，
	// 不能丢；需要"只要文件"的调用方（如 `glob_files`）自己按 `is_dir` 过滤。
	var filesOnly []map[string]any
	for _, f := range payload.Files {
		if isDir, _ := f["is_dir"].(bool); !isDir {
			filesOnly = append(filesOnly, f)
		}
	}
	if len(filesOnly) != 2 {
		t.Fatalf("期望 2 个文件，得到 %d：%v", len(filesOnly), payload.Files)
	}
	// 递归展开：`docs/a.md` 在第二层，一层 List 拿不到它
	paths := map[string]bool{}
	for _, f := range filesOnly {
		p, _ := f["path"].(string)
		paths[p] = true
	}
	if !paths["docs/a.md"] || !paths["docs/b.md"] {
		t.Fatalf("递归展开未覆盖子目录：%v", paths)
	}
	// 返回的是**相对路径**（剥掉 agent-files/{tenant}/{user}/），调用方据此拼虚拟路径
	for _, f := range payload.Files {
		p, _ := f["path"].(string)
		if strings.HasPrefix(p, agentFilePrefix) || strings.HasPrefix(p, "t1/") {
			t.Fatalf("路径未剥离命名空间前缀：%q", p)
		}
	}
}

func TestInternalStorageUnavailableWithoutBackend(t *testing.T) {
	h := NewInternalStorageHandler(nil)
	rec := doRead(t, h, "t1", "u1", "a.txt")
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("未配置存储时期望 503，得到 %d", rec.Code)
	}
}
