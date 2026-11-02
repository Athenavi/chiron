package db

import (
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// 这两组断言守的是「不会悄悄退回去」：
// 超时缺失不会有任何编译错误或功能性失败，只会在网络黑洞 / 锁竞争下
// 表现为请求无限挂住，而那种故障在测试环境里几乎复现不出来。

func TestApplyClientTimeoutsFillsZeroValues(t *testing.T) {
	// ParseConfig 不设 connect_timeout 时它是零值，正是「没配就无限等」的场景。
	cfg, err := pgxpool.ParseConfig("postgres://u:p@localhost:5432/db")
	if err != nil {
		t.Fatalf("ParseConfig: %v", err)
	}
	if cfg.ConnConfig.ConnectTimeout != 0 || cfg.PingTimeout != 0 {
		t.Fatalf("前置条件不成立：期望解析后两者均为零值，实际 connect=%v ping=%v",
			cfg.ConnConfig.ConnectTimeout, cfg.PingTimeout)
	}

	applyClientTimeouts(cfg)

	if cfg.ConnConfig.ConnectTimeout != defaultConnectTimeout {
		t.Errorf("connect_timeout 未补默认值：期望 %v，实际 %v",
			defaultConnectTimeout, cfg.ConnConfig.ConnectTimeout)
	}
	if cfg.PingTimeout != defaultPingTimeout {
		t.Errorf("PingTimeout 未补默认值：期望 %v，实际 %v",
			defaultPingTimeout, cfg.PingTimeout)
	}
}

func TestApplyClientTimeoutsRespectsExplicitDSN(t *testing.T) {
	// 部署方显式写死的值必须原样保留 —— 库不该在背后把它改小。
	cfg, err := pgxpool.ParseConfig("postgres://u:p@localhost:5432/db?connect_timeout=3")
	if err != nil {
		t.Fatalf("ParseConfig: %v", err)
	}
	cfg.PingTimeout = 2 * time.Second

	applyClientTimeouts(cfg)

	if cfg.ConnConfig.ConnectTimeout != 3*time.Second {
		t.Errorf("DSN 显式 connect_timeout 被覆盖：期望 3s，实际 %v", cfg.ConnConfig.ConnectTimeout)
	}
	if cfg.PingTimeout != 2*time.Second {
		t.Errorf("已设置的 PingTimeout 被覆盖：期望 2s，实际 %v", cfg.PingTimeout)
	}
}

func TestSessionTimeoutsGoIntoStartupPacket(t *testing.T) {
	cfg, err := pgxpool.ParseConfig("postgres://u:p@localhost:5432/db")
	if err != nil {
		t.Fatalf("ParseConfig: %v", err)
	}

	applied := applySessionTimeouts(cfg)

	// 三条各对应一类挂死；少一条就是少一类保护。
	want := map[string]string{
		"statement_timeout":                   "30000", // 慢查询
		"lock_timeout":                        "5000",  // 等锁
		"idle_in_transaction_session_timeout": "60000", // 事务内空闲持锁阻塞 VACUUM
	}
	for k, v := range want {
		got, ok := cfg.ConnConfig.RuntimeParams[k]
		if !ok {
			t.Errorf("RuntimeParams 缺少 %s，该类挂死将重新出现", k)
			continue
		}
		if got != v {
			t.Errorf("%s 值不符：期望 %q，实际 %q", k, v, got)
		}
	}

	// 返回值用于启动日志核对，必须恰好是实际写入的那几条。
	if len(applied) != len(want) {
		t.Errorf("applied 项数不符：期望 %d，实际 %d (%v)", len(want), len(applied), applied)
	}
}

// 守的是「不要退回 SET」这条具体退化路径。
//
// 经 PgBouncer transaction 池时，会话级 SET 出来的状态不跨事务保留
// （server_reset_query = DISCARD ALL 会清掉），只有 startup packet 里的值
// 会被 PgBouncer 在每次客户端活跃时重新应用。这条断言没有编译期保护：
// 改回 AfterConnect+SET 一样能编译、一样能跑，只在生产上悄悄失效。
func TestSessionTimeoutsMustNotRelyOnAfterConnect(t *testing.T) {
	cfg, err := pgxpool.ParseConfig("postgres://u:p@localhost:5432/db")
	if err != nil {
		t.Fatalf("ParseConfig: %v", err)
	}

	if cfg.ConnConfig.RuntimeParams == nil {
		t.Fatal("ParseConfig 未初始化 RuntimeParams，前置条件不成立")
	}
	// 关键：参数必须落在 RuntimeParams（→ startup packet），
	// 而不是依赖 AfterConnect 钩子去执行 SET。
	for _, k := range []string{
		"statement_timeout",
		"lock_timeout",
		"idle_in_transaction_session_timeout",
	} {
		if _, ok := cfg.ConnConfig.RuntimeParams[k]; ok {
			t.Fatalf("%s 在调用前已存在，测试前提不成立", k)
		}
	}

	applySessionTimeouts(cfg)

	for _, k := range []string{
		"statement_timeout",
		"lock_timeout",
		"idle_in_transaction_session_timeout",
	} {
		if _, ok := cfg.ConnConfig.RuntimeParams[k]; !ok {
			t.Errorf("%s 未进入 startup packet —— 经 PgBouncer 时该超时会静默失效", k)
		}
	}
}

func TestSessionTimeoutsRespectExplicitDSN(t *testing.T) {
	// DSN 里显式写死的值必须原样保留。
	cfg, err := pgxpool.ParseConfig(
		"postgres://u:p@localhost:5432/db?statement_timeout=1234")
	if err != nil {
		t.Fatalf("ParseConfig: %v", err)
	}

	applySessionTimeouts(cfg)

	if got := cfg.ConnConfig.RuntimeParams["statement_timeout"]; got != "1234" {
		t.Errorf("DSN 显式 statement_timeout 被覆盖：期望 1234，实际 %q", got)
	}
}
