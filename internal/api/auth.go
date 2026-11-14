package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/session"
	"github.com/jackc/pgx/v5"
	"golang.org/x/crypto/bcrypt"
)

const tokenCookieName = "chiron_token"

// Email 脱敏：保留前 3 字符 + @ + 域名首字符，其余用 * 遮盖
func maskEmail(email string) string {
	at := strings.Index(email, "@")
	if at < 1 {
		return "***"
	}
	local := email[:at]
	domain := email[at+1:]
	dot := strings.Index(domain, ".")
	if dot < 1 {
		return "***@" + domain
	}
	if len(local) <= 3 {
		return local[:1] + "***@" + domain[:1] + "***" + domain[dot:]
	}
	return local[:3] + "***@" + domain[:1] + "***" + domain[dot:]
}

// maskPhone 保留前 3 位与后 2 位，中间打码（如 138****12）。
// 与 maskEmail 同为日志脱敏：审计流水里出现完整手机号，等于把用户 PII 长期留痕。
func maskPhone(phone string) string {
	if len(phone) < 7 {
		return "***"
	}
	return phone[:3] + "****" + phone[len(phone)-2:]
}

// DefaultTenantID is the default tenant ID for single-tenant mode.
const DefaultTenantID = db.DefaultTenantID

// inviteStore 是 consumeInvite 真正需要的最小能力。
//
// 不用 `pgx.Tx` 直接做形参，是为了让"单语句语义"可被单测钉住：仓库没有 pgx 的
// 测试替身（无 pgxmock/sqlmock），单元测试也不允许连真实数据库。收窄成只有一个
// 方法的接口后，测试就能用一个假 Tx 断言**只发了一条语句**、且拿到 ErrNoRows 时
// 映射成哪条文案。`pgx.Tx` 天然满足这个接口，调用点不需要改。
type inviteStore interface {
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// consumeInviteQuery 是消费邀请码的唯一语句。
//
// 提成常量是为了让测试能断言"校验条件与自增在同一条语句里"—— 一旦有人改回
// 「先 SELECT 再 UPDATE」，这个常量里就会出现缺失的 `uses < max_uses`，测试立刻红。
const consumeInviteQuery = `UPDATE tenant_invites SET uses = uses + 1
	  WHERE code = $1
	    AND (expires_at IS NULL OR expires_at > NOW())
	    AND (max_uses = 0 OR uses < max_uses)
	 RETURNING tenant_id`

// consumeInvite 校验并消费一个租户邀请码，返回其对应的租户 ID（F2）。
//
// 校验与 `uses` 自增都在**注册事务内**完成：若注册后续步骤失败并回滚，计数一并
// 回滚，不会出现"码被消费但用户没建成"。空码由调用方先行处理（走默认租户）。
//
// **必须是单条语句**。此前是「SELECT 校验 → UPDATE 自增」两步，中间没有行锁、
// UPDATE 也不带 `uses < max_uses` 条件，于是 `max_uses = 1` 的码能被并发注册用两次：
// 两个事务同时通过 SELECT（READ COMMITTED 下各自读快照），随后各自 +1。
//
// 合并成 `UPDATE ... WHERE ... RETURNING` 后，PostgreSQL 会对目标行加行锁，并在
// 拿到锁后**重新评估 WHERE**：第二个事务发现 `uses < max_uses` 已不成立，影响 0 行，
// 于是拿到 `pgx.ErrNoRows` —— 判重与自增成了同一个原子操作。
func consumeInvite(ctx context.Context, tx inviteStore, code string) (int16, error) {
	var tenantID int16
	// 条件与返回值都在这一句里：不存在 / 已过期 / 已达上限三种情况都表现为
	// 影响 0 行（ErrNoRows），对外合并成同一条文案 —— 区分它们只会让攻击者
	// 能枚举出"这个码存在且已用满"。
	err := tx.QueryRow(ctx, consumeInviteQuery, code).Scan(&tenantID)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, errors.New("邀请码无效、已过期或已达使用上限")
	}
	if err != nil {
		slog.Error("consume invite: update failed", "error", err)
		return 0, errors.New("邀请码校验失败")
	}
	return tenantID, nil
}

type AuthHandler struct {
	auth    *auth.Authenticator
	cfg     *config.Config
	captcha *CaptchaHandler
	// lockout 是**账号维度**的密码登录失败锁定（见 login_lockout.go）。
	// 与 captcha 的 IP 维度计数并存：后者挡单机爆破，前者挡分布式撞库。
	lockout *loginLockout
	// mfaLimiter 按用户限制 TOTP 验证码尝试次数（见 mfa.go）。
	// 6 位数字只有 10^6 种组合，不限次数的话 MFA 只是"多花几分钟"。
	mfaLimiter *mfaAttemptLimiter
	// mail 可选：注入后注册流程支持"邮箱验证码校验 + 欢迎邮件"。
	mail *MailHandler
	// emailBloom 可选：注入后注册流程先用布隆过滤器快速否定"该邮箱未注册"，
	// 从而跳过昂贵的 DB 判重。nil 表示未启用（始终回落 DB 查证）。
	emailBloom *userEmailBloom
	// notifier 可选：注入后凭邀请码注册成功会给新成员本人发一条站内信（P2-3）。
	notifier notificationWriter
	// sessionMgr 可选：注入后注销冻结时能打断该用户正在跑的回合。
	// nil 时只冻结资产、不打断（会话会在下次交互时自然失败）。
	sessionMgr *session.Manager
}

// SetSessionManager injects the session manager (注销冻结时打断运行中的回合).
func (h *AuthHandler) SetSessionManager(m *session.Manager) {
	h.sessionMgr = m
}

func NewAuthHandler(cfg *config.Config) *AuthHandler {
	return &AuthHandler{
		auth:       auth.NewAuthenticator(cfg.JWTSecret, cfg.JWTExpiration),
		cfg:        cfg,
		lockout:    newLoginLockout(),
		mfaLimiter: newMFAAttemptLimiter(),
	}
}

// SetCaptchaHandler injects the captcha handler.
func (h *AuthHandler) SetCaptchaHandler(c *CaptchaHandler) {
	h.captcha = c
}

// SetMailHandler injects the mail handler (邮箱验证码注册校验 / 欢迎邮件).
func (h *AuthHandler) SetMailHandler(m *MailHandler) {
	h.mail = m
}

// SetEmailBloomHandler injects the registration email bloom filter.
// 传 nil 表示关闭该优化（注册流程始终回落 DB 判重）。
func (h *AuthHandler) SetEmailBloomHandler(b *userEmailBloom) {
	h.emailBloom = b
}

// SetNotifier 装配通知中心（P2-3）。传 nil 即关闭通知。
func (h *AuthHandler) SetNotifier(n notificationWriter) { h.notifier = n }

type LoginRequest struct {
	Email          string `json:"email"`
	Password       string `json:"password"`
	CaptchaToken   string `json:"captcha_token"`
	CaptchaRandstr string `json:"captcha_randstr"`
}

type UserResponse struct {
	ID    string `json:"id"`
	Email string `json:"email"`
	Name  string `json:"name"`
	Role  string `json:"role"`
	// AccountStatus 是注销状态（active / pending_deletion / purged）。
	//
	// 随登录响应下发，前端才能在**登录成功后立刻**把用户导向受限页面
	// （只留设置页的导出与撤销），而不是等他点开对话页撞 403。
	// 空串按 active 处理（兼容未填该字段的旧响应）。
	AccountStatus string `json:"account_status,omitempty"`
}

// SetTokenCookie sets the JWT as an HTTP-only secure cookie.
func SetTokenCookie(w http.ResponseWriter, token string, maxAge int, secure bool) {
	http.SetCookie(w, &http.Cookie{
		Name:     tokenCookieName,
		Value:    token,
		Path:     "/",
		HttpOnly: true,
		Secure:   secure,
		SameSite: http.SameSiteLaxMode,
		MaxAge:   maxAge,
	})
}

func ClearTokenCookie(w http.ResponseWriter, secure bool) {
	http.SetCookie(w, &http.Cookie{
		Name:     tokenCookieName,
		Value:    "",
		Path:     "/",
		HttpOnly: true,
		Secure:   secure,
		SameSite: http.SameSiteLaxMode,
		MaxAge:   -1,
	})
}

// logLoginFailure 统一的登录失败审计日志+验证码记录
func (h *AuthHandler) logLoginFailure(ctx context.Context, r *http.Request, email string, tenantID int16, remoteAddr string) {
	db.AuditLog(ctx, "", tenantID, "login_failed", "/v1/auth/login", "email="+maskEmail(email), remoteAddr, nil)
	if h.captcha != nil {
		// 必须传真实请求：RecordFailure 会经 clientIP(r) 读 r.RemoteAddr，
		// 传 nil 会让任何登录失败都触发 nil 解引用 panic（表现为 500）。
		h.captcha.RecordFailure(ctx, r)
	}
	// 账号维度计数：与上面的 IP 计数是**两层**防护，不是替代关系。
	// 分布式撞库每次来自不同 IP，IP 计数永远到不了阈值，只有账号维度能兜住。
	// 放在这里而不是各失败分支里，是为了确保"用户不存在"与"密码错误"走同一条
	// 路径 —— 两者计数不一致会让攻击者借此枚举账号是否存在。
	if h.lockout != nil {
		h.lockout.RecordFailure(ctx, email)
	}
}

func (h *AuthHandler) Login(w http.ResponseWriter, r *http.Request) {
	var req LoginRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if req.Email == "" || req.Password == "" {
		BadRequest(w, "email and password are required")
		return
	}
	if len(req.Email) > 255 {
		BadRequest(w, "email too long")
		return
	}
	if len(req.Password) > 128 {
		BadRequest(w, "password too long")
		return
	}

	// 账号级锁定检查：必须在密码校验**之前**。
	//
	// 放在 bcrypt 之后是无效的 —— 那样每次尝试仍会跑完整次哈希（CPU 成本不变），
	// 而且攻击者能从"响应时间长短"判断账号是否存在、密码对不对。
	//
	// 账号不存在时也走同样的判断（不区分），否则"是否被锁"本身就成了账号枚举信号。
	ctx := r.Context()
	if h.lockout != nil && h.lockout.Locked(ctx, req.Email) {
		db.AuditLog(ctx, "", DefaultTenantID, "login_locked_out", "/v1/auth/login",
			"email="+maskEmail(req.Email), r.RemoteAddr, nil)
		// 429 而不是 401：这是"你试太多次了，等一会儿"，与"密码错"是两回事，
		// 用户需要可操作的区分（前端据此提示等待时长）。
		JSON(w, http.StatusTooManyRequests, APIResponse{
			Success: false,
			Error:   "too many failed attempts; account temporarily locked for 15 minutes",
		})
		return
	}

	// 人机验证栅栏：启用后按风险升级强制校验；达到硬上限直接 429
	if h.captcha != nil {
		if err := h.captcha.Enforce(w, r, &auth.CaptchaToken{
			Token:   req.CaptchaToken,
			Randstr: req.CaptchaRandstr,
		}); err != nil {
			return
		}
	}

	// No dev bypass — always validate against DB

	// 设置租户上下文以绕过 RLS
	tx, err := db.GlobalDBManager.Begin(ctx)
	if err != nil {
		slog.Error("begin tx for tenant context", "error", err)
		InternalError(w, "login failed")
		return
	}
	defer tx.Rollback(ctx)

	if _, err := tx.Exec(ctx, "SELECT set_config('app.current_tenant_id', $1, true)", tenantStr(DefaultTenantID)); err != nil {
		slog.Error("set tenant context", "error", err)
		InternalError(w, "login failed")
		return
	}

	var user UserResponse
	var passwordHash string
	var tenantID int16
	var totpEnabled bool
	// F2：不再用 `tenant_id = DefaultTenantID` 过滤 —— 经邀请码注册的用户属于别的
	// 租户，硬过滤会让它们永远登不进来。改为按 email 取用户，并在同 email 跨租户
	// 重名时优先默认租户（存量账号行为不变）。
	err = tx.QueryRow(ctx,
		`SELECT id, email, name, role, tenant_id, password_hash, totp_enabled,
		        COALESCE(account_status, 'active')
		   FROM users
		  WHERE email = $1
		  ORDER BY (tenant_id = $2) DESC, created_at ASC
		  LIMIT 1`,
		req.Email, DefaultTenantID,
	).Scan(&user.ID, &user.Email, &user.Name, &user.Role, &tenantID, &passwordHash,
		&totpEnabled, &user.AccountStatus)
	if err != nil {
		slog.Warn("login failed", "email", maskEmail(req.Email), "error", err)
		h.logLoginFailure(r.Context(), r, req.Email, DefaultTenantID, r.RemoteAddr)
		Unauthorized(w, "invalid email or password")
		return
	}

	if err := bcrypt.CompareHashAndPassword([]byte(passwordHash), []byte(req.Password)); err != nil {
		h.logLoginFailure(r.Context(), r, req.Email, tenantID, r.RemoteAddr)
		Unauthorized(w, "invalid email or password")
		return
	}

	if h.captcha != nil {
		h.captcha.ClearFailures(r.Context(), r)
	}
	// 密码验证通过即清空账号失败计数：否则合法用户偶尔打错几次，
	// 会在一段时间内持续逼近阈值（每次成功都不归零）。
	if h.lockout != nil {
		h.lockout.Clear(ctx, req.Email)
	}

	// multi-tenant isolation
	// P1-5: tenant_id 为空直接拒绝登录
	if tenantID == 0 {
		slog.Warn("login rejected: user has null tenant_id", "user_id", user.ID)
		Unauthorized(w, "user has no tenant binding; contact admin")
		return
	}

	// 已启用双因子：**绝不在此签发正式 token**。
	//
	// 这是整个 MFA 的关键分界 —— 密码正确只证明"知道密码"，还差"持有设备"。
	// 若这里吐 token，等于 MFA 可以被一次请求跳过。
	// 返回的是短期挑战令牌（独立派生密钥签名，无法通过 AuthMiddleware 的校验），
	// 客户端拿它去 /v1/auth/mfa/verify 换正式 token。
	if totpEnabled {
		challenge, cErr := h.auth.GenerateMFAChallengeToken(user.ID)
		if cErr != nil {
			slog.Error("mfa: generate challenge failed", "user_id", user.ID, "error", cErr)
			InternalError(w, "authentication failed")
			return
		}
		db.AuditLog(r.Context(), user.ID, tenantID, "login_mfa_challenged", "/v1/auth/login",
			"email="+maskEmail(req.Email), r.RemoteAddr, nil)
		OK(w, map[string]interface{}{
			"mfa_required":    true,
			"challenge_token": challenge,
			// 明确告知有效期：用户输错几次后挑战过期需要重新登录，
			// 不说明的话会以为是"密码又被拒了"。
			"expires_in_seconds": auth.MFAChallengeTTLSeconds(),
		})
		return
	}

	// C1-4：权限优先取 tenant_members（角色真相），查不到回退 auth.RolePermissions。
	token, err := h.auth.GenerateToken(user.ID, user.Email, user.Role, tenantID,
		resolveUserPermsDefault(r.Context(), user.ID, user.Role))
	if err != nil {
		InternalError(w, "authentication failed")
		return
	}

	SetTokenCookie(w, token, int(h.cfg.JWTExpiration.Seconds()), h.cfg.CookieSecure)
	db.AuditLog(r.Context(), user.ID, tenantID, "login_success", "/v1/auth/login", "email="+maskEmail(req.Email), r.RemoteAddr, nil)
	OK(w, map[string]interface{}{
		"token": token,
		"user":  user,
	})
}

type RegisterRequest struct {
	Email    string `json:"email"`
	Password string `json:"password"`
	Name     string `json:"name"`
	// EmailCode 邮箱验证码：后台开启「注册邮箱验证」(register_verify) 时必填。
	EmailCode      string `json:"email_code"`
	CaptchaToken   string `json:"captcha_token"`
	CaptchaRandstr string `json:"captcha_randstr"`
	// InviteCode 租户邀请码（F2）：非空时新用户归入该码对应的租户，
	// 为空则归入默认租户（存量单租户行为不变）。
	InviteCode string `json:"invite_code"`
}

func (h *AuthHandler) Register(w http.ResponseWriter, r *http.Request) {
	if h.cfg.DisableRegistration {
		Forbidden(w, "registration is disabled on this instance")
		return
	}

	var req RegisterRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}

	// 注册接口防刷：人机验证标杆
	if h.captcha != nil {
		if err := h.captcha.Enforce(w, r, &auth.CaptchaToken{
			Token:   req.CaptchaToken,
			Randstr: req.CaptchaRandstr,
		}); err != nil {
			return
		}
	}

	if req.Email == "" || req.Password == "" || req.Name == "" {
		BadRequest(w, "email, password, and name are required")
		return
	}
	if ok, msg := auth.ValidatePasswordComplexity(req.Password); !ok {
		BadRequest(w, msg)
		return
	}
	if len(req.Email) > 255 || len(req.Name) > 128 {
		BadRequest(w, "email or name too long")
		return
	}

	ctx := r.Context()

	// 设置租户上下文（SET LOCAL）：必须在事务中才持续生效
	tx, err := db.GlobalDBManager.Begin(ctx)
	if err != nil {
		slog.Error("begin tx for tenant context", "error", err)
		InternalError(w, "registration failed")
		return
	}
	defer tx.Rollback(ctx)

	// 解析租户归属（F2）：携带邀请码则归入该租户，否则维持默认租户（存量行为）。
	// 校验与计数都在本事务内完成 —— 后续步骤失败回滚时 uses 一并回滚。
	tenantID := DefaultTenantID
	// joinedViaInvite 标记本次注册是否通过邀请码加入了一个既有团队。
	// 只有这种情况才发「已加入团队」通知：默认租户注册是单人开户，
	// 给自己发一条"你加入了团队"没有信息量。
	joinedViaInvite := false
	if code := strings.TrimSpace(req.InviteCode); code != "" {
		resolved, ierr := consumeInvite(ctx, tx, code)
		if ierr != nil {
			BadRequest(w, ierr.Error())
			return
		}
		tenantID = resolved
		joinedViaInvite = true
	}

	if _, err := tx.Exec(ctx, "SELECT set_config('app.current_tenant_id', $1, true)", tenantStr(tenantID)); err != nil {
		slog.Error("set tenant context", "error", err)
		InternalError(w, "registration failed")
		return
	}

	// Check for existing user
	//
	// 布隆过滤器前置快速否定：**仅当**它断言"该租户下这个邮箱一定不存在"时才跳过
	// 这次 DB 判重 —— users 上 email 无索引时这是一次全表扫描，也是注册热路径上
	// 最贵的一步。只要判定为"可能存在"（含预热未完成、跨实例广播滞后的保守态），
	// 就照常查库：唯一索引始终是真相源，过滤器出错最多让我们多查一次库。
	if h.emailBloom.MightExist(tenantStr(tenantID), req.Email) {
		var exists int
		if err := tx.QueryRow(ctx,
			`SELECT COUNT(*) FROM users WHERE email = $1 AND tenant_id = $2`,
			req.Email, tenantID,
		).Scan(&exists); err != nil {
			slog.Error("check existing user", "error", err)
			InternalError(w, "registration failed")
			return
		}
		if exists > 0 {
			// 用户枚举缓解：不再回 "email already registered" —— 那等于给出"该邮箱已注册"
			// 的确定性答案，可被批量用来探测账号。改成与其它注册失败同形的模糊提示，
			// 真实原因只进结构化日志。注册接口本身另有验证码 + 限流兜底。
			slog.Info("registration rejected: email already exists", "tenant", tenantID)
			BadRequest(w, "registration could not be completed")
			return
		}
	}

	// 邮箱验证码校验（后台开启「注册邮箱验证」时必填）。
	// 刻意放在"邮箱是否已被占用"之后：邮箱已注册是注册流程最常见的失败，
	// 若先消费验证码，用户改个邮箱就要重新收信，体验很差。
	if h.mail != nil {
		required, verr := h.mail.RegisterVerifyEnabled(ctx)
		if verr != nil {
			logAndRespond(w, verr, http.StatusInternalServerError, ErrDBUnavailable)
			return
		}
		if required {
			if strings.TrimSpace(req.EmailCode) == "" {
				BadRequest(w, "请先获取邮箱验证码")
				return
			}
			if cerr := h.mail.ConsumeCode(ctx, mailPurposeRegister, req.Email, req.EmailCode); cerr != nil {
				if errors.Is(cerr, errCodeStoreUnavailable) {
					ServiceUnavailable(w, "验证码存储不可用")
					return
				}
				BadRequest(w, cerr.Error())
				return
			}
		}
	}

	// 首个注册用户成为系统管理员（owner）：以 users 表是否为空判定；
	// 咨询锁保证并发注册时只有一个请求能成为 owner，其余按普通用户落库。
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext('chiron_first_user'))`); err != nil {
		slog.Error("acquire first-user lock", "error", err)
		InternalError(w, "registration failed")
		return
	}
	var userTotal int
	if err := tx.QueryRow(ctx, `SELECT COUNT(*) FROM users`).Scan(&userTotal); err != nil {
		slog.Error("count users for first-user check", "error", err)
		InternalError(w, "registration failed")
		return
	}
	role := "user"
	if userTotal == 0 {
		role = "owner"
		slog.Info("first registered user becomes owner", "email", maskEmail(req.Email))
	}

	hash, err := bcrypt.GenerateFromPassword([]byte(req.Password), auth.BcryptCost)
	if err != nil {
		InternalError(w, "registration failed")
		return
	}

	// 使用 PostgreSQL 的 gen_random_uuid() 生成 UUID
	var userID string
	err = tx.QueryRow(ctx,
		`INSERT INTO users (id, tenant_id, email, name, password_hash, role, created_at, updated_at)
		 VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, NOW(), NOW())
		 RETURNING id`,
		tenantID, req.Email, req.Name, string(hash), role,
	).Scan(&userID)
	if err != nil {
		// 唯一索引兜底：布隆过滤器同步滞后（预热未完成 / 广播延迟 / 实例刚重启）时，
		// 重复邮箱会走到这里被拦下。返回与"邮箱已存在"同形的模糊提示以避免用户枚举。
		// users 的 INSERT 除 (tenant_id, email) 外没有会冲突的唯一列（id 用
		// gen_random_uuid、其余唯一列为空），所以 23505 即等价于邮箱冲突。
		if isUniqueViolation(err) {
			slog.Info("registration rejected: email already exists (unique index)", "tenant", tenantID)
			BadRequest(w, "registration could not be completed")
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "registration failed")
		return
	}

	// 租户成员关系：与 users 的 INSERT 在同一事务内，避免出现「用户已存在但
	// 没有 tenant_members 记录」的窗口 —— 读取路径一旦切到 tenant_members，
	// 这类用户会对团队能力凭空消失（roadmap §12.4 灰度第 2 步）。
	//
	// 失败即整体回滚而不是只告警：账已建好却没有成员记录，是后续所有团队
	// 功能的静默失效源，比注册失败更难排查。
	if err := addTenantMember(ctx, tx, tenantID, userID, role); err != nil {
		slog.Error("add tenant member failed", "user_id", userID, "tenant", tenantID, "error", err)
		InternalError(w, "registration failed")
		return
	}

	// 鎻愪氦浜嬪姟
	if err := tx.Commit(ctx); err != nil {
		slog.Error("commit tx", "error", err)
		InternalError(w, "registration failed")
		return
	}

	// 登记到布隆过滤器并广播给其他副本。放在 commit 之后是有意为之：只有真正落库的
	// 邮箱才值得登记；若在 commit 前登记而事务回滚，会留下一个永久的假阳性 —— 无害
	// 但降低过滤器的过滤率。广播失败同样无害（其他副本只是回落 DB 多查一次）。
	if h.emailBloom != nil {
		h.emailBloom.Add(tenantStr(tenantID), req.Email)
		broadcastUserEmailBloomAdd(tenantStr(tenantID), req.Email)
	}

	token, err := h.auth.GenerateToken(userID, req.Email, role, tenantID,
		resolveUserPermsDefault(r.Context(), userID, role))
	if err != nil {
		InternalError(w, "authentication failed")
		return
	}

	// 新用户引导：为这个账号建好三步的 pending 行（P0-3）。
	//
	// 放在 commit **之后**：引导进度写不进去不能让一个已经合法的账号注册失败。
	// 存量用户（没有这一步之前注册的）因此不会在该表里留下行 ⇒ 前端判定
	// eligible=false，不弹引导，符合 roadmap「不打断已有用户」。
	StartOnboarding(ctx, userID)

	// 团队加入通知（P2-3 的 member_invited）：必须放在 commit 之后 ——
	// 事务回滚时用户并不存在，通知会指向一个不存在的账号。
	//
	// 语义说明：`tenant_invites` 只有 code/tenant_id/created_by/... 而**没有
	// email 列**（基线 0001:725），是通用邀请码而非指向具体人的邀请，创建时
	// 没有确定的收件人。所以这个事件落在"成员真正加入"的这一刻，通知对象是
	// 新成员本人，而不是"邀请发出时的被邀请人"。
	if joinedViaInvite {
		h.notifyMemberJoined(r.Context(), tenantID, userID, req.Name)
	}

	// 欢迎邮件：异步发送（不阻塞、不因发信失败而回滚已创建的账号）。
	if h.mail != nil {
		h.mail.SendWelcomeAsync(req.Email, req.Name)
	}

	SetTokenCookie(w, token, int(h.cfg.JWTExpiration.Seconds()), h.cfg.CookieSecure)
	Created(w, map[string]interface{}{
		"token": token,
		"user":  UserResponse{ID: userID, Email: req.Email, Name: req.Name, Role: role},
	})
}

func (h *AuthHandler) Logout(w http.ResponseWriter, r *http.Request) {
	// ── JWT 黑名单：将该 token 加入 Redis 黑名单，TTL 等于剩余有效期 ──
	if claims := auth.GetClaims(r.Context()); claims != nil && claims.ID != "" && db.Redis != nil {
		remaining := time.Until(claims.ExpiresAt.Time)
		if remaining > 0 {
			db.Redis.Set(r.Context(), db.RedisKey("jwt:blacklist:")+claims.ID, "1", remaining)
			// 跨实例同步黑名单
			broadcastBlacklistSync(claims.ID)
		}
	}
	// 同步本地缓存，确保本实例后续请求立即拒绝该 token
	if claims := auth.GetClaims(r.Context()); claims != nil {
		markJWTBlacklisted(claims.ID)
	}
	ClearTokenCookie(w, h.cfg.CookieSecure)
	OK(w, map[string]string{"message": "logged out"})
}

func (h *AuthHandler) Profile(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, "not authenticated")
		return
	}

	var settings map[string]interface{}
	var accountStatus string
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT COALESCE(settings::jsonb, '{}'::jsonb), COALESCE(account_status, 'active')
		   FROM users WHERE id = $1`, claims.UserID).Scan(&settings, &accountStatus); err != nil {
		settings = map[string]interface{}{}
		// 读不到状态时按 active 处理：这是"展示层"的判断依据，
		// 真正的拦截在 AccountDeletionGate（服务端 fail-close）。
		// 若这里也 fail-close，DB 抖动会让所有正常用户的设置页都报错。
		accountStatus = accountStatusActive
	}
	OK(w, map[string]interface{}{
		"user_id":  claims.UserID,
		"email":    claims.Email,
		"role":     claims.Role,
		"perms":    claims.Perms,
		// account_status 供前端路由守卫把注销中的账号限制在设置页。
		"account_status": accountStatus,
		"settings": settings,
	})
}

// UpdateProfile updates the authenticated user's profile (name/email).
func (h *AuthHandler) UpdateProfile(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, "not authenticated")
		return
	}

	var body struct {
		Email    string                 `json:"email"`
		Name     string                 `json:"name"`
		Settings map[string]interface{} `json:"settings"` // 自定义换肤等用户设置（局部合并）
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.Email == "" && body.Name == "" && body.Settings == nil {
		BadRequest(w, "no fields to update")
		return
	}

	setClauses := ""
	args := []interface{}{}
	argIdx := 1
	if body.Email != "" {
		setClauses += fmt.Sprintf("email = $%d, ", argIdx)
		args = append(args, body.Email)
		argIdx++
	}
	if body.Name != "" {
		setClauses += fmt.Sprintf("name = $%d, ", argIdx)
		args = append(args, body.Name)
		argIdx++
	}
	if body.Settings != nil {
		settingsJSON, err := json.Marshal(body.Settings)
		if err != nil {
			InternalError(w, "invalid settings")
			return
		}
		// 局部合并：settings = settings || $n
		setClauses += fmt.Sprintf("settings = COALESCE(settings::jsonb, '{}'::jsonb) || $%d::jsonb, ", argIdx)
		args = append(args, string(settingsJSON))
		argIdx++
	}
	setClauses = strings.TrimSuffix(setClauses, ", ")
	if setClauses == "" {
		// 三个字段都没给：直接返回而不是让 SQL 走成 `UPDATE users SET , updated_at ...`。
		// 空 PATCH 视为「无变更」，客户端重试时不该拿到 500。
		OK(w, map[string]string{"status": "unchanged"})
		return
	}
	args = append(args, claims.UserID)

	if _, err := db.GlobalDBManager.Exec(r.Context(),
		fmt.Sprintf("UPDATE users SET %s, updated_at = NOW() WHERE id = $%d", setClauses, argIdx),
		args...); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "update profile failed")
		return
	}

	OK(w, map[string]string{"status": "updated"})
}

type RefreshRequest struct {
	Token string `json:"token"`
}

// Session returns the current session from the httpOnly cookie.
func (h *AuthHandler) Session(w http.ResponseWriter, r *http.Request) {
	cookie, err := r.Cookie(tokenCookieName)
	if err != nil || cookie.Value == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	claims, err := h.auth.ValidateToken(cookie.Value)
	if err != nil || claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	ctx := r.Context()
	// 租户取 token 里的（Refresh 会按 users 表重取后重签，所以这里不会用到过期的归属）。
	// 此前这里硬编码 DefaultTenantID：`WHERE id=$1 AND tenant_id=$2` 于是恒不匹配
	// 非默认租户的用户 —— 他们每次刷新页面都 401，且 set_config 也把 RLS 上下文
	// 指到了别的租户。
	tenantID := claims.TenantID
	if tenantID == 0 {
		tenantID = DefaultTenantID
	}
	tx, err := db.GlobalDBManager.Begin(ctx)
	if err != nil {
		slog.Error("begin tx for tenant context", "error", err)
		InternalError(w, "session lookup failed")
		return
	}
	defer tx.Rollback(ctx)
	if _, err := tx.Exec(ctx, "SELECT set_config('app.current_tenant_id', $1, true)", tenantStr(tenantID)); err != nil {
		slog.Error("set tenant context", "error", err)
		InternalError(w, "session lookup failed")
		return
	}

	var user UserResponse
	if err := tx.QueryRow(ctx,
		`SELECT id, email, name, role FROM users WHERE id = $1 AND tenant_id = $2`,
		claims.UserID, tenantID,
	).Scan(&user.ID, &user.Email, &user.Name, &user.Role); err != nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	OK(w, map[string]interface{}{
		"token": cookie.Value,
		"user":  user,
	})
}

func (h *AuthHandler) Refresh(w http.ResponseWriter, r *http.Request) {
	cookie, err := r.Cookie(tokenCookieName)
	if err != nil {
		Unauthorized(w, "not authenticated")
		return
	}

	// 1) 校验 token 未被加入黑名单
	oldClaims, err := h.auth.ValidateToken(cookie.Value)
	if err != nil {
		Unauthorized(w, "session expired")
		return
	}
	if oldClaims.ID != "" && db.Redis != nil {
		if n, err := db.Redis.Exists(r.Context(), db.RedisKey("jwt:blacklist:")+oldClaims.ID).Result(); err == nil && n > 0 {
			Unauthorized(w, "token revoked")
			return
		}
	}

	// 2) 校验用户仍然存在，并**重取最新角色/租户**。
	//    此前直接 RefreshToken(旧 token) 会原样复制旧 claims 的 role/permissions，
	//    于是管理员被降权后，只要在旧 token 有效期内刷新，权限就被「续期」——
	//    降权不生效（P1）。这里改为从 users 表读当前 role 与 tenant_id 后重新签发。
	var dbEmail, dbRole string
	var dbTenant int16
	err = db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT COALESCE(email, ''), COALESCE(role, ''), COALESCE(tenant_id, 0)
		   FROM users WHERE id = $1`,
		oldClaims.UserID).Scan(&dbEmail, &dbRole, &dbTenant)
	if err != nil {
		Unauthorized(w, "user not found")
		return
	}
	if dbTenant == 0 {
		dbTenant = DefaultTenantID
	}

	// 权限按**当前角色**重新计算，而非沿用旧 token 的快照。
	newToken, err := h.auth.GenerateToken(
		oldClaims.UserID, dbEmail, dbRole, dbTenant,
		resolveUserPermsDefault(r.Context(), oldClaims.UserID, dbRole))
	if err != nil {
		Unauthorized(w, "session expired")
		return
	}

	SetTokenCookie(w, newToken, int(h.cfg.JWTExpiration.Seconds()), h.cfg.CookieSecure)
	OK(w, map[string]string{"message": "token refreshed"})
}
