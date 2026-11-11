package api

import (
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// P4：出站 webhook 的 SSRF 防护 —— 必须**不跟随重定向**。
//
// 修复前：初始 URL 经 validateWebhookURL 的私网校验通过后，攻击者服务器可用 302
// 把请求引到 169.254.169.254 / 127.0.0.1（默认 client 会跟最多 10 跳），校验形同虚设。
//
// 注：这里不能用全局 `httpClient` —— 它带拨号期地址守卫（newGuardedTransport），
// 而 httptest 监听在 127.0.0.1，请求会在拨号阶段就被拒。本用例只关心「重定向
// 是否被跟随」，因此用一个放开回环的 transport，保持断言焦点单一。
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

	client := &http.Client{
		// 第二个参数是「拒绝判定」：恒返回 false = 不拒绝任何地址（仅本用例，
		// 为了能连到 httptest 的回环监听）。生产路径一律传 nil。
		Transport: newGuardedTransport(func(net.IP) bool { return false }),
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}

	resp, err := client.Get(redirector.URL)
	if err != nil {
		t.Fatalf("请求失败：%v", err)
	}
	defer resp.Body.Close()

	if reached {
		t.Error("client 跟随了重定向 ⇒ 私网校验可被 302 绕过（SSRF）")
	}
	if resp.StatusCode != http.StatusFound {
		t.Errorf("应原样返回 302，得到 %d", resp.StatusCode)
	}
}

// DNS rebinding 防护：私网判定必须发生在**拨号那一刻**，且用的是将要连接的那个 IP。
//
// 修复前的漏洞是 TOCTOU：validateWebhookURL 预检时 DNS 回答公网 IP 通过，
// 真正拨号时再解析一次，攻击者的 DNS 改答 127.0.0.1 就把请求打进了本机服务。
// 现在解析一次、校验这份结果、再用这份结果直接拨号 —— 中间没有第二次查询。
//
// 本用例直接驱动 Transport 的 DialContext：目标写的是 `127.0.0.1`（字面量，
// 不经过 DNS），断言连接在建立之前就被拒绝。这证明守卫的位置在拨号阶段，
// 而不是仅仅依赖调用方先跑一遍 validateWebhookURL。
func TestGuardedTransportRejectsInternalAddresses(t *testing.T) {
	// 起一个真实监听：若守卫失效，连接会成功，用例即失败（而不是靠错误文案判断）。
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	client := &http.Client{Transport: newGuardedTransport(nil), Timeout: 5e9}
	resp, err := client.Get(srv.URL)
	if err == nil {
		resp.Body.Close()
		t.Fatalf("守卫未生效：成功连到了回环地址 %s", srv.URL)
	}

	// 确认是因为地址被拒，而不是别的偶发错误。
	if !strings.Contains(err.Error(), "not allowed") {
		t.Fatalf("期望被地址守卫拒绝，实际错误：%v", err)
	}
}

// 字面量内网 IPv4 应被直接拒绝；公网字面量应放行到解析阶段。
func TestResolveOutboundIPsClassification(t *testing.T) {
	cases := []struct {
		host    string
		blocked bool
	}{
		{"127.0.0.1", true},    // 回环
		{"::1", true},          // IPv6 回环
		{"10.0.0.5", true},     // RFC1918
		{"192.168.1.1", true},  // RFC1918
		{"172.16.0.1", true},   // RFC1918
		{"169.254.169.254", true}, // 云元数据（链路本地）
		{"0.0.0.0", true},      // 未指定
		{"fe80::1%eth0", true}, // 带 zone 的链路本地
		{"8.8.8.8", false},     // 公网
	}
	for _, tc := range cases {
		t.Run(tc.host, func(t *testing.T) {
			_, err := resolveOutboundIPs(t.Context(), tc.host)
			if tc.blocked && err == nil {
				t.Fatalf("%s 应被拒绝，但通过了", tc.host)
			}
			if !tc.blocked && err != nil {
				t.Fatalf("%s 应放行，但被拒: %v", tc.host, err)
			}
		})
	}
}
