package api

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/storage"
)

// 媒体签名 URL（P0 修复：媒体不再裸公开可猜测；本地后端 HMAC，S3 后端走原生预签名）。
// 签名 = HMAC-SHA256(媒体专用密钥, assetID|tenant|user|exp)，短时效（默认 15 分钟），
// 与资产**及其归属**绑定。
//
// P1 修复：此前密钥直接复用 JWT 签名密钥、且签名输入只有 assetID|exp ——
// 一是域未分隔（一侧密钥暴露波及另一侧），二是签名不绑归属（同签名可被换 assetID）。

const mediaSignTTL = 15 * time.Minute

// mediaSignSecret 返回媒体签名专用密钥，与 JWT 签名密钥**域分隔**。
//
// 取值顺序：
//  1. MEDIA_SIGN_SECRET（≥16 字符）——专用密钥；
//  2. APP_SECRET 域分隔派生（派生域与 JWT/settings/凭据加密各不相同）；
//  3. 都没有时返回空串，调用方必须拒绝签发/校验（fail-close）。
func mediaSignSecret() string {
	if v := os.Getenv("MEDIA_SIGN_SECRET"); len(v) >= 16 {
		return v
	}
	if app := os.Getenv("APP_SECRET"); len(app) >= 8 {
		sum := sha256.Sum256([]byte("chiron-media-sign:" + app))
		return hex.EncodeToString(sum[:])
	}
	return ""
}

// signMediaURL 为资产生成签名下载 URL（校验归属后签发）。
func signMediaURL(ctx context.Context, assetID, secret string, tenantID int16, userID string) (string, error) {
	// 归属校验并获取文件扩展名
	var filePath string
	if err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COALESCE(file_path, '') FROM media_assets WHERE id = $1 AND tenant_id = $2 AND user_id = $3`,
		assetID, tenantID, userID).Scan(&filePath); err != nil {
		return "", err
	}

	// 从 file_path 提取文件扩展名
	ext := filepath.Ext(filePath)

	exp := time.Now().Add(mediaSignTTL).Unix()
	// 签名绑定 (assetID, tenant, user, exp)：A 租户拿到的 URL 无法用于访问
	// B 租户的资产，即使 assetID 被猜到或泄漏（P1）。
	sig := mediaHMAC(secret, assetID, tenantStr(tenantID), userID, exp)

	// 如果有扩展名，附加到 URL 中（flyfish-viewer 需要）
	signedURL := "/media/s/" + assetID
	if ext != "" {
		signedURL += ext
	}
	signedURL += "?exp=" + strconv.FormatInt(exp, 10) +
		"&t=" + url.QueryEscape(tenantStr(tenantID)) +
		"&u=" + url.QueryEscape(userID) +
		"&sig=" + sig

	return signedURL, nil
}

func mediaHMAC(secret, assetID, tenantID, userID string, exp int64) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(assetID + "|" + tenantID + "|" + userID + "|" + strconv.FormatInt(exp, 10)))
	return hex.EncodeToString(mac.Sum(nil))
}

// SignMedia POST /v1/media/{id}/sign —— 鉴权后签发签名 URL。
func (h *MediaHandler) SignMedia(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID == 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	assetID := r.PathValue("id")
	if assetID == "" {
		BadRequest(w, "id is required")
		return
	}
	secret := mediaSignSecret()
	if secret == "" {
		InternalError(w, "media signing key unavailable")
		return
	}
	signedURL, err := signMediaURL(r.Context(), assetID, secret, claims.TenantID, claims.UserID)
	if err != nil {
		NotFound(w, "media asset not found")
		return
	}
	OK(w, map[string]interface{}{"url": signedURL})
}

// ServeSignedMedia GET /media/s/{assetID}{ext}?exp=&t=&u=&sig= —— 校验签名后流式返回文件。
func (h *MediaHandler) ServeSignedMedia(w http.ResponseWriter, r *http.Request) {
	// 从路径中提取 assetID（去掉可能的文件扩展名）
	rawAssetID := r.PathValue("assetID")
	assetID := strings.TrimSuffix(rawAssetID, filepath.Ext(rawAssetID))

	expStr := r.URL.Query().Get("exp")
	sig := r.URL.Query().Get("sig")
	tenantID := r.URL.Query().Get("t")
	userID := r.URL.Query().Get("u")
	if assetID == "" || expStr == "" || sig == "" || tenantID == "" || userID == "" {
		http.Error(w, "missing signature params", http.StatusBadRequest)
		return
	}
	exp, err := strconv.ParseInt(expStr, 10, 64)
	if err != nil || time.Now().Unix() > exp {
		http.Error(w, "link expired", http.StatusForbidden)
		return
	}
	secret := mediaSignSecret()
	if secret == "" {
		http.Error(w, "media signing unavailable", http.StatusServiceUnavailable)
		return
	}
	// 签名绑定 (assetID, tenant, user, exp)：租户之间不可互换（P1）。
	if !hmac.Equal([]byte(sig), []byte(mediaHMAC(secret, assetID, tenantID, userID, exp))) {
		http.Error(w, "invalid signature", http.StatusForbidden)
		return
	}
	// 取文件路径
	var filePath string
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT COALESCE(file_path, '') FROM media_assets WHERE id = $1`, assetID).Scan(&filePath); err != nil || filePath == "" {

		// Fallback: 如果 file_path 为空，从 file_url 动态推导
		var fileURL string
		if err2 := db.GlobalDBManager.QueryRow(r.Context(),
			`SELECT COALESCE(file_url, '') FROM media_assets WHERE id = $1`, assetID).Scan(&fileURL); err2 != nil || fileURL == "" {
			slog.Error("ServeSignedMedia: asset not found", "assetID", assetID)
			http.Error(w, "media not found", http.StatusNotFound)
			return
		}

		// 从 file_url 推导 file_path: /media/xxx -> xxx
		if strings.HasPrefix(fileURL, "/media/") {
			filePath = strings.TrimPrefix(fileURL, "/media/")
			slog.Debug("ServeSignedMedia: derived file_path from file_url",
				"assetID", assetID, "fileURL", fileURL, "filePath", filePath)
		} else {
			slog.Error("ServeSignedMedia: invalid file_url format", "assetID", assetID, "fileURL", fileURL)
			http.Error(w, "media not found", http.StatusNotFound)
			return
		}
	}

	// 归一化 file_path：历史数据里直传路径写入的是完整对象键（含 "media/" 前缀），
	// 分片路径写入/推导的是相对媒体根的路径。统一为「相对媒体根」，否则与 mediaRoot
	// 叠加会出现 StorageRoot/media/media/... 的错位路径。
	filePath = strings.TrimPrefix(filepath.ToSlash(filePath), "media/")

	// S3 后端：签名已校验通过，直接 302 到对象存储的预签名 URL
	// （对象存储自行处理 Range/断点续传，网关不中转大文件）。
	// 本地后端：继续走下方 ServeFile，保留 Range 支持。
	if s3 := h.s3Store(); s3 != nil {
		url, perr := s3.PresignedGetURL(r.Context(), "media/"+filePath, mediaSignTTL)
		if perr != nil {
			slog.Error("ServeSignedMedia: presign failed", "assetID", assetID, "error", perr)
			http.Error(w, "media unavailable", http.StatusBadGateway)
			return
		}
		http.Redirect(w, r, url, http.StatusFound)
		return
	}

	full := filepath.Join(h.mediaRoot(), filepath.FromSlash(filePath))
	// 防御：确保解析后仍在媒体根目录内
	root := filepath.Clean(h.mediaRoot())
	clean := filepath.Clean(full)
	if clean != root && !strings.HasPrefix(clean, root+string(os.PathSeparator)) {
		http.Error(w, "invalid path", http.StatusForbidden)
		return
	}

	// 设置正确的响应头以支持音频/视频流式播放
	w.Header().Set("Accept-Ranges", "bytes")
	w.Header().Set("Cache-Control", "public, max-age=3600")

	// 根据文件扩展名设置 Content-Type（flyfish-viewer 需要）
	ext := strings.ToLower(filepath.Ext(clean))
	switch ext {
	case ".mp3":
		w.Header().Set("Content-Type", "audio/mpeg")
	case ".wav":
		w.Header().Set("Content-Type", "audio/wav")
	case ".ogg":
		w.Header().Set("Content-Type", "audio/ogg")
	case ".flac":
		w.Header().Set("Content-Type", "audio/flac")
	case ".m4a":
		w.Header().Set("Content-Type", "audio/mp4")
	case ".mp4":
		w.Header().Set("Content-Type", "video/mp4")
	case ".webm":
		w.Header().Set("Content-Type", "video/webm")
	case ".pdf":
		w.Header().Set("Content-Type", "application/pdf")
	case ".jpg", ".jpeg":
		w.Header().Set("Content-Type", "image/jpeg")
	case ".png":
		w.Header().Set("Content-Type", "image/png")
	case ".gif":
		w.Header().Set("Content-Type", "image/gif")
	}

	http.ServeFile(w, r, clean)
}

// mediaRoot 返回媒体存储根（与路由注册时 FileServer 同源）。
func (h *MediaHandler) mediaRoot() string {
	return h.root
}

// s3Store 返回底层后端为 S3 时的实例（AtomicStore 支持热切换，需解包判断）。
// 用于决定媒体下载走「预签名 302」还是「本地 ServeFile」。
func (h *MediaHandler) s3Store() *storage.S3Store {
	if h.store == nil {
		return nil
	}
	inner := h.store
	if atomic, ok := inner.(*storage.AtomicStore); ok {
		inner = atomic.LoadRaw()
	}
	if s3, ok := inner.(*storage.S3Store); ok {
		return s3
	}
	return nil
}
