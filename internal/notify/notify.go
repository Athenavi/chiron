// Package notify 实现站内通知中心（P2-3）：写通知、读通知、用户级邮件偏好。
//
// 设计边界（与迁移 0011 的文件头取舍一一对应）
// ------------------------------------------------
// 1. **站内信不可关闭，只让用户关邮件**。站内是事实留痕，关掉它会让"用户声称
//    没收到"变成无法查证的口径之争；退订诉求由 EmailEnabled 满足。
// 2. **幂等靠唯一索引撞 23505，不做"先查后写"**。并发下两个请求都能查到"没有"，
//    于是各插一条，用户收到重复推送。([user_id, dedupe_key] 唯一)
// 3. **邮件是尽力而为的旁路**。站内写入失败才返回错误；邮件发送失败只记日志，
//    因为邮件依赖外部 SMTP/HTTP 服务，让它决定业务成败会把可用性绑到最不可靠
//    的一环上。
// 4. **本包不依赖 auth / api**。邮件如何发出、配置从哪解密，都由调用方通过 Mailer
//    接口注入 —— 否则 notify 会反向依赖 api，形成循环。
package notify

import (
	"context"
	"errors"
	"log/slog"
	"time"
)

// ErrUnknownKind 事件类型不在 AllKinds 里。
//
// 单独定义而不是让上层比字符串：handler 需要把"类型不认识"与"写库失败"
// 分成 400 与 500 两种响应。
var ErrUnknownKind = errors.New("notify: unknown notification kind")

// 事件类型。写入 notifications.kind。
//
// 存字符串而不是枚举号：新增事件类型时不必改表结构，也不必做数据迁移。
const (
	// KindPaymentSucceeded 支付成功（含套餐订单与纯充值）。
	KindPaymentSucceeded = "payment_succeeded"
	// KindEntitlementExpiring 套餐权益即将到期。
	KindEntitlementExpiring = "entitlement_expiring"
	// KindQuotaChanged 租户配额被管理员变更。
	KindQuotaChanged = "quota_changed"
	// KindMemberInvited 凭邀请码加入团队成功（通知新成员本人）。
	//
	// 为什么不是"邀请发出时通知被邀请人"：`tenant_invites`（基线 0001:725）
	// 只有 code/tenant_id/created_by/max_uses/uses/expires_at，**没有 email 列**，
	// 是通用邀请码而非指向具体人的邀请，创建时没有确定的收件人。
	// 因此这个事件落在"成员真正加入"的那一刻。
	KindMemberInvited = "member_invited"
)

// AllKinds 是全部事件类型，供偏好接口做参数校验与前端渲染开关列表。
var AllKinds = []string{
	KindPaymentSucceeded,
	KindEntitlementExpiring,
	KindQuotaChanged,
	KindMemberInvited,
}

// IsValidKind 判断事件类型是否属于已知集合。
// 未知类型直接拒绝：放任任意字符串会让 `user_notification_prefs` 积累永远
// 不会匹配任何事件的垃圾行。
func IsValidKind(kind string) bool {
	for _, k := range AllKinds {
		if k == kind {
			return true
		}
	}
	return false
}

// Notification 是一条站内通知。
//
// 不存渲染后的 HTML：标题/正文由前端按当前语言渲染，存成 HTML 会让多语言切换后
// 历史通知仍是旧语言，也会把 XSS 面拉进数据库。
type Notification struct {
	ID        string         `json:"id"`
	TenantID  int16          `json:"tenant_id"`
	UserID    string         `json:"user_id"`
	Kind      string         `json:"kind"`
	DedupeKey string         `json:"-"`
	Title     string         `json:"title"`
	Body      string         `json:"body"`
	Link      string         `json:"link"`
	Payload   map[string]any `json:"payload,omitempty"`
	ReadAt    *time.Time     `json:"read_at"`
	CreatedAt time.Time      `json:"created_at"`
}

// Store 是通知的持久化边界。测试用内存实现替身，生产走 PGStore。
type Store interface {
	// Insert 幂等写入。inserted=false 表示"该事件对这个人已推送过"，
	// 不是错误 —— 调用方据此决定是否跳过邮件。
	Insert(ctx context.Context, n *Notification) (inserted bool, err error)
	// List 按时间倒序返回通知；unreadOnly=true 时只返回未读。
	List(ctx context.Context, userID string, limit int, unreadOnly bool) ([]Notification, error)
	// UnreadCount 返回未读数，供角标使用。
	UnreadCount(ctx context.Context, userID string) (int, error)
	// MarkRead 把指定 id 标为已读，返回实际受影响行数（已读的不会重复计数）。
	MarkRead(ctx context.Context, userID string, ids []string) (int, error)
	// MarkAllRead 把该用户全部未读标为已读。
	MarkAllRead(ctx context.Context, userID string) (int, error)
	// EmailEnabled 返回该用户对该类事件的邮件开关。**缺行即默认开启**。
	EmailEnabled(ctx context.Context, userID, kind string) (bool, error)
	// SetEmailEnabled 写入/更新开关。
	SetEmailEnabled(ctx context.Context, userID, kind string, enabled bool) error
	// ListPrefs 返回该用户显式设置过的开关（未出现的 kind 由调用方补默认开启）。
	ListPrefs(ctx context.Context, userID string) (map[string]bool, error)
	// DeleteOlderThan 分批删除 created_at < cutoff 的通知，返回本批删除行数。
	//
	// **按批而非一次删完**：一次 DELETE 十万行会长时间持有行锁，把并发的写入
	// （支付回调、配额变更）堵在后面。返回行数 = 0 表示已删空。
	DeleteOlderThan(ctx context.Context, cutoff time.Time, batchSize int) (int, error)
}

// Mailer 是邮件的唯一出口，由 api 层实现（复用既有 MailHandler 与库里的 SMTP 配置）。
//
// 只有两个参数：收件人由实现自己查（userID → email），内容已由 Manager 渲染成
// 纯文本。这样 notify 不必知道 users 表长什么样，也不必碰邮件密钥解密。
type Mailer interface {
	// SendNotification 尽力而为地投递；error 只用于日志，不向上传播。
	SendNotification(ctx context.Context, userID, subject, body string) error
}

// Manager 是通知的业务入口。Mailer 可以为 nil（未配置邮件时只发站内信）。
type Manager struct {
	store  Store
	mailer Mailer
}

// NewManager 构造 Manager。mailer 传 nil 表示该部署不发通知邮件。
func NewManager(store Store, mailer Mailer) *Manager {
	return &Manager{store: store, mailer: mailer}
}

// Notify 写一条站内通知，并在用户未关闭该类邮件时尽力发一封邮件。
//
// 返回 (inserted, err)：
//   - err != nil 表示**站内写入**失败（调用方决定是否忽略，业务主流程通常应忽略
//     并记日志，绝不让通知失败回退一次已完成的支付）。
//   - inserted=false 且 err==nil 表示该事件对这个人已推送过（幂等跳过），
//     此时**不发邮件** —— 否则重复回调会重复发信。
func (m *Manager) Notify(ctx context.Context, n *Notification) (bool, error) {
	if n == nil {
		return false, nil
	}
	inserted, err := m.store.Insert(ctx, n)
	if err != nil {
		return false, err
	}
	if !inserted {
		return false, nil
	}
	m.sendMail(ctx, n)
	return true, nil
}

// sendMail 是邮件旁路：任何失败只记日志。
func (m *Manager) sendMail(ctx context.Context, n *Notification) {
	if m.mailer == nil {
		return
	}
	enabled, err := m.store.EmailEnabled(ctx, n.UserID, n.Kind)
	if err != nil {
		// 查不到偏好时**按默认开启**继续发：宁可多发一封，也不要因为一次
		// 读失败让用户错过到期预警这类有实际后果的通知。
		slog.Warn("notify: read email preference failed, falling back to enabled",
			"user", n.UserID, "kind", n.Kind, "error", err)
		enabled = true
	}
	if !enabled {
		return
	}
	// 邮件正文按**收件人的语言**重新渲染，而不是发通知里存的兜底文本。
	//
	// 站内信由前端按界面语言实时渲染（历史通知也会跟着切），但邮件在发送这一刻
	// 就固化了 —— 用中文兜底文本发出去，en-US/ar 用户收到的就是一段看不懂的内容。
	title, text := RenderMail(n, m.userLocale(ctx, n.UserID))
	body := text
	if n.Link != "" {
		body = body + "\n\n" + n.Link
	}
	if err := m.mailer.SendNotification(ctx, n.UserID, title, body); err != nil {
		slog.Warn("notify: send email failed (in-app notification is already stored)",
			"user", n.UserID, "kind", n.Kind, "error", err)
	}
}

// userLocale 取收件人的界面语言，失败时回落 zh-CN。
//
// 用可选接口（LocaleResolver）而不是给 Store 再加一个必选方法：这会让内存替身
// 与所有既有实现都被迫补一个与其余逻辑无关的方法；不实现即回落默认语言，
// 行为完全可接受。
func (m *Manager) userLocale(ctx context.Context, userID string) string {
	resolver, ok := m.store.(LocaleResolver)
	if !ok {
		return "zh-CN"
	}
	loc, err := resolver.UserLocale(ctx, userID)
	if err != nil {
		slog.Warn("notify: read user locale failed, falling back to zh-CN",
			"user", userID, "error", err)
		return "zh-CN"
	}
	return loc
}

// LocaleResolver 由能读到用户语言偏好的 Store 实现（PGStore 实现了它）。
type LocaleResolver interface {
	// UserLocale 返回该用户的界面语言（如 "en-US"）；未设置返回空串。
	UserLocale(ctx context.Context, userID string) (string, error)
}

// List 透传存储层。
func (m *Manager) List(ctx context.Context, userID string, limit int, unreadOnly bool) ([]Notification, error) {
	return m.store.List(ctx, userID, limit, unreadOnly)
}

// UnreadCount 透传存储层。
func (m *Manager) UnreadCount(ctx context.Context, userID string) (int, error) {
	return m.store.UnreadCount(ctx, userID)
}

// MarkRead 透传存储层。
func (m *Manager) MarkRead(ctx context.Context, userID string, ids []string) (int, error) {
	return m.store.MarkRead(ctx, userID, ids)
}

// MarkAllRead 透传存储层。
func (m *Manager) MarkAllRead(ctx context.Context, userID string) (int, error) {
	return m.store.MarkAllRead(ctx, userID)
}

// SetEmailEnabled 校验事件类型后写入偏好。
func (m *Manager) SetEmailEnabled(ctx context.Context, userID, kind string, enabled bool) error {
	if !IsValidKind(kind) {
		return ErrUnknownKind
	}
	return m.store.SetEmailEnabled(ctx, userID, kind, enabled)
}

// Prefs 返回全部事件类型的邮件开关，未设置过的按默认开启补齐。
//
// 补默认值放在这里而不是存储层：存储层只该反映"用户显式设置过什么"，
// 而接口要给出"当前实际生效什么"。
func (m *Manager) Prefs(ctx context.Context, userID string) (map[string]bool, error) {
	explicit, err := m.store.ListPrefs(ctx, userID)
	if err != nil {
		return nil, err
	}
	out := make(map[string]bool, len(AllKinds))
	for _, k := range AllKinds {
		if v, ok := explicit[k]; ok {
			out[k] = v
			continue
		}
		out[k] = true
	}
	return out, nil
}
