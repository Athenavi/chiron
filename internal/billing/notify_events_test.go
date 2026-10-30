package billing

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/notify"
)

// recordingNotifier 记录 Manager 发出的通知，可注入错误。
//
// 手写替身而不是引 mock 库：本仓库无 gomock/testify-mock 依赖，而这些用例要
// 断言的正是"发了什么内容""失败是否影响主流程"这类事实，手写更直观。
type recordingNotifier struct {
	got  []*notify.Notification
	fail error
}

func (r *recordingNotifier) Notify(_ context.Context, n *notify.Notification) (bool, error) {
	if r.fail != nil {
		return false, r.fail
	}
	// 复制一份：调用方可能在 Notify 返回后复用同一个指针。
	cp := *n
	r.got = append(r.got, &cp)
	return true, nil
}

func newTestManager() *Manager {
	// store 传 nil 是安全的：这些用例只走通知分支，不触碰存储。
	return NewManager(nil)
}

// 纯充值订单：通知标题是"支付成功"，去重键为 kind + 订单号。
func TestNotifyPaymentSucceededTopup(t *testing.T) {
	m := newTestManager()
	rec := &recordingNotifier{}
	m.SetNotifier(rec)

	p := NewPayment("u-1", ChannelAlipay, 1000, 1000, "CNY")
	m.notifyPaymentSucceeded(context.Background(), p, nil)

	if len(rec.got) != 1 {
		t.Fatalf("应发 1 条通知，实际 %d", len(rec.got))
	}
	n := rec.got[0]
	if n.Kind != notify.KindPaymentSucceeded {
		t.Errorf("kind 应为 %s，实际 %s", notify.KindPaymentSucceeded, n.Kind)
	}
	if want := notify.KindPaymentSucceeded + ":" + p.ID; n.DedupeKey != want {
		t.Errorf("去重键应为 %q，实际 %q", want, n.DedupeKey)
	}
	if n.UserID != "u-1" {
		t.Errorf("收件人应为 u-1，实际 %s", n.UserID)
	}
	if n.Title != "支付成功" {
		t.Errorf("纯充值标题不符: %q", n.Title)
	}
	if n.Payload["credits"] != 1000 {
		t.Errorf("payload.credits 应为 1000，实际 %v", n.Payload["credits"])
	}
	// 纯充值不应带套餐字段 —— 否则前端会误判为套餐订单。
	if _, ok := n.Payload["package_code"]; ok {
		t.Error("纯充值订单不应带 package_code")
	}
}

// 套餐订单：标题/正文要体现套餐名与到期日，payload 带权益信息。
func TestNotifyPaymentSucceededPackage(t *testing.T) {
	m := newTestManager()
	rec := &recordingNotifier{}
	m.SetNotifier(rec)

	p := NewPayment("u-2", ChannelWechat, 5000, 9900, "CNY")
	p.PackageCode = "starter"
	expires := time.Date(2026, 12, 30, 10, 0, 0, 0, time.UTC)
	ent := &Entitlement{
		ID:          "ent-1",
		UserID:      "u-2",
		PackageCode: "starter",
		PackageName: "入门包",
		ExpiresAt:   expires,
	}
	m.notifyPaymentSucceeded(context.Background(), p, ent)

	if len(rec.got) != 1 {
		t.Fatalf("应发 1 条通知，实际 %d", len(rec.got))
	}
	n := rec.got[0]
	if n.Title != "套餐购买成功" {
		t.Errorf("套餐订单标题不符: %q", n.Title)
	}
	// 正文必须含套餐名（用户认的是名字，不是 code）。
	if want := "入门包"; !contains(n.Body, want) {
		t.Errorf("正文应含套餐名 %q，实际 %q", want, n.Body)
	}
	if want := "2026-12-30"; !contains(n.Body, want) {
		t.Errorf("正文应含到期日 %q，实际 %q", want, n.Body)
	}
	if n.Payload["package_code"] != "starter" {
		t.Errorf("payload.package_code 不符: %v", n.Payload["package_code"])
	}
	if n.Payload["entitlement_id"] != "ent-1" {
		t.Errorf("payload.entitlement_id 不符: %v", n.Payload["entitlement_id"])
	}
	if n.Payload["expires_at"] != expires.Format(time.RFC3339) {
		t.Errorf("payload.expires_at 不符: %v", n.Payload["expires_at"])
	}
}

// 权益发放失败（ent=nil）时仍要发通知，只是退化为按 package_code 描述。
// 理由：credits 已入账，用户必须收到"支付成功"；少一份权益细节不该让整条通知消失。
func TestNotifyPaymentSucceededPackageWithoutEntitlement(t *testing.T) {
	m := newTestManager()
	rec := &recordingNotifier{}
	m.SetNotifier(rec)

	p := NewPayment("u-3", ChannelWechat, 5000, 9900, "CNY")
	p.PackageCode = "pro"
	m.notifyPaymentSucceeded(context.Background(), p, nil)

	if len(rec.got) != 1 {
		t.Fatalf("权益缺失时仍应发 1 条通知，实际 %d", len(rec.got))
	}
	n := rec.got[0]
	if n.Title != "套餐购买成功" {
		t.Errorf("仍是套餐订单，标题应为「套餐购买成功」，实际 %q", n.Title)
	}
	// 拿不到套餐名时退化用 code，不能出现空名字。
	if !contains(n.Body, "pro") {
		t.Errorf("正文应退化为使用 package_code，实际 %q", n.Body)
	}
	if _, ok := n.Payload["entitlement_id"]; ok {
		t.Error("未发放权益时不应带 entitlement_id")
	}
}

// nil 订单不应发通知，也不应 panic。
func TestNotifyPaymentSucceededNilPayment(t *testing.T) {
	m := newTestManager()
	rec := &recordingNotifier{}
	m.SetNotifier(rec)

	m.notifyPaymentSucceeded(context.Background(), nil, nil)

	if len(rec.got) != 0 {
		t.Fatalf("nil 订单不应发通知，实际 %d", len(rec.got))
	}
}

// 未装配通知中心时退化为 no-op（不能 panic、不能报错）。
func TestNotifyPaymentSucceededWithoutNotifier(t *testing.T) {
	m := newTestManager()
	p := NewPayment("u-4", ChannelAlipay, 100, 100, "CNY")
	// 不调用 SetNotifier。
	m.notifyPaymentSucceeded(context.Background(), p, nil)
}

// 通知失败不得影响主流程：emitNotification 只记日志，不返回错误。
func TestEmitNotificationSwallowsError(t *testing.T) {
	m := newTestManager()
	m.SetNotifier(&recordingNotifier{fail: errors.New("db down")})

	p := NewPayment("u-5", ChannelAlipay, 100, 100, "CNY")
	// 这里的关键断言是"不 panic 且函数正常返回" —— 支付链路不能因为通知失败而中断。
	m.notifyPaymentSucceeded(context.Background(), p, nil)
}

// SetNotifier(nil) 之后再发通知也应安全（运维可在线关闭）。
func TestSetNotifierNilDisables(t *testing.T) {
	m := newTestManager()
	rec := &recordingNotifier{}
	m.SetNotifier(rec)
	m.SetNotifier(nil)

	m.notifyPaymentSucceeded(context.Background(), NewPayment("u-6", ChannelAlipay, 1, 1, "CNY"), nil)
	if len(rec.got) != 0 {
		t.Fatalf("关闭通知后不应再发，实际 %d 条", len(rec.got))
	}
}

// contains 是小的子串判定，避免为几处断言引入 strings 之外的辅助。
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
