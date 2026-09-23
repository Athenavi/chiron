package billing

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"unicode/utf8"

	"golang.org/x/text/encoding/simplifiedchinese"
)

func testKeyPEM(t *testing.T) (priv, pub string) {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	privDER, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatalf("marshal private: %v", err)
	}
	pubDER, err := x509.MarshalPKIXPublicKey(&key.PublicKey)
	if err != nil {
		t.Fatalf("marshal public: %v", err)
	}
	return string(pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: privDER})),
		string(pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: pubDER}))
}

// 网关地址配错（请求落到非 API 端点）时，支付宝返回 HTML 而非 JSON。
// 错误信息必须给出**实际网关地址**与响应片段，否则只有 "invalid character '<'"
// 这种无从下手的信息 —— 线上排障正是卡在这里。
func TestAlipayPrecreateReportsGatewayOnNonJSONResponse(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html")
		_, _ = w.Write([]byte("<html><head><title>404</title></head><body>Not Found</body></html>"))
	}))
	defer srv.Close()

	priv, pub := testKeyPEM(t)
	client, err := NewAlipayClient("2021000000000000", priv, pub, srv.URL, "")
	if err != nil {
		t.Fatalf("构造客户端: %v", err)
	}

	_, err = client.Precreate(context.Background(), "pay_test_order", 100, "test")
	if err == nil {
		t.Fatal("非 JSON 响应应返回错误")
	}
	msg := err.Error()
	if !strings.Contains(msg, srv.URL) {
		t.Errorf("错误信息应包含实际网关地址，got: %s", msg)
	}
	if !strings.Contains(msg, "404") {
		t.Errorf("错误信息应包含响应片段，got: %s", msg)
	}
	if !strings.Contains(msg, "gateway.do") {
		t.Errorf("错误信息应给出正确网关形态的提示，got: %s", msg)
	}
}

// 网关返回 JSON 但业务失败（如未签约当面付）时，错误里要带上支付宝的 code/msg/sub_msg。
func TestAlipayPrecreateReportsBizError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"alipay_trade_precreate_response":{"code":"40004","msg":"Business Failed","sub_msg":"isv.pay-no-sign"}}`))
	}))
	defer srv.Close()

	priv, pub := testKeyPEM(t)
	client, err := NewAlipayClient("2021000000000000", priv, pub, srv.URL, "")
	if err != nil {
		t.Fatalf("构造客户端: %v", err)
	}

	_, err = client.Precreate(context.Background(), "pay_test_order", 100, "test")
	if err == nil {
		t.Fatal("业务失败应返回错误")
	}
	for _, want := range []string{"40004", "Business Failed", "isv.pay-no-sign"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("错误信息应包含 %q，got: %s", want, err.Error())
		}
	}
}

// 待签名串必须包含 sign_type（支付宝规则：除 sign 外的全部参数都参与签名）。
// 漏掉它会让本地算出的签名与支付宝的永远不一致，网关固定返回
// code=40002 Invalid Arguments —— 且无论私钥配置得多正确都无法通过。
func TestBuildSignContentIncludesSignType(t *testing.T) {
	params := map[string]string{
		"app_id":      "2021000000000000",
		"method":      "alipay.trade.precreate",
		"charset":     "utf-8",
		"sign_type":   "RSA2",
		"timestamp":   "2026-09-23 21:00:00",
		"version":     "1.0",
		"biz_content": `{"out_trade_no":"pay_x"}`,
		"sign":        "SHOULD_NOT_APPEAR",
		"empty_param": "",
	}
	got := buildSignContent(params)

	if !strings.Contains(got, "sign_type=RSA2") {
		t.Errorf("待签名串必须包含 sign_type，got: %s", got)
	}
	if strings.Contains(got, "SHOULD_NOT_APPEAR") || strings.Contains(got, "sign=") {
		t.Errorf("待签名串不得包含 sign 本身，got: %s", got)
	}
	if strings.Contains(got, "empty_param") {
		t.Errorf("空值参数不参与签名，got: %s", got)
	}

	want := `app_id=2021000000000000&biz_content={"out_trade_no":"pay_x"}&charset=utf-8` +
		`&method=alipay.trade.precreate&sign_type=RSA2&timestamp=2026-09-23 21:00:00&version=1.0`
	if got != want {
		t.Errorf("待签名串不正确\n got: %s\nwant: %s", got, want)
	}
}

// 支付宝在参数/charset 校验失败时用它自身的默认字符集（GBK）返回文案。
// 必须解码为可读中文，否则线上只能看到 "��ǩ..." 这种无从下手的乱码。
func TestDecodeBodyConvertsGBK(t *testing.T) {
	// 注意：不能用单个 0xC7 0xA9 来测 —— 它在 GBK 里是"签"，但这两个字节恰好也是
	// 合法的 2 字节 UTF-8 序列（U+01E9 "ǩ"），utf8.Valid 会放行。整段 GBK 文本几乎
	// 不可能全部落在合法 UTF-8 序列上，因此按"整体是否合法"判断是可靠的。
	gbk, err := simplifiedchinese.GBK.NewEncoder().Bytes([]byte("签名不正确，请检查charset参数"))
	if err != nil {
		t.Fatalf("构造 GBK 字节: %v", err)
	}
	if utf8.Valid(gbk) {
		t.Fatal("测试前提不成立：该 GBK 文本不应是合法 UTF-8")
	}
	if got := decodeBody(gbk); got != "签名不正确，请检查charset参数" {
		t.Errorf("GBK 解码 = %q", got)
	}

	// 合法 UTF-8 必须原样返回，不能被误当成 GBK 二次编码
	if got := decodeBody([]byte("中文 UTF-8 正常")); got != "中文 UTF-8 正常" {
		t.Errorf("合法 UTF-8 被改动: %q", got)
	}
	if got := decodeBody([]byte("plain ascii")); got != "plain ascii" {
		t.Errorf("ASCII 被改动: %q", got)
	}
}

// 端到端复刻线上故障：支付宝用 GBK 返回错误 JSON 时，错误信息里的 sub_msg 必须可读，
// 而不是 "��ǩ..." 这种只能靠猜的乱码。
func TestAlipayPrecreateDecodesGBKErrorResponse(t *testing.T) {
	const wantSubMsg = "签名不正确，请检查charset参数和参数值"
	payload := `{"alipay_trade_precreate_response":{"code":"40002","msg":"Invalid Arguments","sub_msg":"` + wantSubMsg + `"}}`
	gbkBody, err := simplifiedchinese.GBK.NewEncoder().Bytes([]byte(payload))
	if err != nil {
		t.Fatalf("构造 GBK 响应: %v", err)
	}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json;charset=GBK")
		_, _ = w.Write(gbkBody)
	}))
	defer srv.Close()

	priv, pub := testKeyPEM(t)
	client, err := NewAlipayClient("2021000000000000", priv, pub, srv.URL, "")
	if err != nil {
		t.Fatalf("构造客户端: %v", err)
	}

	_, err = client.Precreate(context.Background(), "pay_test_order", 100, "test")
	if err == nil {
		t.Fatal("业务失败应返回错误")
	}
	if !strings.Contains(err.Error(), wantSubMsg) {
		t.Errorf("sub_msg 应可读（GBK 已解码），got: %s", err.Error())
	}
	if !strings.Contains(err.Error(), "40002") {
		t.Errorf("错误信息应含业务码，got: %s", err.Error())
	}
}

func TestResponseSnippetFoldsWhitespaceAndTruncates(t *testing.T) {
	if got := responseSnippet(nil); got != "(空响应)" {
		t.Errorf("空响应 = %q", got)
	}
	multi := []byte("<html>\n  <body>\n    <p>x</p>\n  </body>\n</html>")
	if got := responseSnippet(multi); strings.Contains(got, "\n") {
		t.Errorf("多行响应应折叠为单行，got: %q", got)
	}
	long := []byte(strings.Repeat("a", 500))
	got := responseSnippet(long)
	if len(got) > 210 || !strings.HasSuffix(got, "...") {
		t.Errorf("超长响应应截断，got len=%d", len(got))
	}
}
