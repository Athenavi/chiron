package api

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/pquerna/otp"
	"github.com/pquerna/otp/totp"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// mfa.go —— 用户级 TOTP 双因子认证。
//
// 企业采购安全问卷几乎必问「是否支持双因子」，而此前密码是唯一因子：
// 一旦泄露即账号失守（且这个平台能开 agent、跑工具、读知识库，代价很高）。
//
// 三个刻意的设计决定：
//
//  1. **密钥加密落库**：TOTP secret 等价于"永久密码"——拿到它就能无限生成
//     有效验证码。因此用 `auth.EncryptAESGCM`（密钥沿用 ENT_OIDC_SECRET_KEY
//     或 APP_SECRET）后再写 `users.totp_secret_enc`，绝不存明文。
//
//  2. **`totp_enabled` 与 secret 分离**：扫码后若没输入验证码就关掉页面，
//     secret 已有值但用户从未证明他能生成正确验证码。此时若算作"已启用"，
//     他下次登录会被要求输入一个从未配置成功的码，直接把自己锁在门外。
//     所以只有 `Confirm` 成功才置 `totp_enabled = true`。
//
//  3. **备用码哈希存储**：备用码是"手机丢了"时的唯一出路，但它本身等价于
//     一次性密码。只存 SHA-256，比对时用常数时间比较，用掉一个就从列表里删掉。

const (
	// mfaIssuer 显示在验证器 App 里（如 Google Authenticator 的条目名）。
	mfaIssuer = "Chiron"
	// mfaRecoveryCodeCount 备用码数量。10 个足够覆盖"换手机 + 几次误用"，
	// 又不至于让用户打印一长串。
	mfaRecoveryCodeCount = 10
	// mfaRecoveryCodeBytes 每个备用码的随机字节数（编码后 16 个 hex 字符）。
	mfaRecoveryCodeBytes = 8
	// mfaTotpSkew 允许前后各 1 个时间片（±30s）。这是验证器 App 与服务器
	// 时钟轻微漂移的容错；再大就显著扩大暴力破解窗口了。
	mfaTotpSkew = 1
)

var (
	errMFANotConfigured = errors.New("mfa not configured")
	errMFAInvalidCode   = errors.New("invalid verification code")
)

// mfaEnrollment 是"生成密钥但尚未确认"时返回给前端的载荷。
type mfaEnrollment struct {
	// Secret 是 base32 明文，仅用于**本次**展示给用户手动录入；
	// 之后只能从数据库密文解出，绝不出现在任何读接口里。
	Secret string
	// URL 是 otpauth:// 链接，前端可直接生成二维码。
	URL string
	// RecoveryCodes 是明文备用码，仅此一次返回（库里只有哈希）。
	RecoveryCodes []string
}

// generateMFAEnrollment 生成新密钥与备用码。
//
// 返回的 enrollment 只应展示一次；调用方负责把 secret 加密后落库、
// 把 recovery codes 的哈希落库。
func generateMFAEnrollment(accountName string) (mfaEnrollment, error) {
	key, err := totp.Generate(totp.GenerateOpts{
		Issuer:      mfaIssuer,
		AccountName: accountName,
		Period:      30,
		Digits:      otp.DigitsSix,
		Algorithm:   otp.AlgorithmSHA1, // 兼容性最好：所有验证器 App 都支持
	})
	if err != nil {
		return mfaEnrollment{}, fmt.Errorf("generate totp key: %w", err)
	}

	codes, err := generateRecoveryCodes(mfaRecoveryCodeCount)
	if err != nil {
		return mfaEnrollment{}, err
	}

	return mfaEnrollment{
		Secret:        key.Secret(),
		URL:           key.URL(),
		RecoveryCodes: codes,
	}, nil
}

// generateRecoveryCodes 生成明文备用码（形如 `a1b2-c3d4-e5f6-g7h8`）。
// 分组展示只是为了用户抄写时不易错，校验前会去掉分隔符。
func generateRecoveryCodes(n int) ([]string, error) {
	out := make([]string, 0, n)
	for i := 0; i < n; i++ {
		buf := make([]byte, mfaRecoveryCodeBytes)
		if _, err := rand.Read(buf); err != nil {
			return nil, fmt.Errorf("generate recovery code: %w", err)
		}
		h := hex.EncodeToString(buf)
		out = append(out, h[0:4]+"-"+h[4:8]+"-"+h[8:12]+"-"+h[12:16])
	}
	return out, nil
}

// normalizeRecoveryCode 去掉分隔符与空白并统一小写，让用户抄错大小写/多打空格
// 也能通过。这是"备用码在紧急情况下使用"的前提——那时用户没有耐心排查格式。
func normalizeRecoveryCode(code string) string {
	replacer := strings.NewReplacer("-", "", " ", "", "\t", "")
	return strings.ToLower(replacer.Replace(code))
}

// hashRecoveryCode 返回备用码的 SHA-256 十六进制。
//
// 用裸 SHA-256 而非 bcrypt：备用码是 8 字节随机数，熵足够高（64 bit），
// 不存在可被字典攻击的弱口令空间；bcrypt 在这里只会让每次校验多花 100ms，
// 而备用码校验恰恰发生在"用户急着登录"的时刻。
func hashRecoveryCode(normalized string) string {
	sum := sha256.Sum256([]byte(normalized))
	return hex.EncodeToString(sum[:])
}

// hashRecoveryCodes 把明文列表转成可落库的哈希列表。
func hashRecoveryCodes(codes []string) []string {
	out := make([]string, 0, len(codes))
	for _, c := range codes {
		out = append(out, hashRecoveryCode(normalizeRecoveryCode(c)))
	}
	return out
}

// marshalRecoveryHashes 序列化为 jsonb 列的内容。
func marshalRecoveryHashes(hashes []string) ([]byte, error) {
	return json.Marshal(hashes)
}

// unmarshalRecoveryHashes 解析 jsonb 列；空值/坏数据一律当"没有备用码"。
func unmarshalRecoveryHashes(raw []byte) []string {
	if len(raw) == 0 {
		return nil
	}
	var hashes []string
	if err := json.Unmarshal(raw, &hashes); err != nil {
		return nil
	}
	return hashes
}

// validateTOTP 校验 6 位验证码。
//
// 用 ValidateCustom 而不是 Validate：后者的默认 Skew 是 1，但 Period/Digits
// 依赖库内部默认值，显式传参才能在库升级时保持行为不变。
func validateTOTP(secret, code string) bool {
	code = strings.TrimSpace(code)
	if secret == "" || code == "" {
		return false
	}
	ok, err := totp.ValidateCustom(code, secret, time.Now().UTC(), totp.ValidateOpts{
		Period:    30,
		Skew:      mfaTotpSkew,
		Digits:    otp.DigitsSix,
		Algorithm: otp.AlgorithmSHA1,
	})
	if err != nil {
		return false
	}
	return ok
}

// consumeRecoveryCode 在哈希列表里查找并**消耗**匹配项。
//
// 返回 (剩余哈希, 是否命中)。命中时必须把剩余列表写回库——备用码是一次性的，
// 不删就能被反复使用，那和永久密码没有区别。
//
// 用 subtle.ConstantTimeCompare 逐项比较：备用码校验是攻击者可控输入的比对，
// 普通 == 会在字节级短路，理论上可被时间侧信道逐字符猜解（虽然 64 bit 熵
// 让这在实际中很难利用，但常数时间比较的成本几乎为零，没有理由不用）。
func consumeRecoveryCode(hashes []string, input string) ([]string, bool) {
	normalized := normalizeRecoveryCode(input)
	if normalized == "" || len(hashes) == 0 {
		return hashes, false
	}
	target := hashRecoveryCode(normalized)
	remaining := make([]string, 0, len(hashes))
	hit := false
	for _, h := range hashes {
		if !hit && subtle.ConstantTimeCompare([]byte(h), []byte(target)) == 1 {
			hit = true
			continue // 消耗掉，不再放回 remaining
		}
		remaining = append(remaining, h)
	}
	if !hit {
		return hashes, false
	}
	return remaining, true
}

// encryptTOTPSecret / decryptTOTPSecret 是落库格式的唯一出入口。
//
// 加密密钥沿用 ENT_OIDC_SECRET_KEY（缺失时 auth 包会回退到 APP_SECRET）——
// 与其它凭据（SSO secret、provider key）共用同一把部署级主密钥，
// 避免再引入一个需要运维额外管理的环境变量。
func encryptTOTPSecret(secret string) (string, error) {
	key := auth.LoadOIDCEncryptionKey()
	if len(key) == 0 {
		// 没有主密钥时不能静默存明文：那会让"加密落库"的承诺变成谎言。
		return "", errors.New("mfa: encryption key unavailable (set ENT_OIDC_SECRET_KEY or APP_SECRET)")
	}
	return auth.EncryptAESGCM(key, secret)
}

func decryptTOTPSecret(enc string) (string, error) {
	if enc == "" {
		return "", errMFANotConfigured
	}
	key := auth.LoadOIDCEncryptionKey()
	if len(key) == 0 {
		return "", errors.New("mfa: encryption key unavailable")
	}
	return auth.DecryptAESGCM(key, enc)
}

// ── 兑换端点 ──

// mfaVerifyRequest 是 /v1/auth/mfa/verify 的请求体。
type mfaVerifyRequest struct {
	// ChallengeToken 是登录接口在 mfa_required=true 时返回的短期令牌。
	ChallengeToken string `json:"challenge_token"`
	// Code 是 6 位 TOTP，或一个备用码（两者同一个字段：用户不会区分
	// "我现在输的是哪种"，前端也不必多一个切换开关）。
	Code string `json:"code"`
}

// MFAVerify POST /v1/auth/mfa/verify —— 用挑战令牌 + 验证码换取正式 token。
//
// 安全要点（每一条都对应一种常见实现错误）：
//  1. userID **只能**从挑战令牌里取，绝不接受请求体传入 —— 否则攻击者
//     可以指定任意用户去撞验证码。
//  2. 校验前先查尝试次数，超限直接 429，避免 6 位码被枚举。
//  3. TOTP 失败**不算**最终失败：还要试备用码（用户可能就是手机丢了）。
//  4. 备用码命中后立刻从库里删掉（一次一用）。
//  5. 只有当账号仍处于 `totp_enabled = true` 时才接受 —— 用户中途禁用 MFA
//     （或管理员重置）后，未过期的挑战令牌不应还能换 token。
func (h *AuthHandler) MFAVerify(w http.ResponseWriter, r *http.Request) {
	var req mfaVerifyRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if strings.TrimSpace(req.ChallengeToken) == "" || strings.TrimSpace(req.Code) == "" {
		BadRequest(w, "challenge_token and code are required")
		return
	}

	ctx := r.Context()
	userID, err := h.auth.ValidateMFAChallengeToken(req.ChallengeToken)
	if err != nil {
		// 过期/伪造/用错密钥 —— 一律 401，不区分（区分会泄露"令牌是否存在"）。
		Unauthorized(w, "invalid or expired challenge")
		return
	}

	if h.mfaLimiter != nil && h.mfaLimiter.Locked(ctx, userID) {
		db.AuditLog(ctx, userID, DefaultTenantID, "mfa_locked_out", "/v1/auth/mfa/verify",
			"", r.RemoteAddr, nil)
		JSON(w, http.StatusTooManyRequests, APIResponse{
			Success: false,
			Error:   "too many invalid codes; try again later",
		})
		return
	}

	// 重新读用户状态：挑战令牌签发到兑换之间，账号可能已被禁用 MFA 或被删除。
	var (
		user           UserResponse
		tenantID       int16
		totpEnabled    bool
		secretEnc      *string
		recoveryRaw    []byte
	)
	err = db.GlobalDBManager.QueryRow(ctx,
		`SELECT id, email, name, role, tenant_id, totp_enabled, totp_secret_enc, totp_recovery_hashes
		   FROM users WHERE id = $1`, userID).
		Scan(&user.ID, &user.Email, &user.Name, &user.Role, &tenantID,
			&totpEnabled, &secretEnc, &recoveryRaw)
	if err != nil {
		if isNoRows(err) {
			Unauthorized(w, "invalid or expired challenge")
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "mfa verify failed")
		return
	}
	if !totpEnabled || secretEnc == nil || *secretEnc == "" {
		// 状态在挑战之后变了：拒绝，让客户端重新走登录流程。
		Unauthorized(w, "mfa no longer enabled; please sign in again")
		return
	}

	secret, derr := decryptTOTPSecret(*secretEnc)
	if derr != nil {
		slog.Error("mfa: decrypt secret failed", "user_id", userID, "error", derr)
		InternalError(w, "mfa unavailable")
		return
	}

	ok := validateTOTP(secret, req.Code)
	remainingHashes := unmarshalRecoveryHashes(recoveryRaw)
	if !ok {
		// TOTP 不匹配时再试备用码：用户可能正处在"手机丢了"的场景。
		if rest, hit := consumeRecoveryCode(remainingHashes, req.Code); hit {
			ok = true
			remainingHashes = rest
			// 立即落库删掉已用的那个：不删就能重复使用，等价于永久密码。
			payload, mErr := marshalRecoveryHashes(remainingHashes)
			if mErr != nil {
				slog.Error("mfa: marshal recovery hashes failed", "user_id", userID, "error", mErr)
			} else if _, uErr := db.GlobalDBManager.Exec(ctx,
				`UPDATE users SET totp_recovery_hashes = $2 WHERE id = $1`,
				userID, payload); uErr != nil {
				// 写失败意味着这个备用码还能再用一次 —— 必须告警，但它不该
				// 阻止用户本次登录（他甚至可能就是靠它救急的）。
				slog.Error("mfa: persist consumed recovery code failed",
					"user_id", userID, "error", uErr)
			}
			db.AuditLog(ctx, userID, tenantID, "mfa_recovery_code_used",
				"/v1/auth/mfa/verify", "", r.RemoteAddr, nil)
		}
	}

	if !ok {
		if h.mfaLimiter != nil {
			h.mfaLimiter.RecordFailure(ctx, userID)
		}
		db.AuditLog(ctx, userID, tenantID, "mfa_verify_failed", "/v1/auth/mfa/verify",
			"", r.RemoteAddr, nil)
		Unauthorized(w, "invalid verification code")
		return
	}

	if h.mfaLimiter != nil {
		h.mfaLimiter.Clear(ctx, userID)
	}

	token, err := h.auth.GenerateToken(user.ID, user.Email, user.Role, tenantID,
		resolveUserPermsDefault(ctx, user.ID, user.Role))
	if err != nil {
		InternalError(w, "authentication failed")
		return
	}
	SetTokenCookie(w, token, int(h.cfg.JWTExpiration.Seconds()), h.cfg.CookieSecure)
	db.AuditLog(ctx, user.ID, tenantID, "login_success", "/v1/auth/mfa/verify",
		"email="+maskEmail(user.Email)+" mfa=true", r.RemoteAddr, nil)
	OK(w, map[string]interface{}{
		"token": token,
		"user":  user,
	})
}
//
// 6 位数字只有 10^6 种组合。若不限次数，攻击者拿到挑战令牌后（例如通过
// 已泄露的密码）可以在几分钟内枚举出正确码 —— MFA 就退化成"多花几分钟"。
// 因此**按用户**限制尝试次数，与密码锁定同一量级（5 次 / 15 分钟）。
//
// 为什么按 userID 而不是按 IP：撞库/爆破的形态同样是分布式的，
// 而这里的约束对象明确是"某一个账号的验证码"，按 userID 才是正确的判据。
const (
	mfaMaxAttempts  = 5
	mfaAttemptWindow = 15 * time.Minute
	mfaAttemptPrefix = "login:mfa-fail:"
)

type mfaAttemptLimiter struct {
	rdb func() db.RedisClient
}

func newMFAAttemptLimiter() *mfaAttemptLimiter {
	return &mfaAttemptLimiter{rdb: func() db.RedisClient { return db.Redis }}
}

func mfaAttemptKey(userID string) string {
	sum := sha256.Sum256([]byte("mfa:" + userID))
	return mfaAttemptPrefix + hex.EncodeToString(sum[:16])
}

func (l *mfaAttemptLimiter) Locked(ctx context.Context, userID string) bool {
	rdb := l.rdb()
	if rdb == nil || userID == "" {
		return false
	}
	n, err := rdb.Get(ctx, mfaAttemptKey(userID)).Int()
	if err != nil {
		return false
	}
	return n >= mfaMaxAttempts
}

func (l *mfaAttemptLimiter) RecordFailure(ctx context.Context, userID string) {
	rdb := l.rdb()
	if rdb == nil || userID == "" {
		return
	}
	key := mfaAttemptKey(userID)
	if err := rdb.Incr(ctx, key).Err(); err != nil {
		slog.Warn("mfa attempt: incr failed (limiter inactive)", "error", err)
		return
	}
	rdb.Expire(ctx, key, mfaAttemptWindow)
}

func (l *mfaAttemptLimiter) Clear(ctx context.Context, userID string) {
	rdb := l.rdb()
	if rdb == nil || userID == "" {
		return
	}
	if err := rdb.Del(ctx, mfaAttemptKey(userID)).Err(); err != nil {
		slog.Warn("mfa attempt: clear failed", "error", err)
	}
}
