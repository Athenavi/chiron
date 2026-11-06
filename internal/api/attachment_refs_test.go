package api

import (
	"encoding/json"
	"strings"
	"testing"
)

// 附件清单落库的唯一契约：**只存引用，不存内容**。
//
// 这组用例守的是引用化的核心不变量 —— 若哪天有人"顺手"把 url 白名单加回来，
// `data:` 内联载荷就会重新随消息永久驻留，那正是本次改造要根治的问题。
func TestAttachmentRefsForStorageDropsURL(t *testing.T) {
	in := []map[string]interface{}{{
		"id":        "asset-1",
		"name":      "截图.png",
		"mime_type": "image/png",
		"size":      float64(2048),
		"is_image":  true,
		// 两种"内容驻留"形态同时给出，验证它们都进不了库。
		"url": "data:image/png;base64,AAAA",
	}}

	got := attachmentRefsForStorage(in)
	if got == "" {
		t.Fatal("有 id 的附件不该被丢掉")
	}
	for _, forbidden := range []string{"url", "data:", "base64", "AAAA"} {
		if strings.Contains(got, forbidden) {
			t.Fatalf("落库清单不得含 %q：%s", forbidden, got)
		}
	}

	var refs []map[string]interface{}
	if err := json.Unmarshal([]byte(got), &refs); err != nil {
		t.Fatalf("清单必须是合法 JSON：%v (%s)", err, got)
	}
	if len(refs) != 1 || refs[0]["id"] != "asset-1" || refs[0]["name"] != "截图.png" {
		t.Fatalf("引用字段丢失：%+v", refs)
	}
	if refs[0]["is_image"] != true {
		t.Fatalf("is_image 应保留（前端据此决定内联还是文件卡片）：%+v", refs[0])
	}
}

// 没有 id 的附件无法按 asset_id 回放、也无法重新取字节 —— 留在库里只是死数据。
func TestAttachmentRefsForStorageSkipsEntriesWithoutID(t *testing.T) {
	in := []map[string]interface{}{
		{"name": "a.txt", "url": "data:text/plain;base64,eA=="},
		{"id": "  ", "name": "b.txt"},
	}
	if got := attachmentRefsForStorage(in); got != "" {
		t.Fatalf("全部无效时应返回空串（写入侧 COALESCE 成 []），得到 %s", got)
	}
}

func TestAttachmentRefsForStorageEmptyInput(t *testing.T) {
	if got := attachmentRefsForStorage(nil); got != "" {
		t.Fatalf("无附件应返回空串，得到 %s", got)
	}
}
