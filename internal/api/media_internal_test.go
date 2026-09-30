package api

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"io/fs"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/storage"
)

// newMediaTestHandler 用真实 LocalStore（临时目录）构造 handler。
// 用真存储才看得见"内容是否完整落盘"——那正是 1MB 上限曾经截断的地方。
func newMediaTestHandler(t *testing.T, dir string) *MediaHandler {
	t.Helper()
	return NewMediaHandler(storage.NewAtomicStore(storage.NewLocalStore(dir)), nil)
}

func postInternalAsset(t *testing.T, h *MediaHandler, payload map[string]any) *httptest.ResponseRecorder {
	t.Helper()
	body, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal payload: %v", err)
	}
	req := httptest.NewRequest(http.MethodPost, "/v1/internal/media/assets", bytes.NewReader(body))
	rec := httptest.NewRecorder()
	h.InternalCreateAsset(rec, req)
	return rec
}

// ── base64 解析：宽 input、严 output ──

func TestDecodeBase64Field(t *testing.T) {
	hello := base64.StdEncoding.EncodeToString([]byte("hello"))
	binary := []byte{0xfb, 0xff, 0xfe, 0x00, 0x01}

	cases := []struct {
		name    string
		in      string
		want    []byte
		wantErr bool
	}{
		{"standard", hello, []byte("hello"), false},
		{"line breaks", "aGVs\nbG8=", []byte("hello"), false},
		{"spaces", "aGVs bG8=", []byte("hello"), false},
		{"url safe unpadded", base64.RawURLEncoding.EncodeToString(binary), binary, false},
		{"empty", "", nil, true},
		{"garbage", "!!!!", nil, true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, err := decodeBase64Field(c.in)
			if c.wantErr {
				if err == nil {
					t.Fatalf("期望报错，得到 %v", got)
				}
				return
			}
			if err != nil {
				t.Fatalf("解析失败：%v", err)
			}
			if !bytes.Equal(got, c.want) {
				t.Fatalf("得到 %v，期望 %v", got, c.want)
			}
		})
	}
}

// ── 请求体上限：必须与用户直传同量级（曾经复用了 DecodeJSON 的 1MB）──

func TestInternalCreateAssetAcceptsBodyBeyondLegacyOneMegabyte(t *testing.T) {
	dir := t.TempDir()
	h := newMediaTestHandler(t, dir)

	// 2MB 文本：超过 DecodeJSON 的 1MB 通用护栏，但远低于本端点的 50MB。
	content := strings.Repeat("a", 2<<20)
	rec := postInternalAsset(t, h, map[string]any{
		"tenant_id": "t1", "user_id": "u1", "name": "big.txt",
		"type": "text", "content": content,
	})

	if rec.Code == http.StatusBadRequest && strings.Contains(rec.Body.String(), "too large") {
		t.Fatalf("2MB 内容被请求体上限拒绝：%s", rec.Body.String())
	}

	// 内容必须**完整**落盘 —— 截断正是这条路径曾经的失效形态。
	var total int64
	if err := filepath.WalkDir(dir, func(_ string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return nil
		}
		if info, infoErr := d.Info(); infoErr == nil {
			total += info.Size()
		}
		return nil
	}); err != nil {
		t.Fatalf("遍历存储目录失败：%v", err)
	}
	if total != int64(len(content)) {
		t.Fatalf("落盘 %d 字节，期望 %d（内容被截断了）", total, len(content))
	}
}

func TestInternalCreateAssetRejectsOversizedBodyWithActionableMessage(t *testing.T) {
	h := newMediaTestHandler(t, t.TempDir())

	// 略超请求体上限：必须在**读取阶段**就被拦，且说明"该怎么办"。
	content := strings.Repeat("a", maxInternalAssetBody+1024)
	rec := postInternalAsset(t, h, map[string]any{
		"tenant_id": "t1", "user_id": "u1", "name": "huge.txt",
		"type": "text", "content": content,
	})

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("期望 400，得到 %d：%s", rec.Code, rec.Body.String())
	}
	body := rec.Body.String()
	if !strings.Contains(body, "too large") {
		t.Fatalf("超限文案应说明原因，得到：%s", body)
	}
	if !strings.Contains(body, "/v1/uploads") {
		t.Fatalf("超限文案应给出替代路径（/v1/uploads），得到：%s", body)
	}
}

func TestInternalCreateAssetValidatesIdentityAndPayload(t *testing.T) {
	h := newMediaTestHandler(t, t.TempDir())

	if rec := postInternalAsset(t, h, map[string]any{
		"tenant_id": "t1", "user_id": "u1", "content": "x",
	}); rec.Code != http.StatusBadRequest {
		t.Fatalf("缺 name 应 400，得到 %d", rec.Code)
	}
	if rec := postInternalAsset(t, h, map[string]any{
		"user_id": "u1", "name": "a.txt", "content": "x",
	}); rec.Code != http.StatusBadRequest {
		t.Fatalf("缺 tenant_id 应 400，得到 %d", rec.Code)
	}
	if rec := postInternalAsset(t, h, map[string]any{
		"tenant_id": "t1", "name": "a.txt", "content": "x",
	}); rec.Code != http.StatusBadRequest {
		t.Fatalf("缺 user_id 应 400，得到 %d", rec.Code)
	}
	if rec := postInternalAsset(t, h, map[string]any{
		"tenant_id": "t1", "user_id": "u1", "name": "a.txt",
	}); rec.Code != http.StatusBadRequest {
		t.Fatalf("缺内容应 400，得到 %d", rec.Code)
	}
	if rec := postInternalAsset(t, h, map[string]any{
		"tenant_id": "t1", "user_id": "u1", "name": "a.sh", "content": "rm -rf /",
	}); rec.Code != http.StatusBadRequest {
		t.Fatalf("脚本类扩展名应被拒（与用户直传同策略），得到 %d", rec.Code)
	}
}
