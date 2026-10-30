package api

// 本文件由 plugin_handler.go 按职责外移而来（插件命令与参数校验，全为纯函数）。
// 代码逐字未改：只搬位置，不改逻辑。

import (
	"fmt"
	"os"
	"strings"
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
// validatePluginCommandShape 校验命令**形态**（与授权无关，始终生效）：
// 只接受裸 basename（走 PATH）或绝对路径，拒绝相对路径、`..` 段与 UNC。
func validatePluginCommandShape(command string) error {
	if strings.TrimSpace(command) == "" {
		return fmt.Errorf("command is required")
	}
	if !pluginCommandPathAllowed(command) {
		return fmt.Errorf("plugin command %q must be a bare basename or an absolute path "+
			"(relative paths, '..' and UNC are rejected)", command)
	}
	return nil
}
// checkOperatorAllowlist 运维级命令白名单（**可选上界**）。
//
// 未配置 PLUGIN_COMMAND_ALLOWLIST ⇒ 不下发限制（授权交给**用户级**总开关 + 每插件确认）；
// 配置了 ⇒ 命令 basename 必须命中，运维可用它对整台网关收紧可执行命令集。
func checkOperatorAllowlist(command string) error {
	allowed := allowedPluginCommands()
	if allowed == nil {
		return nil
	}
	// ⚠ **不能用 `filepath.Base`**：它是**平台相关**的 —— Linux 上 `\` 不是路径分隔符，
	// 于是 `C:\tools\npx` 的 basename 会变成整个字符串，在 CI（Linux）上被判"不在白名单"，
	// 而本机（Windows）却是绿的。命令字符串来自**运维配置**，不保证与运行平台同源，
	// 所以这里自己按**两种分隔符**取 basename。
	base := command
	if i := strings.LastIndexAny(command, `/\`); i >= 0 {
		base = command[i+1:]
	}
	if !allowed[base] {
		return fmt.Errorf("plugin command %q not in operator allowlist (PLUGIN_COMMAND_ALLOWLIST)", base)
	}
	return nil
}
// checkPluginExecutionAllowed 是**执行**插件前的完整闸门（deny-by-default，四层）：
//
//  1. 命令形态合法（validatePluginCommandShape）；
//  2. 运维可选上界命中（checkOperatorAllowlist，未配置即不限制）；
//  3. 用户级总开关 commands_enabled 已开启；
//  4. 该插件已被用户**一次性授权**（approved）。
//
// 创建/更新插件**不**走这里 —— 允许保存配置（未授权状态），只有真正执行才需全部通过。
func checkPluginExecutionAllowed(plugin MCPPlugin, commandsEnabled bool) error {
	if err := validatePluginCommandShape(plugin.Command); err != nil {
		return err
	}
	if err := checkOperatorAllowlist(plugin.Command); err != nil {
		return err
	}
	if !commandsEnabled {
		return fmt.Errorf("plugin command execution is disabled for this user: enable it in the plugin settings first")
	}
	if !plugin.Approved {
		return fmt.Errorf("plugin %q is not approved: approve it before executing", plugin.Name)
	}
	return nil
}
// checkPluginArgsAllowed 对插件参数做与命令同口径的防御性校验。
//
// 真正的边界是 PLUGIN_COMMAND_ALLOWLIST（**默认禁用**执行，见 checkPluginCommandAllowed），
// 且 exec.CommandContext 不走 shell ⇒ 不存在命令注入。这里只挡两类"参数本身即越权"的
// 形态：NUL/换行（可截断下游解析器）与路径穿越（可让白名单命令读写任意路径）。
func checkPluginArgsAllowed(args []string) error {
	for i, a := range args {
		if strings.ContainsAny(a, "\x00\r\n") {
			return fmt.Errorf("plugin arg #%d contains control characters", i)
		}
		if a == ".." || strings.HasPrefix(a, "../") || strings.HasSuffix(a, "/..") ||
			strings.Contains(a, "/../") {
			return fmt.Errorf("plugin arg #%d contains path traversal", i)
		}
	}
	return nil
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
