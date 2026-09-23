package auth

// 邮件模板渲染与内置默认模板。
//
// 模板语法与晴辰云邮一致：Go 模板占位符 {{.变量名}}（docs/mail.md 第五节）。
// 使用 html/template 而非 text/template：模板里的插值按 HTML 上下文自动转义，
// 于是"验证码/用户名里塞标签"不可能变成注入点；模板自身的 HTML 保持原样。

import (
	"bytes"
	"errors"
	"fmt"
	"html/template"
	"strings"
)

// 邮件模板可用的变量名（在后台「邮件配置」中直接写 {{.Code}} 这样引用）。
const (
	MailVarCode       = "Code"       // 验证码
	MailVarSiteName   = "SiteName"   // 站点名
	MailVarAction     = "Action"     // 动作描述（登录/注册/重置密码）
	MailVarTTLMinutes = "TTLMinutes" // 有效期（分钟）
	MailVarURL        = "URL"        // 操作链接
	MailVarEmail      = "Email"      // 收件人邮箱
	MailVarName       = "Name"       // 收件人昵称
	MailVarIP         = "IP"         // 请求来源 IP
)

// 内置默认模板。后台可整体覆盖，留空即回落到这里。
const (
	DefaultMailCodeSubject = "{{.SiteName}} 验证码"
	DefaultMailCodeBody    = `<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;font-size:14px;color:#333">
  <p>您好，</p>
  <p>您正在{{.Action}}，验证码为：</p>
  <p style="font-size:28px;font-weight:700;letter-spacing:4px;color:#111">{{.Code}}</p>
  <p>验证码 {{.TTLMinutes}} 分钟内有效，请勿泄露给他人。</p>
  <p>若非本人操作，请忽略本邮件。</p>
  <hr style="border:none;border-top:1px solid #eee;margin:20px 0">
  <p style="color:#999;font-size:12px">此邮件由 {{.SiteName}} 自动发送，请勿直接回复。</p>
</div>`

	DefaultMailWelcomeSubject = "欢迎加入 {{.SiteName}}"
	DefaultMailWelcomeBody    = `<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;font-size:14px;color:#333">
  <p>{{.Name}} 您好，</p>
  <p>您的账号 <b>{{.Email}}</b> 已创建成功，欢迎使用 {{.SiteName}}。</p>
  <p><a href="{{.URL}}" style="color:#4f46e5">立即开始使用</a></p>
  <hr style="border:none;border-top:1px solid #eee;margin:20px 0">
  <p style="color:#999;font-size:12px">此邮件由 {{.SiteName}} 自动发送，请勿直接回复。</p>
</div>`

	DefaultMailResetSubject = "{{.SiteName}} 密码重置"
	DefaultMailResetBody    = `<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;font-size:14px;color:#333">
  <p>您好，</p>
  <p>我们收到了针对账号 <b>{{.Email}}</b> 的密码重置请求。</p>
  <p><a href="{{.URL}}" style="color:#4f46e5">重置密码</a>（{{.TTLMinutes}} 分钟内有效，仅可使用一次）</p>
  <p>若非本人操作，请忽略本邮件，您的密码不会被修改。</p>
  <hr style="border:none;border-top:1px solid #eee;margin:20px 0">
  <p style="color:#999;font-size:12px">此邮件由 {{.SiteName}} 自动发送，请勿直接回复。</p>
</div>`

	// 邮箱验证码校验通过的注册流程里，"邮箱验证"复用默认验证码模板。
	DefaultMailVerifySubject = "{{.SiteName}} 邮箱验证"
)

// ErrMailTemplateInvalid 模板语法错误（后台保存时应立即拒绝）。
var ErrMailTemplateInvalid = errors.New("mail: invalid template")

// RenderMailTemplate 用 vars 渲染主题与正文；模板为空时由调用方先行回落到默认值。
func RenderMailTemplate(subjectTpl, bodyTpl string, vars map[string]string) (subject, body string, err error) {
	subject, err = renderMailPiece("subject", subjectTpl, vars)
	if err != nil {
		return "", "", err
	}
	// 主题是邮件头，必须单行，否则可被用于头注入。
	subject = strings.NewReplacer("\r", " ", "\n", " ").Replace(strings.TrimSpace(subject))
	body, err = renderMailPiece("body", bodyTpl, vars)
	if err != nil {
		return "", "", err
	}
	return subject, body, nil
}

func renderMailPiece(name, tpl string, vars map[string]string) (string, error) {
	t, err := template.New(name).Option("missingkey=zero").Parse(tpl)
	if err != nil {
		return "", fmt.Errorf("%w: %s: %v", ErrMailTemplateInvalid, name, err)
	}
	data := make(map[string]string, len(vars))
	for k, v := range vars {
		data[k] = v
	}
	var buf bytes.Buffer
	if err := t.Execute(&buf, data); err != nil {
		return "", fmt.Errorf("%w: %s: %v", ErrMailTemplateInvalid, name, err)
	}
	return buf.String(), nil
}

// ValidateMailTemplate 校验模板语法（后台保存时前置调用，fail-loud）。
func ValidateMailTemplate(tpl string) error {
	if strings.TrimSpace(tpl) == "" {
		return nil
	}
	if _, err := template.New("check").Option("missingkey=zero").Parse(tpl); err != nil {
		return fmt.Errorf("%w: %v", ErrMailTemplateInvalid, err)
	}
	return nil
}

// GenerateMailCode 生成 n 位纯数字验证码（与短信验证码同一实现）。
func GenerateMailCode(n int) (string, error) {
	return GenerateSmsCode(n)
}
