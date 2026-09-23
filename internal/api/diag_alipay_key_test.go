package api

import (
	"bytes"
	"context"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/pem"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/settings"
	"github.com/jackc/pgx/v5/pgxpool"
)

// 临时诊断：读出后台保存的支付宝私钥，推导其对应的**应用公钥**，
// 供用户与开放平台「应用公钥」逐字对比（验签出错 = 两者不配对）。
func TestDiagDeriveAlipayPublicKey(t *testing.T) {
	dsn := os.Getenv("CHIRON_TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Skip("CHIRON_TEST_POSTGRES_DSN 未设置")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer pool.Close()

	cfg := config.LoadAllowUnconfigured()
	store := settings.New(pool, cfg.AppSecret)
	m, err := store.LoadConfig(ctx, "payment")
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	if len(m) == 0 {
		t.Skip("库里没有 payment 配置")
	}

	appID, _ := m["alipay_app_id"].(string)
	gateway, _ := m["alipay_gateway"].(string)
	privPEM, _ := m["alipay_private_key"].(string)
	pubCfg, _ := m["alipay_public_key"].(string)

	t.Logf("AppID   = %q (len=%d)", appID, len(appID))
	t.Logf("Gateway = %q", gateway)

	// ── 私钥（与 billing.parseRSAPrivateKey 一致：兼容裸 base64 与 PEM 两种形态）──
	var priv *rsa.PrivateKey
	if privBlock, _ := pem.Decode([]byte(privPEM)); privBlock != nil {
		if k, err := x509.ParsePKCS8PrivateKey(privBlock.Bytes); err == nil {
			if rk, ok := k.(*rsa.PrivateKey); ok {
				priv = rk
			}
		} else if rk, err := x509.ParsePKCS1PrivateKey(privBlock.Bytes); err == nil {
			priv = rk
		}
		t.Logf("私钥形态: PEM(%q)", privBlock.Type)
	} else {
		der, err := base64.StdEncoding.DecodeString(strings.TrimSpace(privPEM))
		if err != nil {
			t.Fatalf("私钥既不是 PEM 也不是合法 base64: %v", err)
		}
		if k, err := x509.ParsePKCS8PrivateKey(der); err == nil {
			if rk, ok := k.(*rsa.PrivateKey); ok {
				priv = rk
			}
		} else if rk, err := x509.ParsePKCS1PrivateKey(der); err == nil {
			priv = rk
		}
		t.Logf("私钥形态: 裸 base64（无 PEM 头），DER %d 字节", len(der))
	}
	if priv == nil {
		t.Fatal("私钥无法解析为 RSA 私钥（PKCS#1/PKCS#8 均失败）")
	}
	t.Logf("私钥: RSA %d 位", priv.N.BitLen())

	derived, err := x509.MarshalPKIXPublicKey(&priv.PublicKey)
	if err != nil {
		t.Fatalf("推导公钥: %v", err)
	}
	derivedPEM := pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: derived})
	t.Logf("该私钥对应的**应用公钥**（应与开放平台「应用公钥」完全一致）:\n%s", string(derivedPEM))

	// ── 配置里的 alipay_public_key 是什么？──
	// 按签名规则它应当是「支付宝公钥」（用于验签回调）。
	// 如果它恰好等于上面推导出的公钥，说明它被填成了「应用公钥」。
	cfgPubDER := decodeKeyAny(pubCfg)
	switch {
	case cfgPubDER == nil:
		t.Logf("alipay_public_key 无法解析（前 60 字节: %q）", firstN(pubCfg, 60))
	case bytes.Equal(cfgPubDER, derived):
		t.Logf("⚠️ alipay_public_key 与私钥配对 —— 它填的是「应用公钥」，而这里应当填「支付宝公钥」")
	default:
		t.Logf("alipay_public_key 与私钥不配对（符合预期：它应是「支付宝公钥」）")
	}
}

// decodeKeyAny 以 PKIX / PKCS#1 / 裸 base64 三种形态尝试解析公钥，返回其 DER。
func decodeKeyAny(s string) []byte {
	if blk, _ := pem.Decode([]byte(s)); blk != nil {
		return blk.Bytes
	}
	if der, err := base64.StdEncoding.DecodeString(strings.TrimSpace(s)); err == nil {
		return der
	}
	return nil
}

func firstN(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}
