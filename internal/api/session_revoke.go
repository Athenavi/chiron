package api

import (
	"context"
	"errors"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/athenavi/chiron/internal/db"
)

// 会话纪元（session epoch）—— 让"改密/密码重置"这个标准止血动作真正生效。
//
// ## 为什么需要它
//
// JWT 是无状态的：改密成功后，**此前签发的 token 仍然有效**直到过期（默认 24h）。
// 也就是说凭据一旦泄露，用户改密并不能把攻击者踢下线 —— 而改密正是业界标准的
// 止血动作。全仓此前没有任何"按用户批量撤销"的机制（只有按 jti 的单次登出黑名单）。
//
// ## 与 jwt:blacklist 的分工
//
// * `jwt:blacklist:<jti>` —— **单次登出**，精确到某一个 token；
// * `jwt:epoch:<userID>`  —— **批量撤销**，一次让该用户此刻之前的**所有** token 失效。
//
// ## 判定方式
//
// 纪元值是该用户被撤销时的 Unix 秒。`authMW` 每次请求比对：
// `epoch > token.iat` ⇒ 该 token 签发于撤销之前 ⇒ 拒绝。
// 精度是**秒**：同一秒内签发又撤销的 token 不会被拦（窗口 1 秒，可接受）。
const sessionEpochKeyPrefix = "jwt:epoch:"

// sessionEpochTTL 略大于 JWT 最大有效期：保证被撤销的 token 在自然过期前，
// 纪元键一直存在（否则纪元一消失，旧 token 又"复活"了）。
const sessionEpochTTL = 25 * time.Hour

// revokeUserSessions 让该用户**当前已签发**的所有 JWT 立即失效。
//
// 返回 error 由调用方决定如何处置：改密本身已经成功，不该因为撤销失败而回滚，
// 但**必须记日志**（否则"以为踢下线了、其实没有"）。
func revokeUserSessions(ctx context.Context, userID string) error {
	if userID == "" {
		return errors.New("session revoke: empty user id")
	}
	if db.Redis == nil {
		return errors.New("session revoke store unavailable (redis not configured)")
	}
	return db.Redis.Set(ctx, db.RedisKey(sessionEpochKeyPrefix+userID),
		time.Now().Unix(), sessionEpochTTL).Err()
}

// sessionEpoch 读取用户的会话纪元；无记录（从未被撤销）返回 0。
//
// Redis 不可用时返回 error —— 调用方（authMW）按 **fail-close** 处理：拒绝请求，
// 而不是放行（放行会让撤销在故障期间失效）。
func sessionEpoch(ctx context.Context, userID string) (int64, error) {
	if userID == "" {
		return 0, nil
	}
	if db.Redis == nil {
		return 0, errors.New("session revoke store unavailable (redis not configured)")
	}
	epoch, err := db.Redis.Get(ctx, db.RedisKey(sessionEpochKeyPrefix+userID)).Int64()
	if errors.Is(err, redis.Nil) {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	return epoch, nil
}
