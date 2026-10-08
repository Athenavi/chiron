package api

import (
	"strings"
	"testing"
)

// 插件命令白名单的**路径规则**回归。
//
// 背景（2026-10-08）：此前只比 basename，于是 `../../tmp/npx`、`..\..\tmp\npx`（相对穿越）
// 与 `\\evil-host\share\npx`（UNC）都会命中白名单 —— 宿主机会执行**攻击者放置或远程共享**上的
// 同名二进制。Python 侧同一条规则见 `python-engine/app/tools/ssrf.py` 的 `_command_path_ok`
// （那里连 docstring 都声称拦这些写法，代码却没做，实测三种全部放行）。
func TestCheckPluginCommandAllowedPathRule(t *testing.T) {
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
		{"非白名单 basename", "/usr/bin/bash", "not in allowlist"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := checkPluginCommandAllowed(tc.command)
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

func TestCheckPluginCommandAllowedDisabledByDefault(t *testing.T) {
	t.Setenv("PLUGIN_COMMAND_ALLOWLIST", "")
	if err := checkPluginCommandAllowed("npx"); err == nil {
		t.Fatal("白名单未配置时必须禁止所有自定义插件命令（安全默认）")
	}
}
