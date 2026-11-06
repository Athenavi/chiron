package api

import (
	"context"
	"errors"
	"path/filepath"
	"strings"

	"github.com/athenavi/chiron/internal/db"
)

// 媒体资产的**定位**只有这一份实现。
//
// 为什么抽出来：签名下载（`ServeSignedMedia`）与内部取字节（`InternalReadAsset`）
// 要回答的是同一个问题 —— "这个 (assetID, tenant, user) 对应的对象键是什么"。
// 此前这段查询与"`file_path` 为空时从 `file_url` 推导"的逻辑只存在于签名下载里，
// 新端点若另写一份，两处迟早对历史资产的推导口径产生分歧：一边能读、一边 404，
// 而那种差异在线上表现为"某些老附件偶尔取不回来"。
//
// **归属谓词写在查询里**（`id + tenant_id + user_id` 三者同时成立）：调用方传错租户
// 只会得到"不存在"，不会得到别人的文件。签名校验与查询谓词是两道独立的关，
// 不靠"签名已经验过了"来省掉后者。

// errMediaAssetNotFound 统一表示"不存在 / 不属于你" —— 调用方一律按 404 处理，
// 不区分两种情况（区分即等于给出资产是否存在的信息，可被用来枚举）。
var errMediaAssetNotFound = errors.New("media asset not found")

// mediaAssetObject 是一次定位的结果：对象键、媒体类型、展示名与大小。
type mediaAssetObject struct {
	// ObjectKey 是**完整对象键**（`media/<tenant>/<id>/<name>`），交给 FileStore 用。
	ObjectKey string
	// RelPath 是相对媒体根的路径（`<tenant>/<id>/<name>`）。
	// 本地后端要拼 `mediaRoot + RelPath` 才能 ServeFile（Range / 音频拖动需要它）。
	RelPath string
	MimeType string
	Name     string
	Size     int64
}

// resolveMediaAssetObject 按归属定位媒体资产。
//
// 以函数变量的形式存在，是为了让不连数据库的单元测试能替换它（与
// `marketItemLookup` / `resolveModelAccess` 的做法一致）：仓库没有 sqlmock，
// 单元测试也不许连真库。
var resolveMediaAssetObject = func(
	ctx context.Context, assetID string, tenantID int16, userID string,
) (mediaAssetObject, error) {
	var obj mediaAssetObject
	var filePath, fileURL string
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COALESCE(file_path, ''), COALESCE(file_url, ''),
		        COALESCE(mime_type, ''), COALESCE(name, ''), COALESCE(size, 0)
		   FROM media_assets
		  WHERE id = $1 AND tenant_id = $2 AND user_id = $3`,
		assetID, tenantID, userID).Scan(&filePath, &fileURL, &obj.MimeType, &obj.Name, &obj.Size)
	if err != nil {
		return mediaAssetObject{}, errMediaAssetNotFound
	}
	obj.ObjectKey, obj.RelPath, err = normalizeMediaObjectKey(filePath, fileURL)
	if err != nil {
		return mediaAssetObject{}, err
	}
	return obj, nil
}

// normalizeMediaObjectKey 把库里存的两列收敛成一份对象键 —— **纯函数**，不查库。
//
// 拆出来是为了能直接测"历史形态"：`file_path` 为空、只写了 `file_url` 的存量资产，
// 以及前缀存成 `media/...` 的老记录。这两类只在真实数据里出现，而它们出问题时的
// 表现是"某些老附件偶尔取不回来"，靠手工点验很难覆盖。
func normalizeMediaObjectKey(filePath, fileURL string) (objectKey, relPath string, err error) {
	key := strings.TrimSpace(filePath)
	if key == "" {
		// 历史资产（早期直传路径只写 `file_url`）仍然要能读：从 URL 推导对象键。
		// 只认本地后端的 `/media/` 前缀 —— 其它形态（例如 S3 的绝对 URL）
		// 无法在不知道 bucket/prefix 的情况下反推，宁可 404 也不猜。
		if !strings.HasPrefix(fileURL, "/media/") {
			return "", "", errMediaAssetNotFound
		}
		key = strings.TrimPrefix(fileURL, "/media/")
	}

	// 历史上两条写入路径分别存过 "media/..." 与相对媒体根的 "..."。
	// 统一成"完整对象键 / 相对媒体根"两份，避免 `StorageRoot/media/media/...` 的错位。
	rel := strings.TrimPrefix(filepath.ToSlash(key), "media/")
	if rel == "" || strings.Contains(rel, "..") {
		return "", "", errMediaAssetNotFound
	}
	return "media/" + rel, rel, nil
}
