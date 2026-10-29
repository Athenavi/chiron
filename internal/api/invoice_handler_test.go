package api

import (
	"context"
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
// 仓库没有 pgxmock/sqlmock，单元测试也不许联网，所以用最小假实现。
//
// 这些替身钉住的是**分支**（409/404/400 的区分）与**SQL 形状**（校验与写入是否在
// 同一语句里、状态流转是否带 `AND status = 'pending'`），而不是模拟 PostgreSQL 的
// 实际行为 —— 后者只有真库能验证，属于 `*_live_test.go` 的范畴。
//
// 命名一律加 invoice 前缀：同包已有 `fakeRows`（harness_profiles_test.go）、
// `fakeScanRow`（mail_handler_test.go）、`apiKeyRows`（api_keys_handler_test.go），
// 重名会让整个包的测试编译失败。

// invoiceSingleRow 是 pgx.Row 的替身。
type invoiceSingleRow struct {
	scan func(dest ...any) error
}

func (r invoiceSingleRow) Scan(dest ...any) error {
	if r.scan == nil {
		return errors.New("invoiceSingleRow: no scan func")
	}
	return r.scan(dest...)
}

// invoiceRows 是 pgx.Rows 的替身。
type invoiceRows struct {
	rows [][]any
	idx  int
}

func (r *invoiceRows) Close()                                       {}
func (r *invoiceRows) Err() error                                   { return nil }
func (r *invoiceRows) CommandTag() pgconn.CommandTag                { return pgconn.CommandTag{} }
func (r *invoiceRows) FieldDescriptions() []pgconn.FieldDescription { return nil }
func (r *invoiceRows) Values() ([]any, error)                       { return nil, nil }
func (r *invoiceRows) RawValues() [][]byte                          { return nil }
func (r *invoiceRows) Conn() *pgx.Conn                              { return nil }

func (r *invoiceRows) Next() bool {
	if r.idx >= len(r.rows) {
		return false
	}
	r.idx++
	return true
}

func (r *invoiceRows) Scan(dest ...any) error {
	row := r.rows[r.idx-1]
	if len(dest) != len(row) {
		return errors.New("invoiceRows: column count mismatch")
	}
	for i, d := range dest {
		switch p := d.(type) {
		case *string:
			*p, _ = row[i].(string)
		case *int64:
			v, _ := row[i].(int64)
			*p = v
		case **time.Time:
			if row[i] == nil {
				*p = nil
			} else {
				tm := row[i].(time.Time)
				*p = &tm
			}
		default:
			return errors.New("invoiceRows: unsupported dest type")
		}
	}
	return nil
}

type fakeInvoiceStore struct {
	query    func(ctx context.Context, sql string, args ...interface{}) (pgx.Rows, error)
	queryRow func(ctx context.Context, sql string, args ...interface{}) pgx.Row
	exec     func(ctx context.Context, sql string, args ...interface{}) (pgconn.CommandTag, error)

	queries  []string
	rowSQL   []string
	execs    []string
	execArgs [][]interface{}
}

func (s *fakeInvoiceStore) Query(ctx context.Context, sql string, args ...interface{}) (pgx.Rows, error) {
	s.queries = append(s.queries, sql)
	if s.query == nil {
		return &invoiceRows{}, nil
	}
	return s.query(ctx, sql, args...)
}

func (s *fakeInvoiceStore) QueryRow(ctx context.Context, sql string, args ...interface{}) pgx.Row {
	s.rowSQL = append(s.rowSQL, sql)
	if s.queryRow == nil {
		return invoiceSingleRow{scan: func(dest ...any) error { return pgx.ErrNoRows }}
	}
	return s.queryRow(ctx, sql, args...)
}

func (s *fakeInvoiceStore) Exec(ctx context.Context, sql string, args ...interface{}) (pgconn.CommandTag, error) {
	s.execs = append(s.execs, sql)
	s.execArgs = append(s.execArgs, args)
	if s.exec == nil {
		return pgconn.NewCommandTag("INSERT 0 0"), nil
	}
	return s.exec(ctx, sql, args...)
}

// ── 辅助 ──

func invoicePostRequest(t *testing.T, userID, body string) *http.Request {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/billing/invoices", strings.NewReader(body))
	if userID != "" {
		req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: userID}))
	}
	return req
}

func invoiceGetRequest(t *testing.T, userID, target string) *http.Request {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, target, nil)
	if userID != "" {
		req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: userID}))
	}
	return req
}

func invoiceReviewRequest(t *testing.T, adminID, invoiceID, body string) *http.Request {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/invoices/"+invoiceID+"/review", strings.NewReader(body))
	req.SetPathValue("id", invoiceID)
	if adminID != "" {
		req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: adminID}))
	}
	return req
}

func decodeInvoiceResponse(t *testing.T, rec *httptest.ResponseRecorder) apiKeyResponseBody {
	t.Helper()
	var body apiKeyResponseBody
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("response is not JSON: %v (body=%q)", err, rec.Body.String())
	}
	return body
}

func duplicateKeyErr() error {
	return &pgconn.PgError{Code: "23505", Message: "duplicate key value violates unique constraint"}
}

// ── 创建：校验 ──

func TestInvoiceCreate_RequiresAuth(t *testing.T) {
	store := &fakeInvoiceStore{}
	h := &InvoiceHandler{store: store}

	rec := httptest.NewRecorder()
	h.Create(rec, invoicePostRequest(t, "", `{"payment_id":"p1","title":"x","tax_no":"y"}`))

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("want 401, got %d", rec.Code)
	}
	if len(store.execs) != 0 {
		t.Fatalf("unauthenticated request must not touch the store, got %d execs", len(store.execs))
	}
}

func TestInvoiceCreate_RejectsMissingFields(t *testing.T) {
	cases := []struct {
		name string
		body string
	}{
		{"missing payment_id", `{"title":"Acme","tax_no":"91110000X"}`},
		{"blank title", `{"payment_id":"p1","title":"   ","tax_no":"91110000X"}`},
		{"blank tax_no", `{"payment_id":"p1","title":"Acme","tax_no":"  "}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			store := &fakeInvoiceStore{}
			h := &InvoiceHandler{store: store}
			rec := httptest.NewRecorder()
			h.Create(rec, invoicePostRequest(t, "u1", tc.body))

			if rec.Code != http.StatusBadRequest {
				t.Fatalf("want 400, got %d (body=%s)", rec.Code, rec.Body.String())
			}
			if len(store.execs) != 0 {
				t.Fatalf("invalid request must not reach the database")
			}
		})
	}
}

// 长度按**字符**计（与 varchar(n) 一致）。用一个汉字算 3 字节的输入来钉住这点：
// 如果实现里写 len(raw) 而不是 len([]rune(raw))，255 个汉字的抬头会被误拒。
func TestInvoiceCreate_LengthLimitsCountRunes(t *testing.T) {
	t.Run("255 chinese chars accepted", func(t *testing.T) {
		title := strings.Repeat("中", 255)
		store := &fakeInvoiceStore{
			exec: func(context.Context, string, ...interface{}) (pgconn.CommandTag, error) {
				return pgconn.NewCommandTag("INSERT 0 1"), nil
			},
			queryRow: func(_ context.Context, sql string, _ ...interface{}) pgx.Row {
				return invoiceSingleRow{scan: func(dest ...any) error {
					now := time.Now()
					*(dest[0].(*string)) = "inv-1"
					*(dest[1].(*string)) = "p1"
					*(dest[2].(*string)) = title
					*(dest[3].(*string)) = "91110000X"
					*(dest[4].(*int64)) = 9900
					*(dest[5].(*string)) = "CNY"
					*(dest[6].(*string)) = "pending"
					*(dest[7].(*string)) = ""
					*(dest[8].(*string)) = ""
					*(dest[9].(**time.Time)) = nil
					*(dest[10].(**time.Time)) = &now
					return nil
				}}
			},
		}
		h := &InvoiceHandler{store: store}
		body, _ := json.Marshal(map[string]string{"payment_id": "p1", "title": title, "tax_no": "91110000X"})
		rec := httptest.NewRecorder()
		h.Create(rec, invoicePostRequest(t, "u1", string(body)))

		if rec.Code != http.StatusCreated {
			t.Fatalf("255-rune title must be accepted (varchar(255) counts characters), got %d: %s",
				rec.Code, rec.Body.String())
		}
	})

	t.Run("256 chinese chars rejected", func(t *testing.T) {
		store := &fakeInvoiceStore{}
		h := &InvoiceHandler{store: store}
		body, _ := json.Marshal(map[string]string{
			"payment_id": "p1", "title": strings.Repeat("中", 256), "tax_no": "91110000X",
		})
		rec := httptest.NewRecorder()
		h.Create(rec, invoicePostRequest(t, "u1", string(body)))

		if rec.Code != http.StatusBadRequest {
			t.Fatalf("want 400, got %d", rec.Code)
		}
	})
}

func TestInvoiceCreate_RejectsOverlongNote(t *testing.T) {
	store := &fakeInvoiceStore{}
	h := &InvoiceHandler{store: store}
	body, _ := json.Marshal(map[string]string{
		"payment_id": "p1", "title": "Acme", "tax_no": "91110000X",
		"note": strings.Repeat("a", invoiceNoteMaxLen+1),
	})
	rec := httptest.NewRecorder()
	h.Create(rec, invoicePostRequest(t, "u1", string(body)))

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", rec.Code)
	}
	if len(store.execs) != 0 {
		t.Fatalf("invalid request must not reach the database")
	}
}

// ── 创建：SQL 形状 ──

// 这条是 T3 的核心断言：**校验与写入必须在同一条语句里**。
// 如果实现写成"先 SELECT 查 payments 再 INSERT"，那么两个并发请求可以都查到
// "可以开票"，然后都去 INSERT —— 唯一索引只能挡住第二次，且挡住的那次会以
// 23505 的形式暴露成一句无从解释的错误。
func TestInvoiceCreate_ValidatesAndInsertsInOneStatement(t *testing.T) {
	store := &fakeInvoiceStore{
		exec: func(context.Context, string, ...interface{}) (pgconn.CommandTag, error) {
			return pgconn.NewCommandTag("INSERT 0 1"), nil
		},
		queryRow: func(context.Context, string, ...interface{}) pgx.Row {
			return invoiceSingleRow{scan: func(dest ...any) error {
				now := time.Now()
				*(dest[0].(*string)) = "inv-1"
				*(dest[1].(*string)) = "p1"
				*(dest[2].(*string)) = "Acme"
				*(dest[3].(*string)) = "91110000X"
				*(dest[4].(*int64)) = 9900
				*(dest[5].(*string)) = "CNY"
				*(dest[6].(*string)) = "pending"
				*(dest[7].(*string)) = ""
				*(dest[8].(*string)) = ""
				*(dest[9].(**time.Time)) = nil
				*(dest[10].(**time.Time)) = &now
				return nil
			}}
		},
	}
	h := &InvoiceHandler{store: store}
	rec := httptest.NewRecorder()
	h.Create(rec, invoicePostRequest(t, "u1", `{"payment_id":"p1","title":"Acme","tax_no":"91110000X"}`))

	if len(store.execs) != 1 {
		t.Fatalf("want exactly 1 exec, got %d", len(store.execs))
	}
	sql := store.execs[0]
	for _, must := range []string{"INSERT INTO invoice_requests", "FROM payments", "p.user_id = $2", "p.status = 'paid'"} {
		if !strings.Contains(sql, must) {
			t.Fatalf("write statement must contain %q to keep check+insert atomic; got:\n%s", must, sql)
		}
	}
	// user_id 必须取自 token，不能取自请求体。
	if got := store.execArgs[0][1]; got != "u1" {
		t.Fatalf("user_id arg must come from claims, got %v", got)
	}
}

// ── 创建：错误分支 ──

func TestInvoiceCreate_DuplicateReturns409(t *testing.T) {
	store := &fakeInvoiceStore{
		exec: func(context.Context, string, ...interface{}) (pgconn.CommandTag, error) {
			return pgconn.CommandTag{}, duplicateKeyErr()
		},
	}
	h := &InvoiceHandler{store: store}
	rec := httptest.NewRecorder()
	h.Create(rec, invoicePostRequest(t, "u1", `{"payment_id":"p1","title":"Acme","tax_no":"91110000X"}`))

	if rec.Code != http.StatusConflict {
		t.Fatalf("want 409 for duplicate payment, got %d: %s", rec.Code, rec.Body.String())
	}
	body := decodeInvoiceResponse(t, rec)
	if body.Code != CodeInvoiceAlreadyRequested {
		t.Fatalf("want code %q, got %q", CodeInvoiceAlreadyRequested, body.Code)
	}
	// 重复申请**不应**再触发诊断查询：唯一索引已经给出确定答案。
	if len(store.rowSQL) != 0 {
		t.Fatalf("duplicate path must not run diagnostic queries, got %d", len(store.rowSQL))
	}
}

func TestInvoiceCreate_OtherUsersPaymentReturns404(t *testing.T) {
	store := &fakeInvoiceStore{
		exec: func(context.Context, string, ...interface{}) (pgconn.CommandTag, error) {
			return pgconn.NewCommandTag("INSERT 0 0"), nil
		},
		queryRow: func(context.Context, string, ...interface{}) pgx.Row {
			return invoiceSingleRow{scan: func(dest ...any) error {
				owner := "someone-else"
				paid := "paid"
				amount := int64(9900)
				*(dest[0].(**string)) = &owner
				*(dest[1].(**string)) = &paid
				*(dest[2].(**int64)) = &amount
				return nil
			}}
		},
	}
	h := &InvoiceHandler{store: store}
	rec := httptest.NewRecorder()
	h.Create(rec, invoicePostRequest(t, "u1", `{"payment_id":"p1","title":"Acme","tax_no":"91110000X"}`))

	// 404 而不是 403：403 等于确认"这个 payment_id 确实存在，只是不属于你"，
	// 攻击者可以据此枚举他人的订单号。
	if rec.Code != http.StatusNotFound {
		t.Fatalf("want 404 (not 403) for someone else's payment, got %d", rec.Code)
	}
}

func TestInvoiceCreate_UnpaidPaymentReturns400(t *testing.T) {
	store := &fakeInvoiceStore{
		exec: func(context.Context, string, ...interface{}) (pgconn.CommandTag, error) {
			return pgconn.NewCommandTag("INSERT 0 0"), nil
		},
		queryRow: func(context.Context, string, ...interface{}) pgx.Row {
			return invoiceSingleRow{scan: func(dest ...any) error {
				owner := "u1"
				pending := "pending"
				amount := int64(9900)
				*(dest[0].(**string)) = &owner
				*(dest[1].(**string)) = &pending
				*(dest[2].(**int64)) = &amount
				return nil
			}}
		},
	}
	h := &InvoiceHandler{store: store}
	rec := httptest.NewRecorder()
	h.Create(rec, invoicePostRequest(t, "u1", `{"payment_id":"p1","title":"Acme","tax_no":"91110000X"}`))

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400 for unpaid order, got %d: %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(strings.ToLower(rec.Body.String()), "paid") {
		t.Fatalf("message should tell the user only paid orders can be invoiced, got %s", rec.Body.String())
	}
}

func TestInvoiceCreate_UnknownPaymentReturns404(t *testing.T) {
	store := &fakeInvoiceStore{
		exec: func(context.Context, string, ...interface{}) (pgconn.CommandTag, error) {
			return pgconn.NewCommandTag("INSERT 0 0"), nil
		},
		// queryRow 默认返回 pgx.ErrNoRows
	}
	h := &InvoiceHandler{store: store}
	rec := httptest.NewRecorder()
	h.Create(rec, invoicePostRequest(t, "u1", `{"payment_id":"nope","title":"Acme","tax_no":"91110000X"}`))

	if rec.Code != http.StatusNotFound {
		t.Fatalf("want 404, got %d", rec.Code)
	}
}

// 成功路径：金额来自 payments 快照，响应里不含审核人。
func TestInvoiceCreate_ReturnsCreatedSnapshot(t *testing.T) {
	store := &fakeInvoiceStore{
		exec: func(context.Context, string, ...interface{}) (pgconn.CommandTag, error) {
			return pgconn.NewCommandTag("INSERT 0 1"), nil
		},
		queryRow: func(context.Context, string, ...interface{}) pgx.Row {
			return invoiceSingleRow{scan: func(dest ...any) error {
				now := time.Now()
				*(dest[0].(*string)) = "inv-1"
				*(dest[1].(*string)) = "p1"
				*(dest[2].(*string)) = "Acme"
				*(dest[3].(*string)) = "91110000X"
				*(dest[4].(*int64)) = 19900 // 快照
				*(dest[5].(*string)) = "CNY"
				*(dest[6].(*string)) = "pending"
				*(dest[7].(*string)) = ""
				*(dest[8].(*string)) = ""
				*(dest[9].(**time.Time)) = nil
				*(dest[10].(**time.Time)) = &now
				return nil
			}}
		},
	}
	h := &InvoiceHandler{store: store}
	rec := httptest.NewRecorder()
	h.Create(rec, invoicePostRequest(t, "u1", `{"payment_id":"p1","title":"Acme","tax_no":"91110000X"}`))

	if rec.Code != http.StatusCreated {
		t.Fatalf("want 201, got %d: %s", rec.Code, rec.Body.String())
	}
	body := decodeInvoiceResponse(t, rec)
	if !body.Success {
		t.Fatalf("want success=true, got %s", rec.Body.String())
	}
	var got InvoiceRequest
	if err := json.Unmarshal(body.Data, &got); err != nil {
		t.Fatalf("data is not an invoice: %v", err)
	}
	if got.AmountCents != 19900 {
		t.Fatalf("amount must be the payment snapshot, got %d", got.AmountCents)
	}
	if got.Status != invoiceStatusPending {
		t.Fatalf("new request must start pending, got %q", got.Status)
	}
	if strings.Contains(rec.Body.String(), "reviewed_by") {
		t.Fatalf("reviewed_by must never be exposed to the applicant: %s", rec.Body.String())
	}
}

// ── 列表 ──

// 空列表必须是 `[]` 而不是 `null`：前端会直接 `v-for` 或 `.length`，
// null 会让它们当场抛错。
func TestInvoiceList_EmptyIsArrayNotNull(t *testing.T) {
	store := &fakeInvoiceStore{}
	h := &InvoiceHandler{store: store}
	rec := httptest.NewRecorder()
	h.List(rec, invoiceGetRequest(t, "u1", "/v1/billing/invoices"))

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	body := decodeInvoiceResponse(t, rec)
	if string(body.Data) != "[]" {
		t.Fatalf("empty list must encode as [], got %s", string(body.Data))
	}
}

func TestInvoiceList_ScopesToCurrentUser(t *testing.T) {
	store := &fakeInvoiceStore{}
	h := &InvoiceHandler{store: store}
	h.List(httptest.NewRecorder(), invoiceGetRequest(t, "u1", "/v1/billing/invoices"))

	if len(store.queries) != 1 {
		t.Fatalf("want 1 query, got %d", len(store.queries))
	}
	if !strings.Contains(store.queries[0], "WHERE user_id = $1") {
		t.Fatalf("list must scope to the current user, got:\n%s", store.queries[0])
	}
}

// ── 审核 ──

func TestInvoiceReview_RejectsNonTerminalStatus(t *testing.T) {
	for _, status := range []string{"pending", "PAID", "", "approve"} {
		t.Run("status="+status, func(t *testing.T) {
			store := &fakeInvoiceStore{}
			h := &InvoiceHandler{store: store}
			body := `{"status":"` + status + `"}`
			rec := httptest.NewRecorder()
			h.Review(rec, invoiceReviewRequest(t, "admin", "inv-1", body))

			if rec.Code != http.StatusBadRequest {
				t.Fatalf("status %q must be rejected, got %d", status, rec.Code)
			}
			if len(store.execs) != 0 {
				t.Fatalf("invalid status must not reach the database")
			}
		})
	}
}

// 状态流转限制必须写在 UPDATE 的 WHERE 里。写成"先读后判"的话，两个管理员
// 同时点「通过」和「驳回」时，后到的那个会静默覆盖前一个的结论。
func TestInvoiceReview_TransitionsOnlyFromPending(t *testing.T) {
	store := &fakeInvoiceStore{
		exec: func(context.Context, string, ...interface{}) (pgconn.CommandTag, error) {
			return pgconn.NewCommandTag("UPDATE 1"), nil
		},
	}
	h := &InvoiceHandler{store: store}
	rec := httptest.NewRecorder()
	h.Review(rec, invoiceReviewRequest(t, "admin", "inv-1", `{"status":"approved"}`))

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec.Code, rec.Body.String())
	}
	sql := store.execs[0]
	if !strings.Contains(sql, "AND status = 'pending'") {
		t.Fatalf("update must guard on pending to keep the transition one-way, got:\n%s", sql)
	}
	// reviewed_at 必须由服务端写，不能由请求体给：否则审核时间可被伪造。
	if !strings.Contains(sql, "reviewed_at = now()") {
		t.Fatalf("reviewed_at must be server-generated, got:\n%s", sql)
	}
}

func TestInvoiceReview_AlreadyReviewedReturns409(t *testing.T) {
	store := &fakeInvoiceStore{
		exec: func(context.Context, string, ...interface{}) (pgconn.CommandTag, error) {
			return pgconn.NewCommandTag("UPDATE 0"), nil
		},
		queryRow: func(context.Context, string, ...interface{}) pgx.Row {
			return invoiceSingleRow{scan: func(dest ...any) error {
				*(dest[0].(*bool)) = true
				return nil
			}}
		},
	}
	h := &InvoiceHandler{store: store}
	rec := httptest.NewRecorder()
	h.Review(rec, invoiceReviewRequest(t, "admin", "inv-1", `{"status":"rejected"}`))

	if rec.Code != http.StatusConflict {
		t.Fatalf("want 409 when another admin already reviewed it, got %d: %s", rec.Code, rec.Body.String())
	}
	if body := decodeInvoiceResponse(t, rec); body.Code != CodeInvoiceAlreadyReviewed {
		t.Fatalf("want code %q, got %q", CodeInvoiceAlreadyReviewed, body.Code)
	}
}

func TestInvoiceReview_NotFoundReturns404(t *testing.T) {
	store := &fakeInvoiceStore{
		exec: func(context.Context, string, ...interface{}) (pgconn.CommandTag, error) {
			return pgconn.NewCommandTag("UPDATE 0"), nil
		},
		queryRow: func(context.Context, string, ...interface{}) pgx.Row {
			return invoiceSingleRow{scan: func(dest ...any) error {
				*(dest[0].(*bool)) = false
				return nil
			}}
		},
	}
	h := &InvoiceHandler{store: store}
	rec := httptest.NewRecorder()
	h.Review(rec, invoiceReviewRequest(t, "admin", "missing", `{"status":"approved"}`))

	if rec.Code != http.StatusNotFound {
		t.Fatalf("want 404, got %d", rec.Code)
	}
}

func TestInvoiceReview_EmptyIDReturns404(t *testing.T) {
	store := &fakeInvoiceStore{}
	h := &InvoiceHandler{store: store}
	rec := httptest.NewRecorder()
	h.Review(rec, invoiceReviewRequest(t, "admin", "", `{"status":"approved"}`))

	if rec.Code != http.StatusNotFound {
		t.Fatalf("want 404, got %d", rec.Code)
	}
}

// ── 管理端列表 ──

// 审核队列的默认视图应该是"等待我处理的事"，而不是把历史全摊开。
func TestInvoiceAdminList_DefaultsToPending(t *testing.T) {
	store := &fakeInvoiceStore{}
	h := &InvoiceHandler{store: store}
	rec := httptest.NewRecorder()
	h.AdminList(rec, invoiceGetRequest(t, "admin", "/invoices"))

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	if len(store.queries) != 1 {
		t.Fatalf("want 1 query, got %d", len(store.queries))
	}
	if !strings.Contains(store.queries[0], "WHERE status = $1") {
		t.Fatalf("default view must filter to pending, got:\n%s", store.queries[0])
	}
}

// `status=all` 才是"看全量"，且此时不该带 WHERE。
func TestInvoiceAdminList_AllOmitsFilter(t *testing.T) {
	store := &fakeInvoiceStore{}
	h := &InvoiceHandler{store: store}
	rec := httptest.NewRecorder()
	h.AdminList(rec, invoiceGetRequest(t, "admin", "/invoices?status=all"))

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	if strings.Contains(store.queries[0], "WHERE status") {
		t.Fatalf("status=all must not filter, got:\n%s", store.queries[0])
	}
}

// 非法过滤值明确 400，而不是静默回落成"全部"：后者会让管理员以为在筛选，
// 实际看到的是全量，容易漏掉待处理的单子。
func TestInvoiceAdminList_RejectsUnknownFilter(t *testing.T) {
	store := &fakeInvoiceStore{}
	h := &InvoiceHandler{store: store}
	rec := httptest.NewRecorder()
	h.AdminList(rec, invoiceGetRequest(t, "admin", "/invoices?status=weird"))

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("want 400 for unknown filter, got %d", rec.Code)
	}
	if len(store.queries) != 0 {
		t.Fatalf("invalid filter must not reach the database")
	}
}

func TestInvoiceAdminList_EmptyIsArrayNotNull(t *testing.T) {
	store := &fakeInvoiceStore{}
	h := &InvoiceHandler{store: store}
	rec := httptest.NewRecorder()
	h.AdminList(rec, invoiceGetRequest(t, "admin", "/invoices?status=pending"))

	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	var body struct {
		Data struct {
			Invoices []InvoiceRequest `json:"invoices"`
			Total    int              `json:"total"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("bad response: %v", err)
	}
	if body.Data.Invoices == nil {
		t.Fatalf("invoices must be [] not null: %s", rec.Body.String())
	}
	if body.Data.Total != 0 {
		t.Fatalf("want total 0, got %d", body.Data.Total)
	}
}
