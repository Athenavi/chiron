package billing

import (
	"context"
	"fmt"
	"log/slog"
	"math"
	"time"

	"github.com/athenavi/chiron/internal/notify"
)

// 到期预警的两个档位（天）。用户已定：提前 3 天与 1 天各一次。
//
// 为什么是"档位"而不是"每天提醒"：每天一条会让一条 30 天套餐发出几十条通知，
// 用户很快就把通知中心当噪音关掉 —— 那反而让真正重要的到期信息发不出去。
var expiryWarnDays = []int{3, 1}

// expiryScanWindowDays 是扫描窗口上界：只查这些天内到期的权益。
//
// 取最大档位（3）而不是更大值：窗口越小，单次扫描的行数越少，而更大的前提下
// 多查出来的行也会因"剩余天数不在档位上"被跳过，纯属浪费。
const expiryScanWindowDays = 3

// ExpiryScanResult 是一次扫描的统计，供日志与测试断言。
type ExpiryScanResult struct {
	// Scanned 是窗口内命中的权益数（含未落在档位上的）。
	Scanned int
	// Notified 是实际写入（未被去重跳过）的通知数。
	Notified int
}

// ScanEntitlementExpiry 扫描即将到期的权益并发出到期预警（P2-3）。
//
// **只发通知，不改任何状态**。权益是否生效完全由 `starts_at/expires_at` 与当前
// 时间比较决定（见 ActiveEntitlements 的注释），没有 status 列需要维护 ——
// 所以这个扫描不是"到期任务"，重启或漏跑一次都不会让权益错乱。
//
// 幂等靠去重键 `entitlement_expiring:<entitlement_id>:<days_before>`：同一份权益
// 的同一档位重复扫描会撞唯一索引，直接跳过。于是"每小时扫一次"与"每天扫一次"
// 在结果上等价，运维不必担心扫描频率调高会重复推送。
func (m *Manager) ScanEntitlementExpiry(ctx context.Context, now time.Time) (ExpiryScanResult, error) {
	var res ExpiryScanResult
	if m.notifierOrNil() == nil {
		// 未装配通知中心时直接返回：省掉一次全表窗口查询。
		return res, nil
	}

	items, err := m.store.ExpiringEntitlements(ctx, now, expiryScanWindowDays)
	if err != nil {
		return res, fmt.Errorf("scan entitlement expiry: %w", err)
	}
	res.Scanned = len(items)

	for i := range items {
		e := &items[i]
		daysBefore, ok := expiryWarnBucket(now, e.ExpiresAt)
		if !ok {
			// 剩余时间不在 3 天/1 天档位上（例如刚进窗口时是 2.5 天）。
			// 不是错误：下一个扫描周期会落到正确档位。
			continue
		}
		inserted, err := m.notifierOrNil().Notify(ctx, &notify.Notification{
			UserID: e.UserID,
			Kind:   notify.KindEntitlementExpiring,
			// 档位进键：3 天与 1 天是两条独立通知，各自只发一次。
			DedupeKey: fmt.Sprintf("%s:%s:%d", notify.KindEntitlementExpiring, e.ID, daysBefore),
			Title:     fmt.Sprintf("套餐将在 %d 天内到期", daysBefore),
			Body: fmt.Sprintf("套餐「%s」将于 %s 到期，到期后相关模型权益将失效。如需继续使用请及时续购。",
				e.PackageName, e.ExpiresAt.Format("2006-01-02")),
			Link: "/billing",
			Payload: map[string]any{
				"entitlement_id": e.ID,
				"package_code":   e.PackageCode,
				"package_name":   e.PackageName,
				"expires_at":     e.ExpiresAt.Format(time.RFC3339),
				"days_before":    daysBefore,
			},
		})
		if err != nil {
			// 单条失败不中断整轮扫描：其余用户的到期预警更值得发出去。
			slog.Warn("billing: emit entitlement expiry notice failed",
				"user", e.UserID, "entitlement", e.ID, "days_before", daysBefore, "error", err)
			continue
		}
		if inserted {
			res.Notified++
		}
	}
	return res, nil
}

// StartEntitlementExpiryScan 启动周期性到期预警扫描（受 ctx 控制，随网关退出）。
//
// 启动即扫一次：否则网关刚起来的那个窗口内到期的权益要等一个完整周期才被提醒。
func (m *Manager) StartEntitlementExpiryScan(ctx context.Context, interval time.Duration) {
	if interval <= 0 {
		interval = time.Hour
	}
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		m.runExpiryScan(ctx)
		slog.Info("entitlement expiry scan started", "interval", interval.String())
		for {
			select {
			case <-ctx.Done():
				slog.Info("entitlement expiry scan stopped")
				return
			case <-ticker.C:
				m.runExpiryScan(ctx)
			}
		}
	}()
}

// runExpiryScan 执行一轮扫描并记录统计；错误只记日志（下一轮会重试）。
func (m *Manager) runExpiryScan(ctx context.Context) {
	res, err := m.ScanEntitlementExpiry(ctx, time.Now())
	if err != nil {
		slog.Warn("entitlement expiry scan failed", "error", err)
		return
	}
	if res.Scanned > 0 || res.Notified > 0 {
		slog.Info("entitlement expiry scan done",
			"scanned", res.Scanned, "notified", res.Notified)
	}
}

// expiryWarnBucket 判断某份权益此刻应归入哪个预警档位。
//
// 用 **向上取整** 的天数，即每个档位覆盖区间 (N-1, N]：
//   - 剩余 3.0 天 → ceil = 3 → 3 天档；
//   - 剩余 2.4 天 → ceil = 3 → 仍是 3 天档（与首次提醒同档 ⇒ 去重键相同，
//     不会重复推送）；
//   - 剩余 1.4 天 → ceil = 2 → 不在档位，跳过；
//   - 剩余 1.0 天或 0.5 天 → ceil = 1 → 1 天档。
//
// 若改用 floor，剩余 3.9 天会被判成 3 天档而立刻发信，等于把"提前 3 天"提前到
// "提前 4 天"，与用户定下的口径不符。
//
// 因为档位是区间而非精确时刻，通知文案用"N 天内到期"而不是"将在 N 天后到期"：
// 剩余半天时说"1 天后"是错的，"1 天内"才准确。
func expiryWarnBucket(now, expiresAt time.Time) (int, bool) {
	remaining := expiresAt.Sub(now)
	if remaining <= 0 {
		return 0, false
	}
	days := int(math.Ceil(remaining.Hours() / 24))
	for _, d := range expiryWarnDays {
		if days == d {
			return d, true
		}
	}
	return 0, false
}
