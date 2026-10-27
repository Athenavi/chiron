package api

// routes_desktop.go —— 桌面端（Electron 双模式）用户侧路由。
//
// 网关只暴露桌面端**真的需要云端**的那一个能力：片段推理。其余桌面端能力
// （模式切换、组件管理、本地推理代理）都在主进程与本地后端，与云端网关无关，
// 不应该出现在这张表里。

import (
	"net/http"

	"github.com/athenavi/chiron/internal/engine"
)

// registerDesktopRoutes 注册 `/v1/desktop/**`。
//
// 鉴权口径与 `/v1/chat/submit` 一致：**用户 JWT（authMW）+ 限流（rlMW）**。
// 片段推理会真的产生上游费用，所以不能只挂 authMW —— 少一层限流就等于给
// 每个登录用户开了一条不限速的打上游通道。
func registerDesktopRoutes(
	mux *http.ServeMux,
	authMW, rlMW routeMiddleware,
	pythonClient *engine.PythonClient,
) {
	// 契约：docs/desktop-local/contracts.md §9（请求/响应形状、幂等、用量口径）。
	mux.Handle("POST /v1/desktop/infer", authMW(rlMW(DesktopInferHandler(pythonClient))))
}
