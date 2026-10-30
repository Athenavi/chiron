package api

import (
	"context"
	"fmt"
	"log/slog"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/notify"
)

// notificationWriter 是通知中心的最小写入入口，*notify.Manager 实现它。
//
// 收窄到"只写"而不是直接依赖 *notify.Manager：auth / 配额这些 handler 只需要
// 发一条通知，不需要读列表或管偏好；接口窄了，单测注入替身也不必构造整套存储。
type notificationWriter interface {
	Notify(ctx context.Context, n *notify.Notification) (bool, error)
}

// emitNotification 是所有 handler 侧通知的统一出口：**失败只记日志**。
//
// 通知是旁路。注册、改配额这些操作已经成功落库，若因为"通知没写进去"返回
// 500，用户会重试，而重试可能撞唯一索引或产生重复副作用 —— 代价远大于少一条
// 站内信。真正的失败排查靠这条 Warn 日志。
func emitNotification(ctx context.Context, w notificationWriter, n *notify.Notification) {
	if w == nil || n == nil {
		return
	}
	if _, err := w.Notify(ctx, n); err != nil {
		slog.Warn("api: emit notification failed (business result unaffected)",
			"user", n.UserID, "kind", n.Kind, "dedupe_key", n.DedupeKey, "error", err)
	}
}

// notifyQuotaChanged 通知该租户的 owner/admin：配额池被创建或调整。
//
// action 只用于日志语义（created/updated），**不进去重键**：同一个池先建后改是
// 两次真实事件，用户应各看到一条；用 action 参与去重反而会把"改成同样数值的
// 第二次调整"和第一次合并。
func (h *EntCostCenterHandler) notifyQuotaChanged(ctx context.Context, pool *EntQuotaPool, action string) {
	if h == nil || h.notifier == nil || pool == nil || pool.ID == "" {
		return
	}
	tenantID := parseTenantID(pool.TenantID)
	if tenantID <= 0 {
		return
	}
	admins, err := listTenantAdmins(ctx, tenantID)
	if err != nil {
		slog.Warn("api: list tenant admins for quota notification failed", "tenant", tenantID, "error", err)
		return
	}
	if len(admins) == 0 {
		// 存量租户尚未回填 tenant_members 时无人可通知，属正常情况。
		return
	}

	// 去重键带 updated_at：每次真实写入都能拿到新的时间戳（UpdateQuotaPool 的
	// `SET ... updated_at = NOW() ... RETURNING`），于是"每次实际变更一条通知"
	// 与"同一次变更被重复调用只发一条"两个目标同时成立。
	// 列可空，零值时退化为按池去重（至少不会静默丢失首条）。
	dedupe := fmt.Sprintf("%s:%s:%d", notify.KindQuotaChanged, pool.ID, pool.UpdatedAt.UnixNano())
	title := "配额已变更"
	verb := "已创建"
	if action == "updated" {
		verb = "已调整"
	}
	body := fmt.Sprintf("租户配额%s：%s 为 %d（周期 %s）。",
		verb, pool.ResourceType, pool.TotalAmount, pool.Period)

	payload := map[string]any{
		"pool_id":       pool.ID,
		"resource_type": pool.ResourceType,
		"total_amount":  pool.TotalAmount,
		"period":        pool.Period,
		"action":        action,
	}
	for _, uid := range admins {
		emitNotification(ctx, h.notifier, &notify.Notification{
			UserID:    uid,
			Kind:      notify.KindQuotaChanged,
			DedupeKey: dedupe,
			Title:     title,
			Body:      body,
			Link:      "/admin/costcenter",
			Payload:   payload,
		})
	}
}

// notifyMemberJoined 通知新加入的成员本人（P2-3 的 member_invited）。
//
// 为什么通知本人而不是"发出邀请的管理员"：新成员刚注册完，最需要确认的是
// "我确实进了这个团队"，这也是这条通知唯一有信息量的读者。
//
// 去重键只含 user_id：一个人只能通过邀请码加入一次（注册是一次性动作），
// 用 kind:user_id 即可保证重放/重试不会重复推送。
func (h *AuthHandler) notifyMemberJoined(ctx context.Context, tenantID int16, userID, displayName string) {
	if h == nil || h.notifier == nil || userID == "" {
		return
	}
	// 租户名取不到就退化为 ID：文案不精确好过整条通知丢失。
	tenantName := tenantStr(tenantID)
	if err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COALESCE(NULLIF(name, ''), $2) FROM tenants WHERE id = $1`,
		tenantID, tenantStr(tenantID)).Scan(&tenantName); err != nil {
		slog.Warn("api: load tenant name for join notification failed",
			"tenant", tenantID, "user", userID, "error", err)
	}
	emitNotification(ctx, h.notifier, &notify.Notification{
		UserID:    userID,
		Kind:      notify.KindMemberInvited,
		DedupeKey: notify.KindMemberInvited + ":" + userID,
		Title:     "已加入团队",
		Body:      fmt.Sprintf("你已通过邀请加入团队「%s」。你的额度与可用模型由该团队统一管理。", tenantName),
		Link:      "/",
		Payload: map[string]any{
			"tenant_id":    tenantID,
			"tenant_name":  tenantName,
			"display_name": displayName,
		},
	})
}

// tenantAdminQuery 枚举某租户的 owner/admin 成员。
// 配额是**租户级**资源，变更只需要通知能改它的人：
//   - 通知全租户成员时，一次配额调整会往 notifications 写入 N 行（N = 成员数），
//     大租户下单次操作就能写出上千行，且绝大多数收件人对此无能为力；
//   - 普通成员真正需要感知的是"自己额度用完了"，那由调用时的 quota_exceeded
//     错误提示承担，不是这条广播。
//
// 角色名取 ent_roles.name（owner/admin 与系统级同名，见 0008 迁移文件头）。
const tenantAdminQuery = `SELECT tm.user_id
  FROM tenant_members tm
  JOIN ent_roles r ON r.id = tm.role_id
 WHERE tm.tenant_id = $1 AND r.name IN ('owner', 'admin')
 ORDER BY tm.user_id`

// listTenantAdmins 返回该租户 owner/admin 的 user_id 列表。
//
// 查不到成员（存量租户尚未回填 tenant_members）时返回空切片而不是错误：
// 没有可通知的人不是故障，不该让配额变更因此失败。
func listTenantAdmins(ctx context.Context, tenantID int16) ([]string, error) {
	rows, err := db.GlobalDBManager.Query(ctx, tenantAdminQuery, tenantID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []string
	for rows.Next() {
		var uid string
		if err := rows.Scan(&uid); err != nil {
			return nil, err
		}
		out = append(out, uid)
	}
	return out, rows.Err()
}
