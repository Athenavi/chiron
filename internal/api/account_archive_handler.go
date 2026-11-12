package api

import (
	"io"
	"log/slog"
	"net/http"
	"path"
	"regexp"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/storage"
)

// account_archive_handler.go —— 账号注销归档的运维/合规通道。
//
// 归档是「到期清除前生成的个人数据快照」，写在对象存储的
// `account-archive/{user_id}/{时间戳}.json`。此前只有写入、没有读取通道 ——
// 意味着一旦用户注销，这份留底就只能靠人登进对象存储去翻。
//
// **权限刻意比其它 admin 读端点更严**（平台 owner 而非租户 admin）：
// 归档内容是完整的个人数据（邮箱、手机号、会话、积分流水、支付记录），
// 而 `admin:read` 是**租户** admin 就持有的权限。若沿用 adminReadMW，
// 任何一个租户管理员都能读到别的租户已注销用户的全部历史 ——
// 那比"没有这个接口"更糟。
//
// 只读，不提供删除：归档的保留期应由对象存储的生命周期策略统一管理，
// 应用层删除会让"什么时候删了什么"无法审计。

// archiveFileNameRe 限制文件名形态：时间戳 + .json。
//
// 路径穿越的第一道防线在存储层的 safePath，但那是在拼出完整路径之后才校验；
// 这里前置拒绝，让非法请求连对象存储都不必碰。
var archiveFileNameRe = regexp.MustCompile(`^[0-9A-Za-z_-]+\.json$`)

// ListAccountArchives GET /account-archives/{user_id} —— 列出某账号的全部归档。
//
// 返回文件名、大小、修改时间；不含内容（内容可能很大，逐个读出来列目录会
// 把内存打满）。要内容请用下面的下载端点。
func (h *AdminHandler) ListAccountArchives(w http.ResponseWriter, r *http.Request) {
	userID := r.PathValue("user_id")
	if !uuidRe.MatchString(userID) {
		// 用 UUID 校验而不只是"非空"：这个值会拼进对象键，
		// 放任任意字符串等于把路径构造交给调用方。
		BadRequest(w, "user_id must be a UUID")
		return
	}
	store, ok := h.archiveStore(w)
	if !ok {
		return
	}

	prefix := accountArchivePrefix + userID
	files, err := store.List(r.Context(), prefix)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list archives failed")
		return
	}

	out := make([]map[string]interface{}, 0, len(files))
	for _, f := range files {
		if f.IsDir {
			continue
		}
		out = append(out, map[string]interface{}{
			"name":     path.Base(f.Path),
			"size":     f.Size,
			"modified": f.Modified,
		})
	}
	OK(w, map[string]interface{}{"user_id": userID, "archives": out})
}

// DownloadAccountArchive GET /account-archives/{user_id}/{file} —— 下载单个归档。
//
// 以附件下发：归档是 JSON，浏览器内联渲染会让"分享链接"变成一次
// 完整的个人数据泄漏（与 /v1/me/export 同一考量）。
func (h *AdminHandler) DownloadAccountArchive(w http.ResponseWriter, r *http.Request) {
	userID := r.PathValue("user_id")
	file := r.PathValue("file")
	if !uuidRe.MatchString(userID) {
		BadRequest(w, "user_id must be a UUID")
		return
	}
	if !archiveFileNameRe.MatchString(file) {
		BadRequest(w, "invalid archive file name")
		return
	}
	store, ok := h.archiveStore(w)
	if !ok {
		return
	}

	key := accountArchivePrefix + userID + "/" + file
	rc, err := store.OpenStream(r.Context(), key)
	if err != nil {
		// 不存在与"存储故障"在这里难以区分，统一给 404 并留日志：
		// 对调用方而言"取不到这份归档"是可操作的信息，而把存储错误细节
		// 暴露出去只会增加探测面。
		slog.Warn("account archive: open failed",
			"user_id", userID, "file", file, "error", err)
		NotFound(w, "archive not found")
		return
	}
	defer rc.Close()

	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Content-Disposition",
		`attachment; filename="chiron-archive-`+shortID(userID)+`-`+file+`"`)
	w.WriteHeader(http.StatusOK)
	// 归档下载是**跨租户**读取个人数据的敏感操作，必须留痕 ——
	// 没有痕迹的话，事后无法回答"谁在什么时候导出过谁的归档"。
	db.AuditLog(r.Context(), claimsUserID(r), archiveAuditTenant(r),
		"account.archive_download", "/v1/admin/account-archives",
		"target_user="+userID+" file="+file, r.RemoteAddr, nil)
	if _, err := io.Copy(w, rc); err != nil {
		// 状态码已发出，只能记日志（客户端断开会走到这里，属正常）。
		slog.Warn("account archive: stream copy interrupted",
			"user_id", userID, "file", file, "error", err)
	}
}

// archiveStore 取出可用的存储后端；不可用时已写好响应。
func (h *AdminHandler) archiveStore(w http.ResponseWriter) (*storage.AtomicStore, bool) {
	if h.store == nil {
		InternalError(w, "storage backend unavailable")
		return nil, false
	}
	return h.store, true
}

// archiveAuditTenant 取当前操作者的租户，供审计记录使用。
//
// 归档下载是**跨租户**读取个人数据的敏感操作，必须留痕 —— 没有痕迹的话，
// 事后无法回答"谁在什么时候导出过谁的归档"。
func archiveAuditTenant(r *http.Request) int16 {
	if c := auth.GetClaims(r.Context()); c != nil {
		return c.TenantID
	}
	return 0
}
