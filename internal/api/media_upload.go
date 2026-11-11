package api

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"path/filepath"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/id"
	"github.com/athenavi/chiron/internal/storage"
)

// ── Upload (multipart file) ──

func (h *MediaHandler) Upload(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID == 0 {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenantID := claims.TenantID

	// 单请求流式上传：上限 50MB。更大的文件应走 /v1/uploads 分片上传
	// （前端 api/index.ts uploadFile 会按大小自动分流）。
	const maxInlineUpload = 50 << 20
	r.Body = http.MaxBytesReader(w, r.Body, maxInlineUpload)
	if err := r.ParseMultipartForm(4 << 20); err != nil {
		// 暴露真实原因（请求体超限 / boundary 缺失 / 格式损坏），便于定位
		slog.Warn("parse multipart form failed", "error", err, "content_type", r.Header.Get("Content-Type"), "content_length", r.ContentLength)
		BadRequest(w, "file too large or invalid form")
		return
	}

	file, header, err := r.FormFile("file")
	if err != nil {
		slog.Warn("get form file failed", "error", err, "form_keys", r.MultipartForm.File)
		BadRequest(w, "file is required")
		return
	}
	defer file.Close()

	// 流式写入：不再把整个文件读进内存。此前 io.ReadAll 在 200MB 上限下会让
	// 单个请求的内存峰值达到 200MB，并发上传极易触发 OOM。
	// magic bytes 检测只需前 512 字节，因此先预读头部，再与剩余流拼接后整体落盘。
	fileSize := header.Size
	head := make([]byte, 512)
	n, err := io.ReadFull(file, head)
	if err != nil && err != io.EOF && err != io.ErrUnexpectedEOF {
		logAndRespond(w, err, http.StatusInternalServerError, "read file failed")
		return
	}
	head = head[:n]

	// P1-6: 用 magic bytes 检测真实 MIME，避免客户端伪造 Content-Type 上传可执行文件
	declaredMIME := truncateMIME(header.Header.Get("Content-Type"))
	detectedMIME := truncateMIME(http.DetectContentType(head))
	// 优先采用检测到的 MIME；声明与检测不一致时以检测为准（更安全）
	mimeType := detectedMIME
	if declaredMIME != "" && declaredMIME == detectedMIME {
		// 声明与检测一致，保留声明的（可能更精确，如 image/png vs image/jpeg）
		mimeType = declaredMIME
	}
	// 拒绝可执行/脚本类 MIME（即使客户端伪装为图片）
	// P1-6: 传入文件名以便扩展名兜底（PE/ELF magic bytes 只检测为 octet-stream）
	if isExecutableMIME(mimeType, header.Filename) {
		BadRequest(w, "file type not allowed: "+mimeType)
		return
	}
	assetType := detectType(mimeType)
	category := r.FormValue("category")
	parentID := r.FormValue("parent_id") // 当前目录；空 = 根目录

	name := r.FormValue("name")
	if name == "" {
		name = header.Filename
	}

	// 租户配额门禁：存储容量 + 文件数量。此前 CheckStorageQuota / CheckFileCountQuota
	// 从未被调用，配额完全不生效。放在落库**之前**，避免超限文件先入库再回滚。
	if allowed, reason := CheckUploadAllowed(r.Context(), tenantStr(tenantID), fileSize); !allowed {
		JSON(w, http.StatusRequestEntityTooLarge, APIResponse{Success: false, Error: reason})
		return
	}

	// 先插入数据库获取 UUID（含 parent_id，子文件夹上传不落到根目录）
	assetID, err := id.UUID()
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "generate id failed")
		return
	}
	_, err = db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO media_assets (id, tenant_id, user_id, type, name, file_url, mime_type, category, size, parent_id, created_at, updated_at)
		 VALUES ($1, $2, $3, $4, $5, '', $6, $7, $8, $9, NOW(), NOW())`,
		assetID, tenantID, claims.UserID, assetType, name, mimeType, nullableStr(category), fileSize, parentID,
	)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create asset failed")
		return
	}

	objectKey := mediaObjectKey(tenantID, assetID, sanitizeUploadName(name))
	// 预读的头部 + 剩余流拼接，保证落盘内容与原始文件完全一致
	stream := io.MultiReader(bytes.NewReader(head), file)
	if err := h.store.WriteStream(r.Context(), objectKey, stream, fileSize, 0o644); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "save file failed")
		return
	}
	fileURL := h.objectURL(objectKey)

	// 更新 file_url 和 file_path（签名URL服务需要）
	_, err = db.GlobalDBManager.Exec(r.Context(),
		`UPDATE media_assets SET file_url = $1, file_path = $2 WHERE id = $3 AND tenant_id = $4`,
		fileURL, objectKey, assetID, tenantID)
	if err != nil {
		slog.Warn("update file_url/file_path", "error", err)
	}

	OK(w, map[string]string{
		"id": assetID, "name": name, "type": assetType,
		"file_url": fileURL, "size": fmt.Sprintf("%d", fileSize),
	})
}

// ── PresignUpload — returns a presigned URL for client-side direct upload ──

func (h *MediaHandler) PresignUpload(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Name     string `json:"name"`
		Type     string `json:"type"`
		Category string `json:"category"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.Name == "" {
		BadRequest(w, "name is required")
		return
	}
	if body.Type == "" {
		body.Type = "file"
	}
	if body.Category == "" {
		body.Category = "upload"
	}

	// Type-assert to S3Store for presigned URL support (unwrap AtomicStore first)
	inner := h.store
	if atomic, ok := h.store.(*storage.AtomicStore); ok {
		inner = atomic.LoadRaw()
	}
	s3store, ok := inner.(*storage.S3Store)
	if !ok {
		InternalError(w, "presigned upload requires S3 storage backend")
		return
	}

	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID == 0 {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	// 对象键唯一性来源：完整 UUID（id.NextID() 是 snowflake→base62，高位为毫秒时间戳，
	// 任何前缀截断都会在同一毫秒内碰撞，故不再使用）。
	assetID, err := id.UUID()
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "generate id failed")
		return
	}
	objectKey := mediaObjectKey(claims.TenantID, assetID, sanitizeUploadName(body.Name))
	fileURL := s3store.ObjectURL(objectKey)

	presignedURL, err := s3store.PresignedPutURL(r.Context(), objectKey, 15*time.Minute)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "generate presigned url failed")
		return
	}

	OK(w, map[string]string{
		"id":            assetID,
		"name":          body.Name,
		"type":          body.Type,
		"category":      body.Category,
		"file_url":      fileURL,
		"presigned_url": presignedURL,
		"expires_in":    "900",
	})
}

// ── CompleteUpload — called by client after presigned upload is done ──

func (h *MediaHandler) CompleteUpload(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID == 0 {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenantID := claims.TenantID

	var body struct {
		ID       string `json:"id"`
		Name     string `json:"name"`
		Type     string `json:"type"`
		FileURL  string `json:"file_url"`
		Category string `json:"category"`
		Size     int64  `json:"size"`
		MimeType string `json:"mime_type"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.FileURL == "" {
		BadRequest(w, "file_url is required")
		return
	}
	if body.Type == "" {
		body.Type = "file"
	}

	// 校验 file_url 指向配置的存储后端（防止客户端伪造任意 URL 入库）
	inner := h.store
	if atomic, ok := h.store.(*storage.AtomicStore); ok {
		inner = atomic.LoadRaw()
	}
	switch s := inner.(type) {
	case *storage.S3Store:
		// 使用 url.Parse 严格校验 host/scheme，防止前缀匹配绕过
		parsedURL, err := url.Parse(body.FileURL)
		if err != nil {
			BadRequest(w, "invalid file_url")
			return
		}
		expectedURL, err := url.Parse(s.ObjectURL(""))
		if err != nil {
			InternalError(w, "storage backend misconfigured")
			return
		}
		if parsedURL.Scheme != expectedURL.Scheme || parsedURL.Host != expectedURL.Host {
			BadRequest(w, "file_url does not match configured storage backend")
			return
		}
	case *storage.LocalStore:
		// Reject protocol-relative URLs and require /media/ prefix
		if !strings.HasPrefix(body.FileURL, "/media/") || strings.HasPrefix(body.FileURL, "//") {
			BadRequest(w, "file_url does not match configured storage backend")
			return
		}
	default:
		BadRequest(w, "unsupported storage backend")
		return
	}

	// 用存储后端读取对象头部做 magic bytes 判定（与直传路径同一份口径）。
	//
	// 旧实现是 `http.Head(body.FileURL)`，三个问题：
	//   ① 生产对象桶几乎都是私有的，匿名 HEAD 拿到 403，Content-Type 是 S3 的 XML
	//      错误体 —— isExecutableMIME 永远不命中，校验在真实部署里**等于没有**（fail-open）；
	//   ② 用的是无超时的 http.DefaultClient，且从不关闭 resp.Body —— 连接/fd 泄漏，
	//      存储后端变慢时网关会持续堆积 goroutine（govulncheck 也在该行报警）；
	//   ③ 让网关对用户提交的 URL 发出站请求，平白多一个 SSRF 面。
	if err := h.checkStoredObjectType(r.Context(), body.FileURL, body.Name, body.MimeType); err != nil {
		BadRequest(w, err.Error())
		return
	}

	// 资产 ID 与预签名阶段保持一致：客户端传入的 ID 只有在「合法 UUID」且
	// 「其对象键确实位于本租户前缀 media/<tenantID>/<assetID>/ 之下」时才复用，
	// 因此无法用它去覆盖他人或其他路径的对象。其余情况回退到服务端新生成。
	assetID := ""
	if uuidRe.MatchString(body.ID) && strings.Contains(body.FileURL, "/"+tenantStr(tenantID)+"/"+body.ID+"/") {
		assetID = body.ID
	}
	if assetID == "" {
		var genErr error
		assetID, genErr = id.UUID()
		if genErr != nil {
			logAndRespond(w, genErr, http.StatusInternalServerError, "generate id failed")
			return
		}
	}
	_, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO media_assets (id, tenant_id, user_id, type, name, file_url, mime_type, category, size, parent_id, created_at, updated_at)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, '', NOW(), NOW())`,
		assetID, tenantID, claims.UserID, body.Type, body.Name, body.FileURL, truncateMIME(body.MimeType), nullableStr(body.Category), body.Size)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create asset failed")
		return
	}

	OK(w, map[string]string{"id": assetID, "name": body.Name, "file_url": body.FileURL})
}

// ── Upload helpers ──

// checkStoredObjectType 从**存储后端**读取对象头部 512 字节，用 magic bytes 判定
// 真实类型，与直传路径（Upload 里 DetectContentType + isExecutableMIME）同一口径。
//
// 为什么不用 HTTP HEAD：见 CompleteUpload 里的注释——私有桶下 HEAD 会 403，
// 拿到的 Content-Type 是 S3 的错误 XML，校验形同虚设；而且那把无超时的
// DefaultClient 还会泄漏连接。直接问存储后端既准确又没有出站请求。
func (h *MediaHandler) checkStoredObjectType(ctx context.Context, fileURL, name, declaredMIME string) error {
	if h.store == nil {
		return fmt.Errorf("storage backend unavailable")
	}
	inner := h.store
	if atomic, ok := h.store.(*storage.AtomicStore); ok {
		inner = atomic.LoadRaw()
	}

	var objectKey string
	switch s := inner.(type) {
	case *storage.S3Store:
		key, ok := s.ObjectKeyFromURL(fileURL)
		if !ok {
			// host/scheme/bucket 对不上就是伪造的 file_url，直接拒（不放行）
			return fmt.Errorf("file_url does not match configured storage backend")
		}
		objectKey = key
	case *storage.LocalStore:
		// 本地后端约定 file_url 为 /media/...，相对 STORAGE_ROOT，去掉前导斜杠即对象键
		objectKey = strings.TrimPrefix(fileURL, "/")
	default:
		return fmt.Errorf("unsupported storage backend")
	}

	rc, err := h.store.OpenStream(ctx, objectKey)
	if err != nil {
		// Complete 阶段对象必须已存在；读不到说明客户端跳过了上传，
		// 继续入库只会产生一条指向空对象的记录 —— 拒绝而非放行。
		slog.Warn("complete upload: object unreadable for MIME check", "key", objectKey, "error", err)
		return fmt.Errorf("uploaded object not found")
	}
	defer rc.Close()

	head := make([]byte, 512)
	n, readErr := io.ReadFull(rc, head)
	if readErr != nil && readErr != io.ErrUnexpectedEOF && readErr != io.EOF {
		slog.Warn("complete upload: read object head failed", "key", objectKey, "error", readErr)
		return fmt.Errorf("read uploaded object failed")
	}
	head = head[:n]

	detected := truncateMIME(http.DetectContentType(head))
	if isExecutableMIME(detected, name) {
		return fmt.Errorf("file type not allowed: %s", detected)
	}
	// 客户端声明的类型也过一遍：扩展名兜底能拦下 magic bytes 认不出的 PE/ELF
	// （DetectContentType 对它们只返回 application/octet-stream）。
	if declaredMIME != "" && isExecutableMIME(declaredMIME, name) {
		return fmt.Errorf("file type not allowed: %s", declaredMIME)
	}
	return nil
}

func detectType(mime string) string {
	if strings.HasPrefix(mime, "image/") {
		return "image"
	}
	if strings.HasPrefix(mime, "video/") {
		return "video"
	}
	if strings.HasPrefix(mime, "audio/") {
		return "audio"
	}
	if strings.Contains(mime, "pdf") || strings.Contains(mime, "document") {
		return "document"
	}
	return "file"
}

// truncateMIME ensures MIME type fits in the VARCHAR(64) column.
func truncateMIME(mime string) string {
	if len(mime) > 64 {
		return mime[:64]
	}
	return mime
}

// isExecutableMIME 拦截可执行/脚本类 MIME，防止伪装为图片上传恶意文件。
// P1-6: 即使 magic bytes 检测出真实类型，仍需拒绝危险类型落地存储。
// fileName 用于扩展名兜底（net/http 的 DetectContentType 对 PE/ELF/Mach-O
// 通常返回 application/octet-stream，无法区分可执行文件与普通二进制）。
func isExecutableMIME(mime string, fileName string) bool {
	// 归一化小写并去掉参数
	m := strings.ToLower(strings.TrimSpace(mime))
	if i := strings.Index(m, ";"); i >= 0 {
		m = strings.TrimSpace(m[:i])
	}
	switch m {
	case "application/x-msdownload",
		"application/x-msdos-program",
		"application/x-msi",
		"application/x-sh",
		"application/x-shar",
		"application/x-csh",
		"application/x-bat",
		"application/x-batch",
		"application/vnd.microsoft.portable-executable",
		"application/x-elf",
		"application/x-executable",
		"application/x-mach-o-executable",
		"application/x-mach-binary":
		return true
	}
	// 扩展名兜底：octet-stream（PE/ELF/Mach-O magic bytes 通用回退）或 text/plain
	// （shell 脚本检测为 text/plain）+ 危险扩展名 → 拒绝
	if m == "application/octet-stream" || m == "" || m == "text/plain" || m == "text/html" {
		ext := strings.ToLower(filepath.Ext(fileName))
		switch ext {
		case ".exe", ".dll", ".msi", ".sh", ".bat", ".cmd", ".com", ".scr",
			".so", ".dylib", ".app", ".jar", ".class", ".py", ".rb", ".pl",
			".ps1", ".vbs", ".wsf", ".htm", ".html",
			// SVG / XHTML / XML 家族：浏览器**按文档解析并执行其中的脚本**，而
			// DetectContentType 会把它们判成 text/plain（正好走上面这个入口）⇒
			// 此前可上传 `<svg><script>…</script></svg>`，再经 /media/s/ 签名 URL
			// 以 image/svg+xml 返回 ⇒ **同源存储型 XSS**（S3 后端下 302 到对象存储域、
			// 无 CSP 兜底，直接执行）。
			".svg", ".svgz", ".xhtml", ".xht", ".xml", ".xsl", ".xslt",
			".mhtml", ".mht", ".swf":
			return true
		}
	}
	// text/plain 可能是任何脚本，但 magic bytes 只能识别文本；
	// 这里只拦截明确的可执行二进制类型，text/plain 由业务层判断扩展名
	return false
}
