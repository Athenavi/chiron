package api

// ── 会话地图布局 API（Redis 热层 + 异步落 PostgreSQL）──
//
//	GET    /v1/session-map/workspaces               列出当前用户的画布
//	POST   /v1/session-map/workspaces               创建画布
//	GET    /v1/session-map/workspaces/{id}          读取（viewport + nodes）
//	PUT    /v1/session-map/workspaces/{id}          整体保存（viewport + nodes 全量）
//	DELETE /v1/session-map/workspaces/{id}          删除
//	POST   /v1/session-map/workspaces/{id}/fork-node 从某个节点分叉出新会话节点
//
// 为什么是"整体保存"而不是逐字段 PATCH：地图的写入热点是**拖动节点**（高频、小数据），
// 逐字段 PATCH 会把一次拖动拆成几十个请求。整体保存 + 前端防抖（见前端 800ms 策略）
// 只发一次，且天然幂等 —— 重复保存同一份快照结果相同。
//
// 存储分层（与运行期缓存同一套思路）：
//   - **Redis 是热层**：读优先命中它（布局是"看地图的姿势"，丢了不影响数据本身）；
//   - **PostgreSQL 是权威层**：由后台 flusher 异步落库；
//   - dirty 集合放在 Redis 里，多实例用 `SPOP` 原子认领 —— 谁抢到谁落库，
//     天然避免"两个副本同时写同一份快照"，也不需要 leader 选举。
//   - Redis 不可用时**降级为直写 PG**（可用性优先，不丢用户操作）。
//
// 数据本身（会话消息）不在这里：地图节点只是"对会话的引用"，消息走既有的
// `/v1/conversations/{id}`，所以**不需要** dsh-synapse 那套投影层。

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/id"
	"github.com/jackc/pgx/v5"
)

const (
	smCacheTTL    = 24 * time.Hour
	smFlushPeriod = 3 * time.Second
	smMaxNodes    = 2000
)

// SessionMapNode 是地图上的一个节点：对会话的引用 + 布局。
type SessionMapNode struct {
	ID            string   `json:"id"`
	SessionID     string   `json:"session_id,omitempty"`
	ParentNodeID  string   `json:"parent_node_id,omitempty"`
	EdgeKind      string   `json:"edge_kind,omitempty"`
	X             int      `json:"x"`
	Y             int      `json:"y"`
	Title         string   `json:"title,omitempty"`
	Color         string   `json:"color,omitempty"`
	Collapsed     bool     `json:"collapsed,omitempty"`
	Hidden        bool     `json:"hidden,omitempty"`
	Pinned        bool     `json:"pinned,omitempty"`
	BranchFromSeq int      `json:"branch_from_seq,omitempty"`
	CreatedAt     string   `json:"created_at,omitempty"`
	UpdatedAt     string   `json:"updated_at,omitempty"`
	_             struct{} `json:"-"`
}

// SessionMapWorkspace 是一张画布（含它的节点）。
type SessionMapWorkspace struct {
	ID        string                 `json:"id"`
	Name      string                 `json:"name"`
	Viewport  map[string]interface{} `json:"viewport"`
	Nodes     []SessionMapNode       `json:"nodes"`
	CreatedAt string                 `json:"created_at,omitempty"`
	UpdatedAt string                 `json:"updated_at,omitempty"`
	// TenantID / UserID 是画布归属，用于热层命中后的跨租户/跨用户校验（M3 修复）：
	// 热层 key 只有 workspaceID，命中即返回会让任何人凭 wsID 读到他人画布，且
	// SaveWorkspace 可污染他人热层并经 flusher 落库覆盖（smPersist 的 UPDATE 只按 id）。
	// 前端不消费这两个字段。
	TenantID string `json:"tenant_id,omitempty"`
	UserID   string `json:"user_id,omitempty"`
}

type SessionMapHandler struct{}

func NewSessionMapHandler() *SessionMapHandler { return &SessionMapHandler{} }

// ── key 工具（与引擎侧的 rkey 前缀语义一致）──

func smCacheKey(workspaceID string) string { return db.RedisKey("sessionmap:ws:" + workspaceID) }
func smDirtyKey() string                   { return db.RedisKey("sessionmap:dirty") }

// smInflightKey 记录"已被某个实例认领、尚未落库完成"的画布（ZSET，score = 认领时刻）。
//
// 为什么需要它：此前落库是 `SPOP` 之后直接干活 —— 认领即出队，一旦中途出事
// （进程被 OOM/滚动发布杀掉、Redis 抖动导致快照读不到、panic），这条 ID 就从 dirty
// 集合里消失了，而热层里的布局从未写进 PG。用户拖动过的位置就这样**永久丢失**，
// 且没有任何日志能说明"哪张画布没落库"。
//
// 现在认领改为「SPOP + ZADD inflight（原子 Lua）」，落库结束才 ZREM。崩溃遗留的
// inflight 项由下一轮的回收步骤放回 dirty —— 认领仍然是原子的（谁抢到谁写 PG），
// 但不再是"一抢到就再也回不去"。
func smInflightKey() string { return db.RedisKey("sessionmap:inflight") }

// smClaimStaleSecs：inflight 项超过这个时间未归还，视为认领者已死，回收重试。
// 取 5 分钟：正常落库是毫秒级（smFlushPeriod 才 3 秒），这个阈值只用来兜崩溃，
// 又不能太短 —— 太短会让慢查询期间的画布被两个实例同时写。
const smClaimStaleSecs = 300

// smClaimLua 原子地回收过期认领 + 从 dirty 领取一张画布。
//
// 必须是一个脚本：分两条命令做"回收 + 领取"时，两个实例可能同时回收同一条并
// 各自认领，导致同一画布被并发写（虽然有幂等兜底，但会浪费连接并放大写冲突）。
const smClaimLua = `
local stale = redis.call('ZRANGEBYSCORE', KEYS[2], 0, tonumber(ARGV[2]) - tonumber(ARGV[1]), 'LIMIT', 0, 50)
for _, id in ipairs(stale) do
  redis.call('SADD', KEYS[1], id)
  redis.call('ZREM', KEYS[2], id)
end
local id = redis.call('SPOP', KEYS[1])
if not id then
  return false
end
redis.call('ZADD', KEYS[2], ARGV[2], id)
return id
`

// smClaimDirty 领取一张待落库的画布；没有可领取项时返回 ("", false)。
func smClaimDirty(ctx context.Context, now time.Time) (string, bool) {
	res := db.Redis.Eval(ctx, smClaimLua,
		[]string{smDirtyKey(), smInflightKey()},
		smClaimStaleSecs, now.Unix())
	if err := res.Err(); err != nil {
		return "", false
	}
	id, err := res.Text()
	if err != nil || id == "" {
		return "", false
	}
	return id, true
}

// smReleaseLua 原子地归还认领：ZREM inflight，requeue 时同时放回 dirty。
//
// 为什么必须是一个脚本：`ZREM` 与 `SADD` 分两条命令时，进程在两者之间被杀
// （滚动发布、OOM、panic）就会得到"inflight 已清、dirty 没加"——这张画布的
// 布局从此既不在待落库队列里、也不在崩溃回收范围里，**永久丢失**，且没有任何
// 日志能指出是哪一张。而这一对操作的语义本来就是"归还"这一件事，分开做没有意义。
//
// 注意即使退化成非原子，也应当先 SADD 再 ZREM（宁可"既在 dirty 又在 inflight"
// 重复落库 —— smPersist 是事务内全量覆盖，幂等；也不要"两边都没有"丢数据）。
const smReleaseLua = `
if ARGV[2] == '1' then
  redis.call('SADD', KEYS[1], ARGV[1])
end
redis.call('ZREM', KEYS[2], ARGV[1])
return 1
`

// smReleaseClaim 归还认领：requeue=true 表示本次没落库成功，放回 dirty 下一轮重试。
//
// 失败一律吞掉：脚本没跑到时 inflight 项仍在，下一轮的 stale 回收（smClaimStaleSecs）
// 会把它放回 dirty —— 归还失败的最坏结果是"晚 5 分钟重试"，而不是丢数据。
func smReleaseClaim(ctx context.Context, workspaceID string, requeue bool) {
	if db.Redis == nil || workspaceID == "" {
		return
	}
	flag := "0"
	if requeue {
		flag = "1"
	}
	// 与 smClaimDirty 一样用 Eval：Do("EVAL", numkeys, …) 的 numkeys 与键参数错位
	// 是静默失败（脚本照跑，只是 KEYS 变成 ARGV），Eval 由类型系统绑死键列表。
	db.Redis.Eval(ctx, smReleaseLua, []string{smDirtyKey(), smInflightKey()}, workspaceID, flag)
}

func smTenant(claims *auth.Claims) string {
	if claims == nil {
		return ""
	}
	if claims.TenantID != 0 {
		return tenantStr(claims.TenantID)
	}
	return claims.UserID
}

// ── 热层读写 ──

// smCacheGet 读热层快照；"不存在"与"Redis 出错"都按未命中处理（读路径可接受）。
func smCacheGet(ctx context.Context, workspaceID string) (*SessionMapWorkspace, bool) {
	ws, ok, _ := smCacheGetChecked(ctx, workspaceID)
	return ws, ok
}

// smCacheGetChecked 与 smCacheGet 相同，但区分"不存在"与"Redis 出错"。
//
// flush 路径必须区分这两者：key 不存在说明热层数据本身已丢失（没什么可落库），
// 而 Redis 出错只是这一刻读不到 —— 把它当成"不存在"就把用户的操作永久丢掉了。
func smCacheGetChecked(ctx context.Context, workspaceID string) (*SessionMapWorkspace, bool, error) {
	if db.Redis == nil {
		return nil, false, nil
	}
	res := db.Redis.Do(ctx, "GET", smCacheKey(workspaceID))
	if err := res.Err(); err != nil {
		return nil, false, err
	}
	raw, _ := res.Text()
	if raw == "" {
		return nil, false, nil
	}
	var ws SessionMapWorkspace
	if err := json.Unmarshal([]byte(raw), &ws); err != nil {
		return nil, false, nil
	}
	return &ws, true, nil
}

func smCachePut(ctx context.Context, ws *SessionMapWorkspace) {
	if db.Redis == nil {
		return
	}
	payload, err := json.Marshal(ws)
	if err != nil {
		return
	}
	key := smCacheKey(ws.ID)
	if res := db.Redis.Do(ctx, "SET", key, payload, "EX", "86400"); res.Err() != nil {
		return
	}
	// 认领落库任务（多实例用 SPOP 原子取，谁取到谁写 PG）
	db.Redis.Do(ctx, "SADD", smDirtyKey(), ws.ID)
}

// smFlushOnce 把 dirty 集合里的画布落到 PG。返回处理条数。
//
// 认领语义见 smClaimDirty：领取即登记 inflight，落库结束才归还 —— 进程中途被杀
// 也不会让这张画布从此再没人落库。
func smFlushOnce(ctx context.Context) int {
	if db.Redis == nil || db.GlobalDBManager == nil {
		return 0
	}
	handled := 0
	for i := 0; i < 20; i++ { // 每轮最多 20 张，避免长时间占用
		raw, ok := smClaimDirty(ctx, time.Now())
		if !ok {
			break
		}
		ws, found, err := smCacheGetChecked(ctx, raw)
		if err != nil {
			// 只是这一刻读不到：放回 dirty 重试，不能当成"画布不存在"而丢弃。
			slog.Warn("sessionmap flush: cache read failed, will retry", "workspace", raw, "error", err)
			smReleaseClaim(ctx, raw, true)
			continue
		}
		if !found {
			// 热层已过期（24h TTL）或已被删除：没有可落库的快照，归还而不重排，
			// 否则这个 ID 会在 dirty/inflight 之间永久打转。
			smReleaseClaim(ctx, raw, false)
			continue
		}
		if err := smPersist(ctx, ws); err != nil {
			slog.Warn("sessionmap flush failed, will retry", "workspace", raw, "error", err)
			// 放回 dirty 集合，下一轮重试（不丢用户操作）
			smReleaseClaim(ctx, raw, true)
			continue
		}
		smReleaseClaim(ctx, raw, false)
		handled++
	}
	return handled
}

// StartSessionMapFlusher 后台把热层快照落库。main 启动时调用一次。
func StartSessionMapFlusher(ctx context.Context) {
	if db.Redis == nil {
		slog.Info("sessionmap flusher disabled: redis unavailable (writes go straight to PG)")
		return
	}
	go func() {
		ticker := time.NewTicker(smFlushPeriod)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				// 关机前尽量把热层写回，避免"用户拖过的位置丢失"
				runBackgroundCycle("sessionmap-flush-final", func() {
					if n := smFlushOnce(context.WithoutCancel(ctx)); n > 0 {
						slog.Info("sessionmap flusher final pass", "workspaces", n)
					}
				})
				return
			case <-ticker.C:
				// panic 必须拦在**这一轮**：落到 goroutine 顶层会终止整个进程，
				// 而这里处理的是前端传来的任意画布布局，一条坏数据就足以让所有副本
				// 同时退出（见 runBackgroundCycle 注释）。
				runBackgroundCycle("sessionmap-flush", func() { smFlushOnce(ctx) })
			}
		}
	}()
	slog.Info("sessionmap flusher started", "period", smFlushPeriod)
}

// sessionMapReconcilePeriod 兜底对账周期。
//
// 为什么需要"周期"而不能只靠事件：删会话时主动失效热层（invalidateSessionMapWorkspaces）
// 依赖**删除一定经过本进程**。若会话是在别的实例、别的工作台，或直接改库删的，
// 热层收不到通知 —— 周期对账是唯一能兜住这种情况的手段。
// 代价是延迟一个周期，可以接受（用户不会盯着秒级一致性）。
const sessionMapReconcilePeriod = 10 * time.Minute

// StartSessionMapReconciler 周期性地把画布与真实会话对账（删掉"会话已不存在"的节点）。
func StartSessionMapReconciler(ctx context.Context) {
	if db.Redis == nil || db.GlobalDBManager == nil {
		slog.Info("sessionmap reconciler disabled: redis or db unavailable")
		return
	}
	go func() {
		ticker := time.NewTicker(sessionMapReconcilePeriod)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				runBackgroundCycle("sessionmap-reconcile", func() { smReconcileOnce(ctx) })
			}
		}
	}()
	slog.Info("sessionmap reconciler started", "period", sessionMapReconcilePeriod)
}

// smReconcileOnce 一次全量对账：一条 SQL 删掉所有指向不存在会话的节点。
//
// 用 SQL 而不是"逐画布读出再比"：判定与 smPruneMissingSessions 完全一致，
// 但一次扫全表，不受画布数量影响。便签（session_id 为空）天然不在删除范围内。
func smReconcileOnce(ctx context.Context) {
	tag, err := db.GlobalDBManager.Exec(ctx,
		`DELETE FROM session_map_nodes n
		  WHERE n.session_id IS NOT NULL AND n.session_id <> ''
		    AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.id = n.session_id)`)
	if err != nil {
		slog.Warn("sessionmap reconcile: prune failed", "error", err)
		return
	}
	// CommandTag 是值类型（不能与 nil 比较）；err 为 nil 时它一定有效
	removed := int(tag.RowsAffected())
	if removed == 0 {
		return
	}
	slog.Info("sessionmap reconcile: removed stale nodes", "count", removed)
	// PG 已干净，但热层仍持有旧快照 —— 全量失效，让下一次读回源 PG。
	smInvalidateAllWorkspaceCaches(ctx)
}

// smInvalidateAllWorkspaceCaches 失效所有画布的热层。
//
// 画布是"用户 × 若干个"的量级（几十到几百），列 id 逐个 DEL 即可；
// 不用 KEYS（在大 keyspace 上会阻塞 Redis）。
func smInvalidateAllWorkspaceCaches(ctx context.Context) {
	if db.Redis == nil {
		return
	}
	rows, err := db.GlobalDBManager.FetchAll(ctx, `SELECT id FROM session_map_workspaces`)
	if err != nil {
		slog.Warn("sessionmap reconcile: list workspaces failed", "error", err)
		return
	}
	ids := make([]string, 0, len(rows))
	for _, r := range rows {
		if id := stringOf(r["id"]); id != "" {
			ids = append(ids, id)
		}
	}
	invalidateSessionMapWorkspaces(ctx, ids)
}

// ── PG 读写 ──

func smPersist(ctx context.Context, ws *SessionMapWorkspace) error {
	if db.GlobalDBManager == nil {
		// 落库前先剪枝：引用已删会话的节点在 INSERT 时会触发外键违反（session_id → sessions），
		// 导致**整个画布**的落库失败 —— 表现是热层一直有数据、PG 永远为空（本项目实际踩过）。
		// 放在这里而不是只放在读路径：读路径的剪枝只在有人打开地图时触发，而 flusher 是后台跑的。
		ws = smPruneMissingSessions(ctx, ws)
		return nil
	}
	viewport, _ := json.Marshal(ws.Viewport)
	// 必须整体在**一个事务**里：先 DELETE 全部节点、再逐条 INSERT。
	// 此前每条语句各自提交，于是"DELETE 成功、第 7 条 INSERT 失败"时，画布在 PG 里
	// 只剩前 6 个节点 —— 回滚不了、重试也只会从同一个快照再做一次同样的部分删除。
	// 用户看到的是"地图上的节点凭空少了一半"，而日志里只有一行 flush failed。
	// 事务化之后失败即整体回滚，dirty 重试拿到的仍是完整快照。
	return db.GlobalDBManager.WithTransaction(ctx, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx,
			`UPDATE session_map_workspaces SET name = $2, viewport = $3::jsonb, updated_at = NOW()
			  WHERE id = $1`, ws.ID, ws.Name, string(viewport)); err != nil {
			return err
		}
		// 节点全量替换：先删后插，天然处理"节点被移除"的情况。
		// 节点量有上限（smMaxNodes），一次 DELETE + 批量 INSERT 比逐节点 diff 简单且不易出错。
		if _, err := tx.Exec(ctx,
			`DELETE FROM session_map_nodes WHERE workspace_id = $1`, ws.ID); err != nil {
			return err
		}
		for _, n := range ws.Nodes {
			if _, err := tx.Exec(ctx,
				`INSERT INTO session_map_nodes
				   (id, workspace_id, session_id, parent_node_id, edge_kind, x, y, title, color,
				    collapsed, hidden, pinned, branch_from_seq, created_at, updated_at)
				 VALUES ($1,$2,NULLIF($3,''),NULLIF($4,''),COALESCE(NULLIF($5,''),'manual'),$6,$7,
				         NULLIF($8,''),NULLIF($9,''),$10,$11,$12,NULLIF($13,0),NOW(),NOW())
				 ON CONFLICT (id) DO UPDATE SET
				   parent_node_id = EXCLUDED.parent_node_id, edge_kind = EXCLUDED.edge_kind,
				   x = EXCLUDED.x, y = EXCLUDED.y, title = EXCLUDED.title, color = EXCLUDED.color,
				   collapsed = EXCLUDED.collapsed, hidden = EXCLUDED.hidden, pinned = EXCLUDED.pinned,
				   branch_from_seq = EXCLUDED.branch_from_seq, updated_at = NOW()`,
				n.ID, ws.ID, n.SessionID, n.ParentNodeID, n.EdgeKind, n.X, n.Y, n.Title, n.Color,
				n.Collapsed, n.Hidden, n.Pinned, n.BranchFromSeq); err != nil {
				return err
			}
		}
		return nil
	})
}

func smLoadFromPG(ctx context.Context, workspaceID, tenant, userID string) (*SessionMapWorkspace, error) {
	if db.GlobalDBManager == nil {
		return nil, nil
	}
	row, err := db.GlobalDBManager.FetchOne(ctx,
		`SELECT id, COALESCE(name,''), COALESCE(viewport, '{}'::jsonb)::text
		   FROM session_map_workspaces
		  WHERE id = $1 AND tenant_id = $2 AND user_id = $3`, workspaceID, tenant, userID)
	if err != nil || row == nil {
		return nil, err
	}
	ws := &SessionMapWorkspace{ID: stringOf(row["id"]), Name: stringOf(row["name"]), Nodes: []SessionMapNode{}, TenantID: tenant, UserID: userID}
	_ = json.Unmarshal([]byte(stringOf(row["viewport"])), &ws.Viewport)
	if ws.Viewport == nil {
		ws.Viewport = map[string]interface{}{}
	}
	rows, err := db.GlobalDBManager.FetchAll(ctx,
		`SELECT n.id, COALESCE(n.session_id,''), COALESCE(n.parent_node_id,''), COALESCE(n.edge_kind,'manual'),
		        n.x, n.y, COALESCE(NULLIF(n.title, ''), s.title, '') AS title, COALESCE(n.color, ''), n.collapsed, n.hidden, n.pinned,
		        COALESCE(n.branch_from_seq, 0), n.created_at, n.updated_at
		   FROM session_map_nodes n LEFT JOIN sessions s ON s.id = n.session_id
		  WHERE n.workspace_id = $1 ORDER BY n.created_at`, workspaceID)
	if err != nil {
		return ws, nil
	}
	for _, r := range rows {
		ws.Nodes = append(ws.Nodes, SessionMapNode{
			ID: stringOf(r["id"]), SessionID: stringOf(r["session_id"]),
			ParentNodeID: stringOf(r["parent_node_id"]), EdgeKind: stringOf(r["edge_kind"]),
			X: intOf(r["x"]), Y: intOf(r["y"]), Title: stringOf(r["title"]), Color: stringOf(r["color"]),
			Collapsed: boolOf(r["collapsed"]), Hidden: boolOf(r["hidden"]), Pinned: boolOf(r["pinned"]),
			BranchFromSeq: intOf(r["branch_from_seq"]),
		})
	}
	return ws, nil
}

// sessionMapWorkspaceIDsForSession 查出某会话出现在哪些画布上。
//
// 必须在**删除会话之前**调用：`session_map_nodes.session_id` 有 ON DELETE CASCADE，
// 会话一删节点就没了，那时再查什么都查不到。
func sessionMapWorkspaceIDsForSession(ctx context.Context, sessionID string) []string {
	if db.GlobalDBManager == nil || sessionID == "" {
		return nil
	}
	rows, err := db.GlobalDBManager.FetchAll(ctx,
		`SELECT DISTINCT workspace_id FROM session_map_nodes WHERE session_id = $1`, sessionID)
	if err != nil {
		slog.Warn("sessionmap: lookup workspaces for session failed", "session", sessionID, "error", err)
		return nil
	}
	ids := make([]string, 0, len(rows))
	for _, r := range rows {
		if id := stringOf(r["workspace_id"]); id != "" {
			ids = append(ids, id)
		}
	}
	return ids
}

// invalidateSessionMapWorkspaces 失效这些画布的热层快照。
//
// 准则 3（从外部会话列表删会话 → 画布里的引用必须一起消失）要求它：
// PG 侧有 CASCADE 兜底，但 **Redis 热层是跨实例缓存**，不显式失效就会留下幽灵卡片
// （本项目实际踩过：删了会话，地图上还在）。删掉 key 即可 —— 下次读回源 PG，
// 那时 CASCADE 已经生效，画布自然是干净的。
func invalidateSessionMapWorkspaces(ctx context.Context, workspaceIDs []string) {
	if db.Redis == nil {
		return
	}
	for _, id := range workspaceIDs {
		db.Redis.Do(ctx, "DEL", smCacheKey(id))
		db.Redis.Do(ctx, "SREM", smDirtyKey(), id)
		// inflight 也要清：残留项会在下一轮被"回收"放回 dirty，而热层 key 已被删，
		// 于是它每轮都被领取、发现不存在、再归还 —— 空转到 TTL 到期。
		db.Redis.Do(ctx, "ZREM", smInflightKey(), id)
	}
	if len(workspaceIDs) > 0 {
		slog.Info("sessionmap: invalidated workspaces after session delete", "count", len(workspaceIDs))
	}
}

// smPruneMissingSessions 丢掉"所引用的会话已经不存在"的节点。
//
// 为什么需要它：`session_map_nodes.session_id` 有 ON DELETE CASCADE，PG 侧删会话时节点会
// 跟着消失；但 **Redis 热层是跨实例缓存** —— 会话在别的实例、或别的代码路径被删时，
// 热层快照不会自动更新，表现就是"删了会话，地图上那张卡还在"。
// 这里在读路径上做一次校验：宁可多一次轻量查询，也不要让用户看到幽灵卡片。
//
// 失败时原样返回（不是清空）—— 校验查不动不该把用户的地图清掉。
func smPruneMissingSessions(ctx context.Context, ws *SessionMapWorkspace) *SessionMapWorkspace {
	if ws == nil || len(ws.Nodes) == 0 || db.GlobalDBManager == nil {
		return ws
	}
	ids := make([]string, 0, len(ws.Nodes))
	for _, n := range ws.Nodes {
		if n.SessionID != "" {
			ids = append(ids, n.SessionID)
		}
	}
	if len(ids) == 0 {
		return ws
	}
	placeholders := make([]string, len(ids))
	args := make([]interface{}, len(ids))
	for i, id := range ids {
		placeholders[i] = fmt.Sprintf("$%d", i+1)
		args[i] = id
	}
	rows, err := db.GlobalDBManager.FetchAll(ctx,
		"SELECT id FROM sessions WHERE id IN ("+strings.Join(placeholders, ",")+")", args...)
	if err != nil {
		slog.Warn("sessionmap prune: session lookup failed, keeping nodes as-is", "error", err)
		return ws
	}
	alive := make(map[string]bool, len(rows))
	for _, r := range rows {
		alive[stringOf(r["id"])] = true
	}
	kept := make([]SessionMapNode, 0, len(ws.Nodes))
	removed := 0
	for _, n := range ws.Nodes {
		// 便签（无 session_id）永远保留
		if n.SessionID != "" && !alive[n.SessionID] {
			removed++
			continue
		}
		kept = append(kept, n)
	}
	if removed > 0 {
		slog.Info("sessionmap prune: dropped nodes whose session is gone", "workspace", ws.ID, "removed", removed)
		ws.Nodes = kept
		// 热层已经脏了：回写清理后的快照（并把画布标脏，让 flusher 落到 PG）
		smCachePut(ctx, ws)
	}
	return ws
}

// ── HTTP handlers ──

func (h *SessionMapHandler) ListWorkspaces(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	out := []map[string]interface{}{}
	if db.GlobalDBManager != nil {
		rows, err := db.GlobalDBManager.FetchAll(r.Context(),
			`SELECT id, COALESCE(name,''), updated_at FROM session_map_workspaces
			  WHERE tenant_id = $1 AND user_id = $2 ORDER BY updated_at DESC LIMIT 50`,
			smTenant(claims), claims.UserID)
		if err == nil {
			for _, row := range rows {
				out = append(out, map[string]interface{}{
					"id": stringOf(row["id"]), "name": stringOf(row["name"]),
				})
			}
		}
	}
	OK(w, map[string]interface{}{"workspaces": out})
}

func (h *SessionMapHandler) CreateWorkspace(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	if db.GlobalDBManager == nil {
		JSON(w, http.StatusServiceUnavailable, APIResponse{Success: false, Error: "database unavailable"})
		return
	}
	var body struct {
		Name string `json:"name"`
	}
	_ = DecodeJSON(w, r, &body)
	wsID, err := id.UUID()
	if err != nil {
		JSON(w, http.StatusInternalServerError, APIResponse{Success: false, Error: "cannot allocate id"})
		return
	}
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO session_map_workspaces (id, tenant_id, user_id, name, viewport, created_at, updated_at)
		 VALUES ($1, $2, $3, $4, '{}'::jsonb, NOW(), NOW())`,
		wsID, smTenant(claims), claims.UserID, strings.TrimSpace(body.Name)); err != nil {
		slog.Error("create sessionmap workspace failed", "error", err)
		JSON(w, http.StatusInternalServerError, APIResponse{Success: false, Error: "create failed"})
		return
	}
	ws := &SessionMapWorkspace{ID: wsID, Name: body.Name, Viewport: map[string]interface{}{}, Nodes: []SessionMapNode{},
		TenantID: smTenant(claims), UserID: claims.UserID}
	smCachePut(r.Context(), ws)
	JSON(w, http.StatusCreated, APIResponse{Success: true, Data: ws})
}

func (h *SessionMapHandler) GetWorkspace(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	wsID := strings.TrimSpace(r.PathValue("id"))
	if wsID == "" {
		BadRequest(w, "workspace id is required")
		return
	}
	// 热层优先；miss 再回源 PG 并回填（布局是"看地图的姿势"，热层丢了不影响数据）
	// 热层 key 只含 workspaceID，命中后**必须**校验归属，否则任何人凭 wsID 即读到他人画布。
	if ws, ok := smCacheGet(r.Context(), wsID); ok {
		if ws.TenantID == smTenant(claims) && ws.UserID == claims.UserID {
			OK(w, smPruneMissingSessions(r.Context(), ws))
			return
		}
		JSON(w, http.StatusNotFound, APIResponse{Success: false, Error: "workspace not found"})
		return
	}
	ws, err := smLoadFromPG(r.Context(), wsID, smTenant(claims), claims.UserID)
	if err != nil {
		JSON(w, http.StatusInternalServerError, APIResponse{Success: false, Error: "load failed"})
		return
	}
	if ws == nil {
		JSON(w, http.StatusNotFound, APIResponse{Success: false, Error: "workspace not found"})
		return
	}
	smCachePut(r.Context(), ws)
	OK(w, smPruneMissingSessions(r.Context(), ws))
}

func (h *SessionMapHandler) SaveWorkspace(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	wsID := strings.TrimSpace(r.PathValue("id"))
	if wsID == "" {
		BadRequest(w, "workspace id is required")
		return
	}
	var body SessionMapWorkspace
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, "invalid request")
		return
	}
	if len(body.Nodes) > smMaxNodes {
		BadRequest(w, "too many nodes")
		return
	}
	body.ID = wsID
	if body.Viewport == nil {
		body.Viewport = map[string]interface{}{}
	}
	// 归属校验：热层若已有同 ID 画布且不属于当前用户，拒绝覆盖 —— 否则攻击者用他人 wsID
	// 保存即可污染他人热层，并经 flusher 落库覆盖（smPersist 的 UPDATE 只按 id）。
	if existing, ok := smCacheGet(r.Context(), wsID); ok {
		if existing.TenantID != smTenant(claims) || existing.UserID != claims.UserID {
			JSON(w, http.StatusNotFound, APIResponse{Success: false, Error: "workspace not found"})
			return
		}
	}
	body.TenantID = smTenant(claims)
	body.UserID = claims.UserID
	if db.Redis == nil {
		// 降级：没有热层就直写 PG（可用性优先，不丢用户拖动）
		if err := smPersist(r.Context(), &body); err != nil {
			JSON(w, http.StatusInternalServerError, APIResponse{Success: false, Error: "save failed"})
			return
		}
		OK(w, map[string]interface{}{"saved": true, "degraded": true})
		return
	}
	smCachePut(r.Context(), &body)
	OK(w, map[string]interface{}{"saved": true, "nodes": len(body.Nodes)})
}

func (h *SessionMapHandler) DeleteWorkspace(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	wsID := strings.TrimSpace(r.PathValue("id"))
	if wsID == "" || db.GlobalDBManager == nil {
		BadRequest(w, "workspace id is required")
		return
	}
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`DELETE FROM session_map_workspaces WHERE id = $1 AND tenant_id = $2 AND user_id = $3`,
		wsID, smTenant(claims), claims.UserID); err != nil {
		JSON(w, http.StatusInternalServerError, APIResponse{Success: false, Error: "delete failed"})
		return
	}
	if db.Redis != nil {
		db.Redis.Do(r.Context(), "DEL", smCacheKey(wsID))
		db.Redis.Do(r.Context(), "SREM", smDirtyKey(), wsID)
		// inflight 也必须清：与 invalidateSessionMapWorkspaces 同一理由 —— 残留项会在
		// stale 阈值后被回收进 dirty，再被领出来、发现热层已无快照、又归还一次。
		// 少这几条不会丢数据，但会凭空产生一轮无用读写，且日志里看不出是"已删除"。
		db.Redis.Do(r.Context(), "ZREM", smInflightKey(), wsID)
	}
	OK(w, map[string]string{"status": "deleted", "id": wsID})
}

// ForkNode 从某个地图节点分叉出新会话，并把它作为该节点的子节点接回地图。
//
// 这一步把两个世界接上：`sessions.parent_session_id`（真实分支）→
// `session_map_nodes.parent_node_id` + `edge_kind='branch'`（地图上的连线）。
func (h *SessionMapHandler) ForkNode(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	wsID := strings.TrimSpace(r.PathValue("id"))
	if wsID == "" || db.GlobalDBManager == nil {
		BadRequest(w, "workspace id is required")
		return
	}
	var body struct {
		NodeID    string `json:"node_id"`
		FromIndex int    `json:"from_index"`
		Title     string `json:"title"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, "invalid request")
		return
	}
	row, err := db.GlobalDBManager.FetchOne(r.Context(),
		`SELECT COALESCE(session_id,'') AS session_id, x, y FROM session_map_nodes
		  WHERE id = $1 AND workspace_id = $2`, strings.TrimSpace(body.NodeID), wsID)
	if err != nil || row == nil {
		JSON(w, http.StatusNotFound, APIResponse{Success: false, Error: "node not found"})
		return
	}
	srcSession := stringOf(row["session_id"])
	if srcSession == "" {
		BadRequest(w, "node has no session to fork")
		return
	}
	// 分叉点由后端权威给出：它必须和复制逻辑读同一张表 —— `manager_branch.go` 的
	// 复制语句是 `INSERT INTO messages … SELECT … FROM messages`。
	//
	// 前端此前用 `/v1/chat/sessions/{id}/messages` 数条数，而那条路径在引擎侧读的是
	// `unified_sessions` / `unified_messages`，**不是同一套表**。本地实测
	// `messages=32` 而 `unified_messages=0` 时，接口返回 "Session not found"，
	// 前端 `catch` 把分叉点兜底成 1 —— 分叉"成功"但新会话只剩一条消息，
	// 用户看到的就是"点了分叉没反应"。
	//
	// 这里以 `messages` 的**实际行数**为准（同时覆盖 `body.FromIndex` 为 0 / 负数的情况）。
	fromIndex := body.FromIndex
	var total int
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT count(*) FROM messages WHERE session_id = $1`, srcSession).Scan(&total); err == nil && total > 0 {
		fromIndex = total
	}
	if fromIndex <= 0 {
		// 空会话（或 count 查询失败）仍允许分叉：`1` 是后端接受的最小合法值，
		// 复制窗口内一条都没有时就是建一个空分支 —— 与改造前的行为一致。
		fromIndex = 1
	}
	// 真正的复制交给既有端点，这里只负责接回地图（单一职责，避免两处各写一份复制逻辑）
	OK(w, map[string]interface{}{
		"fork_endpoint":  "/v1/conversations/" + srcSession + "/fork",
		"from_index":     fromIndex,
		"parent_node_id": body.NodeID,
		"hint":           "调用 fork 端点后，用 PUT 工作区把新节点接到 parent_node_id 上",
	})
}

// 小工具：stringOf / intOf 在同包的 subagent_handler.go，boolOf 在 agents.go —— 直接复用，
// 不在本文件重复声明（曾因重名导致编译失败）。
