package auth

import (
	"bufio"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// ── 晴辰云邮 HTTP 通道（docs/mail.md 契约）────────────────

func TestSendQingchenSuccess(t *testing.T) {
	var gotPath, gotAuth string
	var gotBody qingchenSendRequest
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotAuth = r.Header.Get("Authorization")
		if err := json.NewDecoder(r.Body).Decode(&gotBody); err != nil {
			t.Errorf("decode request: %v", err)
		}
		w.WriteHeader(http.StatusAccepted)
		_, _ = w.Write([]byte(`{"message":"Email queued successfully","queue_id":1024}`))
	}))
	defer srv.Close()

	sender := NewHTTPMailSender()
	err := sender.Send(context.Background(), &MailConfig{
		Provider: MailProviderQingchen,
		BaseURL:  srv.URL + "/api/v1",
		APIKey:   "sk_live_test",
		From:     "noreply@example.com",
	}, &MailMessage{
		To:      []string{"user@example.com"},
		Subject: "您的验证码",
		HTML:    "<h1>8848</h1>",
	})
	if err != nil {
		t.Fatalf("send: %v", err)
	}
	if gotPath != "/api/v1/send" {
		t.Fatalf("path = %q, want /api/v1/send", gotPath)
	}
	if gotAuth != "Bearer sk_live_test" {
		t.Fatalf("authorization = %q", gotAuth)
	}
	if gotBody.To != "user@example.com" || gotBody.Subject != "您的验证码" || gotBody.Body != "<h1>8848</h1>" {
		t.Fatalf("unexpected payload: %+v", gotBody)
	}
	if gotBody.From != "noreply@example.com" {
		t.Fatalf("from = %q", gotBody.From)
	}
}

func TestSendQingchenTemplateAndChannel(t *testing.T) {
	var gotBody qingchenSendRequest
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&gotBody)
		w.WriteHeader(http.StatusAccepted)
	}))
	defer srv.Close()

	err := NewHTTPMailSender().Send(context.Background(), &MailConfig{
		Provider: MailProviderQingchen,
		BaseURL:  srv.URL,
		APIKey:   "k",
	}, &MailMessage{
		To:         []string{"u@example.com"},
		TemplateID: 7,
		Variables:  map[string]string{"code": "8848"},
		ChannelID:  3,
	})
	if err != nil {
		t.Fatalf("send: %v", err)
	}
	if gotBody.TemplateID != 7 || gotBody.ChannelID != 3 {
		t.Fatalf("template/channel not passed: %+v", gotBody)
	}
	if gotBody.Variables["code"] != "8848" {
		t.Fatalf("variables not passed: %+v", gotBody.Variables)
	}
}

// 附件走本地文件路径时必须自动读盘并转 Base64（docs/mail.md 编写要求 5）。
func TestSendQingchenAttachmentFromFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "invoice.pdf")
	content := []byte("%PDF-1.4 fake")
	if err := os.WriteFile(path, content, 0o600); err != nil {
		t.Fatal(err)
	}

	var gotBody struct {
		Attachments []struct {
			Filename    string `json:"filename"`
			Content     string `json:"content"`
			ContentType string `json:"content_type"`
		} `json:"attachments"`
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&gotBody)
		w.WriteHeader(http.StatusAccepted)
	}))
	defer srv.Close()

	att, err := AttachmentFromFile(path)
	if err != nil {
		t.Fatalf("AttachmentFromFile: %v", err)
	}
	att.ContentType = "application/pdf"
	err = NewHTTPMailSender().Send(context.Background(), &MailConfig{
		Provider: MailProviderQingchen, BaseURL: srv.URL, APIKey: "k",
	}, &MailMessage{
		To: []string{"u@example.com"}, Subject: "发票", HTML: "<p>请查收</p>",
		Attachments: []MailAttachment{att},
	})
	if err != nil {
		t.Fatalf("send: %v", err)
	}
	if len(gotBody.Attachments) != 1 {
		t.Fatalf("attachments = %+v", gotBody.Attachments)
	}
	if gotBody.Attachments[0].Filename != "invoice.pdf" {
		t.Fatalf("filename = %q", gotBody.Attachments[0].Filename)
	}
	decoded, err := base64.StdEncoding.DecodeString(gotBody.Attachments[0].Content)
	if err != nil {
		t.Fatalf("content is not base64: %v", err)
	}
	if string(decoded) != string(content) {
		t.Fatalf("content round-trip mismatch: %q", decoded)
	}
}

// 超过 10MB 的附件在本地就被拦下，不发无谓请求。
func TestSendQingchenRejectsOversizeAttachment(t *testing.T) {
	called := false
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
		w.WriteHeader(http.StatusAccepted)
	}))
	defer srv.Close()

	err := NewHTTPMailSender().Send(context.Background(), &MailConfig{
		Provider: MailProviderQingchen, BaseURL: srv.URL, APIKey: "k",
	}, &MailMessage{
		To: []string{"u@example.com"}, Subject: "s", HTML: "b",
		Attachments: []MailAttachment{{
			Filename: "big.bin",
			Content:  make([]byte, MaxMailAttachmentBytes+1),
		}},
	})
	if !errors.Is(err, ErrMailConfigInvalid) {
		t.Fatalf("err = %v, want ErrMailConfigInvalid", err)
	}
	if called {
		t.Fatal("oversize attachment must be rejected before any HTTP call")
	}
}

// 429 触发指数退避重试；重试成功后整体视为成功（docs/mail.md 编写要求 4）。
func TestSendQingchenRateLimitRetriesThenSucceeds(t *testing.T) {
	restore := shortBackoff(t)
	defer restore()

	var mu sync.Mutex
	attempts := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		attempts++
		n := attempts
		mu.Unlock()
		if n == 1 {
			w.Header().Set("Retry-After", "0")
			w.WriteHeader(http.StatusTooManyRequests)
			_, _ = w.Write([]byte(`{"error":"Too many requests, please try again later"}`))
			return
		}
		w.WriteHeader(http.StatusAccepted)
	}))
	defer srv.Close()

	err := NewHTTPMailSender().Send(context.Background(), &MailConfig{
		Provider: MailProviderQingchen, BaseURL: srv.URL, APIKey: "k",
	}, &MailMessage{To: []string{"u@example.com"}, Subject: "s", HTML: "b"})
	if err != nil {
		t.Fatalf("send: %v", err)
	}
	if attempts != 2 {
		t.Fatalf("attempts = %d, want 2", attempts)
	}
}

// 一直 429 时重试有限次后放弃（不会无限打服务商）。
func TestSendQingchenRateLimitExhausted(t *testing.T) {
	restore := shortBackoff(t)
	defer restore()

	attempts := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		attempts++
		w.WriteHeader(http.StatusTooManyRequests)
	}))
	defer srv.Close()

	err := NewHTTPMailSender().Send(context.Background(), &MailConfig{
		Provider: MailProviderQingchen, BaseURL: srv.URL, APIKey: "k",
	}, &MailMessage{To: []string{"u@example.com"}, Subject: "s", HTML: "b"})
	if !errors.Is(err, ErrMailSendFailed) {
		t.Fatalf("err = %v, want ErrMailSendFailed", err)
	}
	if want := mailHTTPMaxRetries + 1; attempts != want {
		t.Fatalf("attempts = %d, want %d", attempts, want)
	}
}

func TestSendQingchenErrorStatusCarriesProviderMessage(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"error":"Template not found"}`))
	}))
	defer srv.Close()

	err := NewHTTPMailSender().Send(context.Background(), &MailConfig{
		Provider: MailProviderQingchen, BaseURL: srv.URL, APIKey: "k",
	}, &MailMessage{To: []string{"u@example.com"}, TemplateID: 99})
	if !errors.Is(err, ErrMailSendFailed) {
		t.Fatalf("err = %v, want ErrMailSendFailed", err)
	}
	if !strings.Contains(err.Error(), "Template not found") {
		t.Fatalf("provider error message lost: %v", err)
	}
}

func TestSendQingchenUnreachable(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	url := srv.URL
	srv.Close() // 立刻关掉：制造连接失败

	err := NewHTTPMailSender().Send(context.Background(), &MailConfig{
		Provider: MailProviderQingchen, BaseURL: url, APIKey: "k", TimeoutSeconds: 2,
	}, &MailMessage{To: []string{"u@example.com"}, Subject: "s", HTML: "b"})
	if !errors.Is(err, ErrMailUnreachable) {
		t.Fatalf("err = %v, want ErrMailUnreachable", err)
	}
}

// 服务地址未配置时必须 fail-loud，而不是悄悄打到某个内置域名。
func TestSendQingchenRequiresConfiguredBaseURL(t *testing.T) {
	err := NewHTTPMailSender().Send(context.Background(), &MailConfig{
		Provider: MailProviderQingchen, APIKey: "k",
	}, &MailMessage{To: []string{"u@example.com"}, Subject: "s", HTML: "b"})
	if !errors.Is(err, ErrMailConfigInvalid) {
		t.Fatalf("err = %v, want ErrMailConfigInvalid", err)
	}
}

// ── SMTP 通道 ───────────────────────────────────────────

func TestSendSMTPDeliversMIME(t *testing.T) {
	srv := startFakeSMTP(t, "")
	defer srv.Close()

	err := NewHTTPMailSender().Send(context.Background(), &MailConfig{
		Provider: MailProviderSMTP,
		Host:     srv.host(),
		Port:     srv.port(),
		Security: MailSecurityNone,
		From:     "noreply@example.com",
		FromName: "晴辰云邮",
	}, &MailMessage{
		To:      []string{"user@example.com"},
		Subject: "测试邮件",
		HTML:    "<h1>Hello</h1>",
	})
	if err != nil {
		t.Fatalf("send: %v", err)
	}
	from, rcpts, data := srv.captured()
	if from != "noreply@example.com" {
		t.Fatalf("MAIL FROM = %q", from)
	}
	if len(rcpts) != 1 || rcpts[0] != "user@example.com" {
		t.Fatalf("RCPT TO = %v", rcpts)
	}
	msg := string(data)
	lower := strings.ToLower(msg)
	for _, want := range []string{"subject: ", "text/html", "mime-version: 1.0"} {
		if !strings.Contains(lower, want) {
			t.Fatalf("message missing %q:\n%s", want, msg)
		}
	}
	// 正文以 base64 内联（大小写敏感，单独断言）
	if !strings.Contains(msg, base64.StdEncoding.EncodeToString([]byte("<h1>Hello</h1>"))) {
		t.Fatalf("html body not base64-inlined:\n%s", msg)
	}
	// 中文显示名必须按 RFC 2047 编码，不能裸 8bit 进头
	if strings.Contains(msg, "From: 晴辰云邮") {
		t.Fatalf("display name was not encoded:\n%s", msg)
	}
}

func TestSendSMTPWithPlainAuth(t *testing.T) {
	srv := startFakeSMTP(t, "smtpuser")
	defer srv.Close()

	err := NewHTTPMailSender().Send(context.Background(), &MailConfig{
		Provider: MailProviderSMTP,
		Host:     srv.host(),
		Port:     srv.port(),
		Security: MailSecurityNone,
		Username: "smtpuser",
		Password: "smtppass",
		From:     "noreply@example.com",
	}, &MailMessage{To: []string{"user@example.com"}, Subject: "s", HTML: "b"})
	if err != nil {
		t.Fatalf("send: %v", err)
	}
	user, pass, ok := srv.credentials()
	if !ok || user != "smtpuser" || pass != "smtppass" {
		t.Fatalf("auth = %q/%q ok=%v", user, pass, ok)
	}
}

// 服务端不提供 STARTTLS 时，配置 starttls 必须报错而不是明文降级。
func TestSendSMTPStartTLSUnsupportedFails(t *testing.T) {
	srv := startFakeSMTP(t, "")
	defer srv.Close()

	err := NewHTTPMailSender().Send(context.Background(), &MailConfig{
		Provider: MailProviderSMTP,
		Host:     srv.host(),
		Port:     srv.port(),
		Security: MailSecurityStartTLS,
		From:     "noreply@example.com",
	}, &MailMessage{To: []string{"user@example.com"}, Subject: "s", HTML: "b"})
	if !errors.Is(err, ErrMailConfigInvalid) {
		t.Fatalf("err = %v, want ErrMailConfigInvalid (绝不能明文降级)", err)
	}
}

func TestSendSMTPUnreachable(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().(*net.TCPAddr)
	_ = ln.Close()

	err = NewHTTPMailSender().Send(context.Background(), &MailConfig{
		Provider: MailProviderSMTP, Host: addr.IP.String(), Port: addr.Port,
		Security: MailSecurityNone, From: "noreply@example.com", TimeoutSeconds: 2,
	}, &MailMessage{To: []string{"user@example.com"}, Subject: "s", HTML: "b"})
	if !errors.Is(err, ErrMailUnreachable) {
		t.Fatalf("err = %v, want ErrMailUnreachable", err)
	}
}

// ── MIME 组装 ───────────────────────────────────────────

func TestBuildMIMEMessageWithAttachment(t *testing.T) {
	raw, err := buildMIMEMessage(&MailConfig{FromName: "平台"}, &MailMessage{
		To:      []string{"user@example.com"},
		Subject: "发票通知",
		HTML:    "<p>请查收附件</p>",
		Attachments: []MailAttachment{{
			Filename: "账单.pdf",
			Content:  []byte("PDFDATA"),
		}},
	}, "noreply@example.com")
	if err != nil {
		t.Fatalf("buildMIMEMessage: %v", err)
	}
	msg := string(raw)
	if !strings.Contains(msg, "multipart/mixed") {
		t.Fatalf("expected multipart/mixed:\n%s", msg)
	}
	if !strings.Contains(msg, "multipart/alternative") {
		t.Fatalf("expected nested alternative body:\n%s", msg)
	}
	if !strings.Contains(msg, "filename*=UTF-8''") {
		t.Fatalf("非 ASCII 附件名应以 RFC 2231 编码:\n%s", msg)
	}
	if !strings.Contains(msg, base64.StdEncoding.EncodeToString([]byte("PDFDATA"))) {
		t.Fatalf("attachment content not base64-inlined:\n%s", msg)
	}
}

func TestBuildMIMEMessageRejectsEmptyTemplate(t *testing.T) {
	_, err := buildMIMEMessage(&MailConfig{}, &MailMessage{To: []string{"u@example.com"}}, "a@b.com")
	if !errors.Is(err, ErrMailConfigInvalid) {
		t.Fatalf("err = %v, want ErrMailConfigInvalid", err)
	}
}

// ── 模板渲染 ────────────────────────────────────────────

func TestRenderMailTemplate(t *testing.T) {
	subject, body, err := RenderMailTemplate(DefaultMailCodeSubject, DefaultMailCodeBody, map[string]string{
		MailVarCode:       "8848",
		MailVarSiteName:   "晴辰云邮",
		MailVarTTLMinutes: "5",
		MailVarAction:     "<b>登录</b>",
	})
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	if !strings.Contains(body, "8848") {
		t.Fatalf("code missing:\n%s", body)
	}
	// 变量按 HTML 上下文转义：动作描述里的标签不能变成真标签
	if !strings.Contains(body, "&lt;b&gt;登录&lt;/b&gt;") {
		t.Fatalf("variable was not HTML-escaped:\n%s", body)
	}
	if !strings.Contains(subject, "晴辰云邮") {
		t.Fatalf("subject = %q", subject)
	}
}

func TestRenderMailTemplateUnknownVarRendersEmpty(t *testing.T) {
	_, body, err := RenderMailTemplate("s", "验证码：{{.NotDefined}}|", nil)
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	if body != "验证码：|" {
		t.Fatalf("body = %q, want 验证码：|", body)
	}
}

func TestValidateMailTemplateRejectsBrokenSyntax(t *testing.T) {
	if err := ValidateMailTemplate("{{.Code"); err == nil {
		t.Fatal("broken template must be rejected")
	}
	if err := ValidateMailTemplate(""); err != nil {
		t.Fatalf("empty template should be allowed: %v", err)
	}
}

// ── 校验与工具 ──────────────────────────────────────────

func TestValidateMailAddress(t *testing.T) {
	valid := []string{"a@b.com", "user.name+tag@sub.example.com", "USER@example.com"}
	for _, addr := range valid {
		if !ValidateMailAddress(addr) {
			t.Errorf("%q should be valid", addr)
		}
	}
	invalid := []string{"", "a@b", "a@", "@b.com", "a b@c.com", "a@b.com\nBcc: x@y.com", "Name <a@b.com>"}
	for _, addr := range invalid {
		if ValidateMailAddress(addr) {
			t.Errorf("%q should be invalid", addr)
		}
	}
}

func TestParseRetryAfter(t *testing.T) {
	if d := parseRetryAfter("30"); d != 30*time.Second {
		t.Fatalf("numeric Retry-After = %v", d)
	}
	if d := parseRetryAfter("garbage"); d != 0 {
		t.Fatalf("garbage Retry-After = %v", d)
	}
	if d := parseRetryAfter(""); d != 0 {
		t.Fatalf("empty Retry-After = %v", d)
	}
}

func TestBackoffDelayCappedAtMax(t *testing.T) {
	// 无 Retry-After 时指数增长，且不超过上限
	if d := backoffDelay(0, 0); d != mailHTTPBaseBackoff {
		t.Fatalf("attempt0 = %v", d)
	}
	if d := backoffDelay(10, 0); d != mailHTTPMaxBackoff {
		t.Fatalf("attempt10 = %v, want capped %v", d, mailHTTPMaxBackoff)
	}
	// 服务端给的 Retry-After 同样受上限约束
	if d := backoffDelay(0, 10*time.Minute); d != mailHTTPMaxBackoff {
		t.Fatalf("huge Retry-After = %v, want %v", d, mailHTTPMaxBackoff)
	}
}

func TestIsKnownMailProviderAndSecurity(t *testing.T) {
	if !IsKnownMailProvider(MailProviderSMTP) || !IsKnownMailProvider(MailProviderQingchen) {
		t.Fatal("known providers rejected")
	}
	if IsKnownMailProvider("sendgrid") {
		t.Fatal("unknown provider accepted")
	}
	for _, m := range []string{MailSecurityNone, MailSecurityStartTLS, MailSecuritySSL} {
		if !IsKnownMailSecurity(m) {
			t.Fatalf("%q rejected", m)
		}
	}
	if IsKnownMailSecurity("sslv3") {
		t.Fatal("unknown security mode accepted")
	}
}

func shortBackoff(t *testing.T) func() {
	t.Helper()
	prev := mailHTTPBackoffBase
	mailHTTPBackoffBase = time.Millisecond
	return func() { mailHTTPBackoffBase = prev }
}

// ── 假 SMTP 服务端 ──────────────────────────────────────

// fakeSMTPServer 是一个最小 SMTP 服务端（明文），用于验证"MIME 报文被正确投递"
// 这条链路，而不引入任何外部依赖。它声明 AUTH PLAIN/LOGIN，但不声明 STARTTLS
// （供"starttls 未提供必须报错"的失败路径复用）。
type fakeSMTPMsg struct {
	from  string
	rcpts []string
	data  []byte
	user  string
	pass  string
	auth  bool
}

type fakeSMTPServer struct {
	ln  net.Listener
	wg  sync.WaitGroup
	mu  sync.Mutex
	msg fakeSMTPMsg
}

func startFakeSMTP(t *testing.T, requireUser string) *fakeSMTPServer {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	s := &fakeSMTPServer{ln: ln}
	s.wg.Add(1)
	go func() {
		defer s.wg.Done()
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			s.serve(conn, requireUser)
			_ = conn.Close()
		}
	}()
	return s
}

func (s *fakeSMTPServer) host() string {
	return s.ln.Addr().(*net.TCPAddr).IP.String()
}

func (s *fakeSMTPServer) port() int {
	return s.ln.Addr().(*net.TCPAddr).Port
}

func (s *fakeSMTPServer) Close() {
	_ = s.ln.Close()
	s.wg.Wait()
}

func (s *fakeSMTPServer) captured() (string, []string, []byte) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.msg.from, s.msg.rcpts, s.msg.data
}

func (s *fakeSMTPServer) credentials() (string, string, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.msg.user, s.msg.pass, s.msg.auth
}

func (s *fakeSMTPServer) serve(conn net.Conn, requireUser string) {
	br := bufio.NewReader(conn)
	bw := bufio.NewWriter(conn)
	write := func(line string) {
		_, _ = bw.WriteString(line + "\r\n")
		_ = bw.Flush()
	}
	write("220 fake ESMTP ready")

	for {
		line, err := br.ReadString('\n')
		if err != nil {
			return
		}
		line = strings.TrimRight(line, "\r\n")
		upper := strings.ToUpper(line)

		switch {
		case strings.HasPrefix(upper, "EHLO"), strings.HasPrefix(upper, "HELO"):
			write("250-fake")
			write("250-AUTH PLAIN LOGIN")
			write("250 OK")
		case strings.HasPrefix(upper, "AUTH PLAIN"):
			parts := strings.Fields(line)
			user, pass := decodePlainAuth(parts)
			s.recordAuth(user, pass, requireUser, write)
		case strings.HasPrefix(upper, "AUTH LOGIN"):
			write("334 " + base64.StdEncoding.EncodeToString([]byte("Username:")))
			u, _ := br.ReadString('\n')
			write("334 " + base64.StdEncoding.EncodeToString([]byte("Password:")))
			p, _ := br.ReadString('\n')
			user, _ := base64.StdEncoding.DecodeString(strings.TrimSpace(u))
			pass, _ := base64.StdEncoding.DecodeString(strings.TrimSpace(p))
			s.recordAuth(string(user), string(pass), requireUser, write)
		case strings.HasPrefix(upper, "MAIL FROM:"):
			s.mu.Lock()
			s.msg.from = extractAddress(line)
			s.mu.Unlock()
			write("250 OK")
		case strings.HasPrefix(upper, "RCPT TO:"):
			s.mu.Lock()
			s.msg.rcpts = append(s.msg.rcpts, extractAddress(line))
			s.mu.Unlock()
			write("250 OK")
		case upper == "DATA":
			write("354 End data with <CR><LF>.<CR><LF>")
			var body strings.Builder
			for {
				dl, err := br.ReadString('\n')
				if err != nil {
					return
				}
				if dl == ".\r\n" || dl == ".\n" {
					break
				}
				body.WriteString(dl)
			}
			s.mu.Lock()
			s.msg.data = []byte(body.String())
			s.mu.Unlock()
			write("250 OK: queued")
		case upper == "QUIT":
			write("221 Bye")
			return
		case upper == "RSET":
			write("250 OK")
		default:
			write("250 OK")
		}
	}
}

func (s *fakeSMTPServer) recordAuth(user, pass, requireUser string, write func(string)) {
	if requireUser != "" && user != requireUser {
		write("535 authentication failed")
		return
	}
	s.mu.Lock()
	s.msg.user, s.msg.pass, s.msg.auth = user, pass, true
	s.mu.Unlock()
	write("235 Authentication successful")
}

// decodePlainAuth 解出 AUTH PLAIN 的凭据（支持内联与分步两种形态）。
func decodePlainAuth(parts []string) (string, string) {
	if len(parts) < 3 {
		return "", ""
	}
	raw, err := base64.StdEncoding.DecodeString(parts[2])
	if err != nil {
		return "", ""
	}
	fields := strings.Split(string(raw), "\x00")
	if len(fields) != 3 {
		return "", ""
	}
	return fields[1], fields[2]
}

func extractAddress(line string) string {
	start := strings.Index(line, "<")
	end := strings.LastIndex(line, ">")
	if start >= 0 && end > start {
		return line[start+1 : end]
	}
	return strings.TrimSpace(line[strings.Index(line, ":")+1:])
}
