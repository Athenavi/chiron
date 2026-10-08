package api

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"log/slog"
	"net/http"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/billing"
	"github.com/athenavi/chiron/internal/broadcast"
	"github.com/athenavi/chiron/internal/monitor"
	"github.com/athenavi/chiron/internal/session"
)

// routes_agent.go —— Agent 提交/取消/事件的路由注册。
// 由 gateway_router.go 按业务域拆分而来（纯移动：函数签名、函数体、调用点一字未改）。

// ── Agent submit/cancel/events ──

func registerAgentRoutes(
	mux *http.ServeMux,
	authMW, rlMW, publicMW, sanitizeMW routeMiddleware,
	submitHandler *SubmitHandler,
	billingMgr *billing.Manager,
	agentSem *SharedSemaphore,
	tenantResMgr *TenantResourceManager,
	eventHub *broadcast.Hub,
	sessionMgr *session.Manager,
	authenticator *auth.Authenticator,
	rpaHub *RPAHub,
	internalToken string,
	// submitTimeout 单次提交（一条 SSE 回合）的后台执行上限；
	// 来自 config.AgentSubmitTimeout，<=0 时回退 DefaultAgentTimeout。
	submitTimeout time.Duration,
) {
	mux.Handle("POST /v1/agent/approval", authMW(rlMW(http.HandlerFunc(submitHandler.SubmitApproval))))
	// 结构化提问的回答：与审批同源（引擎 ask_user 阻塞等待用户输入），此前漏注册导致 404
	mux.Handle("POST /v1/agent/answer", authMW(rlMW(http.HandlerFunc(submitHandler.SubmitAnswer))))
	// 真取消（ACP `session/cancel` 的落点）：与 approval / answer 同一套校验 —— 三者都是
	// "从外部作用于正在运行的 agent 循环"的通道，少了身份校验就成了骚扰与成本攻击的入口。
	mux.Handle("POST /v1/agent/interrupt", authMW(rlMW(http.HandlerFunc(submitHandler.SubmitInterrupt))))

	// submitHandlerFunc 提取为命名函数，用于 legacy 和 v1 双路由注册
	submitHandlerFunc := func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Content   string                 `json:"content"`
			SessionID string                 `json:"session_id"`
			LLMConfig map[string]interface{} `json:"llm_config"`
			// 工作台上下文（kb_id / agent / skill_names / workflow_id 等）：由前端
			// ChatView.buildContext 组装。此前该字段未被解析 → 前端的"带知识库/技能/
			// Agent 进入对话"在网关就被丢弃，是六大工作台互通的根断点。
			Context map[string]interface{} `json:"context"`
		}
		if err := DecodeJSON(w, r, &body); err != nil {
			BadRequest(w, "invalid request")
			return
		}
		if body.Content == "" {
			BadRequest(w, "content is required")
			return
		}
		claims := auth.GetClaims(r.Context())
		if claims == nil {
			Unauthorized(w, ErrAuthRequired)
			return
		}
		userID := claims.UserID

		// ── 会话归属校验（fail-closed）──
		//
		// session_id 来自请求体，此前这里**不校验归属**就直接落库并转发引擎：
		// SaveUserMessage 的 ensure-session 用 `ON CONFLICT (id) DO UPDATE`（只更新时间戳、
		// 不比对归属），紧接着 GetMessages 又按 session_id 直查 —— 于是知道别人的
		// session_id 就能**往那个会话写消息，并把对方历史读进自己的 LLM 上下文**（IDOR）。
		//
		// 策略与 SSE 入口（events.go 的 SSEHandler）完全一致：**新会话放行**（前端先建会话
		// 再提交、以及首条消息创建会话的路径），已存在的会话必须是本人的。
		// user_id 全局唯一，因此它同时覆盖租户维度。
		if sessionMgr != nil && body.SessionID != "" {
			sess, sessErr := sessionMgr.GetSession(r.Context(), body.SessionID)
			if sessErr != nil {
				if !errors.Is(sessErr, session.ErrSessionNotFound) {
					InternalError(w, "session check failed")
					return
				}
			} else if sess == nil || sess.UserID != userID {
				Forbidden(w, "session does not belong to the current user")
				return
			}
		}

		// Billing pre-check
		if billingMgr != nil {
			count, err := billingMgr.DailyFreeCount(r.Context(), userID)
			overFreeQuota := err != nil || count >= billing.DailyFreeLimit
			if overFreeQuota {
				if balance, balErr := billingMgr.GetBalance(userID); balErr == nil && balance <= 0 {
					JSON(w, http.StatusPaymentRequired, APIResponse{
						Success: false,
						Error:   "insufficient credits — please recharge in Billing",
					})
					return
				}
			}
		}

		// 租户 token 配额预检（企业版配额池 ent_quota_pools；fail-open，见
		// ent_quota_enforce.go）。接线位置在 billing 预检之后、run 锁与并发槽位之前：
		// 此时尚未占用任何资源，直接拒绝即可，无需回滚。
		//
		// 注意：EnforceTenantQuota 仅在配额超限时返回非 nil；其计数是"请求级近似
		// 用量"（每次调用 INCR 1，缓存缺失时先从 billing_records 回填），真实 token
		// 用量仍以计费链路为准。
		if err := EnforceTenantQuota(r.Context(), claims, 0); err != nil {
			monitor.IncQuotaExceeded()
			slog.Warn("tenant token quota exceeded, submit rejected",
				"user_id", userID, "tenant_id", claims.TenantID)
			JSON(w, http.StatusTooManyRequests, APIResponse{
				Success: false,
				Error:   "tenant token quota exceeded",
			})
			return
		}

		// Reject concurrent submits within the same session（跨实例：Redis 运行锁）
		// 修复：后台任务不得挂在 r.Context() 上——202 响应返回后客户端连接可关闭/断开，
		// 会立即取消整条 submit 链路（曾致 "request cancelled before attempt 1" 的
		// "Service temporarily unavailable"）。WithoutCancel 保留 ctx 携带值（trace 等），
		// 仅剥离取消/超时；下方独立超时兜底。
		//
		// 修复：此前硬编码 180s，比 api.DefaultAgentTimeout(300s) 更短，长回合
		// （多轮 LLM + 工具调用）必然被提前取消，表现为"思考/工具调用做一半就断"。
		// 现取配置项 AGENT_SUBMIT_TIMEOUT（默认 5 分钟），并兜底不低于 DefaultAgentTimeout。
		limit := submitTimeout
		if limit < DefaultAgentTimeout {
			limit = DefaultAgentTimeout
		}
		ctx, cancel := context.WithTimeout(context.WithoutCancel(r.Context()), limit)
		// 批 E2：每次 run 唯一 token（锁归属校验：续期/释放均需匹配，防旧 run 误删新锁）
		var rnd [12]byte
		_, _ = rand.Read(rnd[:])
		runToken := userID + "-" + hex.EncodeToString(rnd[:])
		// 同会话已有回合在跑时的拒绝提示。必须说清"在跑的是上一轮"——它很可能正卡在
		// 等待子 Agent（委派），而用户只看到"发不出去"，不知道该等还是该停。
		// 相关：python-engine/app/tools/subagent.py 的委派语义（默认后台、可被「停止」中止）。
		const sessionBusyMsg = "this session already has a turn in progress (it may be waiting on a sub-agent) — stop it or wait for it to finish"
		releaseRun, runLocked, lockErr := AcquireSessionRunLock(r.Context(), body.SessionID, runToken)
		if lockErr != nil {
			// Redis 不可用：兑底进程内防重（多实例下退化为近似限制）
			slog.Warn("session run lock degraded to in-process (redis unavailable)",
				"session_id", body.SessionID)
			if _, loaded := sessionCancels.LoadOrStore(body.SessionID, sessionCancel{userID: userID, cancel: cancel}); loaded {
				cancel()
				BadRequest(w, sessionBusyMsg)
				return
			}
		} else if !runLocked {
			cancel() // cancel the new one since there's already an active task
			BadRequest(w, sessionBusyMsg)
			return
		} else {
			// 分布式锁持有成功：登记本地 registry（供取消）
			sessionCancels.Store(body.SessionID, sessionCancel{userID: userID, cancel: cancel})
		}

		releaseSem, ok := agentSem.TryAcquire(r.Context())
		if !ok {
			sessionCancels.Delete(body.SessionID)
			cancel()
			TooManyRequests(w)
			return
		}

		// 租户并发闸门（ent_quota_pools: resource_type='concurrency'，跨副本共享计数；
		// 未配置该配额的租户恒放行，见 TenantResourceManager.Acquire）。
		// 与全局 agentSem 合并为一个释放函数：后台 goroutine 继续 defer releaseSem()
		// 即可，无需再感知第二把闸门（避免漏释放导致租户槽位泄漏）。
		releaseTenant, tenantOK := tenantResMgr.Acquire(claims.TenantID)
		if !tenantOK {
			monitor.IncRateLimitBlocked()
			slog.Warn("tenant concurrency quota exhausted, submit rejected",
				"tenant_id", claims.TenantID, "user_id", userID)
			releaseSem()
			sessionCancels.Delete(body.SessionID)
			cancel()
			TooManyRequests(w)
			return
		}
		if releaseTenant != nil {
			releaseAgentSem := releaseSem
			releaseSem = func() {
				releaseTenant()
				releaseAgentSem()
			}
		}

		// 批 E2：run 锁心跳续期（60s/次，TTL=5min）。随 submit ctx 结束/run 完成自动停止；
		// 持有实例崩溃后无续期，锁 ≤5min 自动过期，用户可重试（历史消息已持久化）。
		var stopHeartbeat chan struct{}
		if releaseRun != nil {
			stopHeartbeat = make(chan struct{})
			go func() {
				ticker := time.NewTicker(60 * time.Second)
				defer ticker.Stop()
				for {
					select {
					case <-ctx.Done():
						return
					case <-stopHeartbeat:
						return
					case <-ticker.C:
						if !RefreshSessionRunLock(ctx, body.SessionID, runToken) {
							return // 锁已易主/过期：停止续期
						}
					}
				}
			}()
		}

		// ── 工作台互通兜底：只带 agent_id 时由网关补全 Agent 配置 ──
		// 前端「带 Agent 进对话」在拉不到 /v1/agents 配置时只发 agent_id，而引擎侧
		// 只读 context.agent（dict）→ 会静默退化成"没带 Agent"。桌面端与直连 API
		// 同样只传 id，所以补全放在网关，而不是要求每个客户端都发全量配置。
		resolveAgentContext(r.Context(), body.Context, claims.TenantID, userID)

		Accepted(w, map[string]string{"status": "accepted", "session_id": body.SessionID})
		go func() {
			if releaseRun != nil {
				defer releaseRun()
			}
			if stopHeartbeat != nil {
				defer close(stopHeartbeat)
			}
			defer releaseSem()
			defer func() {
				if r := recover(); r != nil {
					slog.Error("submit handler panic", "panic", r)
				}
			}()
			defer cancel()
			defer sessionCancels.Delete(body.SessionID)
			submitHandler.HandleSubmit(ctx, userID, body.SessionID, body.Content, body.LLMConfig, body.Context)
		}()
	}
	submitMW := authMW(sanitizeMW(http.HandlerFunc(submitHandlerFunc)))
	// Legacy 路由：向前兼容旧版前端
	mux.Handle("POST /submit", submitMW)
	// 版本化路由：v1 agent submit 入口
	mux.Handle("POST /v1/agent/submit", submitMW)

	// cancelHandlerFunc 提取为命名函数，用于 legacy 和 v1 双路由注册
	cancelHandlerFunc := func(w http.ResponseWriter, r *http.Request) {
		handleCancel(w, r)
	}
	cancelMW := authMW(rlMW(http.HandlerFunc(cancelHandlerFunc)))
	mux.Handle("POST /cancel", cancelMW)
	mux.Handle("POST /v1/agent/cancel", cancelMW)

	// 实时通道统一为 SSE（events.go：Redis Stream + Pub/Sub 跨实例、Last-Event-ID 断线重放）。
	// 批 B-3′：下线 /ws/{sessionId} 与 WebSocketHub（前端仅 EventSource）；RPA 插件通道 /ws/rpa 保留。
	sseHandler := SSEHandler(eventHub, sessionMgr)
	mux.Handle("GET /events", authMW(rlMW(sseHandler)))
	mux.Handle("GET /v1/events", authMW(rlMW(sseHandler)))
	mux.HandleFunc("GET /ws/rpa", RPAWebSocketHandler(rpaHub, authenticator))
	// 浏览器 RPA 桥（Python engine → 网关 → 插件；仅共享 internal token 可调）
	mux.Handle("POST /v1/rpa/exec", rlMW(http.HandlerFunc(RPAExecHandler(rpaHub, internalToken))))
	mux.Handle("GET /v1/rpa/clients", rlMW(http.HandlerFunc(RPAClientsHandler(rpaHub, internalToken))))
}
