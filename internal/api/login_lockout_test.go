package api

import (
	"context"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/redis/go-redis/v9"
)

// ── 账号级登录锁定 ──
//
// 为什么需要这层（而不是只靠 captcha_handler.go 的 login:fail:{ip}）：
// 撞库的真实形态是分布式的 —— 每次尝试换一个代理 IP，IP 维度计数永远到不了
// 阈值，而目标账号被尝试成千上万次。只有按账号计数才能挡住。
//
// 这组用例钉住五个边界，每个都对应一种"看起来实现了、实际没防护"的写法：
//  1. 阈值以下放行（别把正常用户锁死）；
//  2. 达到阈值即锁定（防护真的生效）；
//  3. 成功后清零（否则合法用户偶发打错会持续逼近阈值）；
//  4. 邮箱大小写/空格归一（`A@x.com` 与 `a@x.com ` 必须是同一个账号 ——
//     否则攻击者只要变换大小写就能绕过计数）；
//  5. Redis 不可用时放行（fail-open）—— 用可用性换概率性收益不划算，
//     但必须在日志里可见。

// lockoutFakeRedis 只实现锁定逻辑用到的四个命令。
// 其余方法由内嵌接口提供（本用例不会调用）。
type lockoutFakeRedis struct {
	db.RedisClient
	values  map[string]int
	ttls    map[string]time.Duration
	failGet bool
}

func newLockoutFakeRedis() *lockoutFakeRedis {
	return &lockoutFakeRedis{values: map[string]int{}, ttls: map[string]time.Duration{}}
}

func (f *lockoutFakeRedis) Get(_ context.Context, key string) *redis.StringCmd {
	if f.failGet {
		return redis.NewStringResult("", redis.Nil)
	}
	n, ok := f.values[key]
	if !ok {
		return redis.NewStringResult("", redis.Nil)
	}
	return redis.NewStringResult(strconv.Itoa(n), nil)
}

func (f *lockoutFakeRedis) Incr(_ context.Context, key string) *redis.IntCmd {
	f.values[key]++
	return redis.NewIntResult(int64(f.values[key]), nil)
}

func (f *lockoutFakeRedis) Expire(_ context.Context, key string, ttl time.Duration) *redis.BoolCmd {
	f.ttls[key] = ttl
	return redis.NewBoolResult(true, nil)
}

func (f *lockoutFakeRedis) Del(_ context.Context, keys ...string) *redis.IntCmd {
	var n int64
	for _, k := range keys {
		if _, ok := f.values[k]; ok {
			delete(f.values, k)
			n++
		}
	}
	return redis.NewIntResult(n, nil)
}

func newLockoutWith(fake db.RedisClient) *loginLockout {
	return &loginLockout{rdb: func() db.RedisClient { return fake }}
}

func TestLoginLockoutThresholdBoundary(t *testing.T) {
	fake := newLockoutFakeRedis()
	l := newLockoutWith(fake)
	ctx := context.Background()
	const email = "victim@example.com"

	// 阈值以下一律放行。
	for i := 0; i < loginLockoutThreshold-1; i++ {
		l.RecordFailure(ctx, email)
		if l.Locked(ctx, email) {
			t.Fatalf("第 %d 次失败后就锁定了，阈值应仍有余量", i+1)
		}
	}
	if got := l.RemainingAttempts(ctx, email); got != 1 {
		t.Fatalf("阈值前最后一次应剩 1 次，得到 %d", got)
	}

	// 达到阈值即锁定。
	l.RecordFailure(ctx, email)
	if !l.Locked(ctx, email) {
		t.Fatal("达到阈值后仍未锁定：这层防护等于没做")
	}
	if got := l.RemainingAttempts(ctx, email); got != 0 {
		t.Fatalf("已锁定时剩余次数应为 0，得到 %d", got)
	}
}

// 登录成功必须清零，否则合法用户偶发打错会在一段时间内持续逼近阈值，
// 最终被自己的正常操作锁在门外。
func TestLoginLockoutClearedOnSuccess(t *testing.T) {
	fake := newLockoutFakeRedis()
	l := newLockoutWith(fake)
	ctx := context.Background()
	const email = "user@example.com"

	for i := 0; i < loginLockoutThreshold-1; i++ {
		l.RecordFailure(ctx, email)
	}
	l.Clear(ctx, email)

	if got := l.RemainingAttempts(ctx, email); got != loginLockoutThreshold {
		t.Fatalf("成功后计数应归零（剩余 %d），得到 %d", loginLockoutThreshold, got)
	}
	if l.Locked(ctx, email) {
		t.Fatal("清除后不应处于锁定状态")
	}
}

// 邮箱归一：大小写与首尾空格的变化必须落到**同一个**计数上。
// 若不做归一，攻击者只要把邮箱写成 `Victim@Example.com`、`VICTIM@example.com`……
// 就能无限次尝试同一个账号而计数永远是 1。
func TestLoginLockoutNormalizesEmail(t *testing.T) {
	fake := newLockoutFakeRedis()
	l := newLockoutWith(fake)
	ctx := context.Background()

	variants := []string{
		"victim@example.com",
		"Victim@Example.com",
		"  victim@example.com  ",
		"VICTIM@EXAMPLE.COM",
		"victim@example.com",
	}
	for _, v := range variants {
		l.RecordFailure(ctx, v)
	}

	if !l.Locked(ctx, "ViCtIm@ExAmPlE.CoM") {
		t.Fatal("大小写变体绕过了计数：同一账号被当成多个账号")
	}
}

// key 里不能出现明文邮箱：Redis 的 KEYS/SCAN 与慢日志都会暴露 key 名，
// 邮箱属于个人信息，不应以明文留在键空间。
func TestLoginLockoutKeyHidesEmail(t *testing.T) {
	const email = "sensitive.person@corp.example"
	key := lockoutKey(email)
	if strings.Contains(strings.ToLower(key), "sensitive") ||
		strings.Contains(strings.ToLower(key), "corp.example") {
		t.Fatalf("锁定 key 泄露了明文邮箱：%s", key)
	}
	if !strings.HasPrefix(key, loginLockoutKeyPrefix) {
		t.Fatalf("key 前缀不对：%s", key)
	}
	// 同一邮箱必须稳定映射到同一 key，否则计数会被打散。
	if lockoutKey("  SENSITIVE.PERSON@corp.example ") != key {
		t.Fatal("归一化后应得到同一个 key")
	}
}

// Redis 不可用时必须放行（fail-open）：锁定是附加防护，若这里 fail-close，
// Redis 一抖整个登录入口就全站不可用 —— 拿可用性换概率性收益不划算。
// 注意这与 RBAC 的 fail-close 不矛盾：那边是"授权依据读不到"，这边是
// "附加防护的位置读不到"，两者的正确方向相反。
func TestLoginLockoutFailsOpenWithoutRedis(t *testing.T) {
	l := newLockoutWith(nil) // 模拟 db.Redis == nil
	ctx := context.Background()

	// 即使记了很多次，没有 Redis 也不该锁人。
	for i := 0; i < loginLockoutThreshold*3; i++ {
		l.RecordFailure(ctx, "user@example.com")
	}
	if l.Locked(ctx, "user@example.com") {
		t.Fatal("Redis 不可用时不应锁定（fail-open），否则 Redis 故障会连带登录全站不可用")
	}

	// 空邮箱也不该 panic 或误锁。
	l.RecordFailure(ctx, "")
	if l.Locked(ctx, "") {
		t.Fatal("空邮箱不应被锁定")
	}
}

// 首次失败要设置窗口 TTL，否则计数永不过期 —— 一个 15 分钟窗口的防护
// 会变成"只要累计错 5 次就永久锁定"。
func TestLoginLockoutSetsWindowTTL(t *testing.T) {
	fake := newLockoutFakeRedis()
	l := newLockoutWith(fake)
	ctx := context.Background()

	l.RecordFailure(ctx, "user@example.com")

	key := lockoutKey("user@example.com")
	ttl, ok := fake.ttls[key]
	if !ok {
		t.Fatal("失败计数没有设置 TTL：锁定将永不到期")
	}
	if ttl != loginLockoutWindow {
		t.Fatalf("TTL = %v，期望 %v", ttl, loginLockoutWindow)
	}
}
