package api

import "testing"

// 「哪个字段该被响应脱敏」的回归 —— 这个包此前**没有任何测试文件**，所以下面那条缺陷
// （登录/注册的 token 被打码）能在 CI 沉默很久。
func TestSensitiveFieldsKeepsClientCredentials(t *testing.T) {
	// 回归：`token` 曾被列进 SensitiveFields，而 sanitizeSensitivePaths 的白名单**包含**
	// `/v1/auth/login` 与 `/v1/auth/register`（意图是脱敏密码传输）⇒ 这两个响应体里的
	// `token` 被替换成 `********`。症状极具迷惑性：浏览器端一切正常（走 SetTokenCookie 的
	// **响应头**，不经 body 脱敏），而任何读 body 的 API/SDK 客户端一律 401
	// （`test_chiron_client_sdk_live.py` 自加入起从未绿过）。
	if isSensitiveField("token") {
		t.Fatal("token 不能脱敏：它是本来就要交给客户端的凭据，脱敏它等于打破认证")
	}
	if isSensitiveField("access_token") {
		t.Fatal("access_token 同理：它也是要交给客户端的凭据")
	}

	// 反向：真正该脱敏的仍必须命中（`isSensitiveField` 用的是**子串**匹配）。
	for _, field := range []string{"password", "client_secret", "private_key", "api_key", "dsn"} {
		if !isSensitiveField(field) {
			t.Fatalf("%q 必须脱敏", field)
		}
	}
}
