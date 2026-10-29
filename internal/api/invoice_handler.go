package api

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/id"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// invoice_handler.go —— 工单式发票申请（roadmap §11.4 / B2）。
//
// 现状（已核实）：`payments` 表早已存在且状态机完整（pending/paid/failed），
// 但全仓**没有任何开票实现** —— 用户要报销只能找人工。这个 handler 补的是
// 「申请 → 审核 → 线下开票」的流转，不接第三方开票平台（那涉及资质与对账，
// 是产品与合规问题，不是工程问题）。
//
// 一期口径（§11.4）：**全额开票 + 不可撤销**。部分开票需要"一张支付拆多条发票 +
// 剩余可开金额"这套账，退款后已开票的处理也要产品定义；没有真实诉求前不实现。
//
// 三条不可动摇的约定：
//  1. **校验与写入在同一条语句里**（`INSERT ... SELECT ... FROM payments WHERE ...`）。
//     "先查后插"在并发下两个请求都能查到"没有"，然后都插进去；而唯一索引只能
//     挡住其中一次 —— 挡住的那次会以 23505 的形式暴露成一个无从解释的错误。
//  2. **重复申请靠唯一索引判 23505**，不做先查后插。这与注册邮箱判重同款做法。
//  3. **越权一律 404**：403 会确认"这个 payment 存在"，是可枚举性泄漏。

const (
	// invoiceTitleMaxLen 与 `invoice_requests.title` 的 varchar(255) 对齐。
	// 按**字符**计（PostgreSQL 的 varchar(n) 也是按字符计的）。
	invoiceTitleMaxLen = 255
	// invoiceTaxNoMaxLen 与 `invoice_requests.tax_no` 的 varchar(64) 对齐。
	invoiceTaxNoMaxLen = 64
	// invoiceNoteMaxLen 与 `invoice_requests.note` 的 text 列不对齐 —— text 没有上限，
	// 但申请备注是给人看的，限制长度是为了不让一行备注把审核列表挤爆。
	invoiceNoteMaxLen = 500
)

// 审核结果只允许两个取值。`pending` 不是可选项：审核动作的结果不可能还是待审。
const (
	invoiceStatusPending  = "pending"
	invoiceStatusApproved = "approved"
	invoiceStatusRejected = "rejected"
)

// invoiceStore 是该 handler 需要的最小数据库能力（与 inviteStore / apiKeyStore 同款理由：
// 收窄成接口后单测能覆盖分支，不需要连真实数据库，而仓库没有 pgxmock/sqlmock）。
type invoiceStore interface {
	Query(ctx context.Context, sql string, args ...interface{}) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...interface{}) pgx.Row
	Exec(ctx context.Context, sql string, args ...interface{}) (pgconn.CommandTag, error)
}

// InvoiceRequest 是返回给客户端的发票申请。
//
// 不含 `reviewed_by`：审核人是内部账号，对申请人没有意义，泄漏出去只是多一条
// 可做社交工程的信息。
type InvoiceRequest struct {
	ID          string     `json:"id"`
	PaymentID   string     `json:"payment_id"`
	Title       string     `json:"title"`
	TaxNo       string     `json:"tax_no"`
	AmountCents int64      `json:"amount_cents"`
	Currency    string     `json:"currency"`
	Status      string     `json:"status"`
	Note        string     `json:"note,omitempty"`
	ReviewNote  string     `json:"review_note,omitempty"`
	ReviewedAt  *time.Time `json:"reviewed_at,omitempty"`
	CreatedAt   *time.Time `json:"created_at,omitempty"`
}

type InvoiceHandler struct {
	store invoiceStore
}

func NewInvoiceHandler() *InvoiceHandler {
	return &InvoiceHandler{store: db.GlobalDBManager}
}

// RegisterRoutes 注册**用户侧**端点（挂在 /v1/billing 下，与充值/账单同一片）。
func (h *InvoiceHandler) RegisterRoutes(mux *http.ServeMux, authMW, rlMW routeMiddleware) {
	mux.Handle("GET /v1/billing/invoices", authMW(rlMW(http.HandlerFunc(h.List))))
	mux.Handle("POST /v1/billing/invoices", authMW(rlMW(http.HandlerFunc(h.Create))))
	// 可开票订单：用户要选"对哪一笔开票"，而 /v1/billing/history 是积分流水
	// （credit_transactions），不含 payment_id；PaymentStore 也没有按用户列表能力。
	mux.Handle("GET /v1/billing/invoiceable-orders", authMW(rlMW(http.HandlerFunc(h.ListInvoiceableOrders))))
}

// RegisterAdminRoutes 注册**管理端**审核端点。
//
// 挂到 adminMux 而不是外层 mux：外层已经做了 `StripPrefix("/v1/admin", adminMux)`，
// 在此注册才能拿到正确的 `{id}` 路径参数。
func (h *InvoiceHandler) RegisterAdminRoutes(adminMux *http.ServeMux) {
	adminMux.HandleFunc("GET /invoices", h.AdminList)
	adminMux.HandleFunc("POST /invoices/{id}/review", h.Review)
}

// ── 纯函数 ──

// normalizeInvoiceText 校验并规范化一个必填文本字段。
//
// 空值一律拒绝：发票抬头是开票的必要信息，留空只会让管理员来回追问。
func normalizeInvoiceText(field, raw string, maxLen int) (string, error) {
	v := strings.TrimSpace(raw)
	if v == "" {
		return "", fmt.Errorf("%s is required", field)
	}
	if n := len([]rune(v)); n > maxLen {
		return "", fmt.Errorf("%s must be at most %d characters", field, maxLen)
	}
	return v, nil
}

// normalizeReviewStatus 校验审核结论，只接受 approved / rejected。
//
// 不接受 pending：审核动作的结果不可能是"待审"。允许它会让一条 UPDATE 把
// 已审的行改回待审，审核历史就此失真。
func normalizeReviewStatus(raw string) (string, error) {
	switch strings.TrimSpace(raw) {
	case invoiceStatusApproved:
		return invoiceStatusApproved, nil
	case invoiceStatusRejected:
		return invoiceStatusRejected, nil
	default:
		return "", fmt.Errorf("status must be %q or %q", invoiceStatusApproved, invoiceStatusRejected)
	}
}

// normalizeInvoiceStatusFilter 校验管理端列表的状态过滤值。
// 空串 = 全部；非法值明确拒绝而不是静默回落成"全部"（否则管理员以为在筛选，
// 实际看到的是全量，容易漏处理）。
func normalizeInvoiceStatusFilter(raw string) (string, error) {
	switch v := strings.TrimSpace(raw); v {
	case "", "all":
		return "", nil
	case invoiceStatusPending, invoiceStatusApproved, invoiceStatusRejected:
		return v, nil
	default:
		return "", fmt.Errorf("unknown status filter %q", raw)
	}
}

// ── 用户侧 ──

// applyInvoiceQuery 是申请写入的唯一语句。
//
// `FROM payments p WHERE p.id = $3 AND p.user_id = $2 AND p.status = 'paid'`
// 一次完成三件事：存在性、归属、可开票状态。任何一项不满足都影响 0 行 ——
// 不存在一个"校验通过但写入失败"的窗口。
//
// `amount_cents IS NOT NULL AND > 0` 也在 WHERE 里：金额取自支付记录本身
// （快照），NULL 或 0 的订单开出来的票没有意义，让它同样落到"影响 0 行"。
//
// 参数必须逐个显式 cast：`$2` 同时出现在 SELECT 列表和 `p.user_id = $2` 两处，
// PostgreSQL 在 `INSERT ... SELECT` 里会为同一个未定型参数做**两次独立推断**，
// 一旦两处上下文给出的类型不一致就报 `42P08 inconsistent types deduced for
// parameter $2`（真库实测踩到，申请发票直接 500）。统一钉死类型后只剩一条推断路径。
const applyInvoiceQuery = `INSERT INTO invoice_requests
    (id, user_id, payment_id, title, tax_no, amount_cents, currency, status, note, created_at)
SELECT $1::varchar, $2::varchar, p.id, $4::varchar, $5::varchar,
       p.amount_cents, COALESCE(NULLIF(p.currency, ''), 'CNY'),
       'pending', NULLIF($6::text, ''), now()
  FROM payments p
 WHERE p.id = $3::varchar
   AND p.user_id = $2::varchar
   AND p.status = 'paid'
   AND p.amount_cents IS NOT NULL
   AND p.amount_cents > 0`

// Create 提交一张发票申请。
func (h *InvoiceHandler) Create(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	var body struct {
		PaymentID string `json:"payment_id"`
		Title     string `json:"title"`
		TaxNo     string `json:"tax_no"`
		Note      string `json:"note"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}

	paymentID := strings.TrimSpace(body.PaymentID)
	if paymentID == "" {
		BadRequest(w, "payment_id is required")
		return
	}
	title, err := normalizeInvoiceText("title", body.Title, invoiceTitleMaxLen)
	if err != nil {
		BadRequest(w, err.Error())
		return
	}
	taxNo, err := normalizeInvoiceText("tax_no", body.TaxNo, invoiceTaxNoMaxLen)
	if err != nil {
		BadRequest(w, err.Error())
		return
	}
	note := strings.TrimSpace(body.Note)
	if len([]rune(note)) > invoiceNoteMaxLen {
		BadRequest(w, fmt.Sprintf("note must be at most %d characters", invoiceNoteMaxLen))
		return
	}

	invoiceID, err := id.UUID()
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "generate invoice id failed")
		return
	}

	tag, err := h.store.Exec(r.Context(), applyInvoiceQuery,
		invoiceID, claims.UserID, paymentID, title, taxNo, note)
	if err != nil {
		// 唯一索引 ux_invoice_requests_payment：同一笔支付已经开过票。
		// 这里不做"先查后插"，所以并发下的双提交也只有一次能插进去。
		if isUniqueViolation(err) {
			JSONWithCode(w, http.StatusConflict, CodeInvoiceAlreadyRequested,
				map[string]interface{}{"payment_id": paymentID},
				"an invoice has already been requested for this payment")
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "create invoice request failed")
		return
	}

	if tag.RowsAffected() == 0 {
		// 写入没发生 —— 但原因有几种，直接回 400 会让用户不知道该怎么办。
		// 这一步**只做诊断**，不参与写入决策（写入决策已经由上面那条语句原子完成），
		// 所以不存在"先查后插"的并发问题。
		h.explainApplyFailure(w, r, claims.UserID, paymentID)
		return
	}

	req, err := h.loadOne(r.Context(), invoiceID, claims.UserID)
	if err != nil {
		// 申请已经落库，回查失败不该让用户以为没提交成功。
		slog.Warn("invoice created but reload failed", "user_id", claims.UserID, "invoice_id", invoiceID, "error", err)
		OK(w, InvoiceRequest{
			ID:        invoiceID,
			PaymentID: paymentID,
			Title:     title,
			TaxNo:     taxNo,
			Status:    invoiceStatusPending,
			Note:      note,
		})
		return
	}

	slog.Info("invoice requested", "user_id", claims.UserID, "invoice_id", invoiceID, "payment_id", paymentID)
	Created(w, req)
}

// explainApplyFailure 把"影响 0 行"翻译成用户能据此行动的文案。
//
// 归属不符一律回 404（与"支付不存在"同一个响应）：区分它们等于告诉调用方
// "这个 payment_id 确实存在，只是不是你的"。
func (h *InvoiceHandler) explainApplyFailure(w http.ResponseWriter, r *http.Request, userID, paymentID string) {
	var ownerID *string
	var status *string
	var amount *int64
	err := h.store.QueryRow(r.Context(),
		`SELECT user_id, status, amount_cents FROM payments WHERE id = $1`, paymentID).
		Scan(&ownerID, &status, &amount)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			NotFound(w, "payment not found")
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "load payment failed")
		return
	}
	if ownerID == nil || *ownerID != userID {
		NotFound(w, "payment not found")
		return
	}
	if status == nil || *status != "paid" {
		BadRequest(w, "only paid orders can be invoiced")
		return
	}
	// 到这里只可能是金额无效（NULL 或 <= 0）。
	BadRequest(w, "this order has no invoiceable amount")
}

// List 返回当前用户的全部发票申请（含已驳回的）。
//
// 驳回的记录**保留展示**：用户需要看到"这笔我没开成、以及为什么"，
// 否则会反复提交同一笔，每次都被 409 拦下而不知道原因。
func (h *InvoiceHandler) List(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	rows, err := h.store.Query(r.Context(),
		`SELECT id, payment_id, title, tax_no, amount_cents,
		        COALESCE(NULLIF(currency, ''), 'CNY'), status,
		        COALESCE(note, ''), COALESCE(review_note, ''), reviewed_at, created_at
		   FROM invoice_requests
		  WHERE user_id = $1
		  ORDER BY created_at DESC NULLS LAST, id DESC`,
		claims.UserID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list invoices failed")
		return
	}
	defer rows.Close()

	out := make([]InvoiceRequest, 0, 8)
	for rows.Next() {
		var it InvoiceRequest
		if err := rows.Scan(&it.ID, &it.PaymentID, &it.Title, &it.TaxNo, &it.AmountCents,
			&it.Currency, &it.Status, &it.Note, &it.ReviewNote, &it.ReviewedAt, &it.CreatedAt); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "scan invoice failed")
			return
		}
		out = append(out, it)
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list invoices failed")
		return
	}

	OK(w, out)
}

// InvoiceableOrder 是"可以开票的订单"的展示用投影。
//
// 只给前端选择订单所需的最小信息：id 用来提交申请，金额与时间用来辨认是哪一笔。
// 不返回 `trade_no` / `provider_order_id` / `qr_code` 这些渠道内部字段 —— 它们是
// 资金链路的技术细节，露给前端只是徒增泄漏面。
type InvoiceableOrder struct {
	ID          string     `json:"id"`
	AmountCents int64      `json:"amount_cents"`
	Currency    string     `json:"currency"`
	Credits     int        `json:"credits"`
	PaidAt      *time.Time `json:"paid_at,omitempty"`
}

// invoiceableOrdersQuery 只取**当前用户**的**已支付**订单。
//
// `NOT EXISTS (... invoice_requests ...)` 把已经申请过的排除掉：用户界面上的
// 下拉一旦列出已经开过票的订单，用户就会点它，然后收到 409 —— 309 的文案再好，
// 也不如一开始就不列出来。唯一索引仍然是最终裁决（并发下两个人同时提交时
// 由它挡住），这里只是把主路径上无意义的选择删掉。
const invoiceableOrdersQuery = `SELECT p.id,
       p.amount_cents,
       COALESCE(NULLIF(p.currency, ''), 'CNY'),
       COALESCE(p.credits, 0),
       p.paid_at
  FROM payments p
 WHERE p.user_id = $1
   AND p.status = 'paid'
   AND p.amount_cents IS NOT NULL
   AND p.amount_cents > 0
   AND NOT EXISTS (SELECT 1 FROM invoice_requests ir WHERE ir.payment_id = p.id)
 ORDER BY p.paid_at DESC NULLS LAST, p.created_at DESC NULLS LAST
 LIMIT 200`

// ListInvoiceableOrders 返回当前用户可以开票的订单（已支付、金额有效、尚未申请）。
func (h *InvoiceHandler) ListInvoiceableOrders(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	rows, err := h.store.Query(r.Context(), invoiceableOrdersQuery, claims.UserID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list invoiceable orders failed")
		return
	}
	defer rows.Close()

	out := make([]InvoiceableOrder, 0, 8)
	for rows.Next() {
		var o InvoiceableOrder
		if err := rows.Scan(&o.ID, &o.AmountCents, &o.Currency, &o.Credits, &o.PaidAt); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "scan invoiceable order failed")
			return
		}
		out = append(out, o)
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list invoiceable orders failed")
		return
	}

	OK(w, out)
}

// loadOne 回查单条申请（创建成功后返回完整记录用）。
func (h *InvoiceHandler) loadOne(ctx context.Context, invoiceID, userID string) (InvoiceRequest, error) {
	var it InvoiceRequest
	err := h.store.QueryRow(ctx,
		`SELECT id, payment_id, title, tax_no, amount_cents,
		        COALESCE(NULLIF(currency, ''), 'CNY'), status,
		        COALESCE(note, ''), COALESCE(review_note, ''), reviewed_at, created_at
		   FROM invoice_requests
		  WHERE id = $1 AND user_id = $2`,
		invoiceID, userID).
		Scan(&it.ID, &it.PaymentID, &it.Title, &it.TaxNo, &it.AmountCents,
			&it.Currency, &it.Status, &it.Note, &it.ReviewNote, &it.ReviewedAt, &it.CreatedAt)
	return it, err
}

// ── 管理端 ──

// AdminList 分页列出全部申请，可按状态过滤。
//
// 默认只返回待审（`status=pending`）：审核队列的默认视图就该是"等待我处理的事"，
// 而不是把历史全摊开。要看全量显式传 `status=all`。
func (h *InvoiceHandler) AdminList(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	status := strings.TrimSpace(q.Get("status"))
	// 默认待审；显式传 status= 空串也当待审（前端下拉默认项）。
	if status == "" {
		status = invoiceStatusPending
	}
	statusFilter, err := normalizeInvoiceStatusFilter(status)
	if err != nil {
		BadRequest(w, err.Error())
		return
	}

	sql := `SELECT id, payment_id, title, tax_no, amount_cents,
	               COALESCE(NULLIF(currency, ''), 'CNY'), status,
	               COALESCE(note, ''), COALESCE(review_note, ''), reviewed_at, created_at
	          FROM invoice_requests`
	args := []interface{}{}
	if statusFilter != "" {
		sql += ` WHERE status = $1`
		args = append(args, statusFilter)
	}
	sql += ` ORDER BY created_at DESC NULLS LAST, id DESC LIMIT 500`

	rows, err := h.store.Query(r.Context(), sql, args...)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list invoices failed")
		return
	}
	defer rows.Close()

	out := make([]InvoiceRequest, 0, 16)
	for rows.Next() {
		var it InvoiceRequest
		if err := rows.Scan(&it.ID, &it.PaymentID, &it.Title, &it.TaxNo, &it.AmountCents,
			&it.Currency, &it.Status, &it.Note, &it.ReviewNote, &it.ReviewedAt, &it.CreatedAt); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "scan invoice failed")
			return
		}
		out = append(out, it)
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list invoices failed")
		return
	}

	OK(w, map[string]interface{}{"invoices": out, "total": len(out)})
}

// Review 审核一张申请：pending → approved | rejected，**单向**。
//
// 状态流转限制写在 UPDATE 的 WHERE 里（`AND status = 'pending'`）而不是先读后判：
// 两个管理员同时点"通过"和"驳回"时，只有一个能生效，另一个影响 0 行并收到 409 ——
// 先读后判则会让后到的那个静默覆盖前一个的结论。
func (h *InvoiceHandler) Review(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	invoiceID := strings.TrimSpace(r.PathValue("id"))
	if invoiceID == "" {
		NotFound(w, ErrNotFound)
		return
	}

	var body struct {
		Status     string `json:"status"`
		ReviewNote string `json:"review_note"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	status, err := normalizeReviewStatus(body.Status)
	if err != nil {
		BadRequest(w, err.Error())
		return
	}
	reviewNote := strings.TrimSpace(body.ReviewNote)
	if len([]rune(reviewNote)) > invoiceNoteMaxLen {
		BadRequest(w, fmt.Sprintf("review_note must be at most %d characters", invoiceNoteMaxLen))
		return
	}

	tag, err := h.store.Exec(r.Context(),
		`UPDATE invoice_requests
		    SET status = $2, reviewed_by = $3, reviewed_at = now(), review_note = NULLIF($4, '')
		  WHERE id = $1 AND status = 'pending'`,
		invoiceID, status, claims.UserID, reviewNote)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "review invoice failed")
		return
	}
	if tag.RowsAffected() == 0 {
		// 0 行有两种可能：不存在，或已经审过。这里要区分 —— 与用户侧的支付
		// 归属不同，**申请 id 是随机 UUID 且审核本来就在管理端**，区分它不会
		// 泄漏给攻击者任何东西，却能让管理员知道"是不是有人先审了"。
		var exists bool
		if err := h.store.QueryRow(r.Context(),
			`SELECT EXISTS(SELECT 1 FROM invoice_requests WHERE id = $1)`, invoiceID).
			Scan(&exists); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "load invoice failed")
			return
		}
		if !exists {
			NotFound(w, "invoice request not found")
			return
		}
		JSONWithCode(w, http.StatusConflict, CodeInvoiceAlreadyReviewed,
			map[string]interface{}{"id": invoiceID},
			"this invoice request has already been reviewed")
		return
	}

	slog.Info("invoice reviewed", "reviewer_id", claims.UserID, "invoice_id", invoiceID, "status", status)
	OK(w, map[string]interface{}{"id": invoiceID, "status": status})
}
