package api

// auth_local_session.go —— 本地模式（桌面端自带的 Go 网关）的会话引导。
//
// 云端模式的身份来自注册/登录/SSO；本地模式没有账号体系，但网关的鉴权中间件、
// RBAC、审计与前端 `requiresAdmin` 判定都建立在「claims 里有真实 role 与 perms」之上。
// 用假身份（设备 token 之类）绕过这套判定，等于让本地模式跑在另一套身份语义上 ——
// 每次前端加一个权限判定，本地模式就多一处行为差异。这里改成**真的铸一个 owner**：
// 身份、token、cookie、审计与云端登录走的是同一条代码路径，唯一的区别是它免口令。
//
// 免口令只对本机成立，因此有两道闸门：
//  1. 路由本身只在 `LOCAL_BOOTSTRAP=1` 时注册（默认关，云端部署不可能带上这条路由）；
//  2. handler 再校验来源是 loopback，局域网请求 403。
//
// 局域网里的其它设备要用这台机器上的服务，只能走 POST /v1/auth/login 用口令登录，
// 而口令必须由本机用户自己在「设置 → 账户」里首设（见 ensureLocalOwner 的 password_set）。

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"log/slog"
	"net"
	"net/http"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/jackc/pgx/v5"
	"golang.org/x/crypto/bcrypt"
)

// localOwnerEmail 是本地引导账号的邮箱。
//
// 取固定值而不是随机值有两个理由：一是局域网设备登录时得知道该输哪个邮箱，
// 二是 (tenant_id, email) 上有唯一索引（迁移 0002），固定值天然幂等 ——
// 每次重启都命中同一行，不会积累出一堆本地账号。
// 域名用保留 TLD `.local`：它不会被 DNS 解析，也就不会被误当成可发信的真实地址。
const localOwnerEmail = "owner@chiron.local"

// localOwnerName 是引导账号的显示名，用户随时可在「设置 → 账户」里改。
// 与 desktop-local core 的 LOCAL_USER.name 保持一致（契约 §4），切换后端时界面不跳字。
const localOwnerName = "Local"

// isLoopbackPeer 判断请求是否来自本机。
//
// **只看 r.RemoteAddr，不看任何转发头**：X-Forwarded-For 是客户端可自填的，
// 拿它判"是不是本机"等于把闸门交给请求方。本地网关由桌面壳在同一台机器上拉起，
// 中间没有反代，RemoteAddr 就是真相；真有反代的部署不该开 LOCAL_BOOTSTRAP。
func isLoopbackPeer(remoteAddr string) bool {
	host, _, err := net.SplitHostPort(remoteAddr)
	if err != nil {
		return false
	}
	ip := net.ParseIP(host)
	if ip == nil {
		return false
	}
	// ::ffff:127.0.0.1 这类 IPv4-mapped 地址由 To4() 归一，IsLoopback 一并覆盖。
	return ip.IsLoopback()
}

// LocalBootstrapSession POST /v1/auth/session —— 本地模式免口令换取会话。
//
// 响应形状与 POST /v1/auth/login 完全一致（{token, user}）并同样下发 httpOnly cookie：
// 前端 `ensureLocalSession` 已经按这个形状在读，SSE（EventSource 不能带自定义头）
// 也依赖 cookie 这一条通道。
func (h *AuthHandler) LocalBootstrapSession(w http.ResponseWriter, r *http.Request) {
	// 路由注册已经按 LOCAL_BOOTSTRAP 拦过一层，这里再判来源：开关是按进程判的，
	// 而"谁能免口令拿到 owner"是按连接判的，两者不是一回事，不能互相代替。
	if !isLoopbackPeer(r.RemoteAddr) {
		slog.Warn("local bootstrap rejected: non-loopback peer", "remote_addr", r.RemoteAddr)
		Forbidden(w, "local session bootstrap is only available from this machine")
		return
	}

	ctx := r.Context()
	user, err := ensureLocalOwner(ctx)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "local session bootstrap failed")
		return
	}

	token, err := h.auth.GenerateToken(user.ID, user.Email, user.Role, DefaultTenantID,
		resolveUserPermsDefault(ctx, user.ID, user.Role))
	if err != nil {
		InternalError(w, "local session bootstrap failed")
		return
	}

	SetTokenCookie(w, token, int(h.cfg.JWTExpiration.Seconds()), h.cfg.CookieSecure)
	db.AuditLog(ctx, user.ID, DefaultTenantID, "login_success", "/v1/auth/session",
		"email="+maskEmail(user.Email)+" local_bootstrap", r.RemoteAddr, nil)
	OK(w, map[string]interface{}{
		"token": token,
		"user":  user,
	})
}

// ensureLocalOwner 幂等地保证默认租户里存在一个 owner 账号，并返回它。
//
// 账号是"不可口令登录"的：password_hash 存随机 32 字节的 bcrypt（与 SSO 建号同一做法，
// 见 sso_identity.go::provisionAndBind），password_set 置 FALSE。于是：
//   - 本机：免口令走本端点，永远能进；
//   - 局域网：只能走 POST /v1/auth/login，而没人知道那个随机口令 ⇒ 必然失败，
//     直到本机用户在「设置 → 账户」里首设口令。POST /v1/auth/password 对
//     password_set=FALSE 的账号**免验旧密码**，所以这里不需要任何新界面。
//
// 这一串性质依赖"password_set 为 FALSE 时口令登录不可用"。它由 bcrypt 的随机性保证
// （猜中随机 32 字节的概率可忽略），而不是由登录路径显式拒绝 —— 与 SSO 建号同一口径，
// 不额外发明本地特例。
func ensureLocalOwner(ctx context.Context) (UserResponse, error) {
	var zero UserResponse

	tx, err := db.GlobalDBManager.Begin(ctx)
	if err != nil {
		return zero, err
	}
	defer tx.Rollback(ctx)

	// 租户上下文：users 上有 RLS，与 Login/Session 一样先设好再查。
	if _, err := tx.Exec(ctx,
		"SELECT set_config('app.current_tenant_id', $1, true)", tenantStr(DefaultTenantID)); err != nil {
		return zero, err
	}
	// 咨询锁把"查存在 → 不存在就建"串行化：桌面壳首启时窗口可能在后端就绪前重试，
	// 两个并发请求都读到空表会各插一行（唯一索引会把第二个拦下，但拦下是 500，
	// 对一个"每次启动都要成功"的端点来说不可接受）。
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext('chiron_local_bootstrap'))`); err != nil {
		return zero, err
	}

	var user UserResponse
	err = tx.QueryRow(ctx,
		`SELECT id, email, name, role FROM users WHERE tenant_id = $1 AND email = $2`,
		DefaultTenantID, localOwnerEmail,
	).Scan(&user.ID, &user.Email, &user.Name, &user.Role)
	if err == nil {
		// 已存在（第二次及以后启动）：不动 name/role，用户在管理后台里的改名与降权
		// 必须活过重启 —— 本端点每次启动都会被调用，写成 UPSERT 会把改动抹掉。
		return user, tx.Commit(ctx)
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return zero, err
	}

	randomPassword := make([]byte, 32)
	if _, err := rand.Read(randomPassword); err != nil {
		return zero, err
	}
	passwordHash, err := bcrypt.GenerateFromPassword([]byte(hex.EncodeToString(randomPassword)), auth.BcryptCost)
	if err != nil {
		return zero, err
	}

	// id 必须显式给：users.id 是 varchar(36) NOT NULL 且无默认值（回归网见
	// user_insert_id_test.go，漏这一列 ⇒ null value in column "id"）。
	err = tx.QueryRow(ctx,
		`INSERT INTO users (id, tenant_id, email, name, password_hash, role, password_set)
		 VALUES (gen_random_uuid(), $1, $2, $3, $4, 'owner', FALSE)
		 RETURNING id`,
		DefaultTenantID, localOwnerEmail, localOwnerName, string(passwordHash),
	).Scan(&user.ID)
	if err != nil {
		return zero, err
	}
	user.Email, user.Name, user.Role = localOwnerEmail, localOwnerName, "owner"

	// 成员关系与 users 的 INSERT 同事务：漏了它，C1-4 之后"权限优先读 tenant_members"
	// 的读取路径会让这个 owner 在团队/企业能力上凭空失权（理由同 Register）。
	if err := addTenantMember(ctx, tx, DefaultTenantID, user.ID, user.Role); err != nil {
		return zero, err
	}

	if err := tx.Commit(ctx); err != nil {
		return zero, err
	}
	slog.Info("local bootstrap: owner account provisioned", "email", localOwnerEmail)
	return user, nil
}
