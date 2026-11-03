package api

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/jackc/pgx/v5"
)

// media_copy.go —— `POST /v1/media/copy`：复制资产（含文件夹整棵子树）。
//
// 为什么需要这个接口：前端右键菜单里"复制/粘贴"是一等公民操作，但在本接口出现
// 之前，剪贴板只能承载"剪切"（落地走已有的 PUT /v1/media/{id} 移动）。用户复制
// 一个文件后按 Ctrl+V 没有任何反应，菜单项也只好长期置灰 —— 一个每天都要用的
// 交互缺失，比缺一个高级功能更伤。
//
// 三个设计取舍：
//
//  1. **字节流搬运用 OpenStream + WriteStream**，不用 Read/Write。后者把整个对象
//     读进内存，复制一个 2GB 视频就会打爆进程 —— 这是纯内存实现的经典事故点。
//
//  2. **库记录在单个事务内建完，对象字节在提交后搬运**。反过来（先写对象再建库）
//     一旦中途失败，对象存储里会留下一批无人引用的孤儿文件，既占空间又无法从界面
//     删除。而先建库后搬字节的最坏情况是"记录存在但 file_url 为空"，前端表现为
//     一条待修复的条目，可被重试，不会污染存储。
//
//  3. **同名冲突按 Windows 规则改名**（`a.pdf` → `a (2).pdf`），而不是报错拒绝。
//     资源管理器里 Ctrl+V 到同一目录就是这个行为；报错会让"复制到原目录"这条
//     最常用的路径直接失败，而用户的意图通常是"我要一份副本"。

// copyNode 是从 media_assets 取出的、复制时需要的最小字段集。
type copyNode struct {
	Name     string
	Type     string
	MimeType string
	Category string
	Size     int64
}

// pendingObject 描述"事务提交后要把哪个源对象搬到哪个新键"。
type pendingObject struct {
	AssetID string
	SrcKey  string
	DstKey  string
	Size    int64
}

// Copy 复制一个或多个资产到指定目录。
func (h *MediaHandler) Copy(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	var body struct {
		IDs      []string `json:"ids"`
		ParentID string   `json:"parent_id"`
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
	tenantID, userID := claims.TenantID, claims.UserID

	// 源资产的对象键一次批量解析（含存量旧布局），避免逐资产查询。
	srcKeys, err := h.assetObjectKeys(ctx, body.IDs, tenantID, userID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "resolve source keys failed")
		return
	}

	tx, err := db.GlobalDBManager.Begin(ctx)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "begin copy tx failed")
		return
	}
	// 提交前任何早退都回滚，避免半棵树落库。
	defer func() { _ = tx.Rollback(ctx) }()

	var created []string
	var pending []pendingObject
	for _, srcID := range body.IDs {
		// 逐个复制；同一请求内先复制的项已成为后续冲突检查的"已有项"，
		// 因此"复制两个同名文件到同一目录"会得到 `a` 与 `a (2)` 而不是互相覆盖。
		if err := h.copyTree(ctx, tx, tenantID, userID, srcID, body.ParentID, srcKeys, &created, &pending); err != nil {
			if err == errAssetNotFound {
				NotFound(w, "media asset not found")
				return
			}
			logAndRespond(w, err, http.StatusInternalServerError, "copy asset failed")
			return
		}
	}

	if err := tx.Commit(ctx); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "commit copy failed")
		return
	}

	// 库记录已落定，再搬字节。单条失败只记日志：记录已存在、可重试，
	// 不因此让整个请求失败（用户已拿到可用的空条目，而不是"全部失败"）。
	for _, p := range pending {
		if err := h.streamCopy(ctx, p); err != nil {
			slog.Warn("copy object failed", "asset_id", p.AssetID, "src", p.SrcKey, "dst", p.DstKey, "error", err)
			continue
		}
		if _, err := db.GlobalDBManager.Exec(ctx,
			`UPDATE media_assets SET file_url=$1 WHERE id=$2 AND tenant_id=$3 AND user_id=$4`,
			h.objectURL(p.DstKey), p.AssetID, tenantID, userID); err != nil {
			slog.Warn("update copied file_url failed", "asset_id", p.AssetID, "error", err)
		}
	}

	OK(w, map[string]interface{}{"status": "copied", "ids": created, "count": len(created)})
}

var errAssetNotFound = fmt.Errorf("media asset not found")

// copyTree 递归复制一项。文件夹会先建自身，再对每个子项递归调用。
//
// `tx` 而不是全局连接：整棵子树的库记录要么全成、要么全不成。
func (h *MediaHandler) copyTree(
	ctx context.Context,
	tx pgx.Tx,
	tenantID int16,
	userID, srcID, dstParent string,
	srcKeys map[string]string,
	created *[]string,
	pending *[]pendingObject,
) error {
	node, err := loadCopyNodeInTx(ctx, tx, tenantID, userID, srcID)
	if err != nil {
		return err
	}

	name, err := uniqueSiblingNameInTx(ctx, tx, tenantID, userID, dstParent, node.Name, node.Type)
	if err != nil {
		return err
	}

	var newID string
	if err := tx.QueryRow(ctx,
		`INSERT INTO media_assets (id, tenant_id, user_id, type, name, file_url, mime_type, category, size, parent_id, created_at, updated_at)
		 VALUES (gen_random_uuid(), $1, $2, $3, $4, '', $5, $6, $7, $8, NOW(), NOW())
		 RETURNING id`,
		tenantID, userID, node.Type, name, nullableStr(node.MimeType), nullableStr(node.Category), node.Size, dstParent,
	).Scan(&newID); err != nil {
		return fmt.Errorf("insert copied asset: %w", err)
	}
	*created = append(*created, newID)

	if node.Type == "folder" {
		children, err := childIDsInTx(ctx, tx, tenantID, userID, srcID)
		if err != nil {
			return err
		}
		for _, cid := range children {
			if err := h.copyTree(ctx, tx, tenantID, userID, cid, newID, srcKeys, created, pending); err != nil {
				return err
			}
		}
		return nil
	}

	// 文件：记下待搬运对象。源键可能为空（存量记录 file_path 为空且旧布局也解析不到），
	// 此时只建记录、不搬字节 —— 与 Create 的"空内容资产"语义一致。
	if srcKey := srcKeys[srcID]; srcKey != "" {
		*pending = append(*pending, pendingObject{
			AssetID: newID,
			SrcKey:  srcKey,
			DstKey:  mediaObjectKey(tenantID, newID, sanitizeUploadName(name)),
			Size:    node.Size,
		})
	}
	return nil
}

// streamCopy 把一个对象从源键流式搬到目标键，内存占用与文件大小无关。
func (h *MediaHandler) streamCopy(ctx context.Context, p pendingObject) error {
	rc, err := h.store.OpenStream(ctx, p.SrcKey)
	if err != nil {
		return err
	}
	defer func() { _ = rc.Close() }()
	return h.store.WriteStream(ctx, p.DstKey, rc, p.Size, 0o644)
}

func loadCopyNodeInTx(ctx context.Context, tx pgx.Tx, tenantID int16, userID, id string) (copyNode, error) {
	var n copyNode
	var mime, category *string
	err := tx.QueryRow(ctx,
		`SELECT name, COALESCE(type,''), mime_type, category, COALESCE(size, 0)
		   FROM media_assets WHERE id=$1 AND tenant_id=$2 AND user_id=$3`,
		id, tenantID, userID).Scan(&n.Name, &n.Type, &mime, &category, &n.Size)
	if err != nil {
		return n, errAssetNotFound
	}
	if mime != nil {
		n.MimeType = *mime
	}
	if category != nil {
		n.Category = *category
	}
	return n, nil
}

func childIDsInTx(ctx context.Context, tx pgx.Tx, tenantID int16, userID, parent string) ([]string, error) {
	rows, err := tx.Query(ctx,
		`SELECT id FROM media_assets WHERE tenant_id=$1 AND user_id=$2 AND COALESCE(parent_id,'')=$3`,
		tenantID, userID, parent)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

// uniqueSiblingNameInTx 在 `parent` 下找一个不冲突的名字（Windows 资源管理器规则）。
//
// 文件夹没有扩展名，` (2)` 直接追加；文件则插在最后一个点之前 —— `report.pdf`
// 的副本是 `report (2).pdf` 而不是 `report.pdf (2)`（后者会变成一个无扩展名文件，
// 在 Windows 上双击打不开）。
func uniqueSiblingNameInTx(
	ctx context.Context,
	tx pgx.Tx,
	tenantID int16,
	userID, parent, name, typ string,
) (string, error) {
	stem, ext := name, ""
	if typ != "folder" {
		if i := strings.LastIndex(name, "."); i > 0 {
			stem, ext = name[:i], name[i:]
		}
	}
	candidate := name
	// 上限防止病态数据下无限循环；1000 个同名副本已经远超正常使用。
	for n := 2; n < 1000; n++ {
		var exists bool
		if err := tx.QueryRow(ctx,
			`SELECT EXISTS(SELECT 1 FROM media_assets
			   WHERE tenant_id=$1 AND user_id=$2 AND COALESCE(parent_id,'')=$3 AND name=$4)`,
			tenantID, userID, parent, candidate).Scan(&exists); err != nil {
			return "", fmt.Errorf("check name conflict: %w", err)
		}
		if !exists {
			return candidate, nil
		}
		candidate = fmt.Sprintf("%s (%d)%s", stem, n, ext)
	}
	return "", fmt.Errorf("too many name collisions for %q", name)
}
