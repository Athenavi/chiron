package billing

import (
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/settings"
	"github.com/jackc/pgx/v5/pgxpool"
)

// 临时诊断：用真实沙箱凭据，把「签名规则」与「密钥配对」两个可能性二分定位。
// 规则 A = 当前实现（sign_type 参与签名）；规则 B = 改动前（排除 sign_type）。
func TestDiagAlipaySignatureRuleLive(t *testing.T) {
	if os.Getenv("DIAG_ALIPAY_REAL") == "" {
		t.Skip("DIAG_ALIPAY_REAL 未设置")
	}
	dsn := os.Getenv("CHIRON_TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Skip("CHIRON_TEST_POSTGRES_DSN 未设置")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()

	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer pool.Close()

	store := settings.New(pool, config.LoadAllowUnconfigured().AppSecret)
	m, err := store.LoadConfig(ctx, "payment")
	if err != nil {
		t.Fatalf("读配置: %v", err)
	}
	appID, _ := m["alipay_app_id"].(string)
	gateway, _ := m["alipay_gateway"].(string)
	privPEM, _ := m["alipay_private_key"].(string)
	pubKey, _ := m["alipay_public_key"].(string)
	t.Logf("AppID=%s  gateway=%s", appID, gateway)
	if appID == "" || privPEM == "" {
		t.Skip("配置缺失，跳过")
	}

	ts := time.Now().Format("150405")

	// ── 规则 A：当前实现（sign_type 参与签名）──
	client, err := NewAlipayClient(appID, privPEM, pubKey, gateway, "")
	if err != nil {
		t.Fatalf("构造客户端: %v", err)
	}

	// A-1：纯 ASCII subject（我的对照用例，此前成功）
	qrA, errA := client.Precreate(ctx, "diagA"+ts, 100, "diagnostic ascii subject")
	if errA != nil {
		t.Logf("A-1 ASCII subject            -> 失败: %v", errA)
	} else {
		t.Logf("A-1 ASCII subject            -> 成功 qr=%.60s", qrA)
	}

	// A-2：复刻服务端的 subject：含中文与空格（fmt.Sprintf("chiron 充值 %d credits", n)）
	subjectCN := fmt.Sprintf("chiron 充值 %d credits", 1000)
	qrA2, errA2 := client.Precreate(ctx, "diagC"+ts, 10000, subjectCN)
	if errA2 != nil {
		t.Logf("A-2 中文 subject %q -> 失败: %v", subjectCN, errA2)
	} else {
		t.Logf("A-2 中文 subject %q -> 成功 qr=%.60s", subjectCN, qrA2)
	}

	// A-3：中文但无空格，隔离"空格"与"中文"两个变量
	qrA3, errA3 := client.Precreate(ctx, "diagD"+ts, 100, "充值")
	if errA3 != nil {
		t.Logf("A-3 纯中文无空格 subject         -> 失败: %v", errA3)
	} else {
		t.Logf("A-3 纯中文无空格 subject         -> 成功 qr=%.60s", qrA3)
	}

	// ── 规则 B：改动前的行为（排除 sign_type）──
	qrB, errB := precreateExcludingSignType(ctx, appID, privPEM, gateway, "diagB"+ts)
	if errB != nil {
		t.Logf("规则B（排除 sign_type）-> 失败: %v", errB)
	} else {
		t.Logf("规则B（排除 sign_type）-> 成功 qr=%.60s", qrB)
	}

	switch {
	case errA == nil && errA2 != nil:
		t.Error("==> 结论：中文 subject 会导致验签失败（ASCII 通过、中文被拒）")
	case errA == nil && errA2 == nil && errA3 == nil:
		t.Log("==> 结论：各用例均通过 —— 服务端失败与环境差异有关")
	case errA != nil && errB == nil:
		t.Error("==> 结论：应回滚 sign_type 改动")
	default:
		t.Log("==> 结论：ASCII 用例也被拒 —— 问题在签名规则或密钥配对")
	}
}

// precreateExcludingSignType 复刻改动前的签名行为，用于对照。
func precreateExcludingSignType(ctx context.Context, appID, privPEM, gateway, outTradeNo string) (string, error) {
	priv, err := parseRSAPrivateKey(privPEM)
	if err != nil {
		return "", fmt.Errorf("解析私钥: %w", err)
	}
	biz, _ := json.Marshal(map[string]any{
		"out_trade_no": outTradeNo,
		"total_amount": "0.01",
		"subject":      "diagnostic",
	})
	params := map[string]string{
		"app_id":      appID,
		"method":      "alipay.trade.precreate",
		"format":      "JSON",
		"charset":     "utf-8",
		"sign_type":   "RSA2",
		"timestamp":   time.Now().Format("2006-01-02 15:04:05"),
		"version":     "1.0",
		"biz_content": string(biz),
	}

	// 旧规则：排除 sign 与 sign_type
	keys := make([]string, 0, len(params))
	for k, v := range params {
		if k == "sign" || k == "sign_type" || v == "" {
			continue
		}
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, k+"="+params[k])
	}
	digest := sha256.Sum256([]byte(strings.Join(parts, "&")))
	sig, err := rsa.SignPKCS1v15(rand.Reader, priv, crypto.SHA256, digest[:])
	if err != nil {
		return "", err
	}
	params["sign"] = base64.StdEncoding.EncodeToString(sig)

	form := url.Values{}
	for k, v := range params {
		form.Set(k, v)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, gateway, strings.NewReader(form.Encode()))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := (&http.Client{Timeout: 25 * time.Second}).Do(req)
	if err != nil {
		return "", fmt.Errorf("请求失败: %w", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))

	var r struct {
		Response struct {
			Code   string `json:"code"`
			Msg    string `json:"msg"`
			SubMsg string `json:"sub_msg"`
			QRCode string `json:"qr_code"`
		} `json:"alipay_trade_precreate_response"`
	}
	if err := json.Unmarshal([]byte(decodeBody(body)), &r); err != nil {
		return "", fmt.Errorf("响应解码失败: %w (body=%.200s)", err, string(body))
	}
	if r.Response.Code != "10000" {
		return "", fmt.Errorf("code=%s msg=%s sub_msg=%s", r.Response.Code, r.Response.Msg, r.Response.SubMsg)
	}
	return r.Response.QRCode, nil
}
