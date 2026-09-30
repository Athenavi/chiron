package api

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"unicode"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/id"
)

// ─────────────────────────────────────────────────────────────
// 内部媒体端点（受 X-Internal-Token 保护，见 internalTokenMW）
//
// 用途：让 Python 引擎的 agent 工具（media_create / image_generate）把产物
// 直接写入 Go 侧媒体库（media_assets 表 + 配置的对象存储后端）。
//
// 背景：python-engine/app/tools/media.py 原先写的是引擎进程本地的 LocalStore
//（app/media/store.py），与「媒体库」页面读取的 media_assets 完全隔离，
// 因此 agent 生成的资产在媒体库里看不到。
// ─────────────────────────────────────────────────────────────

// internalMediaCreateRequest 是 agent 工具写入媒体资产的请求体。
// 归属信息（tenant_id/user_id）来自 Python 侧的工具执行上下文，由引擎携带。
type internalMediaCreateRequest struct {
	TenantID string   `json:"tenant_id"`
	UserID   string   `json:"user_id"`
	Name     string   `json:"name"`
	Type     string   `json:"type"`
	Category string   `json:"category"`
	MimeType string   `json:"mime_type"`
	Tags     []string `json:"tags"`
	// Content 为纯文本快捷字段；ContentBase64 用于二进制安全传输（图片/音频等）。
	Content       string `json:"content"`
	ContentBase64 string `json:"content_base64"`
}

// 内部资产写入的量级口径（必须与**用户直传**同量级，见下面 maxInternalAssetBody 的说明）。
const (
	// maxInternalAssetBody 是请求体（JSON，含 base64）上限。
	maxInternalAssetBody = 50 << 20 // 50MB
	// maxInternalAssetBytes 是解码**之后**的真实文件上限 —— base64 会把字节放大 ~33%，
	// 只卡请求体就等于"随内容类型不同而隐含不同的文件上限"，口径会含糊。
	maxInternalAssetBytes = 32 << 20 // 32MB
)

// decodeBase64Field 解析内容字段里的 base64。
//
// 为什么不是一句 base64.StdEncoding.DecodeString：这个字段的值是**模型生成**的，
// 而模型输出的 base64 常见三种形态 —— 带换行/空格的（分块粘贴）、URL-safe 字母表的、
// 以及省略 padding 的。逐字严格校验会把它们全判成"非法 base64"，于是工具报错、
// 而用户只看到"创建失败"。这里宽容输入、严格输出（解码后的字节仍然要过 MIME 与体积校验）。
func decodeBase64Field(raw string) ([]byte, error) {
	compact := strings.Map(func(r rune) rune {
		if unicode.IsSpace(r) {
			return -1
		}
		return r
	}, raw)
	if compact == "" {
		return nil, errors.New("content_base64 is empty")
	}
	if data, err := base64.StdEncoding.DecodeString(compact); err == nil {
		return data, nil
	}
	// 再宽容一次：URL-safe 字母表且省略 padding。
	return base64.RawURLEncoding.DecodeString(strings.TrimRight(compact, "="))
}

// InternalCreateAsset POST /v1/internal/media/assets
func (h *MediaHandler) InternalCreateAsset(w http.ResponseWriter, r *http.Request) {
	// ⚠️ 这里**不能**用 DecodeJSON：它给普通内部 JSON 端点 1MB 护栏（见 response.go），
	// 而本端点承载的是**文件内容** —— 与用户直传 POST /v1/media/upload（50MB）是同一件事。
	// 曾经复用 DecodeJSON 的后果：任何 >1MB 的产物（图片、docx、稍大的 CSV）在网关侧被
	// 截断成 400，Python 侧的 persist_to_library 又回退到引擎本地 store ⇒ **工具报"创建成功"，
	// 媒体库里却什么都没有**。这就是"agent 无法和媒体库联动"的机械原因。
	// 量级口径必须与直传对齐：请求体 50MB、解码后文件 32MB。
	r.Body = http.MaxBytesReader(w, r.Body, maxInternalAssetBody)
	var body internalMediaCreateRequest
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			// 超限要与"请求格式错"分开报：前者要告诉调用方**该怎么办**
			BadRequest(w, fmt.Sprintf(
				"asset too large: request body exceeds %d MB (use POST /v1/uploads for larger files)",
				maxInternalAssetBody>>20))
			return
		}
		BadRequest(w, ErrInvalidReq)
		return
	}
	body.Name = strings.TrimSpace(body.Name)
	if body.Name == "" {
		BadRequest(w, "name is required")
		return
	}
	if body.TenantID == "" || body.UserID == "" {
		BadRequest(w, "tenant_id and user_id are required")
		return
	}

	var data []byte
	switch {
	case body.ContentBase64 != "":
		decoded, err := decodeBase64Field(body.ContentBase64)
		if err != nil {
			BadRequest(w, "content_base64 is not valid base64")
			return
		}
		data = decoded
	case body.Content != "":
		data = []byte(body.Content)
	default:
		BadRequest(w, "content or content_base64 is required")
		return
	}
	if len(data) > maxInternalAssetBytes {
		BadRequest(w, fmt.Sprintf(
			"asset too large: %d bytes (max %d MB)", len(data), maxInternalAssetBytes>>20))
		return
	}

	// 与直传路径同等的安全校验：拒绝可执行/脚本类内容落地
	declaredMIME := truncateMIME(body.MimeType)
	detectedMIME := truncateMIME(http.DetectContentType(data))
	mimeType := detectedMIME
	if declaredMIME != "" && declaredMIME == detectedMIME {
		mimeType = declaredMIME
	}
	if isExecutableMIME(mimeType, body.Name) {
		BadRequest(w, "file type not allowed: "+mimeType)
		return
	}

	assetType := body.Type
	if assetType == "" {
		assetType = detectType(mimeType)
	}

	assetID, err := id.UUID()
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "generate id failed")
		return
	}
	// 与用户直传共用同一键布局：media/<tenantID>/<assetID>/<name>
	objectKey := mediaObjectKey(body.TenantID, assetID, sanitizeUploadName(body.Name))

	if err := h.store.Write(r.Context(), objectKey, data); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "save file failed")
		return
	}
	fileURL := h.objectURL(objectKey)

	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO media_assets (id, tenant_id, user_id, type, name, file_url, file_path, mime_type, category, tags, size, created_at, updated_at)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW(), NOW())`,
		assetID, body.TenantID, body.UserID, assetType, body.Name, fileURL, objectKey,
		mimeType, nullableStr(body.Category), strings.Join(body.Tags, ","), len(data)); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create asset failed")
		return
	}

	slog.Info("internal media asset created", "asset_id", assetID, "tenant_id", body.TenantID, "type", assetType, "size", len(data))
	OK(w, map[string]interface{}{
		"id":       assetID,
		"name":     body.Name,
		"type":     assetType,
		"file_url": fileURL,
		"size":     len(data),
	})
}
