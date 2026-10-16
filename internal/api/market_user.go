package api

import (
	"context"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/id"
)

// UserMarketHandler 面向用户的三大市场（技能/Agent/MCP）浏览与一键安装。
// 管理端发布与租户授权仍走 market_handler.go（ent_catalog_items / ent_catalog_installs）。
type UserMarketHandler struct {
	cfg          *config.Config
	pythonClient *engine.PythonClient
}

func NewUserMarketHandler(cfg *config.Config, pythonClient *engine.PythonClient) *UserMarketHandler {
	return &UserMarketHandler{cfg: cfg, pythonClient: pythonClient}
}

// marketItemSummary 用户可见的市场条目（已发布 + 租户授权 fail-open）。
type marketItemSummary struct {
	ID        string          `json:"id"`
	Type      string          `json:"type"` // skill / agent / mcp
	Name      string          `json:"name"`
	Version   string          `json:"version"`
	Status    string          `json:"status"`
	Manifest  json.RawMessage `json:"manifest"`
	Installed bool            `json:"installed"`
}

// List 浏览市场：GET /v1/market?type=skill|agent|mcp
func (h *UserMarketHandler) List(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID == "" {
		Unauthorized(w, "missing tenant context")
		return
	}
	itemType := strings.TrimSpace(r.URL.Query().Get("type"))
	if itemType == "" {
		BadRequest(w, "type is required (skill|agent|mcp)")
		return
	}
	if !validMarketItemTypes[itemType] {
		BadRequest(w, "invalid market type")
		return
	}

	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT id::text, type, name, version, manifest, status FROM ent_catalog_items
		 WHERE type = $1 AND status = 'published' ORDER BY created_at DESC`, itemType)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "market list failed")
		return
	}
	defer rows.Close()

	items := make([]marketItemSummary, 0, 8)
	for rows.Next() {
		var it marketItemSummary
		var manifest []byte
		if err := rows.Scan(&it.ID, &it.Type, &it.Name, &it.Version, &manifest, &it.Status); err != nil {
			continue
		}
		it.Manifest = json.RawMessage(manifest)
		// 租户授权 fail-open：有授权记录则必须 enabled；无记录放行
		if enabled, err := itemEnabledForTenant(r.Context(), it.ID, claims.TenantID); err == nil {
			it.Installed = enabled
		}
		items = append(items, it)
	}
	OK(w, map[string]interface{}{"items": items})
}

// itemEnabledForTenant 查询租户安装记录（fail-open：无记录视为未安装）。
func itemEnabledForTenant(ctx context.Context, itemID, tenantID string) (bool, error) {
	var enabled bool
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COALESCE(enabled, false) FROM ent_catalog_installs WHERE item_id = $1 AND tenant_id = $2`,
		itemID, tenantID).Scan(&enabled)
	if err != nil {
		return false, err
	}
	return enabled, nil
}

// Install 一键安装：POST /v1/market/{type}/{itemID}/install
func (h *UserMarketHandler) Install(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID == "" {
		Unauthorized(w, "missing tenant context")
		return
	}
	itemType := r.PathValue("type")
	itemID := r.PathValue("itemID")
	if !validMarketItemTypes[itemType] || itemID == "" {
		BadRequest(w, "invalid type or item id")
		return
	}

	var name string
	var manifest []byte
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT name, manifest FROM ent_catalog_items WHERE id = $1 AND type = $2 AND status = 'published'`,
		itemID, itemType).Scan(&name, &manifest); err != nil {
		NotFound(w, "market item not found or not published")
		return
	}

	var m map[string]interface{}
	if err := json.Unmarshal(manifest, &m); err != nil {
		InternalError(w, "invalid market manifest")
		return
	}

	switch itemType {
	case "skill":
		h.installSkill(w, r, claims, m)
	case "agent":
		h.installAgent(w, r, claims, m)
	case "mcp":
		h.installMCP(w, r, claims, m)
	default:
		BadRequest(w, "unsupported market type")
	}
}

// installSkill 技能安装：将 manifest 定义写入 Python SkillStore（用户可于对话/Agent 中调用）。
func (h *UserMarketHandler) installSkill(w http.ResponseWriter, r *http.Request, claims *auth.Claims, m map[string]interface{}) {
	if h.pythonClient == nil {
		InternalError(w, "python engine not available")
		return
	}
	skillName, _ := m["name"].(string)
	if skillName == "" {
		BadRequest(w, "manifest missing skill name")
		return
	}
	inline := map[string]interface{}{
		"name":        skillName,
		"description": m["description"],
		"exec":        m["exec"],
		"parameters":  m["parameters"],
	}
	body := map[string]interface{}{"inline": mustJSON(inline)}
	target := "/v1/skills/install?user_id=" + claims.UserID + "&tenant_id=" + claims.TenantID
	var resp map[string]interface{}
	if err := h.pythonClient.PostJSON(r.Context(), target, body, &resp); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "skill install failed")
		return
	}
	OK(w, map[string]interface{}{"success": true, "type": "skill", "name": skillName, "detail": resp})
}

// installAgent Agent 安装：manifest 快照复制为当前用户的私有 Agent（严格私有）。
func (h *UserMarketHandler) installAgent(w http.ResponseWriter, r *http.Request, claims *auth.Claims, m map[string]interface{}) {
	agentID, err := id.UUID()
	if err != nil {
		InternalError(w, "generate id failed")
		return
	}
	name, _ := m["name"].(string)
	if name == "" {
		BadRequest(w, "manifest missing agent name")
		return
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
		logAndRespond(w, err, http.StatusInternalServerError, "agent install failed")
		return
	}
	OK(w, map[string]interface{}{"success": true, "type": "agent", "id": agentID, "name": name})
}

// installMCP MCP 安装：将 manifest 的 MCP server 配置追加到当前用户 plugins.json（命令需命中白名单）。
func (h *UserMarketHandler) installMCP(w http.ResponseWriter, r *http.Request, claims *auth.Claims, m map[string]interface{}) {
	pName, _ := m["name"].(string)
	command, _ := m["command"].(string)
	if pName == "" || command == "" {
		BadRequest(w, "manifest missing mcp name/command")
		return
	}
	// 命令**形态**必须合法；运维白名单（若配置了）是可选上界。
	// 二者都不因"未配置白名单"而拒绝 —— 市场插件落地后默认**未授权**，执行时另走用户级闸。
	if err := validatePluginCommandShape(command); err != nil {
		Forbidden(w, "plugin command not allowed")
		return
	}
	if err := checkOperatorAllowlist(command); err != nil {
		Forbidden(w, "plugin command not allowed")
		return
	}
	plugin := MCPPlugin{
		Name:    pName,
		Command: command,
		Args:    toStringSlice(m["args"]),
		Env:     toStringMap(m["env"]),
		Status:  "active",
		Source:  "market",
	}
	dir := h.cfg.PluginDataDir
	if dir == "" {
		dir = filepath.Join(config.GetDefaultDataDir(), "plugins")
	}
	path := userPluginPath(dir, claims.UserID)
	if err := appendPlugin(dir, claims.UserID, plugin); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "mcp install failed")
		return
	}
	_ = path
	OK(w, map[string]interface{}{"success": true, "type": "mcp", "name": pName})
}

// ── 小工具 ──

func mustJSON(v interface{}) string {
	b, _ := json.Marshal(v)
	return string(b)
}

func intValAny(v interface{}, def int) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	case json.Number:
		if i, err := n.Int64(); err == nil {
			return int(i)
		}
	}
	return def
}

func toStringSlice(v interface{}) []string {
	arr, ok := v.([]interface{})
	if !ok {
		return nil
	}
	out := make([]string, 0, len(arr))
	for _, e := range arr {
		if s, ok := e.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

func toStringMap(v interface{}) map[string]string {
	obj, ok := v.(map[string]interface{})
	if !ok {
		return nil
	}
	out := make(map[string]string, len(obj))
	for k, e := range obj {
		if s, ok := e.(string); ok {
			out[k] = s
		}
	}
	return out
}

// userPluginPath 返回用户插件配置路径（与 plugin_handler.go 同规则）。
func userPluginPath(dataDir, userID string) string {
	return dataDir + "/" + userID + "/plugins.json"
}

// appendPlugin 读取用户 plugins.json 并追加 MCP 插件（幂等：同名覆盖）。
// 保留 file 级字段（commands_enabled），只替换 mcp_servers。
func appendPlugin(dataDir, userID string, plugin MCPPlugin) error {
	path := userPluginPath(dataDir, userID)
	pf := pluginsFile{MCPServers: []MCPPlugin{}}
	if data, err := os.ReadFile(path); err == nil && len(data) > 0 {
		var existing pluginsFile
		if err := json.Unmarshal(data, &existing); err == nil {
			pf = existing
		}
	} else if err != nil && !os.IsNotExist(err) {
		return err
	}
	if pf.MCPServers == nil {
		pf.MCPServers = []MCPPlugin{}
	}
	replaced := false
	for i := range pf.MCPServers {
		if pf.MCPServers[i].Name == plugin.Name {
			pf.MCPServers[i] = plugin
			replaced = true
			break
		}
	}
	if !replaced {
		pf.MCPServers = append(pf.MCPServers, plugin)
	}
	return writePluginsFile(path, pf)
}

func writePluginsFile(path string, pf pluginsFile) error {
	if pf.MCPServers == nil {
		pf.MCPServers = []MCPPlugin{}
	}
	data, err := json.MarshalIndent(pf, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	return os.WriteFile(path, data, 0o644)
}
