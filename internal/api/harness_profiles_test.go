package api

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// P1-④ 批 B：租户/用户级 harness 配置端点。
//
// 三条关键语义：
//  1. **身份只来自 token** —— 请求体里的 tenant_id/user_id 一律忽略（防越权主开关）；
//  2. **超长/非法前缀是拒绝而不是截断** —— 写入时就该失败，否则用户对不上账；
//  3. **enabled=false 是显式禁用**（占位、不回落），否则用户无法关掉租户强加的 suffix。

const harnessTenantID int16 = 1
const harnessUserID = "22222222-2222-2222-2222-222222222222"

// ── 替身 ─────────────────────────────────────────────────────────────────

// fakeRows 是 pgx.Rows 的最小替身（仓库既有替身只覆盖了 pgx.Row）。
type fakeRows struct {
	rows [][]any
	idx  int
	cur  []any
}

func (r *fakeRows) Close()                                       {}
func (r *fakeRows) Err() error                                   { return nil }
func (r *fakeRows) CommandTag() pgconn.CommandTag                { return pgconn.CommandTag{} }
func (r *fakeRows) FieldDescriptions() []pgconn.FieldDescription { return nil }
func (r *fakeRows) Conn() *pgx.Conn                              { return nil }
func (r *fakeRows) RawValues() [][]byte                          { return nil }
func (r *fakeRows) Values() ([]any, error)                       { return r.cur, nil }

func (r *fakeRows) Next() bool {
	if r.idx >= len(r.rows) {
		return false
	}
	r.cur = r.rows[r.idx]
	r.idx++
	return true
}

func (r *fakeRows) Scan(dest ...any) error {
	if len(dest) != len(r.cur) {
		return fmt.Errorf("fake scan: %d dests, %d values", len(dest), len(r.cur))
	}
	for i := range dest {
		switch d := dest[i].(type) {
		case *string:
			*d, _ = r.cur[i].(string)
		case *bool:
			*d, _ = r.cur[i].(bool)
		default:
			return fmt.Errorf("fake scan: unsupported dest type %T", d)
		}
	}
	return nil
}

type fakeHarnessStore struct {
	// perUser 按 user_id 给出 Query 返回的行（列序：model_prefix, suffix, enabled）
	perUser   map[string][][]any
	queryArgs [][]any
	execSQL   []string
	execArgs  [][]any
}

func (s *fakeHarnessStore) Query(_ context.Context, _ string, args ...any) (pgx.Rows, error) {
	s.queryArgs = append(s.queryArgs, args)
	userID, _ := args[1].(string)
	return &fakeRows{rows: s.perUser[userID]}, nil
}

func (s *fakeHarnessStore) QueryRow(context.Context, string, ...any) pgx.Row {
	return fakeScanRow{fn: func(...any) error { return nil }}
}

func (s *fakeHarnessStore) Exec(_ context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	s.execSQL = append(s.execSQL, sql)
	s.execArgs = append(s.execArgs, args)
	return pgconn.CommandTag{}, nil
}

func newHarnessRequest(method, target, body string, claims *auth.Claims) *http.Request {
	var r *http.Request
	if body == "" {
		r = httptest.NewRequest(method, target, nil)
	} else {
		r = httptest.NewRequest(method, target, strings.NewReader(body))
	}
	if claims != nil {
		r = r.WithContext(auth.WithClaims(r.Context(), claims))
	}
	return r
}

func tenantAdminClaims() *auth.Claims {
	return &auth.Claims{UserID: harnessUserID, Role: "admin", TenantID: harnessTenantID}
}

// ── 身份：只信 token ─────────────────────────────────────────────────────

func TestHarnessProfileIgnoresBodyIdentity(t *testing.T) {
	store := &fakeHarnessStore{}
	h := NewHarnessProfileHandler(store, nil)

	// 请求体**故意**带上别人的身份 —— 必须被忽略
	body := `{"model_prefix":"claude-","system_prompt_suffix":"X","enabled":true,
	          "user_id":"attacker","tenant_id":"other-tenant"}`
	w := httptest.NewRecorder()
	h.PutMine(w, newHarnessRequest(http.MethodPut, "/v1/settings/harness-profiles", body, tenantAdminClaims()))

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	if len(store.execArgs) != 1 {
		t.Fatalf("exec calls = %d, want 1", len(store.execArgs))
	}
	args := store.execArgs[0]
	if args[0] != tenantStr(harnessTenantID) || args[1] != harnessUserID {
		t.Fatalf("scope = (%v, %v)，应来自 claims 而不是请求体", args[0], args[1])
	}
}

func TestHarnessProfileRequiresAuthentication(t *testing.T) {
	h := NewHarnessProfileHandler(&fakeHarnessStore{}, nil)

	w := httptest.NewRecorder()
	h.PutMine(w, newHarnessRequest(http.MethodPut, "/v1/settings/harness-profiles",
		`{"model_prefix":"","system_prompt_suffix":"X","enabled":true}`, nil))

	if w.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", w.Code)
	}
}

// ── 校验：拒绝而不是截断 ─────────────────────────────────────────────────

func TestHarnessProfileRejectsOverlongSuffix(t *testing.T) {
	store := &fakeHarnessStore{}
	h := NewHarnessProfileHandler(store, nil)
	long := strings.Repeat("x", MaxHarnessSuffixChars+1)

	body, _ := json.Marshal(HarnessProfile{ModelPrefix: "claude-", Suffix: long, Enabled: true})
	w := httptest.NewRecorder()
	h.PutMine(w, newHarnessRequest(http.MethodPut, "/v1/settings/harness-profiles", string(body), tenantAdminClaims()))

	if w.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", w.Code)
	}
	if len(store.execArgs) != 0 {
		t.Fatal("超长必须在**落库之前**被拒（否则用户对不上账）")
	}
}

func TestHarnessProfileRejectsLikeWildcards(t *testing.T) {
	store := &fakeHarnessStore{}
	h := NewHarnessProfileHandler(store, nil)

	// 注意：大写不在拒绝列表里 —— 它会被**规范化**为小写（见 TestHarnessProfileLowercasesPrefix）
	for _, prefix := range []string{"%", "claude_%", "claude*"} {
		w := httptest.NewRecorder()
		body, _ := json.Marshal(HarnessProfile{ModelPrefix: prefix, Suffix: "X", Enabled: true})
		h.PutMine(w, newHarnessRequest(http.MethodPut, "/v1/settings/harness-profiles", string(body), tenantAdminClaims()))
		if w.Code != http.StatusBadRequest {
			t.Fatalf("prefix %q: status = %d, want 400（通配符/大写都不该被接受）", prefix, w.Code)
		}
	}
}

func TestHarnessProfileLowercasesPrefix(t *testing.T) {
	store := &fakeHarnessStore{}
	h := NewHarnessProfileHandler(store, nil)

	body, _ := json.Marshal(HarnessProfile{ModelPrefix: "Claude-Sonnet", Suffix: "X", Enabled: true})
	w := httptest.NewRecorder()
	h.PutMine(w, newHarnessRequest(http.MethodPut, "/v1/settings/harness-profiles", string(body), tenantAdminClaims()))

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200（大写应被规范化为小写而非拒绝）", w.Code)
	}
	if got := store.execArgs[0][2]; got != "claude-sonnet" {
		t.Fatalf("stored prefix = %v, want claude-sonnet", got)
	}
}

// ── 写入语义 ─────────────────────────────────────────────────────────────

func TestHarnessProfileUpsertsOnSamePrefix(t *testing.T) {
	store := &fakeHarnessStore{}
	h := NewHarnessProfileHandler(store, nil)

	w := httptest.NewRecorder()
	h.PutMine(w, newHarnessRequest(http.MethodPut, "/v1/settings/harness-profiles",
		`{"model_prefix":"claude-","system_prompt_suffix":"X","enabled":true}`, tenantAdminClaims()))

	if !strings.Contains(store.execSQL[0], "ON CONFLICT (tenant_id, user_id, model_prefix)") {
		t.Fatalf("应为按主键 upsert，实际 SQL=%s", store.execSQL[0])
	}
}

func TestHarnessProfileTenantScopeUsesEmptyUserID(t *testing.T) {
	store := &fakeHarnessStore{}
	h := NewHarnessProfileHandler(store, nil)

	w := httptest.NewRecorder()
	h.PutTenant(w, newHarnessRequest(http.MethodPut, "/v1/admin/settings/harness-profiles",
		`{"model_prefix":"","system_prompt_suffix":"TENANT DEFAULT","enabled":true}`, tenantAdminClaims()))

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", w.Code)
	}
	if got := store.execArgs[0][1]; got != "" {
		t.Fatalf("租户级写入的 user_id = %v, want 空串（租户级默认的约定）", got)
	}
}

func TestHarnessProfileDeleteUsesClaimsScope(t *testing.T) {
	store := &fakeHarnessStore{}
	h := NewHarnessProfileHandler(store, nil)

	r := newHarnessRequest(http.MethodDelete, "/v1/settings/harness-profiles/claude-", "", tenantAdminClaims())
	r.SetPathValue("prefix", "claude-")
	w := httptest.NewRecorder()
	h.DeleteMine(w, r)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", w.Code)
	}
	args := store.execArgs[0]
	if args[0] != tenantStr(harnessTenantID) || args[1] != harnessUserID || args[2] != "claude-" {
		t.Fatalf("delete args = %v，应为 (claims.tenant, claims.user, prefix)", args)
	}
}

func TestHarnessProfileNotifiesOnWrite(t *testing.T) {
	store := &fakeHarnessStore{}
	var notified []string
	h := NewHarnessProfileHandler(store, func(_ context.Context, tenantID string) {
		notified = append(notified, tenantID)
	})

	w := httptest.NewRecorder()
	h.PutMine(w, newHarnessRequest(http.MethodPut, "/v1/settings/harness-profiles",
		`{"model_prefix":"","system_prompt_suffix":"X","enabled":true}`, tenantAdminClaims()))

	if len(notified) != 1 || notified[0] != tenantStr(harnessTenantID) {
		t.Fatalf("notified = %v，写成功后应广播该租户（引擎据此清缓存）", notified)
	}
}

// ── 读取与合并 ───────────────────────────────────────────────────────────

func TestHarnessProfileGetReportsEffectiveAndSources(t *testing.T) {
	store := &fakeHarnessStore{perUser: map[string][][]any{
		"":            {{"", "TENANT", true}},
		harnessUserID: {{"claude-", "USER", true}},
	}}
	h := NewHarnessProfileHandler(store, nil)

	w := httptest.NewRecorder()
	h.GetMine(w, newHarnessRequest(http.MethodGet, "/v1/settings/harness-profiles", "", tenantAdminClaims()))

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", w.Code)
	}
	var resp struct {
		Data struct {
			Effective []HarnessProfile `json:"effective"`
			Limits    struct {
				MaxSuffixChars int `json:"max_suffix_chars"`
			} `json:"limits"`
		} `json:"data"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode: %v; body=%s", err, w.Body.String())
	}
	if resp.Data.Limits.MaxSuffixChars != MaxHarnessSuffixChars {
		t.Fatalf("limits.max_suffix_chars = %d, want %d", resp.Data.Limits.MaxSuffixChars, MaxHarnessSuffixChars)
	}
	byPrefix := map[string]HarnessProfile{}
	for _, p := range resp.Data.Effective {
		byPrefix[p.ModelPrefix] = p
	}
	if got := byPrefix[""].Source; got != "tenant" {
		t.Fatalf("'' 的 source = %q, want tenant", got)
	}
	if got := byPrefix["claude-"]; got.Source != "user" || got.Suffix != "USER" {
		t.Fatalf("claude- = %+v, want source=user suffix=USER", got)
	}
}

func TestEffectiveProfilesUserOverridesTenantIncludingDisable(t *testing.T) {
	tenant := []HarnessProfile{{ModelPrefix: "claude-", Suffix: "TENANT", Enabled: true}}
	// 用户把同一个前缀**显式关掉**：必须整体覆盖，不能因为租户那条 enabled 而复活
	user := []HarnessProfile{{ModelPrefix: "claude-", Suffix: "", Enabled: false}}

	got := effectiveProfiles(tenant, user)

	if len(got) != 1 {
		t.Fatalf("effective = %+v, want 单条覆盖", got)
	}
	if got[0].Enabled || got[0].Source != "user" {
		t.Fatalf("got %+v, want enabled=false 且 source=user", got[0])
	}
}

func TestEffectiveProfilesKeepsTenantOnlyPrefixes(t *testing.T) {
	tenant := []HarnessProfile{{ModelPrefix: "gpt-", Suffix: "T", Enabled: true}}
	user := []HarnessProfile{{ModelPrefix: "claude-", Suffix: "U", Enabled: true}}

	got := effectiveProfiles(tenant, user)

	if len(got) != 2 {
		t.Fatalf("effective = %+v, want 两条并列（不同 prefix）", got)
	}
}

func TestHarnessProfileChannelIsStable(t *testing.T) {
	// 该字面量必须与 Python 侧 `harness_store.CHANGE_CHANNEL` **逐字相同**
	// （跨语言无法共享常量，因此两侧各钉一次）。
	if HarnessProfileChannel != "chiron:harness_profile_changed" {
		t.Fatalf("channel = %q，与 Python 侧约定不符", HarnessProfileChannel)
	}
}
