package engine

import (
	"context"
	"encoding/json"
	"sync"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

// ── session → 引擎实例归属（批次 4）──
//
// 背景：agent 进行中 run 的现场状态（AgentRuntime/工具审批/持久终端）在引擎进程内，
// 网关此前只能用「session 一致性哈希」尽力路由（见 python_client.addressFor）。哈希在
// 扩缩容/副本上下线后会漂移，审批与取消可能落到错误实例，用户只得到含糊的
// "no active agent for this session"。
//
// 引擎现在把归属写进 Redis（python-engine/app/run_registry.py）：
//
//	{REDIS_KEY_PREFIX}engine:run:{session_id} = {"instance_id","run_token","owner_uid","url",...}
//	TTL 300s + 100s 心跳续期。
//
// 网关据此：(1) 路由时优先直连归属实例；(2) 审批转发时取出 run_token 注入请求体，
// 供引擎校验「审批属于当前 run 而非陈旧 run」。映射缺失/实例不可用一律回退哈希。
//
// Redis 客户端用包级注入（StartEngineDiscovery 设置）：每个网关进程只持有一个
// PythonClient，这样不必给 PythonClient 增加字段与 import。

const (
	runRecordPrefix   = "engine:run:"
	instanceKeyPrefix = "engine:instance:"
	// subagentRunPrefix 是「子 Agent 作业 → 实例」的归属键前缀：后台 run 的任务活在持有它的
	// 引擎进程里，网关据此判断"这个作业是否真的有人在跑"（写入端见
	// python-engine/app/subagent/affinity.py）。
	subagentRunPrefix = "subagent:run:"
	// runLookupTimeout 归属查询超时：只在审批/取消等显式要求归属的请求上调用
	// （WithRunAffinity），必须远小于请求预算——Redis 抖动时快速回退哈希（A1）。
	runLookupTimeout = 250 * time.Millisecond
)

var (
	affinityRedisMu sync.RWMutex
	affinityRedis   db.RedisClient
)

// SetAffinityRedis 注入读取 session 归属映射用的 Redis 客户端（nil = 不可用）。
func SetAffinityRedis(rdb db.RedisClient) {
	affinityRedisMu.Lock()
	defer affinityRedisMu.Unlock()
	affinityRedis = rdb
}

func affinityRedisClient() db.RedisClient {
	affinityRedisMu.RLock()
	defer affinityRedisMu.RUnlock()
	return affinityRedis
}

// runRecord 与 python-engine/app/run_registry.py 写入的 JSON 对应。
type runRecord struct {
	InstanceID string `json:"instance_id"`
	RunToken   string `json:"run_token"`
	OwnerUID   string `json:"owner_uid,omitempty"`
	URL        string `json:"url,omitempty"`
}

// RunOwner 读取某 session 当前 run 的归属记录。
// 无映射（未运行/TTL 过期）、Redis 不可用或解析失败时返回 ok=false，调用方应回退。
func RunOwner(ctx context.Context, sessionID string) (runRecord, bool) {
	var rec runRecord
	if sessionID == "" {
		return rec, false
	}
	rdb := affinityRedisClient()
	if rdb == nil {
		return rec, false
	}
	lctx, cancel := context.WithTimeout(ctx, runLookupTimeout)
	defer cancel()
	data, err := rdb.Get(lctx, db.RedisKey(runRecordPrefix)+sessionID).Bytes()
	if err != nil {
		return rec, false // 无映射 / TTL 过期 / Redis 抖动：调用方回退
	}
	if err := json.Unmarshal(data, &rec); err != nil || rec.InstanceID == "" {
		return rec, false
	}
	return rec, true
}

// RunOwnerURL 返回归属实例的地址：优先用归属记录里的 url，其次按 instance_id 查引擎注册表。
func RunOwnerURL(ctx context.Context, sessionID string) (string, runRecord, bool) {
	rec, ok := RunOwner(ctx, sessionID)
	if !ok {
		return "", rec, false
	}
	if rec.URL != "" {
		return rec.URL, rec, true
	}
	url := instanceURL(ctx, rec.InstanceID)
	if url == "" {
		return "", rec, false
	}
	return url, rec, true
}

// instanceURL 从引擎注册表（engine:instance:{id}，批 E1）读取某实例的对外地址。
func instanceURL(ctx context.Context, instanceID string) string {
	rdb := affinityRedisClient()
	if rdb == nil || instanceID == "" {
		return ""
	}
	lctx, cancel := context.WithTimeout(ctx, runLookupTimeout)
	defer cancel()
	data, err := rdb.Get(lctx, db.RedisKey(instanceKeyPrefix)+instanceID).Bytes()
	if err != nil {
		return ""
	}
	var rec engineInstanceRecord
	if err := json.Unmarshal(data, &rec); err != nil {
		return ""
	}
	return rec.URL
}

// SubagentOwner 是子 Agent run 的归属记录（字段名与引擎侧
// python-engine/app/subagent/affinity.py 写入的 JSON 逐字一致）。
type SubagentOwner struct {
	InstanceID string `json:"instance_id"`
	URL        string `json:"url,omitempty"`
	Token      string `json:"token,omitempty"`
	RunID      string `json:"run_id,omitempty"`
	SessionID  string `json:"session_id,omitempty"`
	TenantID   string `json:"tenant_id,omitempty"`
}

// SubagentRunOwner 读取某子 Agent run 的归属：谁（哪个实例）正在跑它。
//
// 无映射（run 已收尾注销/TTL 过期）、Redis 不可用或解析失败一律 ok=false ——
// 调用方必须把"没有归属"当成一个**明确结论**（无人认领），而不是当成查询失败，
// 否则又会退回"取消永远返回已受理"的老问题。
func SubagentRunOwner(ctx context.Context, runID string) (SubagentOwner, bool) {
	var rec SubagentOwner
	if runID == "" {
		return rec, false
	}
	rdb := affinityRedisClient()
	if rdb == nil {
		return rec, false
	}
	lctx, cancel := context.WithTimeout(ctx, runLookupTimeout)
	defer cancel()
	data, err := rdb.Get(lctx, db.RedisKey(subagentRunPrefix)+runID).Bytes()
	if err != nil {
		return rec, false
	}
	if err := json.Unmarshal(data, &rec); err != nil || rec.InstanceID == "" {
		return rec, false
	}
	return rec, true
}

// SubagentRunOwnerURL 返回持有该 run 的实例地址（归属记录里的 url 优先，
// 其次按 instance_id 查引擎注册表）。第二个返回值为是否找到归属。
func SubagentRunOwnerURL(ctx context.Context, runID string) (string, SubagentOwner, bool) {
	rec, ok := SubagentRunOwner(ctx, runID)
	if !ok {
		return "", rec, false
	}
	if rec.URL != "" {
		return rec.URL, rec, true
	}
	url := instanceURL(ctx, rec.InstanceID)
	if url == "" {
		return "", rec, false
	}
	return url, rec, true
}

// isKnownHealthy 判断 url 是否在当前地址表中且未处于熔断冷却期。// 归属实例不在地址表内（已下线/注册表回退静态）时返回 false，路由回退哈希。
func (c *PythonClient) isKnownHealthy(url string) bool {
	if url == "" {
		return false
	}
	now := time.Now().Unix()
	for _, e := range c.snapshot() {
		if e.url == url {
			return e.cooldownUntil <= now
		}
	}
	return false
}
