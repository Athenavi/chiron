package api

import (
	"context"
	"encoding/json"
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
// Database stores only metadata 鈥?the actual file content lives in S3.
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

// 鈹€鈹€ Types 鈹€鈹€

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

// 鈹€鈹€ List 鈹€鈹€

func (h *MediaHandler) List(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())

	parentID := r.URL.Query().Get("parent_id")
	category := r.URL.Query().Get("category")
	mediaType := r.URL.Query().Get("type")
	search := r.URL.Query().Get("search")
	tagsParam := r.URL.Query().Get("tags")
	page, pageSize := parsePagination(r.URL.Query())

	where := " WHERE tenant_id = $1 AND user_id = $2 AND parent_id = $3"
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

// 鈹€鈹€ Create (text/code content) 鈹€鈹€

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
			`INSERT INTO media_assets (id, tenant_id, user_id, type, name, file_url, category, tags, metadata, size, created_at, updated_at)
			 VALUES (gen_random_uuid(), $1, $2, $3, $4, '', $5, $6, $7, $8, NOW(), NOW())
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
			`INSERT INTO media_assets (id, tenant_id, user_id, type, name, file_url, category, tags, metadata, size, created_at, updated_at)
			 VALUES (gen_random_uuid(), $1, $2, $3, $4, '', $5, $6, $7, $8, NOW(), NOW())
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

// 鈹€鈹€ CreateFolder (virtual folder) 鈹€鈹€

// CreateFolder creates a virtual folder (type='folder', no storage object).
// ListFolders 杩斿洖鐢ㄦ埛鍏ㄩ儴鏂囦欢澶癸紙鍚?parent_id锛夛紝渚涚Щ鍔ㄥ眰绾ф爲鏋勫缓銆?
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
	body.Name = strings.TrimSpace(body.Name)
	if body.Name == "" {
		BadRequest(w, "name is required")
		return
	}

	// 鍚岀骇閲嶅悕妫€鏌?
	var exists bool
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT EXISTS(SELECT 1 FROM media_assets WHERE tenant_id=$1 AND user_id=$2 AND parent_id=$3 AND name=$4)`,
		claims.TenantID, claims.UserID, body.ParentID, body.Name).Scan(&exists); err != nil {
		InternalError(w, "check folder name")
		return
	}
	if exists {
		BadRequest(w, "a folder or file with this name already exists")
		return
	}

	var id string
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`INSERT INTO media_assets (id, tenant_id, user_id, type, name, parent_id, created_at, updated_at)
		 VALUES (gen_random_uuid(), $1, $2, 'folder', $3, $4, NOW(), NOW()) RETURNING id`,
		claims.TenantID, claims.UserID, body.Name, body.ParentID).Scan(&id); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create folder failed")
		return
	}
	OK(w, map[string]string{"id": id, "name": body.Name, "type": "folder", "parent_id": body.ParentID})
}

// 鈹€鈹€ Update (rename / move) 鈹€鈹€

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

	// 鎵€鏈夋潈 + 褰撳墠鍊?
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

	// 閲嶅悕妫€鏌ワ紙鎺掗櫎鑷韩锛?
	if newName != curName || newParent != curParent {
		var exists bool
		if err := db.GlobalDBManager.QueryRow(r.Context(),
			`SELECT EXISTS(SELECT 1 FROM media_assets WHERE tenant_id=$1 AND user_id=$2 AND parent_id=$3 AND name=$4 AND id<>$5)`,
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
	// 鏍囩鐙珛鏇存柊锛堜笉褰卞搷鍚嶇О/鐖剁洰褰曟鏌ワ級
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

// assetObjectKey resolves the storage object key for an asset, preferring
// the persisted file_path column; falls back to legacy name-based key.
func (h *MediaHandler) assetObjectKey(r *http.Request, id string) (string, error) {
	var fileName, filePath, userID string
	err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT COALESCE(name,''), COALESCE(file_path,''), COALESCE(user_id,'') FROM media_assets WHERE id=$1`,
		id).Scan(&fileName, &filePath, &userID)
	if err != nil {
		return "", err
	}
	if filePath != "" {
		return filePath, nil
	}
	dir := "anonymous"
	if userID != "" {
		dir = "u_" + userID
	}
	return fmt.Sprintf("media/%s/%s_%s", dir, legacyShortAssetID(id), fileName), nil
}

// assetObjectKeys 批量解析资产对象存储 key（单次查询，修复逐资产 N+1）。
func (h *MediaHandler) assetObjectKeys(ctx context.Context, ids []string) (map[string]string, error) {
	if len(ids) == 0 {
		return map[string]string{}, nil
	}
	rows, err := db.GlobalDBManager.Query(ctx,
		`SELECT id::text, COALESCE(name,''), COALESCE(file_path,''), COALESCE(user_id,'')
		 FROM media_assets WHERE id = ANY($1)`, ids)
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

// 鈹€鈹€ Delete (recursive for folders) 鈹€鈹€

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

	// 鏌ヨ璧勪骇骞舵牎楠屾墍鏈夋潈
	if err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT 1 FROM media_assets WHERE id = $1 AND tenant_id = $2 AND user_id = $3`,
		id, claims.TenantID, claims.UserID,
	).Scan(new(int)); err != nil {
		NotFound(w, "media asset not found")
		return
	}

	ids, err := collectFolderIDs(func(parent string) ([]string, error) {
		return h.getChildren(ctx, claims.TenantID, claims.UserID, parent)
	}, id)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "collect folder tree failed")
		return
	}

	// 鍒犻櫎瀛樺偍瀵硅薄锛圖B 涓哄噯锛屽け璐ヤ粎璁版棩蹇楋級
	keys, err := h.assetObjectKeys(ctx, ids)
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

// 鈹€鈹€ BatchDelete 鈹€鈹€

// BatchDelete deletes multiple assets, folders recursively.
func (h *MediaHandler) BatchDelete(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
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

	allIDs := make([]string, 0, len(body.IDs))
	for _, id := range body.IDs {
		sub, err := collectFolderIDs(func(parent string) ([]string, error) {
			return h.getChildren(r.Context(), claims.TenantID, claims.UserID, parent)
		}, id)
		if err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "collect folder tree failed")
			return
		}
		allIDs = append(allIDs, sub...)
	}

	// 鍒犻櫎瀛樺偍瀵硅薄锛圖B 涓哄噯锛屽け璐ヤ粎璁版棩蹇楋級
	keys, err := h.assetObjectKeys(r.Context(), allIDs)
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

// 鈹€鈹€ Shared helpers 鈹€鈹€

// getChildren returns the child asset IDs for a given parent folder.
func (h *MediaHandler) getChildren(ctx context.Context, tenantID, userID, parentID string) ([]string, error) {
	rows, err := db.GlobalDBManager.Query(ctx,
		`SELECT id FROM media_assets WHERE tenant_id=$1 AND user_id=$2 AND parent_id=$3`,
		tenantID, userID, parentID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var cid string
		if err := rows.Scan(&cid); err != nil {
			return nil, err
		}
		ids = append(ids, cid)
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
func mediaObjectKey(tenantID, assetID, name string) string {
	if name == "" {
		name = "file"
	}
	return "media/" + tenantID + "/" + assetID + "/" + name
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
