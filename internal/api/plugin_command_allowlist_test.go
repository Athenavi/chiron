package api

import (
	"strings"
	"testing"
)

// 插件命令的校验分两层：
//   - **形态**（validatePluginCommandShape）：始终生效；
//   - **运维白名单**（checkOperatorAllowlist，PLUGIN_COMMAND_ALLOWLIST）：**可选上界** ——
//     未配置即不限制，配置了则命令 basename 必须命中。
//
// 创建/更新走这两层的组合（下面 checkPluginCreateAllowed 模拟）；真正**执行**另加
// 用户级总开关 + 每插件一次性授权，见 checkPluginExecutionAllowed。
//
// 背景（2026-10-08）：此前只比 basename，于是 `../../tmp/npx`、`..\..\tmp\npx`（相对穿越）
// 与 `\\evil-host\share\npx`（UNC）都会命中白名单 —— 宿主机会执行**攻击者放置或远程共享**上的
// 同名二进制。Python 侧同一条规则见 `python-engine/app/tools/ssrf.py` 的 `_command_path_ok`。
func checkPluginCreateAllowed(command string) error {
	if err := validatePluginCommandShape(command); err != nil {
		return err
	}
	return checkOperatorAllowlist(command)
}

func TestPluginCommandPathRule(t *testing.T) {
	t.Setenv("PLUGIN_COMMAND_ALLOWLIST", "npx")

	cases := []struct {
		name    string
		command string
		wantErr string // 空串 = 应放行
	}{
		{"裸 basename", "npx", ""},
		{"POSIX 绝对路径", "/usr/local/bin/npx", ""},
		{"Windows 绝对路径", `C:\tools\npx`, ""},
		{"相对穿越", "../../tmp/npx", "absolute path"},
		{"Windows 穿越", `..\..\tmp\npx`, "absolute path"},
		{"UNC 共享", `\\evil-host\share\npx`, "absolute path"},
		{"相对路径带分隔符", "./npx", "absolute path"},
		{"非白名单 basename", "/usr/bin/bash", "not in operator allowlist"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := checkPluginCreateAllowed(tc.command)
			if tc.wantErr == "" {
				if err != nil {
					t.Fatalf("%q 应放行，得到错误：%v", tc.command, err)
				}
				return
			}
			if err == nil {
				t.Fatalf("%q 应被拒绝（%s），却放行了", tc.command, tc.wantErr)
			}
			if !strings.Contains(err.Error(), tc.wantErr) {
				t.Fatalf("%q 的拒绝理由应含 %q，得到：%v", tc.command, tc.wantErr, err)
			}
		})
	}
}

// 运维白名单未配置 = 可选上界不生效：**不**限制命令（授权交给用户级闸），
// 但命令形态仍须合法。
func TestOperatorAllowlistUnsetDoesNotRestrict(t *testing.T) {
	t.Setenv("PLUGIN_COMMAND_ALLOWLIST", "")
	if err := checkOperatorAllowlist("npx"); err != nil {
		t.Fatalf("未配置 PLUGIN_COMMAND_ALLOWLIST 时不应限制命令，得到：%v", err)
	}
	if err := checkPluginCreateAllowed("npx"); err != nil {
		t.Fatalf("未配置白名单时合法形态应放行，得到：%v", err)
	}
	// 形态非法仍拒（与白名单无关）。
	if err := checkPluginCreateAllowed("../npx"); err == nil {
		t.Fatal("相对路径必须仍被拒绝（形态校验）")
	}
}

// 执行闸是 deny-by-default 的四层。这里覆盖用户级两层 + 运维上界。
func TestPluginExecutionGate(t *testing.T) {
	t.Setenv("PLUGIN_COMMAND_ALLOWLIST", "")
	ready := MCPPlugin{Name: "p", Command: "npx", Approved: true}

	if err := checkPluginExecutionAllowed(ready, true); err != nil {
		t.Fatalf("总开关开启 + 已授权应放行，得到：%v", err)
	}
	if err := checkPluginExecutionAllowed(ready, false); err == nil {
		t.Fatal("用户总开关未开启时必须拒绝（deny-by-default）")
	}
	notApproved := ready
	notApproved.Approved = false
	if err := checkPluginExecutionAllowed(notApproved, true); err == nil {
		t.Fatal("插件未一次性授权时必须拒绝")
	}

	// 运维上界：配置后，白名单之外的命令即使总开关/授权都具备也要拒。
	t.Setenv("PLUGIN_COMMAND_ALLOWLIST", "uvx")
	if err := checkPluginExecutionAllowed(ready, true); err == nil {
		t.Fatal("命令不在运维白名单内时必须拒绝")
	}
}
