package billing

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"time"
)

// ── Types ──

// CreditChange records a credit transaction.
type CreditChange struct {
	ID        string    `json:"id"`
	UserID    string    `json:"user_id"`
	Amount    int       `json:"amount"`  // positive = credit, negative = debit
	Balance   int       `json:"balance"` // balance after transaction
	Reason    string    `json:"reason"`  // "llm_call", "image_gen", "recharge", "admin"
	CreatedAt time.Time `json:"created_at"`
}

// BillingConfig holds pricing and limits.
type BillingConfig struct {
	FreeCredits      int `json:"free_credits"`       // credits given on registration
	LLMCostPerToken  int `json:"llm_cost_per_token"` // cost per token (input)
	LLMCostPerOutput int `json:"llm_cost_per_output"`
	ImageCost        int `json:"image_cost"` // per image generation
}

var DefaultConfig = BillingConfig{
	FreeCredits:      1000,
	LLMCostPerToken:  1,  // 1 credit per 1000 input tokens
	LLMCostPerOutput: 2,  // 2 credits per 1000 output tokens
	ImageCost:        50, // 50 credits per image
}

// DailyFreeLimit is the number of free conversations per user per day.
const DailyFreeLimit = 5

// CreditEvent represents a credit balance change event.
type CreditEvent struct {
	UserID    string
	Amount    int // positive = credit, negative = debit
	Balance   int // balance after this change
	Reason    string
	Timestamp time.Time
}

// BillingObserver is notified asynchronously when credits change.
type BillingObserver interface {
	OnCreditChange(event CreditEvent)
}

// balanceCacheTTL：余额读缓存有效期。多实例下其它实例的扣/加不会更新本进程缓存，
// 短 TTL 强制回源 DB，避免余额展示与预检长期陈旧（扣减/入账本身走 PG 原子语句，不受影响）。
const balanceCacheTTL = 5 * time.Second

// balanceCacheEntry 余额读缓存条目：余额快照 + 回源时间戳。
type balanceCacheEntry struct {
	balance  int64
	loadedAt time.Time
}

// Manager handles credit operations with async observer notification.
// 写路径（扣/加）以 PG 原子语句为唯一事实源（多副本不超扣/重复扣费）；
// balances 仅作短 TTL 读缓存，供余额展示与发送前预检。
type Manager struct {
	mu        sync.RWMutex
	config    BillingConfig
	store     Store
	observers []BillingObserver
	eventCh   chan CreditEvent
	done      chan struct{}
	closeOnce sync.Once
	wg        sync.WaitGroup // waits for dispatch goroutine to exit
	balances  sync.Map       // userID → *balanceCacheEntry（读缓存，5s TTL）
}

// Store is the interface for persisting credit data.
// 余额变更与流水落库必须同事务（AtomicDeductBalance/AtomicAddBalance 自带 reason 流水），
// 异步路径不再单独写流水，避免"已扣/已加未记流水"窗口。
type Store interface {
	GetBalance(ctx context.Context, userID string) (int, error)
	SetBalance(ctx context.Context, userID string, balance int) error
	GetHistory(ctx context.Context, userID string, limit int) ([]CreditChange, error)
	DailyFreeCount(ctx context.Context, userID string) (int, error)
	MarkFreeUsage(ctx context.Context, userID, turnID string) error
	AtomicDeductBalance(ctx context.Context, userID string, amount int, reason, turnID string) (int, error)
	AtomicAddBalance(ctx context.Context, userID string, amount int, reason string) (int, error)
	RecordBillingRecord(ctx context.Context, userID, sessionID string, inputTokens, outputTokens, costCents int, turnID string) error
	PaymentStore
	// EntitlementStore 承载套餐目录与用户权益（P2-2 一次性套餐）。
	// 单独成表而不是塞进 credit_transactions：权益发放可幂等重放，余额变更不能。
	EntitlementStore
}

// NewManager creates a billing manager with the given store.
// It starts a background goroutine to dispatch events to observers.
func NewManager(store Store) *Manager {
	m := &Manager{
		store:   store,
		config:  DefaultConfig,
		eventCh: make(chan CreditEvent, 1024),
		done:    make(chan struct{}),
	}
	m.wg.Add(1)
	go m.dispatch()
	return m
}

// Subscribe registers a BillingObserver to receive credit change events.
func (m *Manager) Subscribe(obs BillingObserver) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.observers = append(m.observers, obs)
}

// Close stops the background event dispatcher and drains remaining events.
// Waits for the dispatch goroutine to fully exit before returning.
func (m *Manager) Close() {
	m.closeOnce.Do(func() {
		close(m.done)
	})
	m.wg.Wait()
}

// dispatch runs in a background goroutine, forwarding events to all observers.
func (m *Manager) dispatch() {
	defer m.wg.Done()
	for {
		select {
		case evt := <-m.eventCh:
			m.mu.RLock()
			observers := m.observers
			m.mu.RUnlock()
			for _, obs := range observers {
				obs.OnCreditChange(evt)
			}
		case <-m.done:
			// Drain remaining events before exiting
			for {
				select {
				case evt := <-m.eventCh:
					m.mu.RLock()
					observers := m.observers
					m.mu.RUnlock()
					for _, obs := range observers {
						obs.OnCreditChange(evt)
					}
				default:
					return
				}
			}
		}
	}
}

// publish sends a CreditEvent to the async channel. Non-blocking.
func (m *Manager) publish(evt CreditEvent) {
	select {
	case m.eventCh <- evt:
	default:
		slog.Warn("billing event channel full, dropping event", "user_id", evt.UserID, "reason", evt.Reason)
	}
}

// getOrLoadBalance 返回用户余额读缓存条目。
// 命中且未过期（balanceCacheTTL）直接返回；过期后回源 DB 并刷新缓存。
// DB 读取失败时回退旧缓存（容忍短时陈旧，避免余额预检误拒/服务不可用）；无缓存才返回错误。
// 无外部请求上下文，使用 Background 自建超时上下文。
func (m *Manager) getOrLoadBalance(userID string) (*balanceCacheEntry, error) {
	if v, ok := m.balances.Load(userID); ok {
		e := v.(*balanceCacheEntry)
		if time.Since(e.loadedAt) < balanceCacheTTL {
			return e, nil
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	balance, err := m.store.GetBalance(ctx, userID)
	if err != nil {
		if v, ok := m.balances.Load(userID); ok {
			// 过期但 DB 不可达：回退旧缓存保证可用性（下次调用仍会尝试回源）
			return v.(*balanceCacheEntry), nil
		}
		return nil, fmt.Errorf("load balance: %w", err)
	}
	e := &balanceCacheEntry{balance: int64(balance), loadedAt: time.Now()}
	m.balances.Store(userID, e)
	return e, nil
}

// GetBalance returns the user's current credit balance.
// 读缓存（TTL 5s 回源 DB）；多实例下至多 5s 陈旧，扣减/入账仍以 PG 原子语句为准。
func (m *Manager) GetBalance(userID string) (int, error) {
	e, err := m.getOrLoadBalance(userID)
	if err != nil {
		return 0, err
	}
	return int(e.balance), nil
}

// Deduct deducts credits from a user's balance. Returns the new balance.
// Returns an error if insufficient credits.
// P0-P1 修复：改为 PG 单语句原子扣费（UPDATE ... RETURNING），数据库为唯一
// 事实源，多副本部署下不会超扣/重复扣费；内存仅作读缓存。
// 无外部请求上下文，使用 Background 自建超时上下文（异步事件处理不阻塞请求链路）。
func (m *Manager) Deduct(userID, reason string, amount int) (int, error) {
	return m.deduct(userID, reason, amount, "")
}

// deduct 是 Deduct 的带幂等键版本（B4）：turnID 非空时同一回合只扣一次，
// 重复调用返回当前余额且不再写流水（由 credit_transactions.turn_id 唯一索引保证）。
// turnID 为空时与历史行为完全一致。
func (m *Manager) deduct(userID, reason string, amount int, turnID string) (int, error) {
	if amount <= 0 {
		return 0, fmt.Errorf("invalid deduction amount: %d", amount)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	newBalance, err := m.store.AtomicDeductBalance(ctx, userID, amount, reason, turnID)
	if err != nil {
		return 0, fmt.Errorf("insufficient credits or user not found: %w", err)
	}

	// 先更新缓存，再发布事件（缓存失败不影响扣费，只影响读取性能）
	m.setBalanceCache(userID, newBalance)

	evt := CreditEvent{
		UserID:    userID,
		Amount:    -amount,
		Balance:   newBalance,
		Reason:    reason,
		Timestamp: time.Now(),
	}

	select {
	case m.eventCh <- evt:
		// 成功发布
	case <-ctx.Done():
		slog.Warn("billing event publish timeout, event may be lost",
			"user_id", userID, "reason", reason)
		// 关键事件丢失需要告警，可以考虑写入WAL日志
	}

	return newBalance, nil
}

// setBalanceCache 本实例扣/加后立即刷新缓存（时间戳置当前，TTL 窗口内读侧即时新鲜）
func (m *Manager) setBalanceCache(userID string, balance int) {
	m.balances.Store(userID, &balanceCacheEntry{balance: int64(balance), loadedAt: time.Now()})
}

// AddCredits adds credits to a user's balance (for recharge or admin grants).
// P0-P1 修复：改为 PG 单语句原子充值，数据库为唯一事实源。
// 无外部请求上下文，使用 Background 自建超时上下文（异步事件处理不阻塞请求链路）。
func (m *Manager) AddCredits(userID, reason string, amount int) (int, error) {
	if amount <= 0 {
		return 0, fmt.Errorf("invalid credit amount: %d", amount)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	newBalance, err := m.store.AtomicAddBalance(ctx, userID, amount, reason)
	if err != nil {
		return 0, fmt.Errorf("add credits failed: %w", err)
	}
	m.setBalanceCache(userID, newBalance)
	m.publish(CreditEvent{
		UserID:    userID,
		Amount:    amount,
		Balance:   newBalance,
		Reason:    reason,
		Timestamp: time.Now(),
	})
	return newBalance, nil
}

// GetHistory returns the user's credit transaction history.
func (m *Manager) GetHistory(ctx context.Context, userID string, limit int) ([]CreditChange, error) {
	return m.store.GetHistory(ctx, userID, limit)
}

// ── 支付订单（delegate 到 PaymentStore） ──

// CreatePayment 创建一笔 pending 支付订单。
func (m *Manager) CreatePayment(ctx context.Context, p *Payment) error {
	return m.store.CreatePayment(ctx, p)
}

// GetPayment 按内部订单号查询订单。
func (m *Manager) GetPayment(ctx context.Context, id string) (*Payment, error) {
	return m.store.GetPayment(ctx, id)
}

// PackageByCode 读套餐目录（delegate 到 EntitlementStore）。
func (m *Manager) PackageByCode(ctx context.Context, code string) (*Package, error) {
	return m.store.PackageByCode(ctx, code)
}

// ActiveEntitlements 返回用户在 now 时刻仍有效的权益（delegate 到 EntitlementStore）。
func (m *Manager) ActiveEntitlements(ctx context.Context, userID string, now time.Time) ([]Entitlement, error) {
	return m.store.ActiveEntitlements(ctx, userID, now)
}

// ListPackages 返回已上架套餐目录（delegate 到 EntitlementStore）。
func (m *Manager) ListPackages(ctx context.Context) ([]Package, error) {
	return m.store.ListPackages(ctx)
}

// UserEntitlements 返回用户全部权益（含已过期），供"我的套餐"展示到期状态。
func (m *Manager) UserEntitlements(ctx context.Context, userID string) ([]Entitlement, error) {
	return m.store.UserEntitlements(ctx, userID)
}

// AdminListPackages 返回全部套餐（含已下架），供后台管理页（P2-2c）。
func (m *Manager) AdminListPackages(ctx context.Context) ([]Package, error) {
	return m.store.AdminListPackages(ctx)
}

// CreatePackage 新建套餐；主键由 store 生成，成功后可从 p.ID 取到。
func (m *Manager) CreatePackage(ctx context.Context, p *Package) error {
	return m.store.CreatePackage(ctx, p)
}

// UpdatePackage 按 id 更新套餐可编辑字段；返回 false 表示该 id 不存在。
func (m *Manager) UpdatePackage(ctx context.Context, p *Package) (bool, error) {
	return m.store.UpdatePackage(ctx, p)
}

// SetPackageEnabled 上架/下架套餐；返回 false 表示该 id 不存在。
func (m *Manager) SetPackageEnabled(ctx context.Context, id string, enabled bool) (bool, error) {
	return m.store.SetPackageEnabled(ctx, id, enabled)
}

// UpdatePaymentProvider 预下单成功后回填二维码与渠道订单号。
func (m *Manager) UpdatePaymentProvider(ctx context.Context, id, qrCode, providerOrderID string) error {
	return m.store.UpdatePaymentProvider(ctx, id, qrCode, providerOrderID)
}

// MarkPaymentFailed 标记订单支付失败。
func (m *Manager) MarkPaymentFailed(ctx context.Context, id string) error {
	return m.store.MarkPaymentFailed(ctx, id)
}

// Config returns the current billing config.
func (m *Manager) Config() BillingConfig {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.config
}

// DailyFreeCount returns how many free conversations the user has used today.
func (m *Manager) DailyFreeCount(ctx context.Context, userID string) (int, error) {
	return m.store.DailyFreeCount(ctx, userID)
}

// MarkFreeUsage records one free conversation for today.
func (m *Manager) MarkFreeUsage(ctx context.Context, userID, turnID string) error {
	return m.store.MarkFreeUsage(ctx, userID, turnID)
}

// DeductTokens deducts credits based on token usage.
func (m *Manager) DeductTokens(userID string, inputTokens, outputTokens int, turnID string) (int, error) {
	cfg := m.Config()
	cost := int((int64(inputTokens)*int64(cfg.LLMCostPerToken) + int64(outputTokens)*int64(cfg.LLMCostPerOutput)) / 1000)
	if cost < 1 {
		cost = 1
	}
	return m.deduct(userID, "llm_token", cost, turnID)
}

// RecordTokenUsage 记录企业成本中心 token 明细（billing_records）。
// 仅在 DeductTokens 实际扣费成功后调用；余额/流水已由 Deduct 同事务保障，
// 此处失败仅影响成本中心明细（记录层错误由调用方告警，不影响计费主链路）。
func (m *Manager) RecordTokenUsage(ctx context.Context, userID, sessionID string, inputTokens, outputTokens int, turnID string) error {
	cfg := m.Config()
	cost := int((int64(inputTokens)*int64(cfg.LLMCostPerToken) + int64(outputTokens)*int64(cfg.LLMCostPerOutput)) / 1000)
	if cost < 1 {
		cost = 1
	}
	return m.store.RecordBillingRecord(ctx, userID, sessionID, inputTokens, outputTokens, cost, turnID)
}
