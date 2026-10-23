package api

// interactions.go —— M7 人工介入闭环的网关半边（1B）。
//
// 引擎是 `pending_interactions` 的唯一写入方，也是"这张票能不能答、答完之后归谁"的
// 唯一判定方（见 python-engine/app/api/interactions.py）。网关在这里只负责三件它 uniquely
// 能做的事：
//
//  1. **认证**：`authMW` 之后 claims 才是可信身份；转发时**只用 claims 重写身份**，
//     请求体里客户端自带的 `user_id`/`tenant_id` 一律丢弃（与 /submit、/approval 同一口径）。
//  2. **会话归属**：`session_id` 由客户端给出，必须是本人的会话（IDOR 兜底）。
//     引擎侧的 `user_id = 来电者` 谓词本身已足够挡住跨用户，这一道是纵深：
//     它让"拿别人的 session_id 试探"在网关就结束，不消耗一次引擎往返。
//  3. **状态码**：引擎的业务失败都是 200 + `{ok:false,error_code}`（见其文档字符串），
//     面向用户的 404/409/400 由网关在这里映射。
//
// 注意这里**不唤醒**续跑：答复只是把票写成 `answered`，真正接着跑靠下一轮提交在引擎开头
// 接手挂起现场（`claim_awaiting`）。在前端，这就是"答完顺手发一轮 submit"；在纯 API 调用方，
// 是"下一次提交这条会话时自动带上"。把唤醒放进网关会有两个坏处：答复那一轮的消息流进不了
// 用户正看着的 SSE（唤醒是后台轮，输出只落库），而且要在网关第三次复制 /submit 的那串闸门
// （运行锁 / 全局槽位 / 租户并发 / 计费 / 配额预留），一旦其中某处修了而这里没跟上，
// 就变成一条绕过限流的昂贵入口。

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/session"
)

// InteractionHandler 暴露"还等着人的介入"的读与答。
type InteractionHandler struct {
	python     *engine.PythonClient
	sessionMgr *session.Manager
}

func NewInteractionHandler(python *engine.PythonClient, sessionMgr *session.Manager) *InteractionHandler {
	return &InteractionHandler{python: python, sessionMgr: sessionMgr}
}

// interactionListMaxLimit 与引擎 `MAX_LIST_LIMIT` 同值：待办本来就少，但入参来自外部，必须有界。
const interactionListMaxLimit = 50

// List GET /v1/agent/sessions/{session_id}/interactions
//
// 返回该会话仍 `pending`/`answered` 的介入。`answered`（已答、还没被续跑取走）也要返回：
// 用户刷新页面时最该看到的正是"我答过了，但那一轮还没续上"，把它藏起来只会让人再答一次。
func (h *InteractionHandler) List(w http.ResponseWriter, r *http.Request) {
	userID, tenantID, ok := interactionIdentity(w, r)
	if !ok {
		return
	}
	sessionID := strings.TrimSpace(r.PathValue("session_id"))
	if sessionID == "" {
		BadRequest(w, "session_id is required")
		return
	}
	if !ensureSessionOwner(r.Context(), w, h.sessionMgr, sessionID, userID) {
		return
	}
	if h.python == nil {
		InternalError(w, "python engine not available")
		return
	}

	limit := interactionListLimit(r)
	target := "/v1/agent/sessions/" + url.PathEscape(sessionID) + "/interactions" +
		"?user_id=" + url.QueryEscape(userID) +
		"&tenant_id=" + url.QueryEscape(tenantID) +
		"&limit=" + strconv.Itoa(limit)

	var resp map[string]interface{}
	if err := h.python.GetJSON(r.Context(), target, &resp); err != nil {
		slog.Error("interactions: list proxy failed", "session", sessionID, "error", err)
		logAndRespond(w, err, http.StatusInternalServerError, "python engine error")
		return
	}
	if msg, code, status := interactionEngineError(resp); code != "" {
		JSONWithCode(w, status, code, nil, msg)
		return
	}
	items, _ := resp["items"].([]interface{})
	count, _ := resp["count"].(float64)
	OK(w, map[string]interface{}{
		"session_id": sessionID,
		"count":      int(count),
		"items":      items,
	})
}

// ListForUser GET /v1/agent/interactions
//
// **跨会话**列出"这个人"还等着的介入（M21：全局角标的数据源）。
//
// 与 `List` 的区别只有作用域：那条按会话问，这条按人问。身份**同样只来自 claims**，
// 引擎侧的谓词也完全相同（`tenant_id` + `user_id`）⇒ 放宽作用域不会放宽权限；
// 这也是为什么这里不需要额外的归属校验（没有"别人的会话"这个概念，只有"别人的票"，
// 而那个由引擎的 `user_id = 来电者` 谓词挡住）。
func (h *InteractionHandler) ListForUser(w http.ResponseWriter, r *http.Request) {
	userID, tenantID, ok := interactionIdentity(w, r)
	if !ok {
		return
	}
	if h.python == nil {
		InternalError(w, "python engine not available")
		return
	}

	limit := interactionListLimit(r)
	target := "/v1/agent/interactions" +
		"?user_id=" + url.QueryEscape(userID) +
		"&tenant_id=" + url.QueryEscape(tenantID) +
		"&limit=" + strconv.Itoa(limit)

	var resp map[string]interface{}
	if err := h.python.GetJSON(r.Context(), target, &resp); err != nil {
		slog.Error("interactions: user list proxy failed", "user", userID, "error", err)
		logAndRespond(w, err, http.StatusInternalServerError, "python engine error")
		return
	}
	if msg, code, status := interactionEngineError(resp); code != "" {
		JSONWithCode(w, status, code, nil, msg)
		return
	}
	items, _ := resp["items"].([]interface{})
	count, _ := resp["count"].(float64)
	OK(w, map[string]interface{}{
		"count": int(count),
		"items": items,
	})
}

// Resolve POST /v1/agent/interactions/{interaction_id}/resolve
//
// 请求体：`{"session_id": "...", "response": {...}}`。
// `response` 的形状**由那一行的 kind 决定，而 kind 只有引擎知道**（库里读出来的），
// 所以网关这里只做非空校验，把 approve/reject/edit 与 answer/declined 的判定留给引擎 ——
// 在网关重抄一份判别逻辑就是制造第二真相（它还得先多查一次库才能知道 kind）。
func (h *InteractionHandler) Resolve(w http.ResponseWriter, r *http.Request) {
	userID, tenantID, ok := interactionIdentity(w, r)
	if !ok {
		return
	}
	interactionID := strings.TrimSpace(r.PathValue("interaction_id"))
	if interactionID == "" {
		BadRequest(w, "interaction_id is required")
		return
	}
	var body struct {
		SessionID string                 `json:"session_id"`
		Response  map[string]interface{} `json:"response"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	body.SessionID = strings.TrimSpace(body.SessionID)
	if body.SessionID == "" {
		BadRequest(w, "session_id is required")
		return
	}
	if len(body.Response) == 0 {
		BadRequest(w, "response is required")
		return
	}
	if !ensureSessionOwner(r.Context(), w, h.sessionMgr, body.SessionID, userID) {
		return
	}
	if h.python == nil {
		InternalError(w, "python engine not available")
		return
	}

	// 身份只从 claims 重建：**新建载荷**而不是改写 body —— 客户端字段（含它自己填的
	// user_id）到此为止，不会出现在转发出去的内容里。
	payload := map[string]interface{}{
		"session_id": body.SessionID,
		"response":   body.Response,
		"user_id":    userID,
		"tenant_id":  tenantID,
	}
	target := "/v1/agent/interactions/" + url.PathEscape(interactionID) + "/resolve"
	var resp map[string]interface{}
	if err := h.python.PostJSON(r.Context(), target, payload, &resp); err != nil {
		slog.Error("interactions: resolve proxy failed", "session", body.SessionID,
			"interaction", interactionID, "error", err)
		logAndRespond(w, err, http.StatusInternalServerError, "python engine error")
		return
	}
	if msg, code, status := interactionEngineError(resp); code != "" {
		slog.Info("interactions: resolve rejected by engine", "session", body.SessionID,
			"interaction", interactionID, "error_code", code)
		JSONWithCode(w, status, code, nil, msg)
		return
	}
	OK(w, resp["data"])
}

// interactionIdentity 取"已由 authMW 验证过"的身份。缺租户时直接拒：引擎的每条谓词都带
// tenant，缺它就只能靠 user 单条件匹配，而会话/票的归属语义里租户是更外层的边界。
func interactionIdentity(w http.ResponseWriter, r *http.Request) (string, string, bool) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.UserID == "" {
		Unauthorized(w, ErrAuthRequired)
		return "", "", false
	}
	if claims.TenantID == 0 {
		Unauthorized(w, "missing tenant context")
		return "", "", false
	}
	return claims.UserID, tenantStr(claims.TenantID), true
}

// interactionListLimit 解析并夹紧 limit（非法值回落到默认，而不是报错：这只是显示条数）。
func interactionListLimit(r *http.Request) int {
	const def = 20
	raw := strings.TrimSpace(r.URL.Query().Get("limit"))
	if raw == "" {
		return def
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < 1 {
		return def
	}
	if n > interactionListMaxLimit {
		return interactionListMaxLimit
	}
	return n
}

// ensureSessionOwner 校验"这个会话属于来电用户"。返回 false 表示已写好响应、调用方必须中止。
//
// 口径与 /submit（routes_agent.go）与取消广播（handlers.go）完全一致：查不到会话当"没有"
// 处理（新会话还没落库的竞态），查询本身出错才是 500，属主不符是 403。
// 这里把它抽成函数是因为介入的两条路由都要用；那两处仍是原样内联（改动它们不属于本轮）。
func ensureSessionOwner(ctx context.Context, w http.ResponseWriter,
	sessionMgr *session.Manager, sessionID, userID string) bool {
	if sessionMgr == nil {
		// 没有会话存储：无法核对归属。介入端点仍可依赖引擎的 user_id 谓词兜底，
		// 但这条只在测试/降级部署里成立 —— 记一条 warn，别让"跳过校验"变成无声的默认。
		slog.Warn("interactions: session ownership check skipped (no session manager)")
		return true
	}
	sess, err := sessionMgr.GetSession(ctx, sessionID)
	if err != nil {
		if errors.Is(err, session.ErrSessionNotFound) {
			NotFound(w, ErrNotFound)
			return false
		}
		InternalError(w, "session check failed")
		return false
	}
	if sess == nil || sess.UserID != userID {
		Forbidden(w, "session does not belong to the current user")
		return false
	}
	return true
}

// interactionEngineError 把引擎信封（200 + ok/error_code）映射成面向用户的状态码与稳定错误码。
// 返回的 code 为空串表示"没有业务失败"（成功，或引擎返回了无法识别的形状 —— 交由调用方按成功透传）。
func interactionEngineError(resp map[string]interface{}) (msg, code string, status int) {
	if resp == nil {
		return "", "", 0
	}
	if okv, _ := resp["ok"].(bool); okv {
		return "", "", 0
	}
	msg, _ = resp["error"].(string)
	if msg == "" {
		msg = "engine rejected the request"
	}
	codeRaw, _ := resp["error_code"].(string)
	switch strings.TrimSpace(codeRaw) {
	case "not_found":
		return msg, CodeNotFound, http.StatusNotFound
	case "stale":
		// 已经答过 / 已被取走 / 已过期：冲突，不是"没找到"。用户要能看到"这条已经有了下文"。
		return msg, CodeRequestFailed, http.StatusConflict
	case "invalid_decision":
		return msg, CodeInvalidRequest, http.StatusBadRequest
	case "invalid_request":
		return msg, CodeInvalidRequest, http.StatusBadRequest
	case "unauthorized":
		// 只可能是网关自己漏了身份（claims 已在上面校验过）：如实报 401，不要伪装成参数问题
		return msg, CodeAuthRequired, http.StatusUnauthorized
	case "storage_error":
		return msg, CodeInternal, http.StatusInternalServerError
	default:
		return msg, CodeRequestFailed, http.StatusInternalServerError
	}
}
