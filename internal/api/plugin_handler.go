package api

import (
	"bufio"
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/auth"
)









// PluginHandler manages per-user MCP plugin configurations.
// 配置存储：{PluginDataDir}/{user_id}/plugins.json（用户级隔离，S 安全修复：
// 原实现全局单文件，任何登录用户都可读写/修改其他用户的插件配置）。
type PluginHandler struct {
	cfg           *config.Config
	authenticator *auth.Authenticator
	dataDir       string
	mu            sync.Mutex
}

// MCPPlugin represents an MCP server configuration entry.
type MCPPlugin struct {
	Name        string            `json:"name"`
	Command     string            `json:"command"`
	Args        []string          `json:"args,omitempty"`
	Env         map[string]string `json:"env,omitempty"`
	Description string            `json:"description,omitempty"`
	Version     string            `json:"version,omitempty"`
	Status      string            `json:"status"`
	Source      string            `json:"source,omitempty"` // "market" = 市场授权叠加项（非用户本地配置）
	// Approved：**用户级**「每插件一次性授权」——该插件是否已被用户确认可以执行。
	// 默认 false（未授权，不可执行）；改 command 会重置为 false（旧授权不适用于新命令）。
	Approved bool `json:"approved,omitempty"`
}

// pluginsFile is the on-disk structure of plugins.json.
type pluginsFile struct {
	MCPServers []MCPPlugin `json:"mcp_servers"`
	// CommandsEnabled：**用户级总开关**——是否允许执行本用户的插件命令（默认 false = 未授权）。
	// 与运维级的 PLUGIN_COMMAND_ALLOWLIST（**可选上界**）是两层不同的闸，
	// 完整执行闸见 checkPluginExecutionAllowed。
	CommandsEnabled bool `json:"commands_enabled,omitempty"`
}

func NewPluginHandler(cfg *config.Config, authenticator *auth.Authenticator) *PluginHandler {
	dir := cfg.PluginDataDir
	if dir == "" {
		dir = filepath.Join(".", "data", "plugins")
	}
	return &PluginHandler{cfg: cfg, authenticator: authenticator, dataDir: dir}
}

// userPluginPath 返回当前用户的插件配置文件路径。
func (h *PluginHandler) userPluginPath(userID string) string {
	// 安全：清理 userID 防止路径遍历（如 ../tenant/evil）
	safe := filepath.Clean(filepath.Base(userID))
	if safe == "." || safe == "" {
		safe = "unknown"
	}
	return filepath.Join(h.dataDir, safe, "plugins.json")
}

// resolveUser 从请求认证信息取当前用户 ID（authMW 已保证登录）。
func (h *PluginHandler) resolveUser(r *http.Request) string {
	claims := auth.GetClaims(r.Context())
	if claims != nil {
		return claims.UserID
	}
	return ""
}

// ── List ──

func (h *PluginHandler) List(w http.ResponseWriter, r *http.Request) {
	userID := h.resolveUser(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	plugins, err := h.readPlugins(userID)
	if err != nil {
		if os.IsNotExist(err) {
			OK(w, []MCPPlugin{})
			return
		}
		slog.Error("plugin list: read plugins.json", "error", err)
		InternalError(w, "failed to read plugins config")
		return
	}

	for i := range plugins {
		if plugins[i].Status == "" {
			plugins[i].Status = "active"
		}
	}

	// 叠加市场已授权插件（来源标注 market；查询失败静默跳过，不影响本地列表）
	plugins = h.overlayMarketPlugins(r, plugins)
	maskSensitiveEnv(plugins)
	OK(w, plugins)
}

// overlayMarketPlugins 将租户已安装且启用的市场插件追加到列表（去重）。
func (h *PluginHandler) overlayMarketPlugins(r *http.Request, plugins []MCPPlugin) []MCPPlugin {
	items, err := ListEnabledMarketItems(r.Context(), "plugin", tenantStr(ResolveTenantID(r)))
	if err != nil {
		slog.Debug("plugin list: market overlay skipped", "error", err)
		return plugins
	}
	existing := make(map[string]bool, len(plugins))
	for _, p := range plugins {
		existing[p.Name] = true
	}
	for _, it := range items {
		if existing[it.Name] {
			continue
		}
		var manifest struct {
			Command     string            `json:"command"`
			Args        []string          `json:"args"`
			Env         map[string]string `json:"env"`
			Description string            `json:"description"`
		}
		_ = json.Unmarshal(it.Manifest, &manifest)
		plugins = append(plugins, MCPPlugin{
			Name:        it.Name,
			Command:     manifest.Command,
			Args:        manifest.Args,
			Env:         manifest.Env,
			Description: manifest.Description,
			Version:     it.Version,
			Status:      "active",
			Source:      "market",
		})
	}
	return plugins
}

// ── Install ──

func (h *PluginHandler) Install(w http.ResponseWriter, r *http.Request) {
	userID := h.resolveUser(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	name := r.PathValue("name")
	if name == "" {
		BadRequest(w, "plugin name is required")
		return
	}
	if !validPluginName(name) {
		BadRequest(w, "invalid plugin name")
		return
	}

	// 企业市场门控：市场存在同名 published 条目且租户未启用时禁止安装
	// （查询失败 / 未上架能力由 IsItemEnabledForTenant 内部 fail-open 放行）
	if enabled, _ := IsItemEnabledForTenant(r.Context(), "plugin", name, tenantStr(ResolveTenantID(r))); !enabled {
		Forbidden(w, "plugin is not enabled for this tenant by market policy")
		return
	}

	var body struct {
		Command     string            `json:"command"`
		Args        []string          `json:"args,omitempty"`
		Env         map[string]string `json:"env,omitempty"`
		Description string            `json:"description"`
		Version     string            `json:"version"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.Command == "" {
		BadRequest(w, "command is required")
		return
	}
	// 创建**不再**要求命中运维白名单（那是可选上界）：只校验命令**形态**，允许先把插件存
	// 下来；能否执行由**用户级**闸（总开关 + 每插件授权）在真正执行时决定。
	if err := validatePluginCommandShape(body.Command); err != nil {
		Forbidden(w, "plugin command not allowed")
		return
	}
	if err := checkOperatorAllowlist(body.Command); err != nil {
		Forbidden(w, "plugin command not allowed")
		return
	}

	h.mu.Lock()
	defer h.mu.Unlock()

	plugins, err := h.readPlugins(userID)
	if err != nil && !os.IsNotExist(err) {
		slog.Error("plugin install: read plugins.json", "error", err)
		InternalError(w, "failed to read plugins config")
		return
	}
	if plugins == nil {
		plugins = []MCPPlugin{}
	}

	for _, p := range plugins {
		if p.Name == name {
			BadRequest(w, "plugin already installed: "+name)
			return
		}
	}

	plugin := MCPPlugin{
		Name: name, Command: body.Command, Args: body.Args, Env: body.Env,
		Description: body.Description, Version: body.Version, Status: "active",
	}
	plugins = append(plugins, plugin)

	if err := h.writePlugins(userID, plugins); err != nil {
		slog.Error("plugin install: write plugins.json", "error", err)
		InternalError(w, "failed to save plugins config")
		return
	}

	slog.Info("plugin installed", "user", userID, "name", name, "command", body.Command)
	OK(w, plugin)
}

// ── Uninstall ──

func (h *PluginHandler) Uninstall(w http.ResponseWriter, r *http.Request) {
	userID := h.resolveUser(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	name := r.PathValue("name")
	if name == "" {
		BadRequest(w, "plugin name is required")
		return
	}

	h.mu.Lock()
	defer h.mu.Unlock()

	plugins, err := h.readPlugins(userID)
	if err != nil {
		if os.IsNotExist(err) {
			NotFound(w, "plugin not found: "+name)
			return
		}
		slog.Error("plugin uninstall: read plugins.json", "error", err)
		InternalError(w, "failed to read plugins config")
		return
	}

	found := false
	updated := make([]MCPPlugin, 0, len(plugins))
	for _, p := range plugins {
		if p.Name == name {
			found = true
			continue
		}
		updated = append(updated, p)
	}
	if !found {
		NotFound(w, "plugin not found: "+name)
		return
	}

	if err := h.writePlugins(userID, updated); err != nil {
		slog.Error("plugin uninstall: write plugins.json", "error", err)
		InternalError(w, "failed to save plugins config")
		return
	}

	slog.Info("plugin uninstalled", "user", userID, "name", name)
	OK(w, map[string]string{"status": "deleted", "name": name})
}

// ── Update ──

func (h *PluginHandler) Update(w http.ResponseWriter, r *http.Request) {
	userID := h.resolveUser(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	name := r.PathValue("name")
	if name == "" {
		BadRequest(w, "plugin name is required")
		return
	}
	var body struct {
		Command     *string            `json:"command"`
		Args        *[]string          `json:"args,omitempty"`
		Env         *map[string]string `json:"env,omitempty"`
		Description *string            `json:"description,omitempty"`
		Version     *string            `json:"version,omitempty"`
		Status      *string            `json:"status,omitempty"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.Status != nil && *body.Status != "active" && *body.Status != "inactive" {
		BadRequest(w, "status must be active or inactive")
		return
	}
	if body.Command == nil && body.Args == nil && body.Env == nil &&
		body.Description == nil && body.Version == nil && body.Status == nil {
		BadRequest(w, "nothing to update")
		return
	}

	h.mu.Lock()
	defer h.mu.Unlock()

	plugins, err := h.readPlugins(userID)
	if err != nil {
		InternalError(w, "failed to read plugins config")
		return
	}
	updated := false
	for i := range plugins {
		if plugins[i].Name != name {
			continue
		}
		if body.Command != nil {
			if strings.TrimSpace(*body.Command) == "" {
				BadRequest(w, "command must not be empty")
				return
			}
			// 同 Install：只校验形态 + 可选运维上界；未配白名单不再拒绝。
			if err := validatePluginCommandShape(*body.Command); err != nil {
				Forbidden(w, "plugin command not allowed")
				return
			}
			if err := checkOperatorAllowlist(*body.Command); err != nil {
				Forbidden(w, "plugin command not allowed")
				return
			}
			// 命令变了 ⇒ 旧的**一次性授权**不再适用，重置为未授权。
			if plugins[i].Command != *body.Command {
				plugins[i].Approved = false
			}
			plugins[i].Command = *body.Command
		}
		if body.Args != nil {
			plugins[i].Args = *body.Args
		}
		if body.Env != nil {
			plugins[i].Env = *body.Env
		}
		if body.Description != nil {
			plugins[i].Description = *body.Description
		}
		if body.Version != nil {
			plugins[i].Version = *body.Version
		}
		if body.Status != nil {
			plugins[i].Status = *body.Status
		}
		updated = true
		break
	}
	if !updated {
		NotFound(w, "plugin not found: "+name)
		return
	}
	if err := h.writePlugins(userID, plugins); err != nil {
		InternalError(w, "failed to save plugins config")
		return
	}
	for _, p := range plugins {
		if p.Name == name {
			OK(w, p)
			return
		}
	}
	OK(w, map[string]string{"name": name, "updated": "true"})
}

// ── Test ──

func (h *PluginHandler) Test(w http.ResponseWriter, r *http.Request) {
	// 执行**不再**强制 owner/admin —— 授权已下沉到**用户级**（总开关 + 每插件一次性授权）；
	// 完整闸门见下方 checkPluginExecutionAllowed。
	userID := h.resolveUser(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	name := r.PathValue("name")
	if name == "" {
		BadRequest(w, "plugin name is required")
		return
	}

	pf, err := h.readPluginsFile(userID)
	if err != nil {
		InternalError(w, "failed to read plugins config")
		return
	}
	plugins := pf.MCPServers
	var plugin *MCPPlugin
	for i := range plugins {
		if plugins[i].Name == name {
			plugin = &plugins[i]
			break
		}
	}
	if plugin == nil {
		NotFound(w, "plugin not found: "+name)
		return
	}

	start := time.Now()
	ctx, cancel := context.WithTimeout(r.Context(), 8*time.Second)
	defer cancel()

	// 执行闸（deny-by-default）：命令形态 + 运维可选上界 + 用户总开关 + 该插件已授权
	if err := checkPluginExecutionAllowed(*plugin, pf.CommandsEnabled); err != nil {
		OK(w, map[string]interface{}{
			"ok": false, "message": err.Error(),
			"duration_ms": time.Since(start).Milliseconds(),
		})
		return
	}
	// 参数校验：挡控制字符与路径穿越（命令白名单之外的补充，见 checkPluginArgsAllowed）。
	if err := checkPluginArgsAllowed(plugin.Args); err != nil {
		OK(w, map[string]interface{}{
			"ok": false, "message": "plugin args rejected: " + err.Error(),
			"duration_ms": time.Since(start).Milliseconds(),
		})
		return
	}

	cmd := exec.CommandContext(ctx, plugin.Command, plugin.Args...)
	// P0 安全：仅传递安全的环境变量到插件子进程，避免泄露宿主密钥
	cmd.Env = []string{
		"PATH=" + os.Getenv("PATH"),
		"HOME=" + os.Getenv("HOME"),
		"LANG=" + os.Getenv("LANG"),
		"TERM=" + os.Getenv("TERM"),
	}
	// 如果 PATH 为空，设置合理默认值
	if cmd.Env[0] == "PATH=" {
		cmd.Env[0] = "PATH=/usr/local/bin:/usr/bin:/bin"
	}
	for k, v := range plugin.Env {
		cmd.Env = append(cmd.Env, k+"="+v)
	}
	stdin, err := cmd.StdinPipe()
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "stdin pipe failed")
		return
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "stdout pipe failed")
		return
	}
	if err := cmd.Start(); err != nil {
		OK(w, map[string]interface{}{
			"ok": false, "message": "plugin process start failed",
			"duration_ms": time.Since(start).Milliseconds(),
		})
		return
	}
	// cmd.Start 成功后确保清理：context 超时后 exec.CommandContext 会终止进程树
	defer func() {
		_ = cmd.Process.Kill()
		_ = cmd.Wait() // 等待子进程退出，防止僵尸进程
	}()

	req := `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"chiron","version":"1.0"}}}`
	if _, err := stdin.Write([]byte(req + "\n")); err != nil {
		OK(w, map[string]interface{}{
			"ok": false, "message": "write handshake request failed",
			"duration_ms": time.Since(start).Milliseconds(),
		})
		return
	}

	type respLine struct {
		line string
		err  error
	}
	ch := make(chan respLine, 1)
	go func() {
		line, err := bufio.NewReader(stdout).ReadString('\n')
		ch <- respLine{line: line, err: err}
	}()

	select {
	case resp := <-ch:
		ok := resp.err == nil && strings.Contains(resp.line, "jsonrpc")
		msg := "MCP 握手成功"
		if !ok {
			msg = "无有效 MCP initialize 响应" + strings.TrimSpace(resp.line)
			if resp.err != nil {
				msg = "read response failed"
			}
		}
		OK(w, map[string]interface{}{
			"ok": ok, "message": msg,
			"duration_ms": time.Since(start).Milliseconds(),
		})
	case <-ctx.Done():
		OK(w, map[string]interface{}{
			"ok": false, "message": "连接超时（无 MCP initialize 响应）",
			"duration_ms": time.Since(start).Milliseconds(),
		})
	}
}

// ── Internal helpers ──

func (h *PluginHandler) readPluginsFile(userID string) (pluginsFile, error) {
	data, err := os.ReadFile(h.userPluginPath(userID))
	if err != nil {
		return pluginsFile{}, err
	}
	var pf pluginsFile
	if err := json.Unmarshal(data, &pf); err != nil {
		return pluginsFile{}, err
	}
	if pf.MCPServers == nil {
		pf.MCPServers = []MCPPlugin{}
	}
	return pf, nil
}

func (h *PluginHandler) readPlugins(userID string) ([]MCPPlugin, error) {
	pf, err := h.readPluginsFile(userID)
	if err != nil {
		return nil, err
	}
	return pf.MCPServers, nil
}

func (h *PluginHandler) savePluginsFile(userID string, pf pluginsFile) error {
	if pf.MCPServers == nil {
		pf.MCPServers = []MCPPlugin{}
	}
	path := h.userPluginPath(userID)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	data, err := json.MarshalIndent(pf, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, data, 0o644)
}

func (h *PluginHandler) writePlugins(userID string, plugins []MCPPlugin) error {
	// 保留 file 级既有字段（如 commands_enabled），避免"改插件把用户总开关写没"。
	pf, err := h.readPluginsFile(userID)
	if err != nil && !os.IsNotExist(err) {
		pf = pluginsFile{}
	}
	pf.MCPServers = plugins
	return h.savePluginsFile(userID, pf)
}

// ── Authorization（用户级）──

// GetAuthorization GET /v1/plugins/authorization —— 返回用户级总开关状态。
func (h *PluginHandler) GetAuthorization(w http.ResponseWriter, r *http.Request) {
	userID := h.resolveUser(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	pf, err := h.readPluginsFile(userID)
	if err != nil && !os.IsNotExist(err) {
		InternalError(w, "failed to read plugins config")
		return
	}
	OK(w, map[string]bool{"commands_enabled": pf.CommandsEnabled})
}

// SetAuthorization PUT /v1/plugins/authorization —— 设置用户级总开关
// （是否允许执行本用户的插件命令；默认 false）。
func (h *PluginHandler) SetAuthorization(w http.ResponseWriter, r *http.Request) {
	userID := h.resolveUser(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	var body struct {
		CommandsEnabled *bool `json:"commands_enabled"`
	}
	if err := DecodeJSON(w, r, &body); err != nil || body.CommandsEnabled == nil {
		BadRequest(w, "commands_enabled is required")
		return
	}

	h.mu.Lock()
	defer h.mu.Unlock()
	pf, err := h.readPluginsFile(userID)
	if err != nil && !os.IsNotExist(err) {
		InternalError(w, "failed to read plugins config")
		return
	}
	pf.CommandsEnabled = *body.CommandsEnabled
	if err := h.savePluginsFile(userID, pf); err != nil {
		InternalError(w, "failed to save plugins config")
		return
	}
	slog.Info("plugin authorization updated", "user", userID, "commands_enabled", *body.CommandsEnabled)
	OK(w, map[string]bool{"commands_enabled": pf.CommandsEnabled})
}

// Approve POST /v1/plugins/{name}/approve —— 对单个插件做**一次性授权**
// （缺省 approve，显式传 `{"approved": false}` 撤销）。改命令会由 Update 自动重置为未授权。
func (h *PluginHandler) Approve(w http.ResponseWriter, r *http.Request) {
	userID := h.resolveUser(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	name := r.PathValue("name")
	if name == "" {
		BadRequest(w, "plugin name is required")
		return
	}
	approved := true
	if r.ContentLength != 0 {
		var body struct {
			Approved *bool `json:"approved"`
		}
		if err := DecodeJSON(w, r, &body); err == nil && body.Approved != nil {
			approved = *body.Approved
		}
	}

	h.mu.Lock()
	defer h.mu.Unlock()
	pf, err := h.readPluginsFile(userID)
	if err != nil {
		NotFound(w, "plugin not found: "+name)
		return
	}
	found := false
	for i := range pf.MCPServers {
		if pf.MCPServers[i].Name != name {
			continue
		}
		if approved {
			// 授权前先确认命令形态合法（别授权一个根本执行不了的命令）。
			if err := validatePluginCommandShape(pf.MCPServers[i].Command); err != nil {
				BadRequest(w, err.Error())
				return
			}
		}
		pf.MCPServers[i].Approved = approved
		found = true
		break
	}
	if !found {
		NotFound(w, "plugin not found: "+name)
		return
	}
	if err := h.savePluginsFile(userID, pf); err != nil {
		InternalError(w, "failed to save plugins config")
		return
	}
	slog.Info("plugin authorization updated", "user", userID, "name", name, "approved", approved)
	OK(w, map[string]interface{}{"name": name, "approved": approved})
}

var validPluginName = func() func(string) bool {
	re := regexp.MustCompile(`^[a-zA-Z0-9_.-]{1,64}$`)
	return re.MatchString
}()
