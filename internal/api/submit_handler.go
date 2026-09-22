package api

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/billing"
	"github.com/athenavi/chiron/internal/broadcast"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/id"
	"github.com/athenavi/chiron/internal/session"
	"github.com/redis/go-redis/v9"
)

// SubmitHandler proxies /submit requests to the Python AI engine.
type SubmitHandler struct {
	python     *engine.PythonClient
	sessionMgr *session.Manager
	eventHub   *broadcast.Hub
	biller     engine.Biller
}

func NewSubmitHandler(python *engine.PythonClient, sessionMgr *session.Manager, eventHub *broadcast.Hub, biller engine.Biller) *SubmitHandler {
	return &SubmitHandler{
		python:     python,
		sessionMgr: sessionMgr,
		eventHub:   eventHub,
		biller:     biller,
	}
}

// SubmitApproval proxies the user's tool-approval decision to the Python engine
// (S 安全修复：工具三态栅栏的“确认”态 — 前端确认卡片回调这里).
func (h *SubmitHandler) SubmitApproval(w http.ResponseWriter, r *http.Request) {
	var req struct {
		SessionID  string `json:"session_id"`
		ToolCallID string `json:"tool_call_id"`
		Approved   bool   `json:"approved"`
		Reason     string `json:"reason"`
		UserID     string `json:"user_id,omitempty"`
		// RunToken 由网关从 Redis 归属映射（engine:run:{session}）读出后注入，
		// 供引擎拒绝「陈旧 run」的审批（批次 4）。
		RunToken string `json:"run_token,omitempty"`
	}
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, "invalid request")
		return
	}
	if req.SessionID == "" || req.ToolCallID == "" {
		BadRequest(w, "session_id and tool_call_id are required")
		return
	}
	var out map[string]any
	// S 修复：把已验证 JWT claims 的 user_id 一并透传，供 Python 端校验
	// 来电者是否为会话 owner，防止他人代批/拒批危险工具。
	if claims := auth.GetClaims(r.Context()); claims != nil {
		req.UserID = claims.UserID
	}
	// approval 必须路由到承载该 session 运行的引擎实例：路由优先按 Redis 归属映射
	// （engine:run:{session}，见 engine.RunOwnerURL），一致性哈希仅作回退；否则
	// round-robin/哈希漂移会打到没有该 run 的实例并返回 "no active agent"。
	routeCtx := engine.WithRunAffinity(r.Context(), req.SessionID)
	// 顺带取出当前 run 的 token：引擎据此拒绝陈旧 run 的审批（映射不可用时留空，
	// 引擎只在「带上了且不匹配」时拒绝，兼容未启用归属映射的部署）。
	if rec, ok := engine.RunOwner(routeCtx, req.SessionID); ok {
		req.RunToken = rec.RunToken
	}
	if err := h.python.PostJSON(routeCtx, "/v1/agent/approval", req, &out); err != nil {
		slog.Error("approval: python proxy failed", "session", req.SessionID, "error", err)
		InternalError(w, "approval proxy failed")
		return
	}
	JSON(w, http.StatusOK, APIResponse{Success: true, Data: out})
}

// HandleSubmit proxies the submit request to Python engine and streams SSE events.
// HandleSubmit 执行一次聊天提交。workbenchCtx 是前端组装的工作台上下文
// （kb_id / agent / skill_names / workflow_id），透传给引擎消费 —— 网关不再丢弃它。
func (h *SubmitHandler) HandleSubmit(ctx context.Context, userID, sessionID, content string, llmConfig, workbenchCtx map[string]interface{}) {
	// P1 修复：与 Python 引擎 5min 客户端超时对齐，避免长任务被 180s 硬超时截断
	ctx, cancel := context.WithTimeout(ctx, DefaultAgentTimeout)
	defer cancel()
	// session 亲和：同 session 的所有流式轮次固定路由到同一引擎实例
	// （引擎 PersistentTerminal/后台任务/簿记为进程内状态，round-robin 跨实例会断链）
	ctx = engine.WithSession(ctx, sessionID)
	if sessionID != "" {
		sessionCancels.Store(sessionID, sessionCancel{userID: userID, cancel: cancel})
		defer sessionCancels.Delete(sessionID)
	}

	// 落库专用 ctx 工厂（S 修复）：不继承主 ctx 的取消/期限 —— 流可能被超时、前端断开、
	// 会话取消等截断，但已产生的消息（user/assistant/tool_call）必须写入，否则刷新后丢失。
	//
	// 关键：超时窗口必须从「每次落库那一刻」开始计时。此前只在回合开始时创建一个 10s
	// 窗口，任何长于 10s 的回合（多轮 LLM + 工具调用是常态）都会让之后的落库全部
	// `context deadline exceeded` —— 表现为思考过程、工具调用与助手回复刷新/重启后全丢。
	const storeWriteTimeout = 10 * time.Second
	storeCtxFor := func() (context.Context, context.CancelFunc) {
		return context.WithTimeout(context.WithoutCancel(ctx), storeWriteTimeout)
	}

	// ── 发送侧幂等（#4）──
	//
	// 读取侧已有 per-session 缓冲补发（events.go），解决"**断了怎么续**"；
	// 这里解决另一件事："**重复了怎么不重跑**"。
	//
	// 没有它时：网络抖动导致的重试、或用户以为没发出去而重发，都会让引擎把同一条消息
	// 执行两次 —— 表现是重复写文件、重复计费。有 `client_msg_id` 时用 Redis SETNX 做
	// 5 分钟窗口去重，命中即**不再触发引擎**；没有该字段（老客户端 / 外部调用）时行为完全不变。
	//
	// ID 由前端放进 `llm_config` 一并传（避免为它改 HandleSubmit 的签名），
	// 取出后立即删除，不让传输层字段污染引擎的任务配置。
	clientMsgID := ""
	if v, ok := llmConfig["client_msg_id"].(string); ok {
		clientMsgID = strings.TrimSpace(v)
		delete(llmConfig, "client_msg_id")
	}
	if clientMsgID != "" && db.Redis != nil && sessionID != "" {
		dedupKey := db.RedisKey("submit:dedup:" + sessionID + ":" + clientMsgID)
		// 用 `SET … NX EX`（RedisClient 接口没有类型化的 SetNX，`Do` 是本项目的既有用法）。
		// 语义要点：**只有"键已存在"才判定重复**；Redis 故障等其它错误一律放行 ——
		// 去重是优化，不该因为它自己不可用就把用户的正常提交卡住。
		res := db.Redis.Do(ctx, "SET", dedupKey, "1", "NX", "EX", "300")
		if err := res.Err(); err != nil {
			if err == redis.Nil {
				slog.Info("submit deduplicated (already processing)",
					"session", sessionID, "client_msg_id", clientMsgID)
				// 这条消息已在处理中（同一次提交的重试）：直接结束，不再触发引擎。
				return
			}
			slog.Warn("submit dedup unavailable, proceeding", "error", err)
		}
	}

	// turn 一致性（000.md 第 14 条）：本轮回合 ID，贯穿消息/工具调用/计费落库，
	// 收尾时收敛 turns 状态（completed/failed/cancelled），不再"只记日志"。
	turnID, turnIDErr := id.UUID()
	if turnIDErr != nil {
		slog.Warn("turn: generate id failed, turn tracking disabled", "error", turnIDErr)
		turnID = ""
	}
	// S 修复：上下文丢失 — 提交时立即持久化用户消息（SSE 中断/停止也不丢历史）
	{
		sctx, cancelStore := storeCtxFor()
		h.sessionMgr.SaveUserMessage(sctx, sessionID, userID, content, turnID)
		if turnID != "" {
			h.sessionMgr.CreateTurn(sctx, turnID, sessionID, userID)
		}
		cancelStore()
	}

	histMsgs := make([]map[string]string, 0)
	if hist, err := h.sessionMgr.GetMessages(ctx, sessionID, 50); err == nil && len(hist) > 0 {
		// 只保留最近 8 条消息（Python SessionStore 有完整缓存）
		const maxHistory = 8
		start := 0
		if len(hist) > maxHistory {
			start = len(hist) - maxHistory
		}
		for _, m := range hist[start:] {
			if (m.Role == "user" || m.Role == "assistant" || m.Role == "tool") && m.Content != "" {
				content := m.Content
				if m.Role == "assistant" {
					// 落库的 assistant 内容保留 [thinking]…[/thinking] 思考块（前端刷新后还原思考），
					// 但回传引擎作为 LLM 上下文时应剥离：思考只在当轮有意义，历史思考白占 token。
					content = stripThinkingBlocks(content)
					if content == "" {
						continue // 纯思考轮：剥离后无正文，跳过（避免空 assistant 消息）
					}
				}
				histMsgs = append(histMsgs, map[string]string{"role": m.Role, "content": content})
			}
		}
	}

	// 默认 max_turns，若 llm_config 中有则使用前端指定的值
	defaultMaxTurns := DefaultMaxTurns
	if llmConfig != nil {
		if mt, ok := llmConfig["max_turns"].(float64); ok && mt > 0 {
			defaultMaxTurns = int(mt)
		}
	}
	pythonReq := map[string]interface{}{
		"session_id": sessionID,
		"user_id":    userID,
		"content":    content,
		"history":    histMsgs,
		"max_turns":  defaultMaxTurns,
	}
	// P1-c：用**会话运行时状态**解析最终生效的模式/模型/provider（唯一解析链，spec §3）。
	// 前端 llm_config 视为"本次请求的显式意图"，优先级高于会话 runtime 与默认值；
	// 若前端未传（P1-e 后的目标形态），则由 runtime / 用户默认 / 全局默认依次生效。
	explicit := map[string]string{}
	if llmConfig != nil {
		if v, ok := llmConfig["mode"].(string); ok && v != "" {
			explicit["mode"] = v
		}
		if v, ok := llmConfig["model"].(string); ok && v != "" {
			explicit["model"] = v
		}
	}
	tenantID := ""
	if claims := auth.GetClaims(ctx); claims != nil {
		tenantID = claims.TenantID
		if tenantID == "" {
			tenantID = claims.UserID
		}
	}
	// 透传给引擎：`guards.load_session_mode` 用 tenant+session 拼 Redis 键
	// （`{prefix}session:mode:{tenant}:{session}`，与 mode.go 的 sessionModeKey 同源）。
	// 此前这里**没传** → 引擎侧 tenant 为空 → 兜底成 'default' → 去查一个不存在的键
	// → 会话授权模式**永远回落 auto**：表现为"工具授权设置存了却不生效"。
	// （写入端用的是 claims.TenantID，读取端必须拿到同一个值才对得上。）
	pythonReq["tenant_id"] = tenantID
	agentCfg := ResolveSessionAgentConfig(ctx, db.Redis, tenantID, userID, sessionID, explicit)
	if llmConfig == nil {
		llmConfig = map[string]interface{}{}
	}
	llmConfig["mode"] = agentCfg.mode.Value
	llmConfig["model"] = agentCfg.model.Value
	if agentCfg.provider.Value != "" {
		// 显式 provider：引擎从 llm_config.provider 读（main.py:1007 → AgentRuntime 的
		// provider_hint → gateway._select 优先命中），解决多网关同名模型抢路由
		// （如 OpenCode 与 DeepSeek 直连都提供 deepseek-*）。
		llmConfig["provider"] = agentCfg.provider.Value
	}
	pythonReq["llm_config"] = llmConfig
	// 工作台上下文原样透传：引擎侧决定如何消费（RAG 注入、技能装配、Agent 覆盖）
	if len(workbenchCtx) > 0 {
		pythonReq["context"] = workbenchCtx
	}

	events, err := h.python.RunSSE(ctx, "/v1/agent/submit", pythonReq,
		map[string]string{"X-User-ID": userID})
	if err != nil {
		slog.Error("submit: python proxy failed", "error", err)
		h.eventHub.Publish(broadcast.Event{Type: "text", SessionID: sessionID, Data: map[string]string{"content": "Service temporarily unavailable. Please try again."}})
		h.eventHub.Publish(broadcast.Event{Type: "turn_done", SessionID: sessionID, Data: map[string]string{"session_id": sessionID}})
		return
	}

	var finalContent string
	var streamErr string // 引擎回传的 error 事件内容（用于 turns.status=failed）
	var inputTokens, outputTokens int
	turnToolCallIDs := []string{} // S 修复：messages.tool_calls 列只存 tool_call id 集合（内容在 tool_calls 表）

	// P 性能：text 事件 50ms 合帧转发（公网多租户下 SSE 帧数减半，减少网关/前端处理开销）
	const textFrameInterval = 50 * time.Millisecond
	var textBuf strings.Builder
	lastTextFlush := time.Now()
	flushText := func() {
		if textBuf.Len() == 0 {
			return
		}
		payload := textBuf.String()
		textBuf.Reset()
		finalContent += payload
		h.eventHub.Publish(broadcast.Event{
			Type: "text", SessionID: sessionID,
			Data: engine.PythonEvent{Type: "text", Content: payload},
		})
		lastTextFlush = time.Now()
	}

	// ── 增量落库 ──
	// 回合进行中把已产生的思考块/正文/工具调用按节流写进同一条 assistant 消息，
	// 使刷新、断线、网关重启后都能看到已完成的部分；回合结束时用同一 id 定型，不产生重复行。
	// 此前只在回合结束才落库：长回合（多轮工具调用，常达数分钟）进行中刷新会看到"什么都没有"。
	assistantMsgID, msgIDErr := id.UUID()
	if msgIDErr != nil {
		slog.Warn("submit: generate assistant message id failed", "error", msgIDErr)
		assistantMsgID = ""
	}
	const draftInterval = 3 * time.Second
	lastDraftAt := time.Now()
	saveDraft := func(force bool) {
		if assistantMsgID == "" {
			return
		}
		if !force && time.Since(lastDraftAt) < draftInterval {
			return
		}
		lastDraftAt = time.Now()
		tcJSON, _ := json.Marshal(turnToolCallIDs)
		sctx, cancelStore := storeCtxFor()
		h.sessionMgr.UpsertAssistantMessage(sctx, sessionID, assistantMsgID, finalContent, string(tcJSON), turnID)
		cancelStore()
	}

	for evt := range events {
		// 思考事件（[thinking] 前缀）不参与合帧：过程性内容需即时逐段推送，
		// 否则毫秒级到达的 thinking 片段会被 50ms 合帧合并成整段（思考不流式）。
		isThinking := strings.HasPrefix(evt.Content, "[thinking]")
		if evt.Type == "text" && evt.Content != "" && !isThinking {
			textBuf.WriteString(evt.Content)
			if time.Since(lastTextFlush) >= textFrameInterval {
				flushText()
			}
		} else {
			flushText() // 非 text 事件先冲刷缓冲，保持顺序
			// 思考内容虽然不参与合帧，但必须计入落库文本（finalContent）：
			// 引擎按 80 字符分段下发 "[thinking]片段[/thinking]"（见 runtime.py reasoning 转发），
			// 前端历史回放依赖 splitThinking(loose) 从 content 还原思考块。
			// 此前未累加 → assistant content 只剩正文，刷新后思考永久丢失。
			if evt.Type == "text" && isThinking {
				finalContent += evt.Content
			}
			h.eventHub.Publish(broadcast.Event{Type: evt.Type, SessionID: sessionID, Data: evt})
		}
		// S 修复：工具调用过程落库（tool_call 记录 + tool_result 回填），刷新后显示一致。
		// 每次写入都用独立的短超时（storeCtxFor），避免回合跑久后写入被 deadline 掐掉。
		switch evt.Type {
		case "tool_call":
			{
				sctx, cancelStore := storeCtxFor()
				h.sessionMgr.SaveToolCall(sctx, sessionID, evt.ID, evt.Name, evt.Arguments, turnID)
				cancelStore()
			}
			if evt.ID != "" {
				turnToolCallIDs = append(turnToolCallIDs, evt.ID)
			}
		case "tool_result":
			{
				sctx, cancelStore := storeCtxFor()
				h.sessionMgr.UpdateToolCall(sctx, evt.ID, evt.Content, strings.Contains(evt.Content, `"error"`), turnID)
				cancelStore()
			}
		case "guardrail_blocked":
			// SaaS 合规：栅栏拒绝留痕（输入注入/输出泄露/工具 block 审计）
			{
				sctx, cancelStore := storeCtxFor()
				h.sessionMgr.SaveToolCall(sctx, sessionID,
					"guard_"+evt.ID, "guardrail",
					fmt.Sprintf(`{"reason":%q}`, evt.Content), turnID)
				cancelStore()
			}
		case "error":
			// 引擎侧异常：记录原因，回合终态判为 failed（000.md 第 14 条：失败不再静默）
			streamErr = evt.Content
		}
		if evt.InputTokens > 0 {
			inputTokens += evt.InputTokens
		}
		if evt.OutputTokens > 0 {
			outputTokens += evt.OutputTokens
		}
		// 增量落库：工具事件是关键节点，立即写；其余事件走 3s 节流
		saveDraft(evt.Type == "tool_call" || evt.Type == "tool_result" || evt.Type == "guardrail_blocked")
	}
	flushText()     // 流结束兜底冲刷
	saveDraft(true) // 定型：覆盖正常结束、被取消、断线等所有路径

	// 可观测性：区分正常结束与中断（前端断开 / 会话取消 / DefaultAgentTimeout 超时）。
	// 取消时已产生的事件仍会落库与计费（落库用 storeCtxFor 的独立上下文），此处仅记录原因。
	if err := ctx.Err(); err != nil {
		slog.Info("submit stream ended with cancellation",
			"session_id", sessionID, "error", err)
	}

	// 收尾落库/计费：用「从现在起算」的独立短超时上下文。
	// 不能用回合开始时创建的窗口 —— 回合通常远超 10s，那会让这里的写入全部超时。
	storeCtx, storeCancel := storeCtxFor()
	defer storeCancel()

	if finalContent != "" || len(turnToolCallIDs) > 0 {
		// 纯工具调用轮（无文本）也保存 assistant 消息；
		// messages.tool_calls 列只存 id 集合（内容在 tool_calls 表，避免重复存储）。
		// 复用增量落库的 message id：收尾只更新同一行，不新增重复消息。
		toolCallsJSON, _ := json.Marshal(turnToolCallIDs)
		if assistantMsgID != "" {
			h.sessionMgr.UpsertAssistantMessage(storeCtx, sessionID, assistantMsgID, finalContent, string(toolCallsJSON), turnID)
		} else {
			h.sessionMgr.SaveAssistantMessage(storeCtx, sessionID, finalContent, string(toolCallsJSON), turnID)
		}
	} else {
		// 无文本无工具：仅用户消息已由 SaveUserMessage 持久化
	}

	if inputTokens > 0 || outputTokens > 0 {
		if h.biller != nil {
			// 检查是否仍在免费额度内
			freeCount, fcErr := h.biller.DailyFreeCount(storeCtx, userID)
			if fcErr == nil && freeCount < billing.DailyFreeLimit {
				// 免费对话：记录使用，不扣费
				if markErr := h.biller.MarkFreeUsage(storeCtx, userID, turnID); markErr != nil {
					slog.Error("billing: MarkFreeUsage failed", "user", userID, "error", markErr)
				}
			} else {
				// 超出免费额度或查询失败：正常扣费
				if _, err := h.biller.DeductTokens(userID, inputTokens, outputTokens, turnID); err != nil {
					slog.Error("billing: DeductTokens failed", "user", userID, "error", err)
				} else {
					// 企业成本中心 token 明细（billing_records）；失败仅告警，不影响已扣费与流水
					if recErr := h.biller.RecordTokenUsage(storeCtx, userID, sessionID, inputTokens, outputTokens, turnID); recErr != nil {
						slog.Warn("billing: enterprise token usage record failed",
							"user", userID, "session", sessionID, "error", recErr)
					}
				}
			}
		}
	}

	// 回合终态收敛：取消(断开/超时) -> cancelled；引擎报错 -> failed；否则 completed。
	turnStatus := "completed"
	switch {
	case ctx.Err() != nil:
		turnStatus = "cancelled"
	case streamErr != "":
		turnStatus = "failed"
	}
	if turnID != "" {
		h.sessionMgr.FinishTurn(storeCtx, turnID, turnStatus, streamErr, inputTokens, outputTokens)
	}

	h.eventHub.Publish(broadcast.Event{Type: "turn_done", SessionID: sessionID, Data: map[string]string{"session_id": sessionID}})
}

// thinkingBlockRe 匹配引擎转发的思考块：runtime.py 在正文开始前按 ~80 字符分段
// yield "[thinking]片段[/thinking]"（DeepSeek thinking mode）。
var thinkingBlockRe = regexp.MustCompile(`(?s)\[thinking\].*?\[/thinking\]`)

// stripThinkingBlocks 剥离内容中的思考块与流式切割残留的孤立标签。
// 仅用于回传 LLM 的历史上下文：落库与前端渲染仍保留原始标签（splitThinking 还原思考块）。
func stripThinkingBlocks(s string) string {
	if !strings.Contains(s, "thinking]") {
		return s
	}
	s = thinkingBlockRe.ReplaceAllString(s, "")
	s = strings.ReplaceAll(s, "[thinking]", "")
	s = strings.ReplaceAll(s, "[/thinking]", "")
	return strings.TrimSpace(s)
}
