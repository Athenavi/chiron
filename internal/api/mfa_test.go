package api

import (
	"strings"
	"testing"
	"time"

	"github.com/pquerna/otp"
	"github.com/pquerna/otp/totp"
)

// ── MFA 核心逻辑 ──
//
// 这组用例只覆盖**纯函数**部分（生成、哈希、消耗备用码、TOTP 校验）。
// 不连库：需要验证的是"密钥与备用码的处理是否正确"，不是 SQL 能不能跑。
//
// 三个最容易写错、而写错后最难发现的点：
//  1. 备用码**一次一用**——不删就能反复使用，等价于永久密码；
//  2. 备用码比对要**归一化**（大小写、分隔符），否则用户抄对了也登不进去；
//  3. TOTP 校验必须容忍时钟轻微漂移（Skew），否则用户总在"差几秒"时失败。

func TestGenerateMFAEnrollmentProducesUsableSecret(t *testing.T) {
	enr, err := generateMFAEnrollment("alice@example.com")
	if err != nil {
		t.Fatalf("生成失败: %v", err)
	}
	if enr.Secret == "" || enr.URL == "" {
		t.Fatal("secret / url 不能为空，否则前端无法生成二维码")
	}
	if len(enr.RecoveryCodes) != mfaRecoveryCodeCount {
		t.Fatalf("备用码数量 = %d，期望 %d", len(enr.RecoveryCodes), mfaRecoveryCodeCount)
	}

	// 生成的 secret 必须能真正产出可用验证码——否则用户扫码后永远登不进来。
	code, err := totp.GenerateCodeCustom(enr.Secret, time.Now().UTC(), totp.ValidateOpts{
		Period: 30, Skew: mfaTotpSkew, Digits: otp.DigitsSix, Algorithm: otp.AlgorithmSHA1,
	})
	if err != nil {
		t.Fatalf("按 secret 生成验证码失败: %v", err)
	}
	if !validateTOTP(enr.Secret, code) {
		t.Fatal("自己生成的验证码通不过校验：secret 与校验参数不一致")
	}
}

// 备用码重复率检查：同一批里不应出现重复项（概率极低，但用固定种子的
// 随机源时曾出现过"复用同一 buffer"的经典 bug）。
func TestGenerateRecoveryCodesAreUnique(t *testing.T) {
	codes, err := generateRecoveryCodes(50)
	if err != nil {
		t.Fatalf("生成失败: %v", err)
	}
	seen := map[string]bool{}
	for _, c := range codes {
		if seen[c] {
			t.Fatalf("备用码重复: %s", c)
		}
		seen[c] = true
	}
}

// 备用码**一次一用**：命中后必须从列表里移除，否则同一串能无限登录。
func TestConsumeRecoveryCodeIsSingleUse(t *testing.T) {
	plain := []string{"aaaa-bbbb-cccc-dddd", "1111-2222-3333-4444"}
	hashes := hashRecoveryCodes(plain)

	remaining, ok := consumeRecoveryCode(hashes, plain[0])
	if !ok {
		t.Fatal("第一个备用码应当命中")
	}
	if len(remaining) != 1 {
		t.Fatalf("命中后应剩 1 个，得到 %d", len(remaining))
	}

	// 同一串再用一次必须失败。
	if _, again := consumeRecoveryCode(remaining, plain[0]); again {
		t.Fatal("备用码被重复使用：它必须是一次性的")
	}
	// 另一个码仍然可用。
	if _, ok := consumeRecoveryCode(remaining, plain[1]); !ok {
		t.Fatal("未使用的备用码不应失效")
	}
}

// 用户抄写时的格式差异（大写、少横线、多空格）不应导致登录失败——
// 备用码用在"手机丢了、用户很着急"的时刻，格式容错是可用性要求。
func TestConsumeRecoveryCodeNormalizesInput(t *testing.T) {
	plain := []string{"a1b2-c3d4-e5f6-a7b8"}
	hashes := hashRecoveryCodes(plain)

	variants := []string{
		"a1b2-c3d4-e5f6-a7b8",
		"A1B2-C3D4-E5F6-A7B8",
		"a1b2c3d4e5f6a7b8",
		"  a1b2 c3d4 e5f6 a7b8  ",
	}
	for _, v := range variants {
		if _, ok := consumeRecoveryCode(hashes, v); !ok {
			t.Fatalf("格式变体 %q 应当命中", v)
		}
	}
}

func TestConsumeRecoveryCodeRejectsWrongInput(t *testing.T) {
	hashes := hashRecoveryCodes([]string{"a1b2-c3d4-e5f6-a7b8"})

	for _, bad := range []string{"", "  ", "ffff-ffff-ffff-ffff", "a1b2-c3d4-e5f6-a7b9"} {
		if _, ok := consumeRecoveryCode(hashes, bad); ok {
			t.Fatalf("错误输入 %q 不应命中", bad)
		}
	}
}

// 落库的只有哈希：任何情况下都不该在存储里看到明文备用码。
func TestRecoveryCodesStoredAsHashesOnly(t *testing.T) {
	plain := []string{"a1b2-c3d4-e5f6-a7b8"}
	hashes := hashRecoveryCodes(plain)
	raw, err := marshalRecoveryHashes(hashes)
	if err != nil {
		t.Fatalf("序列化失败: %v", err)
	}
	if string(raw) == "" {
		t.Fatal("序列化结果为空")
	}
	for _, p := range plain {
		if strings.Contains(string(raw), normalizeRecoveryCode(p)) {
			t.Fatalf("落库内容包含明文备用码: %s", raw)
		}
		// 分隔符也应被去掉后不再出现
		if strings.Contains(string(raw), p) {
			t.Fatalf("落库内容包含原始备用码: %s", raw)
		}
	}
}

// 校验容忍 ±1 个时间片（30s）的时钟漂移，但不应容忍过期太久。
func TestValidateTOTPSkewTolerance(t *testing.T) {
	enr, err := generateMFAEnrollment("bob@example.com")
	if err != nil {
		t.Fatalf("生成失败: %v", err)
	}

	gen := func(at time.Time) string {
		code, err := totp.GenerateCodeCustom(enr.Secret, at, totp.ValidateOpts{
			Period: 30, Digits: otp.DigitsSix, Algorithm: otp.AlgorithmSHA1,
		})
		if err != nil {
			t.Fatalf("生成验证码失败: %v", err)
		}
		return code
	}

	now := time.Now().UTC()
	if !validateTOTP(enr.Secret, gen(now)) {
		t.Fatal("当前时间片的验证码应当通过")
	}
	if !validateTOTP(enr.Secret, gen(now.Add(-30*time.Second))) {
		t.Fatal("上一个时间片应当通过（±1 漂移容错）")
	}
	// 5 分钟前早已超出容错窗口。
	if validateTOTP(enr.Secret, gen(now.Add(-5*time.Minute))) {
		t.Fatal("5 分钟前的验证码不应通过：容错窗口过大等于削弱防护")
	}
	if validateTOTP(enr.Secret, "") || validateTOTP("", "123456") {
		t.Fatal("空 secret 或空验证码不应通过")
	}
}

// 加密落库后必须能解回原文；密钥不可用时必须报错而不是静默存明文。
func TestTOTPSecretEncryptRoundTrip(t *testing.T) {
	t.Setenv("ENT_OIDC_SECRET_KEY", "test-mfa-encryption-key-for-unit-test")
	const secret = "JBSWY3DPEHPK3PXP"

	enc, err := encryptTOTPSecret(secret)
	if err != nil {
		t.Fatalf("加密失败: %v", err)
	}
	if enc == secret {
		t.Fatal("密文与明文相同：根本没有加密")
	}
	if strings.Contains(enc, secret) {
		t.Fatal("密文里出现了明文 secret")
	}

	got, err := decryptTOTPSecret(enc)
	if err != nil {
		t.Fatalf("解密失败: %v", err)
	}
	if got != secret {
		t.Fatalf("解密结果 = %q，期望 %q", got, secret)
	}
}
