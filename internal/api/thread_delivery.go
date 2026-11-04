package api

// P1-1 投递层的准入决策（roadmap P1-1.1）。
//
// 裁决是**串行排队**：同一会话短时间收到多个 thread 事件时进队列，跑完一个接着下一个。
// 排队带来一个必须显式回答的问题 —— 队可以排多长？答案是有限度的，理由是这里：
// 一个 run 可能积压的量级就是 20 条左右，再多说明这个会话根本消化不完，
// 继续攒只会把延迟推到没人能接受的地步（用户看到的是"事件早就发生过，很久之后才生效"）。
//
// 超限时**落 dropped 并写审计**，不静默丢。静默丢是最糟的一种：事件消失了，
// 队列长度看着正常，没人知道少了什么 —— 而审计能让"少了一条"这件事可被追问。
const (
	maxPendingThreadEvents = 20

	// threadDeliveryDroppedAudit 是超限丢弃的审计动作名。
	// 与 `superagent.thread.*` 同一命名空间：都是"系统替用户决定了一件事"。
	threadDeliveryDroppedAudit = "superagent.thread.dropped_overflow"
)

// threadDeliveryDecision 是一次入站事件该落到哪个状态。
type threadDeliveryDecision string

const (
	threadDeliverQueued  threadDeliveryDecision = "queued"
	threadDeliverDropped threadDeliveryDecision = "dropped"
)

// decideThreadDelivery 判断新事件能不能进队。
//
// `pending` 是**该会话**当前待处理数（`status='queued'`），不含正在跑的那一条：
// running 已经占着运行锁，它不需要排队位，把它算进来会让上限随并发数漂移。
//
// 判据是纯函数而不是内嵌在 SQL 里，理由与 `budgetExhausted` 相同：
// "20 到底能不能进"必须有一个唯一、可测的答案。写成 `>=` 而不是 `>` 也是有意的 ——
// 上限 20 意味着最多 20 条在等，第 21 条就该被拒；写成 `>` 会变成 21 条。
func decideThreadDelivery(pending int) threadDeliveryDecision {
	if pending < 0 {
		// 负数是调用方算错了（多半是并发读到了不一致的快照）。此时**不丢**
		// 是更安全的一侧：丢事件不可逆，多排一条只是延迟。
		return threadDeliverQueued
	}
	if pending >= maxPendingThreadEvents {
		return threadDeliverDropped
	}
	return threadDeliverQueued
}
