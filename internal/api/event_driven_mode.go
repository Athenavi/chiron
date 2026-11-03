package api

import (
	"context"
	"encoding/json"
	"log/slog"

	"github.com/athenavi/chiron/internal/db"
)

// 事件驱动回合的工具授权降级（roadmap §6 问题 4 的 enforce 落点）。
//
// 已决口径：**事件驱动出的回合默认降级到 `ask`**。理由是信任边界 —— 子 Agent 回投、
// 后台命令终态、接管巡检都不是人当场授权的，而它们的正文完全可能被外部内容影响
// （被审的命令自己打印的文本、子 Agent 读到的网页）。今天这些正文只包成不可信数据块
// （`<subagent-result>` / `<job-result>`），那挡的是"模型把它当指令"，挡不住
// "模型照着它去删文件" —— 后者要靠审批档位，也就是这里。
//
// 为什么放在 `submitOnce` 而不是各个调用方：调用方有三个（`agent_followup.go` 的
// 直跑与 drain 两条、`superagent_driver.go` 的接管拍），漏掉任何一处就等于开了一个后门；
// 而"这条消息是不是系统注入的"判据（`workbenchCtx` 的来源标记）只有 `submitOnce` 手上
// 是最全的。放在这里，新增事件源只要打标记就自动继承降级。

// userToolsModeOverrideKey 是 `users.settings` 里允许用户**显式放宽**事件驱动回合的键。
//
// 显式开关而不是推断：roadmap 明确要求"用户若要放宽，走显式开关，不靠事件来源看起来
// 可信来推断"。缺省不存在 = 不放宽。
const userToolsModeOverrideKey = "event_driven_tools_mode"

// eventDrivenMarkers 列出"不是人当场授权"的回合来源标记。
//
// 刻意不含 `GoalEnforceKey`：续跑轮源于用户**自己**那条请求的证据判定，用户已经在
// 首轮里授权了这条工作线的走向；把它也算成事件驱动会让长任务在跑到一半时突然
// 每个写操作都要人确认，实际效果是任务卡死。
//
// 续跑跟着首轮走：`withGoalEnforce` 复制而非替换 map，所以由 followup 起始的续跑轮
// 仍带着 `FollowupKey`，自动继承降级 —— 不需要为它单列一条。
var eventDrivenMarkers = []string{FollowupKey, JobFollowupKey, TakeoverKey}

// isEventDrivenTurn 判断这一轮是否由事件（而非人）发起。
func isEventDrivenTurn(workbenchCtx map[string]interface{}) bool {
	for _, marker := range eventDrivenMarkers {
		if _, ok := workbenchCtx[marker]; ok {
			return true
		}
	}
	return false
}

// restrictEventDrivenToolsMode 把事件驱动回合的授权档位收到 `ask`。
//
// 只对事件驱动回合生效；用户自己点的提交原样放过，否则"我选了 yolo 却还被拦"会变成
// 一个新的"设置存了却不生效"。返回新的 explicit（`explicit` 由调用方构造，可以就地改，
// 但仍返回一遍以强调"这就是最终要用于解析的那份"）。
func restrictEventDrivenToolsMode(ctx context.Context, userID string,
	workbenchCtx map[string]interface{}, explicit map[string]string) map[string]string {
	if explicit == nil {
		explicit = map[string]string{}
	}
	if !isEventDrivenTurn(workbenchCtx) {
		return explicit
	}
	if override := userToolsModeOverride(ctx, userID); override != "" {
		// 用户显式放宽过：按他的选择走，但仍记一条日志 —— 事后追问"为什么这个自动回合
		// 没要确认"时，答案要能在日志里找到，而不是只能翻设置表。
		slog.Info("event-driven turn: tools_mode relaxed by user setting",
			"user_id", userID, "tools_mode", override)
		explicit["tools_mode"] = override
		return explicit
	}
	// 覆盖而不是"仅当未设置时注入"：事件回合的 llmConfig 由系统构造，若将来有人往里
	// 塞了 tools_mode，也不能让它把降级绕过去。收紧的方向不值得信任调用方。
	explicit["tools_mode"] = ModeAsk
	return explicit
}

// userToolsModeOverride 读用户显式设置的放宽档位；读不到 / 非法一律返回空（= 不放宽）。
//
// fail-closed：设置读不出来时按"没有放宽"处理，也就是仍然降到 `ask`。
// 反过来的话，一次 DB 抖动就能让自动回合拿到 yolo —— 那正是这条护栏要防的事。
func userToolsModeOverride(ctx context.Context, userID string) string {
	if db.GlobalDBManager == nil || userID == "" {
		return ""
	}
	row, err := db.GlobalDBManager.FetchOne(ctx,
		`SELECT COALESCE(settings, '{}'::json) AS settings FROM users WHERE id = $1`, userID)
	if err != nil || row == nil {
		return ""
	}
	var settings map[string]any
	if json.Unmarshal([]byte(stringOf(row["settings"])), &settings) != nil {
		return ""
	}
	raw, _ := settings[userToolsModeOverrideKey].(string)
	if !validModes[raw] {
		// 含空串与拼错的值：都不是有效模式，按未设置处理。
		return ""
	}
	return raw
}
