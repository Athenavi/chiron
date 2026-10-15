package api

import (
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// SearchHandler provides a unified search endpoint across conversations, files, and media.
type SearchHandler struct{}

func NewSearchHandler() *SearchHandler {
	return &SearchHandler{}
}

// SearchResult holds a single search hit.
type SearchResult struct {
	Type      string  `json:"type"` // message, file, media
	ID        string  `json:"id"`
	Title     string  `json:"title"`
	Snippet   string  `json:"snippet"`
	SessionID string  `json:"session_id,omitempty"`
	Path      string  `json:"path,omitempty"`
	URL       string  `json:"url,omitempty"`
	Score     float64 `json:"score"`
}

func (h *SearchHandler) Search(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query().Get("q")
	if q == "" {
		OK(w, map[string]interface{}{"results": []interface{}{}})
		return
	}
	// P0 性能：限制搜索词长度，防止全文搜索 CPU 耗尽
	if len([]rune(q)) > 200 {
		BadRequest(w, "search query too long (max 200 characters)")
		return
	}
	// 多租户隔离修复（P0-S4）：必须按当前租户+用户过滤，否则任一租户用户可全文搜索全库。
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID == "" {
		Unauthorized(w, "missing tenant context")
		return
	}

	pool := db.ReadPool()
	if pool == nil {
		ServiceUnavailable(w, "database not available")
		return
	}

	var results []SearchResult

	// 收尾2：**可选过滤** —— 会话内（`session_id`）与时间范围（`from` / `to`）。
	//
	// 两者都只作用于"用户自己的数据"（下面 `s.tenant_id = $2 AND s.user_id = $3` 是硬条件），
	// 因此不引入新的越权面。**参数只拼占位符编号（$n），值一律走参数化** ——
	// 用户输入永不进入 SQL 文本。
	sessionFilter := strings.TrimSpace(r.URL.Query().Get("session_id"))
	from, to, terr := parseSearchRange(r.URL.Query().Get("from"), r.URL.Query().Get("to"))
	if terr != nil {
		BadRequest(w, terr.Error())
		return
	}
	msgArgs := []any{q, claims.TenantID, claims.UserID}
	msgConds := ""
	if sessionFilter != "" {
		msgArgs = append(msgArgs, sessionFilter)
		msgConds += fmt.Sprintf(" AND m.session_id = $%d", len(msgArgs))
	}
	if from != nil {
		msgArgs = append(msgArgs, *from)
		msgConds += fmt.Sprintf(" AND m.created_at >= $%d", len(msgArgs))
	}
	if to != nil {
		msgArgs = append(msgArgs, *to)
		msgConds += fmt.Sprintf(" AND m.created_at < $%d", len(msgArgs))
	}

	// 1. Search messages (conversations)
	msgRows, err := pool.Query(r.Context(),
		`SELECT m.id, m.session_id, m.content, s.title,
			ts_rank(to_tsvector('simple', m.content), plainto_tsquery('simple', $1)) as rank
		 FROM messages m
		 LEFT JOIN sessions s ON s.id = m.session_id
		 WHERE to_tsvector('simple', m.content) @@ plainto_tsquery('simple', $1)
		   AND s.tenant_id = $2 AND s.user_id = $3`+msgConds+`
		 ORDER BY rank DESC LIMIT 20`, msgArgs...)
	if err == nil {
		defer msgRows.Close()
		for msgRows.Next() {
			var result SearchResult
			var content, sessionID string
			var title *string
			if err := msgRows.Scan(&result.ID, &sessionID, &content, &title, &result.Score); err != nil {
				continue
			}
			result.Type = "message"
			result.SessionID = sessionID
			result.Snippet = truncateText(content, 150)
			if title != nil {
				result.Title = *title
			} else {
				result.Title = truncateText(content, 60)
			}
			results = append(results, result)
		}
		if err := msgRows.Err(); err != nil {
			slog.Warn("search messages iteration error", "error", err)
		}
	}

	// 2. Search files (from editor file list)
	fileRows, err := pool.Query(r.Context(),
		`SELECT id, name, file_path,
			ts_rank(to_tsvector('simple', COALESCE(name,'')), plainto_tsquery('simple', $1)) as rank
		 FROM media_assets
		 WHERE to_tsvector('simple', COALESCE(name,'')) @@ plainto_tsquery('simple', $1)
		   AND tenant_id = $2 AND user_id = $3
		 ORDER BY rank DESC LIMIT 10`, q, claims.TenantID, claims.UserID)
	if err == nil {
		defer fileRows.Close()
		for fileRows.Next() {
			var result SearchResult
			var name, path string
			if err := fileRows.Scan(&result.ID, &name, &path, &result.Score); err != nil {
				continue
			}
			result.Type = "file"
			result.Title = name
			result.Path = path
			result.Snippet = path
			results = append(results, result)
		}
		if err := fileRows.Err(); err != nil {
			slog.Warn("search files iteration error", "error", err)
		}
	}

	OK(w, map[string]interface{}{
		"query":   q,
		"results": results,
		"count":   len(results),
	})
}

// parseSearchRange 解析搜索的时间范围（收尾2）。
//
// 两个参数都可选。**非法格式一律拒绝（400）而不是静默忽略** —— 静默忽略会让用户以为
// 过滤生效了，却拿到超出范围的旧结果，那比报错更误导。`to` 缺省表示"不限"（不默认取 now，
// 这样"到今天为止"能包含当天全部）。
func parseSearchRange(rawFrom, rawTo string) (from, to *time.Time, err error) {
	parse := func(s string) (*time.Time, error) {
		s = strings.TrimSpace(s)
		if s == "" {
			return nil, nil
		}
		t, perr := time.Parse(time.RFC3339, s)
		if perr != nil {
			return nil, fmt.Errorf("time must be RFC3339, e.g. 2026-10-01T00:00:00Z")
		}
		return &t, nil
	}
	if from, err = parse(rawFrom); err != nil {
		return nil, nil, err
	}
	if to, err = parse(rawTo); err != nil {
		return nil, nil, err
	}
	if from != nil && to != nil && to.Before(*from) {
		return nil, nil, fmt.Errorf("to must be after from")
	}
	return from, to, nil
}

func truncateText(s string, maxLen int) string {
	runes := []rune(s)
	if len(runes) <= maxLen {
		return s
	}
	return string(runes[:maxLen]) + "..."
}
