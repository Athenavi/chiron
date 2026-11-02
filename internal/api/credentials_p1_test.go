package api

import (
	"bytes"
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"io"
	"strings"
	"testing"
)

// ── P1 凭据层回归 ──
//
// 这一组盯的是"长期凭据"的两条路：
//
//  1. **API key 撤销**：`apiKeyAuthQuery` 是每个 API 请求都走的语句。它必须自己
//     带 revoked / expires 谓词，并且带回**签发时刻** —— 少了后者，`revokeUserSessions`
//     （改密 / 重置密码 / 管理员踢人）只对 JWT 生效，此前签发的长期 API key 依然能用，
//     而 API key 恰恰是最容易落在 CI 与第三方系统里的那类凭据。
//
//  2. **webhook secret 加密密钥**：从"截取 APP_SECRET 前 32 字节"改成域分隔 KDF，
//     并保留对历史密文的只读回退 —— 换派生方式不能让线上既有订阅的签名瞬间消失。

func TestAPIKeyAuthQueryCarriesIssuedAtAndRevocation(t *testing.T) {
	for _, want := range []string{
		"COALESCE(ak.revoked, false) = false",            // 吊销
		"ak.expires_at IS NULL OR ak.expires_at > NOW()", // 过期
		"AS issued_at", // 签发时刻（会话纪元比对用）
	} {
		if !strings.Contains(apiKeyAuthQuery, want) {
			t.Errorf("apiKeyAuthQuery 缺少 %q —— 长期凭据的撤销/到期不再生效：\n%s", want, apiKeyAuthQuery)
		}
	}

	// 第 5 列必须替换成 Unix 秒；直接扫 `created_at` 会在非 UTC 会话时区上整体偏移
	// 一个时区（`timestamp without time zone` + `now()` 的隐式转换）。
	if !strings.Contains(apiKeyAuthQuery, "EXTRACT(EPOCH FROM") {
		t.Errorf("apiKeyAuthQuery 的签发时刻未按会话时区还原成 epoch，时区偏移会让旧 key 逃过撤销：\n%s",
			apiKeyAuthQuery)
	}
	// 历史行可能是 NULL：必须兜成 0（1970），否则一次升级把全部旧 key 打死。
	if !strings.Contains(apiKeyAuthQuery, "COALESCE(") {
		t.Errorf("apiKeyAuthQuery 未对 NULL 签发时刻兜底：\n%s", apiKeyAuthQuery)
	}
}

func TestDeriveWebhookKeyIsDomainSeparatedKDF(t *testing.T) {
	const appSecret = "0123456789abcdef0123456789abcdef" // 32 字符
	t.Setenv("APP_SECRET", appSecret)

	key := deriveWebhookKey()
	if len(key) != 32 {
		t.Fatalf("派生密钥长度 = %d，期望 32（AES-256）", len(key))
	}
	// 核心回归：派生结果**不能**是 APP_SECRET 的前 32 字节。
	// 那等于把部署级根密钥原文当 AES key 用 —— 数据库备份泄露即可解密，
	// 而根密钥还在继续派发 JWT。
	if string(key) == appSecret {
		t.Fatal("webhook 加密密钥等于 APP_SECRET 原文（或截断），根密钥泄露即密文可解")
	}
	if strings.HasPrefix(appSecret, string(key)[:8]) {
		t.Fatal("webhook 加密密钥与 APP_SECRET 前缀重合")
	}

	// 稳定性：同一次部署内必须可复现（否则重启后既有密文解不开）。
	if again := deriveWebhookKey(); string(again) != string(key) {
		t.Fatal("同一 APP_SECRET 两次派生结果不同")
	}

	// 域分隔：换标签必然换密钥（否则一个用途的密钥泄露等于全部用途泄露）。
	other := sha256Sum("chiron-jwt:" + appSecret)
	if string(key) == other {
		t.Fatal("webhook 密钥与 JWT 派生域重合")
	}
}

func TestDeriveWebhookKeyFailsClosedWithoutAppSecret(t *testing.T) {
	// 强 APP_SECRET 是启动硬性要求（config.Load）。缺失时**不能**退回到一把
	// 硬编码派生密钥 —— 那等于所有人共用同一把可公开计算的密钥，密文形同明文。
	t.Setenv("APP_SECRET", "")
	if key := deriveWebhookKey(); key != nil {
		t.Fatalf("APP_SECRET 缺失时应返回 nil（fail-closed），实际 %d 字节", len(key))
	}
}

func TestDecryptWebhookSecretCompatReadsLegacyCiphertext(t *testing.T) {
	const appSecret = "0123456789abcdef0123456789abcdef"
	t.Setenv("APP_SECRET", appSecret)

	// 用**旧实现**的派生方式（APP_SECRET[:32]）加密一段密文，模拟线上已有数据。
	legacyKey := appSecret[:32]
	cipherText, err := encryptWebhookSecret("s3cret-value", legacyKey)
	if err != nil {
		t.Fatalf("旧格式加密失败: %v", err)
	}

	// 新代码必须仍能解开（否则升级瞬间所有 webhook 的签名头消失）。
	plain, err := decryptWebhookSecretCompat(cipherText, deriveWebhookKey())
	if err != nil {
		t.Fatalf("历史密文解不开 —— 升级会让既有订阅失去签名: %v", err)
	}
	if plain != "s3cret-value" {
		t.Fatalf("解开的值 = %q，期望 s3cret-value", plain)
	}
}

func TestWebhookSecretKDFRoundTrip(t *testing.T) {
	t.Setenv("APP_SECRET", "0123456789abcdef0123456789abcdef")

	key := deriveWebhookKey()
	sealed, err := encryptWebhookSecret("rotated-secret", string(key))
	if err != nil {
		t.Fatalf("加密失败: %v", err)
	}
	plain, err := decryptWebhookSecretCompat(sealed, key)
	if err != nil {
		t.Fatalf("新格式解密失败: %v", err)
	}
	if plain != "rotated-secret" {
		t.Fatalf("往返后 = %q", plain)
	}
}

func TestDecryptWebhookSecretCompatEmptySecretIsNotAnError(t *testing.T) {
	// 不配 secret 是**正常配置**（订阅方不校验签名）。若被当成解密失败，
	// 每条事件都会打一条 Error 日志 —— 一个租户就能刷爆日志，把真正的失败淹掉。
	plain, err := decryptWebhookSecretCompat("", deriveWebhookKey())
	if err != nil {
		t.Fatalf("空 secret 不应报错: %v", err)
	}
	if plain != "" {
		t.Fatalf("空 secret 应解出空串，实际 %q", plain)
	}
}

func TestDecryptWebhookSecretCompatReportsGarbage(t *testing.T) {
	t.Setenv("APP_SECRET", "0123456789abcdef0123456789abcdef")

	// 密文损坏/换过 APP_SECRET 时必须**报错**，而不是静默返回空串让签名头凭空消失。
	if _, err := decryptWebhookSecretCompat("bm90LWEtY2lwaGVydGV4dA==", deriveWebhookKey()); err == nil {
		t.Fatal("无法解密的密文应返回错误")
	}
}

// ── 测试内小工具 ──

// legacyKeysPreUpgrade 独立重算**升级前**的派生结果。
//
// 照抄当时的实现（而不是调用 legacyWebhookKeys）—— 用被测函数构造期望值等于自己
// 证明自己：回退表里把常量写错一个字节，测试照样会绿，而线上旧密文全解不开。
func legacyKeysPreUpgrade(secret string) [][]byte {
	const length = 32
	if secret == "" {
		mac := hmac.New(sha256.New, []byte("chiron-webhook-encryption-fallback"))
		mac.Write([]byte("chiron-webhook-key-derivation"))
		return [][]byte{mac.Sum(nil)[:length]}
	}
	if len(secret) >= length {
		return [][]byte{[]byte(secret)[:length]}
	}
	mac := hmac.New(sha256.New, []byte("chiron-webhook-encryption"))
	mac.Write([]byte(secret))
	return [][]byte{mac.Sum(nil)}
}

// TestLegacyWebhookKeysMatchPreUpgradeDerivation 钉住回退表与旧实现**逐字节**一致。
//
// 旧实现有三条分支：空 APP_SECRET（固定派生）、≥32 字符（截断）、短密钥（HMAC）。
// 既存用例只覆盖了"截断"这一条；另外两条若写错，表现是"部分租户的 webhook 在升级后
// 失去签名"—— 只在线上才看得见，日志里只有一个无法解密。
func TestLegacyWebhookKeysMatchPreUpgradeDerivation(t *testing.T) {
	for name, secret := range map[string]string{
		"empty":                  "",
		"exactly-32":             "0123456789abcdef0123456789abcdef",
		"longer-than-32":         "0123456789abcdef0123456789abcdef-plus-more",
		"shorter-than-32 (HMAC)": "short-secret",
	} {
		t.Run(name, func(t *testing.T) {
			t.Setenv("APP_SECRET", secret)
			got := legacyWebhookKeys()

			for _, want := range legacyKeysPreUpgrade(secret) {
				found := false
				for _, g := range got {
					if bytes.Equal(g, want) {
						found = true
						break
					}
				}
				if !found {
					t.Fatalf("APP_SECRET=%q 时缺少历史候选 —— 用旧密钥加密的密文将解不开", secret)
				}
			}

			// 端到端确认：旧密钥加密的密文，新代码必须解得开。
			legacy := legacyKeysPreUpgrade(secret)[0]
			sealed, err := encryptWebhookSecret("historical-value", string(legacy))
			if err != nil {
				t.Fatalf("旧格式加密失败: %v", err)
			}
			plain, err := decryptWebhookSecretCompat(sealed, deriveWebhookKey())
			if err != nil {
				t.Fatalf("历史密文解不开（升级会让既有订阅失去签名）: %v", err)
			}
			if plain != "historical-value" {
				t.Fatalf("解出 %q，期望 historical-value", plain)
			}
		})
	}
}

// TestWebhookSecretDecryptionFailureIsLoud 防止退回 `decrypted, _ :=` 的静默失败。
//
// 此前 dispatcher 丢弃错误：解不开时 decrypted == ""，于是签名头凭空消失，
// 接收方以为这是条未签名消息，而日志里一个字都没有。现在必须能观测到失败。
func TestWebhookSecretDecryptionFailureIsLoud(t *testing.T) {
	t.Setenv("APP_SECRET", "0123456789abcdef0123456789abcdef")

	other := make([]byte, 32)
	if _, err := io.ReadFull(rand.Reader, other); err != nil {
		t.Fatalf("生成无关密钥失败: %v", err)
	}
	sealed, err := encryptWebhookSecret("x", string(other))
	if err != nil {
		t.Fatalf("加密失败: %v", err)
	}
	if _, err := decryptWebhookSecretCompat(sealed, deriveWebhookKey()); err == nil {
		t.Fatal("用无关密钥加密的密文不应解密成功 —— 失败被静默吞掉了")
	}
}

func sha256Sum(s string) string {
	sum := sha256.Sum256([]byte(s))
	return string(sum[:])
}

// gcmSealWithKey 用给定 key 加密，供构造"别的密钥"密文用。
//
// 保留在这里是为了将来验证"解密失败"分支时不必再依赖 encryptWebhookSecret 的实现细节。
func gcmSealWithKey(t *testing.T, key []byte, plaintext string) string {
	t.Helper()
	block, err := aes.NewCipher(key)
	if err != nil {
		t.Fatalf("aes.NewCipher: %v", err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		t.Fatalf("cipher.NewGCM: %v", err)
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err := io.ReadFull(rand.Reader, nonce); err != nil {
		t.Fatalf("nonce: %v", err)
	}
	return base64.StdEncoding.EncodeToString(gcm.Seal(nonce, nonce, []byte(plaintext), nil))
}
