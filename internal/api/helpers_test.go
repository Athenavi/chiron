package api

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// P4：出站 webhook 的 SSRF 防护 —— `httpClient` 必须**不跟随重定向**。
//
// 修复前：初始 URL 经 validateWebhookURL 的私网校验通过后，攻击者服务器可用 302
// 把请求引到 169.254.169.254 / 127.0.0.1（默认 client 会跟最多 10 跳），校验形同虚设。
func TestWebhookClientDoesNotFollowRedirects(t *testing.T) {
	reached := false
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reached = true
		w.WriteHeader(http.StatusOK)
	}))
	defer target.Close()

	redirector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL, http.StatusFound)
	}))
	defer redirector.Close()

	resp, err := httpClient.Get(redirector.URL)
	if err != nil {
		t.Fatalf("请求失败：%v", err)
	}
	defer resp.Body.Close()

	if reached {
		t.Error("httpClient 跟随了重定向 ⇒ 私网校验可被 302 绕过（SSRF）")
	}
	if resp.StatusCode != http.StatusFound {
		t.Errorf("应原样返回 302，得到 %d", resp.StatusCode)
	}
}
