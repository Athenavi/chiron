package api

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestChaosActiveKeyMatchesPython(t *testing.T) {
	// 与 python-engine/app/redis_keys.py 的 rkey("chaos:active:<tenant>") 对齐，
	// 前缀来自同一个环境变量 REDIS_KEY_PREFIX。
	t.Setenv("REDIS_KEY_PREFIX", "prod:")

	if got, want := chaosActiveKey("t1"), "prod:chaos:active:t1"; got != want {
		t.Errorf("chaosActiveKey = %q, want %q", got, want)
	}

	t.Setenv("REDIS_KEY_PREFIX", "")
	if got, want := chaosActiveKey("t1"), "chaos:active:t1"; got != want {
		t.Errorf("chaosActiveKey(no prefix) = %q, want %q", got, want)
	}
}

func TestChaosErrorCode(t *testing.T) {
	cases := []struct {
		name string
		cfg  string
		want int
	}{
		{"默认 503", ``, http.StatusServiceUnavailable},
		{"空对象", `{}`, http.StatusServiceUnavailable},
		{"合法 500", `{"error_code":500}`, http.StatusInternalServerError},
		{"合法 429", `{"error_code":429}`, 429},
		{"越界 999 回退", `{"error_code":999}`, http.StatusServiceUnavailable},
		{"越界 200 回退", `{"error_code":200}`, http.StatusServiceUnavailable},
		{"坏 JSON 回退", `{`, http.StatusServiceUnavailable},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := chaosErrorCode([]byte(c.cfg)); got != c.want {
				t.Errorf("chaosErrorCode(%q) = %d, want %d", c.cfg, got, c.want)
			}
		})
	}
}

// 默认关闭时中间件必须是**纯放行**：不注入、不碰 Redis。
func TestChaosMiddlewareDisabledIsPassthrough(t *testing.T) {
	t.Setenv("CHAOS_ENABLED", "")

	called := false
	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
		w.WriteHeader(http.StatusTeapot)
	})

	// redis 传 nil —— 关闭状态下不该被解引用
	handler := ChaosMiddleware(nil)(next)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/agents", nil))

	if !called {
		t.Fatal("关闭 CHAOS_ENABLED 时中间件不应拦截请求")
	}
	if rec.Code != http.StatusTeapot {
		t.Errorf("status = %d, want %d", rec.Code, http.StatusTeapot)
	}
}

// 开关打开但 Redis 不可用时也必须放行（失败安全）。
func TestChaosMiddlewareAllowsWhenRedisMissing(t *testing.T) {
	t.Setenv("CHAOS_ENABLED", "true")

	called := false
	next := http.HandlerFunc(func(http.ResponseWriter, *http.Request) { called = true })

	handler := ChaosMiddleware(nil)(next)
	handler.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/v1/agents", nil))

	if !called {
		t.Fatal("Redis 不可用时不应拦截请求")
	}
}

// 支持的组合必须与 Python 侧 injector 的白名单一致，否则会出现
// "网关接受、引擎拒绝"（或反之）的实验 —— 那正是假注入的复现方式。
func TestChaosSupportedWhitelist(t *testing.T) {
	for _, target := range []string{"gateway", "engine"} {
		if !chaosSupportedTargets[target] {
			t.Errorf("target %q 应被支持", target)
		}
	}
	for _, target := range []string{"llm", "db", "redis", ""} {
		if chaosSupportedTargets[target] {
			t.Errorf("target %q 不应被支持", target)
		}
	}
	for _, ft := range []string{"latency", "error", "timeout"} {
		if !chaosSupportedFaults[ft] {
			t.Errorf("fault_type %q 应被支持", ft)
		}
	}
	if chaosSupportedFaults["resource"] {
		t.Error("resource 不应在请求路径上被接受")
	}
}
