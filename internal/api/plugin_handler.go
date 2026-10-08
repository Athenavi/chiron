package api

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
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

// allowedPluginCommands 返回 PLUGIN_COMMAND_ALLOWLIST 环境变量配置的命令白名单
// （以逗号分隔的可执行文件 basename）。空列表 = 默认禁用自定义插件命令
// （安全默认：防止任意登录用户配置任意命令并在网关/引擎主机执行）。
func allowedPluginCommands() map[string]bool {
	raw := strings.TrimSpace(os.Getenv("PLUGIN_COMMAND_ALLOWLIST"))
	if raw == "" {
		return nil
	}
	allowed := make(map[string]bool)
	for _, part := range strings.Split(raw, ",") {
		part = strings.TrimSpace(part)
		if part != "" {
			allowed[part] = true
		}
	}
	return allowed
}

// pluginCommandPathAllowed 只接受**裸 basename**（走 PATH）或**绝对路径**：
// 拒绝相对路径、`..` 段与 UNC。
//
// 为什么不能只比 basename：白名单管的是"哪个**名字**能跑"，而"**从哪跑**"同样要紧 ——
// `../../tmp/npx` 与 `\\evil-host\share\npx` 会让宿主机执行**攻击者放置或远程共享**上的
// 同名二进制，名字却仍然白名单命中。
// ⚠ 与 Python 侧 `python-engine/app/tools/ssrf.py` 的 `_command_path_ok` 是**同一条规则**：
// 改动其一必须同时改另一个（MCP 客户端与 skill 安装走 Python 侧，网关走这里）。
func pluginCommandPathAllowed(command string) bool {
	if strings.TrimSpace(command) == "" {
		return false
	}
	// UNC（\\host\share\…）与协议相对形式（//host/share/…）
	if strings.HasPrefix(command, `\\`) || strings.HasPrefix(command, "//") {
		return false
	}
	parts := strings.FieldsFunc(command, func(r rune) bool { return r == '/' || r == '\\' })
	if len(parts) <= 1 {
		return true // 裸 basename
	}
	for _, p := range parts {
		if p == ".." {
			return false
		}
	}
	if strings.HasPrefix(command, "/") {
		return true // POSIX 绝对路径
	}
	// Windows 绝对路径：X:\… 或 X:/…
	return len(command) >= 3 && command[1] == ':' &&
		(command[2] == '\\' || command[2] == '/') && isASCIILetter(command[0])
}

func isASCIILetter(b byte) bool {
	return (b >= 'a' && b <= 'z') || (b >= 'A' && b <= 'Z')
}

// checkPluginCommandAllowed 校验命令 basename 是否在白名单内。
func checkPluginCommandAllowed(command string) error {
	if strings.TrimSpace(command) == "" {
		return fmt.Errorf("command is required")
	}
	allowed := allowedPluginCommands()
	if allowed == nil {
		return fmt.Errorf("plugin command execution is disabled: set PLUGIN_COMMAND_ALLOWLIST to enable specific commands")
	}
	if !pluginCommandPathAllowed(command) {
		return fmt.Errorf("plugin command %q must be a bare basename or an absolute path "+
			"(relative paths, '..' and UNC are rejected)", command)
	}
	base := filepath.Base(command)
	if !allowed[base] {
		return fmt.Errorf("plugin command %q not in allowlist (PLUGIN_COMMAND_ALLOWLIST)", base)
	}
	return nil
}

// isAdminRole 判断当前请求是否为 owner/admin（插件命令执行敏感操作）。
func isAdminRole(r *http.Request) bool {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		return false
	}
	return claims.Role == "owner" || claims.Role == "admin"
}

// maskSensitiveEnv 对 MCPPlugin.Env 中疑似敏感字段做脱敏处理
func maskSensitiveEnv(plugins []MCPPlugin) {
	sensitiveKeys := []string{"key", "secret", "token", "password", "api_key", "apikey"}
	for i := range plugins {
		for k, v := range plugins[i].Env {
			if len(v) <= 4 {
				continue
			}
			for _, sk := range sensitiveKeys {
				if strings.Contains(strings.ToLower(k), sk) {
					plugins[i].Env[k] = v[:2] + "***" + v[len(v)-2:]
					break
				}
			}
		}
	}
}

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
}

// pluginsFile is the on-disk structure of plugins.json.
type pluginsFile struct {
	MCPServers []MCPPlugin `json:"mcp_servers"`
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
	items, err := ListEnabledMarketItems(r.Context(), "plugin", ResolveTenantID(r))
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
	if enabled, _ := IsItemEnabledForTenant(r.Context(), "plugin", name, ResolveTenantID(r)); !enabled {
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
	// P0-S7 修复：命令必须命中白名单（默认禁用），防任意命令执行
	if err := checkPluginCommandAllowed(body.Command); err != nil {
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
			// P0-S7 修复：命令必须命中白名单（默认禁用）
			if err := checkPluginCommandAllowed(*body.Command); err != nil {
				Forbidden(w, "plugin command not allowed")
				return
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
	// P0-S7 修复：执行用户自定义命令的测试端点仅限 owner/admin
	if !isAdminRole(r) {
		Forbidden(w, "plugin test requires admin role")
		return
	}
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

	plugins, err := h.readPlugins(userID)
	if err != nil {
		InternalError(w, "failed to read plugins config")
		return
	}
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

	// P0 安全：命令白名单检查
	if err := checkPluginCommandAllowed(plugin.Command); err != nil {
		OK(w, map[string]interface{}{
			"ok": false, "message": "plugin command not allowed",
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

func (h *PluginHandler) readPlugins(userID string) ([]MCPPlugin, error) {
	data, err := os.ReadFile(h.userPluginPath(userID))
	if err != nil {
		return nil, err
	}
	var pf pluginsFile
	if err := json.Unmarshal(data, &pf); err != nil {
		return nil, err
	}
	if pf.MCPServers == nil {
		return []MCPPlugin{}, nil
	}
	return pf.MCPServers, nil
}

func (h *PluginHandler) writePlugins(userID string, plugins []MCPPlugin) error {
	if plugins == nil {
		plugins = []MCPPlugin{}
	}
	path := h.userPluginPath(userID)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	pf := pluginsFile{MCPServers: plugins}
	data, err := json.MarshalIndent(pf, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, data, 0o644)
}

var validPluginName = func() func(string) bool {
	re := regexp.MustCompile(`^[a-zA-Z0-9_.-]{1,64}$`)
	return re.MatchString
}()
