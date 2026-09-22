package api

// ── 工具授权模式（ask / auto / yolo）取值字典 ──
//
// ask ：写类工具（执行 / 文件写 / git 写 / 浏览器与网络访问类）需用户批准
// auto：仅危险工具需批准（默认）
// yolo：全部自动执行（secret / 逃逸参数等硬性拦截仍然生效）
//
// **模式不由服务端存储**。此前这里是一套 Redis 存储（`session:mode:{tenant}:{session}`，
// TTL 1h + 进程内降级副本）与 `GET/POST /v1/mode` 读写接口，导致同一份状态存在三处
// （Redis、`unified_sessions.runtime`、前端 localStorage 缓存），任一处不一致就表现为
// "设置存了却不生效"。现在模式是**前端的实时状态**：随每次提交经 `llm_config.tools_mode`
// 携带，服务端只做取值校验与兜底，不做任何持久化。
//
// 裁决逻辑仍在 Python 侧（python-engine/app/agent/guards.py 的 ToolGuard）。

const (
	ModeAsk  = "ask"
	ModeAuto = "auto"
	ModeYOLO = "yolo"
)

// DefaultToolsMode 未显式携带时的模式。保持 auto：既不是最松的 yolo，
// 也不会像 ask 那样把每个写操作都拦下来打断对话。
const DefaultToolsMode = ModeAuto

var validModes = map[string]bool{ModeAsk: true, ModeAuto: true, ModeYOLO: true}

// normalizeToolsMode 校验前端携带的工具授权模式，未知/空值一律回落默认。
// 请求体是不可信输入，非法值不能直接透传给引擎。
func normalizeToolsMode(raw string) string {
	if validModes[raw] {
		return raw
	}
	return DefaultToolsMode
}
