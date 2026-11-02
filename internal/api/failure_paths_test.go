package api

import (
	"context"
	"go/ast"
	"go/parser"
	"go/token"
	"strings"
	"testing"
)

// exprName 把 `db.GlobalDBManager.Exec` 这样的调用目标还原成点分全名。
// 只看末段（`Exec`）不足以区分"事务内的 tx.Exec"和"绕过事务的 DBManager.Exec"。
func exprName(e ast.Expr) string {
	switch x := e.(type) {
	case *ast.Ident:
		return x.Name
	case *ast.SelectorExpr:
		if p := exprName(x.X); p != "" {
			return p + "." + x.Sel.Name
		}
		return x.Sel.Name
	case *ast.CallExpr:
		return exprName(x.Fun)
	case *ast.IndexExpr:
		return exprName(x.X)
	case *ast.ParenExpr:
		return exprName(x.X)
	}
	return ""
}

// recordCall 同时登记点分全名与末段名：调用方按精度自选。
func recordCall(into map[string]bool, fun ast.Expr) {
	if full := exprName(fun); full != "" {
		into[full] = true
		if i := strings.LastIndex(full, "."); i >= 0 {
			into[full[i+1:]] = true
		}
	}
}

// ── P1 失败路径归还回归 ──
//
// 这一组盯的是"任务已经发生、但记账/归还那一步在失败路径上被跳过"的三处：
//
//  1. cron 租约：任务超时/panic 时不归还，job 就被锁到 stale 阈值（20 分钟）；
//  2. sessionmap：`SPOP` 认领后进程被杀，画布永久不再落库；落库非事务则可能只写一半节点；
//  3. audit：读过的消息不 ACK 就没人再读，永久滞留在 PEL。
//
// 断言方式用 **AST** 而不是字符串匹配：这类回归最典型的写法就是"把调用注释掉"
// 或"挪进一个不会执行的 if 里"，字符串匹配对前者无效（Python 侧 tests/source_scan.py
// 记录过两次实测踩坑）。这里检查的是真实的 CallExpr。

// funcCallsIn 返回某个文件里指定函数体内出现过的被调用名（`x.Y()` 取 "Y"，`f()` 取 "f"）。
func funcCallsIn(t *testing.T, path, funcName string) map[string]bool {
	t.Helper()
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, path, nil, 0)
	if err != nil {
		t.Fatalf("解析 %s 失败: %v", path, err)
	}
	calls := map[string]bool{}
	found := false
	ast.Inspect(file, func(n ast.Node) bool {
		fn, ok := n.(*ast.FuncDecl)
		if !ok || fn.Name == nil || fn.Name.Name != funcName || fn.Body == nil {
			return true
		}
		found = true
		ast.Inspect(fn.Body, func(inner ast.Node) bool {
			if call, ok := inner.(*ast.CallExpr); ok {
				recordCall(calls, call.Fun)
			}
			return true
		})
		return false
	})
	if !found {
		t.Fatalf("%s 里找不到函数 %s —— 测试本身需要更新", path, funcName)
	}
	return calls
}

// deferredCallsIn 返回某个文件里指定函数体内**通过 defer 调用**的名字。
// 「归还必须走 defer」这条不能靠"有没有调用"来验 —— 写在函数末尾同样存在。
func deferredCallsIn(t *testing.T, path, funcName string) map[string]bool {
	t.Helper()
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, path, nil, 0)
	if err != nil {
		t.Fatalf("解析 %s 失败: %v", path, err)
	}
	out := map[string]bool{}
	ast.Inspect(file, func(n ast.Node) bool {
		fn, ok := n.(*ast.FuncDecl)
		if !ok || fn.Name == nil || fn.Name.Name != funcName || fn.Body == nil {
			return true
		}
		ast.Inspect(fn.Body, func(inner ast.Node) bool {
			def, ok := inner.(*ast.DeferStmt)
			if !ok {
				return true
			}
			// `defer func() { … }()` 这种包装体也要看到里面的调用
			ast.Inspect(def.Call, func(c ast.Node) bool {
				call, ok := c.(*ast.CallExpr)
				if !ok {
					return true
				}
				switch f := call.Fun.(type) {
				case *ast.SelectorExpr:
					out[f.Sel.Name] = true
				case *ast.Ident:
					out[f.Name] = true
				}
				return true
			})
			return true
		})
		return false
	})
	return out
}

func TestCronLeaseReleaseSurvivesCancellationAndPanic(t *testing.T) {
	calls := deferredCallsIn(t, "cron_scheduler.go", "runJob")
	if !calls["releaseLease"] {
		t.Fatal("runJob 没有用 defer 归还租约：任务 panic 或超时后该 job 会被锁到 stale 阈值，" +
			"期间手动/Webhook 触发一律 409")
	}

	// 归还本身必须脱离取消链，否则"超时"这一最常见的失败恰好让归还失效。
	rel := funcCallsIn(t, "cron_scheduler.go", "releaseLease")
	if !rel["cronReleaseContext"] {
		t.Fatal("releaseLease 没有使用 cronReleaseContext：执行上下文被取消时 UPDATE 必然失败，" +
			"租约挂在行上")
	}
}

func TestCronReleaseContextDetachesFromCancellation(t *testing.T) {
	// 纯行为断言：父 context 取消后，归还上下文仍可用。
	// 这正是 runJob 超时那一刻的状态 —— 用被取消的 ctx 发 UPDATE 会必然失败。
	parent, cancel := context.WithCancel(context.Background())
	cancel()
	if parent.Err() == nil {
		t.Fatal("前提不成立：父 context 应为已取消")
	}

	relCtx, relCancel := cronReleaseContext(parent)
	defer relCancel()
	if err := relCtx.Err(); err != nil {
		t.Fatalf("父 context 已取消时归还上下文也必须仍然可用，实际 Err=%v", err)
	}
}

func TestSessionMapFlushClaimsRecoverably(t *testing.T) {
	// 认领脚本必须"先回收过期项、再领取、领取即登记 inflight"三步齐备。
	for _, want := range []string{"ZRANGEBYSCORE", "SADD", "ZREM", "SPOP", "ZADD"} {
		if !strings.Contains(smClaimLua, want) {
			t.Errorf("smClaimLua 缺少 %q —— 认领要么不可回收，要么会重复写同一张画布：\n%s",
				want, smClaimLua)
		}
	}

	calls := funcCallsIn(t, "sessionmap.go", "smFlushOnce")
	for _, want := range []string{"smClaimDirty", "smReleaseClaim", "smCacheGetChecked"} {
		if !calls[want] {
			t.Errorf("smFlushOnce 没有调用 %s：SPOP 之后崩溃的认领无法恢复", want)
		}
	}

	// 读热层失败 ≠ 画布不存在：flush 必须用可区分错误的版本。
	if calls["smCacheGet"] && !calls["smCacheGetChecked"] {
		t.Fatal("smFlushOnce 用了不区分错误的 smCacheGet：Redis 抖动会被当成'画布不存在'而丢弃写入")
	}
}

func TestSessionMapPersistIsTransactional(t *testing.T) {
	calls := funcCallsIn(t, "sessionmap.go", "smPersist")
	if !calls["db.GlobalDBManager.WithTransaction"] && !calls["WithTransaction"] {
		t.Fatal("smPersist 没有整体事务：DELETE 成功而 INSERT 中途失败会留下缺了一半节点的画布")
	}
	// 必须是事务里的 tx.Exec。裸 DBManager.Exec 会各自提交，正是本次要修的缺陷
	// ——所以这里按**全名**判断，而不是末段的 "Exec"（tx.Exec 也会命中末段）。
	if calls["db.GlobalDBManager.Exec"] {
		t.Fatal("smPersist 仍在用非事务的 db.GlobalDBManager.Exec 写节点：先删后插必须同一事务")
	}
	if !calls["tx.Exec"] {
		t.Fatal("smPersist 事务里没有用 tx.Exec 写节点")
	}
}
