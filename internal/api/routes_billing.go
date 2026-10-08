package api

import (
	"net/http"
)

// routes_billing.go —— 计费的路由注册。
// 由 gateway_router.go 按业务域拆分而来（纯移动：函数签名、函数体、调用点一字未改）。

// ── Billing ──

func registerBillingRoutes(mux *http.ServeMux, billingHandler *BillingHandler, authMW, rlMW routeMiddleware) {
	// Billing (auth + rate limited)
	mux.Handle("GET /v1/billing/balance", authMW(rlMW(http.HandlerFunc(billingHandler.GetBalance))))
	mux.Handle("GET /v1/billing/history", authMW(rlMW(http.HandlerFunc(billingHandler.GetHistory))))
	mux.Handle("POST /v1/billing/recharge", authMW(rlMW(http.HandlerFunc(billingHandler.Recharge))))
	mux.Handle("POST /v1/billing/pay", authMW(rlMW(http.HandlerFunc(billingHandler.CreatePayment))))
	mux.Handle("GET /v1/billing/orders/{id}", authMW(rlMW(http.HandlerFunc(billingHandler.GetOrder))))
	// 支付渠道异步回调（无 auth：支付宝验签 / 微信平台证书验签 + AES-GCM 解密）
	mux.Handle("POST /v1/billing/callback/alipay", rlMW(http.HandlerFunc(billingHandler.AlipayCallback)))
	mux.Handle("POST /v1/billing/callback/wechat", rlMW(http.HandlerFunc(billingHandler.WechatCallback)))
	mux.Handle("POST /v1/billing/paypal-capture", authMW(rlMW(http.HandlerFunc(billingHandler.PayPalCapture))))
	mux.Handle("GET /v1/billing/usage", authMW(rlMW(http.HandlerFunc(billingHandler.GetUsage))))
	// 可用支付渠道：充值页据此只展示后台已配置启用的渠道，避免选中后下单才报 501
	mux.Handle("GET /v1/billing/channels", authMW(rlMW(http.HandlerFunc(billingHandler.ListPaymentChannels))))
}
