package api

import (
	"context"
	"log/slog"
	"net/http"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

// STEP 引导漏斗（P0-3 的「完成率可查」验收）。
//
// ## 为什么要单独一个端点
//
// `user_onboarding` 建表时就是**一行一步**，目的正是让 `GROUP BY step` 直接给出漏斗。
// 只写不读的话，表只是一个没人看的日志，"完成率可查"这条验收等于没交付。
//
// ## 分母必须是「走过新注册流程的人」，不是全站用户
//
// `user_onboarding` 里没有行的用户是存量账号（本迁移之前注册的），他们**本来就不会
// 被弹引导**。若用 `SELECT count(*) FROM users` 当分母，完成率会被这些注定不参与的人
// 稀释成恒定的低位，读起来像是引导毫无效果。因此分母取 `count(DISTINCT user_id)`
// —— 引导表里有行的人，就是"进入过漏斗"的人。
type onboardingFunnelStep struct {
	Step string `json:"step"`
	// 三个计数互斥且相加等于分母：完成优先于跳过，与 `onboardingStatus` 的口径一致。
	Completed int `json:"completed"`
	Skipped   int `json:"skipped"`
	Pending   int `json:"pending"`
	// 完成率 / 跳过率，单位为 0–1 的比例（分母见 payload.entered）。
	// 前端自己乘 100 显示，避免后端格式化出 "33.3%" 这种字符串再被解析回去。
	CompletionRate float64 `json:"completion_rate"`
	SkipRate       float64 `json:"skip_rate"`
}

// buildOnboardingFunnel 把「分母 + 各步计数」折叠成按白名单排序的漏斗。
//
// 抽成纯函数是为了在没有真实 PG 的单元测试里也能验证两件事：
//  1. 表里缺失的步骤必须补 0 而不是从列表里消失（否则"三步"看起来会变短）；
//  2. 分母为 0 时比率必须是 0，不能出现 NaN（0/0 → NaN 会让前端渲染成 "NaN%"）。
func buildOnboardingFunnel(entered, finished int, counts map[string][3]int) ([]onboardingFunnelStep, float64) {
	steps := make([]onboardingFunnelStep, 0, len(onboardingSteps))
	for _, name := range onboardingSteps {
		c := counts[name]
		step := onboardingFunnelStep{
			Step:      name,
			Completed: c[0],
			Skipped:   c[1],
			Pending:   c[2],
		}
		if entered > 0 {
			step.CompletionRate = float64(step.Completed) / float64(entered)
			step.SkipRate = float64(step.Skipped) / float64(entered)
		}
		steps = append(steps, step)
	}
	finishedRate := 0.0
	if entered > 0 {
		finishedRate = float64(finished) / float64(entered)
	}
	return steps, finishedRate
}

// GetOnboardingFunnel 返回新用户引导的逐步完成情况。
func (h *AdminHandler) GetOnboardingFunnel(w http.ResponseWriter, r *http.Request) {
	if db.GlobalDBManager == nil {
		InternalError(w, "database unavailable")
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()

	// 分母：进入过漏斗的人数（引导表里有行的人）。
	var entered int
	if err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COUNT(DISTINCT user_id) FROM user_onboarding`).Scan(&entered); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "load onboarding funnel failed")
		return
	}

	rows, err := db.GlobalDBManager.Query(ctx,
		`SELECT step,
		        COUNT(*) FILTER (WHERE completed_at IS NOT NULL)::int,
		        COUNT(*) FILTER (WHERE completed_at IS NULL AND skipped_at IS NOT NULL)::int,
		        COUNT(*) FILTER (WHERE completed_at IS NULL AND skipped_at IS NULL)::int
		   FROM user_onboarding
		  GROUP BY step`)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "load onboarding funnel failed")
		return
	}
	defer rows.Close()

	counts := make(map[string][3]int, len(onboardingSteps))
	for rows.Next() {
		var step string
		var completed, skipped, pending int
		if err := rows.Scan(&step, &completed, &skipped, &pending); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "scan onboarding funnel failed")
			return
		}
		counts[step] = [3]int{completed, skipped, pending}
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "iterate onboarding funnel failed")
		return
	}

	// 按白名单顺序输出，**缺失的步骤补 0** —— 表里没有行只说明还没人走到，
	// 不该让那一步从漏斗里消失（否则"三步"看起来会变短）。
	steps, finishedRate := buildOnboardingFunnel(entered, 0, counts)

	// 整体完成率：三步全部非 pending 的人数 / entered。
	// 与逐步完成率不同 —— 前者衡量"走完整条流程"，后者衡量"某一步做没做"。
	var finished int
	if err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COUNT(*)::int FROM (
		     SELECT user_id
		       FROM user_onboarding
		      GROUP BY user_id
		     HAVING COUNT(*) FILTER (WHERE completed_at IS NULL AND skipped_at IS NULL) = 0
		        AND COUNT(*) >= 1
		 ) t`).Scan(&finished); err != nil {
		// 这条查询失败不该让整个漏斗 500：逐步数据已经拿到了。
		slog.Warn("onboarding funnel: finished count failed", "error", err)
		finished = 0
	}
	// 整体完成率依赖 finished，所以拿到计数后再算一次（逐步比率不受影响）。
	if entered > 0 {
		finishedRate = float64(finished) / float64(entered)
	}

	OK(w, map[string]interface{}{
		// entered 是**唯一正确的分母**：进入过引导的人数。
		"entered":              entered,
		"finished":             finished,
		"finished_rate":        finishedRate,
		"steps":                steps,
		"steps_order":          onboardingSteps,
		"finished_definition":  "all steps completed or skipped",
		"entered_definition":   "users having any row in user_onboarding",
	})
}
