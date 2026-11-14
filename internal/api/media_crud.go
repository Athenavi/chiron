package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/storage"
)

// MediaHandler manages media assets stored in object storage (S3/MinIO).
// Database stores only metadata — the actual file content lives in S3.
type MediaHandler struct {
	store         storage.FileStore
	authenticator *auth.Authenticator
	root          string // 本地媒体存储根（签名下载用）
}

func NewMediaHandler(store storage.FileStore, authenticator *auth.Authenticator) *MediaHandler {
	return &MediaHandler{store: store, authenticator: authenticator}
}

// SetMediaRoot 注入本地媒体存储根（路由装配时调用）。
func (h *MediaHandler) SetMediaRoot(root string) {
	h.root = root
}

// ── Types ──

type MediaAsset struct {
	ID        string                 `json:"id"`
	Type      string                 `json:"type"`
	Name      string                 `json:"name"`
	FileURL   string                 `json:"file_url"`
	MimeType  string                 `json:"mime_type,omitempty"`
	Thumbnail string                 `json:"thumbnail,omitempty"`
	Metadata  map[string]interface{} `json:"metadata,omitempty"`
	Tags      []string               `json:"tags,omitempty"`
	Category  string                 `json:"category,omitempty"`
	Size      int64                  `json:"size"`
	CreatedAt time.Time              `json:"created_at"`
	UpdatedAt time.Time              `json:"updated_at"`
}

// ── List ──

func (h *MediaHandler) List(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())

	parentID := r.URL.Query().Get("parent_id")
	category := r.URL.Query().Get("category")
	mediaType := r.URL.Query().Get("type")
	search := r.URL.Query().Get("search")
	tagsParam := r.URL.Query().Get("tags")
	page, pageSize := parsePagination(r.URL.Query())

	// ⚠ `parent_id` 必须用 `COALESCE(parent_id, '')` 比较，不能直接等值。
	//
	// 根目录在库里有**两种表示**：空串（用户直传路径写明 `parent_id`）与 NULL
	// （会话内创建的路径过去**整列不写**，见 `media_internal.go:InternalCreateAsset`）。
	// 而 SQL 里 `NULL = ''` 求值为 NULL（不成立），于是 `parent_id = ''` 只命中前者 ——
	// 会话创建的文件**明明在库里，却在媒体库根目录永远看不到**（2026-11-03 生产）。
	//
	// 症状具有迷惑性：用户报"文件没进媒体库"，实际记录齐全、对象存储也正常，
	// 只是列表的 WHERE 把它们筛掉了。这正是"缺失数据库记录"这种描述会把人带偏的地方。
	//
	// 选 COALESCE 而不是 `(parent_id = $3 OR (parent_id IS NULL AND $3 = ''))`：
	// 后者虽能复用索引，但 OR 分支常被优化器拆成两次扫描，而媒体库单用户资产量级
	// 很小（索引前两列 tenant_id/user_id 仍然生效），正确性远比这点差异重要。
	where := " WHERE tenant_id = $1 AND user_id = $2 AND COALESCE(parent_id, '') = $3"
	args := []interface{}{claims.TenantID, claims.UserID, parentID}
	argIdx := 4

	if category != "" {
		where += fmt.Sprintf(" AND category = $%d", argIdx)
		args = append(args, category)
		argIdx++
	}
	if mediaType != "" {
		where += fmt.Sprintf(" AND type = $%d", argIdx)
		args = append(args, mediaType)
		argIdx++
	}
	if search != "" {
		where += fmt.Sprintf(" AND name ILIKE $%d", argIdx)
		args = append(args, "%"+search+"%")
		argIdx++
	}
	if tagsParam != "" {
		// 逗号分隔标签：匹配全部（tags 包含所有指定标签）
		tagList := strings.Split(tagsParam, ",")
		clean := make([]string, 0, len(tagList))
		for _, t := range tagList {
			if t = strings.TrimSpace(t); t != "" {
				clean = append(clean, t)
			}
		}
		if len(clean) > 0 {
			// tags 是逗号分隔的字符串，使用 LIKE 匹配每个标签
			for _, tag := range clean {
				where += fmt.Sprintf(" AND (tags LIKE $%d OR tags LIKE $%d OR tags LIKE $%d)", argIdx, argIdx+1, argIdx+2)
				args = append(args, tag+",%", "%,"+tag+",%", "%,"+tag)
				argIdx += 3
			}
		}
	}

	var total int64
	if err := db.GlobalDBManager.QueryRow(r.Context(), "SELECT COUNT(*) FROM media_assets"+where, args...).Scan(&total); err != nil {
		InternalError(w, "count media assets")
		return
	}

	query := `SELECT id, type, name, COALESCE(file_url, ''), COALESCE(mime_type, ''),
		COALESCE(thumbnail, ''), COALESCE(metadata, '{}'), COALESCE(tags, ''), COALESCE(category, ''), COALESCE(size, 0), created_at, updated_at
		FROM media_assets` + where +
		" ORDER BY (type = 'folder') DESC, name ASC LIMIT $%d OFFSET $%d"
	args = append(args, pageSize, (page-1)*pageSize)
	query = fmt.Sprintf(query, argIdx, argIdx+1)

	rows, err := db.GlobalDBManager.Query(r.Context(), query, args...)
	if err != nil {
		InternalError(w, "query media assets")
		return
	}
	defer rows.Close()

	items := make([]MediaAsset, 0, pageSize)
	for rows.Next() {
		var a MediaAsset
		var metadataJSON []byte
		var tagsStr string
		if err := rows.Scan(&a.ID, &a.Type, &a.Name, &a.FileURL, &a.MimeType,
			&a.Thumbnail, &metadataJSON, &tagsStr, &a.Category, &a.Size, &a.CreatedAt, &a.UpdatedAt); err != nil {
			slog.Error("scan media asset row failed", "error", err, "id", a.ID, "type", a.Type)
			continue
		}
		if len(metadataJSON) > 0 && string(metadataJSON) != "{}" && string(metadataJSON) != "null" {
			if err := json.Unmarshal(metadataJSON, &a.Metadata); err != nil {
				slog.Warn("media: unmarshal metadata failed", "error", err)
			}
		}
		if tagsStr != "" {
			a.Tags = strings.Split(tagsStr, ",")
		}
		items = append(items, a)
	}
	if err := rows.Err(); err != nil {
		slog.Error("iterate media assets failed", "error", err)
		InternalError(w, "iterate media assets")
		return
	}

	OK(w, map[string]interface{}{"items": items, "total": total, "page": page, "page_size": pageSize})
}

// ── Create (text/code content) ──

func (h *MediaHandler) Create(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	var body struct {
		Type     string                 `json:"type"`
		Name     string                 `json:"name"`
		Content  string                 `json:"content"`
		Category string                 `json:"category"`
		Tags     []string               `json:"tags"`
		Metadata map[string]interface{} `json:"metadata"`
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
		body.Type = "text"
	}

	// 使用 PostgreSQL 的 gen_random_uuid() 生成 UUID
	var assetID string
	fileURL := ""

	if body.Content != "" {
		// 先插入数据库获取 UUID
		metadataJSON, _ := json.Marshal(body.Metadata)
		tagsStr := ""
		if len(body.Tags) > 0 {
			tagsStr = strings.Join(body.Tags, ",")
		}

		err := db.GlobalDBManager.QueryRow(r.Context(),
			`INSERT INTO media_assets (id, tenant_id, user_id, type, name, file_url, category, tags, metadata, size, parent_id, created_at, updated_at)
			 VALUES (gen_random_uuid(), $1, $2, $3, $4, '', $5, $6, $7, $8, '', NOW(), NOW())
			 RETURNING id`,
			claims.TenantID, claims.UserID, body.Type, body.Name, nullableStr(body.Category), tagsStr, string(metadataJSON), len(body.Content),
		).Scan(&assetID)
		if err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "create asset failed")
			return
		}

		objectKey := mediaObjectKey(claims.TenantID, assetID, sanitizeUploadName(body.Name))
		if err := h.store.Write(r.Context(), objectKey, []byte(body.Content)); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "save file failed")
			return
		}
		fileURL = h.objectURL(objectKey)

		// 鏇存柊 file_url
		_, err = db.GlobalDBManager.Exec(r.Context(),
			`UPDATE media_assets SET file_url = $1 WHERE id = $2`,
			fileURL, assetID)
		if err != nil {
			slog.Warn("update file_url", "error", err)
		}
	} else {
		metadataJSON, _ := json.Marshal(body.Metadata)
		tagsStr := ""
		if len(body.Tags) > 0 {
			tagsStr = strings.Join(body.Tags, ",")
		}

		err := db.GlobalDBManager.QueryRow(r.Context(),
			`INSERT INTO media_assets (id, tenant_id, user_id, type, name, file_url, category, tags, metadata, size, parent_id, created_at, updated_at)
			 VALUES (gen_random_uuid(), $1, $2, $3, $4, '', $5, $6, $7, $8, '', NOW(), NOW())
			 RETURNING id`,
			claims.TenantID, claims.UserID, body.Type, body.Name, nullableStr(body.Category), tagsStr, string(metadataJSON), 0,
		).Scan(&assetID)
		if err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "create asset failed")
			return
		}
	}

	OK(w, map[string]string{"id": assetID, "name": body.Name, "type": body.Type, "file_url": fileURL})
}

// ── CreateFolder (virtual folder) ──

// CreateFolder creates a virtual folder (type='folder', no storage object).
// ListFolders 返回用户全部文件夹（含 parent_id），供移动层级树构建。
func (h *MediaHandler) ListFolders(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())

	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT id, name, COALESCE(parent_id::text, '') FROM media_assets
		 WHERE tenant_id = $1 AND user_id = $2 AND type = 'folder'
		 ORDER BY name`, claims.TenantID, claims.UserID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list folders failed")
		return
	}
	defer rows.Close()

	type folderNode struct {
		ID       string `json:"id"`
		Name     string `json:"name"`
		ParentID string `json:"parent_id"`
	}
	folders := make([]folderNode, 0, 16)
	for rows.Next() {
		var f folderNode
		if err := rows.Scan(&f.ID, &f.Name, &f.ParentID); err != nil {
			continue
		}
		folders = append(folders, f)
	}
	OK(w, folders)
}

// CreateFolder creates a folder asset.
//
// 支持长路径：`a/b/c` 会建出三层目录，而不是**一个**名字含斜杠的目录。
//
// 历史行为是把整串当成名字直接 INSERT —— 媒体库页面上于是出现一个点进去空空如也、
// 名字还带 `a/b/c` 斜杠的怪条目，而用户以为自己建好了三层结构（2026-11-03 生产报告）。
// 根因与文件侧的"长路径被压平"是同一个：路径在入口处没有被拆开。
func (h *MediaHandler) CreateFolder(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())

	var body struct {
		Name     string `json:"name"`
		ParentID string `json:"parent_id"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	segments, pathErr := splitFolderPath(body.Name)
	if pathErr != nil {
		BadRequest(w, pathErr.Error())
		return
	}

	tenantID := claims.TenantID
	tx, err := db.GlobalDBManager.Begin(r.Context())
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "begin transaction failed")
		return
	}
	defer func() { _ = tx.Rollback(r.Context()) }()

	// 前 N-1 段 get-or-create：路径里带的既有目录直接复用。
	// 若这里也要求"必须不存在"，用户在高亮某目录后再输入完整路径就会莫名报错。
	parentID, err := ensureFolderChain(
		r.Context(), tx, tenantID, claims.UserID, body.ParentID, segments[:len(segments)-1])
	if err != nil {
		// 父目录不存在是**调用方的错**（400），不是我们的错（500）。
		// 混成 500 会让前端只显示"服务器错误"，用户永远不知道该换个目录。
		if errors.Is(err, errInvalidAssetPath) {
			BadRequest(w, err.Error())
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "create folder chain failed")
		return
	}

	// 末段必须**不存在**：这是"新建文件夹"的语义。
	// 静默复用会让用户以为建了新目录，实际点到的是旧的那个（里面可能有别人的东西）。
	leaf := segments[len(segments)-1]
	// 同层重名检查同样要 COALESCE：根目录在库里可能是 NULL（会话创建路径的旧数据），
	// 直接 `parent_id = $3` 会让"根目录下已存在同名文件夹"判断失效 ⇒ 允许创建重名，
	// 而两者的 `parent_id` 一个 NULL 一个空串，之后任何按名查找都会命中两条。
	var exists bool
	if err := tx.QueryRow(r.Context(),
		`SELECT EXISTS(SELECT 1 FROM media_assets WHERE tenant_id=$1 AND user_id=$2 AND COALESCE(parent_id, '')=$3 AND name=$4)`,
		tenantID, claims.UserID, parentID, leaf).Scan(&exists); err != nil {
		InternalError(w, "check folder name")
		return
	}
	if exists {
		BadRequest(w, "a folder or file with this name already exists")
		return
	}

	var folderID string
	if err := tx.QueryRow(r.Context(),
		`INSERT INTO media_assets (id, tenant_id, user_id, type, name, parent_id, created_at, updated_at)
		 VALUES (gen_random_uuid(), $1, $2, 'folder', $3, $4, NOW(), NOW()) RETURNING id`,
		tenantID, claims.UserID, leaf, parentID).Scan(&folderID); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create folder failed")
		return
	}
	if err := tx.Commit(r.Context()); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "commit failed")
		return
	}

	OK(w, map[string]string{
		"id":        folderID,
		"name":      leaf,
		"type":      "folder",
		"parent_id": parentID,
		// 回显规范化后的完整路径：调用方据此确认层级被正确理解。
		"path": strings.Join(segments, "/"),
	})
}

// ── Update (rename / move) ──

// Update renames or moves a media asset (folder or file).
func (h *MediaHandler) Update(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())

	id := r.PathValue("id")
	if id == "" {
		BadRequest(w, "id is required")
		return
	}

	var body struct {
		Name     *string   `json:"name"`
		ParentID *string   `json:"parent_id"`
		Tags     *[]string `json:"tags"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.Name == nil && body.ParentID == nil && body.Tags == nil {
		BadRequest(w, "nothing to update")
		return
	}

	// 校验所有权 + 当前层级?
	var curName, curParent string
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT COALESCE(name,''), COALESCE(parent_id,'') FROM media_assets WHERE id=$1 AND tenant_id=$2 AND user_id=$3`,
		id, claims.TenantID, claims.UserID).Scan(&curName, &curParent); err != nil {
		NotFound(w, "media asset not found")
		return
	}

	newName := curName
	if body.Name != nil {
		newName = strings.TrimSpace(*body.Name)
		if newName == "" {
			BadRequest(w, "name cannot be empty")
			return
		}
		// 重命名同样不许出现路径分隔符。
		//
		// `Update` 只改**这一项**的 name，不建层级；放行 `a/b` 会让库里多出一个
		// 名字含斜杠的条目 —— 与"新建文件夹时整串被当名字"是同一个坑的另一半：
		// 列表按 name 渲染，用户看到的东西与它点进去的层级对不上。
		// 想调整层级请用 parent_id（移动），不要靠改名字。
		if strings.ContainsAny(newName, "/\\") {
			BadRequest(w, "name must not contain path separators; use parent_id to move the item")
			return
		}
	}
	newParent := curParent
	if body.ParentID != nil {
		newParent = *body.ParentID
	}

	// 绉诲姩闃茬幆
	if body.ParentID != nil {
		cycle, err := wouldCreateCycle(func(pid string) (string, error) {
			var p string
			err := db.GlobalDBManager.QueryRow(r.Context(),
				`SELECT COALESCE(parent_id,'') FROM media_assets WHERE id=$1 AND tenant_id=$2 AND user_id=$3`,
				pid, claims.TenantID, claims.UserID).Scan(&p)
			if err != nil {
				return "", err
			}
			return p, nil
		}, id, newParent)
		if err != nil {
			InternalError(w, "check move target")
			return
		}
		if cycle {
			BadRequest(w, "cannot move a folder into itself or its own descendant")
			return
		}
	}

	// 重名检查（排除自身）
	if newName != curName || newParent != curParent {
		var exists bool
		if err := db.GlobalDBManager.QueryRow(r.Context(),
			`SELECT EXISTS(SELECT 1 FROM media_assets WHERE tenant_id=$1 AND user_id=$2 AND COALESCE(parent_id, '')=$3 AND name=$4 AND id<>$5)`,
			claims.TenantID, claims.UserID, newParent, newName, id).Scan(&exists); err != nil {
			InternalError(w, "check name conflict")
			return
		}
		if exists {
			BadRequest(w, "a folder or file with this name already exists")
			return
		}
	}

	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`UPDATE media_assets SET name=$1, parent_id=$2, updated_at=NOW() WHERE id=$3 AND tenant_id=$4 AND user_id=$5`,
		newName, newParent, id, claims.TenantID, claims.UserID); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "update media asset failed")
		return
	}
	// 标签独立更新（不影响名称/父目录检查）
	if body.Tags != nil {
		if _, err := db.GlobalDBManager.Exec(r.Context(),
			`UPDATE media_assets SET tags=$1, updated_at=NOW() WHERE id=$2 AND tenant_id=$3 AND user_id=$4`,
			*body.Tags, id, claims.TenantID, claims.UserID); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "update media tags failed")
			return
		}
	}
	OK(w, map[string]string{"id": id, "name": newName, "parent_id": newParent})
}


// assetObjectKeys 批量解析资产对象存储 key（单次查询，修复逐资产 N+1）。
func (h *MediaHandler) assetObjectKeys(ctx context.Context, ids []string, tenantID int16, userID string) (map[string]string, error) {
	if len(ids) == 0 {
		return map[string]string{}, nil
	}
	// 必须带 tenant/user 过滤：ids 可源自请求体（客户端可控），无过滤会解析出
	// 他人资产的对象键，进而被 store.Delete 删除他人文件（跨租户数据破坏）。
	rows, err := db.GlobalDBManager.Query(ctx,
		`SELECT id::text, COALESCE(name,''), COALESCE(file_path,''), COALESCE(user_id,'')
		 FROM media_assets WHERE id = ANY($1) AND tenant_id = $2 AND user_id = $3`,
		ids, tenantID, userID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	keys := make(map[string]string, len(ids))
	for rows.Next() {
		var id, fileName, filePath, userID string
		if err := rows.Scan(&id, &fileName, &filePath, &userID); err != nil {
			continue
		}
		if filePath != "" {
			keys[id] = filePath
			continue
		}
		dir := "anonymous"
		if userID != "" {
			dir = "u_" + userID
		}
		keys[id] = fmt.Sprintf("media/%s/%s_%s", dir, legacyShortAssetID(id), fileName)
	}
	return keys, rows.Err()
}

// ── Delete (recursive for folders) ──

func (h *MediaHandler) Delete(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	ctx := r.Context()

	id := r.PathValue("id")
	if id == "" {
		BadRequest(w, "id is required")
		return
	}

	// 查询资产并校验所有权
	ids, err := h.collectSubtreeIDs(ctx, claims.TenantID, claims.UserID, []string{id})
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "collect folder tree failed")
		return
	}
	if len(ids) == 0 {
		NotFound(w, "media asset not found")
		return
	}

	// 删除存储对象（DB 为准，失败仅记日志）
	keys, err := h.assetObjectKeys(ctx, ids, claims.TenantID, claims.UserID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "resolve media keys failed")
		return
	}
	for _, aid := range ids {
		if key, ok := keys[aid]; ok && key != "" {
			if err := h.store.Delete(ctx, key); err != nil {
				slog.Warn("failed to delete media object", "key", key, "error", err)
			}
		}
	}

	if _, err := db.GlobalDBManager.Exec(ctx,
		`DELETE FROM media_assets WHERE id = ANY($1) AND tenant_id=$2 AND user_id=$3`,
		ids, claims.TenantID, claims.UserID); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "delete media asset failed")
		return
	}

	OK(w, map[string]interface{}{"status": "deleted", "deleted": len(ids)})
}

// ── BatchDelete ──

// BatchDelete deletes multiple assets, folders recursively.
func (h *MediaHandler) BatchDelete(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	var body struct {
		IDs []string `json:"ids"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if len(body.IDs) == 0 {
		BadRequest(w, "ids is required")
		return
	}

	ctx := r.Context()

	// 归属过滤 + 子树展开一次完成：递归项里带 tenant_id/user_id，因此
	// 传入他人的 assetID 不会进入结果集（静默跳过，避免枚举探测）。
	allIDs, err := h.collectSubtreeIDs(ctx, claims.TenantID, claims.UserID, body.IDs)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "collect folder tree failed")
		return
	}
	if len(allIDs) == 0 {
		NotFound(w, "media asset not found")
		return
	}

	// 删除存储对象（DB 为准，失败仅记日志）
	keys, err := h.assetObjectKeys(ctx, allIDs, claims.TenantID, claims.UserID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "resolve media keys failed")
		return
	}
	for _, id := range allIDs {
		if key, ok := keys[id]; ok && key != "" {
			if err := h.store.Delete(r.Context(), key); err != nil {
				slog.Warn("failed to delete media object", "key", key, "error", err)
			}
		}
	}

	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`DELETE FROM media_assets WHERE id = ANY($1) AND tenant_id=$2 AND user_id=$3`,
		allIDs, claims.TenantID, claims.UserID); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "batch delete media assets failed")
		return
	}
	OK(w, map[string]interface{}{"deleted": len(allIDs)})
}

// ── Shared helpers ──

// collectSubtreeIDs 一次递归 CTE 取回 rootIDs 及其全部后代（文件夹与文件），去重。
//
// 归属过滤（tenant_id / user_id）写在**递归项里**，所以结果集天然只含当前用户可见的节点：
// 传入他人的 assetID 得到空集，不会展开到别人的文件树 —— 这一点比"先展开再过滤"更硬。
// 环由 `UNION`（非 `UNION ALL`）在 id 上消掉；`depth` 兜底防止畸形父指针无限递归。
//
// 相比原先的逐层 BFS：depth=N 的目录从 N 次 DB 往返降为 1 次。
func (h *MediaHandler) collectSubtreeIDs(
	ctx context.Context, tenantID int16, userID string, rootIDs []string,
) ([]string, error) {
	if len(rootIDs) == 0 {
		return nil, nil
	}
	rows, err := db.GlobalDBManager.Query(ctx, `
		WITH RECURSIVE subtree(id, depth) AS (
			SELECT id, 0 FROM media_assets
			 WHERE id = ANY($1) AND tenant_id = $2 AND user_id = $3
			UNION
			SELECT m.id, s.depth + 1 FROM media_assets m
			  JOIN subtree s ON m.parent_id = s.id
			 WHERE m.tenant_id = $2 AND m.user_id = $3 AND s.depth < $4
		)
		SELECT id FROM subtree`, rootIDs, tenantID, userID, maxFolderDepth)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	ids := make([]string, 0, len(rootIDs))
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

// objectURL constructs the public URL for an object.
func (h *MediaHandler) objectURL(objectKey string) string {
	inner := h.store
	if atomic, ok := h.store.(*storage.AtomicStore); ok {
		inner = atomic.LoadRaw()
	}
	if s3store, ok := inner.(*storage.S3Store); ok {
		return s3store.ObjectURL(objectKey)
	}
	return "/" + objectKey
}

// mediaObjectKey 组装媒体资产的对象键。
//
// 布局：media/<tenantID>/<assetID>/<name>
//   - tenantID：多租户隔离前缀，防止跨租户覆盖与枚举，也让生命周期/授权策略可按前缀下发；
//   - assetID ：完整资产 ID（UUID v4），作为唯一性来源 —— 绝不截断。历史实现用
//     shortAssetID 取前 8 字符，而 snowflake 派生的 ID 高位是毫秒时间戳，
//     同一毫秒内前 8 字符必然相同，同名文件会直接互相覆盖；
//   - name    ：已净化的展示文件名，仅用于 Content-Disposition 与人工排查。
func mediaObjectKey(tenantID int16, assetID, name string) string {
	if name == "" {
		name = "file"
	}
	return "media/" + tenantStr(tenantID) + "/" + assetID + "/" + name
}

// legacyShortAssetID 返回历史（v1）对象键的 8 字符前缀。
//
// 仅用于**读取** file_path 为空的存量资产 —— 这些对象仍按旧布局
// media/u_<userID>/<id[:8]>_<name> 存放，改布局会导致老文件全部 404。
// 所有新写入一律走 mediaObjectKey（完整 ID + 租户前缀）。
func legacyShortAssetID(id string) string {
	if len(id) > 8 {
		return id[:8]
	}
	return id
}
