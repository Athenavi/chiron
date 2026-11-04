package config

import (
	"bufio"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// `.env.example` 里 APP_SECRET 的值必须**不可用**。
//
// 回归背景：示例值曾是
// `your-32-plus-char-random-secret-generate-with-secrets-token-urlsafe` ——
// 66 个字符，**越过**了 ValidateJWTSecret 的 32 字符门槛，于是
//
//	cp .env.example .env && docker compose up
//
// 就能带着一把**仓库里人人可见**的密钥跑起来：它派生 JWT_SECRET / INTERNAL_TOKEN、
// 加密 system_settings 里的 LLM/S3/支付密钥。攻击者拿仓库里的这份示例值，
// 就能伪造任意用户的 JWT、解开所有已入库的敏感配置。
//
// 所以这个断言盯两件事：示例值为空；或虽非空但会被 ValidateJWTSecret 拒绝。
// 光靠 weakSecrets 黑名单不够 —— 改示例值时很容易忘记同步黑名单。
func TestEnvExampleAppSecretIsUnusable(t *testing.T) {
	path := filepath.Join("..", ".env.example")
	f, err := os.Open(path)
	if err != nil {
		t.Fatalf("打不开 %s：%v", path, err)
	}
	defer f.Close()

	// 与 .env 解析口径一致：可有 `export ` 前缀，值两侧空白不算内容。
	line := regexp.MustCompile(`^\s*(?:export\s+)?APP_SECRET\s*=(.*)$`)

	var (
		found bool
		value string
	)
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		if m := line.FindStringSubmatch(scanner.Text()); m != nil {
			found = true
			value = strings.TrimSpace(m[1])
			// 去掉尾随的行内注释（`APP_SECRET=   # 说明` 形态）
			if i := strings.Index(value, "#"); i == 0 {
				value = ""
			}
			break
		}
	}
	if err := scanner.Err(); err != nil {
		t.Fatalf("读 %s 失败：%v", path, err)
	}
	if !found {
		t.Fatalf("%s 里没有 APP_SECRET 条目 —— 模板必须让人知道这个变量存在", path)
	}
	if value == "" {
		return // 理想形态：留空，强制使用者填一个自己的
	}
	if ValidateJWTSecret(value) {
		t.Fatalf(".env.example 的 APP_SECRET=%q 能通过强度校验：照抄模板就能用它启动，"+
			"等于把全仓公开的密钥当成部署密钥。请留空，或换成必然被拒绝的占位值。", value)
	}
}

// 同一坚守：已知的公开示例值必须被强制拒绝，而不只是"恰好长度不够"。
func TestKnownExampleSecretsAreRejected(t *testing.T) {
	known := []string{
		"your-32-plus-char-random-secret-generate-with-secrets-token-urlsafe",
		"dev-secret-change-in-production",
		"dev-secret-change-in-production-12345678",
		"change-me",
		"changeme",
		"secret",
		"test-secret",
	}
	for _, s := range known {
		if ValidateJWTSecret(s) {
			t.Fatalf("公开/弱示例密钥 %q 被接受了 —— 它会成为所有照抄者的同一把密钥", s)
		}
	}
}

// 长度门槛与"非空"两个边界，避免以后重构把校验放宽。
func TestValidateJWTSecretBoundaries(t *testing.T) {
	cases := []struct {
		name   string
		secret string
		want   bool
	}{
		{"空", "", false},
		{"31 字符", strings.Repeat("a", 31), false},
		{"32 字符", strings.Repeat("a", 32), true},
		{"长随机串", strings.Repeat("x9Qz", 20), true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := ValidateJWTSecret(tc.secret); got != tc.want {
				t.Fatalf("ValidateJWTSecret(len=%d) = %v, want %v", len(tc.secret), got, tc.want)
			}
		})
	}
}
