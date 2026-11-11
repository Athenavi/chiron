package api

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/pquerna/otp"
	"github.com/pquerna/otp/totp"
	"golang.org/x/crypto/bcrypt"

	"github.com/athenavi/chiron/config"
)

// ── MFA 端点（真库）──
//
// 端点逻辑的正确性只有跑一次才知道：SQL 写法、列类型、jsonb 数组长度、
// 以及"启用前必须先确认"这个两阶段约束，静态读代码都证明不了。
//
// 取值口径与既有 *_live_test.go 一致：CHIRON_TEST_POSTGRES_DSN 未设即 skip。

// mfaTestHandler 造一个可用的 AuthHandler。cfg 只需 JWTSecret（签发会话令牌）
// 与 JWTExpiration；MFA 相关状态全部走 DB。
func mfaTestHandler(t *testing.T) *AuthHandler {
	t.Helper()
	// 加密密钥：TOTP secret 落库前要 AES-GCM 加密；没有密钥时 setup 会
	// 明确报错而不是存明文（见 mfa.go 的 encryptTOTPSecret）。
	t.Setenv("ENT_OIDC_SECRET_KEY", "mfa-endpoint-test-key-0123456789")
	return NewAuthHandler(&config.Config{
		JWTSecret:     strings.Repeat("k", 48),
		JWTExpiration: time.Hour,
	})
}

// mfaCall 以 userID 身份构造请求并执行（模拟 authMW 注入的上下文）。
func mfaCall(t *testing.T, h *AuthHandler, userID, target string, body any,
	fn func(*AuthHandler, http.ResponseWriter, *http.Request)) *httptest.ResponseRecorder {
	t.Helper()
	var payload []byte
	if body != nil {
		var err error
		payload, err = json.Marshal(body)
		if err != nil {
			t.Fatalf("marshal body: %v", err)
		}
	}
	req := httptest.NewRequest(http.MethodPost, target, bytes.NewReader(payload))
	req.Header.Set("Content-Type", "application/json")
	// GetUserID 读的是 TenantMiddleware 写入的 ctx key，不是 claims。
	ctx := context.WithValue(req.Context(), CtxKeyUserID, userID)
	ctx = context.WithValue(ctx, CtxKeyTenantID, int16(1))
	req = req.WithContext(ctx)
	rec := httptest.NewRecorder()
	fn(h, rec, req)
	return rec
}

// decodeMFA 解析 OK 响应的 data 段。
func decodeMFA(t *testing.T, rec *httptest.ResponseRecorder) map[string]interface{} {
	t.Helper()
	var resp struct {
		Success bool                   `json:"success"`
		Data    map[string]interface{} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("解析响应失败: %v body=%s", err, rec.Body.String())
	}
	if !resp.Success {
		t.Fatalf("响应不成功: %s", rec.Body.String())
	}
	return resp.Data
}

// 完整的两阶段启用流程：setup 只落密钥不启用，enable 用一次正确码才置 true。
//
// 这三个断言各自对应一种真实故障：
//   - setup 后 enabled 必须为 false：否则用户扫码失败就锁死自己；
//   - pending_confirmation 必须为 true：前端据此提示"还没配完"；
//   - enable 之后 enabled 为 true 且 pending 归零。
func TestLiveMFAEnrollmentTwoPhase(t *testing.T) {
	pool := withP12Pool(t)
	_, userID := liveSmokeFixture(t, pool)
	h := mfaTestHandler(t)

	// ① setup
	rec := mfaCall(t, h, userID, "/v1/auth/mfa/setup", nil,
		func(h *AuthHandler, w http.ResponseWriter, r *http.Request) { h.MFASetup(w, r) })
	if rec.Code != http.StatusOK {
		t.Fatalf("setup 失败: status=%d body=%s", rec.Code, rec.Body.String())
	}
	setup := decodeMFA(t, rec)
	secret, _ := setup["secret"].(string)
	if secret == "" || setup["otpauth_url"] == "" {
		t.Fatalf("setup 未返回 secret / otpauth_url: %v", setup)
	}
	codes, _ := setup["recovery_codes"].([]interface{})
	if len(codes) != mfaRecoveryCodeCount {
		t.Fatalf("备用码数量 = %d，期望 %d", len(codes), mfaRecoveryCodeCount)
	}

	// ② setup 之后必须仍是"未启用、待确认"
	var enabled bool
	var secretNull bool
	if err := pool.QueryRow(context.Background(),
		`SELECT totp_enabled, (totp_secret_enc IS NULL) FROM users WHERE id = $1`,
		userID).Scan(&enabled, &secretNull); err != nil {
		t.Fatalf("读回状态失败: %v", err)
	}
	if enabled {
		t.Fatal("setup 就把 totp_enabled 置 true：用户未确认即被要求输入验证码，会锁死自己")
	}
	if secretNull {
		t.Fatal("setup 没有落库密钥")
	}

	statusRec := mfaCall(t, h, userID, "/v1/auth/mfa/status", nil,
		func(h *AuthHandler, w http.ResponseWriter, r *http.Request) { h.MFAStatus(w, r) })
	st := decodeMFA(t, statusRec)
	if st["pending_confirmation"] != true {
		t.Fatalf("setup 后 pending_confirmation 应为 true，得到 %v", st["pending_confirmation"])
	}

	// ③ 用错误码 enable 必须被拒
	badRec := mfaCall(t, h, userID, "/v1/auth/mfa/enable", map[string]string{"code": "000000"},
		func(h *AuthHandler, w http.ResponseWriter, r *http.Request) { h.MFAEnable(w, r) })
	if badRec.Code == http.StatusOK {
		t.Fatal("错误验证码竟然启用成功了")
	}

	// ④ 用正确码 enable
	code, err := totp.GenerateCodeCustom(secret, time.Now().UTC(), totp.ValidateOpts{
		Period: 30, Digits: otp.DigitsSix, Algorithm: otp.AlgorithmSHA1,
	})
	if err != nil {
		t.Fatalf("生成验证码失败: %v", err)
	}
	okRec := mfaCall(t, h, userID, "/v1/auth/mfa/enable", map[string]string{"code": code},
		func(h *AuthHandler, w http.ResponseWriter, r *http.Request) { h.MFAEnable(w, r) })
	if okRec.Code != http.StatusOK {
		t.Fatalf("enable 失败: status=%d body=%s", okRec.Code, okRec.Body.String())
	}

	if err := pool.QueryRow(context.Background(),
		`SELECT totp_enabled FROM users WHERE id = $1`, userID).Scan(&enabled); err != nil {
		t.Fatalf("读回状态失败: %v", err)
	}
	if !enabled {
		t.Fatal("enable 成功后 totp_enabled 仍为 false")
	}

	// ⑤ 已启用后再 enable 应 409（不是静默成功，避免前端状态错乱）
	dupRec := mfaCall(t, h, userID, "/v1/auth/mfa/enable", map[string]string{"code": code},
		func(h *AuthHandler, w http.ResponseWriter, r *http.Request) { h.MFAEnable(w, r) })
	if dupRec.Code != http.StatusConflict {
		t.Fatalf("重复 enable 应返回 409，得到 %d", dupRec.Code)
	}
}

// 关闭 MFA 必须验证当前凭证：光有会话就能关掉双因子，等于攻击者拿到一次
// 会话即可永久移除这层防护。
func TestLiveMFADisableRequiresCredential(t *testing.T) {
	pool := withP12Pool(t)
	_, userID := liveSmokeFixture(t, pool)
	h := mfaTestHandler(t)

	setupRec := mfaCall(t, h, userID, "/v1/auth/mfa/setup", nil,
		func(h *AuthHandler, w http.ResponseWriter, r *http.Request) { h.MFASetup(w, r) })
	secret, _ := decodeMFA(t, setupRec)["secret"].(string)

	code, err := totp.GenerateCodeCustom(secret, time.Now().UTC(), totp.ValidateOpts{
		Period: 30, Digits: otp.DigitsSix, Algorithm: otp.AlgorithmSHA1,
	})
	if err != nil {
		t.Fatalf("生成验证码失败: %v", err)
	}
	if rec := mfaCall(t, h, userID, "/v1/auth/mfa/enable", map[string]string{"code": code},
		func(h *AuthHandler, w http.ResponseWriter, r *http.Request) { h.MFAEnable(w, r) }); rec.Code != http.StatusOK {
		t.Fatalf("enable 失败: %s", rec.Body.String())
	}

	// 错误凭证必须被拒。
	denyRec := mfaCall(t, h, userID, "/v1/auth/mfa/disable", map[string]string{"code": "000000"},
		func(h *AuthHandler, w http.ResponseWriter, r *http.Request) { h.MFADisable(w, r) })
	if denyRec.Code == http.StatusOK {
		t.Fatal("错误验证码竟然关闭成功：会话本身不该足以关闭双因子")
	}
	var enabled bool
	if err := pool.QueryRow(context.Background(),
		`SELECT totp_enabled FROM users WHERE id = $1`, userID).Scan(&enabled); err != nil {
		t.Fatalf("读回状态失败: %v", err)
	}
	if !enabled {
		t.Fatal("拒绝之后 MFA 却已被关闭")
	}

	// 正确验证码可以关闭，且密钥与备用码一并清空。
	okRec := mfaCall(t, h, userID, "/v1/auth/mfa/disable", map[string]string{"code": code},
		func(h *AuthHandler, w http.ResponseWriter, r *http.Request) { h.MFADisable(w, r) })
	if okRec.Code != http.StatusOK {
		t.Fatalf("disable 失败: status=%d body=%s", okRec.Code, okRec.Body.String())
	}
	var secretLeft, recoveryLeft bool
	if err := pool.QueryRow(context.Background(),
		`SELECT totp_enabled, (totp_secret_enc IS NOT NULL), (totp_recovery_hashes IS NOT NULL)
		   FROM users WHERE id = $1`, userID).Scan(&enabled, &secretLeft, &recoveryLeft); err != nil {
		t.Fatalf("读回状态失败: %v", err)
	}
	if enabled || secretLeft || recoveryLeft {
		t.Fatalf("关闭后应清空：enabled=%v secret=%v recovery=%v", enabled, secretLeft, recoveryLeft)
	}

	// 幂等：已关闭再关一次仍返回成功（前端无需区分"本来就没开"）。
	if rec := mfaCall(t, h, userID, "/v1/auth/mfa/disable", map[string]string{"code": code},
		func(h *AuthHandler, w http.ResponseWriter, r *http.Request) { h.MFADisable(w, r) }); rec.Code != http.StatusOK {
		t.Fatalf("重复 disable 应幂等成功，得到 %d body=%s", rec.Code, rec.Body.String())
	}
}

// 用密码也能关闭（"手机丢了"的场景）——否则用户只能去找管理员。
func TestLiveMFADisableWithPassword(t *testing.T) {
	pool := withP12Pool(t)
	_, userID := liveSmokeFixture(t, pool)
	h := mfaTestHandler(t)

	// 给这个用户设一个已知密码。
	hash, err := bcrypt.GenerateFromPassword([]byte("correct-horse-battery-staple"), bcrypt.MinCost)
	if err != nil {
		t.Fatalf("生成密码哈希失败: %v", err)
	}
	if _, err := pool.Exec(context.Background(),
		`UPDATE users SET password_hash = $2 WHERE id = $1`, userID, hash); err != nil {
		t.Fatalf("写密码失败: %v", err)
	}

	setupRec := mfaCall(t, h, userID, "/v1/auth/mfa/setup", nil,
		func(h *AuthHandler, w http.ResponseWriter, r *http.Request) { h.MFASetup(w, r) })
	secret, _ := decodeMFA(t, setupRec)["secret"].(string)
	code, err := totp.GenerateCodeCustom(secret, time.Now().UTC(), totp.ValidateOpts{
		Period: 30, Digits: otp.DigitsSix, Algorithm: otp.AlgorithmSHA1,
	})
	if err != nil {
		t.Fatalf("生成验证码失败: %v", err)
	}
	if rec := mfaCall(t, h, userID, "/v1/auth/mfa/enable", map[string]string{"code": code},
		func(h *AuthHandler, w http.ResponseWriter, r *http.Request) { h.MFAEnable(w, r) }); rec.Code != http.StatusOK {
		t.Fatalf("enable 失败: %s", rec.Body.String())
	}

	// 错误密码必须被拒。
	if rec := mfaCall(t, h, userID, "/v1/auth/mfa/disable",
		map[string]string{"password": "wrong-password"},
		func(h *AuthHandler, w http.ResponseWriter, r *http.Request) { h.MFADisable(w, r) }); rec.Code == http.StatusOK {
		t.Fatal("错误密码竟然关闭成功")
	}
	// 正确密码可以关闭。
	rec := mfaCall(t, h, userID, "/v1/auth/mfa/disable",
		map[string]string{"password": "correct-horse-battery-staple"},
		func(h *AuthHandler, w http.ResponseWriter, r *http.Request) { h.MFADisable(w, r) })
	if rec.Code != http.StatusOK {
		t.Fatalf("正确密码应能关闭：status=%d body=%s", rec.Code, rec.Body.String())
	}
	var enabled bool
	if err := pool.QueryRow(context.Background(),
		`SELECT totp_enabled FROM users WHERE id = $1`, userID).Scan(&enabled); err != nil {
		t.Fatalf("读回状态失败: %v", err)
	}
	if enabled {
		t.Fatal("密码正确但 MFA 仍为启用")
	}
}

// 备用码可以关闭 MFA，且用掉的那个会被消耗（不能重复使用）。
func TestLiveMFADisableWithRecoveryCodeConsumesIt(t *testing.T) {
	pool := withP12Pool(t)
	_, userID := liveSmokeFixture(t, pool)
	h := mfaTestHandler(t)

	setupRec := mfaCall(t, h, userID, "/v1/auth/mfa/setup", nil,
		func(h *AuthHandler, w http.ResponseWriter, r *http.Request) { h.MFASetup(w, r) })
	data := decodeMFA(t, setupRec)
	secret, _ := data["secret"].(string)
	rawCodes, _ := data["recovery_codes"].([]interface{})
	code, err := totp.GenerateCodeCustom(secret, time.Now().UTC(), totp.ValidateOpts{
		Period: 30, Digits: otp.DigitsSix, Algorithm: otp.AlgorithmSHA1,
	})
	if err != nil {
		t.Fatalf("生成验证码失败: %v", err)
	}
	if rec := mfaCall(t, h, userID, "/v1/auth/mfa/enable", map[string]string{"code": code},
		func(h *AuthHandler, w http.ResponseWriter, r *http.Request) { h.MFAEnable(w, r) }); rec.Code != http.StatusOK {
		t.Fatalf("enable 失败: %s", rec.Body.String())
	}

	recovery, _ := rawCodes[0].(string)
	if rec := mfaCall(t, h, userID, "/v1/auth/mfa/disable", map[string]string{"code": recovery},
		func(h *AuthHandler, w http.ResponseWriter, r *http.Request) { h.MFADisable(w, r) }); rec.Code != http.StatusOK {
		t.Fatalf("备用码应能关闭 MFA：status=%d body=%s", rec.Code, rec.Body.String())
	}
	// disable 会清空全部备用码，所以这里断言"列表已消失"而非"少了一个"。
	var recoveryLeft bool
	if err := pool.QueryRow(context.Background(),
		`SELECT totp_recovery_hashes IS NOT NULL FROM users WHERE id = $1`,
		userID).Scan(&recoveryLeft); err != nil {
		t.Fatalf("读回状态失败: %v", err)
	}
	if recoveryLeft {
		t.Fatal("关闭 MFA 后备用码应被清空")
	}
}

// 未 setup 就 enable 必须给出可操作的错误，而不是 panic 或 500。
func TestLiveMFAEnableWithoutSetup(t *testing.T) {
	pool := withP12Pool(t)
	_, userID := liveSmokeFixture(t, pool)
	h := mfaTestHandler(t)

	rec := mfaCall(t, h, userID, "/v1/auth/mfa/enable", map[string]string{"code": "123456"},
		func(h *AuthHandler, w http.ResponseWriter, r *http.Request) { h.MFAEnable(w, r) })
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("未 setup 就 enable 应返回 400，得到 %d body=%s", rec.Code, rec.Body.String())
	}
}

// 未登录（ctx 里没有 userID）一律 401，不能靠请求体指定身份。
func TestLiveMFAEndpointsRequireIdentity(t *testing.T) {
	withP12Pool(t)
	h := mfaTestHandler(t)

	for name, fn := range map[string]func(*AuthHandler, http.ResponseWriter, *http.Request){
		"setup":   func(h *AuthHandler, w http.ResponseWriter, r *http.Request) { h.MFASetup(w, r) },
		"enable":  func(h *AuthHandler, w http.ResponseWriter, r *http.Request) { h.MFAEnable(w, r) },
		"disable": func(h *AuthHandler, w http.ResponseWriter, r *http.Request) { h.MFADisable(w, r) },
		"status":  func(h *AuthHandler, w http.ResponseWriter, r *http.Request) { h.MFAStatus(w, r) },
	} {
		t.Run(name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodPost, "/v1/auth/mfa/"+name, nil)
			rec := httptest.NewRecorder()
			fn(h, rec, req)
			if rec.Code != http.StatusUnauthorized {
				t.Fatalf("%s 未登录应 401，得到 %d", name, rec.Code)
			}
		})
	}
}
