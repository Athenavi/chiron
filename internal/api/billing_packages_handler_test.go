package api

import (
	"context"
	"net/http"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
)

// billing_packages_handler_test.go —— 套餐目录管理端的纯校验与挂载测试（P2-2c）。
//
// 只覆盖不需要数据库的部分：`buildPackage` 在 `allowed_models` 为空时不会查
// `llm_models`（空清单 = 不限制模型，本就有意义），因此可在无库环境下完整测
// 字段校验。涉及 `llm_models` 的路径放在真库测试里。

// TestBuildPackageValidation 覆盖字段级校验与"一次报全部问题"的语义。
func TestBuildPackageValidation(t *testing.T) {
	h := &BillingHandler{}
	valid := billingPackageRequest{
		Code:         "starter",
		Name:         "Starter",
		Credits:      1000,
		PriceCents:   990,
		Currency:     "CNY",
		DurationDays: 30,
	}

	cases := []struct {
		name        string
		mutate      func(*billingPackageRequest)
		requireCode bool
		wantSubstr  []string
		wantNoProb  bool
	}{
		{
			name:       "合法请求无问题",
			mutate:     func(*billingPackageRequest) {},
			wantNoProb: true,
		},
		{
			name:       "缺 code（新建）",
			mutate:     func(b *billingPackageRequest) { b.Code = "  " },
			wantSubstr: []string{"code is required"},
		},
		{
			name:   "缺 code 但更新模式下不校验",
			mutate: func(b *billingPackageRequest) { b.Code = "" },
			// requireCode=false 时 code 只用于新建，更新忽略
			wantNoProb: true,
		},
		{
			name:       "code 超过 64 字符",
			mutate:     func(b *billingPackageRequest) { b.Code = strings.Repeat("x", 65) },
			wantSubstr: []string{"at most 64"},
		},
		{
			name:       "缺 name",
			mutate:     func(b *billingPackageRequest) { b.Name = "" },
			wantSubstr: []string{"name is required"},
		},
		{
			name:       "name 超过 128 字符",
			mutate:     func(b *billingPackageRequest) { b.Name = strings.Repeat("名", 129) },
			wantSubstr: []string{"at most 128"},
		},
		{
			name:       "credits 为负",
			mutate:     func(b *billingPackageRequest) { b.Credits = -1 },
			wantSubstr: []string{"credits must be >= 0"},
		},
		{
			name:       "price_cents 为负",
			mutate:     func(b *billingPackageRequest) { b.PriceCents = -1 },
			wantSubstr: []string{"price_cents must be >= 0"},
		},
		{
			name:       "duration_days 为 0 撞 CHECK",
			mutate:     func(b *billingPackageRequest) { b.DurationDays = 0 },
			wantSubstr: []string{"duration_days must be > 0"},
		},
		{
			name:       "币种不在支持范围",
			mutate:     func(b *billingPackageRequest) { b.Currency = "EUR" },
			wantSubstr: []string{"currency must be CNY or USD"},
		},
		{
			name: "多个问题一次性返回",
			mutate: func(b *billingPackageRequest) {
				b.Name = ""
				b.Credits = -5
				b.DurationDays = 0
			},
			wantSubstr: []string{"name is required", "credits must be >= 0", "duration_days must be > 0"},
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			body := valid
			c.mutate(&body)
			requireCode := true
			if c.name == "缺 code 但更新模式下不校验" {
				requireCode = false
			}
			pkg, problems, err := h.buildPackage(context.Background(), body, requireCode)
			if err != nil {
				t.Fatalf("buildPackage 返回基础设施错误: %v", err)
			}
			if c.wantNoProb {
				if len(problems) != 0 {
					t.Fatalf("期望无问题，实际 %v", problems)
				}
				if pkg == nil {
					t.Fatal("无问题时应返回 pkg")
				}
				return
			}
			joined := strings.Join(problems, "; ")
			for _, want := range c.wantSubstr {
				if !strings.Contains(joined, want) {
					t.Errorf("问题列表缺少 %q，实际: %s", want, joined)
				}
			}
		})
	}
}

// TestBuildPackageNormalizesCurrency 币种规范化：空值默认 CNY，小写转大写。
//
// 这两条直接决定下单能否成功：`CreatePayment` 会拿套餐币种与渠道要求比对，
// 存成 "cny" 会让支付宝订单被拒。
func TestBuildPackageNormalizesCurrency(t *testing.T) {
	h := &BillingHandler{}
	cases := map[string]string{
		"":    "CNY",
		"cny": "CNY",
		"usd": "USD",
	}

	for input, want := range cases {
		t.Run("currency="+input, func(t *testing.T) {
			pkg, problems, err := h.buildPackage(context.Background(), billingPackageRequest{
				Code:         "p1",
				Name:         "P1",
				Credits:      100,
				PriceCents:   100,
				Currency:     input,
				DurationDays: 30,
			}, true)
			if err != nil {
				t.Fatalf("buildPackage 错误: %v", err)
			}
			if len(problems) != 0 {
				t.Fatalf("不应有问题: %v", problems)
			}
			if pkg.Currency != want {
				t.Errorf("币种 = %q，期望 %q", pkg.Currency, want)
			}
		})
	}
}

// TestBuildPackageTrimsAndDefaults 名称与 code 去空格、空模型清单落成空切片。
//
// 空切片而不是 nil：`allowed_models` 是 text[] NOT NULL，"nil 切片"在 pgx 里
// 会写成 NULL 而撞非空约束 —— 这条把"不限制模型"固定在空数组语义上。
func TestBuildPackageTrimsAndDefaults(t *testing.T) {
	h := &BillingHandler{}
	pkg, problems, err := h.buildPackage(context.Background(), billingPackageRequest{
		Code:         "  starter  ",
		Name:         "  Starter  ",
		Credits:      100,
		PriceCents:   100,
		DurationDays: 30,
	}, true)
	if err != nil {
		t.Fatalf("buildPackage 错误: %v", err)
	}
	if len(problems) != 0 {
		t.Fatalf("不应有问题: %v", problems)
	}
	if pkg.Code != "starter" {
		t.Errorf("code 未去空格: %q", pkg.Code)
	}
	if pkg.Name != "Starter" {
		t.Errorf("name 未去空格: %q", pkg.Name)
	}
	if pkg.AllowedModels == nil {
		t.Error("allowed_models 应为空切片而非 nil（否则写库会撞 NOT NULL）")
	}
	if len(pkg.AllowedModels) != 0 {
		t.Errorf("allowed_models 应为空: %v", pkg.AllowedModels)
	}
}

// TestRegisterBillingAdminRoutes 套餐路由能挂上且不 panic。
//
// 这条防的是 pattern 冲突：`adminMux` 里已注册了 invoice 的 `/invoices`，
// 若套餐用了同样的 pattern，`http.ServeMux.HandleFunc` 会直接 panic —— 而 panic
// 发生在网关启动路径上，表现为"服务起不来"而不是"某个接口 404"。
func TestRegisterBillingAdminRoutes(t *testing.T) {
	h := &BillingHandler{}
	mux := http.NewServeMux()
	// 先挂一个无关的，模拟 adminMux 已有内容
	mux.HandleFunc("GET /invoices", func(http.ResponseWriter, *http.Request) {})

	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("注册路由 panic: %v", r)
		}
	}()
	h.RegisterAdminRoutes(mux)

	// 用实际请求确认 pattern 生效（注册了但写错前缀的话，这里会 404/405）。
	cases := []struct {
		method, path string
	}{
		{http.MethodGet, "/billing/packages"},
		{http.MethodPost, "/billing/packages"},
		{http.MethodPut, "/billing/packages/pkg-1"},
		{http.MethodDelete, "/billing/packages/pkg-1"},
		{http.MethodPost, "/billing/packages/pkg-1/restore"},
	}
	for _, c := range cases {
		req, err := http.NewRequest(c.method, c.path, nil)
		if err != nil {
			t.Fatalf("构造请求失败: %v", err)
		}
		_, pattern := mux.Handler(req)
		if pattern == "" {
			t.Errorf("%s %s 没有匹配到路由", c.method, c.path)
		}
	}
}

// TestBillingManagePermissionGrants 校验 billing:manage 授予口径。
//
// 新建权限点最容易出的两个问题：(1) 忘了加进 RolePermissions，于是连 owner
// 都被 403 —— 后台页面全员不可用；(2) 忘了加入 apiKeyDeniedPerms，于是长期
// 凭据能改定价。
func TestBillingManagePermissionGrants(t *testing.T) {
	if auth.PermBillingManage != "billing:manage" {
		t.Fatalf("权限串被改动: %q", auth.PermBillingManage)
	}

	for _, role := range []string{"owner", "admin"} {
		claims := &auth.Claims{Role: role}
		if !auth.HasPermission(claims, auth.PermBillingManage) {
			t.Errorf("%s 应拥有 billing:manage", role)
		}
	}

	if auth.HasPermission(&auth.Claims{Role: "user"}, auth.PermBillingManage) {
		t.Error("普通 user 不应拥有 billing:manage")
	}

	// API Key 不得代持定价权
	for _, p := range auth.APIKeyPermissions("owner") {
		if p == auth.PermBillingManage {
			t.Error("api key 不应获得 billing:manage")
		}
	}
}
