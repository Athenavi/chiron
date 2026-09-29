package api

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"path"
	"strings"
	"unicode/utf8"

	"github.com/athenavi/chiron/internal/storage"
)

// agentFilePrefix 是 agent 文件在 FileStore 里的顶层命名空间。
//
// 与媒体资产等其它用途分开，便于按用途配生命周期策略（例如把 agent 工作文件放在
// 可回收的存储类上）。
const agentFilePrefix = "agent-files"

const (
	// maxListDepth 是递归列目录的深度上限（防病态目录结构）。
	maxListDepth = 8
	// maxListEntries 是单次列目录返回的条目上限。目录结构由 agent 自己写出来，
	// 没有上限时一次 list 就可能返回上万条目并拖垮引擎上下文。
	maxListEntries = 5000
)

// InternalStorageHandler 把网关侧的 FileStore（local 共享卷 / S3）暴露给引擎的
// agent 文件工具（方案 02 §3.4）。
//
// 为什么需要它：agent 的文件工具此前只认本地 `workspace_dir()`，多副本部署下
// "写 A 副本、读 B 副本"会间歇性失忆（python-engine/app/tools/sandbox.py 的注释已自陈）。
// 走这里之后，文件落在部署方配置的 FileStore 上，跨副本一致。
//
// **隔离由服务端强制**：请求只带相对路径，实际存储键由这里拼成
// `agent-files/{tenant}/{user}/{相对路径}` —— 引擎即使被攻破也无法跨租户读写。
// 这与 `/v1/internal/db/*` 只允许回环地址的既有思路一致：内部端点同样不信任调用方。
type InternalStorageHandler struct {
	store *storage.AtomicStore
}

// NewInternalStorageHandler 创建 handler；store 为 nil 时所有端点返回 503（未配置存储）。
func NewInternalStorageHandler(store *storage.AtomicStore) *InternalStorageHandler {
	return &InternalStorageHandler{store: store}
}

type storageWriteRequest struct {
	TenantID   string `json:"tenant_id"`
	UserID     string `json:"user_id"`
	Path       string `json:"path"`
	Content    string `json:"content"`
	ContentB64 string `json:"content_b64"`
}

type storagePathRequest struct {
	TenantID string `json:"tenant_id"`
	UserID   string `json:"user_id"`
	Path     string `json:"path"`
}

// sanitizeSegment 把身份段收敛到安全字符集。
//
// 身份来自 JWT/网关注入，理论上是干净的；但这里是**拼存储键**的地方，一旦有段
// 含 `/` 或 `..` 就能跳出自己的前缀 —— 所以按"不信任"处理（与 Python 侧
// sandbox 的路径段校验同一思路）。
func sanitizeSegment(segment string) string {
	var b strings.Builder
	for _, r := range segment {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-', r == '_', r == '.':
			b.WriteRune(r)
		default:
			b.WriteRune('_')
		}
	}
	out := b.String()
	if out == "" || out == "." || out == ".." {
		return "unknown"
	}
	return out
}

// resolveKey 由**服务端**拼装存储键，并拒绝任何越界路径。
func (h *InternalStorageHandler) resolveKey(tenantID, userID, rel string) (string, error) {
	normalized := strings.ReplaceAll(rel, "\\", "/")

	// 先**明确拒绝**含 `..` 段的路径，而不是交给 path.Clean ——
	// `path.Clean("/../x")` 会静默把 `..` 消解成 `/x`：行为上仍在本命名空间内（安全），
	// 但调用方拿到 200 却不知道路径已被悄悄改写，排查时极易误判。
	// 与 Python 侧 `safe_join` 的"拒绝 ../"语义保持一致。
	for _, segment := range strings.Split(normalized, "/") {
		if segment == ".." {
			return "", fmt.Errorf("path escapes namespace: %q", rel)
		}
	}

	cleaned := path.Clean("/" + normalized)
	trimmed := strings.TrimPrefix(cleaned, "/")
	if trimmed == "" {
		return "", fmt.Errorf("empty path")
	}
	return path.Join(agentFilePrefix, sanitizeSegment(tenantID), sanitizeSegment(userID), trimmed), nil
}

func (h *InternalStorageHandler) baseKey(tenantID, userID string) string {
	return path.Join(agentFilePrefix, sanitizeSegment(tenantID), sanitizeSegment(userID))
}

func (h *InternalStorageHandler) available(w http.ResponseWriter) bool {
	if h == nil || h.store == nil {
		http.Error(w, "storage backend not configured", http.StatusServiceUnavailable)
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(payload); err != nil {
		slog.Warn("internal storage response encode failed", "error", err)
	}
}

// StorageRead GET /v1/internal/storage/read?tenant_id=&user_id=&path=
//
// 文件不存在返回 **404** 而不是空内容 —— 调用方必须能区分"没有这个文件"与"文件是空的"。
func (h *InternalStorageHandler) StorageRead(w http.ResponseWriter, r *http.Request) {
	if !h.available(w) {
		return
	}
	q := r.URL.Query()
	key, err := h.resolveKey(q.Get("tenant_id"), q.Get("user_id"), q.Get("path"))
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	data, err := h.store.Read(context.WithoutCancel(r.Context()), key)
	if err != nil {
		// 存储层用错误表达"不存在"（local 与 s3 后端都是）；这里统一映射为 404，
		// 让引擎侧能明确区分"没有"与"空"。
		http.Error(w, "not found", http.StatusNotFound)
		return
	}

	// 文本优先：agent 文件工具操作的是文本，base64 只在含非 UTF-8 字节时启用
	if utf8.Valid(data) {
		writeJSON(w, http.StatusOK, map[string]any{
			"path": q.Get("path"), "content": string(data), "encoding": "utf-8", "size": len(data),
		})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"path": q.Get("path"), "content": base64.StdEncoding.EncodeToString(data), "encoding": "base64", "size": len(data),
	})
}

// StorageWrite POST /v1/internal/storage/write
func (h *InternalStorageHandler) StorageWrite(w http.ResponseWriter, r *http.Request) {
	if !h.available(w) {
		return
	}
	var req storageWriteRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid json body", http.StatusBadRequest)
		return
	}
	key, err := h.resolveKey(req.TenantID, req.UserID, req.Path)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	var payload []byte
	if req.ContentB64 != "" {
		decoded, err := base64.StdEncoding.DecodeString(req.ContentB64)
		if err != nil {
			http.Error(w, "invalid content_b64", http.StatusBadRequest)
			return
		}
		payload = decoded
	} else {
		payload = []byte(req.Content)
	}

	if err := h.store.Write(context.WithoutCancel(r.Context()), key, payload); err != nil {
		slog.Warn("internal storage write failed", "key", key, "error", err)
		http.Error(w, "write failed", http.StatusInternalServerError)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"path": req.Path, "size": len(payload)})
}

// StorageList GET /v1/internal/storage/list?tenant_id=&user_id=&prefix=
//
// 返回的是**相对路径**（剥掉 `agent-files/{tenant}/{user}/`），调用方据此拼虚拟路径。
//
// 递归展开：`FileStore.List` 只列**一层**（`LocalStore` 用 `os.ReadDir`），而 agent 的
// `glob("**/*.py")` 需要整棵树 —— 让调用方自己递归会变成 N 次跨进程请求。这里一次拿到
// 整棵树，并用深度/条目上限防病态目录。
func (h *InternalStorageHandler) StorageList(w http.ResponseWriter, r *http.Request) {
	if !h.available(w) {
		return
	}
	q := r.URL.Query()
	base := h.baseKey(q.Get("tenant_id"), q.Get("user_id"))
	prefix := strings.Trim(path.Clean("/"+strings.ReplaceAll(q.Get("prefix"), "\\", "/")), "/")

	var infos []storage.FileInfo
	h.walk(context.WithoutCancel(r.Context()), base, 0, &infos)

	files := make([]map[string]any, 0, len(infos))
	for _, info := range infos {
		// 归一化分隔符：`LocalStore.List` 用 `filepath.Join` 拼路径，在 Windows 上得到的是
		// 反斜杠形式（`agent-files\t1\u1\docs`）—— 不归一化就永远匹配不上 `base + "/"`，
		// 表现为"列目录永远为空"（这个坑在测试里才暴露出来）。
		normalizedPath := strings.ReplaceAll(info.Path, "\\", "/")
		rel := strings.TrimPrefix(normalizedPath, base+"/")
		if rel == normalizedPath { // 不在本命名空间内，跳过
			continue
		}
		if prefix != "" && prefix != "." && !strings.HasPrefix(rel, prefix) {
			continue
		}
		files = append(files, map[string]any{
			"path": rel, "size": info.Size, "is_dir": info.IsDir, "modified": info.Modified,
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"files": files})
}

// walk 深度优先展开 `FileStore.List` 的"一层"结果。
//
// 上限是**必须**的：目录结构由 agent 自己写出来，没有上限时一次 list 就可能返回
// 上万条目并拖垮引擎的上下文（与工具层的输出治理是同一类考虑）。
func (h *InternalStorageHandler) walk(ctx context.Context, key string, depth int, out *[]storage.FileInfo) {
	if depth > maxListDepth || len(*out) >= maxListEntries {
		return
	}
	infos, err := h.store.List(ctx, key)
	if err != nil {
		return // 不可读的子树跳过，不让整次 list 失败
	}
	for _, info := range infos {
		*out = append(*out, info)
		if len(*out) >= maxListEntries {
			return
		}
		if info.IsDir {
			h.walk(ctx, info.Path, depth+1, out)
		}
	}
}

// StorageDelete POST /v1/internal/storage/delete
func (h *InternalStorageHandler) StorageDelete(w http.ResponseWriter, r *http.Request) {
	if !h.available(w) {
		return
	}
	var req storagePathRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid json body", http.StatusBadRequest)
		return
	}
	key, err := h.resolveKey(req.TenantID, req.UserID, req.Path)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	if err := h.store.Delete(context.WithoutCancel(r.Context()), key); err != nil {
		slog.Warn("internal storage delete failed", "key", key, "error", err)
		http.Error(w, "delete failed", http.StatusInternalServerError)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"path": req.Path, "deleted": true})
}
