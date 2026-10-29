package api

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
)

// 合并口径：用户级权益与租户级策略**取更严者**（交集）。
// 表驱动覆盖四种组合，防止将来有人把它改成"套餐覆盖租户策略"（那是越权）。
func TestEffectiveAllowedModels(t *testing.T) {
	policy := []string{"gpt-4o", "deepseek-chat"}
	ent := []string{"deepseek-chat", "claude-sonnet"}

	cases := []struct {
		name      string
		policy    []string
		hasPolicy bool
		ent       []string
		hasEnt    bool
		want      []string
		wantRes   bool
	}{
		{
			name: "两侧都不限制 → 不施加任何限制",
			want: nil, wantRes: false,
		},
		{
			name: "只有租户策略 → 用租户白名单",
			policy: policy, hasPolicy: true,
			want: policy, wantRes: true,
		},
		{
			name: "只有套餐权益 → 用套餐白名单（付费生效）",
			ent: ent, hasEnt: true,
			want: ent, wantRes: true,
		},
		{
			name: "两侧都限制 → 取交集（付费不得突破租户管控）",
			policy: policy, hasPolicy: true,
			ent: ent, hasEnt: true,
			want: []string{"deepseek-chat"}, wantRes: true,
		},
		{
			name: "交集为空 → 谁的模型都用不了，但仍是受限",
			policy: []string{"gpt-4o"}, hasPolicy: true,
			ent: []string{"claude-sonnet"}, hasEnt: true,
			want: []string{}, wantRes: true,
		},
		{
			name: "套餐不限模型（allowed_models 为空）→ 视同无权益限制",
			policy: policy, hasPolicy: true,
			ent: nil, hasEnt: true,
			want: policy, wantRes: true,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, restricted := effectiveAllowedModels(tc.policy, tc.hasPolicy, tc.ent, tc.hasEnt)
			if restricted != tc.wantRes {
				t.Fatalf("restricted 应为 %v，实际 %v（got=%v）", tc.wantRes, restricted, got)
			}
			if !tc.wantRes {
				return
			}
			if len(got) != len(tc.want) {
				t.Fatalf("允许清单应为 %v，实际 %v", tc.want, got)
			}
			for i := range got {
				if got[i] != tc.want[i] {
					t.Errorf("第 %d 项应为 %s，实际 %s（完整 %v）", i, tc.want[i], got[i], got)
				}
			}
		})
	}
}

// 强制点必须真的把权益算进去：租户允许两个模型、用户套餐只含其中一个时，
// 另一个必须被拒。这是"取更严者"在 HTTP 层的行为证据。
func TestEnforceEnterprisePolicyAppliesEntitlement(t *testing.T) {
	newHandler := func(policy []string, hasPolicy bool, ent []string, hasEnt bool) *EntPolicyHandler {
		return &EntPolicyHandler{
			resolveAllowed: func(context.Context, int16, string) ([]string, bool, error) {
				return policy, hasPolicy, nil
			},
			resolveEntitled: func(context.Context, string) ([]string, bool, error) {
				return ent, hasEnt, nil
			},
		}
	}
	claims := &auth.Claims{UserID: "u-1", TenantID: 1}

	cases := []struct {
		name    string
		handler *EntPolicyHandler
		model   string
		wantErr bool
	}{
		{
			name:    "租户允许 A/B、套餐只含 A → A 可用",
			handler: newHandler([]string{"A", "B"}, true, []string{"A"}, true),
			model:   "A",
		},
		{
			name:    "租户允许 A/B、套餐只含 A → B 被拒（付费不越权）",
			handler: newHandler([]string{"A", "B"}, true, []string{"A"}, true),
			model:   "B", wantErr: true,
		},
		{
			name:    "无租户策略、套餐含 A → A 可用",
			handler: newHandler(nil, false, []string{"A"}, true),
			model:   "A",
		},
		{
			name:    "无租户策略、套餐含 A → B 被拒",
			handler: newHandler(nil, false, []string{"A"}, true),
			model:   "B", wantErr: true,
		},
		{
			name:    "两侧都不限制 → 任意模型放行",
			handler: newHandler(nil, false, nil, false),
			model:   "anything",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
			err := tc.handler.EnforceEnterprisePolicy(r, claims, tc.model)
			if tc.wantErr && err == nil {
				t.Fatalf("模型 %s 应被拒绝", tc.model)
			}
			if !tc.wantErr && err != nil {
				t.Fatalf("模型 %s 应放行，实际 %v", tc.model, err)
			}
		})
	}
}

// 权益查询失败必须 fail-open：权益基础设施故障不该让所有人用不了模型。
func TestResolveEntitledModelsFailsOpen(t *testing.T) {
	h := &EntPolicyHandler{
		resolveEntitled: func(context.Context, string) ([]string, bool, error) {
			return nil, false, context.DeadlineExceeded
		},
	}
	models, has := h.resolveEntitledModels(context.Background(), "u-1")
	if has || models != nil {
		t.Fatalf("查询失败应按无权益限制处理（fail-open），实际 has=%v models=%v", has, models)
	}
}

// effectiveModelSet 是 /v1/models 过滤的判定入口，必须把"不限制"表达成
// restricted=false —— 而不是"返回一个全量集合"。
//
// 这条断言防的是一个具体的线上事故：ResolveAllowedModels 在不限制时返回
// admin_model_configs 的全量 model_id，而该表可以为空（实测 0 行）且与
// llm_models 命名空间不同。若调用方拿它当过滤基准，模型下拉会被清空，
// 用户看到"没有任何模型可选"却查不到任何报错。
func TestEffectiveModelSetDistinguishesUnrestricted(t *testing.T) {
	cases := []struct {
		name       string
		policy     []string
		hasPolicy  bool
		ent        []string
		hasEnt     bool
		wantRes    bool
		wantModels []string
	}{
		{
			name: "两侧都不限制 → restricted=false（调用方必须跳过过滤）",
			wantRes: false,
		},
		{
			name: "只有套餐权益 → 用套餐清单过滤",
			ent:  []string{"A"}, hasEnt: true,
			wantRes: true, wantModels: []string{"A"},
		},
		{
			name:   "租户策略与权益取交集",
			policy: []string{"A", "B"}, hasPolicy: true,
			ent: []string{"B"}, hasEnt: true,
			wantRes: true, wantModels: []string{"B"},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			h := &EntPolicyHandler{
				resolveAllowed: func(context.Context, int16, string) ([]string, bool, error) {
					return tc.policy, tc.hasPolicy, nil
				},
				resolveEntitled: func(context.Context, string) ([]string, bool, error) {
					return tc.ent, tc.hasEnt, nil
				},
			}
			set, restricted := h.effectiveModelSet(context.Background(), 1, "u-1")
			if restricted != tc.wantRes {
				t.Fatalf("restricted 应为 %v，实际 %v（set=%v）", tc.wantRes, restricted, set)
			}
			if !tc.wantRes {
				if set != nil {
					t.Fatalf("不限制时不应返回集合，实际 %v", set)
				}
				return
			}
			if len(set) != len(tc.wantModels) {
				t.Fatalf("应允许 %v，实际 %v", tc.wantModels, set)
			}
			for _, m := range tc.wantModels {
				if _, ok := set[m]; !ok {
					t.Errorf("应允许 %s，实际 %v", m, set)
				}
			}
		})
	}
}

// 策略解析失败同样 fail-open：不能因为 ent_model_policies 查询故障就让
// /v1/models 返回空列表或 500。
func TestEffectiveModelSetFailsOpenOnError(t *testing.T) {
	h := &EntPolicyHandler{
		resolveAllowed: func(context.Context, int16, string) ([]string, bool, error) {
			return nil, false, context.DeadlineExceeded
		},
		resolveEntitled: func(context.Context, string) ([]string, bool, error) {
			return nil, false, nil
		},
	}
	set, restricted := h.effectiveModelSet(context.Background(), 1, "u-1")
	if restricted || set != nil {
		t.Fatalf("解析失败应 fail-open（restricted=false），实际 restricted=%v set=%v", restricted, set)
	}
}

// 拒绝原因必须区分"谁拦的"：这决定文案指向「买套餐」还是「找管理员」。
// 判错的代价很具体 —— 租户策略不允许时提示去买套餐，用户会白花钱。
func TestModelAccessReason(t *testing.T) {
	cases := []struct {
		name       string
		policy     []string
		hasPolicy  bool
		ent        []string
		hasEnt     bool
		model      string
		wantReason string
		wantDenied bool
	}{
		{
			name: "两侧都不限制 → 放行",
			model: "A",
		},
		{
			name: "套餐含 A → 放行",
			ent:  []string{"A"}, hasEnt: true, model: "A",
		},
		{
			name: "套餐不含 A → 拒，原因是权益",
			ent:  []string{"B"}, hasEnt: true, model: "A",
			wantReason: ReasonEntitlement, wantDenied: true,
		},
		{
			name: "租户策略不含 A → 拒，原因是策略",
			policy: []string{"B"}, hasPolicy: true, model: "A",
			wantReason: ReasonPolicy, wantDenied: true,
		},
		{
			name: "两层都不含 → 仍归因于策略（买套餐解决不了，别引导付费）",
			policy: []string{"C"}, hasPolicy: true,
			ent: []string{"B"}, hasEnt: true, model: "A",
			wantReason: ReasonPolicy, wantDenied: true,
		},
		{
			name: "租户允许、套餐不限模型 → 放行",
			policy: []string{"A"}, hasPolicy: true,
			ent: nil, hasEnt: true, model: "A",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			h := &EntPolicyHandler{
				resolveAllowed: func(context.Context, int16, string) ([]string, bool, error) {
					return tc.policy, tc.hasPolicy, nil
				},
				resolveEntitled: func(context.Context, string) ([]string, bool, error) {
					return tc.ent, tc.hasEnt, nil
				},
			}
			reason, denied := h.modelAccessReason(context.Background(), 1, "u-1", tc.model)
			if denied != tc.wantDenied {
				t.Fatalf("denied 应为 %v，实际 %v（reason=%s）", tc.wantDenied, denied, reason)
			}
			if tc.wantDenied && reason != tc.wantReason {
				t.Fatalf("原因应为 %s，实际 %s", tc.wantReason, reason)
			}
		})
	}
}

// chatBodyModel 必须优先取 llm_config.model —— 那是 InjectSessionRuntime
// 补齐后的**最终生效**值；顶层 model 只是客户端原始输入。
func TestChatBodyModelPrefersResolved(t *testing.T) {
	cases := []struct {
		name string
		body map[string]interface{}
		want string
	}{
		{
			name: "llm_config.model 优先于顶层 model",
			body: map[string]interface{}{
				"model":      "raw-top",
				"llm_config": map[string]interface{}{"model": "resolved"},
			},
			want: "resolved",
		},
		{
			name: "无 llm_config 时回退顶层 model",
			body: map[string]interface{}{"model": "top"},
			want: "top",
		},
		{
			name: "都没有 → 空串（放行，交给引擎默认模型）",
			body: map[string]interface{}{},
			want: "",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := chatBodyModel(tc.body); got != tc.want {
				t.Fatalf("应为 %q，实际 %q", tc.want, got)
			}
		})
	}
}

// 越界必须返回 403 + model_not_allowed + reason 参数，而不是放行或 500。
func TestGuardChatModelAccessReturns403(t *testing.T) {
	orig := resolveModelAccess
	t.Cleanup(func() { resolveModelAccess = orig })

	resolveModelAccess = func(_ context.Context, _ int16, _ string, modelID string) (string, bool) {
		if modelID == "blocked" {
			return ReasonEntitlement, true
		}
		return "", false
	}

	claims := &auth.Claims{UserID: "u-1", TenantID: 1}

	t.Run("越界模型被拒", func(t *testing.T) {
		r := httptest.NewRequest(http.MethodPost, "/v1/chat/submit", nil)
		r = r.WithContext(auth.WithClaims(r.Context(), claims))
		w := httptest.NewRecorder()
		ok := GuardChatModelAccess(w, r, map[string]interface{}{
			"llm_config": map[string]interface{}{"model": "blocked"},
		})
		if ok {
			t.Fatal("应被拒绝")
		}
		if w.Code != http.StatusForbidden {
			t.Fatalf("状态码应为 403，实际 %d", w.Code)
		}
		body := w.Body.String()
		if !strings.Contains(body, CodeModelNotAllowed) {
			t.Fatalf("响应应含 code=%s，实际 %s", CodeModelNotAllowed, body)
		}
		if !strings.Contains(body, ReasonEntitlement) {
			t.Fatalf("响应应含 reason=%s，实际 %s", ReasonEntitlement, body)
		}
	})

	t.Run("范围内模型放行", func(t *testing.T) {
		r := httptest.NewRequest(http.MethodPost, "/v1/chat/submit", nil)
		r = r.WithContext(auth.WithClaims(r.Context(), claims))
		w := httptest.NewRecorder()
		if !GuardChatModelAccess(w, r, map[string]interface{}{
			"llm_config": map[string]interface{}{"model": "allowed"},
		}) {
			t.Fatalf("应放行，实际状态码 %d", w.Code)
		}
	})

	t.Run("无模型字段放行（交给引擎默认）", func(t *testing.T) {
		r := httptest.NewRequest(http.MethodPost, "/v1/chat/submit", nil)
		r = r.WithContext(auth.WithClaims(r.Context(), claims))
		w := httptest.NewRecorder()
		if !GuardChatModelAccess(w, r, map[string]interface{}{}) {
			t.Fatal("无模型字段应放行")
		}
	})
}
