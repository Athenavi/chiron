package api

import (
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/billing"
)

// billing_payment_response_test.go —— 订单响应形状（§13.4 小修补）。
//
// 这条测试守两件事：
//  1. 套餐订单必须回显 package_code，否则订单详情看不出买的是哪个套餐，
//     用户拿到的只是一笔"金额恰好相等"的充值，无法与套餐目录对账。
//  2. 纯充值订单**不能**多出这个字段 —— 存量订单都是纯充值，多一个空字段
//     等于悄悄改变已有响应形状，前端与第三方对接方都可能受影响。
func TestPaymentResponseEchoesPackageCode(t *testing.T) {
	h := &BillingHandler{}
	created := time.Date(2026, 10, 29, 10, 0, 0, 0, time.UTC)

	t.Run("套餐订单回显 package_code", func(t *testing.T) {
		resp := h.paymentResponse(&billing.Payment{
			ID:           "pay_1",
			Channel:      "alipay",
			Credits:      6000,
			AmountCents:  6000,
			Currency:     "CNY",
			Status:       "pending",
			PackageCode:  "standard",
			CreatedAt:    created,
		})
		got, ok := resp["package_code"]
		if !ok {
			t.Fatal("套餐订单缺少 package_code，订单详情无法判断买的是哪个套餐")
		}
		if got != "standard" {
			t.Errorf("package_code = %v，期望 standard", got)
		}
	})

	t.Run("纯充值订单不带 package_code", func(t *testing.T) {
		resp := h.paymentResponse(&billing.Payment{
			ID:          "pay_2",
			Channel:     "wechat",
			Credits:     1000,
			AmountCents: 1000,
			Currency:    "CNY",
			Status:      "paid",
			CreatedAt:   created,
		})
		if _, ok := resp["package_code"]; ok {
			t.Error("纯充值订单不该出现 package_code，会改变存量响应形状")
		}
	})

	// 既有字段不能被这次改动挤掉：回归防线比新功能本身更重要。
	t.Run("既有字段保持不变", func(t *testing.T) {
		resp := h.paymentResponse(&billing.Payment{
			ID:          "pay_3",
			Channel:     "alipay",
			Credits:     500,
			AmountCents: 500,
			Currency:    "USD",
			Status:      "pending",
			PackageCode: "pro",
			QRCode:      "https://qr.example/x",
			CreatedAt:   created,
		})
		for _, k := range []string{"id", "channel", "credits", "amount_cents", "currency", "status", "created_at", "qr_code"} {
			if _, ok := resp[k]; !ok {
				t.Errorf("响应缺少既有字段 %q", k)
			}
		}
		if resp["qr_code"] != "https://qr.example/x" {
			t.Errorf("qr_code = %v", resp["qr_code"])
		}
	})

	// 空二维码同样不该出现（既有口径，与 package_code 同一套省略规则）。
	t.Run("无二维码时省略 qr_code", func(t *testing.T) {
		resp := h.paymentResponse(&billing.Payment{ID: "pay_4", CreatedAt: created})
		if _, ok := resp["qr_code"]; ok {
			t.Error("没有二维码却返回了 qr_code 空字段")
		}
	})
}
