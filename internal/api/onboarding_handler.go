package api

import (
	"context"
	"log/slog"
	"net/http"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/billing"
	"github.com/athenavi/chiron/internal/db"
)

// 新用户引导（P0-3）的三步。
//
// 顺序即用户实际走过的顺序，前端按此顺序渲染：
//  1. 领试用额度 —— 改为「展示你已有的试用额度」：注册即自带额度（`users.credits
//     default 1000`），再发一笔既重复又可被反复注册薅走。这一步的价值是让用户**看见**
//     "我有多少钱、每天还能白嫖几次"，而不是再给他钱。
//  2. 选默认模型 —— 决定下一轮对话用哪个。
//  3. 发出第一轮对话 —— 唯一的"真正完成"标志。
const (
	OnboardingStepTrialCredit  = "trial_credit"
	OnboardingStepDefaultModel = "default_model"
	OnboardingStepFirstMessage = "first_message"
)

// onboardingSteps 是**白名单**，顺序即展示顺序。
//
// 不接受任意 step 字符串：那样 `user_onboarding` 会被写成一张"随便什么键"的表，
// 之后按 step 聚合完成率就会混进噪音，而且前端拼错一个键会静默产生一个永远
// pending 的假步骤。非法值一律 400，不做静默忽略。
var onboardingSteps = []string{
	OnboardingStepTrialCredit,
	OnboardingStepDefaultModel,
	OnboardingStepFirstMessage,
}

func isOnboardingStep(s string) bool {
	for _, v := range onboardingSteps {
		if v == s {
			return true
		}
	}
	return false
}

// OnboardingStepState 是单步的状态。
//
// 三种状态用两个时间戳表达（NULL/PENDING 两种态之外再分完成与跳过）：
// 跳过**不等于**完成，统计流失率时必须分开 —— 把跳过算成完成会让完成率虚高。
type OnboardingStepState struct {
	Step        string     `json:"step"`
	Status      string     `json:"status"` // pending | completed | skipped
	CompletedAt *time.Time `json:"completed_at,omitempty"`
	SkippedAt   *time.Time `json:"skipped_at,omitempty"`
}

// OnboardingHandler 读写当前登录用户的引导进度。
//
// **身份只来自 token**（`auth.GetClaims`）：请求体里的 user_id 一律忽略。
type OnboardingHandler struct {
	// balance 取用户当前余额（试用额度展示用）。为 nil 时不返回余额，只返回每日免费次数 ——
	// 引导不该因为计费模块不可用而整个挂掉。
	balance func(userID string) (int, error)
}

func NewOnboardingHandler(mgr *billing.Manager) *OnboardingHandler {
	h := &OnboardingHandler{}
	if mgr != nil {
		h.balance = mgr.GetBalance
	}
	return h
}

func (h *OnboardingHandler) RegisterRoutes(mux *http.ServeMux, authMW, rlMW routeMiddleware) {
	mux.Handle("GET /v1/onboarding", authMW(rlMW(http.HandlerFunc(h.GetStatus))))
	mux.Handle("POST /v1/onboarding/step", authMW(rlMW(http.HandlerFunc(h.CompleteStep))))
}

// GetStatus 返回当前用户的引导进度。
//
// `eligible=false` 表示这**不是**一个走过新注册流程的用户（`user_onboarding` 里没有他的行）：
// 存量账号不该被弹一次与身份不符的引导。前端据此直接跳过，不需要额外判断。
func (h *OnboardingHandler) GetStatus(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, "not authenticated")
		return
	}

	states, err := loadOnboardingStates(r.Context(), claims.UserID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "load onboarding failed")
		return
	}

	OK(w, h.buildPayload(claims.UserID, states))
}

// CompleteStep 标记某一步为完成或跳过。
//
// 幂等：重复调用不会刷新时间戳（否则"第一次完成时间"失去意义，完成率会随用户
// 多点几次而漂移）。已完成的步骤再被跳过时，完成状态**保持不变** —— 做过就是做过。
func (h *OnboardingHandler) CompleteStep(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, "not authenticated")
		return
	}

	var body struct {
		Step    string `json:"step"`
		Skipped bool   `json:"skipped"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if !isOnboardingStep(body.Step) {
		BadRequest(w, "unknown onboarding step")
		return
	}

	// 存量用户没有行时不会被 upsert 出来：先确认他确实在引导里，否则
	// 一个旧账号点一次就会凭空多出"已完成"的步骤，把统计污染成假数据。
	var exists int
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT COUNT(*) FROM user_onboarding WHERE user_id = $1`, claims.UserID).Scan(&exists); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "load onboarding failed")
		return
	}
	if exists == 0 {
		BadRequest(w, "onboarding not started for this user")
		return
	}

	completed := !body.Skipped
	_, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO user_onboarding (user_id, step, completed_at, skipped_at, updated_at)
		 VALUES ($1, $2,
		         CASE WHEN $3 THEN now() ELSE NULL END,
		         CASE WHEN $3 THEN NULL ELSE now() END,
		         now())
		 ON CONFLICT (user_id, step) DO UPDATE
		    SET completed_at = COALESCE(user_onboarding.completed_at, EXCLUDED.completed_at),
		        skipped_at   = CASE
		                         WHEN user_onboarding.completed_at IS NOT NULL THEN user_onboarding.skipped_at
		                         WHEN EXCLUDED.completed_at IS NOT NULL THEN NULL
		                         ELSE COALESCE(user_onboarding.skipped_at, EXCLUDED.skipped_at)
		                       END,
		        updated_at   = now()`,
		claims.UserID, body.Step, completed)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "save onboarding step failed")
		return
	}

	states, err := loadOnboardingStates(r.Context(), claims.UserID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "load onboarding failed")
		return
	}
	OK(w, h.buildPayload(claims.UserID, states))
}

// loadOnboardingStates 读出该用户的全部步骤，**按白名单顺序补齐**。
//
// 补齐很重要：某一步没有行（历史数据、或将来新增步骤）时也要出现在列表里，
// 否则前端会少渲染一步，用户以为流程变短了。
func loadOnboardingStates(ctx context.Context, userID string) (map[string]OnboardingStepState, error) {
	rows, err := db.GlobalDBManager.Query(ctx,
		`SELECT step, completed_at, skipped_at FROM user_onboarding WHERE user_id = $1`, userID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	out := make(map[string]OnboardingStepState, len(onboardingSteps))
	for rows.Next() {
		var step string
		var completedAt, skippedAt *time.Time
		if err := rows.Scan(&step, &completedAt, &skippedAt); err != nil {
			return nil, err
		}
		out[step] = OnboardingStepState{
			Step:        step,
			Status:      onboardingStatus(completedAt, skippedAt),
			CompletedAt: completedAt,
			SkippedAt:   skippedAt,
		}
	}
	return out, rows.Err()
}

// onboardingStatus 把两个时间戳折成三种状态。完成优先于跳过 —— 两者都有值时
// 说明用户先跳过、后来真的做了，那应当算完成。
func onboardingStatus(completedAt, skippedAt *time.Time) string {
	switch {
	case completedAt != nil:
		return "completed"
	case skippedAt != nil:
		return "skipped"
	default:
		return "pending"
	}
}

func (h *OnboardingHandler) buildPayload(userID string, states map[string]OnboardingStepState) map[string]interface{} {
	steps := make([]OnboardingStepState, 0, len(onboardingSteps))
	finished := true
	for _, name := range onboardingSteps {
		st, ok := states[name]
		if !ok {
			// 用户在这一步没有行：既不是完成也不是跳过。
			st = OnboardingStepState{Step: name, Status: "pending"}
		}
		if st.Status == "pending" {
			finished = false
		}
		steps = append(steps, st)
	}

	trial := map[string]interface{}{
		"daily_free_limit": billing.DailyFreeLimit,
		"already_granted":  true,
	}
	if h.balance != nil {
		if bal, err := h.balance(userID); err == nil {
			trial["credits"] = bal
		} else {
			// 余额读失败不该让整个引导 500：这一步的目的只是"给用户看一眼"。
			slog.Warn("onboarding: read balance failed", "user", userID, "error", err)
		}
	}

	return map[string]interface{}{
		// 没有行 = 不是新注册流程里的用户 ⇒ 前端不弹引导（roadmap：不打断已有用户）
		"eligible": len(states) > 0,
		"finished": finished,
		"steps":    steps,
		"trial":    trial,
	}
}

// StartOnboarding 为新注册用户建好三步的 pending 行。
//
// **在注册事务 commit 之后调用，且失败只告警**：引导进度写不进去绝不能让一个
// 已经合法创建的账号注册失败。与 `RecordUsageEvent` 同一原则。
func StartOnboarding(ctx context.Context, userID string) {
	if db.GlobalDBManager == nil || userID == "" {
		return
	}
	for _, step := range onboardingSteps {
		if _, err := db.GlobalDBManager.Exec(ctx,
			`INSERT INTO user_onboarding (user_id, step) VALUES ($1, $2)
			 ON CONFLICT (user_id, step) DO NOTHING`, userID, step); err != nil {
			slog.Warn("onboarding: init failed", "user", userID, "step", step, "error", err)
			return
		}
	}
}
