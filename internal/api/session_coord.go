package api

import (
	"context"
	"errors"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

// ── Agent 跨实例协调 ─────────────────────────────────────────────
//
// 多网关实例下 agent 任务的"同一 session 防重"与"取消"原依赖进程内
// sessionCancels(仅单实例有效)。这里补齐:
//  1. 同 session Redis 运行锁(agent:run-lock:<sid>,SET NX + TTL),杜绝重复执行;
//  2. 取消经 Redis 广播(agent:cancel),持有该 session 的实例执行真实取消。
// Redis 不可用时均兑底为本地行为并告警(与 SharedSemaphore 一致)。
//
// 批 E2（run 锁可恢复性）：
//   - 锁值改为每次 run 的唯一 token(调用方生成),release 用 Lua compare-and-del
//     (防旧 run 误删新 run 的锁);
//   - TTL 收紧为 5min,持有者通过 RefreshSessionRunLock 每 60s 续期;
//     实例崩溃后锁在 ≤5min 自动过期,同一 session 可重试(历史消息已持久化);
//   - 续期同样校验 token,防止已易主的锁被旧持有者无限续期。

var (
	agentRunLockPrefix = db.RedisKey("agent:run-lock:")
	agentCancelChannel = db.RedisKey("agent:cancel")
)

const (
	agentRunLockTTL = 5 * time.Minute // 崩溃后锁自动过期上限;持有者 60s 心跳续期
)

const sessionRunLockAcquireLua = `
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) then
  return 1
end
return 0
`

const sessionRunLockRefreshLua = `
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
  return 1
end
return 0
`

const sessionRunLockReleaseLua = `
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
`

var errRedisUnavailable = errors.New("redis unavailable")

// AcquireSessionRunLock 为 session 抢占跨实例运行锁（原子 SET NX + TTL）。
// runToken 必须为本次 run 的唯一标识（由调用方生成，如 userID+随机 hex），
// 用于续期与释放时的归属校验，避免旧 run 误删/续期新 run 的锁。
// 返回：release（Redis 获锁时非 nil，任务结束/取消必须调用）、ok=是否持有、
// err!=nil 表示 Redis 不可用——调用方应兑底为进程内 sessionCancels。
func AcquireSessionRunLock(ctx context.Context, sessionID, runToken string) (release func(), ok bool, err error) {
	if db.Redis == nil {
		return nil, false, errRedisUnavailable
	}
	key := agentRunLockPrefix + sessionID
	res := db.Redis.Eval(ctx, sessionRunLockAcquireLua,
		[]string{key}, runToken, int(agentRunLockTTL.Seconds()))
	if resErr := res.Err(); resErr != nil {
		return nil, false, resErr
	}
	n, _ := res.Int()
	if n != 1 {
		return nil, false, nil // 已被其它实例持有（同 session 正在运行）
	}
	var once sync.Once
	return func() {
		once.Do(func() {
			_ = releaseSessionRunLock(context.Background(), key, runToken)
		})
	}, true, nil
}

// RefreshSessionRunLock 续期运行锁 TTL（持有者心跳；值仍为本次 runToken 才续期）。
// 返回是否续期成功（锁已易主或已过期时返回 false）。
func RefreshSessionRunLock(ctx context.Context, sessionID, runToken string) bool {
	if db.Redis == nil {
		return false
	}
	key := agentRunLockPrefix + sessionID
	res := db.Redis.Eval(ctx, sessionRunLockRefreshLua,
		[]string{key}, runToken, int(agentRunLockTTL.Seconds()))
	if resErr := res.Err(); resErr != nil {
		slog.Debug("refresh session run lock failed", "session_id", sessionID, "error", resErr)
		return false
	}
	n, _ := res.Int()
	return n == 1
}

// releaseSessionRunLock compare-and-del 释放（仅当锁仍属于本次 runToken）。
func releaseSessionRunLock(ctx context.Context, key, runToken string) error {
	res := db.Redis.Eval(ctx, sessionRunLockReleaseLua, []string{key}, runToken)
	return res.Err()
}

// CancelSessionBroadcast 把取消请求广播给所有网关实例（本实例未命中时调用）。
// payload: sessionID|userID，接收端校验归属后执行取消。
func CancelSessionBroadcast(ctx context.Context, sessionID, userID string) error {
	if db.Redis == nil {
		return errRedisUnavailable
	}
	payload := sessionID + "|" + userID
	if err := db.Redis.Publish(ctx, agentCancelChannel, payload).Err(); err != nil {
		return err
	}
	// 同一意图也要传达给**子 Agent**：父回合被取消本身不再连带取消它们
	// （见 BroadcastSubagentSessionCancel 的说明），所以"用户显式停止"必须显式发这一份。
	if err := BroadcastSubagentSessionCancel(ctx, sessionID, "parent"); err != nil {
		slog.Warn("subagent session cancel broadcast failed",
			"session_id", sessionID, "error", err)
	}
	slog.Info("session cancel broadcast", "session_id", sessionID)
	return nil
}

// StartAgentCancelSubscriber 启动跨实例取消订阅（main 中调用一次）。
// 收到广播后在本实例 sessionCancels 中查找并校验用户，命中则取消该任务。
func StartAgentCancelSubscriber(ctx context.Context) {
	if db.Redis == nil {
		slog.Warn("agent cancel subscriber disabled: redis unavailable")
		return
	}
	go func() {
		pubsub := db.Redis.Subscribe(ctx, agentCancelChannel)
		defer pubsub.Close()
		ch := pubsub.Channel()
		slog.Info("agent cancel subscriber started")
		for {
			select {
			case <-ctx.Done():
				return
			case msg, ok := <-ch:
				if !ok {
					slog.Warn("agent cancel subscriber channel closed")
					return
				}
				parts := strings.SplitN(msg.Payload, "|", 2)
				if len(parts) != 2 {
					continue
				}
				sessionID, userID := parts[0], parts[1]
				if v, loaded := sessionCancels.LoadAndDelete(sessionID); loaded {
					sc := v.(sessionCancel)
					if sc.userID != userID {
						sessionCancels.Store(sessionID, sc) // 非本人 session，放回
						continue
					}
					sc.cancel()
					slog.Info("session cancelled via cross-instance broadcast",
						"session_id", sessionID)
				}
			}
		}
	}()
}
