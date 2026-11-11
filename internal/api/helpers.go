package api

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
)

// P0性能优化：集中管理超时常量，避免散落各处的魔法数字
const (
	DefaultAgentTimeout = 300 * time.Second
	DefaultMaxTurns     = 5
	// DefaultMaxOutputTokens 单次 LLM 调用的默认 max output tokens。
	//
	// M20：它不是 run 预算 —— run 全程的 token 上限在引擎侧（`AGENT_RUN_MAX_TOKENS`）。
	// 与后台「系统设置」的 `agent.max_tokens` 是同一个量；与引擎 `app/main.py` 的兜底
	// 8192 同值，所以"网关没配"时行为与改动前完全一致。
	DefaultMaxOutputTokens = 8192
)

// nullableStr returns nil for empty strings, useful for nullable DB columns.
func nullableStr(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// intVal returns the int value of a JSON field or a default value.
func intVal(m map[string]interface{}, key string, fallback int) int {
	if v, ok := m[key]; ok {
		if f, ok := v.(float64); ok {
			return int(f)
		}
	}
	return fallback
}

// newUUID generates a new UUID string.
func newUUID() string {
	return uuid.New().String()
}

// signHMACSHA256 signs body with secret using HMAC-SHA256.
func signHMACSHA256(body []byte, secret string) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(body)
	return hex.EncodeToString(mac.Sum(nil))
}

// bytesReader wraps []byte into io.Reader.
func bytesReader(b []byte) io.Reader {
	return bytes.NewReader(b)
}

// httpClient is the default HTTP client for outgoing webhook requests.
//
// SSRF 防护（P0 修复）：**不跟随重定向**。默认 client 会跟随最多 10 跳，而出站
// webhook 的初始 URL 虽经私网校验（validateWebhookURL），攻击者服务器仍可用
// 302 把请求引到 `http://169.254.169.254/latest/meta-data/` 或 `http://127.0.0.1:…`
// —— 校验只在创建/首次解析时做过，重定向后完全失效。
// `ErrUseLastResponse` 让 client 直接返回 3xx 响应（投递方只读 statusCode，语义不变）。
//
// SSRF 防护（DNS rebinding）：见 newGuardedTransport —— 私网判定被下沉到拨号那一刻。
var httpClient = &http.Client{
	Timeout: 10 * time.Second,
	Transport: newGuardedTransport(nil),
	CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	},
}

// isBlockedOutboundIP 判定出站目标是否落在禁止范围：回环、私网、链路本地、
// 未指定、组播。webhook 与所有其它出站校验共用这一处判定，避免两套规则漂移。
func isBlockedOutboundIP(ip net.IP) bool {
	if ip == nil {
		return true
	}
	// 统一按 4 字节视图比较：`::ffff:127.0.0.1` 这类 IPv4-mapped 地址若不归一，
	// IsPrivate/IsLoopback 的结论会取决于 Go 版本内部实现细节，而不是地址本身。
	if v4 := ip.To4(); v4 != nil {
		ip = v4
	}
	return ip.IsLoopback() ||
		ip.IsPrivate() ||
		ip.IsLinkLocalUnicast() ||
		ip.IsLinkLocalMulticast() ||
		ip.IsInterfaceLocalMulticast() ||
		ip.IsMulticast() ||
		ip.IsUnspecified()
}

// resolveOutboundIPs 解析 host 并校验**每一个**解析结果，返回可用于拨号的 IP 列表。
//
// 返回的列表就是后续真正要连的地址——调用方不得再解析一次，否则又开出一个
// 重绑定窗口。也正因如此，这里对「任一结果为内网」采取整体拒绝而非跳过：
// 攻击者只要让 DNS 同时在答案里放一个公网 IP 和一个内网 IP，跳过式过滤就
// 会被用来做轮询探测。
func resolveOutboundIPs(ctx context.Context, host string) ([]net.IP, error) {
	return resolveOutboundIPsGuarded(ctx, host, isBlockedOutboundIP)
}

// resolveOutboundIPsGuarded 是 resolveOutboundIPs 的可定制判定版本。
//
// blocked 必须与「拨号前那一层检查」用**同一个**判定函数，否则两处规则一旦
// 分叉，就会出现「解析阶段放行、拨号阶段拒绝」这种自相矛盾的行为（本函数
// 的第一次实现就是这样：内部写死了 isBlockedOutboundIP，导致调用方传入的
// 放行规则在解析阶段被忽略，测试里连 httptest 的回环地址都连不上）。
func resolveOutboundIPsGuarded(ctx context.Context, host string, blocked func(net.IP) bool) ([]net.IP, error) {
	// IPv6 链路本地地址可带 zone（fe80::1%eth0），且这种地址一律应该拒绝。
	if strings.Contains(host, "%") {
		return nil, fmt.Errorf("target address not allowed: %s", host)
	}
	if ip := net.ParseIP(host); ip != nil {
		if blocked(ip) {
			return nil, fmt.Errorf("target address not allowed: %s", ip)
		}
		return []net.IP{ip}, nil
	}
	ips, err := net.DefaultResolver.LookupIP(ctx, "ip", host)
	if err != nil {
		return nil, fmt.Errorf("dns lookup: %w", err)
	}
	if len(ips) == 0 {
		return nil, fmt.Errorf("dns lookup: no address for %s", host)
	}
	for _, ip := range ips {
		if blocked(ip) {
			return nil, fmt.Errorf("target address not allowed: %s", ip)
		}
	}
	return ips, nil
}

// newGuardedTransport 构造带「拨号期地址守卫」的 Transport。
//
// 为什么必须在 DialContext 里校验、而不是只在 validateWebhookURL 里做一次：
// 那是典型的 TOCTOU —— 预检时 DNS 回答公网 IP 通过了校验，等 client 真正
// 拨号时再解析一次，攻击者的 DNS 把 TTL 设成 0 并改答 `127.0.0.1`，请求就打到
// 了本机服务。校验与拨号之间只要存在第二次解析，这个窗口就一直在。
// 这里的做法是：**解析一次、校验这份结果、然后用这份结果直接拨号**，
// 中间没有第二次 DNS 查询，窗口因此为零。
//
// blocked 为 nil 时使用 isBlockedOutboundIP —— 生产路径**一律传 nil**。
// 非 nil 仅供测试放开 httptest 的回环监听；生产代码没有任何一处传自定义判定。
func newGuardedTransport(blocked func(net.IP) bool) *http.Transport {
	if blocked == nil {
		blocked = isBlockedOutboundIP
	}
	dialer := &net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}

	return &http.Transport{
		MaxIdleConns:       100,
		IdleConnTimeout:    90 * time.Second,
		DisableCompression: false,
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			host, port, err := net.SplitHostPort(addr)
			if err != nil {
				return nil, fmt.Errorf("webhook dial: bad address %q: %w", addr, err)
			}
			// 解析与校验都用同一个 blocked：两处规则必须一致。
			ips, err := resolveOutboundIPsGuarded(ctx, host, blocked)
			if err != nil {
				return nil, err
			}
			// 用已校验的 IP 直连，不再交给 dialer 按 hostname 解析一次。
			// TLS 的 SNI 与证书校验仍用原始 hostname（由 http.Transport 的
			// cm.tlsHost() 负责），所以换成 IP 拨号不影响证书校验。
			var lastErr error
			for _, ip := range ips {
				conn, derr := dialer.DialContext(ctx, network, net.JoinHostPort(ip.String(), port))
				if derr == nil {
					return conn, nil
				}
				lastErr = derr
			}
			return nil, lastErr
		},
	}
}

// contextWithTimeout returns a context with timeout.
func contextWithTimeout(d time.Duration) (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.Background(), d)
}
