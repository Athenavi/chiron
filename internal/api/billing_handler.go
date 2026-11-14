package api

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"log/slog"
	"math"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/billing"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/settings"
)

type BillingHandler struct {
	mgr           *billing.Manager
	authenticator *auth.Authenticator
	cfg           *config.Config
	payPalClient  *http.Client

	// payMu 保护下列支付凭据与渠道客户端：后台「支付配置」保存后会热替换
	// （见 ApplyPaymentConfig），而请求处理路径是并发的，必须加锁读取。
	payMu  sync.RWMutex
	payCfg PaymentConfig
	alipay *billing.AlipayClient // nil = 未配置/未启用，充值入口返回 501
	wechat *billing.WechatClient // nil = 未配置/未启用

	// settingsStore 用于按租户读取支付配置（system_settings.tenant_id>0）。
	// 启动时注入一次，之后只读；租户未单独配置时回退全局 payCfg。
	settingsStore *settings.Store

	// wechatSerialIndex: 微信商户证书序列号 → 客户端。微信 V3 回调带
	// `Wechatpay-Serial` 头，命中索引即可 O(1) 定位，避免每次回调遍历所有租户。
	// 懒加载；支付配置变更时清空，下次回调用新配置重建。
	wechatSerialMu    sync.RWMutex
	wechatSerialIndex map[string]*billing.WechatClient
}

func NewBillingHandler(mgr *billing.Manager, authenticator *auth.Authenticator, cfg *config.Config) *BillingHandler {
	h := &BillingHandler{
		mgr: mgr, authenticator: authenticator, cfg: cfg,
		payPalClient: &http.Client{Timeout: 30 * time.Second},
	}
	// 初始凭据来自环境变量；后台配置（system_settings.payment）在启动时由
	// ReloadPaymentConfig 叠加，两者合并结果才是生效配置。
	h.ApplyPaymentConfig(paymentConfigFromEnv(cfg))
	return h
}

// ApplyPaymentConfig 热替换支付凭据并重建渠道客户端。
// 单个渠道构造失败（如私钥 PEM 非法）只令该渠道不可用并记日志，不影响其它渠道，
// 也不回滚已保存的配置 —— 避免把「配错了」放大成「全都不能用」。
func (h *BillingHandler) ApplyPaymentConfig(p PaymentConfig) {
	alipay, wechat, errs := buildPaymentClients(p)
	if len(errs) > 0 {
		slog.Error("payment client init failed", "error", joinErrs(errs))
	}
	h.payMu.Lock()
	h.payCfg = p
	h.alipay = alipay
	h.wechat = wechat
	h.payMu.Unlock()
	// 配置变更后列宽索引失效，下次回调按新配置重建。
	h.invalidateWechatSerialIndex()
}

// paymentConfig 返回当前生效的支付配置快照（值拷贝，调用方可安全读取）。
func (h *BillingHandler) paymentConfig() PaymentConfig {
	h.payMu.RLock()
	defer h.payMu.RUnlock()
	return h.payCfg
}

// SetSettingsStore 注入设置存储：租户级支付配置按需从这里读取。
func (h *BillingHandler) SetSettingsStore(store *settings.Store) {
	h.settingsStore = store
}

// paymentConfigForTenant 返回某租户生效的支付配置：租户单独配置优先，否则全局默认。
//
// 每次下单读一次 DB：支付下单是低频操作，换取"保存即生效、多副本一致"，
// 比维护一层需要失效的进程内缓存更不容易出错。
func (h *BillingHandler) paymentConfigForTenant(ctx context.Context, tenantID int) PaymentConfig {
	if tenantID > 0 && h.settingsStore != nil {
		if saved, err := h.settingsStore.LoadTenantConfig(ctx, tenantID, settingsCategoryPayment); err == nil && len(saved) > 0 {
			return paymentConfigFromEnv(h.cfg).applyMap(saved)
		}
	}
	return h.paymentConfig()
}

// tenantIDForPayment 按订单反查租户（payments.user_id → users.tenant_id）。
//
// 回调/渠道查询没有 JWT，必须从订单归属推导租户；否则租户自己的商户号签出的
// 回调会被全局凭据验签失败，用户永远到不了账。
func (h *BillingHandler) tenantIDForPayment(ctx context.Context, paymentID string) int {
	if paymentID == "" {
		return 0
	}
	var tenantID int
	_ = db.GlobalDBManager.QueryRow(ctx,
		`SELECT COALESCE(u.tenant_id, 0)
		   FROM payments p LEFT JOIN users u ON u.id = p.user_id
		  WHERE p.id = $1`, paymentID).Scan(&tenantID)
	return tenantID
}

// clientsForPayment 按订单所属租户构造渠道客户端。
func (h *BillingHandler) clientsForPayment(ctx context.Context, paymentID string) (PaymentConfig, *billing.AlipayClient, *billing.WechatClient) {
	cfg := h.paymentConfigForTenant(ctx, h.tenantIDForPayment(ctx, paymentID))
	alipay, wechat, _ := buildPaymentClients(cfg)
	return cfg, alipay, wechat
}

// wechatSerialKey 归一化微信商户证书序列号（回调头 Wechatpay-Serial）。
func wechatSerialKey(cfg PaymentConfig) string {
	return strings.ToUpper(strings.TrimSpace(cfg.WechatMchCertSerialNo))
}

// wechatClientsBySerial 返回“证书序列号 → 微信客户端”索引（懒加载）。
//
// 微信 V3 回调带 `Wechatpay-Serial`，用它直接定位对应租户的客户端；
// 未命中时才回退到遍历（旧行为）。索引在支付配置变更后由
// invalidateWechatSerialIndex 清空，下次回调按新配置重建。
func (h *BillingHandler) wechatClientsBySerial(ctx context.Context) map[string]*billing.WechatClient {
	h.wechatSerialMu.RLock()
	idx := h.wechatSerialIndex
	h.wechatSerialMu.RUnlock()
	if idx != nil {
		return idx
	}

	h.wechatSerialMu.Lock()
	defer h.wechatSerialMu.Unlock()
	if h.wechatSerialIndex != nil {
		return h.wechatSerialIndex
	}
	idx = make(map[string]*billing.WechatClient)
	add := func(cfg PaymentConfig) {
		key := wechatSerialKey(cfg)
		if key == "" {
			return
		}
		if _, exists := idx[key]; exists {
			return
		}
		if _, wc, _ := buildPaymentClients(cfg); wc != nil {
			idx[key] = wc
		}
	}
	add(h.paymentConfig())
	if h.settingsStore != nil {
		if ids, err := h.settingsStore.TenantIDsWithConfig(ctx, settingsCategoryPayment); err == nil {
			for _, tenantID := range ids {
				add(h.paymentConfigForTenant(ctx, tenantID))
			}
		}
	}
	h.wechatSerialIndex = idx
	return idx
}

// invalidateWechatSerialIndex 在支付配置变更后清空序列号索引。
func (h *BillingHandler) invalidateWechatSerialIndex() {
	h.wechatSerialMu.Lock()
	h.wechatSerialIndex = nil
	h.wechatSerialMu.Unlock()
}

// alipayClient 返回当前支付宝客户端（nil 表示未配置或未启用）。
func (h *BillingHandler) alipayClient() *billing.AlipayClient {
	h.payMu.RLock()
	defer h.payMu.RUnlock()
	return h.alipay
}

// wechatClient 返回当前微信支付客户端（nil 表示未配置或未启用）。
func (h *BillingHandler) wechatClient() *billing.WechatClient {
	h.payMu.RLock()
	defer h.payMu.RUnlock()
	return h.wechat
}

func (h *BillingHandler) firstOrigin() string {
	allow := currentCORSAllowOrigin()
	if allow == "" && h.cfg != nil {
		// 兜底：启动注入前的构造期值（正常路径 gateway_router 会先 SetCORSAllowOrigin）
		allow = h.cfg.CORSOrigins
	}
	parts := strings.SplitN(allow, ",", 2)
	if len(parts) == 0 {
		return ""
	}
	return strings.TrimRight(parts[0], "/ ")
}

func (h *BillingHandler) GetBalance(w http.ResponseWriter, r *http.Request) {
	userID := h.resolveUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	balance, err := h.mgr.GetBalance(userID)
	if err != nil {
		slog.Error("get balance failed", "user_id", userID, "error", err)
		InternalError(w, "failed to get balance")
		return
	}

	// Also return daily free count for diagnosis
	count, countErr := h.mgr.DailyFreeCount(r.Context(), userID)
	diag := map[string]interface{}{
		"user_id":              userID,
		"balance":              balance,
		"daily_free_limit":     billing.DailyFreeLimit,
		"daily_free_used":      count,
		"daily_free_remaining": billing.DailyFreeLimit - count,
		"within_free_quota":    count < billing.DailyFreeLimit,
	}
	if countErr != nil {
		diag["daily_free_error"] = countErr.Error()
	}

	OK(w, diag)
}

func (h *BillingHandler) GetHistory(w http.ResponseWriter, r *http.Request) {
	userID := h.resolveUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	history, err := h.mgr.GetHistory(r.Context(), userID, 50)
	if err != nil {
		slog.Error("get billing history failed", "user_id", userID, "error", err)
		InternalError(w, "failed to get billing history")
		return
	}

	OK(w, map[string]interface{}{"user_id": userID, "history": history})
}

// usageRangeDays 是 `range` 参数的白名单。用映射而不是直接解析整数：
// 允许任意天数会让用户发起 `range=3650` 这类全表扫描，而这个端点是**同步**查询。
var usageRangeDays = map[string]int{
	"7d":  7,
	"30d": 30,
	"90d": 90,
}

// usageGroupByWhitelist 是 `groupBy` 的白名单。
//
// 只放 usage_events 里**真实有数据**的维度。`endpoint` 曾被考虑但不在此列：
// 明细表根本没记这一列，加进去只会返回一列空值，比不提供更误导。
var usageGroupByWhitelist = map[string]bool{
	"day":     true,
	"model":   true,
	"session": true,
}



// ── 套餐与权益─────────────────────────────────────────────────

// GetPackages 列出已上架套餐，供充值页展示"可购买的套餐"。
func (h *BillingHandler) GetPackages(w http.ResponseWriter, r *http.Request) {
	tenantID := 0
	if claims := auth.GetClaims(r.Context()); claims != nil {
		tenantID = int(claims.TenantID)
	}
	pkgs, err := h.mgr.ListPackages(r.Context(), tenantID)
	if err != nil {
		slog.Error("list packages failed", "error", err)
		InternalError(w, "failed to list packages")
		return
	}
	OK(w, pkgs)
}

// entitlementView 是权益的用户可见视图。
//
// 带上 active / expires_at / days_remaining 三个字段而不是让前端自己算：
// "是否过期"以服务端时间为准，避免客户端时区或时钟偏差造成"显示还有 1 天、
// 实际已经用不了"这类最容易被投诉的错觉。
type entitlementView struct {
	ID            string   `json:"id"`
	PackageCode   string   `json:"package_code"`
	PackageName   string   `json:"package_name"`
	AllowedModels []string `json:"allowed_models"`
	StartsAt      string   `json:"starts_at"`
	ExpiresAt     string   `json:"expires_at"`
	Active        bool     `json:"active"`
	// DaysRemaining 向上取整的剩余天数；已过期为 0。
	DaysRemaining int `json:"days_remaining"`
}

// GetEntitlements 返回当前用户的权益列表（**含已过期**）。
//
// 为什么必须返回已过期的：只返回生效中的条目，到期后记录会从界面消失，用户无法
// 区分"过期了"和"从没买过"，也就无从知道"为什么模型突然不能用了"——
// 这正是验收要求里"不能出现用不了但没提示"的那一类。
func (h *BillingHandler) GetEntitlements(w http.ResponseWriter, r *http.Request) {
	userID := h.resolveUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	ents, err := h.mgr.UserEntitlements(r.Context(), userID)
	if err != nil {
		slog.Error("list entitlements failed", "user_id", userID, "error", err)
		InternalError(w, "failed to list entitlements")
		return
	}

	now := time.Now()
	views := make([]entitlementView, 0, len(ents))
	for _, e := range ents {
		active := !now.Before(e.StartsAt) && now.Before(e.ExpiresAt)
		days := 0
		if active {
			// 向上取整：还剩 6 小时也应显示"1 天"，显示 0 天会让用户以为已经过期。
			days = int(math.Ceil(e.ExpiresAt.Sub(now).Hours() / 24))
			if days < 1 {
				days = 1
			}
		}
		views = append(views, entitlementView{
			ID:            e.ID,
			PackageCode:   e.PackageCode,
			PackageName:   e.PackageName,
			AllowedModels: e.AllowedModels,
			StartsAt:      e.StartsAt.Format(time.RFC3339),
			ExpiresAt:     e.ExpiresAt.Format(time.RFC3339),
			Active:        active,
			DaysRemaining: days,
		})
	}
	OK(w, views)
}


// usageBreakdownLimit 是单次返回的 bucket 行数上限。
//
// 为什么要有它：前端分页只是**视觉上**分页，后端若把 90 天内的全部
// model/session 聚合结果一次性返回，模型多的账号会拉回上千行 ——
// 传输和渲染都白做了，用户在同一屏里也看不完。
const usageBreakdownLimit = 100

// getBreakdownUsage 按 model / session 聚合 usage_events。
//
// 与按日聚合的关键区别：这里统计的是**真实 token 消耗**，不含充值流水。
// 因此返回体里没有 total_added —— 在"钱花在哪"的视角下它没有意义，返回一个恒为 0
// 的字段只会让人以为数据坏了。
//
// 为兼容已经依赖 `daily` 的老前端，非 day 分组时仍返回一个空 `daily` 数组，
// 而不是省略该键：省略会让 `usage.daily.map(...)` 直接抛错。
//
// ## 截断时总量为什么仍然准确
//
// 结果最多返回 usageBreakdownLimit 行，但 total_* 用窗口函数 `SUM(...) OVER ()`
// 在**聚合后的全部行**上求和（窗口函数先于 LIMIT 执行）。若改用「把返回行相加」，
// 截断后总量会小于真实花费，用户拿明细对不上账。total_buckets 同理来自
// `COUNT(*) OVER ()`，因此 truncated 判定也不需要额外一次查询。
// usageBreakdownQuery 是按 model / session 聚合的语句模板（%s 来自白名单列表达式）。
//
// 提成包级常量是为了让真实 PostgreSQL 冒烟用例能直接引用**生产语句本身** ——
// 若测试里另抄一份，两边一旦漂移，就会出现"测试全绿、上线才炸"。
const usageBreakdownQuery = `WITH agg AS (
			    SELECT %s AS bucket,
			           COUNT(*) AS event_count,
			           COALESCE(SUM(input_tokens), 0) AS input_tokens,
			           COALESCE(SUM(output_tokens), 0) AS output_tokens,
			           COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
			           COALESCE(SUM(cost_cents), 0) AS cost_cents
			      FROM usage_events
			     WHERE user_id = $1 AND created_at >= NOW() - make_interval(days => $2)
			     GROUP BY bucket
			 )
			 SELECT bucket, event_count, input_tokens, output_tokens, cached_tokens, cost_cents,
			        COALESCE(SUM(input_tokens) OVER (), 0) AS grand_input,
			        COALESCE(SUM(output_tokens) OVER (), 0) AS grand_output,
			        COALESCE(SUM(cached_tokens) OVER (), 0) AS grand_cached,
			        COALESCE(SUM(cost_cents) OVER (), 0) AS grand_cost,
			        COUNT(*) OVER () AS total_buckets
			   FROM agg
			  ORDER BY (input_tokens + output_tokens) DESC
			  LIMIT $3`


func (h *BillingHandler) Recharge(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if !auth.HasPermission(claims, auth.PermAdminWrite) {
		Forbidden(w, "admin permission required")
		return
	}
	userID := claims.UserID

	var body struct {
		Amount int `json:"amount"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.Amount <= 0 {
		BadRequest(w, "amount > 0 required")
		return
	}

	balance, err := h.mgr.AddCredits(userID, "recharge", body.Amount)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "internal error")
		return
	}

	OK(w, map[string]interface{}{"user_id": userID, "amount": body.Amount, "balance": balance})
}

func (h *BillingHandler) resolveUserID(r *http.Request) string {
	claims := auth.GetClaims(r.Context())
	if claims != nil {
		return claims.UserID
	}
	return ""
}

// ── 支付下单 / 查询 / 回调 ──────────────────────────────────────────────

// CreatePayment 创建充值订单并调用支付渠道预下单。
// body: {credits: int, channel: "alipay" | "wechat" | "paypal"}
// 返回订单信息（alipay/wechat 含 qr_code 二维码内容；paypal 含 checkout_url）。
func (h *BillingHandler) CreatePayment(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	userID := claims.UserID

	var body struct {
		Credits int    `json:"credits"`
		Channel string `json:"channel"`
		// PackageCode 非空表示购买套餐：此时 credits / 金额 / 币种一律取自套餐目录，
		// **忽略** body.Credits —— 否则用户可以用 1 分钱买下任意数量的 credits。
		PackageCode string `json:"package_code"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	switch body.Channel {
	case billing.ChannelAlipay, billing.ChannelWechat, billing.ChannelPayPal, "":
		// 空 channel 只对 0 元套餐合法 —— 查完套餐目录后再判定。
	default:
		BadRequest(w, "channel must be alipay / wechat / paypal")
		return
	}
	// 支付配置按租户一套：租户单独配置优先，未配置回退全局默认。
	tenantID := 0
	if claims != nil {
		tenantID = int(claims.TenantID)
	}

	credits := body.Credits
	// 1 credit = 1 分；支付宝/微信为人民币，PayPal 为美元
	amountCents := int64(credits)
	currency := "CNY"
	if body.Channel == billing.ChannelPayPal {
		currency = "USD"
	}

	// 套餐订单：价格与 credits 全部以目录为准，请求体只用来选套餐 code。
	var pkg *billing.Package
	if code := strings.TrimSpace(body.PackageCode); code != "" {
		loaded, err := h.mgr.PackageByCode(r.Context(), tenantID, code)
		if err != nil {
			slog.Error("load package failed", "package", code, "error", err)
			InternalError(w, "failed to load package")
			return
		}
		if loaded == nil || !loaded.Enabled {
			BadRequest(w, "套餐不存在或已下架")
			return
		}
		pkg = loaded
		credits = pkg.Credits
		amountCents = pkg.PriceCents
		currency = pkg.Currency
	} else if credits <= 0 {
		BadRequest(w, "credits > 0 required")
		return
	}

	// 0 元套餐（试用包）不走任何支付渠道。
	//
	// 支付宝/微信/PayPal 都不受理 0 元交易：塞进正常流程只会落一笔永远付不掉的
	// 订单（用户看到"待支付"却拿不到可扫的码）。所以这里直接落库并确认入账。
	if pkg != nil && pkg.PriceCents == 0 {
		h.claimFreePackage(w, r, claims, pkg)
		return
	}

	if body.Channel == "" {
		BadRequest(w, "channel required")
		return
	}
	pcfg := h.paymentConfigForTenant(r.Context(), tenantID)

	// 后台可将某个渠道停用（不改动其凭据），此时直接拒绝下单，避免落一笔永远付不掉的订单。
	if !pcfg.ChannelEnabled(body.Channel) {
		JSON(w, http.StatusNotImplemented, APIResponse{Success: false, Error: "该支付渠道已停用"})
		return
	}
	// 套餐自带币种必须与渠道收单币种一致：目录里配错币种时明确拒绝，
	// 而不是按 1:1 换算悄悄多收/少收钱。
	if pkg != nil {
		wantCurrency := "CNY"
		if body.Channel == billing.ChannelPayPal {
			wantCurrency = "USD"
		}
		if pkg.Currency != wantCurrency {
			BadRequest(w, fmt.Sprintf("套餐 %s 的币种为 %s，与渠道 %s 要求不一致",
				pkg.Code, pkg.Currency, body.Channel))
			return
		}
	}

	p := billing.NewPayment(userID, body.Channel, credits, amountCents, currency)
	p.PackageCode = strings.TrimSpace(body.PackageCode)
	if err := h.mgr.CreatePayment(r.Context(), p); err != nil {
		slog.Error("create payment failed", "error", err)
		InternalError(w, "failed to create payment order")
		return
	}

	ctx := r.Context()
	// 主题必须用**订单实际额度**（credits），不能用请求体的 body.Credits：
	// 买套餐时 body.Credits 通常是 0 或任意值，用户会在收银台看到
	// "充值 0 credits"这种与实付金额对不上的说明。
	subject := fmt.Sprintf("chiron 充值 %d credits", credits)
	if pkg != nil {
		subject = fmt.Sprintf("chiron 套餐 %s（%d credits）", pkg.Name, credits)
	}
	switch body.Channel {
	case billing.ChannelAlipay:
		alipay, _, _ := buildPaymentClients(pcfg)
		if alipay == nil {
			JSON(w, http.StatusNotImplemented, APIResponse{Success: false, Error: "支付宝支付未配置（后台「支付配置」或 ALIPAY_* 环境变量）"})
			return
		}
		qr, err := alipay.Precreate(ctx, p.ID, amountCents, subject)
		if err != nil {
			if mErr := h.mgr.MarkPaymentFailed(ctx, p.ID); mErr != nil {
				slog.Error("payment status update failed", "error", mErr)
			}
			// 把渠道给出的具体原因一并返回（前端显示在括号里）。只回"支付下单失败"
			// 会让用户与运维都无从下手 —— 例如网关配错时返回非 JSON 的情况。
			logAndRespond(w, err, http.StatusInternalServerError, "支付下单失败："+err.Error())
			return
		}
		if err := h.mgr.UpdatePaymentProvider(ctx, p.ID, qr, p.ID); err != nil {
			slog.Warn("update alipay payment provider", "error", err)
		}
		p.QRCode = qr

	case billing.ChannelWechat:
		_, wechat, _ := buildPaymentClients(pcfg)
		if wechat == nil {
			JSON(w, http.StatusNotImplemented, APIResponse{Success: false, Error: "微信支付未配置（后台「支付配置」或 WXPAY_* 环境变量）"})
			return
		}
		notifyURL := pcfg.WechatNotifyURL()
		if notifyURL == "" {
			JSON(w, http.StatusNotImplemented, APIResponse{Success: false, Error: "公网基础 URL 未配置，无法接收微信回调"})
			return
		}
		codeURL, err := wechat.Precreate(ctx, p.ID, amountCents, subject, notifyURL)
		if err != nil {
			if mErr := h.mgr.MarkPaymentFailed(ctx, p.ID); mErr != nil {
				slog.Error("payment status update failed", "error", mErr)
			}
			logAndRespond(w, err, http.StatusInternalServerError, "微信下单失败："+err.Error())
			return
		}
		if err := h.mgr.UpdatePaymentProvider(ctx, p.ID, codeURL, p.ID); err != nil {
			slog.Warn("update wechat payment provider", "error", err)
		}
		p.QRCode = codeURL

	case billing.ChannelPayPal:
		h.createPayPalPayment(w, r, userID, p)
		return
	}

	OK(w, h.paymentResponse(p))
}

// claimFreePackage 领取 0 元套餐（试用包）。
//
// 为什么不走支付渠道：支付宝/微信/PayPal 都不受理 0 元交易，塞进正常流程只会
// 落一笔永远付不掉的订单。这里直接落库 + 确认入账，链路其余部分（入 credits、
// 发权益快照、发站内信）与付费订单**完全一致** —— 复用 `ConfirmPayment`，
// 而不是另写一份"免费发放"逻辑：两份实现迟早会在幂等/通知上分叉。
//
// 防重复领取：免费包不涉及金额，唯一性只能靠业务判断。这里查
// `user_entitlements(user_id, package_code)`，同一用户同一免费套餐只能领一次
// （无论是否已过期）—— 否则"过期就再领一次"会成为无成本的永久续期。
func (h *BillingHandler) claimFreePackage(
	w http.ResponseWriter, r *http.Request, claims *auth.Claims, pkg *billing.Package,
) {
	userID := claims.UserID

	// 已领取过（含已过期）就拒绝：免费包的价值就在于"只给一次"。
	var claimed bool
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT EXISTS(
		    SELECT 1 FROM user_entitlements
		     WHERE user_id = $1 AND package_code = $2)`,
		userID, pkg.Code).Scan(&claimed); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "check free package claim failed")
		return
	}
	if claimed {
		JSONWithCode(w, http.StatusConflict, CodePackageAlreadyClaimed,
			map[string]interface{}{"package_code": pkg.Code},
			"this free package has already been claimed")
		return
	}

	p := billing.NewPayment(userID, billing.ChannelFree, pkg.Credits, 0, pkg.Currency)
	p.PackageCode = pkg.Code
	if err := h.mgr.CreatePayment(r.Context(), p); err != nil {
		// 并发下两次点击都可能通过上面的存在性检查，最终由部分唯一索引
		// `ux_payments_free_package_per_user` 挡住后到的那一次。
		// 这不是"服务出错"，而是"已经领过了"——必须回 409 而不是 500，
		// 否则前端会把它当成系统故障让用户重试，而重试永远不会成功。
		if isUniqueViolation(err) {
			JSONWithCode(w, http.StatusConflict, CodePackageAlreadyClaimed,
				map[string]interface{}{"package_code": pkg.Code},
				"this free package has already been claimed")
			return
		}
		slog.Error("create free package order failed",
			"user", userID, "package", pkg.Code, "error", err)
		InternalError(w, "failed to create package order")
		return
	}

	confirmed, _, err := h.mgr.ConfirmPayment(r.Context(), p.ID, "free_"+p.ID)
	if err != nil {
		slog.Error("confirm free package order failed",
			"order", p.ID, "user", userID, "package", pkg.Code, "error", err)
		InternalError(w, "failed to activate package")
		return
	}
	if confirmed == nil {
		confirmed = p
	}
	slog.Info("free package claimed",
		"order", p.ID, "user", userID, "package", pkg.Code, "credits", pkg.Credits)
	OK(w, h.paymentResponse(confirmed))
}

// GetOrder 查询订单状态（前端轮询）。pending 时主动向渠道查询一次兜底，
// 已支付则幂等入账并返回最新状态。
func (h *BillingHandler) GetOrder(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	id := r.PathValue("id")
	if id == "" {
		BadRequest(w, "order id required")
		return
	}
	ctx := r.Context()

	p, err := h.mgr.GetPayment(ctx, id)
	if err != nil || p == nil {
		NotFound(w, "payment order not found")
		return
	}
	if p.UserID != claims.UserID {
		Forbidden(w, "access denied")
		return
	}

	if p.Status == billing.PayStatusPending {
		// 渠道查询兜底（失败不阻塞响应）——按订单租户的配置构造客户端。
		_, alipay, wechat := h.clientsForPayment(ctx, p.ID)
		switch p.Channel {
		case billing.ChannelAlipay:
			if alipay != nil {
				if tradeNo, paid, qErr := alipay.Query(ctx, p.ID); qErr == nil && paid {
					if _, _, cErr := h.mgr.ConfirmPayment(ctx, p.ID, tradeNo); cErr != nil {
						slog.Warn("alipay query confirm failed", "error", cErr)
					}
				}
			}
		case billing.ChannelWechat:
			if wechat != nil {
				if tradeNo, paid, qErr := wechat.Query(ctx, p.ID); qErr == nil && paid {
					if _, _, cErr := h.mgr.ConfirmPayment(ctx, p.ID, tradeNo); cErr != nil {
						slog.Warn("wechat query confirm failed", "error", cErr)
					}
				}
			}
		}
		if fresh, fErr := h.mgr.GetPayment(ctx, id); fErr == nil && fresh != nil {
			p = fresh
		}
	}

	OK(w, h.paymentResponse(p))
}

// AlipayCallback 支付宝异步通知（无认证，靠签名验签）。
// 验签通过且金额一致则幂等入账；返回 "success"（支付宝协议要求）。
func (h *BillingHandler) AlipayCallback(w http.ResponseWriter, r *http.Request) {
	if err := r.ParseForm(); err != nil {
		BadRequest(w, "bad form")
		return
	}
	params := make(map[string]string, len(r.PostForm))
	for k, vs := range r.PostForm {
		if len(vs) > 0 {
			params[k] = vs[0]
		}
	}

	// 支付宝回调带明文 out_trade_no：先按订单反查租户，再用该租户的客户端验签。
	// 否则租户自己的商户号签出的回调会被全局凭据验签失败，用户永远到不了账。
	orderID := strings.TrimSpace(params["out_trade_no"])
	_, alipay, _ := h.clientsForPayment(r.Context(), orderID)
	if alipay == nil {
		JSON(w, http.StatusNotImplemented, APIResponse{Success: false, Error: "alipay not configured"})
		return
	}

	outTradeNo, tradeNo, ok := alipay.VerifyCallback(params)
	if !ok {
		slog.Warn("alipay callback verify failed")
		BadRequest(w, "signature verification failed")
		return
	}

	ctx := r.Context()
	p, err := h.mgr.GetPayment(ctx, outTradeNo)
	if err != nil || p == nil {
		slog.Warn("alipay callback unknown order", "out_trade_no", outTradeNo)
		BadRequest(w, "unknown order")
		return
	}

	// 金额校验：回调 total_amount（元）必须与订单一致。
	// 解析失败**即拒绝** —— 此前 aErr != nil 会整段跳过比对，攻击者只要构造一个不带
	// total_amount（或金额非数字）的回调就能绕过金额校验直接入账。
	amt, aErr := strconv.ParseFloat(params["total_amount"], 64)
	if aErr != nil {
		slog.Warn("alipay callback amount missing or unparseable",
			"order", p.ID, "raw", params["total_amount"])
		BadRequest(w, "amount missing")
		return
	}
	if int64(amt*100) != p.AmountCents {
		slog.Warn("alipay callback amount mismatch", "order", p.ID, "got", params["total_amount"], "want", p.AmountCents)
		BadRequest(w, "amount mismatch")
		return
	}

	if _, _, err := h.mgr.ConfirmPayment(ctx, p.ID, tradeNo); err != nil {
		slog.Error("alipay callback confirm failed", "error", err)
		InternalError(w, "confirm failed")
		return
	}
	slog.Info("alipay payment confirmed", "order", p.ID, "user", p.UserID, "credits", p.Credits)

	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.Write([]byte("success"))
}

// WechatCallback 微信支付回调（无认证，靠平台证书验签 + AES-GCM 解密）。
// 验签/金额校验通过则幂等入账；返回微信要求的 {"code":"SUCCESS"}。
func (h *BillingHandler) WechatCallback(w http.ResponseWriter, r *http.Request) {
	// 微信 V3 回调 body 是加密的，验签前拿不到订单号 → 依次尝试全局与各租户客户端。
	// body 只能读一次，因此先缓存原始字节，每个候选客户端用一份新的 Body 重放。
	raw, readErr := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if readErr != nil {
		BadRequest(w, "invalid wechat callback")
		return
	}
	_ = r.Body.Close()

	candidates := make([]*billing.WechatClient, 0, 2)
	// 优先按 `Wechatpay-Serial` 索引 O(1) 定位（租户越多收益越明显）。
	if serial := strings.ToUpper(strings.TrimSpace(r.Header.Get("Wechatpay-Serial"))); serial != "" {
		if wc := h.wechatClientsBySerial(r.Context())[serial]; wc != nil {
			candidates = append(candidates, wc)
		}
	}
	// 未命中（旧版回调 / 证书序列号未配置）才回退到遍历全局 + 各租户。
	if len(candidates) == 0 {
		if _, wc, _ := buildPaymentClients(h.paymentConfig()); wc != nil {
			candidates = append(candidates, wc)
		}
		if h.settingsStore != nil {
			if ids, err := h.settingsStore.TenantIDsWithConfig(r.Context(), settingsCategoryPayment); err == nil {
				for _, tenantID := range ids {
					if _, wc, _ := buildPaymentClients(h.paymentConfigForTenant(r.Context(), tenantID)); wc != nil {
						candidates = append(candidates, wc)
					}
				}
			}
		}
	}
	if len(candidates) == 0 {
		JSON(w, http.StatusNotImplemented, APIResponse{Success: false, Error: "wechat not configured"})
		return
	}

	var (
		outTradeNo  string
		tradeNo     string
		paid        bool
		amountCents *int64
		parseErr    error
	)
	for _, wc := range candidates {
		req := r.Clone(r.Context())
		req.Body = io.NopCloser(bytes.NewReader(raw))
		outTradeNo, tradeNo, paid, amountCents, parseErr = wc.ParseCallback(req)
		if parseErr == nil {
			break
		}
	}
	if parseErr != nil {
		slog.Warn("wechat callback parse failed", "error", parseErr)
		BadRequest(w, "invalid wechat callback")
		return
	}
	ctx := r.Context()
	// 二次校验：验签通过的客户端必须属于**订单所属租户**。
	// 微信 V3 回调的订单号在解密后才可见，所以先解密、再按订单租户重放一次验签；
	// 否则 A 租户的证书签出的回调（若能构造出 B 租户的 out_trade_no）会被当作有效。
	// 订单租户未单独配置时 paymentConfigForTenant 会回退全局，这正是下单时用的配置。
	orderTenantID := h.tenantIDForPayment(ctx, outTradeNo)
	_, orderWC, _ := buildPaymentClients(h.paymentConfigForTenant(ctx, orderTenantID))
	if orderWC == nil {
		slog.Warn("wechat callback order tenant payment not configured",
			"order", outTradeNo, "tenant", orderTenantID)
		BadRequest(w, "payment not configured for order tenant")
		return
	}
	verifyReq := r.Clone(ctx)
	verifyReq.Body = io.NopCloser(bytes.NewReader(raw))
	if _, _, _, _, err := orderWC.ParseCallback(verifyReq); err != nil {
		slog.Warn("wechat callback signature does not match order tenant",
			"order", outTradeNo, "tenant", orderTenantID)
		BadRequest(w, "signature does not match order tenant")
		return
	}
	p, err := h.mgr.GetPayment(ctx, outTradeNo)
	if err != nil || p == nil {
		slog.Warn("wechat callback unknown order", "out_trade_no", outTradeNo)
		BadRequest(w, "unknown order")
		return
	}
	// 金额缺失即拒绝 —— 此前 amountCents == nil 会跳过比对，构造不带金额的回调即可绕过。
	if amountCents == nil {
		slog.Warn("wechat callback amount missing", "order", p.ID)
		BadRequest(w, "amount missing")
		return
	}
	if *amountCents != p.AmountCents {
		slog.Warn("wechat callback amount mismatch", "order", p.ID, "got", *amountCents, "want", p.AmountCents)
		BadRequest(w, "amount mismatch")
		return
	}
	if !paid {
		// 未支付成功（如 USERPAYING），不处理但正常应答
		OK(w, map[string]string{"code": "SUCCESS", "message": "成功"})
		return
	}
	if _, _, err := h.mgr.ConfirmPayment(ctx, p.ID, tradeNo); err != nil {
		slog.Error("wechat callback confirm failed", "error", err)
		InternalError(w, "confirm failed")
		return
	}
	slog.Info("wechat payment confirmed", "order", p.ID, "user", p.UserID, "credits", p.Credits)
	OK(w, map[string]string{"code": "SUCCESS", "message": "成功"})
}

// paymentResponse 序列化订单（不暴露内部字段）。
func (h *BillingHandler) paymentResponse(p *billing.Payment) map[string]interface{} {
	resp := map[string]interface{}{
		"id":           p.ID,
		"channel":      p.Channel,
		"credits":      p.Credits,
		"amount_cents": p.AmountCents,
		"currency":     p.Currency,
		"status":       p.Status,
		"created_at":   p.CreatedAt,
	}
	// 套餐订单要回显 package_code，否则订单详情看不出买的是哪个套餐 ——
	// 用户拿到的只是一笔"金额恰好相等"的充值，无法与套餐目录对账。
	// 纯充值订单该字段为空字符串，按既有口径省略，不改变存量响应形状。
	if p.PackageCode != "" {
		resp["package_code"] = p.PackageCode
	}
	if p.QRCode != "" {
		resp["qr_code"] = p.QRCode
	}
	return resp
}
