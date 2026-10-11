package api

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// SSO provider 的租户归属（F2 延伸）：
//
// CreateProvider 此前硬编码 `db.DefaultTenantID` —— 多租户下无法为其它租户配置
// IdP（只能手工改库），那些租户的用户永远用不上 SSO。而登录链路本就读
// provider.TenantID（sso_handler.go 的 Callback），所以修好创建端即可两端一致。
//
// 口径与 ent_identity_handler.go 的角色/群组创建完全相同：取调用者 claims.TenantID，
// 缺失时回退默认租户。

// recordingEntStore 只记录 INSERT 参数；回读（getProvider）一律 ErrNoRows，
// 因为本用例只关心"provider 落在哪个租户"。
type recordingEntStore struct {
	insertArgs []any
	// domainTenant 非空时，`FROM domains` 的查询返回它（模拟"该域名已验证并归属此租户"）
	domainTenant string
	// domainQueryArg 记录 domains 查询实际传入的域名（用于断言 Host 归一化）
	domainQueryArg string
	// features 非 nil 时，`FROM tenant_features` 返回它（nil = 无记录 ⇒ 全关的灰度默认）
	features *TenantFeatures
}

func (s *recordingEntStore) Query(context.Context, string, ...any) (pgx.Rows, error) {
	return nil, errors.New("fake: Query not used")
}

func (s *recordingEntStore) QueryRow(_ context.Context, sql string, args ...any) pgx.Row {
	if strings.Contains(sql, "INSERT INTO ent_oidc_providers") {
		s.insertArgs = args
		return fakeScanRow{fn: func(dest ...any) error {
			*(dest[0].(*string)) = "11111111-1111-1111-1111-111111111111"
			return nil
		}}
	}
	if strings.Contains(sql, "FROM domains") {
		if len(args) > 0 {
			if d, ok := args[0].(string); ok {
				s.domainQueryArg = d
			}
		}
		if s.domainTenant == "" {
			return deadRow{err: pgx.ErrNoRows}
		}
		return fakeScanRow{fn: func(dest ...any) error {
			*(dest[0].(*string)) = s.domainTenant
			return nil
		}}
	}
	if strings.Contains(sql, "FROM tenant_features") {
		if s.features == nil {
			return deadRow{err: pgx.ErrNoRows}
		}
		return fakeScanRow{fn: func(dest ...any) error {
			*(dest[0].(*bool)) = s.features.SSOEnabled
			*(dest[1].(*bool)) = s.features.SSOSelfService
			return nil
		}}
	}
	return deadRow{err: pgx.ErrNoRows}
}

func (s *recordingEntStore) Exec(context.Context, string, ...any) (pgconn.CommandTag, error) {
	return pgconn.NewCommandTag("INSERT 1"), nil
}

// newCreateProviderRequest 构造一个能通过前置校验的创建请求（OIDC 需 issuer）。
func newCreateProviderRequest(claims *auth.Claims) *http.Request {
	body := `{"name":"corp-idp","client_id":"cid","client_secret":"secret",` +
		`"issuer":"https://idp.example.com"}`
	req := httptest.NewRequest(http.MethodPost, "/v1/ent/sso/providers", strings.NewReader(body))
	if claims != nil {
		req = req.WithContext(auth.WithClaims(req.Context(), claims))
	}
	return req
}

func TestCreateProviderUsesCallerTenant(t *testing.T) {
	// 该租户已开放自助配置（否则非 owner 会被 403 —— 见 TestCreateProviderRejectedWithoutSelfService）
	store := &recordingEntStore{features: &TenantFeatures{SSOSelfService: true}}
	h := &SSOHandler{db: store, encKey: make([]byte, 32)} // 32 字节 = AES-256 密钥

	req := newCreateProviderRequest(&auth.Claims{UserID: "u1", TenantID: "tenant-b", Role: "admin"})
	rec := httptest.NewRecorder()
	h.CreateProvider(rec, req)

	if len(store.insertArgs) == 0 {
		t.Fatalf("未走到 INSERT：status=%d body=%s", rec.Code, rec.Body.String())
	}
	if got := store.insertArgs[0]; got != "tenant-b" {
		t.Errorf("INSERT tenant_id = %v，期望 tenant-b（应取自调用者 claims）", got)
	}
}

func TestCreateProviderFallsBackToDefaultTenant(t *testing.T) {
	store := &recordingEntStore{features: &TenantFeatures{SSOSelfService: true}}
	h := &SSOHandler{db: store, encKey: make([]byte, 32)}

	// 无 claims（例如内部调用）⇒ 回退默认租户，保持存量行为
	req := newCreateProviderRequest(nil)
	rec := httptest.NewRecorder()
	h.CreateProvider(rec, req)

	if len(store.insertArgs) == 0 {
		t.Fatalf("未走到 INSERT：status=%d body=%s", rec.Code, rec.Body.String())
	}
	if got := store.insertArgs[0]; got != DefaultTenantID {
		t.Errorf("INSERT tenant_id = %v，期望 %s（无 claims 时回退默认租户）", got, DefaultTenantID)
	}
}

// L1 灰度生效点：租户未开放自助配置、且调用者不是平台 owner ⇒ 拒绝建 provider。
func TestCreateProviderRejectedWithoutSelfService(t *testing.T) {
	store := &recordingEntStore{} // 无 features 记录 ⇒ sso_self_service=false
	h := &SSOHandler{db: store, encKey: make([]byte, 32)}

	req := newCreateProviderRequest(&auth.Claims{UserID: "u1", TenantID: "tenant-b", Role: "admin"})
	rec := httptest.NewRecorder()
	h.CreateProvider(rec, req)

	if rec.Code != http.StatusForbidden {
		t.Errorf("租户未开放自助配置时应 403，得到 %d", rec.Code)
	}
	if len(store.insertArgs) != 0 {
		t.Error("被拒的请求不得写库")
	}
}

// 平台 owner 不受自助开关限制（灰度正是由 owner 逐个放开的）。
func TestCreateProviderAllowedForPlatformOwner(t *testing.T) {
	store := &recordingEntStore{} // sso_self_service=false
	h := &SSOHandler{db: store, encKey: make([]byte, 32)}

	req := newCreateProviderRequest(&auth.Claims{UserID: "root", TenantID: "tenant-b", Role: "owner"})
	rec := httptest.NewRecorder()
	h.CreateProvider(rec, req)

	if len(store.insertArgs) == 0 {
		t.Fatalf("平台 owner 应能建 provider：status=%d body=%s", rec.Code, rec.Body.String())
	}
	if got := store.insertArgs[0]; got != "tenant-b" {
		t.Errorf("INSERT tenant_id = %v，期望 tenant-b", got)
	}
}

// ── 公开列表的跨租户过滤（P0）─────────────────────────────
//
// `ListPublicProviders` 此前只按 `enabled = TRUE` 过滤，任何未登录访客都能看到所有租户
// 的 IdP 及其 provider id。现在按请求 Host → `domains`（已验证）→ 租户过滤。

func TestResolveTenantByHostNormalizesAndMatchesVerifiedDomain(t *testing.T) {
	store := &recordingEntStore{domainTenant: "tenant-b"}
	h := &SSOHandler{db: store}

	// 大写 + 带端口都应被归一化后匹配
	tid, ok := h.resolveTenantByHost(context.Background(), "App.Example.com:3000")
	if !ok || tid != "tenant-b" {
		t.Fatalf("resolveTenantByHost = (%q,%v)，期望 (tenant-b,true)", tid, ok)
	}
	if store.domainQueryArg != "app.example.com" {
		t.Errorf("查询用的域名 = %q，期望归一化后的 app.example.com", store.domainQueryArg)
	}
}

func TestResolveTenantByHostRejectsUnmatchedAndEmpty(t *testing.T) {
	h := &SSOHandler{db: &recordingEntStore{}} // 无匹配域名

	if tid, ok := h.resolveTenantByHost(context.Background(), "unknown.example.org"); ok {
		t.Errorf("未验证/未知域名不应解析出租户，得到 %q", tid)
	}
	if _, ok := h.resolveTenantByHost(context.Background(), ""); ok {
		t.Error("空 Host 不应解析出租户")
	}
	if _, ok := h.resolveTenantByHost(context.Background(), "   "); ok {
		t.Error("空白 Host 不应解析出租户")
	}
}

func TestListPublicProvidersReturnsEmptyForUnmatchedHost(t *testing.T) {
	store := &recordingEntStore{} // 无匹配域名
	h := &SSOHandler{db: store, encKey: make([]byte, 32)}

	req := httptest.NewRequest(http.MethodGet, "/v1/auth/sso/providers", nil)
	req.Host = "unknown.example.org"
	rec := httptest.NewRecorder()

	h.ListPublicProviders(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d，期望 200", rec.Code)
	}
	body := strings.TrimSpace(rec.Body.String())
	if strings.Contains(body, "11111111-1111-1111-1111-111111111111") {
		t.Error("未匹配 Host 时不得泄漏任何 provider")
	}
}

// L1 灰度生效点：域名匹配上了，但该租户**未开通 SSO** ⇒ 仍然返回空列表。
func TestListPublicProvidersEmptyWhenTenantSSODisabled(t *testing.T) {
	// 域名已验证并归属 tenant-b，但 features 无记录 ⇒ sso_enabled=false（灰度默认）
	store := &recordingEntStore{domainTenant: "tenant-b"}
	h := &SSOHandler{db: store, encKey: make([]byte, 32)}

	req := httptest.NewRequest(http.MethodGet, "/v1/auth/sso/providers", nil)
	req.Host = "app.example.com"
	rec := httptest.NewRecorder()

	h.ListPublicProviders(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d，期望 200", rec.Code)
	}
	if strings.Contains(rec.Body.String(), "11111111-1111-1111-1111-111111111111") {
		t.Error("租户未开通 SSO 时不得返回任何 provider")
	}
}

// M1：配置向导 —— 回调地址与内置 provider 模板。
func TestGetProviderGuideReturnsCallbackAndTemplates(t *testing.T) {
	h := &SSOHandler{redirectURL: "https://app.example.com/v1/auth/sso/callback"}

	req := httptest.NewRequest(http.MethodGet, "/v1/ent/sso/providers/guide", nil)
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{
		UserID: "u1", TenantID: "tenant-b", Role: "admin",
	}))
	rec := httptest.NewRecorder()

	h.GetProviderGuide(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d，期望 200", rec.Code)
	}
	body := rec.Body.String()
	for _, want := range []string{
		"callback_url",
		"https://app.example.com/v1/auth/sso/callback",
		"tenant-b",
		"google",
		"github",
		"oidc",
	} {
		if !strings.Contains(body, want) {
			t.Errorf("响应应含 %q", want)
		}
	}
}
