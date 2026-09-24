package api

import (
	"context"
	"net/http"
	"net/url"
	"regexp"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
)

// SkillHandler proxies skill requests to the Python AI engine.
type SkillHandler struct {
	python *engine.PythonClient
}

func NewSkillHandler(python *engine.PythonClient) *SkillHandler {
	return &SkillHandler{python: python}
}

var validSkillName = regexp.MustCompile(`^[a-zA-Z0-9_.-]+$`)

// RegisterRoutes 注册技能路由。安全修复（P0-S2）：所有路由必须经过 authMW
// （技能即代码，未认证可 install/run = 未认证 RCE），并挂 rlMW 与 sanitizeMW。
func (h *SkillHandler) RegisterRoutes(mux *http.ServeMux, authMW, rlMW, sanitizeMW routeMiddleware) {
	// 同时注册精确和尾斜杠变体：Go ServeMux 中 "/v1/skills/" 只匹配子树（不匹配 "/v1/skills"），
	// 而前端调用的是 GET /v1/skills，Python 端注册的也是 /v1/skills。
	mux.Handle("GET /v1/skills", authMW(rlMW(http.HandlerFunc(h.proxy))))
	mux.Handle("GET /v1/skills/", authMW(rlMW(http.HandlerFunc(h.proxy))))
	mux.Handle("POST /v1/skills/install", authMW(rlMW(sanitizeMW(http.HandlerFunc(h.proxy)))))
	mux.Handle("POST /v1/skills/generate", authMW(rlMW(sanitizeMW(http.HandlerFunc(h.proxy)))))
	mux.Handle("DELETE /v1/skills/{name}", authMW(rlMW(http.HandlerFunc(h.proxyDelete))))
	mux.Handle("GET /v1/skills/discover", authMW(rlMW(http.HandlerFunc(h.proxy))))
	// 启停（PUT）与运行（POST run）——技能工作台主链路
	mux.Handle("PUT /v1/skills/{name}", authMW(rlMW(sanitizeMW(http.HandlerFunc(h.proxy)))))
	mux.Handle("POST /v1/skills/{name}/run", authMW(rlMW(sanitizeMW(http.HandlerFunc(h.proxy)))))
	// 市场技能注册（P1 修复：前端 SkillMarketCard 调用 /v1/skills/{id}/register，
	// 此前两端均无此路由导致注册 404）
	mux.Handle("POST /v1/skills/{name}/register", authMW(rlMW(http.HandlerFunc(h.register))))
}

// register 将能力注册中心的技能注册为本地技能（转发 Python /v1/skills/{name}/register）。
func (h *SkillHandler) register(w http.ResponseWriter, r *http.Request) {
	if h.python == nil {
		InternalError(w, "python engine not available")
		return
	}
	name := r.PathValue("name")
	if name == "" {
		BadRequest(w, "name is required")
		return
	}
	if !validSkillName.MatchString(name) {
		BadRequest(w, "invalid skill name")
		return
	}
	claims := auth.GetClaims(r.Context())
	target := "/v1/skills/" + name + "/register"
	if claims != nil {
		target += "?user_id=" + claims.UserID + "&tenant_id=" + claims.TenantID
	}
	var body map[string]interface{}
	if r.ContentLength > 0 {
		if err := DecodeJSON(w, r, &body); err != nil {
			BadRequest(w, ErrInvalidReq)
			return
		}
	} else {
		body = map[string]interface{}{}
	}
	var result map[string]interface{}
	if err := h.python.PostJSON(r.Context(), target, body, &result); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "python engine error")
		return
	}
	OK(w, result)
}

// proxy forwards the request to the Python engine.
// identityParams 构造网关注入的身份参数（`user_id` / `tenant_id`）。
//
// 引擎无鉴权，网关是**唯一可信边界**：POST/PUT 把身份写进 body（见 proxy），
// 而 GET/DELETE 没有 body，必须走 query —— 引擎侧 handler 正是从 query 读这两个字段
// （如 app/api/skills.py 的 `list_skills(user_id, tenant_id)`）。
//
// 历史缺陷：GET / DELETE 此前**不传任何身份**，引擎收到空 user_id/tenant_id 后
// 一律回退全局共享层 —— 用户看不到自己的私有技能与租户共享技能（删除也打不准目标层）。
func identityParams(r *http.Request) url.Values {
	q := url.Values{}
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		return q
	}
	if claims.UserID != "" {
		q.Set("user_id", claims.UserID)
	}
	if tid := ResolveTenantID(r); tid != "" {
		q.Set("tenant_id", tid)
	}
	return q
}

// withQuery 把 query 追加到 path（path 可能已自带 query）。
func withQuery(path, query string) string {
	if query == "" {
		return path
	}
	if strings.Contains(path, "?") {
		return path + "&" + query
	}
	return path + "?" + query
}

func (h *SkillHandler) proxy(w http.ResponseWriter, r *http.Request) {
	if h.python == nil {
		InternalError(w, "python engine not available")
		return
	}

	var result map[string]interface{}
	var err error

	switch r.Method {
	case "GET":
		// 规范化转发路径：Python 端注册的是 /v1/skills（无尾斜杠）
		basePath := strings.TrimSuffix(r.URL.Path, "/")
		// 身份注入：GET 无 body，只能走 query（见 identityParams 的说明）。
		path := withQuery(basePath, identityParams(r).Encode())
		err = h.python.GetJSON(r.Context(), path, &result)
		if err == nil && strings.HasSuffix(basePath, "/discover") {
			filterDiscoverByMarket(r.Context(), result, ResolveTenantID(r))
		}
	case "POST", "PUT":
		var body map[string]interface{}
		if err := DecodeJSON(w, r, &body); err != nil {
			BadRequest(w, ErrInvalidReq)
			return
		}
		// 身份注入（网关为唯一可信边界）：引擎无鉴权，必须由网关注入租户/用户身份。
		if tid := ResolveTenantID(r); tid != "" {
			body["tenant_id"] = tid
		}
		if uid := auth.GetClaims(r.Context()); uid != nil && uid.UserID != "" {
			body["user_id"] = uid.UserID
		}
		if r.Method == "PUT" {
			err = h.python.PutJSON(r.Context(), r.URL.Path, body, &result)
		} else {
			err = h.python.PostJSON(r.Context(), r.URL.Path, body, &result)
		}
	default:
		BadRequest(w, "unsupported method")
		return
	}

	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "python engine error")
		return
	}
	OK(w, result)
}

// proxyDelete forwards DELETE requests to the Python engine.
//
// `?scope=tenant` 删的是**租户共享层**（团队资产），因此需要技能管理权限；
// 缺省 / `scope=private` 仍是调用者私有目录，保持既有行为（任何登录用户可删自己的）。
func (h *SkillHandler) proxyDelete(w http.ResponseWriter, r *http.Request) {
	if h.python == nil {
		InternalError(w, "python engine not available")
		return
	}

	name := r.PathValue("name")
	if name == "" {
		BadRequest(w, "name is required")
		return
	}
	if !validSkillName.MatchString(name) {
		BadRequest(w, "invalid skill name")
		return
	}

	scope := r.URL.Query().Get("scope")
	if scope == "tenant" {
		claims := auth.GetClaims(r.Context())
		if !AllowedByEntOrLegacy(r.Context(), claims, auth.PermMarketManage) {
			Forbidden(w, "insufficient permissions to delete tenant-shared skills")
			return
		}
	}

	// DELETE 无 body：身份与 scope 都经 query 下发（见 identityParams）。
	params := identityParams(r)
	if scope != "" {
		params.Set("scope", scope)
	}

	var result map[string]interface{}
	if err := h.python.DeleteJSON(
		r.Context(), withQuery("/v1/skills/"+name, params.Encode()), &result,
	); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "python engine error")
		return
	}
	OK(w, result)
}

// filterDiscoverByMarket 对 discover 代理响应做市场白名单过滤：
// 仅当市场存在同名 published 条目时按租户授权过滤（IsItemEnabledForTenant
// 内部保证未上架能力与查询故障均放行）。PG 不可用时整体跳过，避免逐条 warn。
func filterDiscoverByMarket(ctx context.Context, result map[string]interface{}, tenantID string) {
	if db.ReadPool() == nil {
		return
	}
	// 兼容多种响应结构：在常见列表键中定位技能数组
	var list []interface{}
	var listKey string
	for _, key := range []string{"skills", "items", "data", "list"} {
		if arr, ok := result[key].([]interface{}); ok {
			list, listKey = arr, key
			break
		}
	}
	if listKey == "" {
		return
	}
	result[listKey] = filterSkillsByMarket(ctx, list, tenantID)
}

// filterSkillsByMarket 对技能列表逐项做市场门控过滤（纯逻辑，独立可测）。
func filterSkillsByMarket(ctx context.Context, list []interface{}, tenantID string) []interface{} {
	kept := make([]interface{}, 0, len(list))
	for _, raw := range list {
		entry, ok := raw.(map[string]interface{})
		if !ok {
			kept = append(kept, raw)
			continue
		}
		name, _ := entry["name"].(string)
		if name == "" {
			kept = append(kept, raw)
			continue
		}
		if enabled, _ := IsItemEnabledForTenant(ctx, "skill", name, tenantID); enabled {
			kept = append(kept, raw)
		}
	}
	return kept
}
