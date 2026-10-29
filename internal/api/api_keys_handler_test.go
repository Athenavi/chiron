package api

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// ── 替身 ──
//
// 仓库没有 pgxmock/sqlmock，单元测试也不许联网，所以这里用最小假实现。
// 目的是钉住**分支**（越权 404、tenant_id 为空拦截、活跃数上限）与**落库的 SQL 形状**，
// 而不是模拟 PostgreSQL 行为。
//
// 不复用同包的 `fakeRows`（harness_profiles_test.go）：那份只认 string/bool 两列，
// 而这里要扫时间戳、可空 smallint 与 COUNT(*) 的整数。

// apiKeySingleRow 是 pgx.Row 的替身。
type apiKeySingleRow struct {
	scan func(dest ...any) error
}

func (r apiKeySingleRow) Scan(dest ...any) error {
	if r.scan == nil {
		return errors.New("apiKeySingleRow: no scan func")
	}
	return r.scan(dest...)
}

// apiKeyRows 是 pgx.Rows 的替身。
type apiKeyRows struct {
	rows [][]any
	idx  int
}

func (r *apiKeyRows) Close()                                       {}
func (r *apiKeyRows) Err() error                                   { return nil }
func (r *apiKeyRows) CommandTag() pgconn.CommandTag                { return pgconn.CommandTag{} }
func (r *apiKeyRows) FieldDescriptions() []pgconn.FieldDescription { return nil }
func (r *apiKeyRows) Values() ([]any, error)                       { return nil, nil }
func (r *apiKeyRows) RawValues() [][]byte                          { return nil }
func (r *apiKeyRows) Conn() *pgx.Conn                              { return nil }

func (r *apiKeyRows) Next() bool {
	if r.idx >= len(r.rows) {
		return false
	}
	r.idx++
	return true
}

func (r *apiKeyRows) Scan(dest ...any) error {
	row := r.rows[r.idx-1]
	if len(dest) != len(row) {
		return errors.New("apiKeyRows: column count mismatch")
	}
	for i, d := range dest {
		switch p := d.(type) {
		case *string:
			*p, _ = row[i].(string)
		case *bool:
			*p, _ = row[i].(bool)
		case **time.Time:
			if row[i] == nil {
				*p = nil
			} else {
				tm := row[i].(time.Time)
				*p = &tm
			}
		case **int16:
			if row[i] == nil {
				*p = nil
			} else {
				v := row[i].(int16)
				*p = &v
			}
		case *int:
			*p = row[i].(int)
		default:
			return errors.New("apiKeyRows: unsupported dest type")
		}
	}
	return nil
}

type fakeAPIKeyStore struct {
	query    func(ctx context.Context, sql string, args ...interface{}) (pgx.Rows, error)
	queryRow func(ctx context.Context, sql string, args ...interface{}) pgx.Row
	exec     func(ctx context.Context, sql string, args ...interface{}) (pgconn.CommandTag, error)

	queries  []string
	rowSQL   []string
	execs    []string
	execArgs [][]interface{}
}

func (s *fakeAPIKeyStore) Query(ctx context.Context, sql string, args ...interface{}) (pgx.Rows, error) {
	s.queries = append(s.queries, sql)
	if s.query == nil {
		return &apiKeyRows{}, nil
	}
	return s.query(ctx, sql, args...)
}

func (s *fakeAPIKeyStore) QueryRow(ctx context.Context, sql string, args ...interface{}) pgx.Row {
	s.rowSQL = append(s.rowSQL, sql)
	if s.queryRow == nil {
		return apiKeySingleRow{scan: func(dest ...any) error { return pgx.ErrNoRows }}
	}
	return s.queryRow(ctx, sql, args...)
}

func (s *fakeAPIKeyStore) Exec(ctx context.Context, sql string, args ...interface{}) (pgconn.CommandTag, error) {
	s.execs = append(s.execs, sql)
	s.execArgs = append(s.execArgs, args)
	if s.exec == nil {
		return pgconn.NewCommandTag("UPDATE 0"), nil
	}
	return s.exec(ctx, sql, args...)
}

// ── 工具 ──

func apiKeyRequest(t *testing.T, userID string) *http.Request {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/v1/api-keys", nil)
	if userID != "" {
		req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: userID}))
	}
	return req
}

func apiKeyPostRequest(t *testing.T, userID, body string) *http.Request {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/api-keys", strings.NewReader(body))
	if userID != "" {
		req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: userID}))
	}
	return req
}

type apiKeyResponseBody struct {
	Success bool            `json:"success"`
	Data    json.RawMessage `json:"data"`
	Error   string          `json:"error"`
	Code    string          `json:"code"`
}

func decodeAPIKeyResponse(t *testing.T, rec *httptest.ResponseRecorder) apiKeyResponseBody {
	t.Helper()
	var body apiKeyResponseBody
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("response is not JSON: %v (body=%q)", err, rec.Body.String())
	}
	return body
}

// hashOf 独立算一遍，**不调用被测函数** —— 这样才真正钉住"与 middleware 一致"，
// 否则被测函数自己跟自己比，算错也全绿。
func hashOf(plain string) string {
	sum := sha256.Sum256([]byte(plain))
	return hex.EncodeToString(sum[:])
}

// apiKeyTenantRow 让租户校验查询返回给定的 tenant_id（nil 表示用户未绑租户）。
func apiKeyTenantRow(tenantID *int16) pgx.Row {
	return apiKeySingleRow{scan: func(dest ...any) error {
		*(dest[0].(**int16)) = tenantID
		return nil
	}}
}

// ── 纯函数：生成 ──

func TestGenerateAPIKey_PrefixAndLength(t *testing.T) {
	plain, hash, err := generateAPIKey()
	if err != nil {
		t.Fatalf("generateAPIKey: %v", err)
	}
	if !strings.HasPrefix(plain, apiKeyPrefix) {
		t.Errorf("plain key should start with %q, got %q", apiKeyPrefix, plain)
	}
	// sk- + 32 字节的 hex = 3 + 64
	if want := len(apiKeyPrefix) + apiKeyEntropyBytes*2; len(plain) != want {
		t.Errorf("plain key length = %d, want %d", len(plain), want)
	}
	if len(hash) != 64 {
		t.Errorf("hash length = %d, want 64 (sha256 hex)", len(hash))
	}
}

// 这是 T2 里最容易出错、出错后又最难自查的一点：hash 算法与 middleware 偏移一位，
// 用户拿到的 key 立刻 401，现象看起来像"创建失败"。
func TestGenerateAPIKey_HashMatchesMiddlewareAlgorithm(t *testing.T) {
	plain, hash, err := generateAPIKey()
	if err != nil {
		t.Fatalf("generateAPIKey: %v", err)
	}
	if want := hashOf(plain); hash != want {
		t.Errorf("hash = %q, want sha256 hex of plaintext", hash)
	}
	if hash == plain {
		t.Error("hash must not equal plaintext")
	}
}

func TestGenerateAPIKey_IsRandom(t *testing.T) {
	a, _, err := generateAPIKey()
	if err != nil {
		t.Fatalf("generateAPIKey: %v", err)
	}
	b, _, err := generateAPIKey()
	if err != nil {
		t.Fatalf("generateAPIKey: %v", err)
	}
	if a == b {
		t.Error("two generated keys are identical; entropy source looks broken")
	}
}

// ── 纯函数：名称 ──

func TestNormalizeAPIKeyName(t *testing.T) {
	tests := []struct {
		name    string
		input   string
		want    string
		wantErr bool
	}{
		{name: "普通名称", input: "ci-deploy", want: "ci-deploy"},
		{name: "两端空白被裁剪", input: "  laptop  ", want: "laptop"},
		{name: "空串被拒", input: "", wantErr: true},
		{name: "纯空白被拒", input: "   \t\n ", wantErr: true},
		{name: "恰好 128 个字符", input: strings.Repeat("a", 128), want: strings.Repeat("a", 128)},
		{name: "129 个字符被拒", input: strings.Repeat("a", 129), wantErr: true},
		// 关键：按 rune 而不是 byte 计。128 个汉字 = 384 字节，若用 len() 会被误拒，
		// 而 PostgreSQL 的 varchar(128) 是按字符计的，能存。
		{name: "128 个汉字应当放行", input: strings.Repeat("中", 128), want: strings.Repeat("中", 128)},
		{name: "129 个汉字被拒", input: strings.Repeat("中", 129), wantErr: true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := normalizeAPIKeyName(tc.input)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("expected error, got name %q", got)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got != tc.want {
				t.Errorf("got %q, want %q", got, tc.want)
			}
		})
	}
}

// ── List ──

func TestAPIKeyList_RequiresAuth(t *testing.T) {
	h := &APIKeyHandler{store: &fakeAPIKeyStore{}}
	rec := httptest.NewRecorder()
	h.List(rec, apiKeyRequest(t, ""))

	if rec.Code != http.StatusUnauthorized {
		t.Errorf("status = %d, want 401", rec.Code)
	}
}

// 列表必须**按当前用户收窄**：少了 `user_id = $1` 就是把所有用户的 key 列给任意登录用户。
func TestAPIKeyList_ScopesToCurrentUser(t *testing.T) {
	now := time.Now()
	store := &fakeAPIKeyStore{
		query: func(_ context.Context, _ string, args ...interface{}) (pgx.Rows, error) {
			if len(args) != 1 || args[0] != "u1" {
				t.Errorf("query args = %v, want [u1]", args)
			}
			return &apiKeyRows{rows: [][]any{
				{"k1", "laptop", now, nil, nil, false},
			}}, nil
		},
	}
	h := &APIKeyHandler{store: store}
	rec := httptest.NewRecorder()
	h.List(rec, apiKeyRequest(t, "u1"))

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (body=%s)", rec.Code, rec.Body.String())
	}
	body := decodeAPIKeyResponse(t, rec)
	var got []APIKeyRow
	if err := json.Unmarshal(body.Data, &got); err != nil {
		t.Fatalf("decode data: %v", err)
	}
	if len(got) != 1 || got[0].ID != "k1" || got[0].Name != "laptop" {
		t.Fatalf("unexpected rows: %+v", got)
	}
}

// 响应里绝不能出现 key_hash —— "存 hash 不存明文"之外的第二道门。
func TestAPIKeyList_NeverExposesKeyHash(t *testing.T) {
	store := &fakeAPIKeyStore{
		query: func(_ context.Context, sql string, _ ...interface{}) (pgx.Rows, error) {
			// 列表查询本身就不该 SELECT key_hash。
			if strings.Contains(sql, "key_hash") {
				t.Errorf("list query must not select key_hash:\n%s", sql)
			}
			return &apiKeyRows{rows: [][]any{
				{"k1", "laptop", time.Now(), nil, nil, false},
			}}, nil
		},
	}
	h := &APIKeyHandler{store: store}
	rec := httptest.NewRecorder()
	h.List(rec, apiKeyRequest(t, "u1"))

	if strings.Contains(strings.ToLower(rec.Body.String()), "hash") {
		t.Errorf("response body leaks a hash field: %s", rec.Body.String())
	}
}

// 空列表要回 []，不能回 null：前端 v-for 遇到 null 会炸。
func TestAPIKeyList_EmptyIsArrayNotNull(t *testing.T) {
	h := &APIKeyHandler{store: &fakeAPIKeyStore{}}
	rec := httptest.NewRecorder()
	h.List(rec, apiKeyRequest(t, "u1"))

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	var body struct {
		Data json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if strings.TrimSpace(string(body.Data)) != "[]" {
		t.Errorf("data = %s, want []", body.Data)
	}
}

// ── Create ──

func TestAPIKeyCreate_RequiresAuth(t *testing.T) {
	h := &APIKeyHandler{store: &fakeAPIKeyStore{}}
	rec := httptest.NewRecorder()
	h.Create(rec, apiKeyPostRequest(t, "", `{"name":"x"}`))

	if rec.Code != http.StatusUnauthorized {
		t.Errorf("status = %d, want 401", rec.Code)
	}
}

func TestAPIKeyCreate_RejectsEmptyName(t *testing.T) {
	h := &APIKeyHandler{store: &fakeAPIKeyStore{}}
	rec := httptest.NewRecorder()
	h.Create(rec, apiKeyPostRequest(t, "u1", `{"name":"  "}`))

	if rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", rec.Code)
	}
}

// tenant_id 为空的用户，middleware 会直接拒绝其 API Key 请求。若这里放行，
// 用户会拿到一把"创建成功但永远 401"的 key —— 那种失败最难自查，必须在创建时就拦下。
func TestAPIKeyCreate_RejectsUserWithoutTenant(t *testing.T) {
	store := &fakeAPIKeyStore{queryRow: func(_ context.Context, _ string, _ ...interface{}) pgx.Row {
		return apiKeyTenantRow(nil)
	}}
	h := &APIKeyHandler{store: store}
	rec := httptest.NewRecorder()
	h.Create(rec, apiKeyPostRequest(t, "u1", `{"name":"laptop"}`))

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400 (body=%s)", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "tenant") {
		t.Errorf("error should explain the tenant requirement, got %s", rec.Body.String())
	}
	// 不能在用户没有租户时仍去 INSERT。
	for _, s := range store.rowSQL {
		if strings.Contains(s, "INSERT INTO api_keys") {
			t.Error("must not insert an api key for a tenantless user")
		}
	}
}

func TestAPIKeyCreate_RejectsWhenActiveLimitReached(t *testing.T) {
	tenant := int16(1)
	store := &fakeAPIKeyStore{
		queryRow: func(_ context.Context, sql string, _ ...interface{}) pgx.Row {
			switch {
			case strings.Contains(sql, "tenant_id FROM users"):
				return apiKeyTenantRow(&tenant)
			case strings.Contains(sql, "COUNT(*)"):
				return apiKeySingleRow{scan: func(dest ...any) error {
					*(dest[0].(*int)) = apiKeyMaxActivePerUser
					return nil
				}}
			}
			t.Fatalf("unexpected query: %s", sql)
			return apiKeySingleRow{}
		},
	}
	h := &APIKeyHandler{store: store}
	rec := httptest.NewRecorder()
	h.Create(rec, apiKeyPostRequest(t, "u1", `{"name":"laptop"}`))

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400 (body=%s)", rec.Code, rec.Body.String())
	}
	for _, s := range store.rowSQL {
		if strings.Contains(s, "INSERT INTO api_keys") {
			t.Error("must not insert beyond the active key limit")
		}
	}
}

// 创建路径的核心断言：明文只出现在响应里，库里存的是**它的 sha256**。
func TestAPIKeyCreate_StoresHashAndReturnsPlaintextOnce(t *testing.T) {
	tenant := int16(1)
	var insertedHash string
	store := &fakeAPIKeyStore{
		queryRow: func(_ context.Context, sql string, args ...interface{}) pgx.Row {
			switch {
			case strings.Contains(sql, "tenant_id FROM users"):
				return apiKeyTenantRow(&tenant)
			case strings.Contains(sql, "COUNT(*)"):
				return apiKeySingleRow{scan: func(dest ...any) error {
					*(dest[0].(*int)) = 0
					return nil
				}}
			case strings.Contains(sql, "INSERT INTO api_keys"):
				// $4 = key_hash
				insertedHash, _ = args[3].(string)
				return apiKeySingleRow{scan: func(dest ...any) error {
					now := time.Now()
					*(dest[0].(**time.Time)) = &now
					return nil
				}}
			}
			t.Fatalf("unexpected query: %s", sql)
			return apiKeySingleRow{}
		},
	}
	h := &APIKeyHandler{store: store}
	rec := httptest.NewRecorder()
	h.Create(rec, apiKeyPostRequest(t, "u1", `{"name":"  laptop  "}`))

	if rec.Code != http.StatusCreated {
		t.Fatalf("status = %d, want 201 (body=%s)", rec.Code, rec.Body.String())
	}

	body := decodeAPIKeyResponse(t, rec)
	var created struct {
		ID   string `json:"id"`
		Name string `json:"name"`
		Key  string `json:"key"`
	}
	if err := json.Unmarshal(body.Data, &created); err != nil {
		t.Fatalf("decode data: %v", err)
	}

	if created.ID == "" {
		t.Error("created key must carry an id")
	}
	if created.Name != "laptop" {
		t.Errorf("name = %q, want trimmed %q", created.Name, "laptop")
	}
	if !strings.HasPrefix(created.Key, apiKeyPrefix) {
		t.Errorf("plaintext key = %q, want %q prefix", created.Key, apiKeyPrefix)
	}
	if insertedHash == "" {
		t.Fatal("nothing was stored for key_hash")
	}
	if insertedHash == created.Key {
		t.Error("stored value equals plaintext; must store sha256")
	}
	if want := hashOf(created.Key); insertedHash != want {
		t.Errorf("stored hash does not match sha256 of the returned plaintext")
	}
}

// ── Revoke ──

func TestAPIKeyRevoke_RequiresAuth(t *testing.T) {
	h := &APIKeyHandler{store: &fakeAPIKeyStore{}}
	req := httptest.NewRequest(http.MethodDelete, "/v1/api-keys/k1", nil)
	req.SetPathValue("id", "k1")
	rec := httptest.NewRecorder()
	h.Revoke(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Errorf("status = %d, want 401", rec.Code)
	}
}

// 软删除：用户视角是"删了"，库里是 revoked = true —— 排查泄露需要知道
// "这把 key 什么时候停的"，物理删除会断掉这段审计。
func TestAPIKeyRevoke_SetsRevokedAndScopesToOwner(t *testing.T) {
	var gotSQL string
	store := &fakeAPIKeyStore{
		exec: func(_ context.Context, sql string, args ...interface{}) (pgconn.CommandTag, error) {
			gotSQL = sql
			if !strings.Contains(sql, "SET revoked = true") {
				t.Errorf("revoke must be a soft delete, got:\n%s", sql)
			}
			if !strings.Contains(sql, "user_id = $2") {
				t.Errorf("revoke must scope to the owning user, got:\n%s", sql)
			}
			if len(args) != 2 || args[0] != "k1" || args[1] != "u1" {
				t.Errorf("args = %v, want [k1 u1]", args)
			}
			return pgconn.NewCommandTag("UPDATE 1"), nil
		},
	}
	h := &APIKeyHandler{store: store}
	req := httptest.NewRequest(http.MethodDelete, "/v1/api-keys/k1", nil)
	req.SetPathValue("id", "k1")
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1"}))
	rec := httptest.NewRecorder()
	h.Revoke(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (body=%s)", rec.Code, rec.Body.String())
	}
	if gotSQL == "" {
		t.Error("no UPDATE was issued")
	}
}

// 影响 0 行 = 不存在 或 不是你的。两者回**同一个 404**：区分它们就是告诉
// 攻击者"这个 id 确实存在"，可枚举。
func TestAPIKeyRevoke_ZeroRowsReturns404(t *testing.T) {
	h := &APIKeyHandler{store: &fakeAPIKeyStore{
		exec: func(_ context.Context, _ string, _ ...interface{}) (pgconn.CommandTag, error) {
			return pgconn.NewCommandTag("UPDATE 0"), nil
		},
	}}
	req := httptest.NewRequest(http.MethodDelete, "/v1/api-keys/someone-elses", nil)
	req.SetPathValue("id", "someone-elses")
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1"}))
	rec := httptest.NewRecorder()
	h.Revoke(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Errorf("status = %d, want 404 (never 403: that would confirm the id exists)", rec.Code)
	}
}

func TestAPIKeyRevoke_EmptyIDReturns404(t *testing.T) {
	h := &APIKeyHandler{store: &fakeAPIKeyStore{}}
	req := httptest.NewRequest(http.MethodDelete, "/v1/api-keys/", nil)
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1"}))
	rec := httptest.NewRecorder()
	h.Revoke(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Errorf("status = %d, want 404", rec.Code)
	}
}
