package api

import (
	"log/slog"
	"runtime/debug"
)

// runBackgroundCycle 执行一轮后台维护任务，把 panic 限制在**这一轮**内。
//
// 为什么必须有这一层：Go 里 goroutine 中未捕获的 panic 会终止**整个进程** ——
// 不是终止那个 goroutine（这一点与 Java/Erlang 的直觉相反，线上最常见的误判）。
// 网关里这几个循环（cron 同步、画布落库、画布对账）都是进程级常驻任务，而它们
// 处理的全是**不可信输入**：用户写的 cron 表达式与 task JSON、前端传来的画布布局、
// 从 PG 读回的任意历史行。一次 nil map 写入、一次越界切片、一次空指针解引用，
// 就足够让**所有副本同时退出**——因为它们读的是同一份数据。
//
// 后果不是"少跑一轮"：k8s 会重启，但如果触发条件稳定存在（某一行数据必然触发
// 那条代码路径），就会变成 CrashLoop —— 整个平台不可用，且错误日志指向的是
// 一个与用户操作毫无关系的时间点。
//
// 因此这里的 recover 不是为了"吞掉 bug"，而是把它降级成"这一轮没做成 + 一条带栈
// 的错误日志"，并让下一轮照常开始。日志保留完整栈，问题依然可查。
func runBackgroundCycle(name string, fn func()) {
	defer func() {
		if r := recover(); r != nil {
			slog.Error("background cycle panicked; continuing with next cycle",
				"cycle", name, "panic", r, "stack", string(debug.Stack()))
		}
	}()
	fn()
}
