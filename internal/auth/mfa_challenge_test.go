package auth

import (
	"strings"
	"testing"
	"time"
)

// ── MFA 挑战令牌的隔离性 ──
//
// 这是整个 MFA 最容易被写错、且写错后**完全不报错**的地方：
// 如果挑战令牌能通过 ValidateToken（AuthMiddleware 用的那个），
// 那么"密码正确但还没过 TOTP"的客户端拿着挑战令牌就能直接访问所有业务接口 ——
// 双因子形同虚设，而功能测试全绿（因为它确实能登录）。
//
// 实现上用**派生密钥**而非"标志位 + 显式检查"来隔离：
// 靠检查标志位，漏掉任何一处调用点就是完整绕过；靠密钥隔离则是结构性的。

func newTestAuthenticator() *Authenticator {
	return NewAuthenticator(strings.Repeat("k", 48), time.Hour)
}

func TestMFAChallengeTokenIsNotAValidSessionToken(t *testing.T) {
	a := newTestAuthenticator()

	challenge, err := a.GenerateMFAChallengeToken("user-1")
	if err != nil {
		t.Fatalf("生成挑战令牌失败: %v", err)
	}

	// 核心断言：挑战令牌**不得**能通过 ValidateToken。
	if _, err := a.ValidateToken(challenge); err == nil {
		t.Fatal("挑战令牌通过了 ValidateToken：MFA 可被一次请求绕过")
	}

	// 反向也成立：正式 token 不能被当作挑战令牌使用。
	session, err := a.GenerateToken("user-1", "u@example.com", "user", 1, nil)
	if err != nil {
		t.Fatalf("生成正式令牌失败: %v", err)
	}
	if _, err := a.ValidateMFAChallengeToken(session); err == nil {
		t.Fatal("正式令牌通过了挑战令牌校验：两者必须互不通用")
	}
}

func TestMFAChallengeTokenRoundTrip(t *testing.T) {
	a := newTestAuthenticator()

	challenge, err := a.GenerateMFAChallengeToken("user-42")
	if err != nil {
		t.Fatalf("生成失败: %v", err)
	}
	uid, err := a.ValidateMFAChallengeToken(challenge)
	if err != nil {
		t.Fatalf("自有挑战令牌应能通过校验: %v", err)
	}
	if uid != "user-42" {
		t.Fatalf("userID = %q，期望 user-42", uid)
	}
}

// 另一把主密钥签发的挑战令牌不能被接受（防止跨环境/跨部署混用）。
func TestMFAChallengeTokenRejectsForeignKey(t *testing.T) {
	a := newTestAuthenticator()
	b := NewAuthenticator(strings.Repeat("x", 48), time.Hour)

	challenge, err := a.GenerateMFAChallengeToken("user-1")
	if err != nil {
		t.Fatalf("生成失败: %v", err)
	}
	if _, err := b.ValidateMFAChallengeToken(challenge); err == nil {
		t.Fatal("另一把密钥签发的挑战令牌被接受了")
	}
}

// 挑战令牌必须是短期凭证：有效期不同于会话令牌，且是分钟级。
func TestMFAChallengeTTLIsShort(t *testing.T) {
	secs := MFAChallengeTTLSeconds()
	if secs <= 0 || secs > 600 {
		t.Fatalf("挑战令牌有效期 %d 秒，应在 (0, 600] 之间 —— 太长会扩大验证码被暴力枚举的窗口", secs)
	}
}

// 空串与畸形输入不得 panic，也不得通过。
func TestMFAChallengeTokenRejectsGarbage(t *testing.T) {
	a := newTestAuthenticator()
	for _, bad := range []string{"", "not-a-token", "a.b.c", strings.Repeat("z", 200)} {
		if _, err := a.ValidateMFAChallengeToken(bad); err == nil {
			t.Fatalf("畸形输入 %q 通过了校验", bad)
		}
	}
}
