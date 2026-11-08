package api

// quota_handler.go —— 租户配额管理（查看 → 申请 → 平台审批）。
//
// 背景：执行侧早已完备（ent_quota_enforce.go 的 token 预留 / credits 硬拦，
// tenant_resource_manager.go 的并发与存储闸门），但**没有任何管理入口** ——
// 管理员只能手工往 ent_quota_pools 插行。本文件补上这条管理链路。
//
// 三条不可动摇的约定：
//
//  1. **"已用量"必须与强制侧同源**。token/credits 超限时网关是真的在拦人，
//     所以面板上的百分比必须来自**同一张表/同一个周期起点**；否则会出现
//     "页面显示 40%、请求却被拒"这种无法解释的状态，用户只会来投诉。
//     → token 用 usage_events 聚合、credits 用 credit_transactions 扣减量、
//       storage 用 media_assets、并发没有"用量"概念（它是个上限，不是配额）。
//
//  2. **审批是一次性的状态推进**。`UPDATE ... WHERE status = 'pending'` 写在
//     SQL 里，两个管理员同时点"通过"只有一个生效，另一个收到 409 ——
//     先读后判会让后到的那个静默覆盖前一个的结论（与发票审核同款理由）。
//
//  3. **白名单收窄资源类型**。`ent_quota_pools.resource_type` 是 varchar(20)、
//     没有 CHECK 约束，放任任意字符串会让执行侧永远匹配不到，配额"配了不生效"。
//     这里只接受执行侧真正会读的四个值。

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
	"github.com/athenavi/chiron/internal/notify"
)

// 配额资源类型：只允许执行侧真正会读的四个值。
const (
	quotaResourceToken       = "token"
	quotaResourceCredits     = "credits"
	quotaResourceConcurrency = "concurrency"
	quotaResourceStorageMB   = "storage_mb"
)

// 周期取值。执行侧 quotaPeriodKey 只认 daily，其余一律按月度处理 ——
// 这里显式白名单，避免写进 daily/monthly 之外的值导致周期起点算错。
const (
	quotaPeriodDaily   = "daily"
	quotaPeriodMonthly = "monthly"
)

// 配额申请状态。
const (
	quotaRequestPending  = "pending"
	quotaRequestApproved = "approved"
	quotaRequestRejected = "rejected"
)

// maxQuotaAmount 是单个配额上限（1 万亿）。
//
// 不是业务限制，而是防溢出：`total_amount` 是 bigint，前端用 JS number
// 传输超过 2^53 会静默丢精度。给一个远大于任何真实配额的上界，
// 让"手滑多按了几个零"在写入前就被拒绝，而不是变成一个诡异的负数。
const maxQuotaAmount int64 = 1_000_000_000_000

// quotaReasonMaxLen 与申请表 reason 的 text 列不对齐（text 无上限），
// 限制长度是为了不让一行备注把审批列表挤爆（与发票备注同款理由）。
const quotaReasonMaxLen = 500

// QuotaPoolView 是配额池 + 当期用量的合并视图。
//
// 不复用 EntQuotaPool：那个结构体是存储层视图（tenant_id 是字符串），
// 而这里要给前端一个"已用量"与"是否超限"，两者字段不同。
type QuotaPoolView struct {
	ResourceType string `json:"resource_type"`
	Period       string `json:"period"`
	TotalAmount  int64  `json:"total_amount"`
	UsedAmount   int64  `json:"used_amount"`
	// UsageSource 标注已用量的来源（usage_events / credit_transactions /
	// media_assets / none）。排障时"为什么显示 0"必须能一眼看出是
	// 没有数据还是查询失败 —— 后者会显示 none。
	UsageSource string `json:"usage_source"`
	// Unlimited 是 total_amount <= 0 的显式标注：0 在执行侧表示"不限额"，
	// 前端若直接按 0 算百分比会显示 NaN。
	Unlimited bool `json:"unlimited"`
}

// QuotaRequest 是返回给前端的配额申请。
//
// 不含 reviewed_by：审核人是内部账号，对申请人没有意义（与发票申请同款取舍）。
type QuotaRequest struct {
	ID              string     `json:"id"`
	TenantID        int16      `json:"tenant_id"`
	ResourceType    string     `json:"resource_type"`
	Period          string     `json:"period"`
	CurrentAmount   int64      `json:"current_amount"`
	RequestedAmount int64      `json:"requested_amount"`
	Reason          string     `json:"reason,omitempty"`
	Status          string     `json:"status"`
	ReviewNote      string     `json:"review_note,omitempty"`
	ReviewedAt      *time.Time `json:"reviewed_at,omitempty"`
	CreatedAt       *time.Time `json:"created_at,omitempty"`
}

type QuotaHandler struct {
	notifier notificationWriter
}

func NewQuotaHandler() *QuotaHandler { return &QuotaHandler{} }

// SetNotifier 装配通知中心：审批结果要通知申请人（P2）。
func (h *QuotaHandler) SetNotifier(w notificationWriter) { h.notifier = w }

// ── 纯函数（可单测） ──

// normalizeQuotaResource 校验资源类型白名单。
func normalizeQuotaResource(raw string) (string, error) {
	switch v := strings.ToLower(strings.TrimSpace(raw)); v {
	case quotaResourceToken, quotaResourceCredits,
		quotaResourceConcurrency, quotaResourceStorageMB:
		return v, nil
	default:
		return "", fmt.Errorf(
			"resource_type must be one of: token, credits, concurrency, storage_mb")
	}
}

// normalizeQuotaPeriod 校验周期；空值按月度（执行侧的默认语义）。
func normalizeQuotaPeriod(raw string) (string, error) {
	switch v := strings.ToLower(strings.TrimSpace(raw)); v {
	case "":
		return quotaPeriodMonthly, nil
	case quotaPeriodDaily, quotaPeriodMonthly:
		return v, nil
	default:
		return "", fmt.Errorf("period must be one of: daily, monthly")
	}
}

// normalizeQuotaStatusFilter 校验审批列表的状态过滤。
//
// 空串 = 待审（与发票审核同款：审核队列的默认视图是"等我处理的事"）。
func normalizeQuotaStatusFilter(raw string) (string, error) {
	switch v := strings.ToLower(strings.TrimSpace(raw)); v {
	case "", quotaRequestPending:
		return quotaRequestPending, nil
	case "all":
		return "", nil
	case quotaRequestApproved, quotaRequestRejected:
		return v, nil
	default:
		return "", fmt.Errorf("status must be one of: pending, approved, rejected, all")
	}
}

// validateQuotaAmount 校验目标配额数值。
//
// 允许 0（= 取消限制），但拒绝负数：负数在执行侧会被 `TotalAmount > 0` 判为
// "不限额"，于是管理员以为自己设了个极小值，实际是完全放开 —— 这是最危险的方向。
func validateQuotaAmount(v int64) error {
	if v < 0 {
		return errors.New("requested_amount must be >= 0 (0 means unlimited)")
	}
	if v > maxQuotaAmount {
		return fmt.Errorf("requested_amount must be <= %d", maxQuotaAmount)
	}
	return nil
}

// quotaPeriodStart 返回当期起点（UTC），与执行侧 quotaPeriodKey 的语义一致。
//
// 必须复用同一套边界：如果面板按「自然月本地时区」算、而强制按 UTC 月算，
// 每月 1 号凌晨会出现"面板清零了但请求仍被拦"的窗口。
func quotaPeriodStart(period string, now time.Time) time.Time {
	now = now.UTC()
	if period == quotaPeriodDaily {
		return time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, time.UTC)
	}
	return time.Date(now.Year(), now.Month(), 1, 0, 0, 0, 0, time.UTC)
}

// quotaUsageSQL 按资源类型给出当期用量查询（返回单个 int64）。
//
// 每条都必须与**强制侧**同源：
//   - token  → usage_events（引擎每轮写入真实 token）；强制侧的 Redis 计数器
//     也是由同一批数据回填，两者在稳态下一致。
//   - credits → credit_transactions 的扣减量，与 CreditUsageSQL 完全同源
//     （这是"已花到上限就拒绝"的判定依据，口径不能有一分钱偏差）。
//   - storage_mb → media_assets（与 storageUsageQuery 同源），但**单位是 MB**，
//     而表里存的是字节 —— 下面按 1024*1024 折算。
//   - concurrency → 没有"用量"概念（它是同时执行的实例上限），返回 nil
//     表示不查询，由调用方标注。
func quotaUsageSQL(resourceType string) (query string, isBytes bool) {
	switch resourceType {
	case quotaResourceToken:
		return `SELECT COALESCE(SUM(input_tokens + output_tokens), 0)
		          FROM usage_events
		         WHERE tenant_id = $1 AND created_at >= $2`, false
	case quotaResourceCredits:
		return `SELECT COALESCE(SUM(-ct.amount), 0)
		          FROM credit_transactions ct
		          JOIN users u ON u.id = ct.user_id
		         WHERE u.tenant_id = $1 AND ct.created_at >= $2 AND ct.amount < 0`, false
	case quotaResourceStorageMB:
		return `SELECT COALESCE(SUM(size), 0)
		          FROM media_assets
		         WHERE tenant_id = $1 AND type <> 'folder'`, true
	default:
		return "", false
	}
}

// quotaUsageSource 返回用量来源标签（排障用）。
func quotaUsageSource(resourceType string) string {
	switch resourceType {
	case quotaResourceToken:
		return "usage_events"
	case quotaResourceCredits:
		return "credit_transactions"
	case quotaResourceStorageMB:
		return "media_assets"
	default:
		return "none"
	}
}

// ── 租户侧 ──

// List GET /v1/tenant/quota —— 本租户的配额池与当期用量。
//
// 未配置任何配额池时返回空数组而不是 404：租户后台要能区分
// "没有限制"（空数组 + 前端提示）与"接口挂了"（5xx）。
func (h *QuotaHandler) List(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID <= 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	if db.Pool == nil {
		InternalError(w, "database unavailable")
		return
	}
	tenantID := claims.TenantID
	now := time.Now()

	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT resource_type, COALESCE(period, '') AS period, COALESCE(total_amount, 0)
		   FROM ent_quota_pools
		  WHERE tenant_id = $1
		  ORDER BY resource_type, period`, tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list tenant quota failed")
		return
	}
	defer rows.Close()

	out := make([]QuotaPoolView, 0, 4)
	for rows.Next() {
		var v QuotaPoolView
		if err := rows.Scan(&v.ResourceType, &v.Period, &v.TotalAmount); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "scan tenant quota failed")
			return
		}
		if v.Period == "" {
			v.Period = quotaPeriodMonthly
		}
		v.Unlimited = v.TotalAmount <= 0
		v.UsedAmount, v.UsageSource = h.usageFor(r.Context(), tenantID, v.ResourceType, v.Period, now)
		out = append(out, v)
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list tenant quota failed")
		return
	}
	OK(w, map[string]interface{}{"pools": out, "total": len(out)})
}

// usageFor 计算某资源在当期的已用量。
//
// 查询失败时返回 (0, "none") 而不是报错：面板上的单个数字查不出来，
// 不该让整页 500。真实原因记在日志里。
func (h *QuotaHandler) usageFor(
	ctx context.Context, tenantID int16, resourceType, period string, now time.Time,
) (int64, string) {
	query, isBytes := quotaUsageSQL(resourceType)
	if query == "" {
		// concurrency 没有"已用量"：它是并发上限而非累计配额。
		return 0, "none"
	}
	since := quotaPeriodStart(period, now)
	var used int64
	if err := db.GlobalDBManager.QueryRow(ctx, query, tenantID, since).Scan(&used); err != nil {
		slog.Warn("quota usage query failed",
			"tenant_id", tenantID, "resource", resourceType, "error", err)
		return 0, "none"
	}
	if isBytes {
		// 存储配额以 MB 计，表里是字节 —— 向上取整会让 1 字节显示成 1MB，
		// 所以用截断除法（宁可少报 1MB，也不要虚报）。
		used = used / (1024 * 1024)
	}
	return used, quotaUsageSource(resourceType)
}

// ListRequests GET /v1/tenant/quota/requests —— 本租户的申请历史。
func (h *QuotaHandler) ListRequests(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID <= 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	statusFilter, err := normalizeQuotaStatusFilter(r.URL.Query().Get("status"))
	if err != nil {
		BadRequest(w, err.Error())
		return
	}
	list, err := h.queryRequests(r.Context(), int(claims.TenantID), statusFilter, 100)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list quota requests failed")
		return
	}
	OK(w, map[string]interface{}{"requests": list, "total": len(list)})
}

// CreateRequest POST /v1/tenant/quota/requests —— 提交一笔配额调整申请。
//
// 同一 (租户, 资源, 周期) 已有待审申请时返回 409：否则申请人多点几次就会
// 在审批队列里堆出多条互相矛盾的请求，审批人无从判断该批哪一条。
func (h *QuotaHandler) CreateRequest(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID <= 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	var body struct {
		ResourceType    string `json:"resource_type"`
		Period          string `json:"period"`
		RequestedAmount int64  `json:"requested_amount"`
		Reason          string `json:"reason"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	resourceType, err := normalizeQuotaResource(body.ResourceType)
	if err != nil {
		BadRequest(w, err.Error())
		return
	}
	period, err := normalizeQuotaPeriod(body.Period)
	if err != nil {
		BadRequest(w, err.Error())
		return
	}
	if err := validateQuotaAmount(body.RequestedAmount); err != nil {
		BadRequest(w, err.Error())
		return
	}
	reason := strings.TrimSpace(body.Reason)
	if len([]rune(reason)) > quotaReasonMaxLen {
		BadRequest(w, fmt.Sprintf("reason must be at most %d characters", quotaReasonMaxLen))
		return
	}

	tenantID := claims.TenantID
	// 当前值取自实际配额池（0 = 当前不限额），作为申请单上的"基线"快照。
	// 不能实时 JOIN 读取：审批人需要看到"申请时是什么状态"，
	// 否则中间被人改过配额后，这张单子的上下文就永久丢失了。
	var current int64
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT COALESCE(total_amount, 0) FROM ent_quota_pools
		  WHERE tenant_id = $1 AND resource_type = $2 AND COALESCE(period, 'monthly') = $3
		  LIMIT 1`, tenantID, resourceType, period).Scan(&current); err != nil {
		// 没有配额池不是错误：当前就是"不限额"，current 保持 0。
		current = 0
	}

	id_, err := id.UUID()
	if err != nil {
		InternalError(w, "failed to allocate id")
		return
	}

	// 唯一性靠"先查 pending"：申请单没有可用的唯一索引（同一资源允许
	// 历史多条 approved/rejected），所以这里显式判重，并在冲突时给 409。
	var dup bool
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT EXISTS(
		    SELECT 1 FROM ent_quota_requests
		     WHERE tenant_id = $1 AND resource_type = $2 AND period = $3
		       AND status = 'pending')`,
		tenantID, resourceType, period).Scan(&dup); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "check pending quota request failed")
		return
	}
	if dup {
		JSONWithCode(w, http.StatusConflict, CodeQuotaRequestPending,
			map[string]interface{}{"resource_type": resourceType, "period": period},
			"a pending request for this resource already exists")
		return
	}

	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO ent_quota_requests
		    (id, tenant_id, resource_type, period, current_amount, requested_amount,
		     reason, status, requested_by, created_at)
		 VALUES ($1, $2, $3, $4, $5, $6, NULLIF($7, ''), 'pending', $8, now())`,
		id_, tenantID, resourceType, period, current, body.RequestedAmount, reason, claims.UserID); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create quota request failed")
		return
	}
	slog.Info("quota request created",
		"tenant_id", tenantID, "resource", resourceType, "period", period,
		"requested", body.RequestedAmount, "by", claims.UserID)
	OK(w, map[string]interface{}{
		"id":               id_,
		"resource_type":    resourceType,
		"period":           period,
		"current_amount":   current,
		"requested_amount": body.RequestedAmount,
		"status":           quotaRequestPending,
	})
}

// ── 平台侧（仅 owner，路由层已用 RequirePlatformOwner 收口） ──

// AdminListRequests GET /v1/admin/quota/requests?status=pending
func (h *QuotaHandler) AdminListRequests(w http.ResponseWriter, r *http.Request) {
	statusFilter, err := normalizeQuotaStatusFilter(r.URL.Query().Get("status"))
	if err != nil {
		BadRequest(w, err.Error())
		return
	}
	// tenantID=0 表示不按租户过滤（平台视图看全量）。
	list, err := h.queryRequests(r.Context(), 0, statusFilter, 200)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list quota requests failed")
		return
	}
	OK(w, map[string]interface{}{"requests": list, "total": len(list)})
}

// ReviewRequest POST /v1/admin/quota/requests/{id}/review
//
// 通过时执行两件事（顺序不能反）：
//  1. upsert `ent_quota_pools` —— 这是配额的权威来源；
//  2. 触发 `TenantResourceManager.SyncFromDB` —— 否则并发/存储闸门最多要等
//     一个同步周期（5 分钟）才生效，管理员会以为"批了没用"。
//
// token/credits 不需要额外动作：它们在每次请求时实时读池。
func (h *QuotaHandler) ReviewRequest(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	requestID := strings.TrimSpace(r.PathValue("id"))
	if requestID == "" {
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
	status := strings.ToLower(strings.TrimSpace(body.Status))
	if status != quotaRequestApproved && status != quotaRequestRejected {
		BadRequest(w, `status must be "approved" or "rejected"`)
		return
	}
	reviewNote := strings.TrimSpace(body.ReviewNote)
	if len([]rune(reviewNote)) > quotaReasonMaxLen {
		BadRequest(w, fmt.Sprintf("review_note must be at most %d characters", quotaReasonMaxLen))
		return
	}

	// 先读出申请单（需要 tenant/资源/目标值才能执行通过与通知）。
	// 读与改之间有竞态，但**状态推进本身是原子的**（UPDATE 带 status='pending'），
	// 所以最坏情况是有人先审了 → 下面 RowsAffected()==0 → 409。
	var (
		tenantID     int16
		resourceType string
		period       string
		requestedAmt int64
		requestedBy  string
		curStatus    string
		targetID     int16
	)
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT tenant_id, resource_type, period, requested_amount,
		        COALESCE(requested_by, ''), status
		   FROM ent_quota_requests WHERE id = $1`, requestID).
		Scan(&tenantID, &resourceType, &period, &requestedAmt, &requestedBy, &curStatus); err != nil {
		NotFound(w, "quota request not found")
		return
	}
	targetID = tenantID

	tag, err := db.GlobalDBManager.Exec(r.Context(),
		`UPDATE ent_quota_requests
		    SET status = $2, reviewed_by = $3, reviewed_at = now(), review_note = NULLIF($4, '')
		  WHERE id = $1 AND status = 'pending'`,
		requestID, status, claims.UserID, reviewNote)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "review quota request failed")
		return
	}
	if tag.RowsAffected() == 0 {
		JSONWithCode(w, http.StatusConflict, CodeQuotaRequestReviewed,
			map[string]interface{}{"id": requestID},
			"this quota request has already been reviewed")
		return
	}

	if status == quotaRequestApproved {
		if err := upsertQuotaPool(r.Context(), int(targetID), resourceType, period, requestedAmt); err != nil {
			// 配额没写进去就不该显示"已通过"：把状态退回 pending 让审批人重试。
			// 与支付入账失败回退订单同款取舍 —— 停在 approved 会让这笔申请
			// 永久失去重试入口。
			if _, rErr := db.GlobalDBManager.Exec(r.Context(),
				`UPDATE ent_quota_requests SET status = 'pending', reviewed_by = NULL,
				        reviewed_at = NULL WHERE id = $1`, requestID); rErr != nil {
				slog.Error("revert quota request to pending failed",
					"request_id", requestID, "error", rErr)
			}
			logAndRespond(w, err, http.StatusInternalServerError, "apply quota failed")
			return
		}
		// 并发/存储配额缓存需要显式重载，否则最多滞后一个同步周期才生效。
		reloadTenantResourceQuota(r.Context())
	}

	slog.Info("quota request reviewed",
		"request_id", requestID, "tenant_id", tenantID, "resource", resourceType,
		"status", status, "reviewer", claims.UserID)

	h.notifyQuotaReviewResult(r.Context(), tenantID, resourceType, period, requestedAmt, status, reviewNote, requestedBy)

	OK(w, map[string]interface{}{"id": requestID, "status": status})
}

// upsertQuotaPool 把审批通过的目标值写入配额池（权威来源）。
//
// 用 `ON CONFLICT` 而不是"先查后插/更新"：两个管理员同时给同一租户批两笔
// 不同资源的申请时，先查后写会让后一个把前一个覆盖掉（或撞主键）。
// 冲突目标必须与迁移里的**表达式唯一索引**逐字一致，否则 PostgreSQL 报 42P10
// （与 0014 的 ent_captcha_config 同一个坑）。
func upsertQuotaPool(ctx context.Context, tenantID int, resourceType, period string, amount int64) error {
	poolID, err := id.UUID()
	if err != nil {
		return fmt.Errorf("allocate quota pool id: %w", err)
	}
	_, err = db.GlobalDBManager.Exec(ctx,
		`INSERT INTO ent_quota_pools (id, tenant_id, resource_type, total_amount, period, created_at, updated_at)
		 VALUES ($1, $2, $3, $4, $5, now(), now())
		 ON CONFLICT (tenant_id, (COALESCE(resource_type, '')), (COALESCE(period, '')))
		 DO UPDATE SET total_amount = EXCLUDED.total_amount, updated_at = now()`,
		poolID, tenantID, resourceType, amount, period)
	if err != nil {
		return fmt.Errorf("upsert quota pool: %w", err)
	}
	return nil
}

// reloadTenantResourceQuota 让并发/存储闸门立即看到新配额。
//
// 失败只告警：配额已经写进权威表，缓存最多滞后一个同步周期（5 分钟），
// 而这个窗口只会让限制**更松**（旧配额更小）或**更紧**（旧配额更大），
// 两种都不该让审批接口报错。
func reloadTenantResourceQuota(ctx context.Context) {
	m := tenantResMgrForQuota
	if m == nil {
		return
	}
	m.SyncFromDB(ctx)
}

// queryRequests 是租户视图与平台视图共用的列表查询。
//
// tenantID=0 表示平台全量。两条查询必须共用一份实现：
// 否则"租户看到的"与"平台审批的"会出现字段/排序漂移，
// 而审批人正是要对照租户提交的内容做判断。
func (h *QuotaHandler) queryRequests(
	ctx context.Context, tenantID int, statusFilter string, limit int,
) ([]QuotaRequest, error) {
	sql := `SELECT id, tenant_id, resource_type, period, current_amount, requested_amount,
	               COALESCE(reason, ''), status, COALESCE(review_note, ''), reviewed_at, created_at
	          FROM ent_quota_requests`
	args := []interface{}{}
	if tenantID > 0 {
		args = append(args, tenantID)
		sql += fmt.Sprintf(` WHERE tenant_id = $%d`, len(args))
	}
	if statusFilter != "" {
		args = append(args, statusFilter)
		if tenantID > 0 {
			sql += fmt.Sprintf(` AND status = $%d`, len(args))
		} else {
			sql += fmt.Sprintf(` WHERE status = $%d`, len(args))
		}
	}
	args = append(args, limit)
	sql += fmt.Sprintf(` ORDER BY created_at DESC NULLS LAST, id DESC LIMIT $%d`, len(args))

	rows, err := db.GlobalDBManager.Query(ctx, sql, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	out := make([]QuotaRequest, 0, 16)
	for rows.Next() {
		var it QuotaRequest
		if err := rows.Scan(&it.ID, &it.TenantID, &it.ResourceType, &it.Period,
			&it.CurrentAmount, &it.RequestedAmount, &it.Reason, &it.Status,
			&it.ReviewNote, &it.ReviewedAt, &it.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, it)
	}
	return out, rows.Err()
}

// notifyQuotaReviewResult 把审批结果通知给**提交申请的人**。
//
// 与发票审核同一条理由：审核发生在平台后台，申请人在租户后台看不到审批队列，
// 这条通知是他唯一知道"要不要继续等"的途径。
//
// 复用 KindQuotaChanged（既有的"配额已变更"事件）而不是新增一种：
// 对用户而言这就是同一件事 —— 配额变了。新增事件只会让偏好开关多一个
// 语义重叠的选项。
func (h *QuotaHandler) notifyQuotaReviewResult(
	ctx context.Context, tenantID int16, resourceType, period string,
	amount int64, status, reviewNote, requestedBy string,
) {
	if h.notifier == nil || requestedBy == "" {
		return
	}
	title := "配额申请已审核"
	body := fmt.Sprintf("你对 %s（%s）的配额申请已%s。", resourceType, period, statusTextZh(status))
	payload := map[string]any{
		"action":        "updated",
		"resource_type": resourceType,
		"period":        period,
		"total_amount":  amount,
		"status":        status,
	}
	if note := strings.TrimSpace(reviewNote); note != "" {
		body += fmt.Sprintf("审核备注：%s", note)
		payload["review_note"] = note
	}
	emitNotification(ctx, h.notifier, &notify.Notification{
		TenantID: tenantID,
		UserID:   requestedBy,
		Kind:     notify.KindQuotaChanged,
		// 去重键带申请 id 会把"同一租户的多笔 approved"也区分开，
		// 但这里拿不到 request id（调用方没传），用 租户+资源+周期+状态 已经足够
		// 表达"这一类的最后一次结果"——重复审批本身就被 409 挡住了。
		DedupeKey: fmt.Sprintf("%s:%d:%s:%s:%s", notify.KindQuotaChanged, tenantID, resourceType, period, status),
		Title:     title,
		Body:      body,
		Link:      "/tenant/quota",
		Payload:   payload,
	})
}

// statusTextZh 把审批状态翻成兜底中文（前端会按 payload.status 重渲染）。
func statusTextZh(status string) string {
	if status == quotaRequestApproved {
		return "通过"
	}
	return "驳回"
}
