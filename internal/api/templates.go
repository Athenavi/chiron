package api

import (
	"encoding/json"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/id"
)

type TemplateHandler struct {
	pythonClient *engine.PythonClient
}

func NewTemplateHandler(pythonClient *engine.PythonClient) *TemplateHandler {
	return &TemplateHandler{pythonClient: pythonClient}
}

type templateRow struct {
	ID          string          `json:"id"`
	Type        string          `json:"type"`
	Name        string          `json:"name"`
	Description string          `json:"description"`
	Payload     json.RawMessage `json:"payload"`
	CreatedAt   time.Time       `json:"created_at"`
}

func (h *TemplateHandler) RegisterRoutes(mux *http.ServeMux, authMW, rlMW routeMiddleware) {
	mux.Handle("GET /v1/templates", authMW(rlMW(http.HandlerFunc(h.List))))
	mux.Handle("GET /v1/templates/{id}", authMW(rlMW(http.HandlerFunc(h.Get))))
	mux.Handle("POST /v1/templates/{id}/use", authMW(rlMW(http.HandlerFunc(h.Use))))
}

// List GET /v1/templates?type=workflow|agent|skill
func (h *TemplateHandler) List(w http.ResponseWriter, r *http.Request) {
	itemType := strings.TrimSpace(r.URL.Query().Get("type"))
	sql := `SELECT id::text, type, name, description, payload, created_at FROM ent_templates WHERE published = true`
	args := []interface{}{}
	if itemType != "" {
		if itemType != "workflow" && itemType != "agent" && itemType != "skill" {
			BadRequest(w, "type must be workflow|agent|skill")
			return
		}
		sql += ` AND type = $1`
		args = append(args, itemType)
	}
	sql += ` ORDER BY created_at DESC`
	rows, err := db.GlobalDBManager.Query(r.Context(), sql, args...)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list templates failed")
		return
	}
	defer rows.Close()
	out := []templateRow{}
	for rows.Next() {
		var t templateRow
		var payload []byte
		if rows.Scan(&t.ID, &t.Type, &t.Name, &t.Description, &payload, &t.CreatedAt) == nil {
			t.Payload = json.RawMessage(payload)
			out = append(out, t)
		}
	}
	OK(w, map[string]interface{}{"templates": out, "total": len(out)})
}

func (h *TemplateHandler) Get(w http.ResponseWriter, r *http.Request) {
	var t templateRow
	var payload []byte
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT id::text, type, name, description, payload, created_at FROM ent_templates WHERE id = $1 AND published = true`,
		r.PathValue("id")).Scan(&t.ID, &t.Type, &t.Name, &t.Description, &payload, &t.CreatedAt); err != nil {
		NotFound(w, "template not found")
		return
	}
	t.Payload = json.RawMessage(payload)
	OK(w, t)
}

// Use POST /v1/templates/{id}/use 鈥斺€?涓€閿鍒跺埌鑷繁鐨勫伐浣滃彴
func (h *TemplateHandler) Use(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID == 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	var t templateRow
	var payload []byte
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT id::text, type, name, description, payload FROM ent_templates WHERE id = $1 AND published = true`,
		r.PathValue("id")).Scan(&t.ID, &t.Type, &t.Name, &t.Description, &payload); err != nil {
		NotFound(w, "template not found")
		return
	}
	var m map[string]interface{}
	if err := json.Unmarshal(payload, &m); err != nil {
		InternalError(w, "invalid template payload")
		return
	}

	switch t.Type {
	case "agent":
		h.useAgent(w, r, claims, m)
	case "workflow":
		OK(w, map[string]interface{}{"type": "workflow", "payload": m, "name": t.Name, "description": t.Description})
	case "skill":
		h.useSkill(w, r, claims, m)
	default:
		BadRequest(w, "unsupported template type")
	}
}

func (h *TemplateHandler) useAgent(w http.ResponseWriter, r *http.Request, claims *auth.Claims, m map[string]interface{}) {
	agentID, err := id.UUID()
	if err != nil {
		InternalError(w, "generate id failed")
		return
	}
	name, _ := m["name"].(string)
	if name == "" {
		name = "from_template"
	}
	desc, _ := m["description"].(string)
	prompt, _ := m["system_prompt"].(string)
	tools, _ := json.Marshal(m["tools"])
	llm, _ := json.Marshal(m["llm_config"])
	maxTurns := intVal(m, "max_turns", DefaultMaxTurns)
	timeout := int(intVal(m, "timeout_seconds", int(DefaultAgentTimeout.Seconds())))
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO agents (id, tenant_id, user_id, name, description, system_prompt, tools, llm_config, max_turns, timeout_seconds, enabled)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, true)`,
		agentID, claims.TenantID, claims.UserID, name, desc, prompt,
		string(tools), string(llm), maxTurns, timeout); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create agent from template failed")
		return
	}
	OK(w, map[string]interface{}{"success": true, "type": "agent", "id": agentID, "name": name})
}

func (h *TemplateHandler) useSkill(w http.ResponseWriter, r *http.Request, claims *auth.Claims, m map[string]interface{}) {
	if h.pythonClient == nil {
		InternalError(w, "python engine not available")
		return
	}
	body := map[string]interface{}{"inline": mustJSON(m)}
	target := "/v1/skills/install?user_id=" + claims.UserID + "&tenant_id=" + tenantStr(claims.TenantID)
	var resp map[string]interface{}
	if err := h.pythonClient.PostJSON(r.Context(), target, body, &resp); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "install skill from template failed")
		return
	}
	OK(w, map[string]interface{}{"success": true, "type": "skill", "detail": resp})
}
