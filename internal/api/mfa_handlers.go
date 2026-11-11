package api

import (
	"log/slog"
	"net/http"
	"strings"

	"golang.org/x/crypto/bcrypt"

	"github.com/athenavi/chiron/internal/db"
)

// mfa_handlers.go —— 用户自助管理 MFA 的端点。
//
// 三个端点构成标准的「两步启用」流程：
//
//	POST /v1/auth/mfa/setup   生成密钥 + 备用码（**此时尚未启用**）
//	POST /v1/auth/mfa/enable  用一次正确验证码确认 → totp_enabled = true
//	POST /v1/auth/mfa/disable 凭密码或验证码关闭
//	GET  /v1/auth/mfa/status  返回当前状态（供安全设置页渲染）
//
// 为什么启用要两步：如果 setup 直接置 enabled，用户扫码后关掉页面（没完成
// 录入）就会在下次登录被要求输入一个他从未成功配置过的验证码 —— 直接锁死
// 自己的账号。`totp_enabled` 只在 confirm 成功后才写 true。

// mfaAccountName 决定验证器 App 里显示的账号名。
//
// 必须有兜底：`users.email` 是**可空**的（手机号注册、SSO-only 账号、历史数据
// 都可能没有 email），而 totp.Generate 在 AccountName 为空时直接报错
// `AccountName must be set` —— 那会让这些用户的 MFA 永远开启失败，且错误
// 只出现在服务端日志里。用 userID 兜底虽然不好看，但保证功能可用。
func mfaAccountName(email, userID string) string {
	if e := strings.TrimSpace(email); e != "" {
		return e
	}
	return "user:" + userID
}

type mfaSetupResponse struct {
	Secret        string   `json:"secret"`
	OTPAuthURL    string   `json:"otpauth_url"`
	RecoveryCodes []string `json:"recovery_codes"`
	// AlreadyEnabled 表示用户此前已启用 MFA：此时重新 setup 会**替换**旧密钥
	// 与备用码，前端应提示"旧验证器将失效"。
	AlreadyEnabled bool `json:"already_enabled"`
}

// MFASetup POST /v1/auth/mfa/setup —— 生成（或重置）MFA 密钥。
//
// 本端点**不会**把 totp_enabled 置 true。密钥先落库（未确认状态），
// 用户拿 otpauth_url 扫码，再用 /enable 提交一次验证码确认。
//
// 已启用时重新 setup = 重置：新密钥落库但 enabled 先置 false，
// 直到用户用新密钥成功确认。这样"换手机"不会出现新旧密钥并存、
// 两个设备都能通过的局面。
func (h *AuthHandler) MFASetup(w http.ResponseWriter, r *http.Request) {
	userID := GetUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	ctx := r.Context()

	var email string
	var enabled bool
	if err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COALESCE(email, ''), totp_enabled FROM users WHERE id = $1`,
		userID).Scan(&email, &enabled); err != nil {
		if isNoRows(err) {
			Unauthorized(w, ErrAuthRequired)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "mfa setup failed")
		return
	}

	enr, err := generateMFAEnrollment(mfaAccountName(email, userID))
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "mfa setup failed")
		return
	}

	secretEnc, err := encryptTOTPSecret(enr.Secret)
	if err != nil {
		// 密钥不可用（未配置 ENT_OIDC_SECRET_KEY / APP_SECRET）时**绝不**
		// 退化成存明文 —— 那会让"加密落库"的承诺变成谎言。
		slog.Error("mfa setup: encrypt secret failed", "user_id", userID, "error", err)
		InternalError(w, "mfa encryption unavailable; contact your administrator")
		return
	}
	hashes, err := marshalRecoveryHashes(hashRecoveryCodes(enr.RecoveryCodes))
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "mfa setup failed")
		return
	}

	// 重置时先关掉 enabled：新密钥在用户成功确认前不该生效，
	// 否则旧设备立刻失效而新设备还没配好，用户被锁在中间态。
	if _, err := db.GlobalDBManager.Exec(ctx,
		`UPDATE users SET totp_secret_enc = $2, totp_enabled = false, totp_recovery_hashes = $3
		   WHERE id = $1`, userID, secretEnc, hashes); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "mfa setup failed")
		return
	}

	db.AuditLog(ctx, userID, GetTenantID(r), "mfa.setup", "/v1/auth/mfa/setup",
		"", r.RemoteAddr, map[string]interface{}{"replacing": enabled})

	OK(w, mfaSetupResponse{
		Secret:         enr.Secret,
		OTPAuthURL:     enr.URL,
		RecoveryCodes:  enr.RecoveryCodes,
		AlreadyEnabled: enabled,
	})
}

type mfaEnableRequest struct {
	Code string `json:"code"`
}

// MFAEnable POST /v1/auth/mfa/enable —— 用一次验证码确认并启用。
//
// 这一步本身证明了"用户已经成功把密钥录入验证器"。没有它，用户很可能带着
// 一个 `totp_enabled=true` 但从未配置成功的账号被锁在门外。
func (h *AuthHandler) MFAEnable(w http.ResponseWriter, r *http.Request) {
	userID := GetUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	var req mfaEnableRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if strings.TrimSpace(req.Code) == "" {
		BadRequest(w, "code is required")
		return
	}

	ctx := r.Context()
	var secretEnc *string
	var enabled bool
	if err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT totp_secret_enc, totp_enabled FROM users WHERE id = $1`,
		userID).Scan(&secretEnc, &enabled); err != nil {
		if isNoRows(err) {
			Unauthorized(w, ErrAuthRequired)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "mfa enable failed")
		return
	}
	if enabled {
		// 已启用还来 enable = 客户端状态错乱。返回明确错误而不是静默成功，
		// 否则前端会以为"刚刚才启用"，与真实状态不一致。
		JSON(w, http.StatusConflict, APIResponse{Success: false, Error: "mfa already enabled"})
		return
	}
	if secretEnc == nil || *secretEnc == "" {
		BadRequest(w, "mfa not set up; call /v1/auth/mfa/setup first")
		return
	}

	secret, err := decryptTOTPSecret(*secretEnc)
	if err != nil {
		slog.Error("mfa enable: decrypt failed", "user_id", userID, "error", err)
		InternalError(w, "mfa unavailable")
		return
	}
	if !validateTOTP(secret, req.Code) {
		db.AuditLog(ctx, userID, GetTenantID(r), "mfa.enable_failed", "/v1/auth/mfa/enable",
			"", r.RemoteAddr, nil)
		Unauthorized(w, "invalid verification code")
		return
	}

	if _, err := db.GlobalDBManager.Exec(ctx,
		`UPDATE users SET totp_enabled = true WHERE id = $1`, userID); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "mfa enable failed")
		return
	}
	db.AuditLog(ctx, userID, GetTenantID(r), "mfa.enabled", "/v1/auth/mfa/enable",
		"", r.RemoteAddr, nil)
	OK(w, map[string]interface{}{"mfa_enabled": true})
}

type mfaDisableRequest struct {
	// Code 是当前 6 位 TOTP **或**一个备用码（手机还在手上的情形）。
	Code string `json:"code"`
	// Password 是账户密码（手机丢了、只想先关掉 MFA 登进来的情形）。
	// 没有它，用户就得先找管理员，而很多部署里根本没有管理员可找。
	Password string `json:"password"`
}

// MFADisable POST /v1/auth/mfa/disable —— 关闭 MFA。
//
// 必须提供**当前**凭证（验证码或密码）之一：光凭一个已登录的会话就能关掉
// 双因子，等于攻击者拿到一次会话就能永久移除这层防护。
func (h *AuthHandler) MFADisable(w http.ResponseWriter, r *http.Request) {
	userID := GetUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	var req mfaDisableRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	code := strings.TrimSpace(req.Code)
	password := strings.TrimSpace(req.Password)
	if code == "" && password == "" {
		BadRequest(w, "verification code or password is required")
		return
	}

	ctx := r.Context()
	// 一次读出全部判定所需字段：备用码列表只查这一次，避免"校验用一份、
	// 消耗写另一份"的不一致。
	var secretEnc *string
	var passwordHash string
	var enabled bool
	var recoveryRaw []byte
	if err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT totp_secret_enc, COALESCE(password_hash, ''), totp_enabled, totp_recovery_hashes
		   FROM users WHERE id = $1`,
		userID).Scan(&secretEnc, &passwordHash, &enabled, &recoveryRaw); err != nil {
		if isNoRows(err) {
			Unauthorized(w, ErrAuthRequired)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "mfa disable failed")
		return
	}
	if !enabled {
		// 幂等：已经关了就当成功，不必让前端区分"本来就没开"。
		OK(w, map[string]interface{}{"mfa_enabled": false})
		return
	}

	authorized := false
	if password != "" && passwordHash != "" {
		if bcrypt.CompareHashAndPassword([]byte(passwordHash), []byte(password)) == nil {
			authorized = true
		}
	}
	// 密码不对时再试验证码（TOTP 或备用码）。
	if !authorized && code != "" {
		if secretEnc != nil && *secretEnc != "" {
			if secret, derr := decryptTOTPSecret(*secretEnc); derr == nil && validateTOTP(secret, code) {
				authorized = true
			}
		}
		if !authorized {
			if rest, hit := consumeRecoveryCode(unmarshalRecoveryHashes(recoveryRaw), code); hit {
				authorized = true
				// 消耗掉用过的备用码：不删就能重复使用。
				if payload, mErr := marshalRecoveryHashes(rest); mErr == nil {
					_, _ = db.GlobalDBManager.Exec(ctx,
						`UPDATE users SET totp_recovery_hashes = $2 WHERE id = $1`, userID, payload)
				}
			}
		}
	}

	if !authorized {
		db.AuditLog(ctx, userID, GetTenantID(r), "mfa.disable_denied", "/v1/auth/mfa/disable",
			"", r.RemoteAddr, nil)
		Unauthorized(w, "invalid verification code or password")
		return
	}

	// 关闭时把密钥与备用码一并清空：留着只增加泄露面，且用户下次 setup
	// 会生成全新的（不会复用旧密钥）。
	if _, err := db.GlobalDBManager.Exec(ctx,
		`UPDATE users SET totp_enabled = false, totp_secret_enc = NULL,
		        totp_recovery_hashes = NULL
		   WHERE id = $1`, userID); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "mfa disable failed")
		return
	}
	db.AuditLog(ctx, userID, GetTenantID(r), "mfa.disabled", "/v1/auth/mfa/disable",
		"", r.RemoteAddr, nil)
	OK(w, map[string]interface{}{"mfa_enabled": false})
}

// MFAStatus GET /v1/auth/mfa/status —— 供前端「安全设置」页渲染。
//
// `pending_confirmation` 是关键字段：用户已 setup 但没确认时，前端必须提示
// "你还没完成配置"，否则他以为自己开了双因子，实际并没有。
func (h *AuthHandler) MFAStatus(w http.ResponseWriter, r *http.Request) {
	userID := GetUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	var enabled, hasSecret bool
	var recoveryCount int
	err := db.GlobalDBManager.QueryRow(r.Context(), `
		SELECT totp_enabled,
		       (totp_secret_enc IS NOT NULL AND totp_secret_enc <> ''),
		       COALESCE(jsonb_array_length(totp_recovery_hashes), 0)
		  FROM users WHERE id = $1`, userID).Scan(&enabled, &hasSecret, &recoveryCount)
	if err != nil {
		if isNoRows(err) {
			Unauthorized(w, ErrAuthRequired)
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "mfa status failed")
		return
	}
	OK(w, map[string]interface{}{
		"enabled":              enabled,
		"pending_confirmation": hasSecret && !enabled,
		"recovery_codes_left":  recoveryCount,
	})
}
