package auth

// ── 邮件发送（MAIL）────────────────────────────────────
//
// 支持两类发信通道，均为"服务端可配置、代码零硬编码"：
//
//	smtp     通用 SMTP（host / port / 用户名 / 密码 / TLS 模式 / 发件地址）
//	         覆盖市面绝大多数邮件服务商与自建邮件网关。
//	qingchen 晴辰云邮 HTTP API（契约见 docs/mail.md）
//
// 设计要点：
//   - 通道以 MailSender 抽象，按 cfg.Provider 分派；新增厂商只需加一个分支。
//   - 服务器地址（SMTP host:port、HTTP base_url）**全部来自配置**：代码里没有
//     任何厂商默认域名，后台改配置即切换发信服务，无需重新构建。
//   - SMTP 不引入任何第三方 SDK：标准库 net/smtp + crypto/tls，
//     支持 none（明文/内网中继）、starttls（587）、ssl（465）三种安全模式。
//   - HTTP 通道严格按 docs/mail.md：202 = 入队成功；429 指数退避重试（上限 60s）；
//     附件大小上限 10MB；本地文件路径自动读盘转 Base64。

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/tls"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"mime"
	"mime/multipart"
	"net"
	"net/http"
	"net/mail"
	"net/smtp"
	"net/textproto"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// 邮件通道类型。
const (
	MailProviderSMTP     = "smtp"     // 通用 SMTP
	MailProviderQingchen = "qingchen" // 晴辰云邮（HTTP API，docs/mail.md）
)

// SMTP 安全模式。
const (
	MailSecurityNone     = "none"     // 明文（或由内网中继自行加密）
	MailSecurityStartTLS = "starttls" // 明文握手后升级 TLS（通常 587）
	MailSecuritySSL      = "ssl"      // 直连 TLS（通常 465）
)

// MaxMailAttachmentBytes 单附件上限。与 docs/mail.md 的服务端限制保持一致，
// 在本地先拦一次，避免把 10MB+ 的请求白发给服务商。
const MaxMailAttachmentBytes = 10 << 20

// 晴辰云邮通道的退避重试参数（docs/mail.md 第九节：指数退避，最大间隔 60 秒）。
const (
	mailHTTPMaxRetries  = 3
	mailHTTPBaseBackoff = 1 * time.Second
	mailHTTPMaxBackoff  = 60 * time.Second
	mailHTTPDefaultTTL  = 30 * time.Second // 文档建议的网络超时
)

// mailHTTPBackoffBase 是首次重试前的等待时长。包级变量以便单测把等待压到毫秒级，
// 生产值恒为 mailHTTPBaseBackoff。
var mailHTTPBackoffBase = mailHTTPBaseBackoff

// MailKnownProviders 返回全部受支持的邮件通道（配置校验用）。
func MailKnownProviders() []string {
	return []string{MailProviderSMTP, MailProviderQingchen}
}

// IsKnownMailProvider 判断通道类型是否受支持。
func IsKnownMailProvider(p string) bool {
	switch p {
	case MailProviderSMTP, MailProviderQingchen:
		return true
	}
	return false
}

// IsKnownMailSecurity 判断 SMTP 安全模式是否受支持。
func IsKnownMailSecurity(m string) bool {
	switch m {
	case MailSecurityNone, MailSecurityStartTLS, MailSecuritySSL:
		return true
	}
	return false
}

// MailAttachment 是一个附件。
//
// 内容来源二选一：
//   - Content：直接给出字节（调用方自取）
//   - FilePath：本地文件路径，发送前自动读盘并转 Base64（docs/mail.md 要求 5）
//
// 注意：FilePath 只能来自服务端自身的配置/代码，**绝不可**由请求参数拼出，
// 否则等于开放任意文件读取。
type MailAttachment struct {
	Filename    string
	ContentType string
	Content     []byte
	FilePath    string
}

// MailMessage 是一封待发邮件。
type MailMessage struct {
	To      []string
	Subject string
	HTML    string
	Text    string
	From    string // 空则用配置里的默认发件地址
	ReplyTo string

	// 晴辰云邮模板通道：TemplateID > 0 时由服务端模板决定内容，
	// Subject/HTML 可省略，Variables 用于替换模板里的 {{.key}} 占位符。
	TemplateID int
	Variables  map[string]string
	ChannelID  int

	Attachments []MailAttachment
}

// MailConfig 是一次发送所需的完整配置（secret 已解密）。
type MailConfig struct {
	Provider string

	// SMTP
	Host               string
	Port               int
	Username           string
	Password           string
	Security           string // none / starttls / ssl
	InsecureSkipVerify bool

	// 晴辰云邮（HTTP）
	BaseURL string
	APIKey  string

	// 通用
	From           string
	FromName       string
	ReplyTo        string
	TimeoutSeconds int
}

// 邮件通道错误分类：业务层据此回不同的状态码/文案。
var (
	ErrMailConfigInvalid = errors.New("mail: invalid configuration")
	ErrMailSendFailed    = errors.New("mail provider rejected the request")
	ErrMailUnreachable   = errors.New("mail provider unreachable")
)

// MailSender 抽象邮件发送，测试可替换。
type MailSender interface {
	Send(ctx context.Context, cfg *MailConfig, msg *MailMessage) error
}

// HTTPMailSender 是真实实现：SMTP 走 net/smtp，晴辰云邮走 HTTP。
type HTTPMailSender struct {
	client *http.Client
}

// NewHTTPMailSender 构造发送器（HTTP 通道默认 30s 超时，见 docs/mail.md 第八节）。
func NewHTTPMailSender() *HTTPMailSender {
	return &HTTPMailSender{
		client: &http.Client{Timeout: mailHTTPDefaultTTL},
	}
}

// Send 按通道分派发送。
func (s *HTTPMailSender) Send(ctx context.Context, cfg *MailConfig, msg *MailMessage) error {
	if cfg == nil {
		return fmt.Errorf("%w: nil config", ErrMailConfigInvalid)
	}
	if msg == nil {
		return fmt.Errorf("%w: nil message", ErrMailConfigInvalid)
	}
	if !IsKnownMailProvider(cfg.Provider) {
		return fmt.Errorf("%w: unknown mail provider %q", ErrMailConfigInvalid, cfg.Provider)
	}
	if len(msg.To) == 0 {
		return fmt.Errorf("%w: recipient is required", ErrMailConfigInvalid)
	}
	for _, to := range msg.To {
		if !ValidateMailAddress(to) {
			return fmt.Errorf("%w: invalid recipient %q", ErrMailConfigInvalid, to)
		}
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	switch cfg.Provider {
	case MailProviderSMTP:
		return s.sendSMTP(ctx, cfg, msg)
	default:
		return s.sendQingchen(ctx, cfg, msg)
	}
}

// ── SMTP 通道 ───────────────────────────────────────────

// smtpTimeout 返回本次连接/读写超时。
func (s *HTTPMailSender) smtpTimeout(cfg *MailConfig) time.Duration {
	if cfg.TimeoutSeconds > 0 {
		return time.Duration(cfg.TimeoutSeconds) * time.Second
	}
	return mailHTTPDefaultTTL
}

func (s *HTTPMailSender) sendSMTP(ctx context.Context, cfg *MailConfig, msg *MailMessage) error {
	if cfg.Host == "" {
		return fmt.Errorf("%w: smtp host is required", ErrMailConfigInvalid)
	}
	port := cfg.Port
	if port == 0 {
		switch cfg.Security {
		case MailSecuritySSL:
			port = 465
		default:
			port = 587
		}
	}
	if port < 1 || port > 65535 {
		return fmt.Errorf("%w: invalid smtp port %d", ErrMailConfigInvalid, port)
	}
	from := cfg.From
	if from == "" {
		from = cfg.Username
	}
	if !ValidateMailAddress(from) {
		return fmt.Errorf("%w: invalid sender address", ErrMailConfigInvalid)
	}
	raw, err := buildMIMEMessage(cfg, msg, from)
	if err != nil {
		return err
	}

	addr := net.JoinHostPort(cfg.Host, strconv.Itoa(port))
	tlsCfg := &tls.Config{
		ServerName:         cfg.Host,
		InsecureSkipVerify: cfg.InsecureSkipVerify, //nolint:gosec // 仅在内网自签证书场景由管理员显式开启
		MinVersion:         tls.VersionTLS12,
	}
	timeout := s.smtpTimeout(cfg)
	dialer := &net.Dialer{Timeout: timeout}
	var conn net.Conn
	if cfg.Security == MailSecuritySSL {
		conn, err = tls.DialWithDialer(dialer, "tcp", addr, tlsCfg)
	} else {
		conn, err = dialer.DialContext(ctx, "tcp", addr)
	}
	if err != nil {
		return fmt.Errorf("%w: smtp dial %s: %v", ErrMailUnreachable, addr, err)
	}
	defer conn.Close()
	// net/smtp 不感知 context，用连接级 deadline 保证有界等待。
	_ = conn.SetDeadline(time.Now().Add(timeout))

	c, err := smtp.NewClient(conn, cfg.Host)
	if err != nil {
		return fmt.Errorf("%w: smtp handshake: %v", ErrMailUnreachable, err)
	}
	defer c.Close()

	if cfg.Security == MailSecurityStartTLS {
		if ok, _ := c.Extension("STARTTLS"); !ok {
			return fmt.Errorf("%w: smtp server does not offer STARTTLS", ErrMailConfigInvalid)
		}
		if err := c.StartTLS(tlsCfg); err != nil {
			return fmt.Errorf("%w: smtp starttls: %v", ErrMailUnreachable, err)
		}
		_ = conn.SetDeadline(time.Now().Add(timeout))
	}

	if cfg.Username != "" {
		if cfg.Security == MailSecurityNone {
			// 显式配置的明文认证（内网中继常见）：放行但必须留下痕迹。
			slog.Warn("smtp: sending credentials over an unencrypted connection",
				"host", cfg.Host, "port", port)
		}
		if err := c.Auth(smtpAuth(c, cfg)); err != nil {
			return fmt.Errorf("%w: smtp auth: %v", ErrMailSendFailed, err)
		}
	}

	if err := c.Mail(from); err != nil {
		return fmt.Errorf("%w: smtp MAIL FROM: %v", ErrMailSendFailed, err)
	}
	for _, to := range msg.To {
		if err := c.Rcpt(to); err != nil {
			return fmt.Errorf("%w: smtp RCPT TO %s: %v", ErrMailSendFailed, to, err)
		}
	}
	w, err := c.Data()
	if err != nil {
		return fmt.Errorf("%w: smtp DATA: %v", ErrMailSendFailed, err)
	}
	if _, err := w.Write(raw); err != nil {
		_ = w.Close()
		return fmt.Errorf("%w: smtp write body: %v", ErrMailUnreachable, err)
	}
	if err := w.Close(); err != nil {
		return fmt.Errorf("%w: smtp body rejected: %v", ErrMailSendFailed, err)
	}
	if err := c.Quit(); err != nil {
		return fmt.Errorf("%w: smtp quit: %v", ErrMailUnreachable, err)
	}
	return nil
}

// smtpAuth 选择认证机制：服务端声明支持时用 PLAIN，否则退回 LOGIN。
// 自实现（而非 net/smtp 的 PlainAuth）：后者在非 TLS 连接上直接拒绝，
// 会让"内网明文中继 + 认证"这种常见部署无法配置。
func smtpAuth(c *smtp.Client, cfg *MailConfig) smtp.Auth {
	if ok, params := c.Extension("AUTH"); ok {
		mechs := strings.ToUpper(params)
		if !strings.Contains(mechs, "PLAIN") && strings.Contains(mechs, "LOGIN") {
			return &loginAuth{username: cfg.Username, password: cfg.Password}
		}
	}
	return &plainAuth{username: cfg.Username, password: cfg.Password}
}

// plainAuth 实现 RFC 4616 SASL PLAIN。
type plainAuth struct {
	username, password string
}

func (a *plainAuth) Start(server *smtp.ServerInfo) (string, []byte, error) {
	resp := []byte("\x00" + a.username + "\x00" + a.password)
	return "PLAIN", resp, nil
}

func (a *plainAuth) Next(_ []byte, more bool) ([]byte, error) {
	if more {
		return nil, errors.New("mail: unexpected server challenge for PLAIN auth")
	}
	return nil, nil
}

// loginAuth 实现广泛使用的非标准 SASL LOGIN。
type loginAuth struct {
	username, password string
}

func (a *loginAuth) Start(*smtp.ServerInfo) (string, []byte, error) {
	return "LOGIN", []byte(a.username), nil
}

func (a *loginAuth) Next(fromServer []byte, more bool) ([]byte, error) {
	if !more {
		return nil, nil
	}
	// 服务端依次发 "Username:" / "Password:" 挑战；一律回下一段凭据。
	if strings.Contains(strings.ToLower(strings.TrimSpace(string(fromServer))), "username") {
		return []byte(a.username), nil
	}
	return []byte(a.password), nil
}

// ── MIME 组装 ───────────────────────────────────────────

// buildMIMEMessage 组装完整 RFC 5322 报文（含附件）。
// 结构：无附件 → text/html|text/plain 或 multipart/alternative；
//
//	有附件 → multipart/mixed[ multipart/alternative[...], attachment... ]
func buildMIMEMessage(cfg *MailConfig, msg *MailMessage, from string) ([]byte, error) {
	var buf bytes.Buffer
	name, addr := parseMailbox(from)
	display := cfg.FromName
	if display == "" {
		display = name
	}
	writeAddrHeader(&buf, "From", display, addr)
	for _, to := range msg.To {
		writeAddrHeader(&buf, "To", "", to)
	}
	replyTo := msg.ReplyTo
	if replyTo == "" {
		replyTo = cfg.ReplyTo
	}
	if replyTo != "" {
		writeAddrHeader(&buf, "Reply-To", "", replyTo)
	}
	buf.WriteString("Subject: " + encodeHeader(msg.Subject) + "\r\n")
	buf.WriteString("Date: " + time.Now().Format(time.RFC1123Z) + "\r\n")
	buf.WriteString("Message-ID: " + newMessageID(addr) + "\r\n")
	buf.WriteString("MIME-Version: 1.0\r\n")

	hasBody := msg.HTML != "" || msg.Text != ""
	if len(msg.Attachments) == 0 {
		if !hasBody {
			return nil, fmt.Errorf("%w: subject/body template produced an empty message", ErrMailConfigInvalid)
		}
		if msg.HTML != "" && msg.Text != "" {
			if err := writeAlternativeBody(&buf, msg); err != nil {
				return nil, err
			}
		} else if msg.HTML != "" {
			buf.WriteString("Content-Type: text/html; charset=utf-8\r\n")
			buf.WriteString("Content-Transfer-Encoding: base64\r\n\r\n")
			writeBase64Lines(&buf, []byte(msg.HTML))
		} else {
			buf.WriteString("Content-Type: text/plain; charset=utf-8\r\n")
			buf.WriteString("Content-Transfer-Encoding: base64\r\n\r\n")
			writeBase64Lines(&buf, []byte(msg.Text))
		}
		return buf.Bytes(), nil
	}

	// multipart/mixed：正文 + 附件
	mw := multipart.NewWriter(&buf)
	buf.WriteString("Content-Type: multipart/mixed; boundary=" + strconv.Quote(mw.Boundary()) + "\r\n\r\n")
	if err := writeBodyPart(mw, msg); err != nil {
		return nil, err
	}
	for _, att := range msg.Attachments {
		if err := writeAttachmentPart(mw, att); err != nil {
			return nil, err
		}
	}
	if err := mw.Close(); err != nil {
		return nil, fmt.Errorf("mail: finalize mime: %w", err)
	}
	return buf.Bytes(), nil
}

// writeAlternativeBody 写出 multipart/alternative（plain + html）。
func writeAlternativeBody(buf *bytes.Buffer, msg *MailMessage) error {
	var inner bytes.Buffer
	aw := multipart.NewWriter(&inner)
	if err := writeAlternativeParts(aw, msg); err != nil {
		return err
	}
	if err := aw.Close(); err != nil {
		return fmt.Errorf("mail: finalize alternative body: %w", err)
	}
	buf.WriteString("Content-Type: multipart/alternative; boundary=" + strconv.Quote(aw.Boundary()) + "\r\n\r\n")
	buf.Write(inner.Bytes())
	return nil
}

func writeAlternativeParts(w *multipart.Writer, msg *MailMessage) error {
	text := msg.Text
	if text == "" {
		text = htmlToPlainText(msg.HTML)
	}
	if err := writeTextPart(w, "text/plain", text); err != nil {
		return err
	}
	if msg.HTML != "" {
		if err := writeTextPart(w, "text/html", msg.HTML); err != nil {
			return err
		}
	}
	return nil
}

// writeBodyPart 在 multipart/mixed 中写正文（一个 alternative 子部件）。
func writeBodyPart(w *multipart.Writer, msg *MailMessage) error {
	var inner bytes.Buffer
	aw := multipart.NewWriter(&inner)
	if err := writeAlternativeParts(aw, msg); err != nil {
		return err
	}
	if err := aw.Close(); err != nil {
		return fmt.Errorf("mail: finalize alternative body: %w", err)
	}
	h := textproto.MIMEHeader{}
	h.Set("Content-Type", "multipart/alternative; boundary="+strconv.Quote(aw.Boundary()))
	pw, err := w.CreatePart(h)
	if err != nil {
		return fmt.Errorf("mail: create body part: %w", err)
	}
	if _, err := pw.Write(inner.Bytes()); err != nil {
		return fmt.Errorf("mail: write body part: %w", err)
	}
	return nil
}

func writeTextPart(w *multipart.Writer, contentType, body string) error {
	h := textproto.MIMEHeader{}
	h.Set("Content-Type", contentType+"; charset=utf-8")
	h.Set("Content-Transfer-Encoding", "base64")
	pw, err := w.CreatePart(h)
	if err != nil {
		return fmt.Errorf("mail: create %s part: %w", contentType, err)
	}
	writeBase64Lines(pw, []byte(body))
	return nil
}

// writeAttachmentPart 写一个附件部件；Content 为空时按 FilePath 读盘。
func writeAttachmentPart(w *multipart.Writer, att MailAttachment) error {
	content, err := att.resolve()
	if err != nil {
		return err
	}
	h := textproto.MIMEHeader{}
	ct := att.ContentType
	if ct == "" {
		if ext := strings.ToLower(filepath.Ext(att.Filename)); ext != "" {
			ct = mime.TypeByExtension(ext)
		}
	}
	if ct == "" {
		ct = "application/octet-stream"
	}
	h.Set("Content-Type", ct)
	h.Set("Content-Disposition", contentDisposition(att.Filename))
	h.Set("Content-Transfer-Encoding", "base64")
	pw, err := w.CreatePart(h)
	if err != nil {
		return fmt.Errorf("mail: create attachment part: %w", err)
	}
	writeBase64Lines(pw, content)
	return nil
}

// resolve 解析附件内容：优先 Content，其次 FilePath（本地读盘，10MB 上限）。
func (a MailAttachment) resolve() ([]byte, error) {
	if len(a.Content) > 0 {
		if len(a.Content) > MaxMailAttachmentBytes {
			return nil, fmt.Errorf("%w: attachment %s exceeds limit (10MB)",
				ErrMailConfigInvalid, a.Filename)
		}
		return a.Content, nil
	}
	if a.FilePath == "" {
		return nil, fmt.Errorf("%w: attachment %s has neither content nor path",
			ErrMailConfigInvalid, a.Filename)
	}
	info, err := os.Stat(a.FilePath)
	if err != nil {
		return nil, fmt.Errorf("%w: attachment %s: %v", ErrMailConfigInvalid, a.Filename, err)
	}
	if info.Size() > MaxMailAttachmentBytes {
		return nil, fmt.Errorf("%w: attachment %s exceeds limit (10MB)", ErrMailConfigInvalid, a.Filename)
	}
	data, err := os.ReadFile(a.FilePath)
	if err != nil {
		return nil, fmt.Errorf("%w: attachment %s: %v", ErrMailConfigInvalid, a.Filename, err)
	}
	return data, nil
}

// contentDisposition 生成附件的 Content-Disposition 值：
// 纯 ASCII 文件名用 filename=，含非 ASCII 时补 RFC 2231 的 filename*=。
func contentDisposition(filename string) string {
	if filename == "" {
		return "attachment"
	}
	if isASCII(filename) {
		return `attachment; filename="` + strings.ReplaceAll(filename, `"`, "") + `"`
	}
	fallback := strings.Map(func(r rune) rune {
		if r < 32 || r > 126 || r == '"' || r == '\\' {
			return '_'
		}
		return r
	}, filename)
	return `attachment; filename="` + fallback + `"; filename*=UTF-8''` + url.PathEscape(filename)
}

// writeAddrHeader 写出地址头：有显示名时用 "显示名 <addr>"，显示名非 ASCII 时按 RFC 2047 编码。
func writeAddrHeader(buf *bytes.Buffer, key, display, addr string) {
	if display == "" {
		buf.WriteString(key + ": " + addr + "\r\n")
		return
	}
	buf.WriteString(key + ": " + encodeHeader(display) + " <" + addr + ">\r\n")
}

// parseMailbox 拆分 "显示名 <addr>" 或裸地址。
func parseMailbox(raw string) (name, addr string) {
	if a, err := mail.ParseAddress(raw); err == nil {
		return a.Name, a.Address
	}
	return "", strings.TrimSpace(raw)
}

// encodeHeader 对含非 ASCII 的头值做 RFC 2047 编码。
func encodeHeader(v string) string {
	if isASCII(v) {
		// 折叠掉头注入：换行符一律剔除（值可能来自后台配置）
		return strings.NewReplacer("\r", " ", "\n", " ").Replace(v)
	}
	return mime.QEncoding.Encode("UTF-8", strings.NewReplacer("\r", " ", "\n", " ").Replace(v))
}

func isASCII(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] < 32 || s[i] > 126 {
			return false
		}
	}
	return true
}

// writeBase64Lines 以 76 字符折行写出 Base64（RFC 2045 要求的行长）。
func writeBase64Lines(w io.Writer, data []byte) {
	enc := base64.StdEncoding.EncodeToString(data)
	for len(enc) > 76 {
		_, _ = io.WriteString(w, enc[:76]+"\r\n")
		enc = enc[76:]
	}
	if enc != "" {
		_, _ = io.WriteString(w, enc+"\r\n")
	}
}

// newMessageID 生成形如 <rand@domain> 的 Message-ID。
func newMessageID(from string) string {
	buf := make([]byte, 12)
	if _, err := rand.Read(buf); err != nil {
		return fmt.Sprintf("<%d@chiron>", time.Now().UnixNano())
	}
	domain := "chiron"
	if at := strings.LastIndex(from, "@"); at >= 0 && at+1 < len(from) {
		domain = from[at+1:]
	}
	return "<" + hex.EncodeToString(buf) + "@" + domain + ">"
}

// htmlToPlainText 是极简 HTML 去标签，仅用于 multipart/alternative 的纯文本兜底。
func htmlToPlainText(html string) string {
	var out strings.Builder
	depth := 0
	for _, r := range html {
		switch r {
		case '<':
			depth++
		case '>':
			if depth > 0 {
				depth--
			}
		default:
			if depth == 0 {
				out.WriteRune(r)
			}
		}
	}
	return strings.TrimSpace(out.String())
}

// ── 晴辰云邮（HTTP API，docs/mail.md）────────────────────

// qingchenSendRequest 是 POST {base_url}/send 的请求体。
type qingchenSendRequest struct {
	To          string               `json:"to"`
	Subject     string               `json:"subject,omitempty"`
	Body        string               `json:"body,omitempty"`
	From        string               `json:"from,omitempty"`
	TemplateID  int                  `json:"template_id,omitempty"`
	Variables   map[string]string    `json:"variables,omitempty"`
	ChannelID   int                  `json:"channel_id,omitempty"`
	Attachments []qingchenAttachment `json:"attachments,omitempty"`
}

// qingchenAttachment 是 attachment 对象：content / url 二选一。
type qingchenAttachment struct {
	Filename    string `json:"filename"`
	Content     string `json:"content,omitempty"`
	URL         string `json:"url,omitempty"`
	ContentType string `json:"content_type,omitempty"`
}

// sendQingchen 调用晴辰云邮发信接口；429 按指数退避重试（上限 60s）。
func (s *HTTPMailSender) sendQingchen(ctx context.Context, cfg *MailConfig, msg *MailMessage) error {
	if cfg.BaseURL == "" {
		return fmt.Errorf("%w: HTTP 通道未配置服务地址（base_url）", ErrMailConfigInvalid)
	}
	if cfg.APIKey == "" {
		return fmt.Errorf("%w: HTTP 通道未配置 API Key", ErrMailConfigInvalid)
	}
	if msg.Subject == "" && msg.HTML == "" && msg.TemplateID == 0 {
		return fmt.Errorf("%w: 邮件主题与正文不能同时为空（或改用 template_id）", ErrMailConfigInvalid)
	}
	atts, err := buildQingchenAttachments(msg.Attachments)
	if err != nil {
		return err
	}
	from := msg.From
	if from == "" {
		from = cfg.From
	}
	body := qingchenSendRequest{
		To:          strings.Join(msg.To, ","),
		Subject:     msg.Subject,
		Body:        msg.HTML,
		From:        from,
		TemplateID:  msg.TemplateID,
		Variables:   msg.Variables,
		ChannelID:   msg.ChannelID,
		Attachments: atts,
	}
	payload, err := json.Marshal(body)
	if err != nil {
		return fmt.Errorf("mail: marshal request: %w", err)
	}

	endpoint := strings.TrimRight(cfg.BaseURL, "/") + "/send"
	timeout := time.Duration(cfg.TimeoutSeconds) * time.Second
	if cfg.TimeoutSeconds <= 0 {
		timeout = mailHTTPDefaultTTL
	}

	for attempt := 0; ; attempt++ {
		status, data, retryAfter, err := s.postQingchen(ctx, endpoint, cfg.APIKey, payload, timeout)
		if err != nil {
			return err
		}
		switch {
		case status == http.StatusAccepted:
			// 202 = 已入队（异步发送），符合 docs/mail.md 契约
			return nil
		case status == http.StatusTooManyRequests:
			if attempt >= mailHTTPMaxRetries {
				return fmt.Errorf("%w: 请求频率超限且重试 %d 次后仍被拒绝",
					ErrMailSendFailed, mailHTTPMaxRetries)
			}
			delay := backoffDelay(attempt, retryAfter)
			slog.Warn("mail: rate limited, backing off",
				"attempt", attempt+1, "delay", delay.String())
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(delay):
			}
			continue
		default:
			return fmt.Errorf("%w: HTTP %d: %s", ErrMailSendFailed, status, mailErrorMessage(data))
		}
	}
}

// postQingchen 执行一次请求，返回状态码、响应体、Retry-After（若可解析）。
func (s *HTTPMailSender) postQingchen(
	ctx context.Context, endpoint, apiKey string, payload []byte, timeout time.Duration,
) (int, []byte, time.Duration, error) {
	reqCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodPost, endpoint, bytes.NewReader(payload))
	if err != nil {
		return 0, nil, 0, fmt.Errorf("%w: %v", ErrMailConfigInvalid, err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+apiKey)
	req.Header.Set("Accept", "application/json")

	resp, err := s.client.Do(req)
	if err != nil {
		return 0, nil, 0, fmt.Errorf("%w: %v", ErrMailUnreachable, err)
	}
	defer resp.Body.Close()
	// 上限 64KB：错误响应体不应拖垮网关
	data, err := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	if err != nil {
		return resp.StatusCode, nil, 0, fmt.Errorf("%w: %v", ErrMailUnreachable, err)
	}
	return resp.StatusCode, data, parseRetryAfter(resp.Header.Get("Retry-After")), nil
}

// mailErrorMessage 从 {"error": "..."} 里取错误描述，缺省回退到原始响应片段。
func mailErrorMessage(data []byte) string {
	var body struct {
		Error string `json:"error"`
	}
	if err := json.Unmarshal(data, &body); err == nil && body.Error != "" {
		return body.Error
	}
	msg := strings.TrimSpace(string(data))
	if len(msg) > 200 {
		msg = msg[:200]
	}
	if msg == "" {
		msg = "empty response"
	}
	return msg
}

// parseRetryAfter 解析 Retry-After 头（秒数或 HTTP 日期），无法解析返回 0。
func parseRetryAfter(raw string) time.Duration {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return 0
	}
	if secs, err := strconv.Atoi(raw); err == nil && secs >= 0 {
		return time.Duration(secs) * time.Second
	}
	if t, err := http.ParseTime(raw); err == nil {
		if d := time.Until(t); d > 0 {
			return d
		}
	}
	return 0
}

// backoffDelay 返回第 attempt 次（0 起）重试前的等待时长：指数退避，上限 60s；
// 服务端给出 Retry-After 时以其为准（同样受上限约束）。
func backoffDelay(attempt int, retryAfter time.Duration) time.Duration {
	d := retryAfter
	if d <= 0 {
		d = mailHTTPBackoffBase << uint(attempt)
	}
	if d > mailHTTPMaxBackoff {
		d = mailHTTPMaxBackoff
	}
	return d
}

// buildQingchenAttachments 把附件转成 API 形态：一律内联 Base64（本地文件自动读盘）。
func buildQingchenAttachments(atts []MailAttachment) ([]qingchenAttachment, error) {
	out := make([]qingchenAttachment, 0, len(atts))
	for _, att := range atts {
		content, err := att.resolve()
		if err != nil {
			return nil, err
		}
		name := att.Filename
		if name == "" {
			name = filepath.Base(att.FilePath)
		}
		if name == "" {
			return nil, fmt.Errorf("%w: attachment filename is required", ErrMailConfigInvalid)
		}
		out = append(out, qingchenAttachment{
			Filename:    name,
			Content:     base64.StdEncoding.EncodeToString(content),
			ContentType: att.ContentType,
		})
	}
	return out, nil
}

// ── 地址校验与附件工具 ──────────────────────────────────

// ValidateMailAddress 校验邮箱地址（RFC 5322 简化形式）。
func ValidateMailAddress(addr string) bool {
	addr = strings.TrimSpace(addr)
	if addr == "" || len(addr) > 254 || strings.ContainsAny(addr, "\r\n") {
		return false
	}
	parsed, err := mail.ParseAddress(addr)
	if err != nil {
		return false
	}
	// ParseAddress 会接受 "Name <a@b>"，此处只接受裸地址
	if parsed.Address != addr {
		return false
	}
	at := strings.LastIndex(addr, "@")
	if at <= 0 || at == len(addr)-1 {
		return false
	}
	domain := addr[at+1:]
	return strings.Contains(domain, ".") && !strings.HasPrefix(domain, ".") &&
		!strings.HasSuffix(domain, ".")
}

// NormalizeMailAddress 去空白并转小写域名字面（保留本地部分大小写）。
func NormalizeMailAddress(addr string) string {
	addr = strings.TrimSpace(addr)
	at := strings.LastIndex(addr, "@")
	if at <= 0 || at == len(addr)-1 {
		return addr
	}
	return addr[:at] + "@" + strings.ToLower(addr[at+1:])
}

// AttachmentFromFile 读取本地文件构造附件（docs/mail.md 要求 5：自动转 Base64）。
// 路径只能来自服务端自身的可信上下文，不得由外部请求参数拼出。
func AttachmentFromFile(path string) (MailAttachment, error) {
	info, err := os.Stat(path)
	if err != nil {
		return MailAttachment{}, fmt.Errorf("%w: %v", ErrMailConfigInvalid, err)
	}
	if info.IsDir() {
		return MailAttachment{}, fmt.Errorf("%w: %s is a directory", ErrMailConfigInvalid, path)
	}
	if info.Size() > MaxMailAttachmentBytes {
		return MailAttachment{}, fmt.Errorf("%w: attachment %s exceeds limit (10MB)",
			ErrMailConfigInvalid, filepath.Base(path))
	}
	return MailAttachment{Filename: filepath.Base(path), FilePath: path}, nil
}

// GenerateMailToken 生成 URL 安全的一次性令牌（用于邮箱验证/重置密码链接）。
func GenerateMailToken() (string, error) {
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(buf), nil
}
