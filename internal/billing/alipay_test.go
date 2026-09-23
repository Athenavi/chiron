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
