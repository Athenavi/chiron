package api

import (
	"io"
	"net/http"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
)

// site_icon_handler.go —— 租户站点图标上传与公开读取。
//
// 图标必须能被浏览器直接加载（favicon 请求不带 Authorization），所以写进存储后
// 由一个**无鉴权**的公开路由按 tenant_id 返回；租户 ID 本身不是秘密。
// 只接受栅格图片（png/jpg/webp/ico），拒绝 svg —— svg 可执行脚本，属于存储型 XSS。

const maxSiteIconBytes = 2 << 20 // 2MB

var siteIconExts = map[string]bool{
	".png": true, ".jpg": true, ".jpeg": true, ".webp": true, ".ico": true,
}

func siteIconKey(tenantID int) string {
	return "site-icons/" + strconv.Itoa(tenantID)
}

// UploadTenantSiteIcon POST /v1/tenant/site-icon（管理员）
func (h *AdminHandler) UploadTenantSiteIcon(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID <= 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	if h.store == nil {
		InternalError(w, "storage unavailable")
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxSiteIconBytes+1024)
	if err := r.ParseMultipartForm(maxSiteIconBytes); err != nil {
		BadRequest(w, "icon must be an image up to 2MB")
		return
	}
	file, header, err := r.FormFile("file")
	if err != nil {
		BadRequest(w, "file is required")
		return
	}
	defer file.Close()

	ext := strings.ToLower(filepath.Ext(header.Filename))
	if !siteIconExts[ext] {
		BadRequest(w, "icon must be png/jpg/jpeg/webp/ico")
		return
	}
	data, err := io.ReadAll(io.LimitReader(file, maxSiteIconBytes+1))
	if err != nil || len(data) == 0 {
		BadRequest(w, "read icon failed")
		return
	}
	if len(data) > maxSiteIconBytes {
		BadRequest(w, "icon too large (max 2MB)")
		return
	}
	contentType := http.DetectContentType(data)
	if !strings.HasPrefix(contentType, "image/") || strings.Contains(contentType, "svg") {
		BadRequest(w, "icon must be a raster image")
		return
	}

	if err := h.store.Write(r.Context(), siteIconKey(int(claims.TenantID)), data); err != nil {
		InternalError(w, "save icon failed")
		return
	}
	OK(w, map[string]string{
		"url": "/v1/public/site-icon/" + strconv.Itoa(int(claims.TenantID)),
	})
}

// GetPublicTenantSiteIcon GET /v1/public/site-icon/{tenant_id}（无鉴权）
func (h *AdminHandler) GetPublicTenantSiteIcon(w http.ResponseWriter, r *http.Request) {
	tenantID, err := strconv.Atoi(strings.TrimSpace(r.PathValue("tenant_id")))
	if err != nil || tenantID <= 0 {
		NotFound(w, "icon not found")
		return
	}
	if h.store == nil {
		InternalError(w, "storage unavailable")
		return
	}
	data, err := h.store.Read(r.Context(), siteIconKey(tenantID))
	if err != nil || len(data) == 0 {
		NotFound(w, "icon not found")
		return
	}
	w.Header().Set("Content-Type", http.DetectContentType(data))
	w.Header().Set("Cache-Control", "public, max-age=300")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(data)
}
