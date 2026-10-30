package notify

import (
	"fmt"
	"strings"
	"time"
)

// messages.go —— 通知文案的服务端渲染（P2-3 / T1）。
//
// 为什么需要它：站内信的渲染交给前端（按当前界面语言实时渲染，于是历史通知
// 也会跟着语言切换），但**邮件是服务端拼好的字符串**，没有前端兜底。此前四个
// 事件源把中文标题/正文直接写进 Notification，邮件出口就原样发出这段中文 ——
// 对 en-US / ar 用户等于「用不了但没提示」。
//
// 本文件把「标题/正文模板」集中到一处，按 locale 选表，事件源只提供参数。
// 前端 `notifications.body.*` 与本文件的模板必须保持一致：它们是同一条文案的
// 两个出口（站内 / 邮件），措辞漂移会让用户觉得收到了两件不同的事。

// SupportedLocale 返回受支持的语言代码，其它一律回落 zh-CN。
//
// 只做前缀归一化（"en" → "en-US"）：`users.settings.locale` 由前端写入，
// 可能是 localStorage 里的旧值或用户手改的值，宽松匹配比严格白名单更不容易
// 让一封邮件退回中文。
func SupportedLocale(locale string) string {
	switch {
	case locale == "":
		return "zh-CN"
	case strings.HasPrefix(locale, "en"):
		return "en-US"
	case strings.HasPrefix(locale, "ar"):
		return "ar"
	default:
		return "zh-CN"
	}
}

// 事件类型 → 各语言的标题。
//
// 用「按 kind 查表」而不是给每个事件写一份渲染函数：四类事件的参数形状不同，
// 但标题只依赖 kind，查表最省。
var titles = map[string]map[string]string{
	KindPaymentSucceeded: {
		"zh-CN": "支付成功",
		"en-US": "Payment succeeded",
		"ar":    "تم الدفع بنجاح",
	},
	KindEntitlementExpiring: {
		"zh-CN": "套餐即将到期",
		"en-US": "Package expiring soon",
		"ar":    "الباقة على وشك الانتهاء",
	},
	KindQuotaChanged: {
		"zh-CN": "配额已变更",
		"en-US": "Quota changed",
		"ar":    "تغيّر الحصة",
	},
	KindMemberInvited: {
		"zh-CN": "已加入团队",
		"en-US": "Joined a team",
		"ar":    "انضممت إلى فريق",
	},
}

// 正文模板。占位符用 %s/%d，按 kind 的固定参数顺序填充。
//
// 刻意用 fmt 而不是 text/template：模板数量固定为四类 × 三语，模板引擎的
// 解析开销与出错面都不划算；参数顺序在下面的 RenderBody 里一处集中，好审。
var bodies = map[string]map[string]string{
	KindPaymentSucceeded: {
		"zh-CN": "充值 %d credits 已到账。",
		"en-US": "%d credits have been added to your balance.",
		"ar":    "تمت إضافة %d من الأرصدة إلى رصيدك.",
	},
	KindEntitlementExpiring: {
		"zh-CN": "套餐「%s」将于 %s 到期，到期后相关模型权益将失效。如需继续使用请及时续购。",
		"en-US": "Package \"%s\" expires on %s; the related model entitlements will stop working after that. Buy a new package to keep using them.",
		"ar":    "تنتهي الباقة «%s» في %s؛ وستتوقف صلاحيات النماذج المرتبطة بها بعد ذلك. اشترِ باقة جديدة لمواصلة الاستخدام.",
	},
	KindQuotaChanged: {
		"zh-CN": "租户配额%s：%s 为 %d（周期 %s）。",
		"en-US": "Tenant quota %s: %s is now %d (period %s).",
		"ar":    "حصة المستأجر %s: %s أصبح %d (الدورة %s).",
	},
	KindMemberInvited: {
		"zh-CN": "你已通过邀请加入团队「%s」。你的额度与可用模型由该团队统一管理。",
		"en-US": "You joined the team \"%s\" via an invitation. Your quota and available models are managed by that team.",
		"ar":    "انضممت إلى الفريق «%s» عبر دعوة. تُدار حصتك والنماذج المتاحة بواسطة ذلك الفريق.",
	},
}

// 套餐订单的支付成功有独立措辞：纯充值说"充值已到账"是准的，买套餐则
// 应说清套餐名与到期日，否则用户不知道买到的是什么、什么时候失效。
var packagePaymentTitles = map[string]string{
	"zh-CN": "套餐购买成功",
	"en-US": "Package purchased",
	"ar":    "تم شراء الباقة",
}

var packagePaymentBodies = map[string]string{
	"zh-CN": "套餐「%s」已生效，%d credits 已到账。权益有效期至 %s。",
	"en-US": "Package \"%s\" is now active; %d credits have been added. Entitlements are valid until %s.",
	"ar":    "الباقة «%s» مفعّلة الآن؛ تمت إضافة %d من الأرصدة. الصلاحيات سارية حتى %s.",
}

// RenderTitle 按 locale 返回该类事件的标题；未知 kind 回落 fallback。
func RenderTitle(kind, locale string, fallback string) string {
	loc := SupportedLocale(locale)
	if m, ok := titles[kind]; ok {
		if s, ok := m[loc]; ok {
			return s
		}
	}
	return fallback
}

// RenderBody 按 locale 与参数渲染正文；未知 kind 或参数不匹配时回落 fallback。
//
// 参数顺序是「该类事件在 events 调用点填 payload 的顺序」，两者必须同步改。
func RenderBody(kind, locale string, fallback string, args ...any) string {
	loc := SupportedLocale(locale)
	tpl, ok := bodies[kind][loc]
	if !ok {
		return fallback
	}
	// 参数量与类型都可能不对（payload 来自 jsonb，数字读回来是 float64，而
	// 调用方可能传错）。**渲染后检查 `%!`**：fmt 遇到类型不符时会写出
	// `%!d(string=...)` 这类半成品，用户看到它比看到语言不对的完整文案更糟。
	// 只校验参数个数挡不住这种情况（个数对、类型错时照样产出 %!d）。
	out := fmt.Sprintf(tpl, args...)
	if strings.Contains(out, "%!") {
		return fallback
	}
	return out
}

// RenderPackagePayment 渲染套餐订单的支付成功通知（标题, 正文）。
//
// 单独一个函数而不是塞进 bodies 表：它比纯充值多两个参数（套餐名、到期日），
// 用同一张表会让参数顺序在两类支付之间分叉，正是上面说的事故来源。
func RenderPackagePayment(locale, packageName string, credits int, expiresOn string, fallbackTitle, fallbackBody string) (string, string) {
	loc := SupportedLocale(locale)
	title, ok := packagePaymentTitles[loc]
	if !ok {
		title = fallbackTitle
	}
	tpl, ok := packagePaymentBodies[loc]
	if !ok {
		return title, fallbackBody
	}
	return title, fmt.Sprintf(tpl, packageName, credits, expiresOn)
}

// QuotaActionLabel 把配额动作翻译成用户可读动词。
func QuotaActionLabel(action, locale string) string {
	loc := SupportedLocale(locale)
	labels := map[string]map[string]string{
		"created": {
			"zh-CN": "已创建",
			"en-US": "created",
			"ar":    "أُنشئت",
		},
		"updated": {
			"zh-CN": "已调整",
			"en-US": "updated",
			"ar":    "حُدِّثت",
		},
	}
	if m, ok := labels[action]; ok {
		if s, ok := m[loc]; ok {
			return s
		}
	}
	return action
}

// expiringTitles 是「即将到期」的标题模板：它比其它事件多一个天数参数，
// 所以不能放进 titles 那张静态表。
var expiringTitles = map[string]string{
	"zh-CN": "套餐将在 %d 天内到期",
	"en-US": "Package expires within %d day(s)",
	"ar":    "تنتهي الباقة خلال %d يوم/أيام",
}

// RenderMail 按 kind + payload 与收件人语言重建邮件的标题与正文。
//
// 为什么不在发通知时就把渲染好的文案存下来：站内信由前端按界面语言实时渲染，
// 存死文案会让历史通知永远停在写入时的语言；邮件必须在发送这一刻定稿，所以
// 它在这里**用同一份模板**重新渲染，而不是拿站内信的兜底文本。
//
// 任何一步拿不到参数都回落 n.Title / n.Body —— 用户收到一段语言不对但内容
// 完整的通知，好过收到「%!d(MISSING)」或者空白邮件。
func RenderMail(n *Notification, locale string) (string, string) {
	if n == nil {
		return "", ""
	}
	loc := SupportedLocale(locale)

	switch n.Kind {
	case KindPaymentSucceeded:
		if code, _ := n.Payload["package_code"].(string); code != "" {
			name, _ := n.Payload["package_name"].(string)
			if name == "" {
				name = code
			}
			expiresOn := ""
			if t, err := time.Parse(time.RFC3339, stringOf(n.Payload["expires_at"])); err == nil {
				expiresOn = t.Format("2006-01-02")
			}
			return RenderPackagePayment(loc, name, intOf(n.Payload["credits"]), expiresOn, n.Title, n.Body)
		}
		return RenderTitle(n.Kind, loc, n.Title),
			RenderBody(n.Kind, loc, n.Body, intOf(n.Payload["credits"]))

	case KindEntitlementExpiring:
		days := intOf(n.Payload["days_before"])
		tpl, ok := expiringTitles[loc]
		if !ok || days <= 0 {
			return n.Title, n.Body
		}
		name, _ := n.Payload["package_name"].(string)
		expiresOn := ""
		if t, err := time.Parse(time.RFC3339, stringOf(n.Payload["expires_at"])); err == nil {
			expiresOn = t.Format("2006-01-02")
		}
		if name == "" || expiresOn == "" {
			return fmt.Sprintf(tpl, days), n.Body
		}
		return fmt.Sprintf(tpl, days), RenderBody(n.Kind, loc, n.Body, name, expiresOn)

	case KindQuotaChanged:
		action, _ := n.Payload["action"].(string)
		resource, _ := n.Payload["resource_type"].(string)
		period, _ := n.Payload["period"].(string)
		return RenderTitle(n.Kind, loc, n.Title),
			RenderBody(n.Kind, loc, n.Body,
				QuotaActionLabel(action, loc), resource, intOf(n.Payload["total_amount"]), period)

	case KindMemberInvited:
		name, _ := n.Payload["tenant_name"].(string)
		if name == "" {
			return RenderTitle(n.Kind, loc, n.Title), n.Body
		}
		return RenderTitle(n.Kind, loc, n.Title), RenderBody(n.Kind, loc, n.Body, name)
	}

	// 未知 kind：用兜底文本，不猜。
	return n.Title, n.Body
}

// intOf 把 payload 里的数值转成 int。
//
// jsonb 读回来的数字是 float64（encoding/json 的默认行为），而写入时可能是
// int/int64 —— 两种都要认，否则邮件正文里的额度会变成 0。
func intOf(v any) int {
	switch n := v.(type) {
	case int:
		return n
	case int8:
		return int(n)
	case int16:
		return int(n)
	case int32:
		return int(n)
	case int64:
		return int(n)
	case float32:
		return int(n)
	case float64:
		return int(n)
	default:
		return 0
	}
}

// stringOf 把 payload 里的值转成字符串（空值返回空串）。
func stringOf(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}
