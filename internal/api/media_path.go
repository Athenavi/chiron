package api

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/jackc/pgx/v5"
)

// ─────────────────────────────────────────────────────────────
// 媒体库的「长路径」 —— 把 `reports/2024/q3.md` 落成真实的文件夹层级。
//
// 历史行为是**层层压平**，三处各去掉一次路径，叠起来就是"目录结构全丢"：
//
//  1. `python-engine/app/tools/media.py:media_create` 直接拒绝带分隔符的名字
//     （`name must be a plain file name`）；
//  2. `InternalCreateAsset` 的 INSERT 把 `parent_id` 硬编码成 `''`（根目录）；
//  3. `sanitizeUploadName` 末尾还有一道 `filepath.Base` 兜底。
//
// 于是 agent 产出 `reports/2024/q3.md` 时，媒体库里只剩一个孤零零的 `q3.md`。
// 用户报"长路径没正确处理、没有文件夹嵌套"指的就是这条链。
//
// 修复的分工是**单一权威**：Go 侧负责解析与落库（本文件），Python 侧只做前置友好
// 校验，好让模型立刻拿到可操作的错误而不是等一次往返。
// ─────────────────────────────────────────────────────────────

const (
	// maxAssetFolderDepth 限制层级深度。
	//
	// 不设上限时一个 `a/a/a/...` 能创建成千上万个文件夹行，而媒体库是用户可见的树 ——
	// 深到 16 层已无实用价值，超过必然是生成侧出了问题，早拒比晚拒清楚。
	maxAssetFolderDepth = 16
	// maxAssetSegmentChars 单段长度上限。与 `media_assets.name` 的 varchar(255) 留出余量，
	// 也与 Python 侧 `MAX_NAME_CHARS=200` 对齐 —— 两边口径不一致时，前置校验会放行
	// 一个必然在网关被截断的名字，用户看到的是"创建成功但名字不对"。
	maxAssetSegmentChars = 200
)

// errInvalidAssetPath 表示路径本身不可接受（与"存储/数据库失败"必须区分：
// 前者是调用方的错，后者是我们的错，HTTP 状态码与可操作性都不同）。
var errInvalidAssetPath = errors.New("invalid media asset path")

// splitAssetPath 把 `reports/2024/q3.md` 拆成 `["reports","2024"]` 与 `q3.md`。
//
// 这是**唯一入口**：所有把用户/模型给的路径落成层级的地方都必须过它。
//
// 安全边界逐条对应一种真实危害：
//   - 拒绝**反斜杠**：`a\b` 与 `a/b` 会被当成两个不同目录，同一份内容出现两处，
//     且 Windows 侧 `filepath.Base` 与 POSIX 侧 `path.Base` 结论不同，行为不可预测；
//   - 拒绝**绝对路径**（前导 `/`）：否则"媒体库内路径"与"存储键"的边界就没了；
//   - 拒绝 `.` / `..` 与空段：`..` 是路径穿越的经典形态，空段（`a//b`）会让实际
//     层级与用户看到的路径条数不符；
//   - 拒绝控制字符：会污染日志行与 `Content-Disposition` 下载头；
//   - 拒绝超长段与过深层级：见上面两个常量的说明。
//
// 允许的字符范围刻意保持宽松（不限制中文、空格、括号等）：文件名本就是用户内容，
// 收紧正则只会误伤合法名字，而真正危险的是分隔符与遍历序列，那几个已单独拒绝。
func splitAssetPath(raw string) (folders []string, name string, err error) {
	cleaned := strings.TrimSpace(raw)
	if cleaned == "" {
		return nil, "", fmt.Errorf("%w: empty name", errInvalidAssetPath)
	}
	if strings.Contains(cleaned, `\`) {
		return nil, "", fmt.Errorf(
			"%w: use '/' as the only separator, got %q", errInvalidAssetPath, raw)
	}
	if strings.HasPrefix(cleaned, "/") {
		return nil, "", fmt.Errorf(
			"%w: paths are relative to the media library root, got %q", errInvalidAssetPath, raw)
	}
	// 尾随 `/`（`reports/2024/`）必须**拒绝**，不能修剪后接受。
	//
	// 修剪会把 `reports/2024/` 变成 `reports/2024`，于是最后一个目录段 `2024`
	// 被当成**文件名**落成一个无扩展名的资产 —— 用户以为建了目录，实际得了个怪文件。
	// （这条是测试 `拒绝:尾随斜杠` 抓出来的：初版用了 TrimSuffix，测试当场失败。）
	if strings.HasSuffix(cleaned, "/") {
		return nil, "", fmt.Errorf(
			"%w: %q ends with '/' (that is a folder path, not a file); "+
				"pass a full file path like 'dir/name.ext', or create the folder via POST /v1/media/folders",
			errInvalidAssetPath, raw)
	}

	parts := strings.Split(cleaned, "/")
	if err := checkPathSegments(parts, raw); err != nil {
		return nil, "", err
	}

	name = parts[len(parts)-1]
	folders = parts[:len(parts)-1]
	if len(folders) > maxAssetFolderDepth {
		return nil, "", fmt.Errorf(
			"%w: path nests %d levels deep (max %d)",
			errInvalidAssetPath, len(folders), maxAssetFolderDepth)
	}
	return folders, name, nil
}

// checkPathSegments 逐段校验，**文件路径与文件夹路径共用**。
//
// 共用是刻意的：分成两套规则时，"文件名不许有控制字符、目录名却可以"这类不一致
// 迟早会出现，而它就是一个绕过口子 —— 目录名里的控制字符同样会污染日志与下载头。
func checkPathSegments(parts []string, raw string) error {
	for _, part := range parts {
		if part == "" {
			return fmt.Errorf("%w: empty path segment in %q", errInvalidAssetPath, raw)
		}
		if part == "." || part == ".." {
			return fmt.Errorf(
				"%w: '.' and '..' are not allowed in %q", errInvalidAssetPath, raw)
		}
		if len([]rune(part)) > maxAssetSegmentChars {
			return fmt.Errorf(
				"%w: segment %q exceeds %d characters",
				errInvalidAssetPath, truncateForError(part), maxAssetSegmentChars)
		}
		for _, r := range part {
			if r < 0x20 || r == 0x7f {
				return fmt.Errorf(
					"%w: control characters are not allowed in %q",
					errInvalidAssetPath, truncateForError(part))
			}
		}
	}
	return nil
}

// splitFolderPath 把 `a/b/c` 拆成目录段列表（每一段都是目录）。
//
// 为什么文件夹需要**单独**一个入口：`splitAssetPath` 的契约是"最后一段是文件名"，
// 因此它拒绝尾随斜杠；而建目录时 `reports/2024/` 恰恰是合法输入（从别处复制路径
// 常带尾斜杠），且整串都是目录。
//
// 历史行为是直接把 `a/b/c` 当作**一个**名字含斜杠的文件夹插进库里 ——
// 媒体库页面上会出现一个点进去空空如也、名字还带斜杠的怪目录，
// 而用户以为建了三层结构（2026-11-03 生产报告）。
func splitFolderPath(raw string) ([]string, error) {
	cleaned := strings.TrimSpace(raw)
	if cleaned == "" {
		return nil, fmt.Errorf("%w: empty folder name", errInvalidAssetPath)
	}
	if strings.Contains(cleaned, `\`) {
		return nil, fmt.Errorf(
			"%w: use '/' as the only separator, got %q", errInvalidAssetPath, raw)
	}
	if strings.HasPrefix(cleaned, "/") {
		return nil, fmt.Errorf(
			"%w: paths are relative to the media library root, got %q", errInvalidAssetPath, raw)
	}
	// 这里**允许**尾随斜杠（与 splitAssetPath 相反）：它只是"这是目录"的冗余表达。
	cleaned = strings.TrimSuffix(cleaned, "/")
	if cleaned == "" {
		return nil, fmt.Errorf("%w: empty folder name", errInvalidAssetPath)
	}
	parts := strings.Split(cleaned, "/")
	if err := checkPathSegments(parts, raw); err != nil {
		return nil, err
	}
	if len(parts) > maxAssetFolderDepth {
		return nil, fmt.Errorf(
			"%w: path nests %d levels deep (max %d)",
			errInvalidAssetPath, len(parts), maxAssetFolderDepth)
	}
	return parts, nil
}

// truncateForError 把过长的段截短后再塞进错误信息。
//
// 直接把 2000 字符的原值拼进错误里，等于让一条错误日志把整个日志行吞掉 ——
// 排查时反而看不到真正的原因。
func truncateForError(s string) string {
	const limit = 40
	runes := []rune(s)
	if len(runes) <= limit {
		return s
	}
	return string(runes[:limit]) + "…"
}

// ensureFolderChain 在同一事务内 get-or-create 整条文件夹链，返回末级文件夹 id。
//
// `baseParent` 是链条的**起点**：空串表示从媒体库根开始。显式传入它是为了支持
// "在某个已存在的目录下再建 `a/b`" —— 用户当前所在的目录（前端 `currentParentId`）
// 就是这种情况。缺了这个参数，所有层级都会被迫从根开始，用户在子目录里新建
// `x/y` 会得到根下的 `x/y`，与它眼前的目录对不上。
//
// `folders` 为空时返回 `baseParent` 本身（"只在该目录下建一项"）。
func ensureFolderChain(
	ctx context.Context,
	tx pgx.Tx,
	tenantID int16,
	userID string,
	baseParent string,
	folders []string,
) (string, error) {
	// 起点必须是**一个真实存在、属于本用户**的文件夹。
	//
	// 缺了这一步，"父目录"就是个纯字符串：前端传一个已被删除（或伪造出来）的 id，
	// 整条链会挂在一个不存在的父节点下 —— 媒体库里永远看不到这些目录，而接口
	// 返回 200 说创建成功。表现正是"文件夹创建没被正确处理、层级乱掉"。
	// 传一个**文件**的 id 也一样：那会形成"文件下面还有目录"的诡异结构。
	if baseParent != "" {
		var ok bool
		if err := tx.QueryRow(ctx,
			`SELECT EXISTS(SELECT 1 FROM media_assets
			   WHERE id = $1 AND tenant_id = $2 AND user_id = $3 AND type = 'folder')`,
			baseParent, tenantID, userID).Scan(&ok); err != nil {
			return "", fmt.Errorf("check parent folder: %w", err)
		}
		if !ok {
			return "", fmt.Errorf(
				"%w: parent folder %q not found (it may have been deleted, or is not a folder)",
				errInvalidAssetPath, baseParent)
		}
	}

	parentID := baseParent
	// 锁键必须包含起点：不同目录下的同名子目录是不同的资源，
	// 共用一把锁只会让无关的创建互相串行。
	pathSoFar := baseParent
	for _, segment := range folders {
		pathSoFar += "/" + segment
		lockKey := fmt.Sprintf("media_folder:%d:%s:%s", tenantID, userID, pathSoFar)
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext($1))`, lockKey); err != nil {
			return "", fmt.Errorf("lock folder %q: %w", segment, err)
		}

		var folderID string
		err := tx.QueryRow(ctx,
			`SELECT id FROM media_assets
			  WHERE tenant_id = $1 AND user_id = $2 AND type = 'folder'
			    AND COALESCE(parent_id, '') = $3 AND name = $4
			  ORDER BY created_at
			  LIMIT 1`,
			tenantID, userID, parentID, segment).Scan(&folderID)

		switch {
		case err == nil:
			// 已存在：复用。这正是"同一路径再次发布"该有的行为 —— 不能每次生成
			// 一个 `reports (2)`，那会让用户的媒体库随使用次数不断膨胀。
			parentID = folderID
		case errors.Is(err, pgx.ErrNoRows):
			if err := tx.QueryRow(ctx,
				`INSERT INTO media_assets (id, tenant_id, user_id, type, name, parent_id, created_at, updated_at)
				 VALUES (gen_random_uuid(), $1, $2, 'folder', $3, $4, NOW(), NOW())
				 RETURNING id`,
				tenantID, userID, segment, parentID).Scan(&folderID); err != nil {
				return "", fmt.Errorf("create folder %q: %w", segment, err)
			}
			parentID = folderID
		default:
			return "", fmt.Errorf("lookup folder %q: %w", segment, err)
		}
	}
	return parentID, nil
}
