package api

// desktop_infer.go —— 桌面端云端片段推理的网关代理（`POST /v1/desktop/infer`）。
//
// 这个端点在网关里单独存在，而不是让桌面端直连 Python 引擎，原因只有三条：
//
//	1. 身份：引擎的 AuthMiddleware 只在 ENGINE_ALLOW_DIRECT_JWT 打开时才接受用户 JWT，
//	   生产上默认关闭；网关是唯一稳定能把用户 JWT 换成 (user_id, tenant_id) 的地方。
//	2. 限流：片段推理是"按段落调用"的高频入口，必须和 /v1/chat/submit 一样过 rlMW，
//	   否则它就成了绕过限流打上游的第二条通道。
//	3. 隐私：请求体是**用户的本地文档片段**。网关不解析、不落日志、不落库，只做字节
//	   透传（ForwardRequest 是流式的，2M 字符的片段不会被整个读进内存，也不会被
//	   panic 时的 body dump 带出来）。
//
// 计费目前**不在网关扣 credits**：本端点不读响应体（读了就等于解析用户内容），而
// credits 的计价口径（provider 定价 × 计费 token）在 Go billing 模块里。当前由引擎把
// 用量累加进 `desktop:infer:usage:{tenant}:{date}`（见 app/api/desktop_infer.py），
// 供对账与后续自动计费使用；把 credits 扣费接到这条链路是后续工作，不是本文件的范围。

import (
	"log/slog"
	"net/http"
	"net/url"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/engine"
)

const (
	// desktopInferPath 引擎侧路径（契约见 docs/desktop-local/contracts.md §9）。
	desktopInferPath = "/v1/desktop/infer"

	// desktopInferMaxBodyBytes 请求体上限（16 MiB）。业务上限是引擎侧的 messages
	// 总字符 2_000_000（app/api/desktop_infer.py::MAX_TOTAL_CHARS），按 UTF-8 最坏
	// 4 字节/字符 + JSON 包装约 8 MB；这里留一倍余量。网关只用它挡住明显异常的请求，
	// 真正的业务校验留在引擎侧（单一真相，避免两处阈值各说各话）。
	desktopInferMaxBodyBytes = 16 << 20
)

// DesktopInferHandler 把桌面端片段推理请求代理给 Python 引擎。
//
// 上游（引擎）返回的 422/502 原样透传：桌面端只需要区分"我的请求有问题"与"云端暂时
// 不可用"，两者的处置完全不同（改参数 vs 稍后重试或切本地模式）。
func DesktopInferHandler(pythonClient *engine.PythonClient) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// 身份先判：未认证的请求不该看到"引擎可用性"这类内部状态（401 而不是 500）。
		// authMW 已经保证 claims 存在；这里再判一次是防御"路由被挪到没有 authMW 的地方"
		// —— 那会让匿名请求带着 tenant_id=0 打到引擎，而引擎对 tenant 0 不做隔离。
		claims := auth.GetClaims(r.Context())
		if claims == nil || claims.UserID == "" || claims.TenantID == 0 {
			Unauthorized(w, "missing tenant context")
			return
		}
		if pythonClient == nil {
			InternalError(w, "python engine not available")
			return
		}
		if r.ContentLength > desktopInferMaxBodyBytes {
			JSON(w, http.StatusRequestEntityTooLarge, APIResponse{Success: false, Error: "request body too large"})
			return
		}
		// 兜底：Content-Length 缺失（chunked 上传）时同样在读层封顶。
		r.Body = http.MaxBytesReader(w, r.Body, desktopInferMaxBodyBytes)

		// 身份经 query 透传给引擎（引擎 AuthMiddleware 的网关代理路径要求
		// `?user_id=&tenant_id=` 与网关注入的 X-Internal-Token 同时存在）。这里由
		// 已验证的 claims **重建** query，客户端传什么都不作数。
		q := url.Values{}
		q.Set("user_id", claims.UserID)
		q.Set("tenant_id", tenantStr(claims.TenantID))

		// 审计线索只记元信息：租户、用户、请求体长度。**不记 body**，
		// 也不记 messages —— 那是用户的本地文档内容。
		slog.Info("desktop infer proxy",
			"tenant_id", claims.TenantID,
			"user_id", claims.UserID,
			"content_length", r.ContentLength,
		)

		pythonClient.ForwardRequest(w, r, desktopInferPath+"?"+q.Encode())
	}
}
