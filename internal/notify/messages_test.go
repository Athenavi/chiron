package notify

import (
	"context"
	"errors"
	"testing"
	"time"
)

// T1 的目标：邮件出口不能再对非中文用户发中文正文。
// 站内信由前端实时渲染（历史通知跟随语言切换），邮件必须在发送这一刻按
// **收件人语言**定稿，两者共用同一份模板。

func TestSupportedLocaleNormalizes(t *testing.T) {
	cases := map[string]string{
		"":        "zh-CN",
		"zh-CN":   "zh-CN",
		"zh":      "zh-CN",
		"en-US":   "en-US",
		"en":      "en-US",
		"en-GB":   "en-US",
		"ar":      "ar",
		"ar-SA":   "ar",
		"fr-FR":   "zh-CN", // 未支持的语言回落源语言，而不是发空文案
		"garbage": "zh-CN",
	}
	for in, want := range cases {
		if got := SupportedLocale(in); got != want {
			t.Errorf("SupportedLocale(%q) = %q，期望 %q", in, got, want)
		}
	}
}

// localeAwareStore 在 fakeStore 之上实现 LocaleResolver。
type localeAwareStore struct {
	*fakeStore
	locale string
	err    error
}

func (l *localeAwareStore) UserLocale(context.Context, string) (string, error) {
	return l.locale, l.err
}

// capturingMailer 记录发出的邮件标题与正文（测试是单线程的，不需要互斥）。
type capturingMailer struct {
	subject string
	body    string
	sent    int
}

func (c *capturingMailer) SendNotification(_ context.Context, _, subject, body string) error {
	c.subject = subject
	c.body = body
	c.sent++
	return nil
}

func newPaymentNote(userID string) *Notification {
	return &Notification{
		UserID:    userID,
		Kind:      KindPaymentSucceeded,
		DedupeKey: KindPaymentSucceeded + ":pay_1",
		Title:     "支付成功", // 兜底：语言解析失败时用它
		Body:      "充值 1000 credits 已到账。",
		Payload: map[string]any{
			"payment_id":   "pay_1",
			"credits":      1000,
			"amount_cents": 1000,
		},
	}
}

// 英文用户收到英文邮件 —— 这是 T1 要修的核心问题。
func TestMailRenderedInRecipientLocale(t *testing.T) {
	store := &localeAwareStore{fakeStore: newFakeStore(), locale: "en-US"}
	mail := &capturingMailer{}
	m := NewManager(store, mail)

	if _, err := m.Notify(context.Background(), newPaymentNote("u-1")); err != nil {
		t.Fatalf("Notify 报错: %v", err)
	}
	if mail.sent != 1 {
		t.Fatalf("应发 1 封邮件，实际 %d", mail.sent)
	}
	if want := "Payment succeeded"; mail.subject != want {
		t.Errorf("邮件标题应为 %q，实际 %q", want, mail.subject)
	}
	if want := "1000 credits have been added"; !contains(mail.body, want) {
		t.Errorf("邮件正文应含 %q，实际 %q", want, mail.body)
	}
}

// 阿拉伯语用户收到阿语邮件。
func TestMailRenderedInArabic(t *testing.T) {
	store := &localeAwareStore{fakeStore: newFakeStore(), locale: "ar"}
	mail := &capturingMailer{}
	m := NewManager(store, mail)

	if _, err := m.Notify(context.Background(), newPaymentNote("u-1")); err != nil {
		t.Fatalf("Notify 报错: %v", err)
	}
	if want := "تم الدفع بنجاح"; mail.subject != want {
		t.Errorf("邮件标题应为 %q，实际 %q", want, mail.subject)
	}
}

// 读不到语言偏好（store 未实现 LocaleResolver）时回落中文兜底，不能发空邮件。
func TestMailFallsBackWhenLocaleUnknown(t *testing.T) {
	// fakeStore 没有实现 UserLocale ⇒ 走回落分支。
	store := newFakeStore()
	mail := &capturingMailer{}
	m := NewManager(store, mail)

	if _, err := m.Notify(context.Background(), newPaymentNote("u-1")); err != nil {
		t.Fatalf("Notify 报错: %v", err)
	}
	if want := "支付成功"; mail.subject != want {
		t.Errorf("未实现 LocaleResolver 时应回落中文标题 %q，实际 %q", want, mail.subject)
	}
	if mail.body == "" {
		t.Fatal("正文不能为空")
	}
}

// 语言查询报错时同样回落，且不影响站内信落库。
func TestMailFallsBackWhenLocaleQueryFails(t *testing.T) {
	store := &localeAwareStore{fakeStore: newFakeStore(), err: errors.New("db down")}
	mail := &capturingMailer{}
	m := NewManager(store, mail)

	inserted, err := m.Notify(context.Background(), newPaymentNote("u-1"))
	if err != nil {
		t.Fatalf("Notify 不应报错: %v", err)
	}
	if !inserted {
		t.Fatal("站内信应已写入")
	}
	if want := "支付成功"; mail.subject != want {
		t.Errorf("语言查询失败时应回落 %q，实际 %q", want, mail.subject)
	}
}

// 套餐订单的邮件必须含套餐名与到期日，且随语言切换措辞。
func TestMailPackagePaymentLocalized(t *testing.T) {
	expires := time.Date(2026, 12, 30, 10, 0, 0, 0, time.UTC)
	n := &Notification{
		UserID:    "u-1",
		Kind:      KindPaymentSucceeded,
		DedupeKey: KindPaymentSucceeded + ":pay_2",
		Title:     "套餐购买成功",
		Body:      "套餐「入门包」已生效，5000 credits 已到账。",
		Payload: map[string]any{
			"credits":      5000,
			"package_code": "starter",
			"package_name": "入门包",
			"expires_at":   expires.Format(time.RFC3339),
		},
	}

	title, body := RenderMail(n, "en-US")
	if want := "Package purchased"; title != want {
		t.Errorf("英文套餐标题应为 %q，实际 %q", want, title)
	}
	if !contains(body, "入门包") {
		t.Errorf("正文应含套餐名，实际 %q", body)
	}
	if !contains(body, "2026-12-30") {
		t.Errorf("正文应含到期日，实际 %q", body)
	}
}

// 缺参数时回落兜底文本，绝不渲染出 %!d(MISSING)。
func TestRenderBodyFallsBackOnArgMismatch(t *testing.T) {
	got := RenderBody(KindPaymentSucceeded, "en-US", "兜底正文", "not-a-number")
	if got != "兜底正文" {
		t.Errorf("参数类型不符应回落兜底，实际 %q", got)
	}
	// 正确参数下不应回落。
	got = RenderBody(KindPaymentSucceeded, "en-US", "兜底正文", 100)
	if got == "兜底正文" || !contains(got, "100") {
		t.Errorf("正确参数应渲染出正文，实际 %q", got)
	}
}

// 未知 kind 原样用兜底文本，不猜、不返回空串。
func TestRenderMailUnknownKindFallsBack(t *testing.T) {
	n := &Notification{Kind: "some_future_kind", Title: "标题", Body: "正文"}
	title, body := RenderMail(n, "en-US")
	if title != "标题" || body != "正文" {
		t.Errorf("未知 kind 应回落兜底，实际 (%q, %q)", title, body)
	}
}

// 三类事件的三语渲染都不应残留兜底（说明模板确实被选中）。
func TestRenderMailAllKindsAllLocales(t *testing.T) {
	cases := []struct {
		name string
		n    *Notification
	}{
		{"entitlement_expiring", &Notification{
			Kind: KindEntitlementExpiring, Title: "兜底", Body: "兜底",
			Payload: map[string]any{
				"package_name": "入门包",
				"expires_at":   time.Date(2026, 11, 2, 0, 0, 0, 0, time.UTC).Format(time.RFC3339),
				"days_before":  3,
			},
		}},
		{"quota_changed", &Notification{
			Kind: KindQuotaChanged, Title: "兜底", Body: "兜底",
			Payload: map[string]any{
				"resource_type": "token", "total_amount": 100000,
				"period": "monthly", "action": "updated",
			},
		}},
		{"member_invited", &Notification{
			Kind: KindMemberInvited, Title: "兜底", Body: "兜底",
			Payload: map[string]any{"tenant_name": "Acme"},
		}},
	}
	for _, tc := range cases {
		for _, loc := range []string{"en-US", "ar"} {
			title, body := RenderMail(tc.n, loc)
			if title == "兜底" || body == "兜底" {
				t.Errorf("%s/%s 未选中模板（仍为兜底）", tc.name, loc)
			}
			if title == "" || body == "" {
				t.Errorf("%s/%s 渲染出空文案", tc.name, loc)
			}
		}
	}
}

// contains 是子串判定，避免为几处断言引入额外的辅助依赖。
func contains(s, sub string) bool {
	if sub == "" {
		return true
	}
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
