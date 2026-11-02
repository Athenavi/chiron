package db

import (
	"context"
	"errors"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"strings"
	"testing"

	"github.com/redis/go-redis/v9"
)

// ── P1 审计消费者崩溃恢复回归 ──
//
// 背景：主循环用 `XREADGROUP ... >` 只读新消息，处理过的消息靠 XACK 出 PEL。
// 此前**没有认领逻辑**，于是"读到但未 ACK 就宕机"的审计记录永久滞留在 PEL 里 ——
// 再也没有任何消费者能读到它。审计是合规产物，"静默少一条"比报错更糟。
//
// 这里同时钉住三件事：
//  1. 失败的消息**不能** ACK（否则等同于丢弃）；
//  2. 坏消息（缺字段/坏 JSON）要 ACK，否则它会变成永远重投的毒丸；
//  3. 认领循环真的接上了（`Start` 里启动）——否则写得再对也从不运行。

func newTestConsumer(handler func(context.Context, AuditEntry) error) *AuditConsumer {
	return &AuditConsumer{
		stream:  "test:audit:events",
		group:   "audit-processor",
		handler: handler,
	}
}

func TestAuditHandleMessageReturnsFalseOnHandlerError(t *testing.T) {
	// 返回 false = "不要 ACK"，消息留在 PEL 等 claimLoop 重投。
	c := newTestConsumer(func(context.Context, AuditEntry) error {
		return errors.New("pg down")
	})
	msg := redis.XMessage{ID: "1-0", Values: map[string]interface{}{
		"data": `{"action":"session.create","tenant_id":"1"}`,
	}}
	if ack := c.handleMessage(context.Background(), msg); ack {
		t.Fatal("handler 失败时不应 ACK：这条审计记录会被永久丢弃")
	}
}

func TestAuditHandleMessageAcksOnSuccess(t *testing.T) {
	var got AuditEntry
	c := newTestConsumer(func(_ context.Context, e AuditEntry) error {
		got = e
		return nil
	})
	msg := redis.XMessage{ID: "2-0", Values: map[string]interface{}{
		"data": `{"action":"tool.execute","user_id":"u1"}`,
	}}
	if !c.handleMessage(context.Background(), msg) {
		t.Fatal("处理成功应当 ACK")
	}
	if got.Action != "tool.execute" || got.UserID != "u1" {
		t.Fatalf("消息未被正确反序列化: %+v", got)
	}
}

func TestAuditHandleMessageAcksPoisonMessages(t *testing.T) {
	called := false
	c := newTestConsumer(func(context.Context, AuditEntry) error {
		called = true
		return nil
	})

	cases := []struct {
		name   string
		values map[string]interface{}
	}{
		{"missing data", map[string]interface{}{"other": "x"}},
		{"non-string data", map[string]interface{}{"data": 42}},
		{"bad json", map[string]interface{}{"data": "{not json"}},
	}
	for _, tc := range cases {
		msg := redis.XMessage{ID: "3-0", Values: tc.values}
		if !c.handleMessage(context.Background(), msg) {
			t.Errorf("%s: 永久损坏的消息必须 ACK，否则会在 PEL 里无限重投", tc.name)
		}
	}
	if called {
		t.Fatal("坏消息不应进入 handler")
	}
}

func TestAuditClaimConstantsAreSane(t *testing.T) {
	// 认领阈值必须大于正常处理耗时，否则会与正在处理的消息抢；
	// 上限必须 ≥1，否则"一次失败就放弃"退化成不重试。
	if auditClaimIdle <= 0 || auditClaimIdle < auditClaimEvery {
		t.Errorf("auditClaimIdle=%v 应 ≥ auditClaimEvery=%v", auditClaimIdle, auditClaimEvery)
	}
	if auditMaxDeliveries < 1 {
		t.Errorf("auditMaxDeliveries=%d 应 ≥ 1", auditMaxDeliveries)
	}
}

// callsInFunc 收集某函数体内的调用全名（末段亦可，调用方自选精度）。
func callsInFunc(t *testing.T, path, funcName string) map[string]bool {
	t.Helper()
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, path, nil, 0)
	if err != nil {
		t.Fatalf("解析 %s 失败: %v", path, err)
	}
	out := map[string]bool{}
	found := false
	ast.Inspect(file, func(n ast.Node) bool {
		fn, ok := n.(*ast.FuncDecl)
		if !ok || fn.Name == nil || fn.Name.Name != funcName || fn.Body == nil {
			return true
		}
		found = true
		ast.Inspect(fn.Body, func(inner ast.Node) bool {
			call, ok := inner.(*ast.CallExpr)
			if !ok {
				return true
			}
			sel, ok := call.Fun.(*ast.SelectorExpr)
			if !ok {
				return true
			}
			out[sel.Sel.Name] = true
			if pkg, ok := sel.X.(*ast.Ident); ok {
				out[pkg.Name+"."+sel.Sel.Name] = true
			}
			return true
		})
		return false
	})
	if !found {
		t.Fatalf("%s 里找不到函数 %s", path, funcName)
	}
	return out
}

func TestAuditClaimLoopIsWired(t *testing.T) {
	claim := callsInFunc(t, "audit.go", "claimLoop")
	for _, want := range []string{"XPendingExt", "XClaim", "XAck", "handleMessage"} {
		if !claim[want] {
			t.Errorf("claimLoop 缺少 %s：崩溃遗留的 pending 消息无法恢复", want)
		}
	}

	// 接上才算数：claimLoop 从未被启动的话，写得再对也从不运行。
	start := callsInFunc(t, "audit.go", "Start")
	if !start["claimLoop"] {
		t.Fatal("Start 没有启动 claimLoop —— 崩溃恢复形同虚设")
	}
}

func TestAuditClaimUsesIdleThreshold(t *testing.T) {
	// 认领必须按 idle 过滤（Idle / MinIdle），否则会把别的副本正在处理的消息抢走，
	// 造成审计重复落库 + 原副本 ACK 落空。
	src, err := os.ReadFile("audit.go")
	if err != nil {
		t.Fatalf("读 audit.go 失败: %v", err)
	}
	text := string(src)
	if !strings.Contains(text, "Idle:") || !strings.Contains(text, "MinIdle:") {
		t.Fatal("XPendingExt/XClaim 未设置 idle 阈值：会抢走其它副本正在处理的消息")
	}
}
