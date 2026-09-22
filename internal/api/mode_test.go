package api

import "testing"

// 工具授权模式是**请求级**参数：服务端只做归一化与兜底，不存储。
//
// 这组断言守两件事：
//  1. 非法/缺失输入一律回落 auto —— 请求体是不可信输入，不得直接透传给引擎；
//  2. 解析链里 explicit 仍然优先。曾把这个值存进 Redis 并让 runtime 也多一层来源，
//     三处状态不一致就表现为"设置存了却不生效"（前端 403、引擎仍按旧模式裁决）。
func TestNormalizeToolsMode(t *testing.T) {
	cases := []struct{ in, want string }{
		{"ask", ModeAsk},
		{"auto", ModeAuto},
		{"yolo", ModeYOLO},
		{"", DefaultToolsMode},
		{"YOLO", DefaultToolsMode},     // 大小写不匹配 → 兜底
		{"rm -rf /", DefaultToolsMode}, // 任意字符串不得成为模式
	}
	for _, c := range cases {
		if got := normalizeToolsMode(c.in); got != c.want {
			t.Errorf("normalizeToolsMode(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestResolveAgentConfig_ToolsModeIsRequestScoped(t *testing.T) {
	h := &SessionRuntimeHandler{}

	// 请求显式携带 → 采用请求值
	cfg := h.resolveAgentConfig("u1", &runtimeView{}, map[string]string{}, map[string]string{
		"mode": "ptc", "tools_mode": ModeYOLO,
	})
	if cfg.toolsMode.Value != ModeYOLO || cfg.toolsMode.Source != "request" {
		t.Errorf("tools_mode: got %+v, want yolo/request", cfg.toolsMode)
	}
	if cfg.mode.Value != "ptc" || cfg.mode.Source != "request" {
		t.Errorf("mode: got %+v, want ptc/request", cfg.mode)
	}

	// 未携带 → 用户/全局默认
	cfg = h.resolveAgentConfig("u1", &runtimeView{}, map[string]string{"default_tools_mode": ModeAsk}, nil)
	if cfg.toolsMode.Value != ModeAsk || cfg.toolsMode.Source != "default" {
		t.Errorf("tools_mode default: got %+v, want ask/default", cfg.toolsMode)
	}

	// 都没有 → 系统兜底（对话模式 normal / 工具授权 auto）
	cfg = h.resolveAgentConfig("u1", &runtimeView{}, map[string]string{}, nil)
	if cfg.toolsMode.Value != DefaultToolsMode || cfg.toolsMode.Source != "system" {
		t.Errorf("tools_mode fallback: got %+v, want auto/system", cfg.toolsMode)
	}
	if cfg.mode.Value != fallbackMode || cfg.mode.Source != "system" {
		t.Errorf("mode fallback: got %+v, want %s/system", cfg.mode, fallbackMode)
	}
}

// 会话 runtime 不再承载 mode / tools_mode，但 model / provider 必须保留这一层：
// 用户选定的模型希望下次继续沿用（见 session_runtime.go 的解析链说明）。
func TestResolveAgentConfig_ModelKeepsSessionLayer(t *testing.T) {
	h := &SessionRuntimeHandler{}
	cfg := h.resolveAgentConfig("u1", &runtimeView{Model: "m-session", Provider: "p1"},
		map[string]string{}, nil)
	if cfg.model.Value != "m-session" || cfg.model.Source != "session" {
		t.Errorf("model: got %+v, want m-session/session", cfg.model)
	}
	if cfg.provider.Value != "p1" {
		t.Errorf("provider: got %+v, want p1", cfg.provider)
	}
}
