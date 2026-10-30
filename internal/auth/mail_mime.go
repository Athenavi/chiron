package auth

import (
	"bytes"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"io"
	"mime"
	"mime/multipart"
	"net/mail"
	"net/textproto"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// mail_mime.go —— 邮件报文的 **MIME 组装**。
//
// 由 mail.go 按职责拆出（L3-4 批次 A，同包纯移动，逐字未改）。
//
// 为什么单独一个文件：MIME 组装是**纯函数**——给配置与消息，吐一个字节数组，
// 不触碰网络、不解密凭据、不读数据库。把它与 SMTP 发送、HTTP 通道、
// 地址规范化放在一起，会让「改一行编码逻辑」和「改一次发送超时」在同一个
// 900 行文件里互相干扰。这里的每个函数都能单独写表驱动测试。

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
