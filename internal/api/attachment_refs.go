package api

import (
	"encoding/json"
	"strings"
)

// attachmentRefKeys 是附件引用清单允许落库的字段白名单。
//
// `url` 刻意不在其中：前端上传后拿到的是 `data:` 内联载荷（小文件）或短时效签名 URL，
// 两者都**不能**进库 —— 前者是"附件内容随会话永久驻留"，正是引用化要根治的问题；
// 后者会过期，回放时必然烂掉。前端按 `id` 现签 URL。
var attachmentRefKeys = []string{"id", "name", "mime_type", "size", "is_image"}

// attachmentRefsForStorage 把前端传来的附件数组压成落库用的引用清单 JSON。
//
// 返回值契约：无附件或全部缺 id 时返回 ""（写入侧 COALESCE 成 `[]`），
// 让调用方不必区分"没有附件"和"附件无效"。
//
// 为什么按白名单抽字段而不是整体序列化：`attachments` 是**不可信输入**，
// 其中任何字段都可能被改成 `url: "data:..."`；整体落库等于给"内容驻留"留了后门。
func attachmentRefsForStorage(attachments []map[string]interface{}) string {
	if len(attachments) == 0 {
		return ""
	}
	refs := make([]map[string]interface{}, 0, len(attachments))
	for _, att := range attachments {
		id, _ := att["id"].(string)
		if id = strings.TrimSpace(id); id == "" {
			// 没有 id 就无从回放，也无法重新取字节；留在库里只会是死数据。
			continue
		}
		ref := make(map[string]interface{}, len(attachmentRefKeys))
		for _, key := range attachmentRefKeys {
			if v, ok := att[key]; ok && v != nil {
				ref[key] = v
			}
		}
		refs = append(refs, ref)
	}
	if len(refs) == 0 {
		return ""
	}
	// 序列化失败只可能是含不可编码值（func/chan 等），此类输入本就不该出现在 JSON 请求里。
	// 这里退化成空清单而非报错：附件回放缺失不该阻断消息写入。
	b, err := json.Marshal(refs)
	if err != nil {
		return ""
	}
	return string(b)
}
