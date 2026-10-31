package api

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/athenavi/chiron/config"
)

// auth_local_session_test.go —— 本地模式免口令会话引导的两道闸门。
//
// 这个端点**不校验任何口令就返回一个 owner 身份**，所以它的正确性判据不是
// "能不能拿到 token"，而是"不该拿到的人拿不到"。仓库里不允许单元测试连真实数据库
// （见 session_cancel_live_test.go 的 live 标签约定），因此这里只测闸门与路由本身：
// 数据库不可用时的 500 恰好是"请求已通过闸门、进到 ensureLocalOwner 里"的证据。

func TestIsLoopbackPeer(t *testing.T) {
	cases := []struct {
		remoteAddr string
		want       bool
	}{
		{"127.0.0.1:54321", true},
		{"127.0.0.1", false}, // 没有端口：SplitHostPort 失败 ⇒ 不放行（宁可拒绝）
		{"[::1]:54321", true},
		{"::1", false},
		{"127.4.5.6:1", true}, // 整个 127/8 都是 loopback，不只是 .0.0.1
		{"::ffff:127.0.0.1:1", false},
		{"10.0.0.5:1", false},
		{"192.168.1.7:1", false},
		{"169.254.1.1:1", false},
		{"[::ffff:127.0.0.1]:1", true}, // IPv4-mapped：双栈监听时本机连接可能以此形态出现
		{"example.com:80", false},
		{"", false},
	}
	for _, c := range cases {
		if got := isLoopbackPeer(c.remoteAddr); got != c.want {
			t.Errorf("isLoopbackPeer(%q) = %v，期望 %v", c.remoteAddr, got, c.want)
		}
	}
}

// TestLocalBootstrapRouteNotRegisteredByDefault 钉住"默认关"。
//
// 新机制的默认值必须是"什么都不做"：一个能被互联网访问到的、免口令铸 owner 的端点，
// 不能因为某次配置手滑就跟着进程一起起来。判据用"探针没记到路径 + 404"，
// 与 gateway_router_probe_test.go 里那批"handler 存在但没接线"的测试同一口径。
func TestLocalBootstrapRouteNotRegisteredByDefault(t *testing.T) {
	mux := http.NewServeMux()
	rl := &middlewareProbe{}
	registerAuthRoutes(mux, &AuthHandler{cfg: &config.Config{}}, rl.passthrough, rl.passthrough)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/auth/session", nil)
	mux.ServeHTTP(rec, req)

	if rl.hit("/v1/auth/session") {
		t.Fatal("LOCAL_BOOTSTRAP 关闭时仍注册了 POST /v1/auth/session：默认值不再是" +
			"什么都不做，云端部署一旦带上这个进程就多出免口令 owner 通道")
	}
	// 405 而不是 404：GET /v1/auth/session 在同一路径上注册着，ServeMux 认出路径、
	// 但该方法没有对应 pattern。这里断言 405 的目的在于"POST 没有被接进处理链"。
	if rec.Code != http.StatusMethodNotAllowed {
		t.Errorf("LOCAL_BOOTSTRAP 关闭时 POST /v1/auth/session 不应进入业务 handler，得到 %d", rec.Code)
	}
}

// TestLocalBootstrapRequiresLoopback 是第二道闸门：开关是按进程判的，
// "谁能免口令拿到 owner"必须按连接判。局域网请求要 403，而不是"少一点权限的 200"。
func TestLocalBootstrapRequiresLoopback(t *testing.T) {
	mux := http.NewServeMux()
	rl := &middlewareProbe{}
	registerAuthRoutes(mux,
		&AuthHandler{cfg: &config.Config{LocalBootstrap: true}},
		rl.passthrough, rl.passthrough)

	// httptest.NewRequest 默认的 RemoteAddr 是 10.254.10.254:12345 —— 非本机。
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/auth/session", nil))

	if rec.Code != http.StatusForbidden {
		t.Errorf("非 loopback 来源应被拒 403，得到 %d（body=%s）", rec.Code, rec.Body.String())
	}
}

// TestLocalBootstrapAllowsLoopback 反向钉住闸门没有过紧：本机必须能通过。
//
// 断言"不是 403"而不是"是 200"：单元测试没有数据库，ensureLocalOwner 必然失败成 500，
// 而 500 正是"闸门已放行"的证据。
func TestLocalBootstrapAllowsLoopback(t *testing.T) {
	mux := http.NewServeMux()
	rl := &middlewareProbe{}
	registerAuthRoutes(mux,
		&AuthHandler{cfg: &config.Config{LocalBootstrap: true}},
		rl.passthrough, rl.passthrough)

	req := httptest.NewRequest(http.MethodPost, "/v1/auth/session", nil)
	req.RemoteAddr = "127.0.0.1:51234"
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, req)

	if rec.Code == http.StatusForbidden {
		t.Fatalf("本机来源被拒了：桌面本地模式永远拿不到会话，整个应用会停在未登录状态（body=%s）",
			rec.Body.String())
	}
	if rec.Code == http.StatusNotFound {
		t.Fatal("POST /v1/auth/session 未注册：LOCAL_BOOTSTRAP 打开后路由没接上")
	}
}

// TestLocalBootstrapDoesNotShadowGetSession：GET 是云端 SSO 的会话引导，
// POST 是本地引导，同一个路径的两种方法必须各自可达、互不覆盖。
func TestLocalBootstrapDoesNotShadowGetSession(t *testing.T) {
	mux := http.NewServeMux()
	rl := &middlewareProbe{}
	registerAuthRoutes(mux,
		&AuthHandler{cfg: &config.Config{LocalBootstrap: true}},
		rl.passthrough, rl.passthrough)

	getReq := httptest.NewRequest(http.MethodGet, "/v1/auth/session", nil)
	getRec := httptest.NewRecorder()
	mux.ServeHTTP(getRec, getReq)
	if !rl.hit("/v1/auth/session") {
		t.Fatal("GET /v1/auth/session 的路由被 POST 覆盖：云端/SSO 会话引导会整条断掉")
	}
	// 没有 cookie 时 Session 自己会 401；出现 405/404 才说明路由被改坏了。
	if getRec.Code == http.StatusMethodNotAllowed || getRec.Code == http.StatusNotFound {
		t.Errorf("GET /v1/auth/session 路由异常，得到 %d", getRec.Code)
	}
}
