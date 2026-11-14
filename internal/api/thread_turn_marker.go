package api

import (
	"context"
	"log/slog"
	"strconv"
	"sync"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

// 「该会话有投递回合正在跑」的集合。
//
// 为什么不能只看运行锁：运行锁回答"现在能不能起回合"，而队列续跑要回答另一个问题 ——
// "这一轮**彻底**收尾了吗"。两者在收尾点附近会分叉：锁刚还掉，而 FinishTurn / 计费校正
// 还在跑。只看锁就会在那一刻去领下一条，同会话两个 runtime 短兵相接（正是锁要防的事）。
// 所以额外记一个"谁还在跑"，续跑只在集合为空时推进。
//
// 用 ZSET 而不是 HASH：每个成员需要**各自的**截止时间。进程崩溃会把成员留在里面，
// 没有截止时间的话这个会话就永远"有回合在跑"、队列再也不动；而 HASH 不能给单个
// field 设 TTL，只能整键过期（会把还活着的回合一起清掉）。
//
// 命令全部走 `Do`：`db.RedisClient` 是刻意收窄的接口（只暴露网关真正用到的那几个
// 命令），没有 ZSET 方法。为这一处去扩接口会把"Redis 能用哪些操作"的约束放松掉，
// 而这里要的只是四条标准命令。
const (
	threadTurnsKeyPrefix = "thread:turns:"
	// 单个回合登记的存活时间。回合比它长时由心跳续期（startThreadTurnHeartbeat），
	// 心跳停下的（进程死了 / goroutine 被丢弃）自然过期，不会挡住队列。
	threadTurnOwnerTTL = 6 * time.Minute
	// 续期周期。比 threadTurnOwnerTTL 短一个量级，留足多拍容错。
	threadTurnRenewEvery = 60 * time.Second
	// 标记操作的 Redis 超时。这些调用都在起跑/收尾路径上，不能无限等。
	threadTurnRedisTMO = 3 * time.Second
)

// threadTurnsKey 是某会话的"投递回合在跑"集合。
func threadTurnsKey(sessionID string) string { return db.RedisKey(threadTurnsKeyPrefix + sessionID) }

// threadTurnDeadline 是此刻登记一个新回合该用的截止时间。
func threadTurnDeadline() string {
	return strconv.FormatInt(time.Now().Add(threadTurnOwnerTTL).Unix(), 10)
}

// markThreadTurn 登记一个正在跑的投递回合。owner 只需在同一会话内唯一（用 event_key）。
//
// fail-soft：登记失败只记日志。安全边界始终在运行锁上 —— 少一条登记只会让续跑多一次
// 抢锁失败，不会多跑一个回合。
func markThreadTurn(sessionID, owner string) {
	if db.Redis == nil || sessionID == "" || owner == "" {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), threadTurnRedisTMO)
	defer cancel()
	if err := db.Redis.Do(ctx, "ZADD", threadTurnsKey(sessionID), threadTurnDeadline(), owner).Err(); err != nil {
		slog.Warn("thread turn marker: mark failed", "session", sessionID, "error", err)
	}
}

// clearThreadTurn 注销一个投递回合。
//
// 收尾链上必须保证它被执行：漏掉就等于把会话钉在"有回合在跑"上，
// 队列要等到 threadTurnOwnerTTL（6 分钟）之后才恢复。
func clearThreadTurn(sessionID, owner string) {
	if db.Redis == nil || sessionID == "" || owner == "" {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), threadTurnRedisTMO)
	defer cancel()
	if err := db.Redis.Do(ctx, "ZREM", threadTurnsKey(sessionID), owner).Err(); err != nil {
		slog.Warn("thread turn marker: clear failed", "session", sessionID, "error", err)
	}
}

// threadTurnsInFlight 返回该会话当前仍在跑的投递回合 owner 列表（顺手剔掉过期的）。
//
// 返回 error 表示"读不到"，与"确实是空的"必须分开：前者续跑不推进（保守），
// 只有后者才是真正可以去领队列的信号。
func threadTurnsInFlight(ctx context.Context, sessionID string) ([]string, error) {
	if db.Redis == nil {
		return nil, errRedisUnavailable
	}
	key := threadTurnsKey(sessionID)
	now := strconv.FormatInt(time.Now().Unix(), 10)
	// 过期的成员先剔掉再看剩余：崩溃遗留的登记不该永久挡住队列。
	if err := db.Redis.Do(ctx, "ZREMRANGEBYSCORE", key, "-inf", now).Err(); err != nil {
		return nil, err
	}
	// 左开区间 `(now`：正好等于 now 的已在上一句被剔掉，这里不该再看见。
	owners, err := db.Redis.Do(ctx, "ZRANGEBYSCORE", key, "("+now, "+inf").StringSlice()
	if err != nil {
		return nil, err
	}
	return owners, nil
}

// startThreadTurnHeartbeat 为一个投递回合续期登记，返回**幂等**的停止函数。
//
// 必须与 run-lock 心跳分开：那个维持"锁还归我"，这个维持"我还在跑"。
// Redis 抖动**不停止**是在学 run-lock 心跳：一次抖动就放弃续期会把整个会话的队列卡住。
func startThreadTurnHeartbeat(ctx context.Context, sessionID, owner string) func() {
	if db.Redis == nil || sessionID == "" || owner == "" {
		return func() {}
	}
	stop := make(chan struct{})
	var stopOnce sync.Once
	go func() {
		ticker := time.NewTicker(threadTurnRenewEvery)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-stop:
				return
			case <-ticker.C:
				hctx, cancel := context.WithTimeout(context.Background(), threadTurnRedisTMO)
				err := db.Redis.Do(hctx, "ZADD", threadTurnsKey(sessionID), threadTurnDeadline(), owner).Err()
				cancel()
				if err != nil {
					slog.Warn("thread turn marker: renew failed (retrying next tick)",
						"session", sessionID, "error", err)
				}
			}
		}
	}()
	return func() {
		stopOnce.Do(func() { close(stop) })
	}
}
