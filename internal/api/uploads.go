package api

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/id"
	"github.com/athenavi/chiron/internal/storage"
)

// UploadHandler 提供通用分片上传（断点续传）：
//   - Init        POST   /v1/uploads            → upload_id / chunk_size / chunk_count
//   - PutChunk    PUT    /v1/uploads/{id}/chunks/{index}
//   - GetProgress GET    /v1/uploads/{id}       → received_chunks（断点续传依据）
//   - Complete    POST   /v1/uploads/{id}/complete → 合并并按 purpose 落库
//
// 分片与最终对象都写入 storage.FileStore（local/s3 统一）—— 旧实现直接写本地盘，
// 多副本部署下"写分片"与"合并分片"会落在不同实例导致上传损坏/失败。
type UploadHandler struct {
	authenticator *auth.Authenticator
	// storageRoot 为历史遗留字段：分片合并已改为流式拼接（chunkStream），
	// 不再写本地临时文件。保留字段以免改动构造签名，可后续清理。
	storageRoot string
	// store 分片与最终对象的持久化后端（多副本下状态共享）
	store *storage.AtomicStore
}

func NewUploadHandler(a *auth.Authenticator, storageRoot string, store *storage.AtomicStore) *UploadHandler {
	return &UploadHandler{authenticator: a, storageRoot: storageRoot, store: store}
}

const defaultChunkSize = 2 << 20 // 2MB

// maxKBDocSize 限制 kb_doc 文档大小（防 finalizeKBDoc 整文件读入内存导致 OOM）。
const maxKBDocSize int64 = 64 << 20 // 64MB

// validUploadNameRe 用于文件名净化：拒绝路径分隔符与目录穿越序列。
// 仅允许字母数字、中文等常规字符、点、下划线、连字符、空格。
var validUploadNameRe = regexp.MustCompile(`^[^\x00-\x1f/\\]+$`)

// uuidRe 验证标准 UUID 格式（xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx），防止路径穿越。
var uuidRe = regexp.MustCompile(`^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$`)

// sanitizeUploadName 净化上传文件名（P0-S3 路径穿越修复）：
// 拒绝包含路径分隔符或空白的名字；剥离潜在遍历；限制长度。
func sanitizeUploadName(name string) string {
	name = strings.TrimSpace(name)
	if name == "" || len(name) > 255 {
		return ""
	}
	if !validUploadNameRe.MatchString(name) {
		return ""
	}
	if name == "." || name == ".." {
		return ""
	}
	// 防御：仅保留文件名部分（杜绝任何残余分隔符场景）
	name = filepath.Base(filepath.Clean(name))
	if name == "." || name == ".." {
		return ""
	}
	return name
}

// chunkObjectKey 返回分片在存储后端中的对象路径。
// 验证 uploadID 为 UUID 格式，防止路径穿越（s3 与 local 都按此路径寻址）。
func chunkObjectKey(uploadID string, idx int) (string, error) {
	// 只允许标准 UUID 格式（十六进制+连字符），拒绝 ../ 等路径穿越
	if !uuidRe.MatchString(uploadID) {
		return "", fmt.Errorf("invalid upload ID: %q", uploadID)
	}
	return fmt.Sprintf("uploads/%s/chunk_%d", uploadID, idx), nil
}

func (h *UploadHandler) userID(r *http.Request) (string, bool) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		return "", false
	}
	return claims.UserID, true
}

// claimsOf 返回当前请求的 claims（含 tenant_id 与 user_id）。
func (h *UploadHandler) claimsOf(r *http.Request) (*auth.Claims, bool) {
	c := auth.GetClaims(r.Context())
	if c == nil || c.TenantID == 0 {
		return nil, false
	}
	return c, true
}

// ── Init ────────────────────────────────────────────────────────────

func (h *UploadHandler) Init(w http.ResponseWriter, r *http.Request) {
	claims, ok := h.claimsOf(r)
	if !ok {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	var body struct {
		Name      string `json:"name"`
		Size      int64  `json:"size"`
		MimeType  string `json:"mime_type"`
		Purpose   string `json:"purpose"`   // media / kb_doc / generic
		ParentID  string `json:"parent_id"` // media 文件夹 id；kb_doc 时传 kb_id
		Category  string `json:"category"`
		ChunkSize int    `json:"chunk_size"` // 可选，默认 2MB
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, "invalid request")
		return
	}
	body.Name = strings.TrimSpace(body.Name)
	if body.Name == "" || body.Size <= 0 {
		BadRequest(w, "name and size are required")
		return
	}
	// P0-S3 路径穿越修复：文件名必须净化，拒绝 / \ .. 等
	body.Name = sanitizeUploadName(body.Name)
	if body.Name == "" {
		BadRequest(w, "invalid file name")
		return
	}
	// P0 存储型 XSS 防护（分片路径补齐）：可执行/脚本 MIME 与 html/xml 类扩展名一律拒绝
	// （直传路径已有 isExecutableMIME；此处覆盖分片上传，避免 .html 被按 text/html 同源输出）
	if isExecutableMIME(body.MimeType, body.Name) {
		BadRequest(w, "file type not allowed")
		return
	}
	if ext := strings.ToLower(filepath.Ext(body.Name)); ext == ".html" || ext == ".htm" ||
		ext == ".xml" || ext == ".xhtml" || ext == ".swf" ||
		// SVG 家族：与 isExecutableMIME 同口径 —— 浏览器按文档解析并执行其中的脚本，
		// 此前只拦了 .html/.xml/.xhtml/.swf，漏了 .svg（同源存储型 XSS）。
		ext == ".svg" || ext == ".svgz" || ext == ".xsl" || ext == ".xslt" || ext == ".mht" {
		BadRequest(w, "file type not allowed: "+ext)
		return
	}
	if body.Purpose == "" {
		body.Purpose = "generic"
	}
	if body.Purpose != "media" && body.Purpose != "kb_doc" && body.Purpose != "generic" {
		BadRequest(w, "purpose must be media / kb_doc / generic")
		return
	}
	// P0-P3 防护：kb_doc 会整文件读入内存（知识文档 content 列），限制大小
	if body.Purpose == "kb_doc" && body.Size > maxKBDocSize {
		BadRequest(w, "kb_doc upload too large (max 64MB)")
		return
	}
	chunkSize := body.ChunkSize
	if chunkSize <= 0 {
		chunkSize = defaultChunkSize
	}
	if chunkSize > 64<<20 {
		chunkSize = 64 << 20
	}
	chunkCount := int((body.Size + int64(chunkSize) - 1) / int64(chunkSize))
	if chunkCount <= 0 {
		chunkCount = 1
	}

	// 租户配额门禁（分片路径）：按声明的总大小预检存储容量 + 文件数量 —— 既避免把
	// 超限文件完整传完才在收尾阶段被拒，也堵住"用分片上传绕过直传配额"的口子。
	if allowed, reason := CheckUploadAllowed(r.Context(), tenantStr(claims.TenantID), body.Size); !allowed {
		JSON(w, http.StatusRequestEntityTooLarge, APIResponse{Success: false, Error: reason})
		return
	}

	uploadID, err := id.UUID()
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "generate id failed")
		return
	}
	now := time.Now()
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO uploads (id, tenant_id, user_id, name, size, mime_type, purpose, parent_id, category, chunk_size, chunk_count, status, created_at, updated_at)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'uploading', $12, $12)`,
		uploadID, claims.TenantID, claims.UserID, body.Name, body.Size, truncateMIME(body.MimeType), body.Purpose,
		body.ParentID, body.Category, chunkSize, chunkCount, now); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "init upload failed")
		return
	}

	OK(w, map[string]interface{}{
		"upload_id":   uploadID,
		"chunk_size":  chunkSize,
		"chunk_count": chunkCount,
	})
}

// ── PutChunk ────────────────────────────────────────────────────────

func (h *UploadHandler) PutChunk(w http.ResponseWriter, r *http.Request) {
	claims, ok := h.claimsOf(r)
	if !ok {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	uploadID := r.PathValue("id")
	idxStr := r.PathValue("index")
	idx, err := strconv.Atoi(idxStr)
	if err != nil || idx < 0 {
		BadRequest(w, "invalid chunk index")
		return
	}

	// 归属校验（含 tenant_id，防跨租户读写分片）
	var owner string
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT user_id FROM uploads WHERE id = $1 AND tenant_id = $2`, uploadID, claims.TenantID).Scan(&owner); err != nil || owner != claims.UserID {
		NotFound(w, "upload not found")
		return
	}

	// 限制单分片大小，防止恶意客户端发送超大 chunk 撑爆磁盘（P1-1）
	r.Body = http.MaxBytesReader(w, r.Body, 64<<20)
	// P0 扩展性：分片写入存储后端（local/s3 统一），多副本下任意实例都能读取并合并。
	// 0o600 保持原有"分片仅本进程可读"的隔离语义（本地后端生效；对象存储由 bucket 策略约束）。
	key, err := chunkObjectKey(uploadID, idx)
	if err != nil {
		BadRequest(w, "invalid upload id")
		return
	}
	if err := h.store.WriteStream(r.Context(), key, r.Body, -1, 0o600); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "write chunk failed")
		return
	}

	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`UPDATE uploads SET chunks_received = (
			SELECT jsonb_agg(elem)::text
			FROM (
				SELECT elem FROM jsonb_array_elements_text(
					COALESCE(NULLIF(chunks_received, '')::jsonb, '[]'::jsonb)
				) AS elem
				UNION
				SELECT $1::text AS elem
			) sub
		), updated_at = NOW()
		 WHERE id = $2 AND tenant_id = $3`, idxStr, uploadID, claims.TenantID); err != nil {
		slog.Warn("failed to record upload", "error", err)
	}
	OK(w, map[string]interface{}{"upload_id": uploadID, "index": idx, "received": true})
}

// ── GetProgress ─────────────────────────────────────────────────────

func (h *UploadHandler) GetProgress(w http.ResponseWriter, r *http.Request) {
	claims, ok := h.claimsOf(r)
	if !ok {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	uploadID := r.PathValue("id")
	var received []string
	var status string
	err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT chunks_received, status FROM uploads WHERE id = $1 AND tenant_id = $2 AND user_id = $3`, uploadID, claims.TenantID, claims.UserID).
		Scan(&received, &status)
	if err != nil {
		NotFound(w, "upload not found")
		return
	}
	chunks := make([]int, 0, len(received))
	for _, s := range received {
		if n, err := strconv.Atoi(s); err == nil {
			chunks = append(chunks, n)
		}
	}
	sort.Ints(chunks)
	OK(w, map[string]interface{}{
		"upload_id":       uploadID,
		"received_chunks": chunks,
		"status":          status,
	})
}

// ── Complete ────────────────────────────────────────────────────────

func (h *UploadHandler) Complete(w http.ResponseWriter, r *http.Request) {
	claims, ok := h.claimsOf(r)
	if !ok {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	uploadID := r.PathValue("id")

	var up struct {
		ID        string
		UserID    string
		Name      string
		Size      int64
		MimeType  string
		Purpose   string
		ParentID  string
		Category  string
		ChunkSize int
		ChunkCnt  int
		Received  []string
	}
	var receivedJSON string
	err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT id, user_id, name, size, mime_type, purpose, parent_id, category, chunk_size, chunk_count, chunks_received
		 FROM uploads WHERE id = $1 AND tenant_id = $2 AND user_id = $3`, uploadID, claims.TenantID, claims.UserID).
		Scan(&up.ID, &up.UserID, &up.Name, &up.Size, &up.MimeType, &up.Purpose,
			&up.ParentID, &up.Category, &up.ChunkSize, &up.ChunkCnt, &receivedJSON)
	if err != nil {
		slog.Error("query upload failed", "upload_id", uploadID, "error", err)
		NotFound(w, "upload not found")
		return
	}
	// Parse chunks_received from JSON string to []string
	if receivedJSON != "" && receivedJSON != "[]" {
		if parseErr := json.Unmarshal([]byte(receivedJSON), &up.Received); parseErr != nil {
			slog.Warn("parse chunks_received failed", "upload_id", uploadID, "error", parseErr, "raw", receivedJSON)
			up.Received = []string{}
		}
	}
	slog.Info("upload complete check", "upload_id", uploadID, "received_count", len(up.Received), "chunk_count", up.ChunkCnt, "received_json", receivedJSON)
	if len(up.Received) != up.ChunkCnt {
		BadRequest(w, fmt.Sprintf("incomplete upload: %d/%d chunks received", len(up.Received), up.ChunkCnt))
		return
	}

	// 校验总分片大小（在写入目标对象之前完成，不落地临时文件）
	totalSize, err := h.chunkTotalSize(r.Context(), uploadID, up.ChunkCnt)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "verify chunks failed")
		return
	}
	if totalSize != up.Size {
		BadRequest(w, fmt.Sprintf("size mismatch: got %d want %d", totalSize, up.Size))
		return
	}

	// 按序流式拼接分片（分片来自存储后端，任意实例都能读）。
	// 直接喂给 finalize 写入目标对象，不再合并到本地临时文件 ——
	// 消除一次完整的磁盘写入与读回（旧实现：分片→临时文件→目标对象）。
	stream, err := h.chunkStream(r.Context(), uploadID, up.ChunkCnt)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "merge chunks failed")
		return
	}
	defer stream.Close()

	// assetID 仅在 purpose=media 时由 finalizeMedia 返回（其余用途不落 media_assets）
	var fileURL string
	var assetID string
	switch up.Purpose {
	case "media":
		assetID, fileURL, err = h.finalizeMedia(r, tenantStr(claims.TenantID), up, stream)
	case "kb_doc":
		fileURL, err = h.finalizeKBDoc(r, tenantStr(claims.TenantID), up, stream)
	default:
		fileURL, err = h.finalizeGeneric(up, stream)
	}
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "finalize upload failed")
		return
	}

	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`UPDATE uploads SET status = 'completed', updated_at = NOW() WHERE id = $1 AND tenant_id = $2`, uploadID, claims.TenantID); err != nil {
		slog.Warn("failed to record upload", "error", err)
	}
	// 清理存储后端上的分片（对象存储无 RemoveAll 语义，按 index 逐个删除）
	for i := 0; i < up.ChunkCnt; i++ {
		if key, kerr := chunkObjectKey(uploadID, i); kerr == nil {
			_ = h.store.Delete(r.Context(), key)
		}
	}

	OK(w, map[string]interface{}{
		"upload_id": uploadID, "file_url": fileURL, "asset_id": assetID,
		"purpose": up.Purpose, "name": up.Name, "size": up.Size,
	})
}

// chunkStream 按 index 顺序把存储后端上的分片读流拼接成单个流。
//
// 调用方（finalize*）直接把它写入目标对象，不再落地本地临时文件：
// 旧实现（mergeChunks）先把全部分片合并成本地临时文件、再整份读回上传，
// 相当于一次完整磁盘写入 + 一次完整读回。分片本身来自 FileStore，
// 多副本下任意实例都能读取。
// 调用方负责 Close —— Close 会关闭其下所有分片流。
func (h *UploadHandler) chunkStream(ctx context.Context, uploadID string, count int) (io.ReadCloser, error) {
	readers := make([]io.Reader, 0, count)
	closers := make([]io.Closer, 0, count)
	closeAll := func() {
		for _, c := range closers {
			_ = c.Close()
		}
	}
	for i := 0; i < count; i++ {
		key, err := chunkObjectKey(uploadID, i)
		if err != nil {
			closeAll()
			return nil, err
		}
		part, err := h.store.OpenStream(ctx, key)
		if err != nil {
			closeAll()
			return nil, fmt.Errorf("open chunk %d: %w", i, err)
		}
		readers = append(readers, part)
		closers = append(closers, part)
	}
	return &multiReadCloser{reader: io.MultiReader(readers...), closers: closers}, nil
}

// multiReadCloser 组合 io.MultiReader 与多个底层流的 Close。
type multiReadCloser struct {
	reader  io.Reader
	closers []io.Closer
}

func (m *multiReadCloser) Read(p []byte) (int, error) { return m.reader.Read(p) }

func (m *multiReadCloser) Close() error {
	var firstErr error
	for _, c := range m.closers {
		if err := c.Close(); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}

// chunkTotalSize 汇总全部分片在存储后端上的字节数，用于在写入目标对象**之前**
// 校验总大小 —— 替代旧实现"合并到临时文件后 Stat"的校验方式（FileStore 无 Stat，
// 但 List 返回的 FileInfo 带 Size，零额外读取）。
func (h *UploadHandler) chunkTotalSize(ctx context.Context, uploadID string, count int) (int64, error) {
	// 列出该上传的全部分片，按 chunk_<index> 精确匹配，避免把无关对象计入
	infos, err := h.store.List(ctx, "uploads/"+uploadID+"/")
	if err != nil {
		return 0, err
	}
	sizes := make(map[string]int64, len(infos))
	for _, fi := range infos {
		sizes[filepath.Base(fi.Path)] = fi.Size
	}
	var total int64
	for i := 0; i < count; i++ {
		name := fmt.Sprintf("chunk_%d", i)
		size, ok := sizes[name]
		if !ok {
			return 0, fmt.Errorf("chunk %d missing", i)
		}
		total += size
	}
	return total, nil
}

// finalizeMedia 合并文件写入 media 存储区并落 media_assets（按当前租户）。
func (h *UploadHandler) finalizeMedia(r *http.Request, tenantID string, up struct {
	ID        string
	UserID    string
	Name      string
	Size      int64
	MimeType  string
	Purpose   string
	ParentID  string
	Category  string
	ChunkSize int
	ChunkCnt  int
	Received  []string
}, stream io.Reader) (string, string, error) {
	name := sanitizeUploadName(up.Name)
	if name == "" {
		return "", "", fmt.Errorf("invalid upload name")
	}
	// 先用资产 ID 组装对象键：对象键的唯一性来源必须是完整 assetID，并与
	// media_assets 记录一一对应。旧实现取 uploadID 的前 8 字符，既与入库的
	// assetID 不一致，也存在同前缀互相覆盖的风险。
	assetID, err := id.UUID()
	if err != nil {
		return "", "", err
	}
	objectKey := mediaObjectKey(parseTenantID(tenantID), assetID, name)
	// 流式落盘到存储后端（多副本统一可见）。旧实现直接写本地盘：s3 后端下会"上传成功但下载 404"。
	if err := h.store.WriteStream(r.Context(), objectKey, stream, up.Size, 0o644); err != nil {
		return "", "", err
	}

	assetType := detectType(up.MimeType)
	// file_path 写「相对媒体根」的路径（不含 "media/" 前缀），与签名下载侧的归一化语义一致
	relPath := strings.TrimPrefix(objectKey, "media/")
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO media_assets (id, tenant_id, user_id, type, name, file_url, file_path, mime_type, category, size, parent_id, created_at, updated_at)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW(), NOW())`,
		assetID, tenantID, up.UserID, assetType, up.Name, "/"+objectKey, relPath,
		truncateMIME(up.MimeType), nullableStr(up.Category), up.Size, up.ParentID); err != nil {
		return "", "", err
	}
	// 同时返回 assetID：分片上传完成后前端需要它换取签名 URL（与媒体库一致）
	return assetID, "/" + objectKey, nil
}

// finalizeKBDoc 合并文件内容落 knowledge_documents（content bytea 供 RAG 构建，按当前租户）。
func (h *UploadHandler) finalizeKBDoc(r *http.Request, tenantID string, up struct {
	ID        string
	UserID    string
	Name      string
	Size      int64
	MimeType  string
	Purpose   string
	ParentID  string
	Category  string
	ChunkSize int
	ChunkCnt  int
	Received  []string
}, stream io.Reader) (string, error) {
	if up.ParentID == "" {
		return "", fmt.Errorf("kb_doc upload requires parent_id (kb_id)")
	}
	// 校验 KB 存在且归属当前租户当前用户
	var owner string
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT user_id FROM knowledge_bases WHERE id = $1 AND tenant_id = $2`, up.ParentID, tenantID).Scan(&owner); err != nil || owner != up.UserID {
		return "", fmt.Errorf("knowledge base not found or not owned")
	}
	content, err := io.ReadAll(io.LimitReader(stream, maxKBDocSize+1))
	if err != nil {
		return "", err
	}
	if int64(len(content)) > maxKBDocSize {
		return "", fmt.Errorf("kb_doc content exceeds 64MB limit")
	}
	ext := strings.TrimPrefix(filepath.Ext(up.Name), ".")
	if ext == "" {
		ext = "txt"
	}
	docID, err := id.UUID()
	if err != nil {
		return "", err
	}
	_, err = db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO knowledge_documents (id, tenant_id, knowledge_base_id, user_id, name, file_type, file_size_bytes, status, content, created_at, updated_at)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', $8, NOW(), NOW())`,
		docID, tenantID, up.ParentID, up.UserID, up.Name, ext, up.Size, content)
	if err != nil {
		return "", err
	}
	// 重算 KB 统计（带 tenant_id 限制）
	_, _ = db.GlobalDBManager.Exec(r.Context(),
		`UPDATE knowledge_bases
		 SET document_count = (SELECT COUNT(*) FROM knowledge_documents WHERE knowledge_base_id = $1 AND tenant_id = $2),
		     total_size_bytes = COALESCE((SELECT SUM(file_size_bytes) FROM knowledge_documents WHERE knowledge_base_id = $1 AND tenant_id = $2), 0),
		     updated_at = NOW() WHERE id = $1 AND tenant_id = $2`, up.ParentID, tenantID)
	return fmt.Sprintf("/kb/%s/documents/%s", up.ParentID, docID), nil
}

// finalizeGeneric 合并文件写入通用目录并返回可访问 URL。
func (h *UploadHandler) finalizeGeneric(up struct {
	ID        string
	UserID    string
	Name      string
	Size      int64
	MimeType  string
	Purpose   string
	ParentID  string
	Category  string
	ChunkSize int
	ChunkCnt  int
	Received  []string
}, stream io.Reader) (string, error) {
	name := sanitizeUploadName(up.Name)
	if name == "" {
		return "", fmt.Errorf("invalid upload name")
	}
	// 完整 uploadID 作为目录段隔离（UUID），不再截断前 8 字符
	objectKey := fmt.Sprintf("uploads/final/%s/%s", up.ID, name)
	// 流式落盘到存储后端（多副本统一可见）
	if err := h.store.WriteStream(context.Background(), objectKey, stream, up.Size, 0o644); err != nil {
		return "", err
	}
	return "/" + objectKey, nil
}

// ── 路由注册 ───────────────────────────────────────────────────────

func (h *UploadHandler) RegisterRoutes(mux *http.ServeMux, authMW func(http.Handler) http.Handler, rlMW func(http.Handler) http.Handler) {
	mux.Handle("POST /v1/uploads", authMW(rlMW(http.HandlerFunc(h.Init))))
	// 上传分片使用更宽松的限流策略（避免并发上传触发限流）
	mux.Handle("PUT /v1/uploads/{id}/chunks/{index}", authMW(http.HandlerFunc(h.PutChunk)))
	mux.Handle("GET /v1/uploads/{id}", authMW(rlMW(http.HandlerFunc(h.GetProgress))))
	mux.Handle("POST /v1/uploads/{id}/complete", authMW(rlMW(http.HandlerFunc(h.Complete))))
}
