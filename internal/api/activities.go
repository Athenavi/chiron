package api

import (
	"log/slog"
	"net/http"
	"sort"
	"strconv"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/model"
)

// Activity 聚合最近活动（跨工作台），供前端 WorkstationNav 使用。
//
// Workstation 用 model.WorkstationType 而不是裸 string：六个标识此前散落在 SQL
// 字面量里，新增一台时没有编译期检查（唯一事实源见 shared/workstations.json）。
type Activity struct {
	Workstation model.WorkstationType `json:"workstation"`
	Route       string                `json:"route"`       // 跳转路由
	Title       string                `json:"title"`       // 活动标题
	Status      string                `json:"status"`      // 原始状态
	StatusText  string                `json:"status_text"` // 展示文案
	Timestamp   int64                 `json:"timestamp"`   // Unix 毫秒
}

func activityStatusText(status string) string {
	switch status {
	case "running", "processing", "pending":
		return "进行中"
	case "completed", "done", "active", "success":
		return "已完成"
	case "failed", "error":
		return "失败"
	case "uploading", "building":
		return "处理中"
	default:
		return status
	}
}

// handleActivities 返回当前用户跨工作台的活动（按时间倒序，租户+用户隔离）。
// 优化：通过 UNION ALL 合并查询为单次数据库往返。
//
// 覆盖范围：对话 / Agent / 知识库 / 工作流四类有持久化表可查。技能与插件**没有**
// 对应的表（技能是 python-engine/data/skills 下的文件，插件在 Python 侧 store），
// 因此不产生活动条目 —— 这是数据源缺失，不是漏写；要补得先让它们落库。
func handleActivities(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID == 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	limit := 20
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= 50 {
			limit = n
		}
	}

	type item struct {
		workstation model.WorkstationType
		route       string
		title       string
		status      string
		ts          time.Time
	}
	items := make([]item, 0, limit*4)
	ctx := r.Context()

	// Check if database pool is available
	pool := db.ReadPool()
	if pool == nil {
		ServiceUnavailable(w, "database not available")
		return
	}

	// Single combined query: UNION ALL across all four tables
	// Each sub-query returns (workstation, route, title, status, ts)
	//
	// route 带上资源标识（?session=<id> 或 /knowledge/<kb_id>）：前端点"最近活动"
	// 要能直达那条记录，而不是只落到列表页 —— 静态 '/agents' 让用户还得自己再找一遍。
	//
	// 工作台标识走 $4..$7 参数而非 SQL 字面量：字面量一旦与 model.WorkstationType
	// 漂移，SQL 照样执行、前端拿到未知标识后静默不渲染，是最难查的一类 bug。
	// 参数化后编译器能盯着常量，改动只需动 internal/model/workstation.go。
	const combinedSQL = `
		SELECT workstation, route, title, status, ts FROM (
			SELECT $4::text as workstation, '/agents?session=' || id::text as route, COALESCE(name,'') as title, status, created_at as ts FROM agent_sessions
				WHERE tenant_id = $1 AND user_id = $2 ORDER BY created_at DESC LIMIT $3
		) AS agents_sub
		UNION ALL
		SELECT workstation, route, title, status, ts FROM (
			SELECT $5::text, '/chat?session=' || id::text, COALESCE(title,''), 'active'::text, updated_at FROM sessions
				WHERE tenant_id = $1 AND user_id = $2 ORDER BY updated_at DESC LIMIT $3
		) AS sessions_sub
		UNION ALL
		SELECT workstation, route, title, status, ts FROM (
			SELECT $6::text, '/knowledge/' || knowledge_base_id::text, COALESCE(name,''), status, created_at FROM knowledge_documents
				WHERE tenant_id = $1 AND user_id = $2 ORDER BY created_at DESC LIMIT $3
		) AS knowledge_sub
		UNION ALL
		SELECT workstation, route, title, status, ts FROM (
			SELECT $7::text, '/workflow?id=' || id::text, COALESCE(workflow_name,''), status, created_at FROM workflow_instances
				WHERE user_id = $2 ORDER BY created_at DESC LIMIT $3
		) AS workflow_sub
		ORDER BY 5 DESC
		LIMIT $3
	`
	if rows, err := pool.Query(ctx, combinedSQL,
		claims.TenantID,
		claims.UserID,
		limit*3,
		string(model.WorkstationAgent),
		string(model.WorkstationDialogue),
		string(model.WorkstationKnowledge),
		string(model.WorkstationWorkflow),
	); err == nil {
		for rows.Next() {
			var ws, route, title, status string
			var ts time.Time
			if err := rows.Scan(&ws, &route, &title, &status, &ts); err == nil {
				items = append(items, item{model.WorkstationType(ws), route, title, status, ts})
			}
		}
		if err := rows.Err(); err != nil {
			slog.Warn("activities rows iteration error", "error", err)
		}
		rows.Close()
	}

	sort.Slice(items, func(i, j int) bool { return items[i].ts.After(items[j].ts) })
	if len(items) > limit {
		items = items[:limit]
	}

	out := make([]Activity, 0, len(items))
	for _, it := range items {
		out = append(out, Activity{
			Workstation: it.workstation,
			Route:       it.route,
			Title:       it.title,
			Status:      it.status,
			StatusText:  activityStatusText(it.status),
			Timestamp:   it.ts.UnixMilli(),
		})
	}
	OK(w, map[string]interface{}{"activities": out})
}
