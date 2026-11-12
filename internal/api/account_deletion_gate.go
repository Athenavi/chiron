package api

import (
	"context"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// account_deletion_gate.go —— 注销冻结期的操作闸门。
//
// 产品规则：「注销后不允许用户进行其他操作和交互，只允许下载或导出现有资产」。
// 这条规则必须在**服务端**落地：前端隐藏按钮只是体验，绕过去照样能调接口。
//
// 实现放在 AuthMiddleware 之后作为独立一层，而不是逐条路由包一层：
//   - 全仓有 200+ 条鉴权路由，逐条加会漏（漏一条就是规则失效）；
//   - 新增路由时默认受管，而不是默认漏管 —— 这是"安全默认值"的正确方向。
//
// 放行清单刻意**极短**：只有导出与撤销（含查询注销状态）。
// 名单是白名单，不是黑名单：将来新增任何端点都自动被拦，无需记得登记。

// accountDeletionAllowedPaths 是 pending_deletion 状态下唯一放行的端点。
//
// **必须 path + method 一起匹配**：只按 path 放行会让 `DELETE /v1/me/export`、
// `GET /v1/me/deletion/cancel` 这类方法不匹配的请求也穿透 —— 而我最初正是
// 只按 path 写的，被测试当场抓出来（见 account_deletion_gate_test.go）。
//
// 三项各自对应一个不可省的场景：
//   - GET  /v1/me/export          导出个人数据（"只允许下载或导出"）
//   - GET  /v1/me/deletion        看剩余天数（否则用户不知道什么时候到期）
//   - POST /v1/me/deletion/cancel 撤销（"1 周内可随时撤销"）
//
// 注意 `POST /v1/me/deletion`（申请）**不在**名单里：已经申请了再申请没有意义，
// 而不是"需要放行"。
var accountDeletionAllowedPaths = map[string]map[string]bool{
	"/v1/me/export":          {http.MethodGet: true},
	"/v1/me/deletion":        {http.MethodGet: true},
	"/v1/me/deletion/cancel": {http.MethodPost: true},
	// GET /v1/auth/profile：前端每次进页面都会拉它拿权威身份与 account_status。
	// 不放行的话，注销中的用户连"我处于什么状态"都读不到，
	// 路由守卫会把他当成未登录而踢回登录页 —— 那样导出与撤销的入口就没了。
	"/v1/auth/profile": {http.MethodGet: true},
	// POST /v1/auth/logout：允许登录就必须允许登出。
	// 否则用户被困在"已登录但什么都做不了"的状态里，连换账号都不行。
	"/v1/auth/logout": {http.MethodPost: true},
}

// accountDeletionAllowedPrefixes 用于要下载的资产路径（媒体签名 URL 等）。
//
// 媒体下载走 `/media/s/{assetID}`，不在 /v1 下，因此单靠上面的精确匹配会漏。
// 只放行只读的 GET/HEAD —— 注销期的用户要能把自己上传过的文件取回去。
var accountDeletionAllowedPrefixes = []string{"/media/"}

// deletionStatusCache 缓存"该用户是否处于注销冻结期"。
//
// 为什么需要缓存：这条检查在**每个**已鉴权请求上都要跑一次 DB 查询，
// 而正常用户的答案永远是"否"。逐请求查库会让所有 API 的 p99 都多一跳。
// 缓存只缓存**否定结果**（active），TTL 很短（10s）：
//   - 正常用户（绝大多数）每 10 秒最多查一次；
//   - 刚申请注销的用户最多 10 秒后开始被拦 —— 这个延迟可接受，
//     因为申请成功后前端会立刻跳转，且我们能主动失效（见 invalidate）。
//
// **绝不缓存"冻结中"**：撤销后必须立刻恢复，否则用户撤销成功了却还要
// 再等 TTL 才能用。
const deletionStatusCacheTTL = 10 * time.Second

type deletionStatusEntry struct {
	active    bool // true = 未处于注销冻结期
	expiresAt time.Time
}

var deletionStatusCache sync.Map // userID -> deletionStatusEntry

// isAccountActive 判断用户是否**未**处于注销冻结期；读不到时返回 false（拒绝）。
//
// fail-close 是刻意的：注销冻结是用户明确要求的状态，读不到状态时放行
// 会让"我已经注销了，怎么还在扣费/还在跑任务"变成可能。
// 与登录锁定那种"附加防护"不同，这里是明确的状态约束。
func isAccountActive(ctx context.Context, userID string) bool {
	if userID == "" {
		return false
	}
	// 无数据库时不判定：本地/测试环境根本没有 users 表，也就不存在注销状态。
	// 这与"fail-close"不矛盾 —— 真正的 fail-close 场景是**有库但查失败**
	// （凭据/状态不可读时必须拒绝）。这里区分"没有这个能力"与"有能力但读不到"。
	if db.GlobalDBManager == nil || !db.GlobalDBManager.IsAvailable() {
		return true
	}
	if v, ok := deletionStatusCache.Load(userID); ok {
		if e, ok := v.(deletionStatusEntry); ok && time.Now().Before(e.expiresAt) {
			return e.active
		}
	}

	var status string
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT account_status FROM users WHERE id = $1`, userID).Scan(&status)
	if err != nil {
		slog.Warn("account deletion gate: status unreadable (deny)", "user_id", userID, "error", err)
		return false
	}
	active := status == accountStatusActive
	// 只缓存 active：冻结中不缓存，撤销后立即生效。
	if active {
		deletionStatusCache.Store(userID, deletionStatusEntry{
			active:    true,
			expiresAt: time.Now().Add(deletionStatusCacheTTL),
		})
	} else {
		deletionStatusCache.Delete(userID)
	}
	return active
}

// invalidateAccountStatusCache 主动失效某用户的缓存。
// 申请/撤销注销后调用，避免最长 10 秒的窗口期。
func invalidateAccountStatusCache(userID string) {
	if userID != "" {
		deletionStatusCache.Delete(userID)
	}
}

// AccountDeletionGate 拦住注销冻结期的账号，只放行导出与撤销。
//
// 必须在 AuthMiddleware **之后**：它需要 claims 里的 userID。
func AccountDeletionGate(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		claims := getClaimsFromContext(r.Context())
		if claims == nil || claims.UserID == "" {
			// 无身份：交给下游的鉴权处理（这里不做鉴权判断）。
			next.ServeHTTP(w, r)
			return
		}

		// 放行清单检查放在 DB 查询**之前**：导出/撤销这两个端点
		// 属于冻结期唯一可用的功能，不该因为缓存/DB 抖动而被挡住。
		if isDeletionAllowedRequest(r) {
			next.ServeHTTP(w, r)
			return
		}

		if !isAccountActive(r.Context(), claims.UserID) {
			// 403 而不是 401：用户身份是有效的，只是权限被收窄。
			// 401 会让前端以为登录态失效而踢回登录页 —— 那样用户就再也
			// 看不到"导出 / 撤销"的入口了，与产品要求直接冲突。
			JSON(w, http.StatusForbidden, APIResponse{
				Success: false,
				Error: "account is pending deletion: only data export and cancellation are allowed",
				Code:    "account_pending_deletion",
				// Retryable=false：重试同样会被拒绝，客户端应引导用户去导出/撤销。
				Retryable: BoolPtr(false),
			})
			return
		}

		next.ServeHTTP(w, r)
	})
}

// isDeletionAllowedRequest 判定该请求是否属于冻结期允许的操作。
func isDeletionAllowedRequest(r *http.Request) bool {
	if methods, ok := accountDeletionAllowedPaths[r.URL.Path]; ok {
		return methods[r.Method]
	}
	for _, prefix := range accountDeletionAllowedPrefixes {
		if strings.HasPrefix(r.URL.Path, prefix) {
			// 只放行只读方法：注销期不该还能上传新文件。
			return r.Method == http.MethodGet || r.Method == http.MethodHead
		}
	}
	return false
}

// getClaimsFromContext 是 auth.GetClaims 的薄封装（便于本文件独立测试）。
func getClaimsFromContext(ctx context.Context) *auth.Claims {
	return auth.GetClaims(ctx)
}
