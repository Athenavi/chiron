package api

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"log/slog"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

// login_lockout.go —— 按**账号**维度的登录失败锁定。
//
// 为什么必须在 IP 计数之外再加一层：`captcha_handler.go` 的 `login:fail:{ip}`
// 只能挡住"同一台机器反复试"。撞库的实际形态是**分布式**的 —— 攻击者用代理池
// 让每次尝试来自不同 IP，IP 维度的计数永远到不了阈值，而目标账号被尝试成千上万次。
// 企业渗透测试几乎必报这一条。
//
// 设计取舍：
//   - key 用 **email 的 SHA-256**，不落明文：Redis 的 KEYS/SCAN 与慢日志都会
//     暴露 key 名，邮箱本身属于个人信息。
//   - 窗口 15 分钟、阈值 5 次：与 OWASP 对账号锁定的建议一致；比 IP 那层的
//     阈值更严，因为它保护的是"某一个账号被攻破"，代价是合法用户也会被误锁
//     15 分钟 —— 可以通过重置密码解锁（重置成功会清计数）。
//   - **不存在的邮箱同样计数**：否则攻击者能用"是否被锁"判断账号是否存在
//     （不存在的账号永不累积 → 永不锁定 → 可枚举）。这条路与
//     `logLoginFailure` 对所有失败一视同仁的口径一致。
//   - Redis 不可用时 **fail-open**（放行 + 告警）：锁定的目的是防暴力破解，
//     而 Redis 故障时若 fail-close，整个登录入口会全站不可用 —— 那是用
//     可用性换一个概率性收益，不划算。这与 RBAC 的 fail-close 不同，因为
//     那里是"授权判定的依据读不到"，这里是"附加防护的位置读不到"。

const (
	// loginLockoutThreshold 达到该失败次数即锁定。
	loginLockoutThreshold = 5
	// loginLockoutWindow 既是计数窗口，也是锁定持续时间（key 的 TTL）。
	loginLockoutWindow = 15 * time.Minute
	// loginLockoutKeyPrefix 与 IP 维度的 login:fail: 区分开，避免互相干扰。
	loginLockoutKeyPrefix = "login:lockout:acct:"
)

// loginLockout 按邮箱统计密码登录失败次数。零值不可用，用 newLoginLockout。
type loginLockout struct {
	// rdb 每次调用时现取 db.Redis，而不是构造时快照：网关启动时可能尚未
	// 完成 Redis 初始化，快照到 nil 会让这层防护永久失效且无任何信号。
	rdb func() db.RedisClient
}

func newLoginLockout() *loginLockout {
	return &loginLockout{rdb: func() db.RedisClient { return db.Redis }}
}

// lockoutKey 用邮箱哈希做 key，避免明文个人信息进入 Redis 键空间。
func lockoutKey(email string) string {
	sum := sha256.Sum256([]byte(strings.ToLower(strings.TrimSpace(email))))
	return loginLockoutKeyPrefix + hex.EncodeToString(sum[:16])
}

// failures 返回当前窗口内的失败次数；Redis 不可用或出错时返回 0（放行）。
func (l *loginLockout) failures(ctx context.Context, email string) int {
	rdb := l.rdb()
	if rdb == nil || email == "" {
		return 0
	}
	n, err := rdb.Get(ctx, lockoutKey(email)).Int()
	if err != nil {
		return 0 // redis.Nil（无记录）与连接错误都按 0 处理
	}
	return n
}

// Locked 判断该邮箱是否已达锁定阈值。
func (l *loginLockout) Locked(ctx context.Context, email string) bool {
	return l.failures(ctx, email) >= loginLockoutThreshold
}

// RemainingAttempts 返回锁定前还剩几次机会（已锁定时为 0）。
// 用于给用户可操作的提示，而不是只说"失败"。
func (l *loginLockout) RemainingAttempts(ctx context.Context, email string) int {
	left := loginLockoutThreshold - l.failures(ctx, email)
	if left < 0 {
		return 0
	}
	return left
}

// RecordFailure 记一次失败并在首次写入时设置窗口。
func (l *loginLockout) RecordFailure(ctx context.Context, email string) {
	rdb := l.rdb()
	if rdb == nil || email == "" {
		return
	}
	key := lockoutKey(email)
	if err := rdb.Incr(ctx, key).Err(); err != nil {
		// 计数写不进去 = 这层防护暂时失效，必须留下痕迹（安全事件静默失败
		// 是最糟的形态：以为有防护，实际没有）。
		slog.Warn("login lockout: incr failed (lockout inactive)", "error", err)
		return
	}
	rdb.Expire(ctx, key, loginLockoutWindow)
}

// Clear 登录成功或重置密码后清除计数。
func (l *loginLockout) Clear(ctx context.Context, email string) {
	rdb := l.rdb()
	if rdb == nil || email == "" {
		return
	}
	if err := rdb.Del(ctx, lockoutKey(email)).Err(); err != nil {
		slog.Warn("login lockout: clear failed", "error", err)
	}
}
