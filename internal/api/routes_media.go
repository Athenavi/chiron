package api

import (
	"net/http"
)

// routes_media.go —— 媒体的路由注册。
// 由 gateway_router.go 按业务域拆分而来（纯移动：函数签名、函数体、调用点一字未改）。

// ── Media ──

func registerMediaRoutes(
	mux *http.ServeMux,
	mediaHandler *MediaHandler,
	authMW, rlMW routeMiddleware,
	storageRoot string,
) {
	// Media (auth + rate limited)
	mux.Handle("GET /v1/media", authMW(rlMW(http.HandlerFunc(mediaHandler.List))))
	mux.Handle("POST /v1/media", authMW(rlMW(http.HandlerFunc(mediaHandler.Create))))
	mux.Handle("POST /v1/media/folders", authMW(rlMW(http.HandlerFunc(mediaHandler.CreateFolder))))
	mux.Handle("GET /v1/media/folders", authMW(rlMW(http.HandlerFunc(mediaHandler.ListFolders))))
	mux.Handle("POST /v1/media/upload", authMW(rlMW(http.HandlerFunc(mediaHandler.Upload))))
	mux.Handle("POST /v1/media/presign", authMW(rlMW(http.HandlerFunc(mediaHandler.PresignUpload))))
	mux.Handle("POST /v1/media/complete", authMW(rlMW(http.HandlerFunc(mediaHandler.CompleteUpload))))
	mux.Handle("POST /v1/media/batch-delete", authMW(rlMW(http.HandlerFunc(mediaHandler.BatchDelete))))
	mux.Handle("PUT /v1/media/{id}", authMW(rlMW(http.HandlerFunc(mediaHandler.Update))))
	mux.Handle("GET /v1/media/{id}/download", authMW(rlMW(http.HandlerFunc(mediaHandler.Download))))
	mux.Handle("POST /v1/media/{id}/share", authMW(rlMW(http.HandlerFunc(mediaHandler.Share))))
	mux.Handle("DELETE /v1/media/{id}", authMW(rlMW(http.HandlerFunc(mediaHandler.Delete))))

	// Media file serving（P0 安全修复：禁止目录遍历，仅允许签名URL访问）
	// 原代码允许通过 /media/ 直接浏览所有用户文件，现已移除
	// 所有媒体访问必须通过签名URL (/media/s/{assetID}) 或 API端点 (/v1/media/{id}/download)

	// 签名 URL（P0 修复）：签发 + 校验后服务
	mux.Handle("POST /v1/media/{id}/sign", authMW(rlMW(http.HandlerFunc(mediaHandler.SignMedia))))
	mux.Handle("GET /media/s/{assetID}", rlMW(http.HandlerFunc(mediaHandler.ServeSignedMedia)))
}
