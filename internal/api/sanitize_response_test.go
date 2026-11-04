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

// 嵌套结构必须一路递归到底：敏感字段名下面挂着对象/数组时也要进去。
//
// 回归背景：`sanitizeMap` 曾写成"命中敏感词就打码，否则才递归"，于是
//
//	{"config": {"payment": {"secret_key": "sk-live"}}}
//
// 里 `config` 不含敏感词 ⇒ 递归进去；`payment` 也不含 ⇒ 再进；`secret_key` 命中，
// 值恰好是字符串 ⇒ 打码。看起来没问题。但换成
//
//	{"secret_key": {"nested": "sk-live"}}
//
// —— `secret_key` 命中敏感词、值却不是字符串，**既不替换也不递归**，整棵子树原样
// 透传。这是脱敏里最危险的 fail-open：日志/单测都显示"命中了敏感词"，实际泄露。
func TestSanitizeMapRecursesThroughSensitiveKeys(t *testing.T) {
	payload := map[string]interface{}{
		"category": "payment",
		// ① 敏感键 + 对象值：曾整片跳过
		"api_key": map[string]interface{}{
			"secret_key": "sk-live-should-be-masked",
		},
		// ② 敏感键 + 数组值：曾整片跳过
		"keys": []interface{}{
			map[string]interface{}{"password": "p@ss-1"},
			map[string]interface{}{"note": "keep-me"},
		},
		// ③ 普通键 + 对象值：本来就会递归
		"config": map[string]interface{}{
			"credential": "cred-should-be-masked",
			"timeout":    30,
		},
		// ④ 非敏感、非字符串：必须原样保留
		"total": 3,
	}

	sanitizeMap(payload)

	// ① 与 ② 里的敏感值必须被打码 —— 这正是修复前漏掉的两处。
	apiKey := payload["api_key"].(map[string]interface{})
	if got := apiKey["secret_key"]; got != "********" {
		t.Fatalf("敏感键下的对象值未脱敏：api_key.secret_key = %v", got)
	}
	keysArr := payload["keys"].([]interface{})
	first := keysArr[0].(map[string]interface{})
	if got := first["password"]; got != "********" {
		t.Fatalf("敏感键下的数组元素未脱敏：keys[0].password = %v", got)
	}

	// ③ 普通键下的敏感字段照旧生效，非敏感值不被误伤。
	cfg := payload["config"].(map[string]interface{})
	if got := cfg["credential"]; got != "********" {
		t.Fatalf("config.credential 未脱敏：%v", got)
	}
	if got := cfg["timeout"]; got != 30 {
		t.Fatalf("非敏感字段被误改：config.timeout = %v", got)
	}

	// ④ 非敏感字段与普通文本必须原样保留。
	if got := keysArr[1].(map[string]interface{})["note"]; got != "keep-me" {
		t.Fatalf("普通字段被误改：keys[1].note = %v", got)
	}
	if got := payload["total"]; got != 3 {
		t.Fatalf("顶层非敏感字段被误改：total = %v", got)
	}
	if got := payload["category"]; got != "payment" {
		t.Fatalf("普通字符串字段被误改：category = %v", got)
	}
}

// 空字符串不脱敏：把"未配置"显示成 `********` 会让人以为配了值。
func TestSanitizeMapLeavesEmptySensitiveValuesAlone(t *testing.T) {
	payload := map[string]interface{}{
		"password": "",
		"secret":   "",
	}
	sanitizeMap(payload)
	for _, k := range []string{"password", "secret"} {
		if got := payload[k]; got != "" {
			t.Fatalf("空值不应被打码：%s = %v", k, got)
		}
	}
}
