package api

import (
	"context"
	"testing"
)

// 问题 4 的 enforce：事件驱动回合默认降级到 `ask`。
//
// 这一组断言要证明的不是"字段被赋值了"，而是**裁决结果真的变了** ——
// 所以每个用例都从 `explicit` 一路走到 `RequiresConfirmation`，只看 map 里是不是
// `"ask"` 只能证明赋值语句执行过，证明不了 `ask` 会拦住什么。

// askModeBlocksWrite 用真实的裁决函数回答"这个档位下写操作要不要人确认"。
//
// 它与引擎侧 `guards.ToolGuard.evaluate` 同源（服务端权威见 tool_broker.go），
// 因此这条链路成立就意味着降级在真实回合里成立。
func askModeBlocksWrite(mode string) bool {
	return RequiresConfirmation(ToolLevelWrite, mode)
}

func TestEventDrivenTurnDowngradesToAsk(t *testing.T) {
	cases := []struct {
		name    string
		markers map[string]interface{}
	}{
		{"子 Agent 回投", map[string]interface{}{FollowupKey: "run-1"}},
		{"后台任务终态", map[string]interface{}{JobFollowupKey: "job:1"}},
		{"接管巡检回合", map[string]interface{}{TakeoverKey: "tk-1"}},
		{"子 Agent 的续跑轮（继承标记）", map[string]interface{}{FollowupKey: "run-1", GoalEnforceKey: true}},
	}
	for _, c := range cases {
		// 调用方（或将来某处）显式给了 yolo：事件回合仍必须收紧。
		explicit := restrictEventDrivenToolsMode(context.Background(), "u1", c.markers,
			map[string]string{"tools_mode": ModeYOLO})
		if explicit["tools_mode"] != ModeAsk {
			t.Fatalf("%s: tools_mode = %q, want %q", c.name, explicit["tools_mode"], ModeAsk)
		}
		// 渲染级证据：ask 下写操作要确认，而 yolo 下不用 —— 差别真实存在。
		if !askModeBlocksWrite(explicit["tools_mode"]) {
			t.Errorf("%s: 降级后写操作仍需确认的判据不成立", c.name)
		}
		if askModeBlocksWrite(ModeYOLO) {
			t.Errorf("%s: 前提失效 —— yolo 下写操作本不该要确认", c.name)
		}
	}
}

// 用户自己发的回合不受影响，否则"我选了 yolo 却还被拦"会变成新的
// "设置存了却不生效" —— 那正是 mode.go 里被删掉一整套 Redis 状态的原因。
func TestUserTurnKeepsToolsMode(t *testing.T) {
	explicit := restrictEventDrivenToolsMode(context.Background(), "u1",
		map[string]interface{}{"kb_id": "kb-1"},
		map[string]string{"tools_mode": ModeYOLO})
	if explicit["tools_mode"] != ModeYOLO {
		t.Fatalf("用户回合被误降级: %q", explicit["tools_mode"])
	}
	// 没带 tools_mode 时也不能凭空塞一个：那会让"未表态"变成"选了 ask"。
	empty := restrictEventDrivenToolsMode(context.Background(), "u1",
		map[string]interface{}{}, map[string]string{})
	if _, ok := empty["tools_mode"]; ok {
		t.Fatalf("用户回合被注入了 tools_mode: %#v", empty)
	}
}

// 续跑轮单独打 `goal_enforce` 标记时不算事件驱动。
//
// 那是用户**自己**那条请求的延续：把他手动开的任务在跑到一半时改成每步都确认，
// 实际效果是任务卡死，而不是安全。
func TestGoalEnforceAloneIsNotEventDriven(t *testing.T) {
	explicit := restrictEventDrivenToolsMode(context.Background(), "u1",
		map[string]interface{}{GoalEnforceKey: true},
		map[string]string{"tools_mode": ModeYOLO})
	if explicit["tools_mode"] != ModeYOLO {
		t.Fatalf("用户目标的续跑轮被误降级: %q", explicit["tools_mode"])
	}
}

// 读不到设置时**必须**保守降级（fail-closed）。
//
// 反过来的话，一次 DB 抖动就能让自动回合拿到 yolo —— 那正是这条护栏要防的事。
// 测试环境 `db.GlobalDBManager == nil`，正好是"读不到"的极端情形。
func TestEventDrivenDowngradeFailsClosed(t *testing.T) {
	if got := userToolsModeOverride(context.Background(), "u1"); got != "" {
		t.Fatalf("无 DB 时不该读出放宽档位: %q", got)
	}
	explicit := restrictEventDrivenToolsMode(context.Background(), "u1",
		map[string]interface{}{FollowupKey: "run-1"}, nil)
	if explicit["tools_mode"] != ModeAsk {
		t.Fatalf("读不到设置时必须降级到 ask: %q", explicit["tools_mode"])
	}
}

// nil map 不能 panic：`submitOnce` 里 `explicit` 总是 new 出来的，但
// `restrictEventDrivenToolsMode` 是包级函数，将来别处调用时不该依赖这个前提。
func TestRestrictEventDrivenNilExplicit(t *testing.T) {
	explicit := restrictEventDrivenToolsMode(context.Background(), "",
		map[string]interface{}{TakeoverKey: "tk"}, nil)
	if explicit == nil || explicit["tools_mode"] != ModeAsk {
		t.Fatalf("nil explicit 未被正确处理: %#v", explicit)
	}
}
