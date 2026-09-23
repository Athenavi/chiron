package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/auth"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// ── 邮件 handler 的单测基建 ─────────────────────────────
//
// MailHandler 的全部外部依赖都是接口（entQuerier / codeStore / resetTokenStore /
// auth.MailSender），因此这里全部换成内存 fake：不碰 Postgres、Redis 与真实 SMTP，
// 但走的是与生产完全相同的 HTTP handler 代码路径。

// ── fake entQuerier ─────────────────────────────────────

type fakeMailQuerier struct {
	cfg     *mailConfigRow
	userID  string // 密码重置的账号查询结果（空 = 查无此人）
	execs   []string
	execErr error
}

func (f *fakeMailQuerier) Query(context.Context, string, ...any) (pgx.Rows, error) {
	return nil, errors.New("fake: Query not used")
}

func (f *fakeMailQuerier) QueryRow(_ context.Context, sql string, _ ...any) pgx.Row {
	switch {
	case strings.Contains(sql, "FROM ent_mail_config"):
		if f.cfg == nil {
			return deadRow{err: pgx.ErrNoRows}
		}
		return fakeMailConfigRow{row: f.cfg}
	case strings.Contains(sql, "SELECT id FROM users"):
		if f.userID == "" {
			return deadRow{err: pgx.ErrNoRows}
		}
		return fakeScanRow{fn: func(dest ...any) error {
			*(dest[0].(*string)) = f.userID
			return nil
		}}
	default:
		return deadRow{err: pgx.ErrNoRows}
	}
}

// Exec 模拟真实 upsert：把 NamedArgs 里的值回写成内存行，
// 于是"保存后再读回"这条路径与生产（写库 + 回读）行为一致。
func (f *fakeMailQuerier) Exec(_ context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	if f.execErr != nil {
		return pgconn.CommandTag{}, f.execErr
	}
	f.execs = append(f.execs, sql)
	if len(args) == 1 && strings.Contains(sql, "ent_mail_config") {
		if named, ok := args[0].(pgx.NamedArgs); ok {
			f.cfg = mailRowFromNamedArgs(named)
		}
	}
	return pgconn.NewCommandTag("UPDATE 1"), nil
}

// mailRowFromNamedArgs 把 upsert 的参数还原成内存行（键与 saveConfig 的 @name 一致）。
func mailRowFromNamedArgs(a pgx.NamedArgs) *mailConfigRow {
	str := func(k string) string {
		switch v := a[k].(type) {
		case string:
			return v
		case *string:
			if v != nil {
				return *v
			}
		}
		return ""
	}
	num := func(k string) int {
		if v, ok := a[k].(int); ok {
			return v
		}
		return 0
	}
	boolean := func(k string) bool {
		if v, ok := a[k].(bool); ok {
			return v
		}
		return false
	}
	return &mailConfigRow{
		Provider:         str("provider"),
		Enabled:          boolean("enabled"),
		SMTPHost:         str("smtp_host"),
		SMTPPort:         num("smtp_port"),
		SMTPUsername:     str("smtp_username"),
		SMTPPasswordEnc:  str("smtp_password_enc"),
		SMTPSecurity:     str("smtp_security"),
		SMTPSkipVerify:   boolean("smtp_skip_verify"),
		APIBaseURL:       str("api_base_url"),
		APIKeyEnc:        str("api_key_enc"),
		APIChannelID:     num("api_channel_id"),
		APITemplateID:    num("api_template_id"),
		FromAddress:      str("from_address"),
		FromName:         str("from_name"),
		ReplyTo:          str("reply_to"),
		SiteName:         str("site_name"),
		AppBaseURL:       str("app_base_url"),
		LoginEnabled:     boolean("login_enabled"),
		RegisterVerify:   boolean("register_verify"),
		AutoRegister:     boolean("auto_register"),
		ResetEnabled:     boolean("reset_enabled"),
		WelcomeEnabled:   boolean("welcome_enabled"),
		CodeSubject:      str("code_subject"),
		CodeBody:         str("code_body"),
		WelcomeSubject:   str("welcome_subject"),
		WelcomeBody:      str("welcome_body"),
		ResetSubject:     str("reset_subject"),
		ResetBody:        str("reset_body"),
		CodeTTLSeconds:   num("code_ttl_seconds"),
		SendIntervalSecs: num("send_interval_seconds"),
		DailyLimit:       num("daily_limit"),
		TimeoutSeconds:   num("timeout_seconds"),
	}
}

type fakeScanRow struct{ fn func(dest ...any) error }

func (r fakeScanRow) Scan(dest ...any) error { return r.fn(dest...) }

// fakeMailConfigRow 按 mailConfigColumns 的顺序把内存行喂给 Scan。
type fakeMailConfigRow struct{ row *mailConfigRow }

func (r fakeMailConfigRow) Scan(dest ...any) error {
	if len(dest) != 32 {
		return fmt.Errorf("fake scan: %d dests, want 32 (mailConfigColumns changed?)", len(dest))
	}
	c := r.row
	*(dest[0].(*string)) = c.Provider
	*(dest[1].(*bool)) = c.Enabled
	*(dest[2].(**string)) = mailPtrOrNil(c.SMTPHost)
	*(dest[3].(*int)) = c.SMTPPort
	*(dest[4].(**string)) = mailPtrOrNil(c.SMTPUsername)
	*(dest[5].(**string)) = mailPtrOrNil(c.SMTPPasswordEnc)
	*(dest[6].(**string)) = mailPtrOrNil(c.SMTPSecurity)
	*(dest[7].(*bool)) = c.SMTPSkipVerify
	*(dest[8].(**string)) = mailPtrOrNil(c.APIBaseURL)
	*(dest[9].(**string)) = mailPtrOrNil(c.APIKeyEnc)
	*(dest[10].(*int)) = c.APIChannelID
	*(dest[11].(*int)) = c.APITemplateID
	*(dest[12].(**string)) = mailPtrOrNil(c.FromAddress)
	*(dest[13].(**string)) = mailPtrOrNil(c.FromName)
	*(dest[14].(**string)) = mailPtrOrNil(c.ReplyTo)
	*(dest[15].(**string)) = mailPtrOrNil(c.SiteName)
	*(dest[16].(**string)) = mailPtrOrNil(c.AppBaseURL)
	*(dest[17].(*bool)) = c.LoginEnabled
	*(dest[18].(*bool)) = c.RegisterVerify
	*(dest[19].(*bool)) = c.AutoRegister
	*(dest[20].(*bool)) = c.ResetEnabled
	*(dest[21].(*bool)) = c.WelcomeEnabled
	*(dest[22].(**string)) = mailPtrOrNil(c.CodeSubject)
	*(dest[23].(**string)) = mailPtrOrNil(c.CodeBody)
	*(dest[24].(**string)) = mailPtrOrNil(c.WelcomeSubject)
	*(dest[25].(**string)) = mailPtrOrNil(c.WelcomeBody)
	*(dest[26].(**string)) = mailPtrOrNil(c.ResetSubject)
	*(dest[27].(**string)) = mailPtrOrNil(c.ResetBody)
	*(dest[28].(*int)) = c.CodeTTLSeconds
	*(dest[29].(*int)) = c.SendIntervalSecs
	*(dest[30].(*int)) = c.DailyLimit
	*(dest[31].(*int)) = c.TimeoutSeconds
	return nil
}

func mailPtrOrNil(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// ── fake codeStore / resetTokenStore ────────────────────

type memCodeStore struct {
	mu    sync.Mutex
	codes map[string]string
	tries map[string]int
	cool  map[string]bool
	daily map[string]int
}

func newMemCodeStore() *memCodeStore {
	return &memCodeStore{
		codes: map[string]string{},
		tries: map[string]int{},
		cool:  map[string]bool{},
		daily: map[string]int{},
	}
}

func (s *memCodeStore) SetCode(_ context.Context, id, code string, _ time.Duration) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.codes[id] = code
	return nil
}

func (s *memCodeStore) GetCode(_ context.Context, id string) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.codes[id], nil
}

func (s *memCodeStore) DelCode(_ context.Context, id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.codes, id)
	return nil
}

func (s *memCodeStore) IncrTries(_ context.Context, id string) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.tries[id]++
	return s.tries[id], nil
}

func (s *memCodeStore) ResetTries(_ context.Context, id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.tries, id)
	return nil
}

func (s *memCodeStore) MarkCooldown(_ context.Context, id string, _ time.Duration) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.cool[id] = true
	return nil
}

func (s *memCodeStore) InCooldown(_ context.Context, id string) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.cool[id], nil
}

func (s *memCodeStore) IncrDaily(_ context.Context, id string) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.daily[id]++
	return s.daily[id], nil
}

type memResetStore struct {
	mu sync.Mutex
	m  map[string]string
}

func newMemResetStore() *memResetStore { return &memResetStore{m: map[string]string{}} }

func (s *memResetStore) Available() bool { return true }

func (s *memResetStore) Put(_ context.Context, hash, userID string, _ time.Duration) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.m[hash] = userID
	return nil
}

func (s *memResetStore) Take(_ context.Context, hash string) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	userID := s.m[hash]
	delete(s.m, hash) // 取走即失效：同一链接不能改两次密码
	return userID, nil
}

func (s *memResetStore) Del(_ context.Context, hash string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.m, hash)
	return nil
}

// ── fake MailSender ─────────────────────────────────────

type recordingMailSender struct {
	mu   sync.Mutex
	sent []recordedMail
	err  error
}

type recordedMail struct {
	cfg *auth.MailConfig
	msg *auth.MailMessage
}

func (s *recordingMailSender) Send(_ context.Context, cfg *auth.MailConfig, msg *auth.MailMessage) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.err != nil {
		return s.err
	}
	s.sent = append(s.sent, recordedMail{cfg: cfg, msg: msg})
	return nil
}

func (s *recordingMailSender) last() (recordedMail, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.sent) == 0 {
		return recordedMail{}, false
	}
	return s.sent[len(s.sent)-1], true
}

// ── 测试装配 ────────────────────────────────────────────

func enabledMailRow() *mailConfigRow {
	row := defaultMailConfigRow()
	row.Enabled = true
	row.Provider = auth.MailProviderSMTP
	row.SMTPHost = "smtp.example.com"
	row.SMTPPort = 587
	row.SMTPUsername = "mailer"
	row.FromAddress = "noreply@example.com"
	row.AppBaseURL = "https://app.example.com"
	return row
}

func newTestMailHandler(t *testing.T, row *mailConfigRow) (*MailHandler, *fakeMailQuerier, *memCodeStore, *memResetStore, *recordingMailSender) {
	t.Helper()
	h := NewMailHandler(
		auth.NewAuthenticator("0123456789abcdef0123456789abcdef", time.Hour),
		&config.Config{FrontendURL: "https://app.example.com", JWTExpiration: time.Hour},
		nil, // captcha=nil：单测跳过人机验证
	)
	q := &fakeMailQuerier{cfg: row, userID: "user-1"}
	codes := newMemCodeStore()
	tokens := newMemResetStore()
	sender := &recordingMailSender{}
	h.db = q
	h.store = codes
	h.resetTokens = tokens
	h.sender = sender
	h.encKey = bytes.Repeat([]byte{7}, 32)
	return h, q, codes, tokens, sender
}

func doJSON(t *testing.T, fn http.HandlerFunc, body any) *httptest.ResponseRecorder {
	t.Helper()
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(http.MethodPost, "/", bytes.NewReader(raw))
	req.RemoteAddr = "127.0.0.1:40000"
	rec := httptest.NewRecorder()
	fn(rec, req)
	return rec
}

func decodeData(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var resp struct {
		Success bool           `json:"success"`
		Data    map[string]any `json:"data"`
		Error   string         `json:"error"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode response %q: %v", rec.Body.String(), err)
	}
	return resp.Data
}

// ── 公开状态 ────────────────────────────────────────────

func TestMailPublicStatusDisabledWhenUnconfigured(t *testing.T) {
	h, _, _, _, _ := newTestMailHandler(t, nil)
	rec := httptest.NewRecorder()
	h.PublicStatus(rec, httptest.NewRequest(http.MethodGet, "/v1/auth/email/status", nil))
	data := decodeData(t, rec)
	if data["enabled"] != false || data["login_enabled"] != false {
		t.Fatalf("data = %+v", data)
	}
}

func TestMailPublicStatusReflectsConfig(t *testing.T) {
	row := enabledMailRow()
	row.LoginEnabled = true
	row.RegisterVerify = true
	row.ResetEnabled = true
	h, _, _, _, _ := newTestMailHandler(t, row)
	rec := httptest.NewRecorder()
	h.PublicStatus(rec, httptest.NewRequest(http.MethodGet, "/v1/auth/email/status", nil))
	data := decodeData(t, rec)
	for _, key := range []string{"enabled", "login_enabled", "register_verify", "reset_enabled"} {
		if data[key] != true {
			t.Fatalf("%s = %v", key, data[key])
		}
	}
}

// ── 发送验证码 ──────────────────────────────────────────

func TestMailSendCodeForbiddenWhenServiceDisabled(t *testing.T) {
	h, _, _, _, sender := newTestMailHandler(t, nil)
	rec := doJSON(t, h.SendCode, map[string]any{"email": "u@example.com"})
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403", rec.Code)
	}
	if _, ok := sender.last(); ok {
		t.Fatal("no mail may be sent when the service is disabled")
	}
}

func TestMailSendCodeForbiddenWhenPurposeDisabled(t *testing.T) {
	row := enabledMailRow() // login_enabled=false
	h, _, _, _, _ := newTestMailHandler(t, row)
	rec := doJSON(t, h.SendCode, map[string]any{"email": "u@example.com", "purpose": "login"})
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403", rec.Code)
	}
}

func TestMailSendCodeRejectsBadAddress(t *testing.T) {
	row := enabledMailRow()
	row.LoginEnabled = true
	h, _, _, _, _ := newTestMailHandler(t, row)
	rec := doJSON(t, h.SendCode, map[string]any{"email": "not-an-email"})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
}

func TestMailSendCodeSendsAndStoresCode(t *testing.T) {
	row := enabledMailRow()
	row.LoginEnabled = true
	h, _, codes, _, sender := newTestMailHandler(t, row)

	rec := doJSON(t, h.SendCode, map[string]any{"email": "User@Example.com", "purpose": "login"})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d body=%s", rec.Code, rec.Body.String())
	}
	data := decodeData(t, rec)
	if data["status"] != "sent" {
		t.Fatalf("data = %+v", data)
	}
	if data["expire_seconds"].(float64) != float64(row.CodeTTLSeconds) {
		t.Fatalf("expire_seconds = %v", data["expire_seconds"])
	}

	sent, ok := sender.last()
	if !ok {
		t.Fatal("no mail sent")
	}
	if len(sent.msg.To) != 1 || sent.msg.To[0] != "User@example.com" {
		t.Fatalf("recipient = %v（仅域名小写归一化，本地部分保持原样）", sent.msg.To)
	}
	if sent.cfg.Provider != auth.MailProviderSMTP || sent.cfg.Host != "smtp.example.com" {
		t.Fatalf("sender config = %+v", sent.cfg)
	}
	// 发出去的信里必须包含存下来的那个验证码
	stored, _ := codes.GetCode(context.Background(), mailCodeScope(mailPurposeLogin, "user@example.com"))
	if stored == "" {
		t.Fatal("code not stored")
	}
	if !bytes.Contains([]byte(sent.msg.HTML), []byte(stored)) {
		t.Fatalf("mail body does not contain the code %q", stored)
	}
}

func TestMailSendCodeCooldownReturns429(t *testing.T) {
	row := enabledMailRow()
	row.LoginEnabled = true
	h, _, codes, _, sender := newTestMailHandler(t, row)
	if err := codes.MarkCooldown(context.Background(), "u@example.com", time.Minute); err != nil {
		t.Fatal(err)
	}
	rec := doJSON(t, h.SendCode, map[string]any{"email": "u@example.com"})
	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("status = %d, want 429", rec.Code)
	}
	if _, ok := sender.last(); ok {
		t.Fatal("mail must not be sent while in cooldown")
	}
}

func TestMailSendCodeDailyLimitReturns429(t *testing.T) {
	row := enabledMailRow()
	row.LoginEnabled = true
	row.DailyLimit = 2
	h, _, codes, _, _ := newTestMailHandler(t, row)
	for i := 0; i < row.DailyLimit; i++ {
		if _, err := codes.IncrDaily(context.Background(), "u@example.com"); err != nil {
			t.Fatal(err)
		}
	}
	rec := doJSON(t, h.SendCode, map[string]any{"email": "u@example.com"})
	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("status = %d, want 429", rec.Code)
	}
}

// 信发不出去时不能把验证码存进 Redis（否则用户拿到一个永远收不到的码）。
func TestMailSendCodeStoresNothingWhenSendFails(t *testing.T) {
	row := enabledMailRow()
	row.LoginEnabled = true
	h, _, codes, _, sender := newTestMailHandler(t, row)
	sender.err = auth.ErrMailUnreachable

	rec := doJSON(t, h.SendCode, map[string]any{"email": "u@example.com"})
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502", rec.Code)
	}
	if stored, _ := codes.GetCode(context.Background(), mailCodeScope(mailPurposeLogin, "u@example.com")); stored != "" {
		t.Fatalf("code must not be stored after a failed send, got %q", stored)
	}
}

// ── 验证码消费 ──────────────────────────────────────────

func TestMailConsumeCodeIsOneShot(t *testing.T) {
	h, _, codes, _, _ := newTestMailHandler(t, enabledMailRow())
	ctx := context.Background()
	scope := mailCodeScope(mailPurposeRegister, "u@example.com")
	if err := codes.SetCode(ctx, scope, "123456", time.Minute); err != nil {
		t.Fatal(err)
	}
	if err := h.ConsumeCode(ctx, mailPurposeRegister, "u@example.com", "123456"); err != nil {
		t.Fatalf("first consume: %v", err)
	}
	if err := h.ConsumeCode(ctx, mailPurposeRegister, "u@example.com", "123456"); err == nil {
		t.Fatal("验证码必须是一次性的（重放应失败）")
	}
}

func TestMailConsumeCodeWrongCodeInvalidatesAfterMaxTries(t *testing.T) {
	h, _, codes, _, _ := newTestMailHandler(t, enabledMailRow())
	ctx := context.Background()
	scope := mailCodeScope(mailPurposeLogin, "u@example.com")
	if err := codes.SetCode(ctx, scope, "123456", time.Minute); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < mailMaxTries; i++ {
		if err := h.ConsumeCode(ctx, mailPurposeLogin, "u@example.com", "000000"); err == nil {
			t.Fatal("wrong code accepted")
		}
	}
	if stored, _ := codes.GetCode(ctx, scope); stored != "" {
		t.Fatalf("验证码应在错误次数超限后作废，仍存在 %q", stored)
	}
}

// 不同用途的验证码互不覆盖（登录码不能拿去注册）。
func TestMailCodeScopesAreIsolatedByPurpose(t *testing.T) {
	h, _, codes, _, _ := newTestMailHandler(t, enabledMailRow())
	ctx := context.Background()
	if err := codes.SetCode(ctx, mailCodeScope(mailPurposeLogin, "u@example.com"), "111111", time.Minute); err != nil {
		t.Fatal(err)
	}
	if err := h.ConsumeCode(ctx, mailPurposeRegister, "u@example.com", "111111"); err == nil {
		t.Fatal("登录用途的验证码不得用于注册")
	}
}

// ── 管理端配置校验 ──────────────────────────────────────

func recalcFromUpdate(t *testing.T, existing *mailConfigRow, patch map[string]any) *httptest.ResponseRecorder {
	t.Helper()
	h, _, _, _, _ := newTestMailHandler(t, existing)
	body, err := json.Marshal(patch)
	if err != nil {
		t.Fatal(err)
	}
	rec := httptest.NewRecorder()
	httpReq := httptest.NewRequest(http.MethodPut, "/v1/ent/mail/config", bytes.NewReader(body))
	httpReq.RemoteAddr = "127.0.0.1:40000"
	h.UpdateConfig(rec, httpReq)
	return rec
}

func TestMailUpdateConfigRejectsEnableWithoutSMTPHost(t *testing.T) {
	rec := recalcFromUpdate(t, nil, map[string]any{"provider": "smtp", "enabled": true})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d body=%s, want 400", rec.Code, rec.Body.String())
	}
}

func TestMailUpdateConfigRejectsQingchenEnableWithoutBaseURL(t *testing.T) {
	rec := recalcFromUpdate(t, nil, map[string]any{
		"provider": "qingchen", "enabled": true, "api_key": "sk_live_x",
	})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d body=%s, want 400", rec.Code, rec.Body.String())
	}
}

func TestMailUpdateConfigRejectsFeatureWithoutEnabled(t *testing.T) {
	rec := recalcFromUpdate(t, nil, map[string]any{"enabled": false, "login_enabled": true})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d body=%s, want 400", rec.Code, rec.Body.String())
	}
}

func TestMailUpdateConfigRejectsUnknownProvider(t *testing.T) {
	rec := recalcFromUpdate(t, nil, map[string]any{"provider": "sendgrid"})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
}

func TestMailUpdateConfigRejectsBrokenTemplate(t *testing.T) {
	rec := recalcFromUpdate(t, nil, map[string]any{"code_body": "{{.Code"})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
}

func TestMailUpdateConfigRejectsOutOfRangeTTL(t *testing.T) {
	rec := recalcFromUpdate(t, nil, map[string]any{"code_ttl_seconds": 10})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
}

func TestMailUpdateConfigSavesAndMasksSecrets(t *testing.T) {
	h, q, _, _, _ := newTestMailHandler(t, nil)
	body := map[string]any{
		"provider":      "smtp",
		"smtp_host":     "smtp.example.com",
		"smtp_port":     465,
		"smtp_security": "ssl",
		"smtp_username": "mailer",
		"smtp_password": "s3cret",
		"from_address":  "noreply@example.com",
		"enabled":       true,
	}
	raw, _ := json.Marshal(body)
	req := httptest.NewRequest(http.MethodPut, "/v1/ent/mail/config", bytes.NewReader(raw))
	req.RemoteAddr = "127.0.0.1:40000"
	rec := httptest.NewRecorder()
	h.UpdateConfig(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d body=%s", rec.Code, rec.Body.String())
	}
	if len(q.execs) == 0 {
		t.Fatal("config was not persisted")
	}
	// 响应里明文口令不得回显
	var resp struct {
		Data map[string]any `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if resp.Data["smtp_password"] != maskedSecret {
		t.Fatalf("smtp_password = %v, want masked", resp.Data["smtp_password"])
	}
	if s, _ := resp.Data["smtp_host"].(string); s != "smtp.example.com" {
		t.Fatalf("smtp_host = %v", resp.Data["smtp_host"])
	}
}

// secret 以 maskedSecret 回填时表示"保持原值"，不能被当成新口令覆盖。
func TestMailUpdateConfigKeepsExistingSecretOnMaskedInput(t *testing.T) {
	existing := enabledMailRow()
	existing.SMTPPasswordEnc = "cipher-previous"
	h, q, _, _, _ := newTestMailHandler(t, existing)

	body, _ := json.Marshal(map[string]any{"smtp_password": maskedSecret})
	httpReq := httptest.NewRequest(http.MethodPut, "/v1/ent/mail/config", bytes.NewReader(body))
	httpReq.RemoteAddr = "127.0.0.1:40000"
	rec := httptest.NewRecorder()
	h.UpdateConfig(rec, httpReq)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d body=%s", rec.Code, rec.Body.String())
	}
	if q.cfg == nil {
		t.Fatal("config was not persisted")
	}
	if q.cfg.SMTPPasswordEnc != "cipher-previous" {
		t.Fatalf("existing ciphertext was overwritten: %q", q.cfg.SMTPPasswordEnc)
	}
}

// 新口令必须以密文落库（不是明文），且能解密回原值。
func TestMailUpdateConfigEncryptsNewSecret(t *testing.T) {
	existing := enabledMailRow()
	h, q, _, _, _ := newTestMailHandler(t, existing)

	body, _ := json.Marshal(map[string]any{"smtp_password": "s3cret-value"})
	httpReq := httptest.NewRequest(http.MethodPut, "/v1/ent/mail/config", bytes.NewReader(body))
	httpReq.RemoteAddr = "127.0.0.1:40000"
	rec := httptest.NewRecorder()
	h.UpdateConfig(rec, httpReq)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d body=%s", rec.Code, rec.Body.String())
	}
	if q.cfg.SMTPPasswordEnc == "" || q.cfg.SMTPPasswordEnc == "s3cret-value" {
		t.Fatalf("password stored in plaintext: %q", q.cfg.SMTPPasswordEnc)
	}
	plain, err := auth.DecryptAESGCM(h.encKey, q.cfg.SMTPPasswordEnc)
	if err != nil || plain != "s3cret-value" {
		t.Fatalf("decrypt = %q err=%v", plain, err)
	}
}

// ── 密码重置 ────────────────────────────────────────────

func TestMailRequestPasswordResetUnknownEmailStillReturnsSent(t *testing.T) {
	row := enabledMailRow()
	row.ResetEnabled = true
	h, q, _, _, sender := newTestMailHandler(t, row)
	q.userID = "" // 查无此人

	rec := doJSON(t, h.RequestPasswordReset, map[string]any{"email": "nobody@example.com"})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d body=%s（不得暴露邮箱是否存在）", rec.Code, rec.Body.String())
	}
	if data := decodeData(t, rec); data["status"] != "sent" {
		t.Fatalf("data = %+v", data)
	}
	if _, ok := sender.last(); ok {
		t.Fatal("unknown email must not trigger an actual send")
	}
}

func TestMailRequestPasswordResetRejectsWhenDisabled(t *testing.T) {
	row := enabledMailRow() // reset_enabled=false
	h, _, _, _, _ := newTestMailHandler(t, row)
	rec := doJSON(t, h.RequestPasswordReset, map[string]any{"email": "u@example.com"})
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403", rec.Code)
	}
}

func TestMailRequestPasswordResetSendsTokenizedLink(t *testing.T) {
	row := enabledMailRow()
	row.ResetEnabled = true
	h, _, _, tokens, sender := newTestMailHandler(t, row)

	rec := doJSON(t, h.RequestPasswordReset, map[string]any{"email": "u@example.com"})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d body=%s", rec.Code, rec.Body.String())
	}
	sent, ok := sender.last()
	if !ok {
		t.Fatal("reset mail not sent")
	}
	if !bytes.Contains([]byte(sent.msg.HTML), []byte("https://app.example.com/reset-password?token=")) {
		t.Fatalf("reset link missing:\n%s", sent.msg.HTML)
	}
	// 令牌以 SHA-256 存储：Redis 里不该出现明文
	tokens.mu.Lock()
	defer tokens.mu.Unlock()
	if _, ok := tokens.m["user-1"]; ok {
		t.Fatal("token must be stored under its hash, not the user id")
	}
	if len(tokens.m) != 1 {
		t.Fatalf("token count = %d", len(tokens.m))
	}
}

func TestMailConfirmPasswordResetRejectsInvalidToken(t *testing.T) {
	row := enabledMailRow()
	row.ResetEnabled = true
	h, _, _, _, _ := newTestMailHandler(t, row)
	rec := doJSON(t, h.ConfirmPasswordReset, map[string]any{
		"token": "nope", "password": "Str0ng!Passw0rd",
	})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
}

// 管理员关闭密码重置后，此前发出的链接立即失效（不能只靠"申请时"的开关）。
func TestMailConfirmPasswordResetRejectedWhenFeatureDisabled(t *testing.T) {
	row := enabledMailRow() // reset_enabled=false
	h, _, _, tokens, _ := newTestMailHandler(t, row)
	if err := tokens.Put(context.Background(), hashToken("tok"), "user-1", time.Minute); err != nil {
		t.Fatal(err)
	}
	rec := doJSON(t, h.ConfirmPasswordReset, map[string]any{
		"token": "tok", "password": "Str0ng!Passw0rd",
	})
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403", rec.Code)
	}
}

func TestMailConfirmPasswordResetRejectsWeakPassword(t *testing.T) {
	h, _, _, tokens, _ := newTestMailHandler(t, enabledMailRow())
	if err := tokens.Put(context.Background(), hashToken("tok"), "user-1", time.Minute); err != nil {
		t.Fatal(err)
	}
	rec := doJSON(t, h.ConfirmPasswordReset, map[string]any{"token": "tok", "password": "weak"})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
}

func TestMailConfirmPasswordResetUpdatesAndBurnsToken(t *testing.T) {
	row := enabledMailRow()
	row.ResetEnabled = true
	h, q, _, tokens, _ := newTestMailHandler(t, row)
	if err := tokens.Put(context.Background(), hashToken("tok"), "user-1", time.Minute); err != nil {
		t.Fatal(err)
	}
	rec := doJSON(t, h.ConfirmPasswordReset, map[string]any{
		"token": "tok", "password": "Str0ng!Passw0rd",
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d body=%s", rec.Code, rec.Body.String())
	}
	if len(q.execs) == 0 {
		t.Fatal("password was not updated")
	}
	// 令牌一次即废：同一链接不能再改一次
	rec2 := doJSON(t, h.ConfirmPasswordReset, map[string]any{
		"token": "tok", "password": "Str0ng!Passw0rd",
	})
	if rec2.Code != http.StatusBadRequest {
		t.Fatalf("reused token status = %d, want 400", rec2.Code)
	}
}

// ── 注册验证开关 ────────────────────────────────────────

func TestMailRegisterVerifyEnabled(t *testing.T) {
	ctx := context.Background()
	row := enabledMailRow()
	h, _, _, _, _ := newTestMailHandler(t, row)
	got, err := h.RegisterVerifyEnabled(ctx)
	if err != nil || got {
		t.Fatalf("got %v err %v, want false", got, err)
	}

	row.RegisterVerify = true
	h2, _, _, _, _ := newTestMailHandler(t, row)
	got, err = h2.RegisterVerifyEnabled(ctx)
	if err != nil || !got {
		t.Fatalf("got %v err %v, want true", got, err)
	}

	// 未配置时视为关闭（注册流程不应被"邮件没配"阻断）
	h3, _, _, _, _ := newTestMailHandler(t, nil)
	got, err = h3.RegisterVerifyEnabled(ctx)
	if err != nil || got {
		t.Fatalf("got %v err %v, want false", got, err)
	}
}

// ── 配置映射 ────────────────────────────────────────────

func TestMailSenderConfigDecryptsSecrets(t *testing.T) {
	h, _, _, _, _ := newTestMailHandler(t, enabledMailRow())
	enc, err := auth.EncryptAESGCM(h.encKey, "s3cret")
	if err != nil {
		t.Fatal(err)
	}
	row := enabledMailRow()
	row.SMTPPasswordEnc = enc
	row.APIKeyEnc = enc

	cfg, err := h.senderConfig(row)
	if err != nil {
		t.Fatalf("senderConfig: %v", err)
	}
	if cfg.Password != "s3cret" || cfg.APIKey != "s3cret" {
		t.Fatalf("secrets not decrypted: %+v", cfg)
	}
	if cfg.Host != "smtp.example.com" || cfg.From != "noreply@example.com" {
		t.Fatalf("cfg = %+v", cfg)
	}
}

// 管理端「测试发信」必须把服务商原始原因透出：只有「邮件发送失败」时，
// 管理员无法区分"地址写错（404）/ 密钥无效（401）/ 模板不存在"。
func TestMailSendTestSurfacesProviderError(t *testing.T) {
	h, _, _, _, sender := newTestMailHandler(t, enabledMailRow())
	sender.err = fmt.Errorf("%w: HTTP 404: 404 page not found", auth.ErrMailSendFailed)

	rec := doJSON(t, h.SendTest, map[string]any{"to": "you@example.com"})
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502", rec.Code)
	}
	var resp struct {
		Error string `json:"error"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(resp.Error, "404") {
		t.Fatalf("error 未透出服务商原因: %q", resp.Error)
	}
}

// 面向终端用户的发码路径不得回显服务商细节（内部拓扑/凭据状态不该外泄）。
func TestMailSendCodeDoesNotLeakProviderDetail(t *testing.T) {
	row := enabledMailRow()
	row.LoginEnabled = true
	h, _, _, _, sender := newTestMailHandler(t, row)
	sender.err = fmt.Errorf("%w: HTTP 401: Unauthorized", auth.ErrMailSendFailed)

	rec := doJSON(t, h.SendCode, map[string]any{"email": "u@example.com"})
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502", rec.Code)
	}
	var resp struct {
		Error string `json:"error"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	for _, leak := range []string{"401", "Unauthorized", "HTTP"} {
		if strings.Contains(resp.Error, leak) {
			t.Fatalf("终端用户路径泄露了服务商细节 %q: %q", leak, resp.Error)
		}
	}
}
