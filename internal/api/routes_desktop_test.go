package api

import (
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/engine"
)

// 这些用例盯的是**链路**（路由是否暴露、中间件是否挂齐）而不是 handler 实现，
// 与 gateway_router_probe_test.go 的判据同型：请求只有匹配到已注册的模式才会
// 进入中间件链，所以"探针记到了这个路径"等价于"该路径已注册"。

// TestDesktopInferRouteIsRegistered 防的是"handler 写好了但没注册"—— Go 的
// ServeMux 不会落到子 mux 去试，未注册就是 404（桌面端会以为云端不支持片段推理）。
func TestDesktopInferRouteIsRegistered(t *testing.T) {
	mux := http.NewServeMux()
	authProbe := &middlewareProbe{}
	rlProbe := &middlewareProbe{}
	registerDesktopRoutes(mux, authProbe.reject(http.StatusUnauthorized), rlProbe.passthrough, nil)

	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/desktop/infer", strings.NewReader("{}")))

	if !authProbe.hit("/v1/desktop/infer") {
		t.Fatal("POST /v1/desktop/infer 未经过 authMW：匿名请求会带着 tenant_id=0 打到引擎")
	}
	if rec.Code != http.StatusUnauthorized {
		t.Errorf("未认证的片段推理应被 authMW 拒绝，得到 %d", rec.Code)
	}
}

// TestDesktopInferRouteIsRateLimited：这个端点会真的花上游 token，只挂 authMW
// 等于给每个登录用户开一条不限速的打上游通道。
func TestDesktopInferRouteIsRateLimited(t *testing.T) {
	mux := http.NewServeMux()
	authProbe := &middlewareProbe{}
	rlProbe := &middlewareProbe{}
	registerDesktopRoutes(mux, authProbe.passthrough, rlProbe.reject(http.StatusTooManyRequests), nil)

	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/desktop/infer", strings.NewReader("{}")))

	if !rlProbe.hit("/v1/desktop/infer") {
		t.Fatal("POST /v1/desktop/infer 未经过 rlMW：片段推理成了绕过限流的第二通道")
	}
	if rec.Code != http.StatusTooManyRequests {
		t.Errorf("被限流的请求应返回 429，得到 %d", rec.Code)
	}
}

// TestDesktopInferRejectsAnonymousRequestWithoutGateway：即使 pythonClient 为 nil
// （引擎未接线/启动中），也必须是"明确的错误"，不能 200 空响应。
func TestDesktopInferHandlerWithoutEngine(t *testing.T) {
	// pythonClient 为 nil：handler 必须先自报不可用，而不是把请求转给空客户端 panic。
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/desktop/infer", strings.NewReader("{}"))
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1", TenantID: 1}))
	DesktopInferHandler(nil).ServeHTTP(rec, req)

	if rec.Code != http.StatusInternalServerError {
		t.Errorf("引擎不可用应返回 500，得到 %d", rec.Code)
	}
}

// TestDesktopInferHandlerRequiresTenant：防御"路由被挪到没有 authMW 的地方"。
// tenant_id=0 在引擎侧不做租户隔离，放进去等于跨租户。
func TestDesktopInferHandlerRequiresTenant(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/desktop/infer", strings.NewReader("{}"))
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1"}))
	DesktopInferHandler(nil).ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Errorf("缺租户上下文应返回 401，得到 %d", rec.Code)
	}
}

// ── 计费口径：代理路径不触发任何扣费（contracts.md §9.3） ────────────────────

// TestDesktopInferProxyForwardsVerbatim 跑一遍**真实代理路径**（httptest 当引擎），
// 盯住计费口径的前置条件：
//
//  1. 响应体**逐字节**透传 —— 网关不解析、不重组、不包 `{"data": ...}`。不读响应体，
//     就拿不到 `usage` / `provider` / `model`，也就没有了在网关算 credits 的可能；
//  2. 转发的身份由已验证的 claims **重建** —— 客户端伪造的 `?user_id=attacker` 不作数，
//     否则即便将来接了对账，账也会记到别人头上。
func TestDesktopInferProxyForwardsVerbatim(t *testing.T) {
	const engineBody = `{"content":"压好的两行","usage":{"input_tokens":11,"output_tokens":7,"cached_tokens":3},"provider":"openai","model":"gpt-4o-mini"}`
	const reqBody = `{"model":"gpt-4o-mini","messages":[{"role":"user","content":"压成两行"}]}`

	var gotPath string
	var gotQuery url.Values
	var gotBody string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotQuery = r.URL.Query()
		b, _ := io.ReadAll(r.Body)
		gotBody = string(b)
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, engineBody)
	}))
	defer srv.Close()

	rec := httptest.NewRecorder()
	// 客户端故意带上伪造的 tenant_id/user_id：handler 必须无视它们。
	req := httptest.NewRequest(http.MethodPost,
		"/v1/desktop/infer?tenant_id=999&user_id=attacker", strings.NewReader(reqBody))
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1", TenantID: 7}))
	DesktopInferHandler(engine.NewPythonClient(srv.URL)).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("代理应透传引擎的 200，得到 %d：%s", rec.Code, rec.Body.String())
	}
	if got := rec.Body.String(); got != engineBody {
		t.Errorf("响应体不是逐字节透传（网关动过响应体就有了计费入口）：\n got=%s\nwant=%s", got, engineBody)
	}
	if gotPath != desktopInferPath {
		t.Errorf("转发路径不对：得到 %q，期望 %q", gotPath, desktopInferPath)
	}
	if gotBody != reqBody {
		t.Errorf("请求体被改写：\n got=%s\nwant=%s", gotBody, reqBody)
	}
	if gotQuery.Get("user_id") != "u1" || gotQuery.Get("tenant_id") != "7" {
		t.Errorf("转发身份没有按 claims 重建：得到 user_id=%q tenant_id=%q，期望 u1/7",
			gotQuery.Get("user_id"), gotQuery.Get("tenant_id"))
	}
}

// TestDesktopInferHandlerHasNoBillingCalls 用源码扫描钉住"网关侧不计费"。
//
// 为什么扫描源码而不是运行时断言：`DesktopInferHandler` 只接受 `*engine.PythonClient`，
// 结构上就没有计费入口 —— "没有调用"这件事在运行时不可观测（能观测的只有"没写库"，
// 而单测环境本来就没有库）。所以直接盯源码：出现计费调用或计费表名即失败。
// 注释会被剔除，所以 desktop_infer.go 的文件头可以放心解释计费口径（§9.3）。
func TestDesktopInferHandlerHasNoBillingCalls(t *testing.T) {
	raw, err := os.ReadFile("desktop_infer.go")
	if err != nil {
		t.Fatalf("读不到 desktop_infer.go（用例需在 internal/api 目录下运行）：%v", err)
	}
	code := stripGoComments(string(raw))
	for _, banned := range []string{
		"DeductTokens",        // billing 扣费（写 credit_transactions）
		"RecordTokenUsage",    // billing 成本中心明细（写 billing_records）
		"TrueUpTenantQuota",   // 租户额度池校正
		"credit_transactions", // 用户余额流水表
		"billing_records",     // 企业成本中心明细表
	} {
		if strings.Contains(code, banned) {
			t.Errorf("desktop_infer.go 出现了 %q：片段推理只记 usage、不扣 credits（contracts.md §9.3）；"+
				"要接线请先改口径与用例", banned)
		}
	}
}

// stripGoComments 去掉 `//` 行注释与 `/* */` 块注释，只留代码。
// 最小实现：本文件的代码里没有 URL 字面量（含 `//` 的字符串会被截断，方向是漏检而非误报）。
func stripGoComments(src string) string {
	var b strings.Builder
	inBlock := false
	for _, line := range strings.Split(src, "\n") {
		if inBlock {
			i := strings.Index(line, "*/")
			if i < 0 {
				continue
			}
			line = line[i+2:]
			inBlock = false
		}
		for {
			start := strings.Index(line, "/*")
			if start < 0 {
				break
			}
			end := strings.Index(line[start+2:], "*/")
			if end < 0 {
				line = line[:start]
				inBlock = true
				break
			}
			line = line[:start] + line[start+2+end+2:]
		}
		if i := strings.Index(line, "//"); i >= 0 {
			line = line[:i]
		}
		b.WriteString(line)
		b.WriteString("\n")
	}
	return b.String()
}
