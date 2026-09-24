package model

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

type User struct {
	ID        string    `json:"id"`
	Email     string    `json:"email"`
	Name      string    `json:"name"`
	Role      string    `json:"role"` // owner / admin / user
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

type Session struct {
	ID     string `json:"id"`
	UserID string `json:"user_id"`
	Title  string `json:"title"`
	Pinned bool   `json:"pinned"`
	Tag    string `json:"tag,omitempty"`
	// Alias 是**用户给这个会话起的别名/备注**（自由文本，≤64 字符）。
	// 与 Title 分开是有意的：Title 会被系统改写（首条消息摘要、fork 标题），
	// 而 Alias 只由用户写，且展示时优先（displayName = alias || title）。
	Alias     string    `json:"alias,omitempty"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
	// ── 分支血缘（P0：只暴露已存在的列，零迁移）──
	// ParentSessionID 非空表示"这个会话是从另一个会话分支出来的"；
	// BranchFromSeq 是分叉点（保留到源会话的第几条消息）。两者由 ForkSession 写入，
	// 是会话列表/地图"辨识分支"的唯一依据（此前只存在于 DB，没有对外暴露）。
	ParentSessionID string `json:"parent_session_id,omitempty"`
	BranchFromSeq   int    `json:"branch_from_seq,omitempty"`
}

// ConversationShare is a public snapshot of selected conversation messages
// (rendered at /share/{id} without authentication).
type ConversationShare struct {
	ID         string     `json:"id"`
	SessionID  string     `json:"session_id"`
	UserID     string     `json:"user_id"`
	Title      string     `json:"title"`
	MessageIDs []string   `json:"message_ids"`
	CreatedAt  time.Time  `json:"created_at"`
	RevokedAt  *time.Time `json:"revoked_at,omitempty"`
}

type Message struct {
	ID        string `json:"id"`
	SessionID string `json:"session_id"`
	Role      string `json:"role"` // user / assistant / system / tool
	Content   string `json:"content"`
	ToolCalls string `json:"tool_calls,omitempty"` // OpenAI 格式 tool_calls JSONB（S 修复：落库）
	// TurnID 标识消息所属回合。前端按回合分组渲染与锚定：同一回合的
	// 思考/正文/工具卡片共享同一个稳定身份，历史补丁与分页插入不得改写它。
	TurnID string `json:"turn_id,omitempty"`
	// Source 标记消息来源：空 = 用户正常输入；"subagent_followup" = 子 Agent 自动轮注入
	// （见 internal/api/agent_followup.go）。前端据此渲染成系统卡片而非用户气泡。
	Source    string    `json:"source,omitempty"`
	CreatedAt time.Time `json:"created_at"`
}

type ToolCall struct {
	ID        string    `json:"id"`
	SessionID string    `json:"session_id"`
	MessageID string    `json:"message_id"`
	ToolName  string    `json:"tool_name"`
	Input     string    `json:"input"`
	Output    string    `json:"output"`
	IsError   bool      `json:"is_error"`
	Duration  int64     `json:"duration_ms"`
	CreatedAt time.Time `json:"created_at"`
}

type Task struct {
	ID        string    `json:"id"`
	UserID    string    `json:"user_id"`
	Type      string    `json:"type"`   // llm / tool / batch
	Status    string    `json:"status"` // pending / running / completed / failed
	Payload   string    `json:"payload"`
	Result    string    `json:"result"`
	Error     string    `json:"error,omitempty"`
	Retries   int       `json:"retries"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

// ── Memory Types ──────────────────────────────────────────────────────────

// MemoryItem is a single entry in the agent's memory.
type MemoryItem struct {
	ID        string                 `json:"id"`
	Type      string                 `json:"type"` // thought, observation, tool_result, plan, summary
	Content   string                 `json:"content"`
	Metadata  map[string]interface{} `json:"metadata,omitempty"`
	CreatedAt time.Time              `json:"created_at"`
}

// WorkingMemory is the agent's short-term scratchpad for the current task.
type WorkingMemory struct {
	mu        sync.RWMutex
	sessionID string
	task      string
	items     []MemoryItem
	state     map[string]interface{}
}

func NewWorkingMemory(sessionID, task string) *WorkingMemory {
	return &WorkingMemory{
		sessionID: sessionID,
		task:      task,
		items:     make([]MemoryItem, 0),
		state:     make(map[string]interface{}),
	}
}

func (wm *WorkingMemory) Add(itemType, content string, metadata map[string]interface{}) *MemoryItem {
	wm.mu.Lock()
	defer wm.mu.Unlock()
	item := MemoryItem{
		ID:        fmt.Sprintf("mem_%d", time.Now().UnixNano()),
		Type:      itemType,
		Content:   content,
		Metadata:  metadata,
		CreatedAt: time.Now(),
	}
	wm.items = append(wm.items, item)
	return &item
}

func (wm *WorkingMemory) AddThought(thought string) { wm.Add("thought", thought, nil) }

func (wm *WorkingMemory) AddObservation(toolName, output string, success bool) {
	wm.Add("observation", output, map[string]interface{}{"tool": toolName, "success": success})
}

func (wm *WorkingMemory) GetRecent(n int) []MemoryItem {
	wm.mu.RLock()
	defer wm.mu.RUnlock()
	if n <= 0 || n >= len(wm.items) {
		result := make([]MemoryItem, len(wm.items))
		copy(result, wm.items)
		return result
	}
	result := make([]MemoryItem, n)
	copy(result, wm.items[len(wm.items)-n:])
	return result
}

func (wm *WorkingMemory) SetState(key string, value interface{}) {
	wm.mu.Lock()
	defer wm.mu.Unlock()
	wm.state[key] = value
}

func (wm *WorkingMemory) GetState(key string) interface{} {
	wm.mu.RLock()
	defer wm.mu.RUnlock()
	return wm.state[key]
}

func (wm *WorkingMemory) Summarize() string {
	wm.mu.RLock()
	defer wm.mu.RUnlock()
	var parts []string
	parts = append(parts, fmt.Sprintf("Task: %s", wm.task))
	parts = append(parts, fmt.Sprintf("Steps taken: %d", len(wm.items)))
	recent := wm.items
	if len(recent) > 3 {
		recent = recent[len(recent)-3:]
	}
	for _, item := range recent {
		content := item.Content
		if len(content) > 200 {
			content = content[:200] + "..."
		}
		parts = append(parts, fmt.Sprintf("[%s] %s", item.Type, content))
	}
	if len(wm.state) > 0 {
		stateJSON, _ := json.Marshal(wm.state)
		parts = append(parts, fmt.Sprintf("State: %s", string(stateJSON)))
	}
	return strings.Join(parts, "\n")
}

// ── Episodic Memory (cross-session) ───────────────────────────────────────

type Episode struct {
	ID        string        `json:"id"`
	Task      string        `json:"task"`
	Summary   string        `json:"summary"`
	ToolsUsed []string      `json:"tools_used"`
	Success   bool          `json:"success"`
	Duration  time.Duration `json:"duration"`
	CreatedAt time.Time     `json:"created_at"`
}

type EpisodicMemory struct {
	mu       sync.RWMutex
	episodes []Episode
	maxSize  int
}

func NewEpisodicMemory(maxSize int) *EpisodicMemory {
	if maxSize <= 0 {
		maxSize = 100
	}
	return &EpisodicMemory{
		episodes: make([]Episode, 0, maxSize),
		maxSize:  maxSize,
	}
}

func (em *EpisodicMemory) Record(ep Episode) {
	em.mu.Lock()
	defer em.mu.Unlock()
	if len(em.episodes) >= em.maxSize {
		em.episodes = em.episodes[1:]
	}
	em.episodes = append(em.episodes, ep)
}

func (em *EpisodicMemory) Recent(n int) []Episode {
	em.mu.RLock()
	defer em.mu.RUnlock()
	if n <= 0 || n >= len(em.episodes) {
		result := make([]Episode, len(em.episodes))
		copy(result, em.episodes)
		return result
	}
	result := make([]Episode, n)
	copy(result, em.episodes[len(em.episodes)-n:])
	return result
}

func (em *EpisodicMemory) FindByTool(toolName string) []Episode {
	em.mu.RLock()
	defer em.mu.RUnlock()
	if toolName == "" {
		result := make([]Episode, len(em.episodes))
		copy(result, em.episodes)
		return result
	}
	var result []Episode
	for _, ep := range em.episodes {
		for _, t := range ep.ToolsUsed {
			if t == toolName {
				result = append(result, ep)
				break
			}
		}
	}
	return result
}

// ── PostgreSQL-backed Episode Store ──

type PGEpisodeStore struct {
	pool *pgxpool.Pool
}

func NewPGEpisodeStore(pool *pgxpool.Pool) *PGEpisodeStore {
	return &PGEpisodeStore{pool: pool}
}

// Init 只读校验 episodes 表存在。
//
// 该表不属于权威迁移（数据库中也不存在），且本类型的调用方已全部移除，因此这里**不再建表**：
// schema 只由 Alembic 管理。将来若重新启用该能力，请先把它写进 migrations/versions/ 的权威迁移。
func (s *PGEpisodeStore) Init(ctx context.Context) error {
	if s.pool == nil {
		return nil
	}
	var exists bool
	if err := s.pool.QueryRow(ctx,
		`SELECT to_regclass('public.episodes') IS NOT NULL`).Scan(&exists); err != nil {
		return fmt.Errorf("check episodes table: %w", err)
	}
	if !exists {
		return fmt.Errorf("episodes table is missing; add it to the authoritative migration then run: alembic upgrade head")
	}
	return nil
}

func (s *PGEpisodeStore) Save(ctx context.Context, ep Episode) error {
	if s.pool == nil {
		return nil
	}
	_, err := s.pool.Exec(ctx,
		`INSERT INTO episodes (id, task, summary, tools_used, success, duration_ms, created_at)
		 VALUES ($1, $2, $3, $4, $5, $6, $7)
		 ON CONFLICT (id) DO UPDATE SET summary = $3, tools_used = $4, success = $5`,
		ep.ID, ep.Task, ep.Summary, ep.ToolsUsed, ep.Success, ep.Duration.Milliseconds(), ep.CreatedAt,
	)
	return err
}

func (s *PGEpisodeStore) Recent(ctx context.Context, n int) ([]Episode, error) {
	if s.pool == nil {
		return nil, nil
	}
	rows, err := s.pool.Query(ctx,
		`SELECT id, task, summary, tools_used, success, duration_ms, created_at
		 FROM episodes ORDER BY created_at DESC LIMIT $1`, n)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var episodes []Episode
	for rows.Next() {
		var ep Episode
		var durMs int64
		if err := rows.Scan(&ep.ID, &ep.Task, &ep.Summary, &ep.ToolsUsed, &ep.Success, &durMs, &ep.CreatedAt); err != nil {
			continue
		}
		ep.Duration = time.Duration(durMs) * time.Millisecond
		episodes = append(episodes, ep)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate episodes: %w", err)
	}
	return episodes, nil
}
