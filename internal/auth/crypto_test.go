package auth

import (
	"bytes"
	"crypto/sha256"
	"strings"
	"testing"
)

// 凭据加密密钥的取值顺序：专用密钥优先，缺失时回退 APP_SECRET 派生，二者皆无才返回 nil。
//
// 回归背景：历史上未配置 ENT_OIDC_SECRET_KEY 时直接返回 nil，
// 于是邮件/短信/SSO 的凭据保存一律 503（后台表现为「邮件 smtp password 无法保存」）。
func TestLoadOIDCEncryptionKey(t *testing.T) {
	const appSecret = "app-secret-0123456789abcdef0123456789"

	t.Run("两个都缺失时返回 nil", func(t *testing.T) {
		t.Setenv(EnvOIDCSecretKey, "")
		t.Setenv("APP_SECRET", "")
		if key := LoadOIDCEncryptionKey(); key != nil {
			t.Fatalf("key = %v, want nil", key)
		}
	})

	t.Run("仅有 APP_SECRET 时回退派生且可用", func(t *testing.T) {
		t.Setenv(EnvOIDCSecretKey, "")
		t.Setenv("APP_SECRET", appSecret)

		key := LoadOIDCEncryptionKey()
		if len(key) != 32 {
			t.Fatalf("key length = %d, want 32", len(key))
		}
		// 同一输入必须稳定（否则重启后旧密文解不开）
		if !bytes.Equal(key, LoadOIDCEncryptionKey()) {
			t.Fatal("derived key is not stable across calls")
		}
		// 必须带域分隔前缀：不能等于「APP_SECRET 原文的 SHA-256」
		raw := sha256.Sum256([]byte(appSecret))
		if bytes.Equal(key, raw[:]) {
			t.Fatal("fallback key must be domain-separated from the raw APP_SECRET hash")
		}
		// 真正能加解密（凭据保存路径依赖它）
		enc, err := EncryptAESGCM(key, "smtp-password")
		if err != nil {
			t.Fatalf("encrypt: %v", err)
		}
		plain, err := DecryptAESGCM(key, enc)
		if err != nil || plain != "smtp-password" {
			t.Fatalf("round trip = %q err=%v", plain, err)
		}
	})

	t.Run("专用密钥优先于 APP_SECRET", func(t *testing.T) {
		t.Setenv("APP_SECRET", appSecret)
		t.Setenv(EnvOIDCSecretKey, "dedicated-oidc-secret-key-value")
		dedicated := LoadOIDCEncryptionKey()

		t.Setenv(EnvOIDCSecretKey, "")
		fallback := LoadOIDCEncryptionKey()

		if bytes.Equal(dedicated, fallback) {
			t.Fatal("dedicated key must not equal the APP_SECRET-derived key")
		}
		// 两把密钥互不相通（密文与派生来源强绑定）
		enc, err := EncryptAESGCM(dedicated, "s3cret")
		if err != nil {
			t.Fatalf("encrypt: %v", err)
		}
		if _, err := DecryptAESGCM(fallback, enc); err == nil {
			t.Fatal("ciphertext must not decrypt under the fallback key")
		}
		if got, err := DecryptAESGCM(dedicated, enc); err != nil || got != "s3cret" {
			t.Fatalf("decrypt with dedicated key = %q err=%v", got, err)
		}
	})

	t.Run("专用密钥过短时回落 APP_SECRET 并可用", func(t *testing.T) {
		t.Setenv(EnvOIDCSecretKey, "short")
		t.Setenv("APP_SECRET", appSecret)
		if key := LoadOIDCEncryptionKey(); len(key) != 32 {
			t.Fatalf("key length = %d, want 32（应回退 APP_SECRET）", len(key))
		}
	})
}

func TestEncryptAESGCMRejectsShortKey(t *testing.T) {
	if _, err := EncryptAESGCM([]byte("too-short"), "x"); err == nil {
		t.Fatal("short key must be rejected")
	}
	if !strings.HasPrefix(EnvOIDCSecretKey, "ENT_") {
		t.Fatalf("unexpected env var name: %s", EnvOIDCSecretKey)
	}
}
