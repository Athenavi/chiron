package config

import "testing"

// deriveSubsecret 的口径同时被三处实现：Go（这里）、Python
// （python-engine/app/config.py::_validate_security_defaults）与 Electron 主进程
// （frontend-vue/electron/local-stack.cjs::deriveSubsecret）。
//
// 三边任何一格改动（域标签、算法、base64 变体、是否保留 padding）都不会让改的那侧
// 报错，而是让**另两侧**认不出 token：打包态本地模式表现为网关→引擎全线 401，
// 引擎把网关铸的 JWT 判为无效。所以口径必须用同一组黄金向量钉死，
// 另两侧的等价断言见 frontend-vue/electron/__tests__/local-stack.test.cjs。
//
// 向量来源：HMAC-SHA256(secret, domain) → base64url 无 padding。
func TestDeriveSubsecretGoldenVectors(t *testing.T) {
	const secret = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" // 'x' * 32

	cases := []struct {
		domain string
		want   string
	}{
		{"chiron-jwt", "SYvVXpKr8n5cZ_vf3u_0XzzekZyu1aXr-v225-TQbTg"},
		{"chiron-internal", "BzcOjSRtC43_-5jEb9X-OwD3Degb-CMKy8RwVEyQ9ok"},
	}
	for _, tc := range cases {
		t.Run(tc.domain, func(t *testing.T) {
			got := deriveSubsecret(secret, tc.domain)
			if got != tc.want {
				t.Fatalf("deriveSubsecret(%q, %q) = %q, want %q", secret, tc.domain, got, tc.want)
			}
			// 无 padding：Python 侧 rstrip("=")，网关发给引擎的 token 差一个 '=' 就不相等
			if len(got) != 43 {
				t.Fatalf("derived token length = %d, want 43 (raw base64url, no padding)", len(got))
			}
		})
	}
}

func TestDeriveSubsecretEmptySecretYieldsEmpty(t *testing.T) {
	// 空密钥必须派生出空值，让 Load()/ValidateAppSecret 去拒绝启动；
	// 派生出"HMAC of empty"会让没配密钥的部署悄悄跑起来。
	if got := deriveSubsecret("", "chiron-internal"); got != "" {
		t.Fatalf("deriveSubsecret(\"\", ...) = %q, want empty", got)
	}
}
