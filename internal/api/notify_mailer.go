package api

import (
	"context"
	"fmt"
	"log/slog"

	"github.com/jackc/pgx/v5"
)

// notifyMailer 实现 notify.Mailer：把通知中心的邮件出口接回既有的 MailHandler。
//
// 为什么放在 api 层而不是 notify 包：发信需要两样 notify 不该知道的东西——
// 库里 `ent_mail_config` 的 SMTP 配置（含 AES-GCM 密文解密），以及 `users.email`。
// 把它们塞进 notify 会让一���纯通知包反向依赖 api，形成循环引用。
type notifyMailer struct {
	mail *MailHandler
}

// NewNotifyMailer 用既有邮件能力构造通知邮件出口。mail 为 nil 时返回 nil，
// 供 notify.NewManager 收到"该部署不发通知邮件"。
func NewNotifyMailer(mail *MailHandler) *notifyMailer {
	if mail == nil {
		return nil
	}
	return &notifyMailer{mail: mail}
}

// SendNotification 往该用户邮箱发一封纯文本通知邮件。
//
// 纯文本而非 HTML：通知正文来自服务端拼装（套餐名、配额数值等），转 HTML 要额外
// 转义用户可控内容（如套餐名），而 TEMPLATED 邮件在多语言场景下也没有意义。
func (m *notifyMailer) SendNotification(ctx context.Context, userID, subject, body string) error {
	if m == nil || m.mail == nil || userID == "" {
		return nil
	}
	// 收件地址从 users 取，而不是让调用方传：通知只认 user_id，
	// 邮箱是配送细节，不该出现在业务调用点。
	var email string
	err := m.mail.db.QueryRow(ctx, `SELECT COALESCE(email, '') FROM users WHERE id = $1`, userID).Scan(&email)
	if err != nil {
		if err == pgx.ErrNoRows {
			// 用户已被删除：站内信可能还在，邮件无处可投属正常情况，不当故障。
			return nil
		}
		return fmt.Errorf("lookup email for %s: %w", userID, err)
	}
	if email == "" {
		return nil
	}

	row, err := m.mail.loadConfig(ctx)
	if err != nil {
		return fmt.Errorf("load mail config: %w", err)
	}
	// 未启用或没有配置时静默跳过：通知邮件是可选能力，未配置不等于故障。
	if row == nil || !row.Enabled {
		return nil
	}
	if err := m.mail.sendMail(ctx, row, subject, body, []string{email}); err != nil {
		return fmt.Errorf("send notification mail: %w", err)
	}
	slog.Debug("notify mail sent", "user", userID, "subject", subject)
	return nil
}
