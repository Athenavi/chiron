package api

import (
	"fmt"
	"net/http"
	"strings"

	"github.com/athenavi/chiron/internal/db"
)

// 执行审计的**集中摄取**（N4）：引擎与独立沙箱服务把各自的 `exec_audit` 记录送到网关，
// 统一落到同一条审计流（`db.AuditLog` → Redis Stream `audit:events`）。
//
// 为什么需要它：多副本部署里每个引擎实例（以及 S5e 的沙箱服务）各有自己的落盘目录 ——
// 排障时"只看得见本机那份"。集中摄取让审计回到一处可查（也是 S5e-2 的验收依赖）。
//
// 端点：POST /v1/internal/audit/exec（内部令牌；由引擎/沙箱服务调用）
//
//	请求：{"records": [{tenant_id, user_id, session_id, tool, command, outcome,
//	                    exit_code, reason, duration_ms, ts, instance}]}
//	响应：{"accepted": N}
//
// 语义：**逐条校验、部分接受** —— 缺 `tool` 或 `outcome` 的记录跳过并计入 `skipped`，
// 而不是整批 400（一条坏记录不该让整批审计丢失）。空批次 / 超过上限才是 400。
const (
	execAuditMaxBatch        = 500
	execAuditCommandMaxChars = 500
)

type execAuditRecord struct {
	TenantID   string `json:"tenant_id"`
	UserID     string `json:"user_id"`
	SessionID  string `json:"session_id"`
	Tool       string `json:"tool"`
	Command    string `json:"command"`
	Outcome    string `json:"outcome"`
	ExitCode   *int   `json:"exit_code,omitempty"`
	Reason     string `json:"reason,omitempty"`
	DurationMS int    `json:"duration_ms,omitempty"`
	TS         string `json:"ts,omitempty"`
	Instance   string `json:"instance,omitempty"`
}

type execAuditBatch struct {
	Records []execAuditRecord `json:"records"`
}

// ExecAuditIngestHandler 是执行审计集中摄取入口。
func ExecAuditIngestHandler(w http.ResponseWriter, r *http.Request) {
	var body execAuditBatch
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, "invalid request")
		return
	}
	if len(body.Records) == 0 {
		BadRequest(w, "records is required")
		return
	}
	if len(body.Records) > execAuditMaxBatch {
		BadRequest(w, fmt.Sprintf("too many records in one batch (max %d)", execAuditMaxBatch))
		return
	}

	accepted, skipped := 0, 0
	for _, rec := range body.Records {
		tool := strings.TrimSpace(rec.Tool)
		outcome := strings.TrimSpace(rec.Outcome)
		if tool == "" || outcome == "" {
			skipped++
			continue
		}
		detail := "outcome=" + outcome
		if rec.ExitCode != nil {
			detail += fmt.Sprintf(" exit=%d", *rec.ExitCode)
		}
		if rec.DurationMS > 0 {
			detail += fmt.Sprintf(" %dms", rec.DurationMS)
		}
		if rec.Reason != "" {
			detail += " reason=" + truncateRunes(rec.Reason, 200)
		}
		if cmd := strings.TrimSpace(rec.Command); cmd != "" {
			detail += " cmd=" + truncateRunes(cmd, execAuditCommandMaxChars)
		}
		meta := map[string]interface{}{
			"tool":       tool,
			"outcome":    outcome,
			"session_id": rec.SessionID,
			"instance":   rec.Instance,
			"source":     "engine.exec_audit",
		}
		if rec.TS != "" {
			meta["source_ts"] = rec.TS
		}
		if rec.ExitCode != nil {
			meta["exit_code"] = *rec.ExitCode
		}
		db.AuditLog(r.Context(), rec.UserID, rec.TenantID, "tool.exec", "tool:"+tool, detail, "", meta)
		accepted++
	}

	OK(w, map[string]any{"accepted": accepted, "skipped": skipped})
}

// truncateRunes 按**字符**截断（不是字节）：命令里常见多字节字符，按字节截会切出乱码。
func truncateRunes(s string, limit int) string {
	runes := []rune(s)
	if len(runes) <= limit {
		return s
	}
	return string(runes[:limit]) + "…"
}
