package api

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/id"
)

// AgentHandler 管理自定义 Agent（DB agents 表）+ 运行会话（agent_sessions）。
// 执行链路：Run 落 session(pending) → 异步调 Python /v1/agents/dispatch
// （Python 用 SubAgent 真执行）→ 结果回写 session(completed/failed)。
type AgentHandler struct {
	authenticator *auth.Authenticator
	pythonClient  *engine.PythonClient
	sem           *SharedSemaphore // 并发执行上限（与 /submit 的 agentSem 同源；Redis 共享计数）
}

func NewAgentHandler(a *auth.Authenticator, pc *engine.PythonClient, sem *SharedSemaphore) *AgentHandler {
	h := &AgentHandler{authenticator: a, pythonClient: pc, sem: sem}
	go func() {
		defer func() {
			if r := recover(); r != nil {
				slog.Warn("seed preset agents failed", "panic", r)
			}
		}()
		h.seedPresetAgents()
	}()
	return h
}

// Agent 是自定义 Agent 的 DB 表示。
type Agent struct {
	ID             string          `json:"id"`
	Name           string          `json:"name"`
	Description    string          `json:"description,omitempty"`
	SystemPrompt   string          `json:"system_prompt,omitempty"`
	Tools          json.RawMessage `json:"tools,omitempty"`
	LLMConfig      json.RawMessage `json:"llm_config,omitempty"`
	MaxTurns       int             `json:"max_turns"`
	TimeoutSeconds int             `json:"timeout_seconds"`
	Enabled        bool            `json:"enabled"`
	// 工作台绑定（可空 = 不绑定，保持既有行为）：
	// KbID 是默认知识库，派发时用于 RAG 检索；Skills 是技能名数组，派发时只启用这些技能；
	// Plugins 是 MCP server 名数组，派发时只放这些插件提供的工具；
	// Workflows 是工作流 id 数组，派发时按选择顺序执行（多选即流水线）。
	KbID      string          `json:"kb_id,omitempty"`
	Skills    json.RawMessage `json:"skills,omitempty"`
	Plugins   json.RawMessage `json:"plugins,omitempty"`
	Workflows json.RawMessage `json:"workflows,omitempty"`
	CreatedAt time.Time       `json:"created_at"`
	UpdatedAt time.Time       `json:"updated_at"`
}

// AgentSession 是一次 Agent 运行的持久化记录。
type AgentSession struct {
	ID        string    `json:"id"`
	AgentID   string    `json:"agent_id,omitempty"`
	AgentName string    `json:"agent_name,omitempty"`
	Task      string    `json:"task"`
	Status    string    `json:"status"` // pending / running / completed / failed
	Result    string    `json:"result,omitempty"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

// ── 预置 Agent 播种（DB agents 表为空时插入内置 3 类） ──

type presetAgent struct {
	Name        string           `json:"name"`
	Description string           `json:"description"`
	Prompt      string           `json:"prompt"`
	Tools       []map[string]any `json:"tools"`
	LLM         map[string]any   `json:"llm"`
	Turns       int              `json:"turns"`
}

// loadPresetAgents 从 configs/preset_agents.json 加载预置 Agent 定义。
// 文件不存在时返回空列表（不播种任何预置 Agent）。
func loadPresetAgents() []presetAgent {
	candidates := []string{
		"configs/preset_agents.json",
		"/etc/chiron/preset_agents.json",
	}
	for _, path := range candidates {
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		var presets []presetAgent
		if err := json.Unmarshal(data, &presets); err != nil {
			slog.Warn("parse preset agents config failed", "path", path, "error", err)
			return nil
		}
		slog.Info("loaded preset agents from config", "path", path, "count", len(presets))
		return presets
	}
	slog.Warn("preset agents config not found — no preset agents will be seeded")
	return nil
}

func (h *AgentHandler) seedPresetAgents() {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	ownerTenantID, err := h.resolveOwnerTenantID(ctx)
	if err != nil {
		slog.Warn("seed preset agents: resolve owner tenant failed", "error", err)
		return
	}

	var n int
	if err := db.GlobalDBManager.QueryRow(ctx, `SELECT COUNT(*) FROM agents WHERE tenant_id = $1 AND (kind IS NULL OR kind = 'chat')`, ownerTenantID).Scan(&n); err != nil || n > 0 {
		return
	}
	var ownerUserID string
	if err := db.GlobalDBManager.QueryRow(ctx, `SELECT id::text FROM users WHERE tenant_id = $1 AND role = 'owner' ORDER BY created_at LIMIT 1`, ownerTenantID).Scan(&ownerUserID); err != nil || ownerUserID == "" {
		slog.Warn("seed preset agents: no owner user", "tenant", ownerTenantID)
		return
	}

	presets := loadPresetAgents()
	if len(presets) == 0 {
		return
	}

	for _, p := range presets {
		agentID, err := id.UUID()
		if err != nil {
			slog.Warn("seed preset agent: generate id", "name", p.Name, "error", err)
			continue
		}
		toolsJSON, err := json.Marshal(p.Tools)
		if err != nil {
			slog.Warn("seed preset agent: marshal tools", "name", p.Name, "error", err)
			continue
		}
		llmJSON, err := json.Marshal(p.LLM)
		if err != nil {
			slog.Warn("seed preset agent: marshal llm", "name", p.Name, "error", err)
			continue
		}
		if _, err := db.GlobalDBManager.Exec(ctx,
			`INSERT INTO agents (id, tenant_id, user_id, name, description, system_prompt, tools, llm_config, max_turns, timeout_seconds, enabled)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, true)`,
			agentID, ownerTenantID, ownerUserID, p.Name, p.Description, p.Prompt,
			string(toolsJSON), string(llmJSON), p.Turns, 120); err != nil {
			slog.Warn("seed preset agent", "name", p.Name, "error", err)
		}
	}
}

// ── CRUD ──────────────────────────────────────────────────────

// List 返回当前租户的全部 Agent（按创建时间倒序）。
func (h *AgentHandler) List(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	rows, err := db.GlobalDBManager.Query(r.Context(),
		// 列类型必须逐列对齐：tools / llm_config 是 **json** 列（配 '[]'::json），
		// skills / plugins / workflows 是 **jsonb** 列（配 '[]'::jsonb）。json 与 jsonb
		// 之间两个方向都没有隐式转换，COALESCE 统一类型时会直接报
		// "could not convert type json to jsonb"（或反向）。编译期和单测都看不出来。
		`SELECT id::text, name, COALESCE(description,''), COALESCE(system_prompt,''), COALESCE(tools,'[]'::json), COALESCE(llm_config,'{}'::json), max_turns, timeout_seconds, enabled, COALESCE(kb_id,''), COALESCE(skills,'[]'::jsonb), COALESCE(plugins,'[]'::jsonb), COALESCE(workflows,'[]'::jsonb), created_at, updated_at
		 FROM agents WHERE tenant_id = $1 AND user_id = $2
		   AND (kind IS NULL OR kind = 'chat')  -- 子 Agent Profile（kind='subagent'）不进入对话 Agent 列表
		 ORDER BY created_at DESC`, claims.TenantID, claims.UserID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list agents failed")
		return
	}
	defer rows.Close()

	agents := make([]Agent, 0, 8)
	for rows.Next() {
		var a Agent
		if err := rows.Scan(&a.ID, &a.Name, &a.Description, &a.SystemPrompt, &a.Tools, &a.LLMConfig,
			&a.MaxTurns, &a.TimeoutSeconds, &a.Enabled, &a.KbID, &a.Skills, &a.Plugins, &a.Workflows, &a.CreatedAt, &a.UpdatedAt); err != nil {
			slog.Warn("scan agent", "error", err)
			continue
		}
		agents = append(agents, a)
	}
	OK(w, agents)
}

// Create 新建一个 Agent（绑定当前租户）。
func (h *AgentHandler) Create(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	var body Agent
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, "invalid request")
		return
	}
	body.Name = trimSpace(body.Name)
	if body.Name == "" {
		BadRequest(w, "name is required")
		return
	}

	toolsJSON := body.Tools
	if len(toolsJSON) == 0 || string(toolsJSON) == "null" {
		toolsJSON = json.RawMessage("[]")
	}
	llmJSON := body.LLMConfig
	if len(llmJSON) == 0 || string(llmJSON) == "null" {
		llmJSON = json.RawMessage("{}")
	}
	if body.MaxTurns <= 0 {
		body.MaxTurns = 5
	}
	if body.TimeoutSeconds <= 0 {
		body.TimeoutSeconds = 120
	}

	id, err := id.UUID()
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "generate id failed")
		return
	}
	_, err = db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO agents (id, tenant_id, user_id, name, description, system_prompt, tools, llm_config, max_turns, timeout_seconds, enabled, kb_id, skills, plugins, workflows)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)`,
		id, claims.TenantID, claims.UserID, body.Name, body.Description, body.SystemPrompt,
		string(toolsJSON), string(llmJSON), body.MaxTurns, body.TimeoutSeconds, body.Enabled,
		body.KbID, jsonOrEmptyArray(body.Skills), jsonOrEmptyArray(body.Plugins), jsonOrEmptyArray(body.Workflows))
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create agent failed")
		return
	}
	body.ID = id
	body.CreatedAt = time.Now()
	body.UpdatedAt = time.Now()
	OK(w, body)
}

// Get 返回单个 Agent（必须归属当前租户）。
func (h *AgentHandler) Get(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	agentID := r.PathValue("id")
	if agentID == "" {
		BadRequest(w, "id is required")
		return
	}
	a, err := h.queryAgent(r.Context(), claims.TenantID, claims.UserID, agentID)
	if err != nil {
		NotFound(w, "agent not found")
		return
	}
	OK(w, a)
}

// Update 更新 Agent 字段（name/description/system_prompt/tools/llm_config/max_turns/timeout_seconds/enabled）。
func (h *AgentHandler) Update(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	agentID := r.PathValue("id")
	if agentID == "" {
		BadRequest(w, "id is required")
		return
	}
	// P1 修复：改用指针字段按需更新——原实现 description/system_prompt 成对覆盖
	// （只传其一清空另一个），且 enabled 无条件写入（不传即被重置为 false）。
	var body struct {
		Name           *string         `json:"name"`
		Description    *string         `json:"description"`
		SystemPrompt   *string         `json:"system_prompt"`
		Tools          json.RawMessage `json:"tools"`
		LLMConfig      json.RawMessage `json:"llm_config"`
		MaxTurns       *int            `json:"max_turns"`
		TimeoutSeconds *int            `json:"timeout_seconds"`
		Enabled        *bool           `json:"enabled"`
		KbID           *string         `json:"kb_id"`
		Skills         json.RawMessage `json:"skills"`
		Plugins        json.RawMessage `json:"plugins"`
		Workflows      json.RawMessage `json:"workflows"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}

	// 动态 SET：非零字段才更新（避免把空值当“清除”）
	sets := []string{}
	args := []any{}
	push := func(expr string, v any) {
		sets = append(sets, expr)
		args = append(args, v)
	}
	if body.Name != nil {
		push("name = $"+itoa(len(args)+1), *body.Name)
	}
	if body.Description != nil {
		push("description = $"+itoa(len(args)+1), *body.Description)
	}
	if body.SystemPrompt != nil {
		push("system_prompt = $"+itoa(len(args)+1), *body.SystemPrompt)
	}
	if len(body.Tools) > 0 && string(body.Tools) != "null" {
		push("tools = $"+itoa(len(args)+1), string(body.Tools))
	}
	if len(body.LLMConfig) > 0 && string(body.LLMConfig) != "null" {
		push("llm_config = $"+itoa(len(args)+1), string(body.LLMConfig))
	}
	if body.MaxTurns != nil {
		push("max_turns = $"+itoa(len(args)+1), *body.MaxTurns)
	}
	if body.TimeoutSeconds != nil {
		push("timeout_seconds = $"+itoa(len(args)+1), *body.TimeoutSeconds)
	}
	if body.Enabled != nil {
		push("enabled = $"+itoa(len(args)+1), *body.Enabled)
	}
	if body.KbID != nil {
		push("kb_id = $"+itoa(len(args)+1), *body.KbID)
	}
	if len(body.Skills) > 0 && string(body.Skills) != "null" {
		push("skills = $"+itoa(len(args)+1), string(body.Skills))
	}
	if len(body.Plugins) > 0 && string(body.Plugins) != "null" {
		push("plugins = $"+itoa(len(args)+1), string(body.Plugins))
	}
	if len(body.Workflows) > 0 && string(body.Workflows) != "null" {
		push("workflows = $"+itoa(len(args)+1), string(body.Workflows))
	}
	// WHERE tenant_id = $N+1 AND id = $N+2 —— 双重校验防跨租户
	args = append(args, claims.TenantID, agentID)

	if len(sets) == 0 {
		BadRequest(w, "nothing to update")
		return
	}
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`UPDATE agents SET `+joinComma(sets)+`, updated_at = NOW() WHERE tenant_id = $`+itoa(len(args)-1)+` AND id = $`+itoa(len(args))+" AND user_id = $"+itoa(len(args)+1), append(args, claims.UserID)...); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "update agent failed")
		return
	}
	a, err := h.queryAgent(r.Context(), claims.TenantID, claims.UserID, agentID)
	if err != nil {
		NotFound(w, "agent not found")
		return
	}
	OK(w, a)
}

// Delete 删除 Agent 及其运行记录（仅当归属当前租户）。
func (h *AgentHandler) Delete(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	agentID := r.PathValue("id")
	if agentID == "" {
		BadRequest(w, "id is required")
		return
	}
	if _, err := db.GlobalDBManager.Exec(r.Context(), `DELETE FROM agents WHERE tenant_id = $1 AND id = $2 AND user_id = $3`, claims.TenantID, agentID, claims.UserID); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "delete agent failed")
		return
	}
	OK(w, map[string]string{"status": "deleted"})
}

// ── 运行与会话 ────────────────────────────────────────────────

// Run 派发任务给 Agent：落 session(pending) 后异步执行，结果回写。
func (h *AgentHandler) Run(w http.ResponseWriter, r *http.Request) {
	agentID := r.PathValue("id")
	if agentID == "" {
		BadRequest(w, "id is required")
		return
	}
	claims := auth.GetClaims(r.Context())
	var body struct {
		Task string `json:"task"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	body.Task = trimSpace(body.Task)
	if body.Task == "" {
		BadRequest(w, "task is required")
		return
	}

	agent, err := h.queryAgent(r.Context(), claims.TenantID, claims.UserID, agentID)
	if err != nil {
		NotFound(w, "agent not found")
		return
	}
	if !agent.Enabled {
		BadRequest(w, "agent is disabled")
		return
	}

	sessionID, err := id.UUID()
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "generate id failed")
		return
	}
	now := time.Now()
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO agent_sessions (id, tenant_id, user_id, agent_id, name, task, status, created_at, updated_at)
		 VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7, $7)`,
		sessionID, claims.TenantID, claims.UserID, agent.ID, agent.Name, body.Task, now); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create session failed")
		return
	}

	timeout := time.Duration(agent.TimeoutSeconds) * time.Second
	if timeout <= 0 {
		timeout = DefaultAgentTimeout
	}
	// P1 修复：执行前获取并发信号量，防止无上限并发打爆引擎
	var releaseAgent func()
	if h.sem != nil {
		releaseAgent, _ = h.sem.Acquire(r.Context())
	}
	go func() {
		defer func() {
			if r := recover(); r != nil {
				slog.Error("agent execution panic", "session", sessionID, "agent", agentID, "panic", r)
				// Mark session as failed on panic
				updateCtx, updateCancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer updateCancel()
				_, _ = db.GlobalDBManager.Exec(updateCtx,
					`UPDATE agent_sessions SET status = 'failed', result = $1, updated_at = NOW() WHERE id = $2`,
					fmt.Sprintf(`{"error":"agent execution panicked: %v"}`, r), sessionID)
			}
			if releaseAgent != nil {
				releaseAgent()
			}
		}()
		h.executeAgent(agent, body.Task, sessionID, claims.UserID, claims.TenantID, timeout)
	}()

	OK(w, AgentSession{
		ID:        sessionID,
		AgentID:   agent.ID,
		AgentName: agent.Name,
		Task:      body.Task,
		Status:    "pending",
		CreatedAt: now,
		UpdatedAt: now,
	})
}

func (h *AgentHandler) executeAgent(agent *Agent, task, sessionID, userID, tenantID string, timeout time.Duration) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()

	_, _ = db.GlobalDBManager.Exec(ctx, `UPDATE agent_sessions SET status = 'running', updated_at = NOW() WHERE id = $1`, sessionID)

	// tools/llm_config 转 map 传给 Python（tools 保持 []map 结构）
	var tools []map[string]any
	if len(agent.Tools) > 0 && string(agent.Tools) != "[]" {
		_ = json.Unmarshal(agent.Tools, &tools)
	}
	var llm map[string]any
	if len(agent.LLMConfig) > 0 && string(agent.LLMConfig) != "{}" {
		_ = json.Unmarshal(agent.LLMConfig, &llm)
	}

	body := map[string]any{
		"task":          task,
		"name":          agent.Name,
		"description":   agent.Description,
		"system_prompt": agent.SystemPrompt,
		"tools":         tools,
		"model":         llmString(llm, "model", "deepseek-chat"),
		"max_turns":     agent.MaxTurns,
		"max_tokens":    llmInt(llm, "max_tokens", 4096),
		"temperature":   llmFloat(llm, "temperature", 0.6),
		"tenant_id":     tenantID, // S 多租户隔离:用 JWT claims 的 TenantID,不能用 userID
		"user_id":       userID,
		"session_id":    sessionID,
		// 工作台绑定：Agent 自带的知识库与技能（引擎侧用于 RAG 检索与技能筛选）
		"kb_id":  agent.KbID,
		"skills": agentSkillNames(agent.Skills),
	}

	var result any
	if h.pythonClient == nil {
		result = map[string]any{"success": false, "error": "python engine not available", "output": ""}
	} else if err := h.pythonClient.PostJSON(ctx, "/v1/agents/dispatch", body, &result); err != nil {
		result = map[string]any{"success": false, "error": err.Error(), "output": ""}
	}

	resultJSON, _ := json.Marshal(result)
	status := "completed"
	if m, ok := result.(map[string]any); ok && !boolOf(m["success"]) {
		status = "failed"
	}
	_, _ = db.GlobalDBManager.Exec(ctx,
		`UPDATE agent_sessions SET status = $1, result = $2, updated_at = NOW() WHERE id = $3`,
		status, string(resultJSON), sessionID)
}

// ListSessions 返回当前用户在当前租户下的运行记录（倒序）。
// SetVisibility 设置 Agent 共享可见性（仅 owner）：PUT /v1/agents/{id}/visibility
func (h *AgentHandler) SetVisibility(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID == "" {
		Unauthorized(w, "missing tenant context")
		return
	}
	var body struct {
		Visibility string `json:"visibility"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.Visibility != "private" && body.Visibility != "tenant" {
		BadRequest(w, "visibility must be private or tenant")
		return
	}
	// owner-only：更新必须命中 user_id
	tag, err := db.GlobalDBManager.Exec(r.Context(),
		`UPDATE agents SET visibility = $1, updated_at = NOW() WHERE id = $2 AND tenant_id = $3 AND user_id = $4`,
		body.Visibility, r.PathValue("id"), claims.TenantID, claims.UserID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "update visibility failed")
		return
	}
	if tag.RowsAffected() == 0 {
		Forbidden(w, "agent not found or not owned by you")
		return
	}
	OK(w, map[string]interface{}{"id": r.PathValue("id"), "visibility": body.Visibility})
}

func (h *AgentHandler) ListSessions(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT s.id, COALESCE(s.agent_id::text,''), COALESCE(a.name,''), s.task, s.status, COALESCE(s.result,''), s.created_at, s.updated_at
		 FROM agent_sessions s LEFT JOIN agents a ON a.id = s.agent_id
		 WHERE s.user_id = $1 AND s.tenant_id = $2 ORDER BY s.created_at DESC LIMIT 100`, claims.UserID, claims.TenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list sessions failed")
		return
	}
	defer rows.Close()

	sessions := make([]AgentSession, 0, 16)
	for rows.Next() {
		var s AgentSession
		if err := rows.Scan(&s.ID, &s.AgentID, &s.AgentName, &s.Task, &s.Status, &s.Result, &s.CreatedAt, &s.UpdatedAt); err != nil {
			slog.Warn("scan agent session", "error", err)
			continue
		}
		sessions = append(sessions, s)
	}
	OK(w, sessions)
}

// GetSession 返回单个运行记录（归属校验）。
func (h *AgentHandler) GetSession(w http.ResponseWriter, r *http.Request) {
	sessionID := r.PathValue("id")
	if sessionID == "" {
		BadRequest(w, "id is required")
		return
	}
	claims := auth.GetClaims(r.Context())
	var s AgentSession
	err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT s.id, COALESCE(s.agent_id::text,''), COALESCE(a.name,''), s.task, s.status, COALESCE(s.result,''), s.created_at, s.updated_at
		 FROM agent_sessions s LEFT JOIN agents a ON a.id = s.agent_id
		 WHERE s.id = $1 AND s.user_id = $2 AND s.tenant_id = $3`, sessionID, claims.UserID, claims.TenantID).
		Scan(&s.ID, &s.AgentID, &s.AgentName, &s.Task, &s.Status, &s.Result, &s.CreatedAt, &s.UpdatedAt)
	if err != nil {
		NotFound(w, "session not found")
		return
	}
	OK(w, s)
}

// ── helpers ───────────────────────────────────────────────────

// queryAgent 按 id 取 Agent（含租户归属与 tenant 共享可见性校验）。
func (h *AgentHandler) queryAgent(ctx context.Context, tenantID, userID, agentID string) (*Agent, error) {
	return loadAgent(ctx, tenantID, userID, agentID)
}

// loadAgent 是 queryAgent 的包级形态：网关的 /v1/agent/submit 需要在
// "只带 agent_id、没带 agent 配置"时补全（见 gateway_router.go 的 submitHandlerFunc），
// 而那里只有 db、拿不到 AgentHandler 实例。共用同一查询才能保证
// 工作台页与直达 API 走同一套可见性规则，而不是两套。
func loadAgent(ctx context.Context, tenantID, userID, agentID string) (*Agent, error) {
	var a Agent
	err := db.GlobalDBManager.QueryRow(ctx,
		// 列类型必须对齐：tools / llm_config 是 **json** 列，缺省值要用 '[]'::json /
		// '{}'::json；skills / plugins / workflows 是 **jsonb** 列，用 '[]'::jsonb。
		// 写混了 COALESCE 会在运行时报 "could not convert type jsonb to json" ——
		// 编译期、go vet 和单元测试都发现不了，只有真连库跑这条 SQL 才暴露。
		`SELECT id::text, name, COALESCE(description,''), COALESCE(system_prompt,''), COALESCE(tools,'[]'::json), COALESCE(llm_config,'{}'::json), max_turns, timeout_seconds, enabled, COALESCE(kb_id,''), COALESCE(skills,'[]'::jsonb), COALESCE(plugins,'[]'::jsonb), COALESCE(workflows,'[]'::jsonb), created_at, updated_at
		 FROM agents WHERE tenant_id = $1 AND id = $2 AND (user_id = $3 OR (visibility = 'tenant' AND tenant_id = $1))
		   AND (kind IS NULL OR kind = 'chat')  -- 子 Agent Profile 不可作为对话 Agent 运行`, tenantID, agentID, userID).
		Scan(&a.ID, &a.Name, &a.Description, &a.SystemPrompt, &a.Tools, &a.LLMConfig,
			&a.MaxTurns, &a.TimeoutSeconds, &a.Enabled, &a.KbID, &a.Skills, &a.Plugins, &a.Workflows, &a.CreatedAt, &a.UpdatedAt)
	if err != nil {
		return nil, err
	}
	return &a, nil
}

// resolveOwnerTenantID 查询系统首个 owner 角色用户的 tenant_id，用作预置 Agent 的归属租户。
// 多租户场景下预置 Agent 仅在 owner 租户播种一次（其它租户需自行通过 API 创建）。
func (h *AgentHandler) resolveOwnerTenantID(ctx context.Context) (string, error) {
	var tenantID string
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT tenant_id FROM users WHERE role = 'owner' ORDER BY created_at LIMIT 1`).Scan(&tenantID)
	if err != nil {
		return "", fmt.Errorf("no owner found: %w", err)
	}
	return tenantID, nil
}

// ── 通用小工具（字符串/数值拼接与 llm_config 取值） ──

func trimSpace(s string) string { return strings.TrimSpace(s) }

func itoa(n int) string { return strconv.Itoa(n) }

// jsonOrEmptyArray 把可能为空的 JSON 写进 JSONB 列：空值写 '[]'，
// 因为 JSONB 不接受空字符串（""::jsonb 会直接报错）。
func jsonOrEmptyArray(raw json.RawMessage) string {
	if len(raw) == 0 || string(raw) == "null" {
		return "[]"
	}
	return string(raw)
}

// agentSkillNames 解析 Agent 绑定的技能名（JSONB 字符串数组）；
// 未绑定或格式不对时返回 nil，等价于"不筛选技能"（引擎侧沿用全部已安装技能）。
func agentSkillNames(skills json.RawMessage) []string {
	if len(skills) == 0 {
		return nil
	}
	var names []string
	if err := json.Unmarshal(skills, &names); err != nil {
		return nil
	}
	return names
}

// ── 工作台互通：Agent 配置 → 引擎 context ──

// agentContextPayload 把 Agent 组装成透传给引擎的 context.agent。
//
// 字段名必须与 python 侧读取处一致（app/api/unified_executor.py 的 _execute_via_agent、
// app/main.py 的 workbench_context 消费）。此前只带 system_prompt / max_turns / model
// —— 也就是只带人格不带能力：Agent 自带的工具、知识库、技能全部丢失，
// 用户选了 Agent 却发现它"不会用自己的工具"。
func agentContextPayload(a *Agent) map[string]any {
	if a == nil {
		return nil
	}
	payload := map[string]any{}
	if a.ID != "" {
		payload["id"] = a.ID
	}
	if a.Name != "" {
		payload["name"] = a.Name
	}
	if a.SystemPrompt != "" {
		payload["system_prompt"] = a.SystemPrompt
	}
	if a.MaxTurns > 0 {
		payload["max_turns"] = a.MaxTurns
	}
	if model := agentModel(a.LLMConfig); model != "" {
		payload["model"] = model
	}
	if tools := decodeJSONArray(a.Tools); len(tools) > 0 {
		payload["tools"] = tools
	}
	if a.KbID != "" {
		payload["kb_id"] = a.KbID
	}
	if skills := agentSkillNames(a.Skills); len(skills) > 0 {
		payload["skills"] = skills
	}
	if plugins := agentPluginNames(a.Plugins); len(plugins) > 0 {
		payload["plugins"] = plugins
	}
	if workflows := agentWorkflowIDs(a.Workflows); len(workflows) > 0 {
		payload["workflows"] = workflows
	}
	return payload
}

// agentPluginNames 解析 Agent 绑定的插件名（JSONB 字符串数组）；未绑定或格式不对时
// 返回 nil，等价于"不筛选插件"。与 agentSkillNames 同构 —— 两者都是字符串数组列。
func agentPluginNames(plugins json.RawMessage) []string {
	return agentSkillNames(plugins)
}

// agentWorkflowIDs 解析 Agent 绑定的工作流 id（JSONB 字符串数组）；未绑定或格式不对时
// 返回 nil，等价于"不绑定工作流"。与前两者同构 —— 都是字符串数组列。
func agentWorkflowIDs(workflows json.RawMessage) []string {
	return agentSkillNames(workflows)
}

// decodeJSONArray 解析 JSONB 数组列；空值或格式不对时返回 nil（表示"不传该字段"）
func decodeJSONArray(raw json.RawMessage) []any {
	if len(raw) == 0 {
		return nil
	}
	var out []any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil
	}
	return out
}

// agentModel 从 llm_config（JSONB）里取模型名
func agentModel(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var cfg map[string]any
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return ""
	}
	return llmString(cfg, "model", "")
}

// resolveAgentContext 在"只带 agent_id、没带 agent 配置"时补全 context.agent。
//
// 幂等：已有完整 agent 对象时不动。查不到 Agent（不存在/非本人/非共享）时保持原样并
// 记日志 —— 让请求继续走，只是"这次没带 Agent"，与改动前的行为一致；
// 不因为一个坏 agent_id 让整条对话失败。
func resolveAgentContext(ctx context.Context, wbCtx map[string]any, tenantID, userID string) {
	if wbCtx == nil {
		return
	}
	if existing, ok := wbCtx["agent"].(map[string]any); ok && len(existing) > 0 {
		return
	}
	agentID, _ := wbCtx["agent_id"].(string)
	if strings.TrimSpace(agentID) == "" {
		return
	}
	agent, err := loadAgent(ctx, tenantID, userID, agentID)
	if err != nil {
		slog.Warn("workbench: agent context fallback failed", "agent_id", agentID, "error", err)
		return
	}
	if payload := agentContextPayload(agent); payload != nil {
		wbCtx["agent"] = payload
		applyAgentBindings(wbCtx, agent)
		// 多选的其余 Agent → "可委派的专家"。一个对话只能有一个人格（首选的 Agent），
		// 但可以有多个可请教的专家 —— 这正是"多选 Agent"的语义。
		if experts := loadExpertPayloads(ctx, wbCtx, tenantID, userID); len(experts) > 0 {
			payload["experts"] = experts
		}
	}
}

// applyAgentBindings 把 Agent 自带的绑定提升到 workbench_context 顶层。
//
// 引擎读的是**顶层** kb_id / skill_names / plugin_names：
//   - kb_id、skill_names 见 python-engine/app/agent/prompt_engine.py
//   - plugin_names 见 python-engine/app/agent/runtime.py 的 _restrict_tools_to_plugins
//
// 只放在 context["agent"] 里引擎读不到，等于"绑了没生效"。用户本次显式选的值优先，
// Agent 上带的只是默认值（所以非空就不覆盖）。
//
// 从 resolveAgentContext 里抽出来是为了能直接单测：原来内联在那一层，而那一层要先
// 查库（loadAgent），测它就得先起一个带完整 schema 的 PostgreSQL。
func applyAgentBindings(wbCtx map[string]any, agent *Agent) {
	if wbCtx == nil || agent == nil {
		return
	}
	if kb, _ := wbCtx["kb_id"].(string); strings.TrimSpace(kb) == "" && agent.KbID != "" {
		wbCtx["kb_id"] = agent.KbID
	}
	if _, hasSkills := wbCtx["skill_names"]; !hasSkills {
		if skills := agentSkillNames(agent.Skills); len(skills) > 0 {
			wbCtx["skill_names"] = skills
		}
	}
	if _, hasPlugins := wbCtx["plugin_names"]; !hasPlugins {
		if plugins := agentPluginNames(agent.Plugins); len(plugins) > 0 {
			wbCtx["plugin_names"] = plugins
		}
	}
	// workflow_ids 与前三个同族：引擎侧由 selected_workflow_ids 读取（多值优先、
	// 单值回退），随后 _execute_via_workflow 按顺序执行 —— 多选即流水线。
	// 用户本次显式选的工作流优先，Agent 上带的只是默认值。
	if _, hasWorkflows := wbCtx["workflow_ids"]; !hasWorkflows {
		if workflows := agentWorkflowIDs(agent.Workflows); len(workflows) > 0 {
			wbCtx["workflow_ids"] = workflows
		}
	}
}

// loadExpertPayloads 取 agent_ids 里除首个之外的 Agent（主 Agent 之外的"可委派专家"）。
//
// 单个查不到就跳过：不该因为列表里一个坏 id 让其余专家一起丢失。专家只带
// 展示与人格所需字段，不带工具集 —— 委派出去的 child 复用主对话的工具预算。
func loadExpertPayloads(ctx context.Context, wbCtx map[string]any, tenantID, userID string) []map[string]any {
	ids := contextIDList(wbCtx, "agent")
	if len(ids) < 2 {
		return nil
	}
	experts := make([]map[string]any, 0, len(ids)-1)
	for _, id := range ids[1:] {
		agent, err := loadAgent(ctx, tenantID, userID, id)
		if err != nil || agent == nil {
			slog.Warn("workbench: expert agent not loadable", "agent_id", id, "error", err)
			continue
		}
		expert := map[string]any{"id": agent.ID}
		if agent.Name != "" {
			expert["name"] = agent.Name
		}
		if agent.Description != "" {
			expert["description"] = agent.Description
		}
		if agent.SystemPrompt != "" {
			expert["system_prompt"] = agent.SystemPrompt
		}
		experts = append(experts, expert)
	}
	return experts
}

// contextIDList 读多值 <name>_ids，缺失时回退单值 <name>_id。
// 与前端 buildContextQuery、python 侧 context_ids 同一口径（复数优先、单数兼容），
// 否则同一个 URL 在三条链路上会解析出不同结果。
//
// 数组同时接受 []any（请求体经 DecodeJSON 后的常态）与 []string（Go 侧构造
// context 时的手写形态）：只认前者会让后者静默退化成"没传多值"。
func contextIDList(wbCtx map[string]any, name string) []string {
	if raw, ok := wbCtx[name+"_ids"]; ok {
		out := make([]string, 0, 4)
		switch v := raw.(type) {
		case []any:
			for _, item := range v {
				if s, ok := item.(string); ok && strings.TrimSpace(s) != "" {
					out = append(out, strings.TrimSpace(s))
				}
			}
		case []string:
			for _, s := range v {
				if strings.TrimSpace(s) != "" {
					out = append(out, strings.TrimSpace(s))
				}
			}
		}
		if len(out) > 0 {
			return out
		}
	}
	if s, ok := wbCtx[name+"_id"].(string); ok && strings.TrimSpace(s) != "" {
		return []string{strings.TrimSpace(s)}
	}
	return nil
}

func joinComma(items []string) string { return strings.Join(items, ", ") }

func llmString(m map[string]any, key, fallback string) string {
	if v, ok := m[key].(string); ok && v != "" {
		return v
	}
	return fallback
}

func llmInt(m map[string]any, key string, fallback int) int {
	if v, ok := m[key].(float64); ok && v > 0 {
		return int(v)
	}
	return fallback
}

func llmFloat(m map[string]any, key string, fallback float64) float64 {
	if v, ok := m[key].(float64); ok && v > 0 {
		return v
	}
	return fallback
}

func boolOf(v any) bool {
	b, _ := v.(bool)
	return b
}
