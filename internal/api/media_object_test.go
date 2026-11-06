package api

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/storage"
)

// ── 对象键归一化：历史形态只能靠数据出现，靠点验覆盖不到 ──
//
// 这段逻辑此前只存在于 `ServeSignedMedia` 里，且是"先查 file_path、查不到再查
// file_url"的两段式；抽成纯函数之后，两类存量资产才第一次可以被直接断言：
// 只写了 `file_url` 的老记录，以及把前缀存成 `media/...` 的老记录。

func TestNormalizeMediaObjectKey(t *testing.T) {
	cases := []struct {
		name      string
		filePath  string
		fileURL   string
		wantKey   string
		wantRel   string
		wantError bool
	}{
		{
			name:     "相对媒体根（分片路径写入的形态）",
			filePath: "1/asset-1/a.pdf",
			wantKey:  "media/1/asset-1/a.pdf",
			wantRel:  "1/asset-1/a.pdf",
		},
		{
			name:     "带 media/ 前缀（直传路径写入的形态）",
			filePath: "media/1/asset-1/a.pdf",
			wantKey:  "media/1/asset-1/a.pdf",
			wantRel:  "1/asset-1/a.pdf",
		},
		{
			name:     "file_path 为空时从 file_url 推导",
			fileURL:  "/media/1/asset-1/a.pdf",
			wantKey:  "media/1/asset-1/a.pdf",
			wantRel:  "1/asset-1/a.pdf",
		},
		{
			name:    "S3 绝对 URL 不猜（宁可 404）",
			fileURL: "https://s3.example.com/bucket/media/1/a.pdf",
			wantError: true,
		},
		{
			name:      "空路径与空 URL",
			wantError: true,
		},
		{
			name:     "路径穿越被拒",
			filePath: "media/1/../../etc/passwd",
			wantError: true,
		},
		{
			name:     "只有 media/ 前缀、没有剩余段",
			filePath: "media/",
			wantError: true,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			key, rel, err := normalizeMediaObjectKey(c.filePath, c.fileURL)
			if c.wantError {
				if !errors.Is(err, errMediaAssetNotFound) {
					t.Fatalf("期望 404 语义的错误，得到 key=%q rel=%q err=%v", key, rel, err)
				}
				return
			}
			if err != nil {
				t.Fatalf("不应报错：%v", err)
			}
			if key != c.wantKey || rel != c.wantRel {
				t.Fatalf("得到 (%q, %q)，期望 (%q, %q)", key, rel, c.wantKey, c.wantRel)
			}
		})
	}
}

// ── 内部取字节端点 ──

// withResolvedAsset 临时替换归属查询，跑完恢复。
//
// 仓库没有 sqlmock、单元测试也不许连库（见 `auth_invite_test.go` 的说明），
// 所以这里注入的是"查询结果"，而存储仍用真实的 LocalStore —— 要验证的正是
// "键拼对了没有、字节有没有完整读出来"。
func withResolvedAsset(t *testing.T, obj mediaAssetObject, err error) {
	t.Helper()
	prev := resolveMediaAssetObject
	resolveMediaAssetObject = func(context.Context, string, int16, string) (mediaAssetObject, error) {
		return obj, err
	}
	t.Cleanup(func() { resolveMediaAssetObject = prev })
}

func getInternalContent(h *MediaHandler, target string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodGet, target, nil)
	// `r.PathValue` 由 mux 的路由匹配填充，`httptest.NewRequest` 不会 —— 直接调
	// handler 时必须自己补，否则每次都会走到"缺 id"的 400，测试就测不到真实分支。
	if seg := strings.SplitN(strings.SplitN(target, "?", 2)[0], "/", 6); len(seg) >= 6 {
		req.SetPathValue("id", seg[4])
	}
	rec := httptest.NewRecorder()
	h.InternalReadAsset(rec, req)
	return rec
}

func TestInternalReadAssetStreamsBytesWithDeclaredMIME(t *testing.T) {
	dir := t.TempDir()
	store := storage.NewAtomicStore(storage.NewLocalStore(dir))
	h := NewMediaHandler(store, nil)

	body := "附件正文内容"
	obj := mediaAssetObject{
		ObjectKey: "media/1/asset-1/report.pdf",
		RelPath:   "1/asset-1/report.pdf",
		MimeType:  "application/pdf",
		Name:      "report.pdf",
	}
	if err := store.Write(context.Background(), obj.ObjectKey, []byte(body)); err != nil {
		t.Fatalf("准备对象失败：%v", err)
	}
	withResolvedAsset(t, obj, nil)

	rec := getInternalContent(h, "/v1/internal/media/asset-1/content?tenant_id=1&user_id=u1")

	if rec.Code != http.StatusOK {
		t.Fatalf("期望 200，得到 %d：%s", rec.Code, rec.Body.String())
	}
	if rec.Body.String() != body {
		t.Fatalf("内容不完整：%q", rec.Body.String())
	}
	// `mime_type` 是入库时按 magic-bytes 定的：直接回给引擎，省掉"再猜一次"。
	if got := rec.Header().Get("Content-Type"); got != "application/pdf" {
		t.Fatalf("Content-Type 应为入库值，得到 %q", got)
	}
}

func TestInternalReadAssetSupportsRange(t *testing.T) {
	// 长文档按分片读取是这条端点的实际用法：没有 Range 就只能整份重传。
	dir := t.TempDir()
	store := storage.NewAtomicStore(storage.NewLocalStore(dir))
	h := NewMediaHandler(store, nil)
	obj := mediaAssetObject{ObjectKey: "media/1/a/x.txt", RelPath: "1/a/x.txt", Name: "x.txt"}
	if err := store.Write(context.Background(), obj.ObjectKey, []byte("0123456789")); err != nil {
		t.Fatalf("准备对象失败：%v", err)
	}
	withResolvedAsset(t, obj, nil)

	req := httptest.NewRequest(http.MethodGet,
		"/v1/internal/media/a/content?tenant_id=1&user_id=u1", nil)
	req.SetPathValue("id", "a")
	req.Header.Set("Range", "bytes=2-4")
	rec := httptest.NewRecorder()
	h.InternalReadAsset(rec, req)

	if rec.Code != http.StatusPartialContent {
		t.Fatalf("期望 206，得到 %d", rec.Code)
	}
	if rec.Body.String() != "234" {
		t.Fatalf("Range 取到 %q，期望 %q", rec.Body.String(), "234")
	}
}

func TestInternalReadAssetRequiresIdentity(t *testing.T) {
	dir := t.TempDir()
	h := NewMediaHandler(storage.NewAtomicStore(storage.NewLocalStore(dir)), nil)

	cases := []string{
		"/v1/internal/media/a/content?user_id=u1",             // 缺 tenant_id
		"/v1/internal/media/a/content?tenant_id=1",            // 缺 user_id
		"/v1/internal/media/a/content?tenant_id=x&user_id=u1", // tenant 非数值
	}
	for _, target := range cases {
		if rec := getInternalContent(h, target); rec.Code != http.StatusBadRequest {
			t.Fatalf("%s 应 400，得到 %d", target, rec.Code)
		}
	}
}

func TestInternalReadAssetHidesForeignAssetsAs404(t *testing.T) {
	// 归属不符与"不存在"必须给出同一个答案：区分二者等于提供枚举接口。
	dir := t.TempDir()
	h := NewMediaHandler(storage.NewAtomicStore(storage.NewLocalStore(dir)), nil)
	withResolvedAsset(t, mediaAssetObject{}, errMediaAssetNotFound)

	rec := getInternalContent(h, "/v1/internal/media/other/content?tenant_id=2&user_id=u2")

	if rec.Code != http.StatusNotFound {
		t.Fatalf("期望 404，得到 %d", rec.Code)
	}
	if strings.Contains(rec.Body.String(), "forbidden") {
		t.Fatalf("不应透露「存在但不属于你」，得到：%s", rec.Body.String())
	}
}

func TestInternalReadAssetReportsMissingObjectAs404(t *testing.T) {
	// 行在库里、对象不在存储里：对调用方仍是"没有"，但这是数据不一致，
	// 端点不能 500（引擎会把 500 当成"网关故障"并重试整轮）。
	dir := t.TempDir()
	h := NewMediaHandler(storage.NewAtomicStore(storage.NewLocalStore(dir)), nil)
	withResolvedAsset(t, mediaAssetObject{
		ObjectKey: "media/1/a/missing.txt",
		RelPath:   "1/a/missing.txt",
		Name:      "missing.txt",
	}, nil)

	rec := getInternalContent(h, "/v1/internal/media/a/content?tenant_id=1&user_id=u1")

	if rec.Code != http.StatusNotFound {
		t.Fatalf("期望 404，得到 %d：%s", rec.Code, rec.Body.String())
	}
}
